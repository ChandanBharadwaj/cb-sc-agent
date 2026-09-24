"""Read / operate API: overview, runs (+ live stream), quality, incidents, agent activity, review queue,
snapshots, Q&A and on-demand enrichment."""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from sanctions_agent import review
from sanctions_agent.api.auth import Principal, current_principal, require
from sanctions_agent.api.common import DbJSONResponse, DbRoute
from sanctions_agent.db.engine import fetch_all, fetch_one, fetch_val, tx
from sanctions_agent.ops import incidents
from sanctions_agent.pipeline import runs
from sanctions_agent.settings import get_settings

router = APIRouter(prefix="/api", default_response_class=DbJSONResponse, route_class=DbRoute)


# ------------------------------------------------------------------ overview -------------------
@router.get("/overview")
def overview(p: Principal = Depends(current_principal)) -> Any:
    with tx() as conn:
        sources = fetch_all(
            conn, "SELECT * FROM analytics.v_source_status ORDER BY level, priority, source_id"
        )
        active = fetch_all(conn, "SELECT * FROM analytics.v_run_progress ORDER BY started_at NULLS LAST")
        inc = fetch_all(
            conn, "SELECT severity, count(*) AS n FROM incident WHERE status <> 'RESOLVED' GROUP BY 1"
        )
        pending = fetch_val(conn, "SELECT count(*) FROM proposed_change WHERE status = 'PENDING'")
        snap = fetch_one(conn, "SELECT * FROM analytics.v_snapshots ORDER BY snapshot_id DESC LIMIT 1")
        agent = fetch_one(
            conn,
            """SELECT * FROM analytics.v_agent_activity WHERE agent_name = 'supervisor'
                                   ORDER BY started_at DESC LIMIT 1""",
        )
        cost = fetch_val(
            conn,
            "SELECT coalesce(sum(cost_usd), 0) FROM agent_cycle WHERE started_at >= date_trunc('day', now())",
        )
        held = fetch_val(
            conn,
            "SELECT count(*) FROM removal_candidate WHERE status IN ('PENDING','EVIDENCE_FOUND','REJECTED_PARSER')",
        )
        core_disabled = fetch_all(
            conn,
            "SELECT source_id, status, status_reason FROM source WHERE is_core AND status IN ('PAUSED','DISABLED')",
        )
        maint = fetch_val(conn, "SELECT value FROM system_setting WHERE key = 'maintenance_mode'")
    return {
        "user": {"name": p.user, "role": p.role},
        "sources": sources,
        "active_runs": active,
        "open_incidents": {r["severity"]: r["n"] for r in inc},
        "pending_reviews": pending,
        "snapshot": snap,
        "last_agent_cycle": agent,
        "llm_cost_today_usd": cost,
        "held_removals": held,
        "core_disabled": core_disabled,
        "maintenance": maint or {"enabled": False},
    }


# ------------------------------------------------------------------ runs ------------------------
@router.get("/runs")
def list_runs(
    source_id: str | None = None,
    status: str | None = None,
    limit: int = Query(50, le=500),
    _: Principal = Depends(current_principal),
) -> Any:
    with tx() as conn:
        return fetch_all(
            conn,
            """SELECT * FROM analytics.v_run_summary WHERE (%s::text IS NULL OR source_id = %s)
            AND (%s::text IS NULL OR status = %s) ORDER BY queued_at DESC LIMIT %s""",
            (source_id, source_id, status, status, limit),
        )


@router.get("/runs/{run_id}")
def get_run(run_id: str, _: Principal = Depends(current_principal)) -> Any:
    with tx() as conn:
        run = fetch_one(conn, "SELECT * FROM ingestion_run WHERE run_id = %s", (run_id,))
        if run is None:
            raise HTTPException(404, "run not found")
        steps = fetch_all(
            conn, "SELECT * FROM run_step WHERE run_id = %s ORDER BY started_at, attempt", (run_id,)
        )
        evidence = fetch_all(
            conn,
            """SELECT fetch_id, attempt, requested_url, final_url, redirect_chain, http_status,
            response_headers, tls_leaf_sha256, tls_chain_pem IS NOT NULL AS has_tls_chain, fetched_at, duration_ms,
            not_modified, conditional, sha256, size_bytes, error_class, error_detail
            FROM fetch_evidence WHERE run_id = %s ORDER BY attempt""",
            (run_id,),
        )
        version = fetch_one(conn, "SELECT * FROM list_version WHERE run_id = %s", (run_id,))
        issues = fetch_all(
            conn,
            "SELECT * FROM analytics.v_dq_issues WHERE issue_id IN (SELECT issue_id FROM dq_issue"
            " WHERE run_id = %s) ORDER BY severity",
            (run_id,),
        )
        changes = (
            fetch_all(
                conn,
                """SELECT change_type, entity_type, count(*) AS n FROM change_event
            WHERE version_id = %s GROUP BY 1, 2""",
                (run["version_id"],),
            )
            if run["version_id"]
            else []
        )
    return {
        "run": run,
        "steps": steps,
        "evidence": evidence,
        "version": version,
        "issues": issues,
        "changes": changes,
    }


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, p: Principal = Depends(require("operator"))) -> Any:
    with tx(actor=p.user) as conn:
        return {"result": runs.request_cancel(conn, run_id, p.user)}


@router.get("/stream/runs")
async def stream_runs(request: Request, _: Principal = Depends(current_principal)) -> StreamingResponse:
    """Server-Sent Events: live run progress, fed by Postgres LISTEN/NOTIFY (no polling)."""

    async def gen() -> Any:
        conn = await psycopg.AsyncConnection.connect(get_settings().database_url, autocommit=True)
        try:
            await conn.execute("LISTEN run_progress")
            # "ready" is sent once LISTEN is active: clients re-sync from the API then, so nothing that happened
            # between page load (or a reconnect) and this point is missed.
            yield "retry: 3000\nevent: ready\ndata: {}\n\n"
            while not await request.is_disconnected():
                got = False
                async for n in conn.notifies(timeout=15.0):
                    got = True
                    yield f"event: progress\ndata: {n.payload}\n\n"
                    if await request.is_disconnected():
                        return
                if not got:
                    yield ": keep-alive\n\n"
        finally:
            await conn.close()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ------------------------------------------------------------------ quality ---------------------
# The latest *validated* version per source (published or not): a candidate quarantined for a fill-rate drop
# must show its failing field, not hide behind the last good version.
_LATEST_MEASURED = """WITH latest AS (SELECT DISTINCT ON (source_id) source_id, version_id FROM analytics.v_quality_metrics
                     WHERE (%s::text IS NULL OR source_id = %s) ORDER BY source_id, seq DESC)"""


@router.get("/quality/fill-rates")
def fill_rates(source_id: str | None = None, _: Principal = Depends(current_principal)) -> Any:
    with tx() as conn:
        return fetch_all(
            conn,
            _LATEST_MEASURED
            + """ SELECT f.source_id, f.seq, f.version_status, f.entity_type, f.field, f.fill_rate, f.floor,
                     f.previous_fill_rate, f.status
            FROM analytics.v_fill_rates f JOIN latest USING (source_id, version_id)
            ORDER BY f.source_id, f.entity_type, f.field""",
            (source_id, source_id),
        )


@router.get("/quality/metrics")
def quality_metrics(source_id: str | None = None, _: Principal = Depends(current_principal)) -> Any:
    with tx() as conn:
        return fetch_all(
            conn,
            _LATEST_MEASURED
            + """ SELECT m.source_id, m.seq, m.version_status, m.entity_type, m.metric, m.field, m.value
            FROM analytics.v_quality_metrics m JOIN latest USING (source_id, version_id)
            WHERE m.metric NOT IN ('fill_rate', 'count', 'record_count')
            ORDER BY m.source_id, m.metric, m.field""",
            (source_id, source_id),
        )


@router.get("/quality/issues")
def quality_issues(
    status: str = "OPEN", source_id: str | None = None, _: Principal = Depends(current_principal)
) -> Any:
    with tx() as conn:
        return fetch_all(
            conn,
            """SELECT * FROM analytics.v_dq_issues WHERE status = %s AND (%s::text IS NULL OR source_id = %s)
            ORDER BY CASE severity WHEN 'FAIL' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, last_seen DESC LIMIT 300""",
            (status, source_id, source_id),
        )


@router.get("/quality/trend")
def count_trend(
    source_id: str, days: int = Query(90, le=730), _: Principal = Depends(current_principal)
) -> Any:
    with tx() as conn:
        return fetch_all(
            conn,
            """SELECT seq, published_at, record_count, counts_by_type, added, changed, removed
            FROM analytics.v_version_counts WHERE source_id = %s AND status IN ('PUBLISHED','SUPERSEDED')
              AND published_at > now() - make_interval(days => %s) ORDER BY seq""",
            (source_id, days),
        )


@router.get("/quality/changes")
def change_volume(
    days: int = Query(30, le=365), source_id: str | None = None, _: Principal = Depends(current_principal)
) -> Any:
    with tx() as conn:
        return fetch_all(
            conn,
            """SELECT day, change_type, sum(n)::int AS n FROM analytics.v_change_volume
            WHERE day > current_date - %s AND (%s::text IS NULL OR source_id = %s) GROUP BY 1, 2 ORDER BY 1, 2""",
            (days, source_id, source_id),
        )


@router.get("/quality/field-catalog")
def field_catalog(source_id: str | None = None, _: Principal = Depends(current_principal)) -> Any:
    with tx() as conn:
        return fetch_all(
            conn,
            "SELECT * FROM analytics.v_field_catalog WHERE (%s::text IS NULL OR source_id = %s)"
            " ORDER BY source_id, canonical_field",
            (source_id, source_id),
        )


@router.get("/enrichment/coverage")
def enrichment_coverage(_: Principal = Depends(current_principal)) -> Any:
    with tx() as conn:
        return {
            "coverage": fetch_all(
                conn, "SELECT * FROM analytics.v_enrichment_coverage ORDER BY provider, source_id, status"
            ),
            "evidence": fetch_all(
                conn, "SELECT * FROM analytics.v_notice_coverage ORDER BY source_id, change_type"
            ),
            "removal_holds": fetch_all(
                conn, "SELECT * FROM analytics.v_removal_holds ORDER BY source_id, status"
            ),
            "notices": fetch_all(conn, "SELECT * FROM analytics.v_notices ORDER BY fetched_at DESC LIMIT 50"),
        }


# ------------------------------------------------------------------ incidents / agent -----------
@router.get("/incidents")
def list_incidents(status: str = "OPEN", _: Principal = Depends(current_principal)) -> Any:
    with tx() as conn:
        return fetch_all(
            conn,
            "SELECT * FROM incident WHERE (%s = 'ALL' OR status = %s) ORDER BY opened_at DESC LIMIT 200",
            (status, status),
        )


class IncidentAction(BaseModel):
    note: str = Field("", max_length=1000)


@router.post("/incidents/{incident_id}/ack")
def ack_incident(incident_id: int, body: IncidentAction, p: Principal = Depends(require("operator"))) -> Any:
    with tx(actor=p.user) as conn:
        conn.execute(
            "UPDATE incident SET status = 'ACK' WHERE incident_id = %s AND status = 'OPEN'", (incident_id,)
        )
        incidents.add_action(conn, incident_id, {"by": p.user, "action": f"acknowledged: {body.note}"})
    return {"ok": True}


@router.post("/incidents/{incident_id}/resolve")
def resolve_incident(
    incident_id: int, body: IncidentAction, p: Principal = Depends(require("operator"))
) -> Any:
    with tx(actor=p.user) as conn:
        return {"resolved": incidents.resolve(conn, incident_id=incident_id, note=f"{p.user}: {body.note}")}


@router.get("/agent/cycles")
def agent_cycles(
    limit: int = Query(50, le=500), agent: str | None = None, _: Principal = Depends(current_principal)
) -> Any:
    with tx() as conn:
        rows = fetch_all(
            conn,
            """SELECT * FROM analytics.v_agent_activity WHERE (%s::text IS NULL OR agent_name = %s)
            ORDER BY started_at DESC LIMIT %s""",
            (agent, agent, limit),
        )
        daily = fetch_all(
            conn,
            """SELECT date_trunc('day', started_at)::date AS day, round(sum(cost_usd), 4) AS cost_usd,
            sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens, count(*) AS cycles,
            count(*) FILTER (WHERE fallback_used) AS fallbacks FROM agent_cycle
            WHERE started_at > now() - interval '30 days' GROUP BY 1 ORDER BY 1""",
        )
    return {"cycles": rows, "daily": daily}


@router.get("/agent/cycles/{cycle_id}")
def agent_cycle(cycle_id: str, _: Principal = Depends(current_principal)) -> Any:
    with tx() as conn:
        c = fetch_one(conn, "SELECT * FROM agent_cycle WHERE cycle_id = %s", (cycle_id,))
        if c is None:
            raise HTTPException(404, "cycle not found")
        actions = fetch_all(conn, "SELECT * FROM agent_action WHERE cycle_id = %s ORDER BY seq", (cycle_id,))
        runs_ = fetch_all(
            conn, "SELECT run_id, source_id, status FROM ingestion_run WHERE agent_cycle_id = %s", (cycle_id,)
        )
    return {"cycle": c, "actions": actions, "runs": runs_}


# ------------------------------------------------------------------ review ----------------------
@router.get("/proposals")
def list_proposals(
    status: str = "PENDING", kind: str | None = None, _: Principal = Depends(current_principal)
) -> Any:
    with tx() as conn:
        return fetch_all(
            conn,
            """SELECT * FROM proposed_change WHERE (%s = 'ALL' OR status = %s)
            AND (%s::text IS NULL OR kind = %s) ORDER BY created_at DESC LIMIT 200""",
            (status, status, kind, kind),
        )


class Decision(BaseModel):
    approve: bool
    comment: str | None = Field(None, max_length=2000)
    resolution: str | None = None


@router.post("/proposals/{change_id}/decide")
def decide(change_id: int, body: Decision, p: Principal = Depends(require("reviewer"))) -> Any:
    with tx(actor=p.user) as conn:
        return review.decide(
            conn,
            change_id,
            reviewer=p.user,
            role=p.role,
            approve=body.approve,
            comment=body.comment,
            resolution=body.resolution,
        )


@router.get("/snapshots/current")
def current_snapshot(_: Principal = Depends(current_principal)) -> Any:
    with tx() as conn:
        snap = fetch_one(conn, "SELECT * FROM analytics.v_snapshots ORDER BY snapshot_id DESC LIMIT 1")
        pins = (
            fetch_all(
                conn,
                """SELECT sv.source_id, sv.seq, v.publication_marker, v.record_count FROM snapshot_version sv
            JOIN list_version v USING (version_id) WHERE sv.snapshot_id = %s ORDER BY 1""",
                (snap["snapshot_id"],),
            )
            if snap
            else []
        )
    return {"snapshot": snap, "versions": pins}


# ------------------------------------------------------------------ Q&A / enrichment ------------
class Question(BaseModel):
    question: str = Field(..., min_length=2, max_length=2000)
    session_id: str | None = Field(None, max_length=64)


@router.post("/ask")
async def ask(body: Question, request: Request, p: Principal = Depends(current_principal)) -> Any:
    factory = getattr(request.app.state, "analyst_factory", None)
    if factory is None:
        from sanctions_agent.agent.analyst import AnalystAgent

        factory = AnalystAgent
    agent = factory()
    return await asyncio.to_thread(agent.ask, body.question, user=p.user, session_id=body.session_id)


class OnDemand(BaseModel):
    source_id: str
    source_key: str
    provider_source_id: str
    kind: str = "default"
    reason: str = Field(..., min_length=5, max_length=500)


@router.post("/enrich/on-demand")
def enrich_on_demand(body: OnDemand, p: Principal = Depends(require("reviewer"))) -> Any:
    from sanctions_agent.enrichment.batch import enrich_on_demand as run

    return run(
        body.source_id,
        body.source_key,
        body.provider_source_id,
        requested_by=p.user,
        reason=body.reason,
        kind=body.kind,
    )
