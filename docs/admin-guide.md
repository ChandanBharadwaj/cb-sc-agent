# Admin guide

## Access and roles

| Role | Can |
|---|---|
| viewer | See everything in the console; ask the Q&A assistant |
| operator | + run now / dry run / re-parse, cancel runs, pause and resume sources, acknowledge and resolve incidents |
| reviewer | + approve or reject proposals: removal confirmations, held releases, annex entries, notice links, enrichment matches |
| admin | + edit configuration and schedules, disable/enable, add sources, request activation, roll back, global settings; approve CONFIG_CHANGE / SOURCE_ACTIVATION made by **another** admin |

**Authentication** (`SANCTIONS_AUTH_MODE`):
* `proxy` (production): put the API behind an SSO gateway such as oauth2-proxy that sets
  `X-Forwarded-User` and `X-Forwarded-Roles`. The API must not be reachable except through the gateway.
* `basic` (development): users are set in `SANCTIONS_UI_USERS=user:password:role,...`.
* `none`: local development only, and refused when `SANCTIONS_ENVIRONMENT=prod`.

Every change records the user in `audit_log`, `source_config_version.changed_by`,
`ingestion_run.requested_by` and `proposed_change.reviewed_by`.

## Managing sources (Sources & schedules)

**Status**
* **Pause** (operator) needs a reason and can be time-boxed. The source resumes automatically at
  `paused_until`.
* **Disable** (admin) needs a reason. A **core list** (OFAC, UN, UK, EU FSF, CSL, EU annexes) that is
  paused or disabled shows a red banner on every page and triggers repeating alerts.
* The scheduler, autopilot, agent and watchdog all respect paused and disabled status.

**Schedule**
* An interval, or cron with a timezone. The editor previews the next five runs.
* **Politeness floor.** Each adapter type has a hard minimum interval in code (for example 30 min for
  OFAC/UK/EU, 60 min for UN). Neither the cadence nor the minimum interval can go below it.
* **Staleness thresholds.** "Warn if stale" and "page if stale" drive the health badge, incidents and the
  watchdog.

**Configuration** (JSON, validated against the adapter's schema)
* Every save creates a new version, and a stale edit is refused (reload and re-apply).
* **URL, host and header changes are security-sensitive.** They become a CONFIG_CHANGE proposal that a
  **second admin** approves in the Review queue.
* Hosts must already be on the global allow-list `config/allowed_hosts.yaml`. That list is changed only
  through code review and deploy, never from the UI.

**Validation thresholds**
* Minimum records.
* Removal hold: triggers if removals exceed the count **or** the percentage.
* Addition hold: triggers only if additions exceed the count **and** the percentage.
* Drift policy.
* Per-field fill-rate floors (`ENTITY_TYPE.field`). "Suggest from last 5 versions" proposes floors of the
  lowest observed rate minus 5 points. Nothing applies until you save.

**History** lists every version with its diff. **Roll back** creates a new version; it never edits in place.

## Ad-hoc runs

Run now takes a mandatory reason, and each mode has its own rules:
* **Normal** respects the politeness interval. An admin may override it, but never below 5 minutes, and
  the override is recorded on the run.
* **Force re-fetch** ignores conditional GET and the same-hash shortcut.
* **Dry run** validates without publishing and ends as DRY_RUN_OK.
* **Re-parse** reprocesses an archived file of this source with no download.

A source can have only one active run at a time. Operators can cancel a run cooperatively; this is safe at
any point before the atomic publish.

## Adding a source

New sources reuse an **existing adapter type**; a new file format needs a parser in code.
1. Choose the type, then fill in the details and JSON configuration.
2. The source is created as **DRAFT**.
3. Run a **dry run** and check the result: records parsed, fill rates, issues, drift.
4. **Request activation.** A second admin approves it in the Review queue.

## Global settings

* **Maintenance mode:** stops all new pulls; runs in progress finish.
* **Supervisor agent:**
  * **On/off.** Off means the deterministic autopilot only; ingestion is unaffected.
  * **Model:** default `gpt-5-mini`.
  * **Reasoning effort.**
  * **Daily budget (USD).** When it is reached, cycles fall back to autopilot.
  * **Health-review interval.**
* **Alert channels:** log, webhook (Slack or Teams, set with `SANCTIONS_ALERT_WEBHOOK_URL`) and email
  (SMTP settings). Re-alert intervals apply to open page incidents and to disabled core lists.

## Deployment checklist

1. **Postgres 16.** Run `sanctions-agent db upgrade`.
   * This creates the schemas, triggers, the analytics views and the NOLOGIN role `sanctions_analyst`.
   * In production a DBA gives that role a password (`ALTER ROLE sanctions_analyst LOGIN PASSWORD ...`),
     which then goes in `SANCTIONS_ANALYST_DATABASE_URL`.
   * `SANCTIONS_ANALYST_PASSWORD` exists only as a dev/test convenience.
2. **Seed the sources:** `sanctions-agent sources import`. It never overwrites UI edits unless you pass
   `--overwrite`, which creates audited versions.
3. **Egress.** The network must allow the hosts in `config/allowed_hosts.yaml`, plus `api.openai.com` for
   the agents.
4. **Go-live checks from the production network:**
   * `sanctions-agent verify-sources --out verify.json` checks unknown element paths, fill rates, suggested
     floors and conditional-GET support (BRD Q6/Q7). Review the result.
   * `--write-known-paths` updates the drift baselines. Review the diff in a pull request.
   * `sanctions-agent pin-schemas --source <id> --url <publisher XSD>`, then set `validation.schema_file`.
5. **Secrets** (from your secret store, never committed): `OPENAI_API_KEY`,
   `SANCTIONS_COMPANIES_HOUSE_API_KEY`, SMTP and webhook settings, and the database URLs.
6. **Blob store:** `SANCTIONS_BLOB_BACKEND=s3` with `SANCTIONS_S3_BUCKET` (Object Lock enabled), `SANCTIONS_S3_PREFIX`
   and `SANCTIONS_S3_OBJECT_LOCK_DAYS` in production (install the `s3` extra), or a dedicated
   volume with FS.
7. **Monitoring:** scrape `/metrics` from the API. Load `ops/prometheus-alerts.yml`. Use `/ready` as the
   readiness probe.
8. **Run** one `serve` container (or more) and at least one `supervise` container. Supervisors can be
   scaled out safely: runs are claimed with leases and SKIP LOCKED.

## Retention and housekeeping

* **Partitions.** `fetch_evidence` and `agent_span` are partitioned by month. The supervisor creates
  future partitions daily. Drop old partitions according to your retention policy.
* **Staging.** Staging for HELD / QUARANTINED runs is purged after `SANCTIONS_STAGING_RETENTION_DAYS`
  (default 7).
* **Raw files** are write-once audit evidence. Keep them for the retention period your auditors require.

## YAML import / export (GitOps)

The database is the live source of truth. `sanctions-agent sources export --out config/sources.yaml` writes
the current configuration for backup or review, and `sources import` restores missing sources.
