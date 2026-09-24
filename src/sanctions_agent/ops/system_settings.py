"""Global runtime settings editable by admins from the UI (audited). Environment values are defaults."""

from __future__ import annotations

from typing import Any

import psycopg

from sanctions_agent.db.engine import fetch_all, jsonb
from sanctions_agent.settings import get_settings


def defaults() -> dict[str, Any]:
    s = get_settings()
    return {
        "maintenance_mode": {"enabled": False, "reason": None},
        "agent_enabled": True,
        "agent_model": s.agent_model,
        "agent_reasoning_effort": s.agent_reasoning_effort,
        "agent_daily_budget_usd": s.agent_daily_budget_usd,
        "agent_health_review_minutes": s.agent_health_review_minutes,
        "alert_channels": [
            c
            for c, on in (
                ("log", True),
                ("webhook", bool(s.alert_webhook_url)),
                ("email", bool(s.alert_email_to and s.smtp_host)),
            )
            if on
        ],
        "page_realert_minutes": 60,
        "core_disabled_realert_hours": 6,
    }


def get_all(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    out = defaults()
    for r in fetch_all(conn, "SELECT key, value FROM system_setting"):
        out[r["key"]] = r["value"]
    return out


def get(conn: psycopg.Connection[Any], key: str) -> Any:
    return get_all(conn).get(key)


def set_value(conn: psycopg.Connection[Any], key: str, value: Any, actor: str) -> None:
    if key not in defaults():
        raise KeyError(f"unknown setting {key}")
    conn.execute(
        "INSERT INTO system_setting (key, value, updated_by) VALUES (%s, %s, %s)"
        " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()",
        (key, jsonb(value), actor),
    )


def maintenance_on(conn: psycopg.Connection[Any]) -> bool:
    mm = get(conn, "maintenance_mode") or {}
    return bool(mm.get("enabled"))
