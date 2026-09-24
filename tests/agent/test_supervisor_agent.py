import json

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from agents.usage import Usage

from sanctions_agent import review
from sanctions_agent.agent.supervisor import SupervisorAgent
from sanctions_agent.db.engine import fetch_all, fetch_one, fetch_val, tx
from sanctions_agent.ops import system_settings
from tests.integration.test_pipeline import seeded  # noqa: F401

REPORT = json.dumps(
    {
        "summary": "UN pulled early; ICIJ is disabled on purpose.",
        "health": "healthy",
        "actions": ["queued un_sc"],
        "escalations": [],
        "followups": ["check un_sc run"],
    }
)


def usage(i=1200, o=150):
    return Usage(requests=1, input_tokens=i, output_tokens=o, total_tokens=i + o)


def step_call(name, args, cid):
    return ModelStep(output=[function_call(name, args, call_id=cid)], usage=usage())


def test_cycle_uses_guarded_tools_and_is_fully_recorded(seeded):  # noqa: F811
    model = ScriptedModel(
        [
            step_call("get_error_history", {"source_id": "un_sc", "hours": 24}, "c1"),
            step_call(
                "run_ingestion",
                {"source_id": "un_sc", "reason": "publisher signal", "force_refetch": False},
                "c2",
            ),
            step_call("run_ingestion", {"source_id": "icij", "reason": "try", "force_refetch": False}, "c3"),
            ModelStep(output=[assistant_message(REPORT)], usage=usage(800, 120)),
        ]
    )
    out = SupervisorAgent(model=model).run_cycle("tick", ["periodic health review"])
    assert out["status"] == "SUCCEEDED" and out["report"]["health"] == "healthy"
    assert out["tool_calls"] == 3 and out["cost_usd"] > 0
    first_input = model.calls[0].input
    assert "un_sc" in json.dumps(first_input, default=str)  # state snapshot was provided up front
    with tx() as conn:
        actions = fetch_all(
            conn,
            "SELECT tool, guard_verdict, result_summary FROM agent_action WHERE cycle_id = %s ORDER BY seq",
            (out["cycle_id"],),
        )
        assert [(a["tool"], a["guard_verdict"]) for a in actions] == [
            ("get_error_history", "ALLOWED"),
            ("run_ingestion", "ALLOWED"),
            ("run_ingestion", "DENIED"),
        ]
        assert "DISABLED" in actions[2]["result_summary"]["denied"]
        run = fetch_one(conn, "SELECT * FROM ingestion_run WHERE source_id = 'un_sc'")
        assert run["trigger"] == "AGENT" and str(run["agent_cycle_id"]) == out["cycle_id"]
        cyc = fetch_one(conn, "SELECT * FROM agent_cycle WHERE cycle_id = %s", (out["cycle_id"],))
        assert (
            cyc["status"] == "SUCCEEDED"
            and cyc["input_tokens"] == 3 * 1200 + 800
            and not cyc["fallback_used"]
        )
        assert fetch_val(conn, "SELECT count(*) FROM agent_span WHERE trace_id = %s", (cyc["trace_id"],)) > 0


def test_budget_exceeded_makes_no_model_call(seeded):  # noqa: F811
    with tx() as conn:
        system_settings.set_value(conn, "agent_daily_budget_usd", 0.0, "admin")
    model = ScriptedModel([])
    out = SupervisorAgent(model=model).run_cycle("tick", ["x"])
    assert out["status"] == "BUDGET_EXCEEDED" and model.calls == ()


def test_model_failure_is_recorded_for_fallback(seeded):  # noqa: F811
    model = ScriptedModel([ModelStep.raise_error(RuntimeError("upstream 503"))])
    out = SupervisorAgent(model=model).run_cycle("tick", ["x"])
    assert out["status"] == "FAILED" and "upstream 503" in out["error"]
    with tx() as conn:
        assert fetch_val(
            conn, "SELECT fallback_used FROM agent_cycle WHERE cycle_id = %s", (out["cycle_id"],)
        )


def test_agent_can_only_propose_config_and_humans_approve(seeded):  # noqa: F811
    patch = json.dumps(
        {"fetch": {"url": "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List-2026.xml"}}
    )
    model = ScriptedModel(
        [
            step_call(
                "propose_config_change",
                {
                    "source_id": "uk_fcdo",
                    "json_patch": patch,
                    "rationale": "404 on old URL; landing page links new file",
                    "evidence_url": "https://www.gov.uk/government/publications/the-uk-sanctions-list",
                },
                "c1",
            ),
            step_call(
                "propose_config_change",
                {
                    "source_id": "uk_fcdo",
                    "json_patch": json.dumps({"fetch": {"url": "https://evil.example.com/x.xml"}}),
                    "rationale": "x",
                    "evidence_url": None,
                },
                "c2",
            ),
            ModelStep(output=[assistant_message(REPORT)], usage=usage()),
        ]
    )
    out = SupervisorAgent(model=model).run_cycle("tick", ["1 new incidents without diagnosis"])
    with tx() as conn:
        acts = fetch_all(
            conn,
            "SELECT guard_verdict, result_summary FROM agent_action WHERE cycle_id = %s ORDER BY seq",
            (out["cycle_id"],),
        )
        assert acts[0]["guard_verdict"] == "ALLOWED" and acts[1]["guard_verdict"] == "DENIED"
        prop = fetch_one(conn, "SELECT * FROM proposed_change WHERE kind = 'CONFIG_CHANGE'")
        assert prop["proposed_by"] == "agent:supervisor" and prop["status"] == "PENDING"
        assert fetch_val(
            conn, "SELECT config->'fetch'->>'url' FROM source WHERE source_id = 'uk_fcdo'"
        ).endswith("UK-Sanctions-List.xml")  # unchanged until approved
    with tx(actor="alice") as conn:
        review.decide(conn, prop["change_id"], reviewer="alice", role="admin", approve=True)
    with tx() as conn:
        assert fetch_val(
            conn, "SELECT config->'fetch'->>'url' FROM source WHERE source_id = 'uk_fcdo'"
        ).endswith("2026.xml")


def test_per_cycle_enqueue_limit(seeded):  # noqa: F811
    from sanctions_agent.agent.context import ActionLimits

    steps = [
        step_call("run_ingestion", {"source_id": s, "reason": "r", "force_refetch": False}, f"c{i}")
        for i, s in enumerate(["un_sc", "uk_fcdo", "eu_fsf"])
    ]
    model = ScriptedModel([*steps, ModelStep(output=[assistant_message(REPORT)], usage=usage())])
    out = SupervisorAgent(model=model, limits=ActionLimits(max_enqueues=2)).run_cycle("tick", ["x"])
    with tx() as conn:
        verdicts = [
            r["guard_verdict"]
            for r in fetch_all(
                conn,
                "SELECT guard_verdict FROM agent_action WHERE cycle_id = %s ORDER BY seq",
                (out["cycle_id"],),
            )
        ]
    assert verdicts == ["ALLOWED", "ALLOWED", "DENIED"]


@pytest.mark.llm
def test_live_supervisor_cycle(seeded):  # noqa: F811
    """Runs a real gpt-5-mini cycle (needs OPENAI_API_KEY and network)."""
    out = SupervisorAgent().run_cycle("tick", ["periodic health review"])
    assert out["status"] == "SUCCEEDED"
