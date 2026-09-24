"""Compact system state for the supervisor prompt. Aggregates only - never list records."""

from __future__ import annotations

from typing import Any

import psycopg

from sanctions_agent.db.engine import fetch_all


def _h(v: Any) -> float | None:
    return round(v.total_seconds() / 3600, 1) if v is not None else None


def sources(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    rows = fetch_all(
        conn,
        """
        SELECT s.source_id, s.level, s.kind, s.status, s.is_core, s.breaker_state, s.consecutive_failures,
               now() - s.last_success_at AS since_success, now() - s.last_change_at AS since_change,
               now() - s.last_attempt_at AS since_attempt, s.next_due_at - now() AS due_in,
               s.warn_staleness, s.hard_max_staleness, s.min_interval, s.status_reason,
               v.seq, v.publication_marker, v.record_count,
               r.run_id AS active_run, r.status AS active_status, r.current_step, r.progress->>'pct' AS pct
        FROM source s
        LEFT JOIN list_version v ON v.version_id = s.current_version_id
        LEFT JOIN LATERAL (SELECT * FROM ingestion_run x WHERE x.source_id = s.source_id AND x.status IN ('QUEUED','RUNNING')
                           ORDER BY queued_at DESC LIMIT 1) r ON true
        ORDER BY s.level, s.priority, s.source_id""",
    )
    out = []
    for r in rows:
        since = r["since_success"]
        health = "unknown"
        if r["status"] != "ACTIVE":
            health = r["status"].lower()
        elif since is None:
            health = "never_succeeded"
        elif since > r["hard_max_staleness"]:
            health = "stale_page"
        elif since > r["warn_staleness"]:
            health = "stale_warn"
        elif r["breaker_state"] != "CLOSED" or r["consecutive_failures"]:
            health = "degraded"
        else:
            health = "healthy"
        out.append(
            {
                "source": r["source_id"],
                "level": r["level"],
                "kind": r["kind"],
                "status": r["status"],
                "core": r["is_core"],
                "health": health,
                "hours_since_success": _h(since),
                "hours_since_change": _h(r["since_change"]),
                "hours_since_attempt": _h(r["since_attempt"]),
                "due_in_minutes": round(r["due_in"].total_seconds() / 60)
                if r["due_in"] is not None
                else None,
                "min_interval_minutes": int(r["min_interval"].total_seconds() // 60),
                "breaker": r["breaker_state"],
                "failures_in_row": r["consecutive_failures"],
                "version_seq": r["seq"],
                "marker": r["publication_marker"],
                "records": r["record_count"],
                "active_run": str(r["active_run"]) if r["active_run"] else None,
                "active_step": r["current_step"],
                "active_pct": r["pct"],
                "status_reason": r["status_reason"],
            }
        )
    return out


def snapshot(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    return {
        "sources": sources(conn),
        "open_incidents": fetch_all(
            conn,
            """SELECT incident_id, source_id, error_class, severity, title, occurrences,
                round(extract(epoch FROM now() - opened_at)/3600, 1) AS age_h, diagnosis IS NOT NULL AS diagnosed
            FROM incident WHERE status <> 'RESOLVED' ORDER BY severity DESC, opened_at LIMIT 30""",
        ),
        "recent_failures": fetch_all(
            conn,
            """SELECT run_id::text, source_id, status, error_class,
                left(error_detail, 240) AS detail, round(extract(epoch FROM now() - finished_at)/3600, 1) AS age_h
            FROM ingestion_run WHERE status IN ('FAILED','QUARANTINED','HELD','ABANDONED')
              AND finished_at > now() - interval '24 hours' ORDER BY finished_at DESC LIMIT 20""",
        ),
        "pending_proposals": fetch_all(
            conn,
            """SELECT kind, count(*) AS n FROM proposed_change WHERE status = 'PENDING'
            GROUP BY 1""",
        ),
        "removal_candidates": fetch_all(
            conn,
            """SELECT source_id, status, count(*) AS n FROM removal_candidate
            WHERE status IN ('PENDING','EVIDENCE_FOUND','REJECTED_PARSER') GROUP BY 1, 2""",
        ),
        "unconsumed_signals": fetch_all(
            conn,
            """SELECT target_source_id, count(*) AS n, min(observed_at)::text AS oldest
            FROM signal WHERE consumed_at IS NULL GROUP BY 1""",
        ),
        "notices_needing_extraction": fetch_all(
            conn,
            """SELECT provider, count(*) AS n FROM legal_notice
            WHERE extraction_status = 'NEEDS_AGENT' GROUP BY 1""",
        ),
        "open_dq_issues": fetch_all(
            conn,
            """SELECT source_id, category, severity, count(*) AS n FROM dq_issue
            WHERE status = 'OPEN' AND severity <> 'INFO' GROUP BY 1, 2, 3 ORDER BY 1""",
        ),
    }
