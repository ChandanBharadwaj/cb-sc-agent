-- =====================================================================================================
-- Analytics schema: AGGREGATE-ONLY views for dashboards and the natural-language Q&A agent.
--
-- No view exposes names, identifiers, addresses, birth dates or any other record-level list data.
-- The read-only role `sanctions_analyst` can read ONLY this schema, so "no row-level data" is enforced
-- by the database, not just by the prompt.
-- =====================================================================================================

CREATE SCHEMA IF NOT EXISTS analytics;

CREATE OR REPLACE VIEW analytics.v_source_status AS
SELECT s.source_id, s.display_name, s.level, s.kind, s.adapter_type, s.is_core, s.status, s.status_reason,
       s.paused_until, s.breaker_state, s.consecutive_failures, s.priority, s.schedule_kind,
       round(extract(epoch FROM s.cadence) / 60) AS cadence_minutes, s.cron_expr, s.timezone,
       s.last_attempt_at, s.last_fetch_at, s.last_success_at, s.last_change_at, s.next_due_at,
       round((extract(epoch FROM now() - s.last_success_at) / 3600)::numeric, 2) AS hours_since_success,
       round((extract(epoch FROM now() - s.last_change_at) / 3600)::numeric, 2) AS hours_since_change,
       round((extract(epoch FROM s.warn_staleness) / 3600)::numeric, 1) AS warn_staleness_hours,
       round((extract(epoch FROM s.hard_max_staleness) / 3600)::numeric, 1) AS hard_staleness_hours,
       CASE WHEN s.status <> 'ACTIVE' THEN lower(s.status)
            WHEN s.last_success_at IS NULL THEN 'never_succeeded'
            WHEN now() - s.last_success_at > s.hard_max_staleness THEN 'stale'
            WHEN now() - s.last_success_at > s.warn_staleness THEN 'stale_warning'
            WHEN s.breaker_state <> 'CLOSED' OR s.consecutive_failures > 0 THEN 'degraded'
            ELSE 'healthy' END AS health,
       v.seq AS current_version_seq, v.publication_marker, v.published_at_source, v.published_at,
       v.record_count, v.counts_by_type, s.licence, s.licence_status, s.config_version
FROM sanctions.source s
LEFT JOIN sanctions.list_version v ON v.version_id = s.current_version_id;

CREATE OR REPLACE VIEW analytics.v_run_summary AS
SELECT r.run_id, r.source_id, r.run_kind, r.trigger, r.requested_by, r.reason, r.status, r.current_step, r.attempt,
       r.queued_at, r.started_at, r.finished_at,
       round(extract(epoch FROM coalesce(r.finished_at, now()) - r.started_at)::numeric, 1) AS duration_seconds,
       r.error_class, left(r.error_detail, 300) AS error_detail,
       (r.progress ->> 'pct')::numeric AS progress_pct, r.config_version,
       r.resumed_from_run_id, r.agent_cycle_id, r.version_id,
       (r.summary ->> 'added')::int AS added, (r.summary ->> 'changed')::int AS changed,
       (r.summary ->> 'removed')::int AS removed, (r.summary ->> 'relisted')::int AS relisted
FROM sanctions.ingestion_run r;

CREATE OR REPLACE VIEW analytics.v_run_progress AS
SELECT r.run_id, r.source_id, r.run_kind, r.status, r.current_step,
       (r.progress ->> 'pct')::numeric AS pct, (r.progress ->> 'bytes_done')::bigint AS bytes_done,
       (r.progress ->> 'bytes_total')::bigint AS bytes_total, (r.progress ->> 'records_done')::int AS records_done,
       (r.progress ->> 'records_expected')::int AS records_expected, (r.progress ->> 'items_done')::int AS items_done,
       (r.progress ->> 'items_total')::int AS items_total, (r.progress ->> 'retries')::int AS retries,
       (r.progress ->> 'eta_s')::numeric AS eta_seconds, r.started_at, r.heartbeat_at, r.lease_expires_at
FROM sanctions.ingestion_run r WHERE r.status IN ('QUEUED', 'RUNNING');

CREATE OR REPLACE VIEW analytics.v_version_counts AS
SELECT v.version_id, v.source_id, v.seq, v.status, v.record_count, v.counts_by_type, v.publication_marker,
       v.published_at_source, v.created_at, v.published_at, v.released_by,
       (v.change_summary ->> 'added')::int AS added, (v.change_summary ->> 'changed')::int AS changed,
       (v.change_summary ->> 'removed')::int AS removed, (v.change_summary ->> 'relisted')::int AS relisted,
       v.validation_report ->> 'outcome' AS validation_outcome
FROM sanctions.list_version v;

CREATE OR REPLACE VIEW analytics.v_fill_rates AS
SELECT m.source_id, m.version_id, v.seq, v.status AS version_status, m.entity_type, m.field, m.value AS fill_rate,
       m.floor, m.prev_value AS previous_fill_rate, m.status, m.created_at
FROM sanctions.dq_metric m JOIN sanctions.list_version v USING (version_id)
WHERE m.metric = 'fill_rate';

CREATE OR REPLACE VIEW analytics.v_quality_metrics AS
SELECT m.source_id, m.version_id, v.seq, v.status AS version_status, m.entity_type, m.metric, m.field, m.value,
       m.floor, m.prev_value, m.status, m.created_at
FROM sanctions.dq_metric m JOIN sanctions.list_version v USING (version_id);

CREATE OR REPLACE VIEW analytics.v_change_volume AS
SELECT e.source_id, e.version_id, v.seq, e.created_at::date AS day, e.change_type, coalesce(e.entity_type, 'UNKNOWN') AS entity_type,
       count(*) AS n
FROM sanctions.change_event e JOIN sanctions.list_version v USING (version_id)
GROUP BY 1, 2, 3, 4, 5, 6;

CREATE OR REPLACE VIEW analytics.v_dq_issues AS
SELECT i.issue_id, i.source_id, i.version_id, i.category, i.severity, i.entity_type, i.field, i.count,
       i.detail ->> 'message' AS message, i.first_seen, i.last_seen, i.status
FROM sanctions.dq_issue i;

CREATE OR REPLACE VIEW analytics.v_enrichment_coverage AS
WITH subjects AS (
  SELECT rv.source_id, rv.entity_type, count(*) AS current_records
  FROM sanctions.record_version rv WHERE rv.valid_to_seq IS NULL GROUP BY 1, 2
), enr AS (
  SELECT e.provider, e.subject_source_id AS source_id, e.subject_entity_type AS entity_type, e.status, count(*) AS n
  FROM sanctions.enrichment_record e WHERE e.superseded_by IS NULL GROUP BY 1, 2, 3, 4
)
SELECT enr.provider, enr.source_id, enr.entity_type, subjects.current_records, enr.status, enr.n,
       round(enr.n::numeric / nullif(subjects.current_records, 0), 4) AS share_of_records
FROM enr LEFT JOIN subjects USING (source_id, entity_type);

CREATE OR REPLACE VIEW analytics.v_notice_coverage AS
SELECT e.source_id, e.change_type, count(*) AS change_events,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM sanctions.notice_link l WHERE l.source_id = e.source_id
                                      AND l.source_key = e.source_key AND l.status IN ('AUTO', 'APPROVED'))) AS with_legal_evidence
FROM sanctions.change_event e WHERE e.created_at > now() - interval '90 days'
GROUP BY 1, 2;

CREATE OR REPLACE VIEW analytics.v_notices AS
SELECT n.notice_id, n.provider, n.title, n.published_on, n.url, n.action_types, n.extraction_status, n.fetched_at,
       (SELECT count(*) FROM sanctions.notice_link l WHERE l.notice_id = n.notice_id) AS links
FROM sanctions.legal_notice n;

CREATE OR REPLACE VIEW analytics.v_removal_holds AS
SELECT source_id, status, count(*) AS n, min(created_at) AS oldest
FROM sanctions.removal_candidate GROUP BY 1, 2;

CREATE OR REPLACE VIEW analytics.v_proposals AS
SELECT kind, status, source_id, count(*) AS n, min(created_at) AS oldest, max(created_at) AS newest
FROM sanctions.proposed_change GROUP BY 1, 2, 3;

CREATE OR REPLACE VIEW analytics.v_incidents AS
SELECT incident_id, source_id, error_class, severity, status, title, left(summary, 500) AS summary,
       left(diagnosis, 1000) AS diagnosis, occurrences, opened_at, last_seen_at, resolved_at, alerts_sent
FROM sanctions.incident;

CREATE OR REPLACE VIEW analytics.v_agent_activity AS
SELECT c.cycle_id, c.agent_name, c.trigger, c.gate_reasons, c.model, c.status, c.started_at, c.finished_at,
       c.input_tokens, c.cached_tokens, c.output_tokens, c.cost_usd, c.fallback_used,
       c.report ->> 'summary' AS summary, c.report ->> 'health' AS health, left(c.error, 300) AS error,
       (SELECT count(*) FROM sanctions.agent_action a WHERE a.cycle_id = c.cycle_id) AS tool_calls,
       (SELECT count(*) FROM sanctions.agent_action a WHERE a.cycle_id = c.cycle_id AND a.guard_verdict = 'DENIED') AS denied_calls
FROM sanctions.agent_cycle c;

CREATE OR REPLACE VIEW analytics.v_agent_tool_usage AS
SELECT a.cycle_id, a.tool, a.guard_verdict, count(*) AS n, round(avg(a.duration_ms)) AS avg_ms
FROM sanctions.agent_action a GROUP BY 1, 2, 3;

CREATE OR REPLACE VIEW analytics.v_signals AS
SELECT provider, target_source_id, count(*) AS n, count(*) FILTER (WHERE consumed_at IS NULL) AS unconsumed,
       min(observed_at) FILTER (WHERE consumed_at IS NULL) AS oldest_unconsumed
FROM sanctions.signal GROUP BY 1, 2;

CREATE OR REPLACE VIEW analytics.v_snapshots AS
SELECT s.snapshot_id, s.created_at, s.note, cardinality(s.held_removal_ids) AS held_removals,
       (SELECT count(*) FROM sanctions.snapshot_version sv WHERE sv.snapshot_id = s.snapshot_id) AS sources_pinned
FROM sanctions.screening_snapshot s;

CREATE OR REPLACE VIEW analytics.v_field_catalog AS
SELECT source_id, canonical_field, entity_types, source_path, description, fill_floor, notes, updated_at
FROM sanctions.field_catalog;

CREATE OR REPLACE VIEW analytics.v_fetch_health AS
SELECT source_id, date_trunc('hour', fetched_at) AS hour, count(*) AS attempts,
       count(*) FILTER (WHERE error_class IS NULL) AS ok, count(*) FILTER (WHERE error_class IS NOT NULL) AS failed,
       array_agg(DISTINCT error_class) FILTER (WHERE error_class IS NOT NULL) AS error_classes,
       round(avg(duration_ms)) AS avg_ms, max(size_bytes) AS max_bytes
FROM sanctions.fetch_evidence GROUP BY 1, 2;

-- ---------------------------------------------------------------------------------------------------
-- Read-only, aggregate-only role for the Q&A agent
-- ---------------------------------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sanctions_analyst') THEN
    CREATE ROLE sanctions_analyst NOLOGIN;
  END IF;
END $$;
REVOKE ALL ON SCHEMA sanctions FROM sanctions_analyst;
REVOKE ALL ON ALL TABLES IN SCHEMA sanctions FROM sanctions_analyst;
GRANT USAGE ON SCHEMA analytics TO sanctions_analyst;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO sanctions_analyst;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT ON TABLES TO sanctions_analyst;
ALTER ROLE sanctions_analyst SET default_transaction_read_only = on;
ALTER ROLE sanctions_analyst SET statement_timeout = '5s';
ALTER ROLE sanctions_analyst SET search_path = analytics;
