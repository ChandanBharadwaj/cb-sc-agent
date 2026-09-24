from datetime import UTC, datetime

import httpx
import respx

from sanctions_agent.db.engine import fetch_one, fetch_val, tx
from sanctions_agent.ops import alerts, autopilot, system_settings
from sanctions_agent.ops.metrics import DbCollector
from sanctions_agent.ops.scheduler import Supervisor
from sanctions_agent.ops.worker import WorkerPool
from sanctions_agent.pipeline.runner import PipelineRunner
from tests.integration.test_pipeline import BLOB, FX, UN_URL, seeded  # noqa: F401


def _only(conn, *keep):
    conn.execute(
        "UPDATE source SET status = 'DISABLED', status_reason = 'test' WHERE source_id <> ALL(%s)",
        (list(keep),),
    )


def test_plan_respects_status_pause_and_politeness(seeded):  # noqa: F811
    with tx() as conn:
        _only(conn, "un_sc", "ofac_sdn", "uk_fcdo")
        conn.execute(
            "UPDATE source SET status = 'PAUSED', status_reason = 'x', paused_until = now() + interval '1 hour'"
            " WHERE source_id = 'uk_fcdo'"
        )
        conn.execute("UPDATE source SET last_attempt_at = now() WHERE source_id = 'ofac_sdn'")
        rep = autopilot.run_plan(conn, autopilot.plan(conn))
    enq = {e["source_id"] for e in rep.enqueued}
    skipped = {s["source_id"]: s["why"] for s in rep.skipped}
    assert enq == {"un_sc"}
    assert "politeness" in skipped["ofac_sdn"]


def test_maintenance_mode_blocks_everything(seeded):  # noqa: F811
    with tx() as conn:
        system_settings.set_value(
            conn, "maintenance_mode", {"enabled": True, "reason": "db upgrade"}, "admin"
        )
        rep = autopilot.run_plan(conn, autopilot.plan(conn))
        assert rep.enqueued == [] and all(s["why"] == "maintenance mode is on" for s in rep.skipped)
        assert autopilot.watchdog(conn).enqueued == []


def test_signal_triggers_early_pull(seeded):  # noqa: F811
    with tx() as conn:
        _only(conn, "eu_fsf")
        conn.execute(
            "UPDATE source SET next_due_at = now() + interval '2 hours', last_attempt_at = now() - interval '1 hour'"
            " WHERE source_id = 'eu_fsf'"
        )
        conn.execute(
            "INSERT INTO signal (provider, external_id, target_source_id) VALUES ('eu_fsf_rss', 'item-1', 'eu_fsf')"
        )
        rep = autopilot.run_plan(conn, autopilot.plan(conn))
    assert rep.enqueued[0]["source_id"] == "eu_fsf"
    with tx() as conn:
        assert fetch_val(conn, "SELECT trigger FROM ingestion_run") == "SIGNAL"


def test_watchdog_pages_and_forces_pull_for_stale_core_list(seeded):  # noqa: F811
    with tx() as conn:
        _only(conn, "ofac_sdn")
        conn.execute(
            "UPDATE source SET last_success_at = now() - interval '13 hours',"
            " last_attempt_at = now() - interval '2 hours' WHERE source_id = 'ofac_sdn'"
        )
        rep = autopilot.watchdog(conn)
    assert rep.enqueued and rep.enqueued[0]["source_id"] == "ofac_sdn"
    with tx() as conn:
        inc = fetch_one(conn, "SELECT * FROM incident WHERE source_id = 'ofac_sdn'")
        assert inc["severity"] == "PAGE" and inc["error_class"] == "STALE"
        assert fetch_val(conn, "SELECT trigger FROM ingestion_run") == "WATCHDOG"


def test_no_change_alert_and_auto_resume(seeded):  # noqa: F811
    with tx() as conn:
        _only(conn, "ofac_sdn", "uk_fcdo")
        conn.execute(
            "UPDATE source SET last_success_at = now(), last_change_at = now() - interval '9 days'"
            " WHERE source_id = 'ofac_sdn'"
        )
        autopilot.watchdog(conn)
        assert (
            fetch_val(conn, "SELECT error_class FROM incident WHERE source_id = 'ofac_sdn'")
            == "STALE_NO_CHANGE"
        )
        conn.execute(
            "UPDATE source SET status = 'PAUSED', status_reason = 'x', paused_until = now() - interval '1 minute'"
            " WHERE source_id = 'uk_fcdo'"
        )
        out = autopilot.chores(conn)
        assert out["auto_resumed"] == ["uk_fcdo"]
        assert fetch_val(conn, "SELECT status FROM source WHERE source_id = 'uk_fcdo'") == "ACTIVE"


class FakeAgent:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def run_cycle(self, trigger, reasons):
        self.calls.append(reasons)
        if self.fail:
            raise RuntimeError("OpenAI unavailable")
        return {"status": "SUCCEEDED"}


def test_gate_skips_llm_when_nothing_needs_judgement(seeded):  # noqa: F811
    with tx() as conn:
        _only(conn, "un_sc")
        conn.execute(
            "INSERT INTO agent_cycle (status, trigger, started_at) VALUES ('SUCCEEDED', 'tick', now())"
        )
        assert autopilot.gate(conn, last_review_at=datetime.now(UTC), review_minutes=360) == []
        conn.execute(
            "INSERT INTO incident (source_id, error_class, severity, dedupe_key, title)"
            " VALUES ('un_sc', 'HTTP_5XX', 'WARN', 'k', 't')"
        )
        reasons = autopilot.gate(conn, last_review_at=datetime.now(UTC), review_minutes=360)
    assert reasons == ["1 new incidents without diagnosis"]


@respx.mock
def test_supervisor_tick_falls_back_when_agent_fails_and_workers_run(seeded):  # noqa: F811
    respx.get(UN_URL).mock(return_value=httpx.Response(302, headers={"Location": BLOB}))
    respx.get(BLOB).mock(
        return_value=httpx.Response(200, content=(FX / "un/consolidated_v1.xml").read_bytes())
    )
    with tx() as conn:
        _only(conn, "un_sc")
    agent = FakeAgent(fail=True)
    pool = WorkerPool(PipelineRunner(sleep=lambda s: None), concurrency=2)
    sent = []
    sup = Supervisor(pool=pool, agent=agent, alert_sender=lambda inc, ch: sent.append(inc["incident_id"]))
    out = sup.tick()
    pool.wait_idle(timeout=60)
    assert agent.calls and "periodic health review" in agent.calls[0]
    assert out["agent"]["status"] == "FAILED"
    assert [e["source_id"] for e in out["autopilot"]["enqueued"]] == ["un_sc"]
    assert len(out["claimed"]) == 1
    with tx() as conn:
        assert fetch_val(conn, "SELECT status FROM ingestion_run") == "SUCCEEDED"
    pool.shutdown()


def test_alert_dispatch_dedupes_and_realerts_pages(seeded):  # noqa: F811
    sent = []
    with tx() as conn:
        conn.execute(
            "INSERT INTO incident (source_id, error_class, severity, dedupe_key, title)"
            " VALUES ('un_sc', 'STALE', 'PAGE', 'a', 'stale'), ('eu_fsf', 'HTTP_5XX', 'WARN', 'b', 'fsf 500')"
        )
        assert alerts.dispatch(conn, lambda inc, ch: sent.append(inc["dedupe_key"])) == 2
        assert alerts.dispatch(conn, lambda inc, ch: sent.append(inc["dedupe_key"])) == 0
        conn.execute("UPDATE incident SET last_alert_at = now() - interval '2 hours'")
        assert alerts.dispatch(conn, lambda inc, ch: sent.append(inc["dedupe_key"])) == 1
    assert sent == ["a", "b", "a"]


def test_metrics_collector(seeded):  # noqa: F811
    families = {m.name: m for m in DbCollector().collect()}
    assert families["sanctions_db_up"].samples[0].value == 1
    labels = {s.labels["source_id"] for s in families["sanctions_source_active"].samples}
    assert "ofac_sdn" in labels and "gleif" in labels
