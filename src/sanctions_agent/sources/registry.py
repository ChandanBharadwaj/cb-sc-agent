"""Loading sources from the DB and importing / exporting the YAML seed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import yaml
from pydantic import BaseModel

from sanctions_agent.db.engine import fetch_all, fetch_one, jsonb
from sanctions_agent.scheduling.schedule import Schedule
from sanctions_agent.sources.adapter_types import AdapterType, get_adapter_type


@dataclass
class SourceSpec:
    source_id: str
    display_name: str
    adapter_type: AdapterType
    kind: str
    level: int
    is_core: bool
    status: str
    config: BaseModel
    config_raw: dict[str, Any]
    config_version: int
    schedule: Schedule
    priority: int
    licence: str | None
    licence_status: str
    next_due_at: datetime | None
    last_attempt_at: datetime | None
    last_fetch_at: datetime | None
    last_success_at: datetime | None
    last_change_at: datetime | None
    current_version_id: int | None
    breaker_state: str
    breaker_retry_at: datetime | None
    consecutive_failures: int
    paused_until: datetime | None
    row: dict[str, Any]

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> SourceSpec:
        at = get_adapter_type(row["adapter_type"])
        return cls(
            source_id=row["source_id"],
            display_name=row["display_name"],
            adapter_type=at,
            kind=row["kind"],
            level=row["level"],
            is_core=row["is_core"],
            status=row["status"],
            config=at.validate_config(row["config"]),
            config_raw=row["config"],
            config_version=row["config_version"],
            schedule=Schedule.from_row(row),
            priority=row["priority"],
            licence=row["licence"],
            licence_status=row["licence_status"],
            next_due_at=row["next_due_at"],
            last_attempt_at=row["last_attempt_at"],
            last_fetch_at=row["last_fetch_at"],
            last_success_at=row["last_success_at"],
            last_change_at=row["last_change_at"],
            current_version_id=row["current_version_id"],
            breaker_state=row["breaker_state"],
            breaker_retry_at=row["breaker_retry_at"],
            consecutive_failures=row["consecutive_failures"],
            paused_until=row["paused_until"],
            row=row,
        )


def get_source(conn: psycopg.Connection[Any], source_id: str, *, for_update: bool = False) -> SourceSpec:
    row = fetch_one(
        conn,
        "SELECT * FROM source WHERE source_id = %s" + (" FOR UPDATE" if for_update else ""),
        (source_id,),
    )
    if row is None:
        raise KeyError(f"unknown source {source_id!r}")
    return SourceSpec.from_row(row)


def list_sources(conn: psycopg.Connection[Any], *, status: str | None = None) -> list[SourceSpec]:
    sql = "SELECT * FROM source"
    params: tuple[Any, ...] = ()
    if status:
        sql += " WHERE status = %s"
        params = (status,)
    sql += " ORDER BY level, priority, source_id"
    return [SourceSpec.from_row(r) for r in fetch_all(conn, sql, params)]


# ---------------------------------------------------------------------------------------------
# Seed import / export
# ---------------------------------------------------------------------------------------------
def load_seed(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("sources", []))


def import_seed(
    conn: psycopg.Connection[Any], path: Path, *, actor: str, overwrite: bool = False
) -> dict[str, list[str]]:
    """Create sources missing from the DB. Existing rows are left alone unless ``overwrite``.

    With ``overwrite`` the seed config becomes a *new audited config version* (never an in-place edit).
    """
    from sanctions_agent.sources.config_service import ConfigService

    svc = ConfigService(conn)
    result: dict[str, list[str]] = {"created": [], "skipped": [], "updated": []}
    for entry in load_seed(path):
        sid = entry["source_id"]
        exists = fetch_one(conn, "SELECT config_version FROM source WHERE source_id = %s", (sid,))
        schedule = Schedule.from_seed(entry.get("schedule", {}))
        if exists is None:
            svc.create_source(
                source_id=sid,
                display_name=entry["display_name"],
                adapter_type=entry["adapter_type"],
                config=entry.get("config", {}),
                schedule=schedule,
                actor=actor,
                reason="seed import",
                is_core=bool(entry.get("is_core", False)),
                priority=int(entry.get("priority", 5)),
                licence=entry.get("licence"),
                licence_status=entry.get("licence_status", "OK"),
                initial_status=entry.get("status", "DRAFT"),
                status_reason=entry.get("status_reason"),
                seed=True,
            )
            result["created"].append(sid)
        elif overwrite:
            svc.update_source(
                sid,
                config=entry.get("config", {}),
                schedule=schedule,
                actor=actor,
                reason="seed re-import (--overwrite)",
                expected_version=exists["config_version"],
                trusted=True,
            )
            result["updated"].append(sid)
        else:
            result["skipped"].append(sid)
    return result


def export_seed(conn: psycopg.Connection[Any]) -> str:
    out = []
    for s in list_sources(conn):
        entry: dict[str, Any] = {
            "source_id": s.source_id,
            "display_name": s.display_name,
            "adapter_type": s.adapter_type.type_id,
            "is_core": s.is_core,
            "status": s.status,
            "schedule": s.schedule.to_json(),
            "priority": s.priority,
            "licence": s.licence,
            "licence_status": s.licence_status,
            "config": s.config_raw,
        }
        if s.row.get("status_reason"):
            entry["status_reason"] = s.row["status_reason"]
        out.append(entry)
    return yaml.safe_dump({"sources": out}, sort_keys=False, allow_unicode=True)


def config_json(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


__all__ = ["SourceSpec", "config_json", "export_seed", "get_source", "import_seed", "jsonb", "list_sources"]
