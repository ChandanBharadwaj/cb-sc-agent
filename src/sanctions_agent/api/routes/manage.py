"""Management API: sources (create / edit / status / activation / rollback), schedules (validation + preview),
ad-hoc runs (normal / force / dry run / re-parse / cancel) and global settings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from sanctions_agent.api.auth import Principal, current_principal, require
from sanctions_agent.api.common import DbJSONResponse, DbRoute
from sanctions_agent.db.engine import fetch_all, fetch_one, fetch_val, tx
from sanctions_agent.ops import autopilot, system_settings
from sanctions_agent.pipeline import runs
from sanctions_agent.scheduling.schedule import Schedule, ScheduleError
from sanctions_agent.sources.adapter_types import ADAPTER_TYPES, get_adapter_type
from sanctions_agent.sources.config_service import ConfigService, ValidationFailed
from sanctions_agent.sources.registry import get_source

router = APIRouter(prefix="/api", default_response_class=DbJSONResponse, route_class=DbRoute)


class ScheduleIn(BaseModel):
    kind: Literal["INTERVAL", "CRON"] = "INTERVAL"
    cadence_minutes: int | None = Field(None, ge=1)
    cron_expr: str | None = None
    timezone: str = "UTC"
    min_interval_minutes: int = Field(30, ge=1)
    warn_staleness_hours: float = Field(6, gt=0)
    hard_max_staleness_hours: float = Field(12, gt=0)

    def to_schedule(self) -> Schedule:
        return Schedule.from_seed(self.model_dump())


@router.get("/adapter-types")
def adapter_types(_: Principal = Depends(current_principal)) -> Any:
    return [
        {
            "type_id": t.type_id,
            "kind": t.kind,
            "level": t.level,
            "description": t.description,
            "hard_min_interval_minutes": int(t.hard_min_interval.total_seconds() // 60),
            "config_schema": t.json_schema(),
        }
        for t in ADAPTER_TYPES.values()
    ]


@router.get("/sources")
def list_sources(_: Principal = Depends(current_principal)) -> Any:
    with tx() as conn:
        return fetch_all(conn, "SELECT * FROM analytics.v_source_status ORDER BY level, priority, source_id")


@router.get("/sources/{source_id}")
def source_detail(source_id: str, _: Principal = Depends(current_principal)) -> Any:
    with tx() as conn:
        try:
            s = get_source(conn, source_id)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        versions = fetch_all(
            conn,
            """SELECT version, status, changed_by, changed_at, reason, diff, approval_change_id
            FROM source_config_version WHERE source_id = %s ORDER BY version DESC""",
            (source_id,),
        )
        runs_ = fetch_all(
            conn,
            "SELECT * FROM analytics.v_run_summary WHERE source_id = %s ORDER BY queued_at DESC LIMIT 20",
            (source_id,),
        )
        list_versions = fetch_all(
            conn,
            "SELECT * FROM analytics.v_version_counts WHERE source_id = %s ORDER BY seq DESC LIMIT 20",
            (source_id,),
        )
        artifacts = fetch_all(
            conn,
            """SELECT DISTINCT ON (v.raw_sha256) v.raw_sha256 AS sha256, v.seq, v.status, v.created_at
            FROM list_version v WHERE v.source_id = %s ORDER BY v.raw_sha256, v.seq DESC""",
            (source_id,),
        )
        status = fetch_one(conn, "SELECT * FROM analytics.v_source_status WHERE source_id = %s", (source_id,))
    try:
        preview = [t.isoformat() for t in s.schedule.preview(5)]
    except (ValueError, TypeError):
        preview = []
    return {
        "source": {**s.row, "schedule": s.schedule.to_json(), "schedule_text": s.schedule.describe()},
        "status": status,
        "config_schema": s.adapter_type.json_schema(),
        "hard_min_interval_minutes": int(s.adapter_type.hard_min_interval.total_seconds() // 60),
        "config_versions": versions,
        "runs": runs_,
        "versions": list_versions,
        "archived_artifacts": sorted(artifacts, key=lambda a: a["seq"], reverse=True)[:20],
        "next_runs": preview,
    }


class SourceCreate(BaseModel):
    source_id: str = Field(..., pattern=r"^[a-z][a-z0-9_]{1,62}$")
    display_name: str = Field(..., min_length=3, max_length=200)
    adapter_type: str
    config: dict[str, Any]
    schedule: ScheduleIn
    is_core: bool = False
    priority: int = Field(5, ge=1, le=9)
    licence: str | None = None
    licence_status: Literal["OK", "ATTRIBUTION_REQUIRED", "LEGAL_REVIEW_REQUIRED", "NOT_PERMITTED"] = "OK"
    reason: str = Field(..., min_length=3)


@router.post("/sources", status_code=201)
def create_source(body: SourceCreate, p: Principal = Depends(require("admin"))) -> Any:
    get_adapter_type(body.adapter_type)
    with tx(actor=p.user) as conn:
        ConfigService(conn).create_source(
            source_id=body.source_id,
            display_name=body.display_name,
            adapter_type=body.adapter_type,
            config=body.config,
            schedule=body.schedule.to_schedule(),
            actor=p.user,
            reason=body.reason,
            is_core=body.is_core,
            priority=body.priority,
            licence=body.licence,
            licence_status=body.licence_status,
        )
    return {"source_id": body.source_id, "status": "DRAFT"}


class SourcePatch(BaseModel):
    config: dict[str, Any] | None = None
    schedule: ScheduleIn | None = None
    priority: int | None = Field(None, ge=1, le=9)
    display_name: str | None = None
    reason: str = Field(..., min_length=3)
    expected_version: int | None = None


@router.patch("/sources/{source_id}")
def patch_source(
    source_id: str,
    body: SourcePatch,
    if_match: str | None = Header(None),
    p: Principal = Depends(require("admin")),
) -> Any:
    expected = (
        body.expected_version if body.expected_version is not None else (int(if_match) if if_match else None)
    )
    if expected is None:
        raise HTTPException(428, "send the config_version you edited (If-Match header or expected_version)")
    with tx(actor=p.user) as conn:
        res = ConfigService(conn).update_source(
            source_id,
            actor=p.user,
            reason=body.reason,
            expected_version=expected,
            config=body.config,
            schedule=body.schedule.to_schedule() if body.schedule else None,
            priority=body.priority,
            display_name=body.display_name,
        )
    return res.__dict__


class StatusChange(BaseModel):
    status: Literal["ACTIVE", "PAUSED", "DISABLED"]
    reason: str | None = Field(None, max_length=500)
    paused_until: datetime | None = None
    pause_hours: float | None = Field(None, gt=0, le=24 * 30)


@router.post("/sources/{source_id}/status")
def change_status(source_id: str, body: StatusChange, p: Principal = Depends(require("operator"))) -> Any:
    until = body.paused_until or (
        datetime.now(UTC) + timedelta(hours=body.pause_hours) if body.pause_hours else None
    )
    with tx(actor=p.user) as conn:
        ConfigService(conn).set_status(
            source_id, body.status, actor=p.user, role=p.role, reason=body.reason, paused_until=until
        )
    return {"source_id": source_id, "status": body.status, "paused_until": until}


class Reason(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500)


@router.post("/sources/{source_id}/request-activation")
def request_activation(source_id: str, body: Reason, p: Principal = Depends(require("admin"))) -> Any:
    with tx(actor=p.user) as conn:
        return {
            "proposal_id": ConfigService(conn).request_activation(source_id, actor=p.user, reason=body.reason)
        }


class Rollback(BaseModel):
    reason: str = Field(..., min_length=3)
    expected_version: int


@router.post("/sources/{source_id}/rollback/{version}")
def rollback(source_id: str, version: int, body: Rollback, p: Principal = Depends(require("admin"))) -> Any:
    with tx(actor=p.user) as conn:
        return (
            ConfigService(conn)
            .rollback(
                source_id, version, actor=p.user, reason=body.reason, expected_version=body.expected_version
            )
            .__dict__
        )


@router.post("/schedule/preview")
def schedule_preview(body: ScheduleIn, adapter_type: str, _: Principal = Depends(current_principal)) -> Any:
    at = get_adapter_type(adapter_type)
    sched = body.to_schedule()
    try:
        sched.validate(at.hard_min_interval)
    except ScheduleError as e:
        return {
            "valid": False,
            "error": str(e),
            "next_runs": [],
            "text": None,
            "hard_min_interval_minutes": int(at.hard_min_interval.total_seconds() // 60),
        }
    return {
        "valid": True,
        "error": None,
        "text": sched.describe(),
        "next_runs": [t.isoformat() for t in sched.preview(5)],
        "hard_min_interval_minutes": int(at.hard_min_interval.total_seconds() // 60),
    }


class RunRequest(BaseModel):
    mode: Literal["normal", "force_refetch", "dry_run", "reparse"] = "normal"
    reason: str = Field(..., min_length=3, max_length=500)
    reparse_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    limit: int | None = Field(None, ge=1, le=100000)
    override_min_interval: bool = False


@router.post("/sources/{source_id}/runs", status_code=202)
def run_now(source_id: str, body: RunRequest, p: Principal = Depends(require("operator"))) -> Any:
    options: dict[str, Any] = {}
    if body.mode == "force_refetch":
        options["force_refetch"] = True
    elif body.mode == "dry_run":
        options["dry_run"] = True
    elif body.mode == "reparse":
        if not body.reparse_sha256:
            raise HTTPException(422, "reparse needs reparse_sha256 (an archived artifact of this source)")
        options["reparse_sha256"] = body.reparse_sha256
    if body.limit:
        options["limit"] = body.limit
    override = body.override_min_interval or body.mode == "reparse"
    if body.override_min_interval and not p.has("admin"):
        raise HTTPException(403, "only an admin can override the politeness interval")
    with tx(actor=p.user) as conn:
        if override and body.mode != "reparse":
            last = fetch_one(conn, "SELECT last_attempt_at FROM source WHERE source_id = %s", (source_id,))
            if (
                last
                and last["last_attempt_at"]
                and datetime.now(UTC) - last["last_attempt_at"] < timedelta(minutes=5)
            ):
                raise HTTPException(429, "even with an override, pulls must be at least 5 minutes apart")
        if body.mode == "reparse":
            belongs = fetch_one(
                conn,
                "SELECT 1 FROM list_version WHERE source_id = %s AND raw_sha256 = %s LIMIT 1",
                (source_id, body.reparse_sha256),
            )
            if not belongs:
                raise HTTPException(422, "that archived artifact does not belong to this source")
        res = autopilot.enqueue_guarded(
            conn,
            source_id,
            trigger="MANUAL",
            requested_by=p.user,
            reason=body.reason,
            options=options,
            ignore_min_interval=override,
        )
        if override and res.get("ok"):
            conn.execute(
                "UPDATE ingestion_run SET summary = summary || jsonb_build_object('politeness_override', %s::text)"
                " WHERE run_id = %s",
                (p.user, res["run_id"]),
            )
    if not res["ok"]:
        raise HTTPException(409, res["why"])
    with tx() as conn:
        res["batch_id"] = str(
            fetch_val(conn, "SELECT batch_id FROM ingestion_run WHERE run_id = %s", (res["run_id"],))
        )
    return res


class BatchRequest(BaseModel):
    source_ids: list[str] = Field(..., min_length=1, max_length=100)
    mode: Literal["normal", "force_refetch", "dry_run"] = "normal"
    reason: str = Field(..., min_length=3, max_length=500)
    limit: int | None = Field(None, ge=1, le=100000)
    override_min_interval: bool = False


@router.post("/batches", status_code=202)
def run_batch(body: BatchRequest, p: Principal = Depends(require("operator"))) -> Any:
    """Ad-hoc run of several sources as ONE batch. Every source goes through the same guards as a single run
    (status, maintenance, politeness, breaker, one active run); the ones refused are recorded on the batch with
    the reason, so the request is fully traceable. Re-parse is per source (POST /sources/{id}/runs)."""
    if body.override_min_interval and not p.has("admin"):
        raise HTTPException(403, "only an admin can override the politeness interval")
    options: dict[str, Any] = {}
    if body.mode == "force_refetch":
        options["force_refetch"] = True
    elif body.mode == "dry_run":
        options["dry_run"] = True
    if body.limit:
        options["limit"] = body.limit
    wanted = list(dict.fromkeys(body.source_ids))
    with tx(actor=p.user) as conn:
        known = fetch_all(
            conn,
            "SELECT source_id, last_attempt_at FROM source WHERE source_id = ANY(%s) ORDER BY priority, source_id",
            (wanted,),
        )
        missing = sorted(set(wanted) - {r["source_id"] for r in known})
        if missing:
            raise HTTPException(422, f"unknown sources: {', '.join(missing)}")
        batch_options: dict[str, Any] = {"mode": body.mode}
        if body.limit:
            batch_options["limit"] = body.limit
        if body.override_min_interval:
            batch_options["override_min_interval"] = True
        batch_id = runs.create_batch(
            conn,
            trigger="MANUAL",
            requested_by=p.user,
            reason=body.reason,
            options=batch_options,
            requested_sources=[r["source_id"] for r in known],
        )
        queued: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = []
        now = datetime.now(UTC)
        for r in known:
            sid = r["source_id"]
            if (
                body.override_min_interval
                and r["last_attempt_at"]
                and now - r["last_attempt_at"] < timedelta(minutes=5)
            ):
                refused.append(
                    {"source_id": sid, "why": "even with an override, pulls must be at least 5 minutes apart"}
                )
                continue
            res = autopilot.enqueue_guarded(
                conn,
                sid,
                trigger="MANUAL",
                requested_by=p.user,
                reason=body.reason,
                options=options,
                ignore_min_interval=body.override_min_interval,
                batch_id=batch_id,
            )
            if not res["ok"]:
                refused.append({"source_id": sid, "why": res["why"]})
                continue
            queued.append({"source_id": sid, "run_id": res["run_id"]})
            if body.override_min_interval:
                conn.execute(
                    "UPDATE ingestion_run SET summary = summary || jsonb_build_object('politeness_override', %s::text)"
                    " WHERE run_id = %s",
                    (p.user, res["run_id"]),
                )
        runs.record_refused(conn, batch_id, refused)
    return {"batch_id": batch_id, "queued": queued, "refused": refused}


@router.get("/sources/{source_id}/suggest-floors")
def suggest_floors(
    source_id: str, versions: int = 5, margin: float = 0.05, _: Principal = Depends(current_principal)
) -> Any:
    """Suggest fill-rate floors from the last N versions: the lowest observed rate minus a margin, for fields
    that are materially populated (>= 20%). A starting point for the admin - never applied automatically."""
    with tx() as conn:
        rows = fetch_all(
            conn,
            """SELECT m.entity_type, m.field, min(m.value) AS lowest, max(m.value) AS highest,
                count(*) AS versions FROM dq_metric m
            WHERE m.metric = 'fill_rate' AND m.version_id IN (SELECT version_id FROM list_version WHERE source_id = %s
                  AND status IN ('PUBLISHED','SUPERSEDED') ORDER BY seq DESC LIMIT %s)
            GROUP BY 1, 2 HAVING min(m.value) >= 0.2 ORDER BY 1, 2""",
            (source_id, versions),
        )
    return {
        f"{r['entity_type']}.{r['field']}": {
            "suggested_floor": max(0.0, round(float(r["lowest"]) - margin, 2)),
            "lowest": float(r["lowest"]),
            "highest": float(r["highest"]),
            "versions": r["versions"],
        }
        for r in rows
    }


@router.get("/settings")
def get_settings_api(_: Principal = Depends(current_principal)) -> Any:
    with tx() as conn:
        return system_settings.get_all(conn)


class SettingValue(BaseModel):
    value: Any


@router.put("/settings/{key}")
def put_setting(key: str, body: SettingValue, p: Principal = Depends(require("admin"))) -> Any:
    if key == "agent_daily_budget_usd" and not (0 <= float(body.value) <= 1000):
        raise ValidationFailed("budget must be between 0 and 1000 USD")
    with tx(actor=p.user) as conn:
        system_settings.set_value(conn, key, body.value, p.user)
        return system_settings.get_all(conn)
