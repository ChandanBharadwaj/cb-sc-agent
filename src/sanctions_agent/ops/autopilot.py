"""Deterministic orchestration: the safety net that keeps ingestion running without the LLM.

* ``plan``      - which runs are due now (schedule, early pulls on signals), respecting status,
                  pause-until, maintenance mode, breaker and the politeness floor
* ``watchdog``  - staleness / no-change incidents and *forced* pulls of sources past their hard
                  ceiling, regardless of what the agent decided
* ``chores``    - deterministic housekeeping (auto-resume after pause, removal assessment, partitions)
* ``gate``      - whether anything needs the LLM's judgement this cycle
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from sanctions_agent.db.engine import fetch_all, fetch_val, tx
from sanctions_agent.logs import get_logger
from sanctions_agent.ops import incidents, system_settings
from sanctions_agent.pipeline import removals, runs, source_state

log = get_logger(__name__)

KIND_TO_RUN = {
    "STRUCTURED_LIST": "LIST_INGEST",
    "CURATED_LIST": "LIST_INGEST",
    "NOTICE_FEED": "NOTICE_SYNC",
    "ENRICHMENT": "ENRICHMENT_BATCH",
}


@dataclass
class PlannedRun:
    source_id: str
    trigger: str
    reason: str


@dataclass
class TickReport:
    enqueued: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    incidents: list[int] = field(default_factory=list)
    chores: dict[str, Any] = field(default_factory=dict)


def _kind(conn: psycopg.Connection[Any], source_id: str) -> str:
    return KIND_TO_RUN[fetch_val(conn, "SELECT kind FROM source WHERE source_id = %s", (source_id,))]


def enqueue_guarded(
    conn: psycopg.Connection[Any],
    source_id: str,
    *,
    trigger: str,
    requested_by: str,
    reason: str,
    agent_cycle_id: str | None = None,
    options: dict[str, Any] | None = None,
    ignore_min_interval: bool = False,
) -> dict[str, Any]:
    """Enqueue a run if every guard allows it. Returns {ok, run_id | why}."""
    src = fetch_all(conn, "SELECT * FROM source WHERE source_id = %s", (source_id,))
    if not src:
        return {"ok": False, "why": f"unknown source {source_id}"}
    s = src[0]
    if system_settings.maintenance_on(conn):
        return {"ok": False, "why": "maintenance mode is on"}
    if s["status"] != "ACTIVE":
        return {"ok": False, "why": f"source is {s['status']}"}
    now = datetime.now(UTC)
    if not ignore_min_interval and s["last_attempt_at"] and now - s["last_attempt_at"] < s["min_interval"]:
        return {
            "ok": False,
            "why": f"politeness: last attempt {now - s['last_attempt_at']} ago < min interval {s['min_interval']}",
        }
    allowed, why = source_state.breaker_allows(conn, source_id)
    if not allowed:
        return {"ok": False, "why": why}
    try:
        rid = runs.enqueue_run(
            conn,
            source_id=source_id,
            run_kind=KIND_TO_RUN[s["kind"]],
            trigger=trigger,
            requested_by=requested_by,
            reason=reason,
            agent_cycle_id=agent_cycle_id,
            options=options,
        )
    except runs.RunAlreadyActive as e:
        return {"ok": False, "why": f"already running ({e.run_id})", "run_id": e.run_id}
    return {"ok": True, "run_id": rid}


def plan(conn: psycopg.Connection[Any]) -> list[PlannedRun]:
    out: list[PlannedRun] = []
    rows = fetch_all(
        conn,
        """SELECT s.source_id, s.next_due_at, s.priority,
            (SELECT count(*) FROM signal g WHERE g.target_source_id = s.source_id AND g.consumed_at IS NULL) AS signals
        FROM source s WHERE s.status = 'ACTIVE'
          AND NOT EXISTS (SELECT 1 FROM ingestion_run r WHERE r.source_id = s.source_id AND r.status IN ('QUEUED','RUNNING'))
        ORDER BY s.priority, s.next_due_at NULLS FIRST""",
    )
    now = datetime.now(UTC)
    for r in rows:
        if r["signals"]:
            out.append(PlannedRun(r["source_id"], "SIGNAL", f"{r['signals']} unconsumed publisher signal(s)"))
        elif r["next_due_at"] is None or r["next_due_at"] <= now:
            out.append(PlannedRun(r["source_id"], "SCHEDULE", "scheduled pull due"))
    return out


def run_plan(
    conn: psycopg.Connection[Any], planned: list[PlannedRun], *, requested_by: str = "autopilot"
) -> TickReport:
    rep = TickReport()
    for p in planned:
        res = enqueue_guarded(
            conn, p.source_id, trigger=p.trigger, requested_by=requested_by, reason=p.reason
        )
        (rep.enqueued if res["ok"] else rep.skipped).append(
            {"source_id": p.source_id, **res, "reason": p.reason}
        )
    return rep


def watchdog(conn: psycopg.Connection[Any]) -> TickReport:
    """Staleness monitoring (NFR-04) and forced pulls past the hard ceiling."""
    rep = TickReport()
    if system_settings.maintenance_on(conn):
        return rep
    rows = fetch_all(
        conn,
        """SELECT s.*, now() - coalesce(s.last_success_at, s.created_at) AS stale_for,
            now() - coalesce(s.last_change_at, s.created_at) AS unchanged_for
        FROM source s WHERE s.status = 'ACTIVE'""",
    )
    for s in rows:
        sid = s["source_id"]
        if s["stale_for"] > s["hard_max_staleness"]:
            iid, new = incidents.open_incident(
                conn,
                source_id=sid,
                error_class="STALE",
                severity="PAGE" if s["is_core"] else "WARN",
                title=f"{sid}: no successful ingestion for {_hours(s['stale_for'])}h (limit {_hours(s['hard_max_staleness'])}h)",
                summary="Screening is using stale data for this list. Watchdog forcing a pull.",
                dedupe_key=f"{sid}:STALE",
            )
            if new:
                rep.incidents.append(iid)
            active = fetch_val(
                conn,
                "SELECT count(*) FROM ingestion_run WHERE source_id = %s AND status IN ('QUEUED','RUNNING')",
                (sid,),
            )
            if not active:
                res = enqueue_guarded(
                    conn,
                    sid,
                    trigger="WATCHDOG",
                    requested_by="watchdog",
                    reason="hard staleness ceiling exceeded",
                )
                (rep.enqueued if res["ok"] else rep.skipped).append({"source_id": sid, **res})
        elif s["stale_for"] > s["warn_staleness"]:
            iid, new = incidents.open_incident(
                conn,
                source_id=sid,
                error_class="STALE",
                severity="WARN",
                title=f"{sid}: no successful ingestion for {_hours(s['stale_for'])}h",
                summary=f"warn threshold {_hours(s['warn_staleness'])}h",
                dedupe_key=f"{sid}:STALE",
            )
            if new:
                rep.incidents.append(iid)
        else:
            incidents.resolve(conn, source_id=sid, error_classes=["STALE"], note="fresh again")
        days = ((s["config"] or {}).get("alerts") or {}).get("no_change_alert_days")
        if days and s["last_change_at"] and s["unchanged_for"] > timedelta(days=days):
            iid, new = incidents.open_incident(
                conn,
                source_id=sid,
                error_class="STALE_NO_CHANGE",
                severity="INFO",
                title=f"{sid}: no new version for {s['unchanged_for'].days} days (alert after {days})",
                summary="The file keeps arriving unchanged. Check the publisher is still updating this URL "
                "(e.g. the OFSI list froze when it closed on 28 Jan 2026).",
                dedupe_key=f"{sid}:STALE_NO_CHANGE",
            )
            if new:
                rep.incidents.append(iid)
        elif days:
            incidents.resolve(conn, source_id=sid, error_classes=["STALE_NO_CHANGE"], note="changed again")
    return rep


def chores(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    resumed = fetch_all(
        conn,
        """UPDATE source SET status = 'ACTIVE', status_reason = 'pause window ended',
            status_changed_by = 'system', status_changed_at = now(), paused_until = NULL
        WHERE status = 'PAUSED' AND paused_until IS NOT NULL AND paused_until <= now() RETURNING source_id, is_core""",
    )
    for r in resumed:
        incidents.resolve(
            conn, source_id=r["source_id"], error_classes=["CORE_SOURCE_DISABLED"], note="pause ended"
        )
    out["auto_resumed"] = [r["source_id"] for r in resumed]
    pending = fetch_all(
        conn,
        """SELECT candidate_id FROM removal_candidate WHERE status IN ('PENDING','EVIDENCE_FOUND')
        AND NOT EXISTS (SELECT 1 FROM proposed_change p WHERE p.dedupe_key = 'removal:' || candidate_id)
        ORDER BY candidate_id LIMIT 200""",
    )
    assessed = [removals.assess(conn, r["candidate_id"]) for r in pending]
    out["removals_assessed"] = len(assessed)
    out["removal_proposals"] = sum(1 for a in assessed if a.get("proposed_change_id"))
    return out


def gate(conn: psycopg.Connection[Any], *, last_review_at: datetime | None, review_minutes: int) -> list[str]:
    """Reasons the LLM supervisor should run this cycle (empty -> autopilot only, no tokens spent)."""
    reasons: list[str] = []
    q = [
        (
            "new incidents without diagnosis",
            "SELECT count(*) FROM incident WHERE status = 'OPEN' AND diagnosis IS NULL AND severity <> 'INFO'",
        ),
        (
            "sources with open or half-open breaker",
            "SELECT count(*) FROM source WHERE breaker_state <> 'CLOSED'",
        ),
        (
            "versions held or quarantined in the last day",
            "SELECT count(*) FROM list_version WHERE status IN ('HELD','QUARANTINED') AND created_at > now() - interval '1 day'",
        ),
        (
            "removal candidates without evidence",
            "SELECT count(*) FROM removal_candidate WHERE status = 'PENDING' AND created_at < now() - interval '1 hour'",
        ),
        (
            "notices needing extraction",
            "SELECT count(*) FROM legal_notice WHERE extraction_status = 'NEEDS_AGENT'",
        ),
    ]
    for label, sql in q:
        n = fetch_val(conn, sql)
        if n:
            reasons.append(f"{n} {label}")
    if last_review_at is None or datetime.now(UTC) - last_review_at > timedelta(minutes=review_minutes):
        reasons.append("periodic health review")
    return reasons


def _hours(td: timedelta) -> int:
    return int(td.total_seconds() // 3600)


def autopilot_cycle(requested_by: str = "autopilot") -> TickReport:
    """One deterministic cycle: chores, watchdog, scheduled / signalled pulls."""
    with tx(actor=requested_by) as conn:
        c = chores(conn)
    with tx(actor=requested_by) as conn:
        w = watchdog(conn)
    with tx(actor=requested_by) as conn:
        r = run_plan(conn, plan(conn), requested_by=requested_by)
    r.enqueued = w.enqueued + r.enqueued
    r.skipped = w.skipped + r.skipped
    r.incidents = w.incidents
    r.chores = c
    return r
