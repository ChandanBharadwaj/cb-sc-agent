-- 0003: run batches. A batch groups the per-source runs started together: one scheduled cycle (clock-aligned
-- intervals make sources with the same cadence due in the same tick), one agent cycle, or one ad-hoc request
-- for several sources. Batch status is derived from its runs (never stored), so it cannot drift.

CREATE TABLE sanctions.run_batch (
  batch_id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  trigger            text        NOT NULL CHECK (trigger IN ('SCHEDULED','AGENT','MANUAL')),
  requested_by       text        NOT NULL,
  reason             text,
  options            jsonb       NOT NULL DEFAULT '{}'::jsonb,   -- {mode, limit, override_min_interval}
  requested_sources  text[]      NOT NULL DEFAULT '{}',          -- what the requester selected
  refused            jsonb       NOT NULL DEFAULT '[]'::jsonb,   -- [{source_id, why}] not queued
  agent_cycle_id     uuid        REFERENCES sanctions.agent_cycle (cycle_id),
  created_at         timestamptz NOT NULL DEFAULT clock_timestamp()  -- ordering stays exact within one transaction
);
CREATE INDEX ix_run_batch_created ON sanctions.run_batch (created_at DESC);
CREATE UNIQUE INDEX ux_run_batch_agent_cycle ON sanctions.run_batch (agent_cycle_id) WHERE trigger = 'AGENT';
CREATE TRIGGER tr_run_batch_audit AFTER INSERT OR UPDATE ON sanctions.run_batch
  FOR EACH ROW EXECUTE FUNCTION sanctions.audit_row('batch_id');

ALTER TABLE sanctions.ingestion_run ADD COLUMN batch_id uuid REFERENCES sanctions.run_batch (batch_id);

-- backfill: every existing run becomes a batch of one
INSERT INTO sanctions.run_batch (batch_id, trigger, requested_by, reason, options, requested_sources, agent_cycle_id,
                                 created_at)
SELECT r.run_id,
       CASE r.trigger WHEN 'AGENT' THEN 'AGENT' WHEN 'MANUAL' THEN 'MANUAL' ELSE 'SCHEDULED' END,
       coalesce(r.requested_by, 'system'), r.reason, '{}'::jsonb, ARRAY[r.source_id], r.agent_cycle_id, r.queued_at
FROM sanctions.ingestion_run r WHERE r.batch_id IS NULL;
UPDATE sanctions.ingestion_run SET batch_id = run_id WHERE batch_id IS NULL;
ALTER TABLE sanctions.ingestion_run ALTER COLUMN batch_id SET NOT NULL;
CREATE INDEX ix_ingestion_run_batch ON sanctions.ingestion_run (batch_id, source_id, queued_at DESC);

-- ---------------------------------------------------------------------------------------------------------
-- analytics (aggregate only). New columns are appended so CREATE OR REPLACE keeps existing column order.
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
       v.record_count, v.counts_by_type, s.licence, s.licence_status, s.config_version,
       round(extract(epoch FROM s.min_interval) / 60) AS min_interval_minutes
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
       (r.summary ->> 'removed')::int AS removed, (r.summary ->> 'relisted')::int AS relisted,
       r.batch_id
FROM sanctions.ingestion_run r;

CREATE OR REPLACE VIEW analytics.v_run_progress AS
SELECT r.run_id, r.source_id, r.run_kind, r.status, r.current_step,
       (r.progress ->> 'pct')::numeric AS pct, (r.progress ->> 'bytes_done')::bigint AS bytes_done,
       (r.progress ->> 'bytes_total')::bigint AS bytes_total, (r.progress ->> 'records_done')::int AS records_done,
       (r.progress ->> 'records_expected')::int AS records_expected, (r.progress ->> 'items_done')::int AS items_done,
       (r.progress ->> 'items_total')::int AS items_total, (r.progress ->> 'retries')::int AS retries,
       (r.progress ->> 'eta_s')::numeric AS eta_seconds, r.started_at, r.heartbeat_at, r.lease_expires_at,
       r.batch_id
FROM sanctions.ingestion_run r WHERE r.status IN ('QUEUED', 'RUNNING');

-- One row per batch. Counts use the LATEST attempt per source (a resumed run replaces its abandoned attempt).
CREATE OR REPLACE VIEW analytics.v_run_batches AS
WITH latest AS (
  SELECT DISTINCT ON (r.batch_id, r.source_id) r.batch_id, r.source_id, r.status, r.started_at, r.finished_at,
         (r.summary ->> 'added')::int AS added, (r.summary ->> 'changed')::int AS changed,
         (r.summary ->> 'removed')::int AS removed
  FROM sanctions.ingestion_run r ORDER BY r.batch_id, r.source_id, r.queued_at DESC, r.attempt DESC
), agg AS (
  SELECT l.batch_id, count(*) AS sources_run,
         count(*) FILTER (WHERE l.status IN ('QUEUED', 'RUNNING')) AS active,
         count(*) FILTER (WHERE l.status IN ('SUCCEEDED', 'NO_CHANGE', 'DRY_RUN_OK')) AS ok,
         count(*) FILTER (WHERE l.status IN ('HELD', 'QUARANTINED')) AS attention,
         count(*) FILTER (WHERE l.status IN ('FAILED', 'ABANDONED')) AS failed,
         count(*) FILTER (WHERE l.status = 'CANCELLED') AS cancelled,
         min(l.started_at) AS started_at, max(l.finished_at) AS last_finished_at,
         sum(l.added) AS added, sum(l.changed) AS changed, sum(l.removed) AS removed
  FROM latest l GROUP BY l.batch_id
), attempts AS (
  SELECT r.batch_id, count(*) AS attempts FROM sanctions.ingestion_run r GROUP BY r.batch_id
)
SELECT b.batch_id, b.trigger, b.requested_by, b.reason, b.options ->> 'mode' AS mode, b.agent_cycle_id,
       b.created_at, b.requested_sources, cardinality(b.requested_sources) AS sources_requested,
       jsonb_array_length(b.refused) AS sources_refused,
       coalesce(a.sources_run, 0) AS sources_run, coalesce(a.active, 0) AS active, coalesce(a.ok, 0) AS ok,
       coalesce(a.attention, 0) AS attention, coalesce(a.failed, 0) AS failed,
       coalesce(a.cancelled, 0) AS cancelled,
       coalesce(t.attempts, 0) - coalesce(a.sources_run, 0) AS retried_attempts,
       a.started_at,
       CASE WHEN coalesce(a.active, 0) = 0 THEN a.last_finished_at END AS finished_at,
       round(extract(epoch FROM coalesce(CASE WHEN coalesce(a.active, 0) = 0 THEN a.last_finished_at END, now())
                                - a.started_at)::numeric, 1) AS duration_seconds,
       a.added, a.changed, a.removed,
       CASE WHEN coalesce(a.sources_run, 0) = 0 THEN 'REFUSED'
            WHEN a.active > 0 THEN 'RUNNING'
            WHEN a.failed > 0 THEN 'FAILED'
            WHEN a.attention > 0 THEN 'NEEDS_REVIEW'
            WHEN a.cancelled = a.sources_run THEN 'CANCELLED'
            ELSE 'COMPLETED' END AS status
FROM sanctions.run_batch b
LEFT JOIN agg a ON a.batch_id = b.batch_id
LEFT JOIN attempts t ON t.batch_id = b.batch_id;

GRANT SELECT ON analytics.v_run_batches TO sanctions_analyst;
