"""Analyst Q&A agent: aggregate-only by construction (SQL guard, DB grants, input guardrail) and grounded answers."""

from __future__ import annotations

import json

import psycopg
import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from agents.usage import Usage

from sanctions_agent.agent.analyst import REFUSAL, AnalystAgent, is_row_level
from sanctions_agent.agent.sql_guard import MAX_LIMIT, SqlRejected, guard
from sanctions_agent.db.engine import fetch_all, fetch_one, fetch_val, tx
from sanctions_agent.settings import get_settings
from tests.helpers import load_fixture
from tests.integration.test_pipeline import seeded  # noqa: F401


# ------------------------------------------------------------------ SQL guard (unit) --------------------
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT source_id, health FROM v_source_status",
        "select source_id, count(*) from analytics.v_run_summary where status = 'FAILED' group by 1",
        "WITH x AS (SELECT source_id, fill_rate FROM v_fill_rates WHERE field = 'dob') SELECT * FROM x",
        "SELECT round(avg(duration_seconds), 1) FROM v_run_summary UNION SELECT 1",
        "SELECT trigger, status, count(*) FROM v_run_batches GROUP BY 1, 2",
    ],
)
def test_sql_guard_accepts_aggregate_view_queries(sql):
    out = guard(sql)
    assert "LIMIT" in out.upper()


@pytest.mark.parametrize(
    "sql, why",
    [
        ("DELETE FROM v_source_status", "SELECT"),
        ("SELECT 1; SELECT 2", ""),
        ("SELECT * FROM sanctions.record_version", "analytics"),
        ("SELECT full_name FROM rv_name", "rv_name"),
        ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
        ("SELECT set_config('search_path', 'sanctions', false)", "set_config"),
        ("SELECT * FROM pg_catalog.pg_authid", ""),
        ("SELECT * FROM v_source_status FOR UPDATE", ""),
    ],
)
def test_sql_guard_rejects(sql, why):
    with pytest.raises(SqlRejected) as e:
        guard(sql)
    assert why.lower() in str(e.value).lower()


def test_sql_guard_caps_limit():
    assert f"LIMIT {MAX_LIMIT}" in guard("SELECT * FROM v_source_status LIMIT 100000").upper()


# ------------------------------------------------------------------ guardrail (unit) --------------------
@pytest.mark.parametrize(
    "q",
    [
        "Is Viktor Bout sanctioned?",
        "List the names of all vessels added this week",
        "What is the passport number of Haqqani?",
        "Show me the records for Rosneft",
        "Give me the IMO numbers on Annex XLII",
    ],
)
def test_row_level_questions_are_detected(q):
    assert is_row_level(q)


@pytest.mark.parametrize(
    "q",
    [
        "How many vessels were added across lists in the last 7 days?",
        "What's the DOB fill rate for UN individuals?",
        "Which lists carry MMSI numbers?",
        "Why did the last EU FSF run fail?",
        "How many passport numbers fail validation on the UK list?",
    ],
)
def test_aggregate_questions_pass(q):
    assert not is_row_level(q)


# ------------------------------------------------------------------ DB grants -------------------------------
def test_analyst_role_reads_views_but_not_raw_tables(db):
    url = get_settings().analyst_database_url
    with psycopg.connect(url) as conn:
        assert conn.execute("SELECT count(*) FROM analytics.v_source_status").fetchone() is not None
        assert conn.execute("SELECT count(*) FROM analytics.v_run_batches").fetchone() is not None
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM sanctions.record_version")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT full_name FROM sanctions.rv_name")
        conn.rollback()
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("CREATE TABLE analytics.leak (x int)")


def test_analytics_views_expose_no_record_values(db):
    with tx() as conn:
        cols = {
            r["column_name"]
            for r in fetch_all(
                conn, "SELECT column_name FROM information_schema.columns WHERE table_schema = 'analytics'"
            )
        }
    for forbidden in (
        "full_name",
        "primary_name",
        "value_raw",
        "value_norm",
        "street",
        "date_value",
        "doc",
        "source_key",
        "verbatim_quote",
        "name_parts",
    ):
        assert forbidden not in cols, forbidden


# ------------------------------------------------------------------ agent runs ------------------------------
def usage(i: int = 900, o: int = 150) -> Usage:
    return Usage(requests=1, input_tokens=i, output_tokens=o, total_tokens=i + o)


def test_row_level_question_is_refused_without_a_model_call(db):
    model = ScriptedModel([])
    res = AnalystAgent(model=model).ask("Is Viktor Bout sanctioned?", user="alice")
    assert res["refused"] and res["answer_markdown"] == REFUSAL and not model.calls
    with tx() as conn:
        c = fetch_one(conn, "SELECT * FROM agent_cycle WHERE cycle_id = %s", (res["cycle_id"],))
        assert c["agent_name"] == "analyst" and c["status"] == "SUCCEEDED" and c["report"]["refused"] is True


def test_answer_is_grounded_in_tool_results_with_citations(seeded):  # noqa: F811
    load_fixture("un_sc", "un/consolidated_v1.xml")
    answer = {
        "answer_markdown": "The UN list's **DOB fill rate for individuals is 100%** on the current version.",
        "citations": [{"source": "get_fill_rates / v_fill_rates", "as_of": "2026-09-24T10:00:00Z"}],
        "chart": {
            "type": "bar",
            "title": "DOB fill rate",
            "y_label": "share",
            "labels": ["un_sc"],
            "series": [{"name": "dob", "values": [1.0]}],
        },
    }
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        "get_fill_rates",
                        {"source_id": "un_sc", "entity_type": "PERSON", "field": "dob"},
                        call_id="c1",
                    )
                ],
                usage=usage(),
            ),
            ModelStep(output=[assistant_message(json.dumps(answer))], usage=usage(1200, 200)),
        ]
    )
    res = AnalystAgent(model=model).ask("What's the DOB fill rate for UN individuals?", user="alice")
    assert res["status"] == "SUCCEEDED" and not res["refused"]
    assert res["citations"][0]["source"].startswith("get_fill_rates") and res["chart"]["type"] == "bar"
    # the tool really ran against the analytics views, as the read-only role
    tool_out = json.dumps(model.calls[1].input, default=str)
    assert "fill_rate" in tool_out and "function_call_output" in tool_out and "un_sc" in tool_out
    with tx() as conn:
        acts = fetch_all(
            conn, "SELECT tool, guard_verdict FROM agent_action WHERE cycle_id = %s", (res["cycle_id"],)
        )
        assert [(a["tool"], a["guard_verdict"]) for a in acts] == [("get_fill_rates", "ALLOWED")]
        assert (
            fetch_val(conn, "SELECT count(*) FROM chat_message WHERE session_id = %s", (res["session_id"],))
            >= 2
        )
        cyc = fetch_one(conn, "SELECT * FROM agent_cycle WHERE cycle_id = %s", (res["cycle_id"],))
        assert cyc["input_tokens"] == 2100 and float(cyc["cost_usd"]) > 0


def test_sql_escape_hatch_cannot_reach_raw_tables(seeded):  # noqa: F811
    answer = {"answer_markdown": "I can't access that.", "citations": [], "chart": None}
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        "query_analytics_sql",
                        {"sql": "SELECT full_name FROM sanctions.rv_name"},
                        call_id="c1",
                    )
                ],
                usage=usage(),
            ),
            ModelStep(output=[assistant_message(json.dumps(answer))], usage=usage()),
        ]
    )
    res = AnalystAgent(model=model).ask("How many names are there per list?", user="bob")
    with tx() as conn:
        act = fetch_one(
            conn,
            "SELECT guard_verdict, result_summary FROM agent_action WHERE cycle_id = %s",
            (res["cycle_id"],),
        )
    assert act["guard_verdict"] == "DENIED" and "analytics" in act["result_summary"]["denied"]


def test_conversation_continues_in_the_same_session(seeded):  # noqa: F811
    a1 = {"answer_markdown": "18 sources are configured.", "citations": [], "chart": None}
    a2 = {"answer_markdown": "Of those, 2 are disabled.", "citations": [], "chart": None}
    m1 = ScriptedModel([ModelStep(output=[assistant_message(json.dumps(a1))], usage=usage())])
    r1 = AnalystAgent(model=m1).ask("How many sources are there?", user="carol")
    m2 = ScriptedModel([ModelStep(output=[assistant_message(json.dumps(a2))], usage=usage())])
    r2 = AnalystAgent(model=m2).ask("And how many are disabled?", user="carol", session_id=r1["session_id"])
    assert r2["session_id"] == r1["session_id"]
    assert "How many sources are there?" in json.dumps(m2.calls[0].input, default=str)  # history was replayed
