"""Run batches: every run belongs to one; scheduled cycles, agent cycles and ad-hoc requests group their runs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from agents import RunContextWrapper

from sanctions_agent.agent.context import AgentContext
from sanctions_agent.agent.tools import run_ingestion
from sanctions_agent.db.engine import fetch_all, fetch_one, fetch_val, tx
from sanctions_agent.ops import autopilot
from sanctions_agent.pipeline import runs
from tests.helpers import load_fixture
from tests.integration.test_ops import _only
from tests.integration.test_pipeline import seeded  # noqa: F401


def _batch(conn, bid):
    return fetch_one(conn, "SELECT * FROM analytics.v_run_batches WHERE batch_id = %s", (bid,))


def test_single_run_is_a_batch_of_one(seeded):  # noqa: F811
    with tx() as conn:
        rid = runs.enqueue_run(
            conn,
            source_id="un_sc",
            run_kind="LIST_INGEST",
            trigger="MANUAL",
            requested_by="alice",
            reason="check",
        )
        bid = fetch_val(conn, "SELECT batch_id FROM ingestion_run WHERE run_id = %s", (rid,))
        b = _batch(conn, bid)
    assert b["trigger"] == "MANUAL" and b["requested_by"] == "alice" and b["requested_sources"] == ["un_sc"]
    assert b["status"] == "RUNNING" and b["active"] == 1 and b["sources_run"] == 1


def test_scheduled_cycle_groups_due_sources_and_watchdog_pulls(seeded):  # noqa: F811
    with tx() as conn:
        _only(conn, "un_sc", "ofac_sdn", "uk_fcdo")
        conn.execute(
            "UPDATE source SET next_due_at = now() - interval '1 minute' WHERE source_id IN ('un_sc','ofac_sdn')"
        )
        conn.execute("UPDATE source SET next_due_at = now() + interval '1 hour' WHERE source_id = 'uk_fcdo'")
        # uk is stale past its hard ceiling -> watchdog forces a pull in the same cycle
        conn.execute(
            "UPDATE source SET last_success_at = now() - interval '2 days', created_at = now() - interval '3 days'"
            " WHERE source_id = 'uk_fcdo'"
        )
    autopilot.autopilot_cycle()
    with tx() as conn:
        rows = fetch_all(conn, "SELECT source_id, trigger, batch_id FROM ingestion_run ORDER BY source_id")
        assert {r["source_id"]: r["trigger"] for r in rows} == {
            "ofac_sdn": "SCHEDULE",
            "uk_fcdo": "WATCHDOG",
            "un_sc": "SCHEDULE",
        }
        assert len({r["batch_id"] for r in rows}) == 1
        b = _batch(conn, rows[0]["batch_id"])
        assert b["trigger"] == "SCHEDULED" and b["sources_run"] == 3
        assert sorted(b["requested_sources"]) == ["ofac_sdn", "uk_fcdo", "un_sc"]
        n_batches = fetch_val(conn, "SELECT count(*) FROM run_batch")
    autopilot.autopilot_cycle()  # nothing new is due (all three are busy): no empty batch
    with tx() as conn:
        assert fetch_val(conn, "SELECT count(*) FROM run_batch") == n_batches


def test_agent_cycle_runs_share_one_batch_and_refusals_are_recorded(seeded):  # noqa: F811
    cycle = str(uuid.uuid4())
    with tx() as conn:
        conn.execute(
            "INSERT INTO agent_cycle (cycle_id, trigger, model) VALUES (%s, 'tick', 'test')", (cycle,)
        )
    rc = RunContextWrapper(AgentContext(cycle_id=cycle))
    run_ingestion(rc, "un_sc", "publisher signal")
    run_ingestion(rc, "ofac_sdn", "stale")
    run_ingestion(rc, "icij", "try a disabled source")  # denied by the guard
    with tx() as conn:
        batches = fetch_all(conn, "SELECT * FROM run_batch WHERE agent_cycle_id = %s", (cycle,))
        assert len(batches) == 1 and batches[0]["trigger"] == "AGENT"
        b = _batch(conn, batches[0]["batch_id"])
        assert b["sources_run"] == 2 and b["sources_refused"] == 1
        assert (
            batches[0]["refused"][0]["source_id"] == "icij" and "DISABLED" in batches[0]["refused"][0]["why"]
        )


def test_resumed_run_stays_in_its_batch_and_counts_once(seeded):  # noqa: F811
    with tx() as conn:
        bid = runs.create_batch(conn, trigger="MANUAL", requested_by="op", requested_sources=["un_sc"])
        rid = runs.enqueue_run(
            conn, source_id="un_sc", run_kind="LIST_INGEST", trigger="MANUAL", requested_by="op", batch_id=bid
        )
        conn.execute(
            "UPDATE ingestion_run SET status = 'RUNNING', started_at = now(),"
            " lease_expires_at = now() - interval '1 minute' WHERE run_id = %s",
            (rid,),
        )
        out = runs.reclaim_abandoned(conn)
        new_id = out[0]["resumed_run_id"]
        assert str(fetch_val(conn, "SELECT batch_id FROM ingestion_run WHERE run_id = %s", (new_id,))) == bid
        b = _batch(conn, bid)
    assert b["sources_run"] == 1 and b["retried_attempts"] == 1 and b["status"] == "RUNNING"


def test_batch_status_is_derived_from_latest_attempts(seeded):  # noqa: F811
    with tx() as conn:
        good = runs.create_batch(conn, trigger="MANUAL", requested_by="op")
    load_fixture("un_sc", "un/consolidated_v1.xml", manual_load=True)
    with tx() as conn:
        # re-home that run into the batch under test (fixture loads create their own batch of one)
        conn.execute("UPDATE ingestion_run SET batch_id = %s WHERE source_id = 'un_sc'", (good,))
        assert _batch(conn, good)["status"] == "COMPLETED"
        conn.execute(
            "UPDATE source SET config = jsonb_set(config, '{validation,min_records}', '1000') WHERE source_id = 'un_sc'"
        )
    _, status, _ = load_fixture("un_sc", "un/consolidated_v2.xml", manual_load=True)
    assert status == "QUARANTINED"
    with tx() as conn:
        bad = fetch_val(conn, "SELECT batch_id FROM ingestion_run WHERE status = 'QUARANTINED'")
        assert _batch(conn, bad)["status"] == "NEEDS_REVIEW"
        empty = runs.create_batch(conn, trigger="MANUAL", requested_by="op", requested_sources=["icij"])
        runs.record_refused(conn, empty, [{"source_id": "icij", "why": "source is DISABLED"}])
        b = _batch(conn, empty)
        assert b["status"] == "REFUSED" and b["sources_refused"] == 1 and b["sources_run"] == 0


def test_success_schedules_the_next_aligned_slot(seeded):  # noqa: F811
    load_fixture("uk_fcdo", "uk/uk_sanctions_list_v1.xml", manual_load=True)
    with tx() as conn:
        nxt = fetch_val(
            conn, "SELECT next_due_at FROM source WHERE source_id = 'uk_fcdo'"
        )  # every 2 h, 30 min floor
    now = datetime.now(UTC)
    assert nxt.minute == 0 and nxt.second == 0 and nxt.hour % 2 == 0
    assert now + timedelta(minutes=28) <= nxt <= now + timedelta(hours=2, minutes=30)
