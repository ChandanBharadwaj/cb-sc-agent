"""Incidents: deduplicated, auditable records of operational problems (and what was done about them)."""

from __future__ import annotations

from typing import Any

import psycopg

from sanctions_agent.db.engine import fetch_all, fetch_one, jsonb

_SEVERITY_RANK = {"INFO": 0, "WARN": 1, "PAGE": 2}


def open_incident(
    conn: psycopg.Connection[Any],
    *,
    error_class: str,
    severity: str,
    title: str,
    source_id: str | None = None,
    summary: str | None = None,
    dedupe_key: str | None = None,
) -> tuple[int, bool]:
    """Open an incident or bump the open one with the same dedupe key. Returns (incident_id, is_new)."""
    key = dedupe_key or f"{source_id or 'global'}:{error_class}"
    row = fetch_one(
        conn,
        "SELECT incident_id, severity FROM incident WHERE dedupe_key = %s AND status <> 'RESOLVED' FOR UPDATE",
        (key,),
    )
    if row:
        new_sev = severity if _SEVERITY_RANK[severity] > _SEVERITY_RANK[row["severity"]] else row["severity"]
        conn.execute(
            "UPDATE incident SET occurrences = occurrences + 1, last_seen_at = now(), severity = %s,"
            " summary = coalesce(%s, summary) WHERE incident_id = %s",
            (new_sev, summary, row["incident_id"]),
        )
        return int(row["incident_id"]), False
    new = fetch_one(
        conn,
        "INSERT INTO incident (source_id, error_class, severity, dedupe_key, title, summary)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING incident_id",
        (source_id, error_class, severity, key, title, summary),
    )
    assert new is not None
    return int(new["incident_id"]), True


def add_action(conn: psycopg.Connection[Any], incident_id: int, action: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE incident SET actions = actions || %s WHERE incident_id = %s", (jsonb([action]), incident_id)
    )


def set_diagnosis(conn: psycopg.Connection[Any], incident_id: int, diagnosis: str) -> None:
    conn.execute("UPDATE incident SET diagnosis = %s WHERE incident_id = %s", (diagnosis, incident_id))


def resolve(
    conn: psycopg.Connection[Any],
    *,
    incident_id: int | None = None,
    source_id: str | None = None,
    error_classes: list[str] | None = None,
    note: str | None = None,
) -> int:
    """Resolve by id, or all open incidents of a source (optionally restricted to error classes)."""
    if incident_id is not None:
        cur = conn.execute(
            "UPDATE incident SET status = 'RESOLVED', resolved_at = now(),"
            " summary = coalesce(summary, '') || coalesce(' | resolved: ' || %s, '') WHERE incident_id = %s AND status <> 'RESOLVED'",
            (note, incident_id),
        )
        return cur.rowcount
    sql = (
        "UPDATE incident SET status = 'RESOLVED', resolved_at = now(),"
        " summary = coalesce(summary, '') || coalesce(' | resolved: ' || %s, '')"
        " WHERE source_id = %s AND status <> 'RESOLVED'"
    )
    params: list[Any] = [note, source_id]
    if error_classes:
        sql += " AND error_class = ANY(%s)"
        params.append(error_classes)
    return conn.execute(sql, params).rowcount


def open_incidents(conn: psycopg.Connection[Any], source_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM incident WHERE status <> 'RESOLVED'"
    params: tuple[Any, ...] = ()
    if source_id:
        sql += " AND source_id = %s"
        params = (source_id,)
    return fetch_all(conn, sql + " ORDER BY opened_at DESC", params)
