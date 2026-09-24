# Sanctions ingestion agent (Level 1 + Level 2)

A self-healing, traceable pipeline that pulls sanctions lists **directly from the official publishers**
(BRD Level 1) and adds **free enrichment and legal evidence** (Level 2). It replaces the LexisNexis list
feed. A **gpt-5-mini supervisor agent** orchestrates the pipeline: it decides what to pull, diagnoses
failures, confirms removals with evidence and extracts facts from official notices. Deterministic code does
all parsing and publishing, and a person approves anything the agent extracts. Data is stored in
**Postgres**.

| Level 1 (official lists) | Level 2 (notices and evidence) | Level 2 (free enrichment) |
|---|---|---|
| OFAC SDN + Consolidated (Advanced XML) · UN SC Consolidated · UK FCDO · EU FSF · US CSL (BIS/State) · EU Reg. 833/2014 Annex XLII (vessels) and Annex IV (entities) | Federal Register · OFAC Recent Actions · UN list updates · gov.uk notices · EUR-Lex Official Journal · EU FSF RSS signal | GLEIF · UK Companies House · FAA aircraft registry · Wikidata · ICIJ (disabled, licence) · internal AIS (stub) |

## What you get

* **Self-healing ingestion.** Retries with backoff, per-source circuit breakers, and checkpoints with
  leases. A crashed run resumes from the archived file without downloading again.
  * A bad file is quarantined or held, and the last good version stays live.
  * A watchdog forces a pull of anything overdue.
  * If the agent is down or over budget, the deterministic autopilot takes over.
* **Traceability (BRD §9.4, FR-18).**
  * Write-once raw files with SHA-256.
  * Evidence for every fetch attempt: URL chain, headers, TLS chain.
  * SCD2 versioned records with field-level diffs.
  * `records_as_of(snapshot)` returns exactly what any screening decision used.
  * Append-only evidence tables and a full `audit_log`.
  * Every agent tool call, guard decision, token and dollar is recorded.
* **Operations console (`/ui`).** Live run progress (SSE), source health against SLOs, data quality
  (fill rates against floors, trends, issues), enrichment and legal-evidence coverage, agent activity and
  cost, and the review queue.
* **Ask the agent.** Natural-language questions about ingestion status, fields, counts and data-quality
  issues. Answers are **aggregate only**: the assistant is enforced by a guardrail, a SQL guard and a
  read-only database role that cannot see record-level tables.
* **Management UI (`/ui/manage`).**
  * Enable, pause or disable sources, with reasons and core-list rules.
  * Interval or cron schedules with a live preview and politeness floors.
  * Versioned configuration with rollback, and **maker-checker** for URL and host changes.
  * Ad-hoc runs: normal, force re-fetch, dry run or re-parse.
  * An add-source wizard (DRAFT, then dry run, then approval) and global settings.

| Overview | Data quality |
|---|---|
| ![Overview](docs/img/overview.png) | ![Data quality](docs/img/quality.png) |
| **Run detail (checkpoints, evidence)** | **Ask the agent (aggregate-only)** |
| ![Run detail](docs/img/run_detail.png) | ![Ask](docs/img/ask.png) |

## Quick start

### Docker Compose

```bash
cp .env.example .env         # set OPENAI_API_KEY, SANCTIONS_UI_USERS, user agent contact, alert webhook
docker compose up --build    # postgres, migrate (+ seed sources), supervisor, api
open http://localhost:8080/ui
```

### Local (Python 3.13 + uv, Postgres 16)

```bash
uv sync
export SANCTIONS_DATABASE_URL=postgresql://sanctions:sanctions@localhost:5432/sanctions
export SANCTIONS_ANALYST_PASSWORD=sanctions_analyst   # dev only; a DBA sets it in production
uv run sanctions-agent db upgrade
uv run sanctions-agent sources import
uv run python scripts/dev_seed.py        # optional demo data from the test fixtures (never in prod)
uv run sanctions-agent serve &           # API + console on :8080
uv run sanctions-agent supervise         # scheduler, workers, watchdog, agent (autopilot if no OPENAI_API_KEY)
```

## CLI

| Command | Purpose |
|---|---|
| `db upgrade` | Apply migrations (schema, analytics views, analyst role) |
| `sources import [--overwrite]` / `sources export` / `sources list` | Seed YAML in and out of the database (the DB is the live config) |
| `serve` / `supervise [--no-agent]` / `tick` | Run the API, the supervisor loop, or one supervisor tick |
| `run -s <id> [--mode dry_run\|force_refetch]` | Run one source now, in this process |
| `reparse -s <id> --sha256 <hash>` | Re-process an archived file after a parser fix (no download) |
| `load-manual -s <id> --file f --reason "..."` | NFR-08: load a file obtained out-of-band through the normal pipeline |
| `status` / `ask "question"` | Health summary / aggregate Q&A |
| `proposals list\|show\|approve\|reject` | Review queue (maker-checker) |
| `verify-sources [--write-known-paths]` | Live conformance probe: drift, fill rates, suggested floors, conditional GET |
| `pin-schemas -s <id> --url <xsd>` | Pin a publisher schema for file validation |
| `annex import-csv` / `annex extract-notice` | Propose EU annex entries from an analyst CSV or an OJ act |

## Before go-live (needs the production network)

This build was developed and tested against schema-faithful fixtures and mocked publishers, because the
development sandbox cannot reach government hosts or OpenAI. From a network that can:

1. **Allow egress** to the hosts in `config/allowed_hosts.yaml` and to `api.openai.com`.
2. **Probe the publishers.** `sanctions-agent verify-sources --out verify.json` reports unknown element
   paths, fill rates, suggested floors and conditional-GET support; this also answers BRD Q6/Q7.
   Review the result, then commit updated `schemas/known_paths` with `--write-known-paths`.
3. **Pin the schemas.** `sanctions-agent pin-schemas` for OFAC / UK / EU, then set `validation.schema_file`.
4. **Run the live tests.** `pytest -m live` (publishers) and `pytest -m llm` (with `OPENAI_API_KEY`).
5. **Legal:** see the open items in [docs/source-licence-register.md](docs/source-licence-register.md): UN
   terms (Q5), ICIJ share-alike, attribution.

## Documentation

* [Architecture](docs/architecture.md): components, pipeline, **in-progress data and transaction
  boundaries**, data model, self-healing layers, agents, security.
* [Runbook](docs/runbook.md): what each alert or incident means and what to do.
* [Admin guide](docs/admin-guide.md): roles, managing sources and schedules, settings, deployment
  checklist.
* [Source licence register](docs/source-licence-register.md): NFR-06/07.
* [Prometheus alert rules](ops/prometheus-alerts.yml).

## Development

```bash
uv run ruff check src tests scripts && uv run ruff format --check src tests scripts
uv run mypy
uv run pytest            # unit + integration (Postgres) + agent (scripted model) + API + UI (Chromium)
```

The integration tests need a Postgres at `SANCTIONS_TEST_DATABASE_URL` (default
`postgresql://sanctions:sanctions@localhost:5432/sanctions_test`). The UI tests need Chromium
(`playwright install chromium`, or set `PLAYWRIGHT_CHROMIUM_EXECUTABLE`). Tests marked `live` and `llm`
are skipped by default.

```
src/sanctions_agent/
  http/        guarded HTTP client (allow-list per hop, SSRF guard, evidence), retries, rate limits
  storage/     write-once blob store (FS / S3 Object Lock)
  sources/     adapter types, config models, registry, config service (versions, maker-checker), L1 parsers
  canonical/   canonical record model + normalisers (countries, partial dates, names, IMO/LEI checksums)
  pipeline/    runner (checkpoints, leases, resume), validation, diff/SCD2 publish, removals
  enrichment/  L2 notices (FR, OFAC, UN, gov.uk, EUR-Lex) and providers (GLEIF, CH, FAA, Wikidata, ICIJ)
  agent/       supervisor, extractors, analyst, guarded tools, SQL guard, tracing, prompts
  ops/         scheduler loop, autopilot + watchdog, workers, incidents, alerts, metrics, settings
  quality/     metrics, issues, field catalog
  api/ web/    FastAPI routes, auth, console templates and static assets
migrations/    Alembic (0001 schema + triggers, 0002 analytics views + analyst role)
```
