"""Read-only, aggregate-only tools for the Q&A (analyst) agent.

Every query goes through ``db.analyst`` (role ``sanctions_analyst``, analytics views only, read-only,
5 s timeout). Nothing here can return record-level list data.
"""

from __future__ import annotations

from typing import Any

from agents import RunContextWrapper, Tool, function_tool

from sanctions_agent.agent.context import AgentContext
from sanctions_agent.agent.guards import ToolDenied, guarded
from sanctions_agent.agent.sql_guard import SqlRejected, guard
from sanctions_agent.db import analyst

Ctx = RunContextWrapper[AgentContext]


@guarded("read")
def get_ingestion_overview(rc: Ctx) -> list[dict[str, Any]]:
    """Every source: health, status, freshness (hours since success/change vs thresholds), breaker, failures,
    current version, record counts by entity type, next due time."""
    return analyst.query("""SELECT source_id, level, kind, is_core, status, health, hours_since_success,
        hours_since_change, warn_staleness_hours, hard_staleness_hours, breaker_state, consecutive_failures,
        current_version_seq, publication_marker, record_count, counts_by_type, next_due_at, status_reason
        FROM v_source_status ORDER BY level, source_id""")


@guarded("read")
def get_source_detail(rc: Ctx, source_id: str) -> dict[str, Any]:
    """One source: status, last 10 runs, last 10 versions with change counts, open data-quality issues."""
    return {
        "status": analyst.query("SELECT * FROM v_source_status WHERE source_id = %s", (source_id,)),
        "recent_runs": analyst.query(
            """SELECT run_id, run_kind, trigger, status, current_step, error_class, error_detail,
            queued_at, finished_at, duration_seconds, added, changed, removed FROM v_run_summary WHERE source_id = %s
            ORDER BY queued_at DESC LIMIT 10""",
            (source_id,),
        ),
        "versions": analyst.query(
            """SELECT seq, status, record_count, counts_by_type, added, changed, removed,
            publication_marker, published_at, validation_outcome FROM v_version_counts WHERE source_id = %s
            ORDER BY seq DESC LIMIT 10""",
            (source_id,),
        ),
        "open_issues": analyst.query(
            """SELECT category, severity, entity_type, field, count, message, last_seen
            FROM v_dq_issues WHERE source_id = %s AND status = 'OPEN' ORDER BY severity, category""",
            (source_id,),
        ),
    }


@guarded("read")
def get_run_progress(rc: Ctx, source_id: str | None = None) -> list[dict[str, Any]]:
    """Runs queued or in progress right now, with step, percent, bytes/records done, retries and ETA."""
    return analyst.query(
        "SELECT * FROM v_run_progress WHERE (%s::text IS NULL OR source_id = %s) ORDER BY started_at",
        (source_id, source_id),
    )


@guarded("read")
def list_runs(
    rc: Ctx, source_id: str | None = None, status: str | None = None, hours: int = 24, limit: int = 25
) -> list[dict[str, Any]]:
    """Runs in the last N hours, optionally for one source and/or status (SUCCEEDED, NO_CHANGE, FAILED, ...)."""
    return analyst.query(
        """SELECT run_id, source_id, run_kind, trigger, requested_by, status, error_class, error_detail,
        queued_at, finished_at, duration_seconds, added, changed, removed FROM v_run_summary
        WHERE queued_at > now() - make_interval(hours => %s) AND (%s::text IS NULL OR source_id = %s)
          AND (%s::text IS NULL OR status = %s) ORDER BY queued_at DESC LIMIT %s""",
        (min(hours, 24 * 90), source_id, source_id, status, status, min(limit, 100)),
    )


@guarded("read")
def list_run_batches(
    rc: Ctx, days: int = 7, trigger: str | None = None, status: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """Run batches (runs started together): SCHEDULED cycles (clock-aligned, e.g. every 2 h at :00 UTC), AGENT
    cycles and MANUAL ad-hoc requests. Per batch: sources requested / run / refused, how many ended ok /
    needing review (held, quarantined) / failed, retried attempts, duration, totals added/changed/removed and a
    derived status (RUNNING, COMPLETED, NEEDS_REVIEW, FAILED, CANCELLED, REFUSED). Use for 'how did last
    night's scheduled runs go?' or 'what did alice's ad-hoc run do?'."""
    return analyst.query(
        """SELECT batch_id, trigger, requested_by, reason, mode, created_at, requested_sources, sources_requested,
        sources_run, sources_refused, ok, attention, failed, cancelled, active, retried_attempts, started_at,
        finished_at, duration_seconds, added, changed, removed, status FROM v_run_batches
        WHERE created_at > now() - make_interval(days => %s) AND (%s::text IS NULL OR trigger = %s)
          AND (%s::text IS NULL OR status = %s) ORDER BY created_at DESC LIMIT %s""",
        (min(days, 90), trigger, trigger, status, status, min(limit, 100)),
    )


@guarded("read")
def get_counts(rc: Ctx, source_id: str | None = None) -> list[dict[str, Any]]:
    """Current published record counts per source and entity type (PERSON, ORGANIZATION, VESSEL, AIRCRAFT)."""
    return analyst.query(
        """SELECT s.source_id, e.key AS entity_type, e.value::int AS records
        FROM v_source_status s, jsonb_each_text(coalesce(s.counts_by_type, '{}'::jsonb)) e
        WHERE (%s::text IS NULL OR s.source_id = %s) ORDER BY 1, 2""",
        (source_id, source_id),
    )


@guarded("read")
def get_count_trend(rc: Ctx, source_id: str, days: int = 30) -> list[dict[str, Any]]:
    """Record counts and added/changed/removed per published version over the last N days."""
    return analyst.query(
        """SELECT seq, published_at, record_count, added, changed, removed FROM v_version_counts
        WHERE source_id = %s AND status IN ('PUBLISHED','SUPERSEDED') AND published_at > now() - make_interval(days => %s)
        ORDER BY seq""",
        (source_id, min(days, 365)),
    )


@guarded("read")
def get_change_summary(rc: Ctx, days: int = 7, source_id: str | None = None) -> list[dict[str, Any]]:
    """Additions / changes / removals / re-listings by source and entity type over the last N days."""
    return analyst.query(
        """SELECT source_id, change_type, entity_type, sum(n)::int AS n FROM v_change_volume
        WHERE day > current_date - %s AND (%s::text IS NULL OR source_id = %s)
        GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""",
        (min(days, 365), source_id, source_id),
    )


@guarded("read")
def get_fill_rates(
    rc: Ctx, source_id: str | None = None, entity_type: str | None = None, field: str | None = None
) -> list[dict[str, Any]]:
    """Field fill rates (share of records with the field) on the current published version of each source,
    with configured floors and the previous version's rate. Fields include dob, dob_full, nationality,
    passport, national_id, address, address_country, country, registration_id, lei, imo, mmsi, call_sign,
    flag, owner_link, msn, tail, listed_on, program, measures, reason, alias."""
    return analyst.query(
        """SELECT f.source_id, f.entity_type, f.field, f.fill_rate, f.floor, f.previous_fill_rate, f.status
        FROM v_fill_rates f WHERE f.version_status = 'PUBLISHED'
          AND (%s::text IS NULL OR f.source_id = %s) AND (%s::text IS NULL OR f.entity_type = %s)
          AND (%s::text IS NULL OR f.field = %s) ORDER BY 1, 2, 3""",
        (source_id, source_id, entity_type, entity_type, field, field),
    )


@guarded("read")
def get_quality_metrics(rc: Ctx, source_id: str | None = None) -> list[dict[str, Any]]:
    """Other quality metrics on current versions: weak-alias share, IMO / LEI checksum pass rates, unmapped
    country values, birth-date precision mix."""
    return analyst.query(
        """SELECT source_id, entity_type, metric, field, value FROM v_quality_metrics
        WHERE version_status = 'PUBLISHED' AND metric NOT IN ('fill_rate', 'count')
          AND (%s::text IS NULL OR source_id = %s) ORDER BY 1, 3, 4""",
        (source_id, source_id),
    )


@guarded("read")
def get_data_quality_issues(
    rc: Ctx, source_id: str | None = None, status: str = "OPEN", severity: str | None = None
) -> list[dict[str, Any]]:
    """Data-quality issues (schema drift, fill rate below floor, count anomaly, unmapped countries, invalid
    checksums, unparseable dates, duplicates...) with counts - aggregate only."""
    return analyst.query(
        """SELECT source_id, category, severity, entity_type, field, count, message, first_seen, last_seen,
        status FROM v_dq_issues WHERE (%s::text IS NULL OR source_id = %s) AND status = %s
          AND (%s::text IS NULL OR severity = %s) ORDER BY last_seen DESC LIMIT 100""",
        (source_id, source_id, status, severity, severity),
    )


@guarded("read")
def get_enrichment_coverage(rc: Ctx, provider: str | None = None) -> list[dict[str, Any]]:
    """Level-2 enrichment coverage: per provider and list, how many records matched (auto / review / none)."""
    return analyst.query(
        """SELECT * FROM v_enrichment_coverage WHERE (%s::text IS NULL OR provider = %s)
        ORDER BY provider, source_id, entity_type, status""",
        (provider, provider),
    )


@guarded("read")
def get_evidence_coverage(rc: Ctx) -> list[dict[str, Any]]:
    """Share of recent list changes (90 days) linked to an official notice (legal evidence, FR-14), plus
    removal holds awaiting confirmation."""
    return {
        "notice_coverage": analyst.query("SELECT * FROM v_notice_coverage ORDER BY source_id, change_type"),
        "removal_holds": analyst.query("SELECT * FROM v_removal_holds ORDER BY source_id, status"),
    }  # type: ignore[return-value]


@guarded("read")
def describe_field(rc: Ctx, field: str | None = None, source_id: str | None = None) -> list[dict[str, Any]]:
    """Data dictionary: which source element maps to which canonical field, description, floors and notes
    (e.g. which lists carry MMSI, how alias quality is represented)."""
    return analyst.query(
        """SELECT source_id, canonical_field, entity_types, source_path, description, fill_floor, notes
        FROM v_field_catalog WHERE (%s::text IS NULL OR canonical_field ILIKE '%%' || %s || '%%'
                                    OR description ILIKE '%%' || %s || '%%')
          AND (%s::text IS NULL OR source_id = %s) ORDER BY canonical_field, source_id""",
        (field, field, field, source_id, source_id),
    )


@guarded("read")
def list_incidents(rc: Ctx, status: str = "OPEN") -> list[dict[str, Any]]:
    """Operational incidents (staleness, fetch failures, quarantines, holds) with severity and diagnosis."""
    return analyst.query(
        """SELECT incident_id, source_id, error_class, severity, status, title, summary, diagnosis,
        occurrences, opened_at, resolved_at FROM v_incidents WHERE (%s = 'ALL' OR status = %s)
        ORDER BY opened_at DESC LIMIT 50""",
        (status, status),
    )


@guarded("read")
def get_agent_activity(rc: Ctx, hours: int = 24) -> list[dict[str, Any]]:
    """What the supervisor / extractor agents did: cycles, status, tool calls, denials, tokens and cost."""
    return analyst.query(
        """SELECT agent_name, trigger, status, started_at, tool_calls, denied_calls, input_tokens,
        output_tokens, cost_usd, fallback_used, summary FROM v_agent_activity
        WHERE started_at > now() - make_interval(hours => %s) ORDER BY started_at DESC LIMIT 50""",
        (min(hours, 720),),
    )


@guarded("read")
def get_review_queue(rc: Ctx) -> list[dict[str, Any]]:
    """Counts of proposals by kind and status (pending human reviews)."""
    return analyst.query("SELECT kind, status, source_id, n, oldest FROM v_proposals ORDER BY status, kind")


@guarded("read")
def query_analytics_sql(rc: Ctx, sql: str) -> list[dict[str, Any]]:
    """Escape hatch: a single SELECT over analytics views (v_source_status, v_run_batches, v_run_summary, v_run_progress,
    v_version_counts, v_fill_rates, v_quality_metrics, v_change_volume, v_dq_issues, v_enrichment_coverage,
    v_notice_coverage, v_notices, v_removal_holds, v_proposals, v_incidents, v_agent_activity,
    v_agent_tool_usage, v_signals, v_snapshots, v_field_catalog, v_fetch_health). Max 500 rows."""
    try:
        safe = guard(sql)
    except SqlRejected as e:
        raise ToolDenied(str(e)) from e
    return analyst.query(safe)


ANALYST_FUNCS = [
    get_ingestion_overview,
    get_source_detail,
    get_run_progress,
    list_runs,
    list_run_batches,
    get_counts,
    get_count_trend,
    get_change_summary,
    get_fill_rates,
    get_quality_metrics,
    get_data_quality_issues,
    get_enrichment_coverage,
    get_evidence_coverage,
    describe_field,
    list_incidents,
    get_agent_activity,
    get_review_queue,
    query_analytics_sql,
]


def analyst_tools() -> list[Tool]:
    return [function_tool(f) for f in ANALYST_FUNCS]  # type: ignore[call-overload]
