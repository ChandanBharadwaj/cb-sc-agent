-- =====================================================================================================
-- Sanctions ingestion: core schema (control plane, evidence, staging, versioned list data, L2, review,
-- agent, quality, Q&A). See docs/architecture.md for the data-model narrative.
--
-- Conventions
--   * all timestamps are timestamptz (UTC)
--   * status / kind columns are text + CHECK constraints (easier to evolve than PG enums)
--   * evidence and event tables are append-only (trigger raises on UPDATE/DELETE)
--   * mutable control tables carry an audit trigger writing to audit_log (actor from
--     `SET LOCAL sanctions.actor = '<user>'`)
-- =====================================================================================================

CREATE SCHEMA IF NOT EXISTS sanctions;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- -----------------------------------------------------------------------------------------------------
-- Generic helpers
-- -----------------------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION sanctions.forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'table %.% is append-only (% not allowed)', TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'insufficient_privilege';
END $$;

CREATE TABLE sanctions.audit_log (
  audit_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  table_name  text        NOT NULL,
  row_pk      text,
  op          text        NOT NULL,
  old         jsonb,
  new         jsonb,
  actor       text,
  at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_log_table_pk ON sanctions.audit_log (table_name, row_pk, at DESC);

CREATE OR REPLACE FUNCTION sanctions.audit_row() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  pk_col text := TG_ARGV[0];
  rec    jsonb;
BEGIN
  rec := CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
  INSERT INTO sanctions.audit_log (table_name, row_pk, op, old, new, actor)
  VALUES (TG_TABLE_NAME,
          rec ->> pk_col,
          TG_OP,
          CASE WHEN TG_OP IN ('UPDATE','DELETE') THEN to_jsonb(OLD) END,
          CASE WHEN TG_OP IN ('INSERT','UPDATE') THEN to_jsonb(NEW) END,
          coalesce(nullif(current_setting('sanctions.actor', true), ''), current_user));
  RETURN NULL;
END $$;

-- =====================================================================================================
-- A. Control plane
-- =====================================================================================================
CREATE TABLE sanctions.source (
  source_id           text PRIMARY KEY CHECK (source_id ~ '^[a-z][a-z0-9_]{1,62}$'),
  display_name        text        NOT NULL,
  adapter_type        text        NOT NULL,
  level               smallint    NOT NULL CHECK (level IN (1, 2)),
  kind                text        NOT NULL CHECK (kind IN ('STRUCTURED_LIST','CURATED_LIST','NOTICE_FEED','ENRICHMENT')),
  is_core             boolean     NOT NULL DEFAULT false,
  status              text        NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','ACTIVE','PAUSED','DISABLED')),
  status_reason       text,
  status_changed_by   text,
  status_changed_at   timestamptz,
  paused_until        timestamptz,
  config              jsonb       NOT NULL DEFAULT '{}'::jsonb,
  config_version      int         NOT NULL DEFAULT 0,
  schedule_kind       text        NOT NULL DEFAULT 'INTERVAL' CHECK (schedule_kind IN ('INTERVAL','CRON')),
  cadence             interval,
  cron_expr           text,
  timezone            text        NOT NULL DEFAULT 'UTC',
  min_interval        interval    NOT NULL DEFAULT '30 minutes',
  warn_staleness      interval    NOT NULL DEFAULT '6 hours',
  hard_max_staleness  interval    NOT NULL DEFAULT '12 hours',
  priority            smallint    NOT NULL DEFAULT 5 CHECK (priority BETWEEN 1 AND 9),
  licence             text,
  licence_status      text        NOT NULL DEFAULT 'OK'
                        CHECK (licence_status IN ('OK','ATTRIBUTION_REQUIRED','LEGAL_REVIEW_REQUIRED','NOT_PERMITTED')),
  next_due_at         timestamptz,
  last_attempt_at     timestamptz,
  last_fetch_at       timestamptz,   -- last successful HTTP fetch (any content)
  last_success_at     timestamptz,   -- last run that left published data current (SUCCEEDED / NO_CHANGE)
  last_change_at      timestamptz,   -- last time a new version was published
  current_version_id  bigint,
  breaker_state       text        NOT NULL DEFAULT 'CLOSED' CHECK (breaker_state IN ('CLOSED','OPEN','HALF_OPEN')),
  breaker_opened_at   timestamptz,
  breaker_retry_at    timestamptz,
  consecutive_failures int        NOT NULL DEFAULT 0,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_source_schedule CHECK (
    (schedule_kind = 'INTERVAL' AND cadence IS NOT NULL) OR (schedule_kind = 'CRON' AND cron_expr IS NOT NULL))
);
CREATE INDEX ix_source_due ON sanctions.source (next_due_at) WHERE status = 'ACTIVE';

CREATE TRIGGER tr_source_audit
  AFTER INSERT OR DELETE OR UPDATE OF status, config, config_version, schedule_kind, cadence, cron_expr, timezone,
    min_interval, warn_staleness, hard_max_staleness, priority, paused_until, is_core, licence_status, breaker_state
  ON sanctions.source FOR EACH ROW EXECUTE FUNCTION sanctions.audit_row('source_id');

CREATE TABLE sanctions.source_config_version (
  source_id           text        NOT NULL REFERENCES sanctions.source (source_id),
  version             int         NOT NULL,
  config              jsonb       NOT NULL,
  schedule            jsonb       NOT NULL,
  diff                jsonb,
  changed_by          text        NOT NULL,
  changed_at          timestamptz NOT NULL DEFAULT now(),
  reason              text,
  status              text        NOT NULL CHECK (status IN ('PENDING_APPROVAL','ACTIVE','SUPERSEDED','REJECTED')),
  approval_change_id  bigint,
  PRIMARY KEY (source_id, version)
);
CREATE UNIQUE INDEX ux_source_config_one_active ON sanctions.source_config_version (source_id) WHERE status = 'ACTIVE';
CREATE TRIGGER tr_source_config_version_audit AFTER UPDATE ON sanctions.source_config_version
  FOR EACH ROW EXECUTE FUNCTION sanctions.audit_row('source_id');

CREATE TABLE sanctions.system_setting (
  key         text PRIMARY KEY,
  value       jsonb       NOT NULL,
  updated_by  text        NOT NULL DEFAULT current_user,
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER tr_system_setting_audit AFTER INSERT OR UPDATE OR DELETE ON sanctions.system_setting
  FOR EACH ROW EXECUTE FUNCTION sanctions.audit_row('key');

CREATE TABLE sanctions.agent_cycle (
  cycle_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_name      text        NOT NULL DEFAULT 'supervisor',
  trigger         text        NOT NULL,
  gate_reasons    text[]      NOT NULL DEFAULT '{}',
  model           text,
  started_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz,
  status          text        NOT NULL DEFAULT 'RUNNING'
                    CHECK (status IN ('RUNNING','SUCCEEDED','FAILED','BUDGET_EXCEEDED','SKIPPED','TIMEOUT')),
  input_tokens    int         NOT NULL DEFAULT 0,
  cached_tokens   int         NOT NULL DEFAULT 0,
  output_tokens   int         NOT NULL DEFAULT 0,
  cost_usd        numeric(12, 6) NOT NULL DEFAULT 0,
  report          jsonb,
  error           text,
  fallback_used   boolean     NOT NULL DEFAULT false,
  trace_id        text
);
CREATE INDEX ix_agent_cycle_started ON sanctions.agent_cycle (started_at DESC);

CREATE TABLE sanctions.ingestion_run (
  run_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id           text        NOT NULL REFERENCES sanctions.source (source_id),
  run_kind            text        NOT NULL
                        CHECK (run_kind IN ('LIST_INGEST','RELEASE_HELD','NOTICE_SYNC','ENRICHMENT_BATCH','ANNEX_BUILD')),
  trigger             text        NOT NULL CHECK (trigger IN ('SCHEDULE','SIGNAL','AGENT','WATCHDOG','MANUAL','RESUME')),
  requested_by        text        NOT NULL,
  reason              text,
  options             jsonb       NOT NULL DEFAULT '{}'::jsonb,
  config_version      int,
  status              text        NOT NULL DEFAULT 'QUEUED'
                        CHECK (status IN ('QUEUED','RUNNING','SUCCEEDED','NO_CHANGE','DRY_RUN_OK','HELD',
                                          'QUARANTINED','FAILED','ABANDONED','CANCELLED')),
  current_step        text,
  attempt             int         NOT NULL DEFAULT 1,
  parent_run_id       uuid REFERENCES sanctions.ingestion_run (run_id),
  resumed_from_run_id uuid REFERENCES sanctions.ingestion_run (run_id),
  lease_owner         text,
  lease_expires_at    timestamptz,
  heartbeat_at        timestamptz,
  progress            jsonb       NOT NULL DEFAULT '{}'::jsonb,
  queued_at           timestamptz NOT NULL DEFAULT now(),
  not_before          timestamptz NOT NULL DEFAULT now(),
  started_at          timestamptz,
  finished_at         timestamptz,
  cancel_requested_at timestamptz,
  cancelled_by        text,
  error_class         text,
  error_detail        text,
  trace_id            text,
  agent_cycle_id      uuid REFERENCES sanctions.agent_cycle (cycle_id),
  raw_sha256          char(64),
  version_id          bigint,
  summary             jsonb       NOT NULL DEFAULT '{}'::jsonb
);
-- one active (queued or running) run per source
CREATE UNIQUE INDEX ux_run_one_active_per_source ON sanctions.ingestion_run (source_id)
  WHERE status IN ('QUEUED','RUNNING');
CREATE INDEX ix_run_source_queued ON sanctions.ingestion_run (source_id, queued_at DESC);
CREATE INDEX ix_run_status ON sanctions.ingestion_run (status, not_before) WHERE status IN ('QUEUED','RUNNING');
CREATE INDEX ix_run_cycle ON sanctions.ingestion_run (agent_cycle_id);

CREATE TABLE sanctions.run_step (
  run_id       uuid        NOT NULL REFERENCES sanctions.ingestion_run (run_id),
  step         text        NOT NULL CHECK (step IN ('FETCH','ARCHIVE','VALIDATE_FILE','PARSE','VALIDATE_DATA',
                                                    'DIFF_PUBLISH','SYNC','ENRICH','BUILD')),
  attempt      int         NOT NULL DEFAULT 1,
  status       text        NOT NULL CHECK (status IN ('RUNNING','DONE','FAILED','SKIPPED')),
  started_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  detail       jsonb       NOT NULL DEFAULT '{}'::jsonb,
  error        text,
  PRIMARY KEY (run_id, step, attempt)
);

CREATE TABLE sanctions.signal (
  signal_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  provider            text        NOT NULL,
  external_id         text        NOT NULL,
  observed_at         timestamptz NOT NULL DEFAULT now(),
  title               text,
  url                 text,
  payload             jsonb       NOT NULL DEFAULT '{}'::jsonb,
  target_source_id    text REFERENCES sanctions.source (source_id),
  consumed_by_run_id  uuid REFERENCES sanctions.ingestion_run (run_id),
  consumed_at         timestamptz,
  UNIQUE (provider, external_id)
);
CREATE INDEX ix_signal_unconsumed ON sanctions.signal (target_source_id) WHERE consumed_at IS NULL;

-- =====================================================================================================
-- B. Evidence (append-only)
-- =====================================================================================================
CREATE TABLE sanctions.raw_artifact (
  sha256         char(64) PRIMARY KEY,
  blob_uri       text        NOT NULL,
  size_bytes     bigint      NOT NULL,
  content_type   text,
  compression    text,
  first_seen_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER tr_raw_artifact_append_only BEFORE UPDATE OR DELETE ON sanctions.raw_artifact
  FOR EACH ROW EXECUTE FUNCTION sanctions.forbid_mutation();

ALTER TABLE sanctions.ingestion_run
  ADD CONSTRAINT fk_run_raw FOREIGN KEY (raw_sha256) REFERENCES sanctions.raw_artifact (sha256);

CREATE TABLE sanctions.fetch_evidence (
  fetch_id          bigint GENERATED ALWAYS AS IDENTITY,
  run_id            uuid        NOT NULL,
  source_id         text        NOT NULL,
  attempt           int         NOT NULL,
  requested_url     text        NOT NULL,
  final_url         text,
  redirect_chain    jsonb       NOT NULL DEFAULT '[]'::jsonb,
  http_status       int,
  response_headers  jsonb       NOT NULL DEFAULT '{}'::jsonb,
  tls_chain_pem     text,
  tls_leaf_sha256   text,
  fetched_at        timestamptz NOT NULL DEFAULT now(),
  duration_ms       int,
  not_modified      boolean     NOT NULL DEFAULT false,
  conditional       jsonb       NOT NULL DEFAULT '{}'::jsonb,
  sha256            char(64),
  size_bytes        bigint,
  error_class       text,
  error_detail      text,
  PRIMARY KEY (fetch_id, fetched_at)
) PARTITION BY RANGE (fetched_at);
CREATE TABLE sanctions.fetch_evidence_default PARTITION OF sanctions.fetch_evidence DEFAULT;
CREATE INDEX ix_fetch_evidence_run ON sanctions.fetch_evidence (run_id);
CREATE INDEX ix_fetch_evidence_source ON sanctions.fetch_evidence (source_id, fetched_at DESC);
CREATE TRIGGER tr_fetch_evidence_append_only BEFORE UPDATE OR DELETE ON sanctions.fetch_evidence
  FOR EACH ROW EXECUTE FUNCTION sanctions.forbid_mutation();

-- =====================================================================================================
-- C. In-progress data (staging) - scoped by run_id, never read by screening / analytics / Q&A
-- =====================================================================================================
CREATE TABLE sanctions.staging_record (
  run_id          uuid     NOT NULL REFERENCES sanctions.ingestion_run (run_id),
  source_key      text     NOT NULL,
  entity_type     text     NOT NULL,
  content_hash    char(64) NOT NULL,
  primary_name    text,
  doc             jsonb    NOT NULL,
  parse_warnings  jsonb    NOT NULL DEFAULT '[]'::jsonb,
  PRIMARY KEY (run_id, source_key)
);

CREATE TABLE sanctions.staging_path_stats (
  run_id       uuid NOT NULL REFERENCES sanctions.ingestion_run (run_id),
  xml_path     text NOT NULL,
  occurrences  int  NOT NULL,
  known        boolean NOT NULL,
  PRIMARY KEY (run_id, xml_path)
);

-- =====================================================================================================
-- D. Published list data: versioned canonical model (SCD2)
-- =====================================================================================================
CREATE TABLE sanctions.list_version (
  version_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id            text        NOT NULL REFERENCES sanctions.source (source_id),
  seq                  int         NOT NULL,
  run_id               uuid        NOT NULL REFERENCES sanctions.ingestion_run (run_id),
  raw_sha256           char(64)    NOT NULL REFERENCES sanctions.raw_artifact (sha256),
  publication_marker   text,
  published_at_source  timestamptz,
  record_count         int,
  counts_by_type       jsonb       NOT NULL DEFAULT '{}'::jsonb,
  validation_report    jsonb       NOT NULL DEFAULT '{}'::jsonb,
  change_summary       jsonb,
  status               text        NOT NULL
                         CHECK (status IN ('CANDIDATE','VALIDATED','HELD','QUARANTINED','PUBLISHED','SUPERSEDED','REJECTED')),
  previous_version_id  bigint REFERENCES sanctions.list_version (version_id),
  created_at           timestamptz NOT NULL DEFAULT now(),
  published_at         timestamptz,
  released_by          text,
  UNIQUE (source_id, seq)
);
CREATE UNIQUE INDEX ux_list_version_one_published ON sanctions.list_version (source_id) WHERE status = 'PUBLISHED';
CREATE INDEX ix_list_version_run ON sanctions.list_version (run_id);
CREATE TRIGGER tr_list_version_audit AFTER UPDATE OF status ON sanctions.list_version
  FOR EACH ROW EXECUTE FUNCTION sanctions.audit_row('version_id');

ALTER TABLE sanctions.source
  ADD CONSTRAINT fk_source_current_version FOREIGN KEY (current_version_id) REFERENCES sanctions.list_version (version_id);
ALTER TABLE sanctions.ingestion_run
  ADD CONSTRAINT fk_run_version FOREIGN KEY (version_id) REFERENCES sanctions.list_version (version_id);

CREATE TABLE sanctions.record_version (
  record_version_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id          text     NOT NULL REFERENCES sanctions.source (source_id),
  source_key         text     NOT NULL,
  entity_type        text     NOT NULL CHECK (entity_type IN ('PERSON','ORGANIZATION','VESSEL','AIRCRAFT','UNKNOWN')),
  valid_from_seq     int      NOT NULL,
  valid_to_seq       int,
  content_hash       char(64) NOT NULL,
  primary_name       text,
  doc                jsonb    NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source_id, source_key, valid_from_seq),
  CHECK (valid_to_seq IS NULL OR valid_to_seq > valid_from_seq)
);
CREATE UNIQUE INDEX ux_record_version_current ON sanctions.record_version (source_id, source_key)
  WHERE valid_to_seq IS NULL;
CREATE INDEX ix_record_version_range ON sanctions.record_version (source_id, valid_from_seq, valid_to_seq);

-- record_version rows are immutable except for closing valid_to_seq exactly once
CREATE OR REPLACE FUNCTION sanctions.record_version_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'record_version is append-only' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF OLD.valid_to_seq IS NOT NULL
     OR (to_jsonb(NEW) - 'valid_to_seq') IS DISTINCT FROM (to_jsonb(OLD) - 'valid_to_seq') THEN
    RAISE EXCEPTION 'record_version rows are immutable (only valid_to_seq may be closed once)'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER tr_record_version_guard BEFORE UPDATE OR DELETE ON sanctions.record_version
  FOR EACH ROW EXECUTE FUNCTION sanctions.record_version_guard();

-- Relational projection of record_version.doc (immutable children, versioned with their parent)
CREATE TABLE sanctions.rv_name (
  record_version_id  bigint   NOT NULL REFERENCES sanctions.record_version (record_version_id),
  ord                smallint NOT NULL,
  name_type          text     NOT NULL CHECK (name_type IN ('PRIMARY','AKA','FKA','NKA','OTHER')),
  full_name          text     NOT NULL,
  name_parts         jsonb    NOT NULL DEFAULT '{}'::jsonb,
  script             text,
  language           text,
  quality            text     NOT NULL DEFAULT 'UNKNOWN' CHECK (quality IN ('STRONG','WEAK','UNKNOWN')),
  raw_quality        text,
  normalized_name    text     NOT NULL,
  PRIMARY KEY (record_version_id, ord)
);
CREATE INDEX ix_rv_name_norm ON sanctions.rv_name (normalized_name);

CREATE TABLE sanctions.rv_identifier (
  record_version_id  bigint   NOT NULL REFERENCES sanctions.record_version (record_version_id),
  ord                smallint NOT NULL,
  id_type            text     NOT NULL CHECK (id_type IN ('PASSPORT','NATIONAL_ID','IMO','MMSI','CALL_SIGN','LEI',
                                   'REGISTRATION','TAX','SWIFT','UN_REF','OFAC_UID','EU_REF','AIRCRAFT_MSN',
                                   'AIRCRAFT_TAIL','EMAIL','WEBSITE','OTHER')),
  id_label           text,
  value_raw          text     NOT NULL,
  value_norm         text     NOT NULL,
  country_iso2       char(2),
  country_raw        text,
  issued_on          text,
  expires_on         text,
  checksum_valid     boolean,
  PRIMARY KEY (record_version_id, ord)
);
CREATE INDEX ix_rv_identifier_value ON sanctions.rv_identifier (id_type, value_norm);

CREATE TABLE sanctions.rv_address (
  record_version_id  bigint   NOT NULL REFERENCES sanctions.record_version (record_version_id),
  ord                smallint NOT NULL,
  street             text,
  city               text,
  region             text,
  postal_code        text,
  country_iso2       char(2),
  country_raw        text,
  full_raw           text,
  PRIMARY KEY (record_version_id, ord)
);

CREATE TABLE sanctions.rv_birth (
  record_version_id  bigint   NOT NULL REFERENCES sanctions.record_version (record_version_id),
  ord                smallint NOT NULL,
  kind               text     NOT NULL CHECK (kind IN ('DOB','POB')),
  date_value         date,
  year               int,
  year_from          int,
  year_to            int,
  precision          text     CHECK (precision IN ('DAY','MONTH','YEAR','RANGE','UNKNOWN')),
  place              text,
  country_iso2       char(2),
  raw                text,
  PRIMARY KEY (record_version_id, ord)
);

CREATE TABLE sanctions.rv_nationality (
  record_version_id  bigint   NOT NULL REFERENCES sanctions.record_version (record_version_id),
  ord                smallint NOT NULL,
  kind               text     NOT NULL CHECK (kind IN ('NATIONALITY','CITIZENSHIP','REGISTRATION_COUNTRY','FLAG','COUNTRY')),
  country_iso2       char(2),
  country_raw        text,
  PRIMARY KEY (record_version_id, ord)
);

CREATE TABLE sanctions.rv_listing (
  record_version_id  bigint   NOT NULL REFERENCES sanctions.record_version (record_version_id),
  ord                smallint NOT NULL,
  authority          text     NOT NULL,
  list_name          text,
  program_code       text,
  listed_on          date,
  legal_basis        text,
  reference_no       text,
  measures           text[]   NOT NULL DEFAULT '{}',
  reason             text,
  remarks            text,
  PRIMARY KEY (record_version_id, ord)
);

CREATE TABLE sanctions.rv_relationship (
  record_version_id  bigint   NOT NULL REFERENCES sanctions.record_version (record_version_id),
  ord                smallint NOT NULL,
  rel_type           text     NOT NULL,
  target_source_key  text,
  target_name_raw    text,
  raw                text,
  PRIMARY KEY (record_version_id, ord)
);

CREATE TABLE sanctions.rv_vessel (
  record_version_id   bigint PRIMARY KEY REFERENCES sanctions.record_version (record_version_id),
  vessel_type         text,
  flag_iso2           char(2),
  flag_raw            text,
  tonnage             text,
  build_year          int,
  owner_operator_raw  text
);

CREATE TABLE sanctions.rv_aircraft (
  record_version_id  bigint PRIMARY KEY REFERENCES sanctions.record_version (record_version_id),
  model              text,
  manufacturer       text,
  operator_raw       text,
  build_year         int
);

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['rv_name','rv_identifier','rv_address','rv_birth','rv_nationality','rv_listing',
                           'rv_relationship','rv_vessel','rv_aircraft']
  LOOP
    EXECUTE format('CREATE TRIGGER tr_%s_append_only BEFORE UPDATE OR DELETE ON sanctions.%I
                    FOR EACH ROW EXECUTE FUNCTION sanctions.forbid_mutation()', t, t);
  END LOOP;
END $$;

-- Changes, removals, cross-list links, screening snapshots
CREATE TABLE sanctions.change_event (
  event_id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id              text        NOT NULL REFERENCES sanctions.source (source_id),
  version_id             bigint      NOT NULL REFERENCES sanctions.list_version (version_id),
  source_key             text        NOT NULL,
  change_type            text        NOT NULL CHECK (change_type IN ('ADD','CHANGE','REMOVE','RELIST')),
  entity_type            text,
  old_record_version_id  bigint REFERENCES sanctions.record_version (record_version_id),
  new_record_version_id  bigint REFERENCES sanctions.record_version (record_version_id),
  field_diff             jsonb,
  created_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_change_event_version ON sanctions.change_event (version_id, change_type);
CREATE INDEX ix_change_event_key ON sanctions.change_event (source_id, source_key, created_at DESC);
CREATE TRIGGER tr_change_event_append_only BEFORE UPDATE OR DELETE ON sanctions.change_event
  FOR EACH ROW EXECUTE FUNCTION sanctions.forbid_mutation();

-- =====================================================================================================
-- E. Level 2: notices and enrichment
-- =====================================================================================================
CREATE TABLE sanctions.legal_notice (
  notice_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  provider            text        NOT NULL,
  external_id         text        NOT NULL,
  title               text,
  published_on        date,
  url                 text,
  raw_sha256          char(64) REFERENCES sanctions.raw_artifact (sha256),
  text_excerpt        text,
  full_text_blob_uri  text,
  action_types        text[]      NOT NULL DEFAULT '{}',
  related_sources     text[]      NOT NULL DEFAULT '{}',
  extraction_status   text        NOT NULL DEFAULT 'NEW'
                        CHECK (extraction_status IN ('NEW','MATCHED','NEEDS_AGENT','EXTRACTED','NO_MATCH','FAILED')),
  extracted           jsonb,
  fetched_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider, external_id)
);
CREATE INDEX ix_legal_notice_published ON sanctions.legal_notice (published_on DESC);

CREATE TABLE sanctions.removal_candidate (
  candidate_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id               text        NOT NULL REFERENCES sanctions.source (source_id),
  source_key              text        NOT NULL,
  last_record_version_id  bigint      NOT NULL REFERENCES sanctions.record_version (record_version_id),
  detected_in_version_id  bigint      NOT NULL REFERENCES sanctions.list_version (version_id),
  status                  text        NOT NULL DEFAULT 'PENDING'
                            CHECK (status IN ('PENDING','EVIDENCE_FOUND','CONFIRMED','STILL_LISTED_ELSEWHERE',
                                              'REJECTED_PARSER','REJECTED_ID_CHANGE','RELISTED')),
  evidence_notice_id      bigint REFERENCES sanctions.legal_notice (notice_id),
  crosslist_hits          jsonb       NOT NULL DEFAULT '[]'::jsonb,
  created_at              timestamptz NOT NULL DEFAULT now(),
  decided_by              text,
  decided_at              timestamptz,
  rationale               text
);
CREATE UNIQUE INDEX ux_removal_candidate_open ON sanctions.removal_candidate (source_id, source_key)
  WHERE status IN ('PENDING','EVIDENCE_FOUND','REJECTED_PARSER');
CREATE TRIGGER tr_removal_candidate_audit AFTER UPDATE ON sanctions.removal_candidate
  FOR EACH ROW EXECUTE FUNCTION sanctions.audit_row('candidate_id');

CREATE TABLE sanctions.crosslist_key (
  key_type           text   NOT NULL CHECK (key_type IN ('UN_REF','IMO','LEI','REG_COUNTRY','PASSPORT_COUNTRY','OFAC_UID')),
  key_value          text   NOT NULL,
  source_id          text   NOT NULL REFERENCES sanctions.source (source_id),
  source_key         text   NOT NULL,
  record_version_id  bigint NOT NULL REFERENCES sanctions.record_version (record_version_id),
  PRIMARY KEY (key_type, key_value, source_id, source_key)
);
CREATE INDEX ix_crosslist_source ON sanctions.crosslist_key (source_id, source_key);

CREATE TABLE sanctions.screening_snapshot (
  snapshot_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  created_at          timestamptz NOT NULL DEFAULT now(),
  trigger_version_id  bigint REFERENCES sanctions.list_version (version_id),
  held_removal_ids    bigint[]    NOT NULL DEFAULT '{}',
  note                text
);
CREATE TABLE sanctions.snapshot_version (
  snapshot_id  bigint NOT NULL REFERENCES sanctions.screening_snapshot (snapshot_id),
  source_id    text   NOT NULL REFERENCES sanctions.source (source_id),
  version_id   bigint NOT NULL REFERENCES sanctions.list_version (version_id),
  seq          int    NOT NULL,
  PRIMARY KEY (snapshot_id, source_id)
);
CREATE TRIGGER tr_screening_snapshot_append_only BEFORE UPDATE OR DELETE ON sanctions.screening_snapshot
  FOR EACH ROW EXECUTE FUNCTION sanctions.forbid_mutation();
CREATE TRIGGER tr_snapshot_version_append_only BEFORE UPDATE OR DELETE ON sanctions.snapshot_version
  FOR EACH ROW EXECUTE FUNCTION sanctions.forbid_mutation();

-- The exact records a screening decision used (FR-18): records valid at each pinned version, plus the
-- last version of every removal still held (removals keep blocking until confirmed, BRD 17.4).
CREATE OR REPLACE FUNCTION sanctions.records_as_of(p_snapshot_id bigint)
RETURNS TABLE (record_version_id bigint, source_id text, source_key text, entity_type text,
               primary_name text, removal_pending boolean)
LANGUAGE sql STABLE AS $$
  SELECT rv.record_version_id, rv.source_id, rv.source_key, rv.entity_type, rv.primary_name, false
  FROM sanctions.snapshot_version sv
  JOIN sanctions.record_version rv
    ON rv.source_id = sv.source_id
   AND rv.valid_from_seq <= sv.seq
   AND (rv.valid_to_seq IS NULL OR rv.valid_to_seq > sv.seq)
  WHERE sv.snapshot_id = p_snapshot_id
  UNION ALL
  SELECT rv.record_version_id, rv.source_id, rv.source_key, rv.entity_type, rv.primary_name, true
  FROM sanctions.screening_snapshot s
  JOIN sanctions.removal_candidate rc ON rc.candidate_id = ANY (s.held_removal_ids)
  JOIN sanctions.record_version rv ON rv.record_version_id = rc.last_record_version_id
  WHERE s.snapshot_id = p_snapshot_id
$$;

CREATE TABLE sanctions.enrichment_record (
  enrichment_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  subject_source_id   text        NOT NULL REFERENCES sanctions.source (source_id),
  subject_source_key  text        NOT NULL,
  subject_entity_type text,
  provider            text        NOT NULL,
  provider_key        text,
  match_method        text        NOT NULL,
  match_score         numeric(5, 4),
  status              text        NOT NULL CHECK (status IN ('AUTO_ACCEPTED','NEEDS_REVIEW','APPROVED','REJECTED','NO_MATCH')),
  data                jsonb       NOT NULL DEFAULT '{}'::jsonb,
  source_url          text,
  licence             text,
  raw_sha256          char(64) REFERENCES sanctions.raw_artifact (sha256),
  retrieved_at        timestamptz NOT NULL DEFAULT now(),
  superseded_by       bigint REFERENCES sanctions.enrichment_record (enrichment_id),
  proposed_change_id  bigint
);
CREATE INDEX ix_enrichment_subject ON sanctions.enrichment_record (subject_source_id, subject_source_key, provider)
  WHERE superseded_by IS NULL;
CREATE INDEX ix_enrichment_provider_status ON sanctions.enrichment_record (provider, status) WHERE superseded_by IS NULL;

CREATE TABLE sanctions.enrichment_request (
  request_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  requested_by        text        NOT NULL,
  subject_source_id   text        NOT NULL,
  subject_source_key  text        NOT NULL,
  provider            text        NOT NULL,
  reason              text        NOT NULL,
  status              text        NOT NULL DEFAULT 'REQUESTED' CHECK (status IN ('REQUESTED','DONE','FAILED','DENIED')),
  result_enrichment_id bigint REFERENCES sanctions.enrichment_record (enrichment_id),
  created_at          timestamptz NOT NULL DEFAULT now(),
  finished_at         timestamptz
);

-- =====================================================================================================
-- F. Human review, agent, quality, Q&A
-- =====================================================================================================
CREATE TABLE sanctions.proposed_change (
  change_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind               text        NOT NULL CHECK (kind IN ('ANNEX_ENTRY','REMOVAL_CONFIRMATION','NOTICE_LINK',
                                   'ENRICHMENT_MATCH','LARGE_CHANGE_RELEASE','CONFIG_CHANGE','SOURCE_ACTIVATION')),
  source_id          text REFERENCES sanctions.source (source_id),
  subject_ref        text,
  title              text        NOT NULL,
  payload            jsonb       NOT NULL DEFAULT '{}'::jsonb,
  evidence_urls      text[]      NOT NULL DEFAULT '{}',
  verbatim_quotes    jsonb       NOT NULL DEFAULT '[]'::jsonb,
  verification       jsonb       NOT NULL DEFAULT '{}'::jsonb,
  rationale          text,
  proposed_by        text        NOT NULL,
  agent_cycle_id     uuid REFERENCES sanctions.agent_cycle (cycle_id),
  status             text        NOT NULL DEFAULT 'PENDING'
                       CHECK (status IN ('PENDING','APPROVED','REJECTED','APPLIED','EXPIRED')),
  created_at         timestamptz NOT NULL DEFAULT now(),
  reviewed_by        text,
  reviewed_at        timestamptz,
  review_comment     text,
  applied_in_run_id  uuid REFERENCES sanctions.ingestion_run (run_id),
  dedupe_key         text
);
CREATE INDEX ix_proposed_change_status ON sanctions.proposed_change (status, kind, created_at DESC);
CREATE UNIQUE INDEX ux_proposed_change_open_dedupe ON sanctions.proposed_change (dedupe_key)
  WHERE status = 'PENDING' AND dedupe_key IS NOT NULL;
CREATE TRIGGER tr_proposed_change_audit AFTER INSERT OR UPDATE ON sanctions.proposed_change
  FOR EACH ROW EXECUTE FUNCTION sanctions.audit_row('change_id');

ALTER TABLE sanctions.source_config_version
  ADD CONSTRAINT fk_config_approval FOREIGN KEY (approval_change_id) REFERENCES sanctions.proposed_change (change_id);
ALTER TABLE sanctions.enrichment_record
  ADD CONSTRAINT fk_enrichment_change FOREIGN KEY (proposed_change_id) REFERENCES sanctions.proposed_change (change_id);

CREATE TABLE sanctions.notice_link (
  link_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  notice_id           bigint      NOT NULL REFERENCES sanctions.legal_notice (notice_id),
  source_id           text        NOT NULL REFERENCES sanctions.source (source_id),
  source_key          text        NOT NULL,
  link_type           text        NOT NULL CHECK (link_type IN ('LISTING','DELISTING','AMENDMENT','REFERENCE')),
  method              text        NOT NULL CHECK (method IN ('ID_MATCH','NAME_MATCH','AGENT','IN_FILE')),
  confidence          numeric(4, 3) NOT NULL,
  verbatim_quote      text,
  status              text        NOT NULL CHECK (status IN ('AUTO','PENDING','APPROVED','REJECTED')),
  proposed_change_id  bigint REFERENCES sanctions.proposed_change (change_id),
  created_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (notice_id, source_id, source_key, link_type)
);
CREATE INDEX ix_notice_link_subject ON sanctions.notice_link (source_id, source_key);

CREATE TABLE sanctions.curated_entry (
  entry_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id           text        NOT NULL REFERENCES sanctions.source (source_id),
  entry_key           text        NOT NULL,
  doc                 jsonb       NOT NULL,
  approved_change_id  bigint REFERENCES sanctions.proposed_change (change_id),
  active              boolean     NOT NULL DEFAULT true,
  created_at          timestamptz NOT NULL DEFAULT now(),
  deactivated_at      timestamptz
);
CREATE UNIQUE INDEX ux_curated_entry_active ON sanctions.curated_entry (source_id, entry_key) WHERE active;

CREATE TABLE sanctions.incident (
  incident_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id     text REFERENCES sanctions.source (source_id),
  error_class   text        NOT NULL,
  severity      text        NOT NULL CHECK (severity IN ('INFO','WARN','PAGE')),
  status        text        NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','ACK','RESOLVED')),
  dedupe_key    text        NOT NULL,
  title         text        NOT NULL,
  opened_at     timestamptz NOT NULL DEFAULT now(),
  last_seen_at  timestamptz NOT NULL DEFAULT now(),
  occurrences   int         NOT NULL DEFAULT 1,
  resolved_at   timestamptz,
  summary       text,
  diagnosis     text,
  actions       jsonb       NOT NULL DEFAULT '[]'::jsonb,
  alerts_sent   int         NOT NULL DEFAULT 0,
  last_alert_at timestamptz
);
CREATE UNIQUE INDEX ux_incident_open_dedupe ON sanctions.incident (dedupe_key) WHERE status <> 'RESOLVED';
CREATE TRIGGER tr_incident_audit AFTER INSERT OR UPDATE OF status, severity, diagnosis ON sanctions.incident
  FOR EACH ROW EXECUTE FUNCTION sanctions.audit_row('incident_id');

CREATE TABLE sanctions.agent_action (
  action_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  cycle_id        uuid        NOT NULL REFERENCES sanctions.agent_cycle (cycle_id),
  seq             int         NOT NULL,
  tool            text        NOT NULL,
  args            jsonb       NOT NULL DEFAULT '{}'::jsonb,
  guard_verdict   text        NOT NULL CHECK (guard_verdict IN ('ALLOWED','DENIED')),
  result_summary  jsonb,
  error           text,
  duration_ms     int,
  span_id         text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (cycle_id, seq)
);
CREATE TRIGGER tr_agent_action_append_only BEFORE UPDATE OR DELETE ON sanctions.agent_action
  FOR EACH ROW EXECUTE FUNCTION sanctions.forbid_mutation();

CREATE TABLE sanctions.agent_span (
  span_id         text        NOT NULL,
  trace_id        text        NOT NULL,
  parent_span_id  text,
  kind            text        NOT NULL,
  name            text,
  started_at      timestamptz NOT NULL,
  ended_at        timestamptz,
  data            jsonb       NOT NULL DEFAULT '{}'::jsonb,
  error           jsonb,
  PRIMARY KEY (span_id, started_at)
) PARTITION BY RANGE (started_at);
CREATE TABLE sanctions.agent_span_default PARTITION OF sanctions.agent_span DEFAULT;
CREATE INDEX ix_agent_span_trace ON sanctions.agent_span (trace_id);

CREATE TABLE sanctions.dq_metric (
  version_id   bigint  NOT NULL REFERENCES sanctions.list_version (version_id),
  source_id    text    NOT NULL,
  entity_type  text    NOT NULL,
  metric       text    NOT NULL,
  field        text    NOT NULL DEFAULT '',
  value        numeric NOT NULL,
  floor        numeric,
  prev_value   numeric,
  status       text    NOT NULL DEFAULT 'OK' CHECK (status IN ('OK','WARN','FAIL')),
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (version_id, entity_type, metric, field)
);
CREATE INDEX ix_dq_metric_source ON sanctions.dq_metric (source_id, metric, field, created_at DESC);

CREATE TABLE sanctions.dq_issue (
  issue_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id    text        NOT NULL REFERENCES sanctions.source (source_id),
  version_id   bigint REFERENCES sanctions.list_version (version_id),
  run_id       uuid REFERENCES sanctions.ingestion_run (run_id),
  category     text        NOT NULL CHECK (category IN ('SCHEMA_DRIFT','FILL_RATE_BELOW_FLOOR','COUNT_ANOMALY',
                               'UNMAPPED_COUNTRY','INVALID_CHECKSUM','DATE_UNPARSEABLE','DUPLICATE_KEY',
                               'PUBLICATION_MARKER_REGRESSION','ENRICHMENT_REVIEW_BACKLOG','SCHEMA_INVALID',
                               'PARSE_WARNING','MISSING_REQUIRED_FIELD')),
  severity     text        NOT NULL CHECK (severity IN ('INFO','WARN','FAIL')),
  entity_type  text,
  field        text,
  count        int         NOT NULL DEFAULT 0,
  detail       jsonb       NOT NULL DEFAULT '{}'::jsonb,   -- aggregate only, never record values
  first_seen   timestamptz NOT NULL DEFAULT now(),
  last_seen    timestamptz NOT NULL DEFAULT now(),
  status       text        NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','RESOLVED','ACCEPTED'))
);
CREATE INDEX ix_dq_issue_source ON sanctions.dq_issue (source_id, status, category);

CREATE TABLE sanctions.field_catalog (
  source_id        text    NOT NULL,
  canonical_field  text    NOT NULL,
  entity_types     text[]  NOT NULL DEFAULT '{}',
  source_path      text,
  description      text,
  fill_floor       numeric,
  notes            text,
  updated_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_id, canonical_field)
);

CREATE TABLE sanctions.chat_session (
  session_id  text PRIMARY KEY,
  user_id     text        NOT NULL,
  title       text,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE sanctions.chat_message (
  message_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  session_id  text        NOT NULL REFERENCES sanctions.chat_session (session_id),
  role        text        NOT NULL,
  content     text,
  item        jsonb       NOT NULL,        -- raw SDK conversation item (for session replay)
  tool_calls  jsonb,
  tokens      int,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_chat_message_session ON sanctions.chat_message (session_id, message_id);

-- Monthly partitions for the high-volume evidence/trace tables (called by the scheduler daily, creating
-- months ahead so rows never land in the DEFAULT partition; if they ever do, that month is skipped and
-- rows simply stay in DEFAULT - nothing is lost).
CREATE OR REPLACE FUNCTION sanctions.ensure_month_partitions(p_months_ahead int DEFAULT 2) RETURNS int
LANGUAGE plpgsql AS $$
DECLARE
  base date := date_trunc('month', now())::date;
  i int;
  t text;
  col text;
  p_start date;
  p_end date;
  pname text;
  created int := 0;
  in_default boolean;
BEGIN
  FOREACH t IN ARRAY ARRAY['fetch_evidence','agent_span'] LOOP
    col := CASE WHEN t = 'fetch_evidence' THEN 'fetched_at' ELSE 'started_at' END;
    FOR i IN 0..p_months_ahead LOOP
      p_start := (base + make_interval(months => i))::date;
      p_end := (p_start + interval '1 month')::date;
      pname := format('%s_%s', t, to_char(p_start, 'YYYYMM'));
      CONTINUE WHEN to_regclass('sanctions.' || pname) IS NOT NULL;
      EXECUTE format('SELECT EXISTS (SELECT 1 FROM sanctions.%I WHERE %I >= %L AND %I < %L)',
                     t || '_default', col, p_start, col, p_end) INTO in_default;
      CONTINUE WHEN in_default;
      EXECUTE format('CREATE TABLE sanctions.%I PARTITION OF sanctions.%I FOR VALUES FROM (%L) TO (%L)',
                     pname, t, p_start, p_end);
      created := created + 1;
    END LOOP;
  END LOOP;
  RETURN created;
END $$;
SELECT sanctions.ensure_month_partitions(2);
