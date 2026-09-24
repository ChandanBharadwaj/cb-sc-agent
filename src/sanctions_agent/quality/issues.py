"""Persisting data-quality metrics and issues (aggregate only - never record values)."""

from __future__ import annotations

from typing import Any

import psycopg

from sanctions_agent.db.engine import jsonb
from sanctions_agent.quality.metrics import VersionMetrics
from sanctions_agent.sources.base import Issue
from sanctions_agent.sources.config_models import ValidationConfig


def record_issues(
    conn: psycopg.Connection[Any], source_id: str, version_id: int | None, run_id: str, issues: list[Issue]
) -> None:
    for i in issues:
        entity_type, fld = i.field.split(".", 1) if i.field and "." in i.field else (None, i.field)
        conn.execute(
            """INSERT INTO dq_issue (source_id, version_id, run_id, category, severity, entity_type, field, count, detail,
                   status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                source_id,
                version_id,
                run_id,
                i.category,
                i.severity,
                entity_type,
                fld,
                i.count,
                jsonb({"message": i.message, **i.detail}),
                "OPEN" if i.severity != "INFO" else "ACCEPTED",
            ),
        )
    # issues of earlier versions of this source that did not recur are resolved
    if version_id is not None:
        conn.execute(
            """UPDATE dq_issue SET status = 'RESOLVED', last_seen = now()
               WHERE source_id = %s AND status = 'OPEN' AND (version_id IS NULL OR version_id <> %s)
                 AND (category, coalesce(field, '')) NOT IN
                     (SELECT category, coalesce(field, '') FROM dq_issue WHERE version_id = %s)""",
            (source_id, version_id, version_id),
        )


def record_metrics(
    conn: psycopg.Connection[Any],
    source_id: str,
    version_id: int,
    m: VersionMetrics,
    prev_fill: dict[tuple[str, str], float],
    cfg: ValidationConfig,
) -> None:
    rows = []
    for entity_type, metric, fld, value in m.rows():
        floor = cfg.fill_floors.get(f"{entity_type}.{fld}") if metric == "fill_rate" else None
        prev = prev_fill.get((entity_type, fld)) if metric == "fill_rate" else None
        status = "OK"
        if floor is not None:
            status = "FAIL" if value < floor else ("WARN" if value < floor + cfg.fill_warn_margin else "OK")
        rows.append((version_id, source_id, entity_type, metric, fld, value, floor, prev, status))
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO dq_metric (version_id, source_id, entity_type, metric, field, value, floor, prev_value, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (version_id, entity_type, metric, field) DO UPDATE
                 SET value = EXCLUDED.value, floor = EXCLUDED.floor, prev_value = EXCLUDED.prev_value,
                     status = EXCLUDED.status""",
            rows,
        )
