# Runbook

Where to look first:
* **Console:** Overview, then Runs & progress, then the run detail page, which shows steps, fetch evidence
  and validation.
* **Incidents:** every automated diagnosis is written to `incident.diagnosis` / `incident.actions`.
* **Ask the agent** (aggregate questions): "why did the last EU FSF run fail?", "which sources are stale?"
* **CLI** on the supervisor host: `sanctions-agent status`.

Principles:
* **Screening always has the last good version.** Nothing in this runbook requires rushing a bad file through.
* **A removal never un-blocks by itself.** A person confirms it in the review queue.
* **The LLM is optional.** With the agent off or unavailable, the deterministic autopilot and watchdog keep
  every source on schedule.

---

## Reading an incident

Each incident has an `error_class`. This table says what it means and what to do.

| Error class | Meaning | Automatic response | You |
|---|---|---|---|
| `TRANSIENT_NETWORK`, `HTTP_5XX`, `HTTP_429`, `EMPTY_BODY` | Publisher or network hiccup, rate limit, truncated response | Retried with backoff; the breaker opens after repeated failures | Usually nothing. If it lasts over 2 h see [breaker open](#circuit-breaker-open--repeated-fetch-failures) |
| `HTTP_403` | Blocked by the publisher (UA, IP, WAF) or by **our own egress proxy** | No blind retries; incident opened | Check the egress allow-list and proxy first, then the User-Agent (`SANCTIONS_USER_AGENT`), then contact the publisher |
| `HTTP_404` | Download URL changed or removed | The agent looks for the new link and files a CONFIG_CHANGE proposal | Verify the proposal, then a **second admin** approves it |
| `TLS_ERROR` | Certificate problem | Incident; no retry storm | Compare the TLS chain in the run's fetch evidence; never disable verification |
| `HOST_NOT_ALLOWED`, `REDIRECT_BLOCKED` | A URL or redirect went to a host not on the allow-list (or a private address) | Refused (security control) | If the new host is legitimate, add it through code review to `config/allowed_hosts.yaml`, then to the source |
| `SCHEMA_INVALID` | Not well formed, or fails the pinned XSD / JSON schema | Quarantined | Compare with the publisher's announcements; if the schema changed, `pin-schemas` again after review |
| `SCHEMA_DRIFT` | New element paths in the file | Warns (or quarantines if `drift_policy: quarantine`) | Check the drift list on the run page, run `verify-sources`, update the parser and known paths, then [re-parse](#re-parse-after-a-parser-fix) |
| `FILL_RATE_BELOW_FLOOR` | A field's coverage dropped below its floor | **Quarantined**; the last good version stays live | See [quarantined version](#a-version-was-quarantined) |
| `COUNT_ANOMALY` | Too few records, or a record-count swing | Quarantined (too few) or held (large change) | See [held large change](#a-large-change-is-held) |
| `PUBLICATION_MARKER_REGRESSION` | The file claims an older publication date than the one we hold | Quarantined | Usually a stale CDN copy; the next pull normally fixes it |
| `TOO_LARGE` | File exceeded the source's `max_mb` cap | Failed | Check that the file is genuine; raise `fetch.max_mb` under the source's Configuration tab (admin) |
| `PARSER_ERROR` | The parser crashed | Quarantined, incident with a sample | Fix the parser, then [re-parse](#re-parse-after-a-parser-fix) |
| `STALE_NO_CHANGE` | No new version for longer than the source's `no_change_alert_days` | Forced re-pull, then an alert | See [no change for a long time](#no-change-for-a-long-time) |

## A source is stale

1. Overview: open the source. Check its health, breaker and last run.
2. Open the last run and read the Error line and the fetch evidence (HTTP status, final URL, redirects).
3. The watchdog has probably already forced a pull (trigger `WATCHDOG`). If the cause is on the
   publisher's side and the site is down, see [manual load](#manual-load-publisher-unreachable-nfr-08).
4. If the source is paused on purpose, check `paused_until`. It resumes automatically after that.

## A version was quarantined

The last good version is still in screening. On the run page, **Data-quality issues raised** says why.
On **Data quality**, the latest validated candidate is shown with its failing fields in red.

* **Real drop at the publisher** (for example, a field genuinely removed). Decide with Compliance whether to
  accept it. If accepted, lower the floor under *Sources & schedules → Validation thresholds* ("Suggest
  from last 5 versions" helps), then run a **re-parse** of the same file.
* **Parser problem.** Fix the code, deploy, then [re-parse](#re-parse-after-a-parser-fix).

## A large change is held

More removals or additions than the configured bounds put the version on HELD. A LARGE_CHANGE_RELEASE
proposal appears in the **Review queue**. Compare the counts with the publisher's own announcement (the
Federal Register, OFAC Recent Actions or the EU OJ; the Enrichment & evidence page lists recent notices).
Approve to publish from the retained staging, or reject.

## Removal holds

Every REMOVE event creates a `removal_candidate`, and the party **keeps blocking**. The agent searches for
evidence: the UN de-listing log by reference number, the Federal Register, OFAC Recent Actions, UK notices
and EU amending acts. It also checks whether the party is still on another list. It then files a
REMOVAL_CONFIRMATION proposal with a recommendation. The reviewer picks one outcome:

* **Confirmed:** stop blocking.
* **Still listed elsewhere:** keep blocking via the other list.
* **ID change, not a removal:** the publisher re-keyed the entry.
* **Parser problem:** keep blocking until the file is re-parsed.

## A batch needs review or failed

On *Runs & progress*, expand the batch. Each source row links to its run.
* **Failed** (red): handle each failed source with the incident table above. Usually these are fetch
  errors; the breaker and retries are already working on them.
* **Needs review** (amber): a source was quarantined or held. See [quarantined version](#a-version-was-quarantined)
  and [held large change](#a-large-change-is-held).
* **Not queued:** a source the requester selected was refused. The reason is shown: paused, politeness
  interval, breaker open, or already running. Nothing is wrong with the data. Re-run it later or ask an admin
  for a politeness override.

## Circuit breaker open / repeated fetch failures

After repeated failures the breaker opens and retries back off, up to 6 h. A single probe then runs
(half-open). To retry sooner after fixing the cause: *Sources & schedules → Run now → Force re-fetch*.
Operators get a politeness error if the last attempt was too recent; admins may override down to 5 minutes.

## Manual load (publisher unreachable, NFR-08)

When a publisher site is down, or blocked from our network, but the file is available another way (a
colleague's download, or the publisher's alternative mirror):

```bash
sanctions-agent load-manual --source eu_fsf --file ./xmlFullSanctionsList_1_1.xml \
  --reason "EU FSF site down since 09:00, file from mirror X" [--dry-run]
```

The file is archived write-once with its hash and then goes through the **same** validation and publish
pipeline. The run records `manual_load: true`, the file name and your reason as evidence. Try `--dry-run`
first.

## Re-parse after a parser fix

No download is needed. The archived bytes are re-validated, re-parsed and re-published:
* **Console:** *Sources & schedules → Run now → Re-parse an archived file* (pick the version).
* **CLI:** `sanctions-agent reparse --source uk_fcdo --sha256 <hash from the run page>`.

A re-parse does not count as fresh data, and it does not reset the politeness clock.

## No change for a long time

Official lists change often. A long silence usually means we keep getting a cached or stale copy. Check
the fetch evidence for `304`s and the `ETag` / `Last-Modified` headers. If in doubt, **Force re-fetch**
(this ignores conditional GET). If a publisher signal (an RSS item or a notice) arrived without a file
change, the agent raises `SIGNAL_WITHOUT_CHANGE`.

## A core list is paused or disabled

While a core list is paused or disabled, every console page shows a red banner and alerts repeat. Only an
admin can disable a core list, and a reason is required. Re-enable it on *Sources & schedules*. Paused
sources resume automatically at `paused_until`.

## Review backlog

Reviewers work the **Review queue**. The maker-checker rule means you cannot approve your own proposal.
Configuration changes and source activations need an admin, who must not be the person who made the change.

## Agent unavailable or over budget

The **Agent activity** page shows cycles with `fallback` and the reason. Ingestion continues on autopilot.
* **Budget:** raise it under *Global settings* if needed. `sanctions_agent_cost_usd_today` tracks spend.
* **OpenAI outage:** nothing to do; cycles resume automatically.
* **To stop LLM use entirely:** *Global settings → Agent enabled* off.

## Maintenance mode

*Global settings → Maintenance mode* stops **new** pulls: scheduled, agent and watchdog. Runs in progress
finish. Use it for database upgrades and publisher-agreed quiet periods. A banner shows on every page.

## Database or API down

The supervisor and API are stateless apart from Postgres. Restart the containers. On startup, abandoned
runs are reclaimed and resumed from their last checkpoint. `/ready` reports the schema version.

## Rolling back a configuration change

Under *Sources & schedules → source → History*, **Roll back** creates a new version with the old
settings. Nothing is edited in place. Sensitive fields such as URLs and hosts still need a second admin.

## Useful SQL (as the application user)

```sql
-- what did screening use at 14:00 yesterday?
SELECT * FROM sanctions.records_as_of(
  (SELECT max(snapshot_id) FROM sanctions.screening_snapshot WHERE created_at <= now() - interval '1 day'));
-- every fetch attempt for a run, including failures
SELECT attempt, http_status, final_url, error_class, sha256 FROM sanctions.fetch_evidence WHERE run_id = '<run>';
-- who changed what (audit)
SELECT at, actor, table_name, op, row_pk FROM sanctions.audit_log ORDER BY at DESC LIMIT 50;
```
