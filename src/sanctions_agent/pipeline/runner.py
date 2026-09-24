"""Executes one ingestion run as idempotent, checkpointed steps.

LIST_INGEST:  FETCH -> ARCHIVE -> VALIDATE_FILE -> PARSE -> VALIDATE_DATA -> DIFF_PUBLISH
RELEASE_HELD: DIFF_PUBLISH from a HELD version's retained staging (after reviewer approval)
NOTICE_SYNC / ENRICHMENT_BATCH / ANNEX_BUILD are delegated to the Level-2 / curated modules.

Crash safety (see docs/architecture.md, "How in-progress data moves"): each step commits its own
checkpoint; a resumed run re-uses archived bytes (no re-download) and re-parses into staging; the
publish is a single transaction.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sanctions_agent.canonical.model import CanonicalRecord
from sanctions_agent.db.engine import fetch_all, fetch_one, fetch_val, jsonb, tx
from sanctions_agent.http.client import FetchResult, HttpFetcher, RetryPolicy, fetch_with_retry
from sanctions_agent.http.errors import ErrorClass, FetchError, PipelineError
from sanctions_agent.http.guard import UrlGuard
from sanctions_agent.logs import get_logger, log_context
from sanctions_agent.ops import incidents
from sanctions_agent.pipeline import publish as pub
from sanctions_agent.pipeline import runs, source_state
from sanctions_agent.pipeline.progress import ProgressReporter
from sanctions_agent.pipeline.validate import DiffPreview, validate_data
from sanctions_agent.quality import metrics as qm
from sanctions_agent.quality.issues import record_issues, record_metrics
from sanctions_agent.settings import get_settings
from sanctions_agent.sources.base import Issue, ListAdapter, ParseStats
from sanctions_agent.sources.config_models import ValidationConfig
from sanctions_agent.sources.registry import SourceSpec, get_source
from sanctions_agent.storage.blobstore import get_blob_store

log = get_logger(__name__)
BATCH = 5000


class Cancelled(Exception):
    pass


class RunContext:
    def __init__(self, run: dict[str, Any], source: SourceSpec, cancel_event: threading.Event) -> None:
        self.run = run
        self.run_id = str(run["run_id"])
        self.source = source
        self.cancel_event = cancel_event
        self.options: dict[str, Any] = run.get("options") or {}
        self.progress = ProgressReporter(self.run_id, source.source_id)
        self.work = get_settings().work_dir / self.run_id
        self.work.mkdir(parents=True, exist_ok=True)
        self.version_id: int | None = None
        self.raw_sha256: str | None = None
        self.raw_path: Path | None = None
        self.http_last_modified: str | None = None
        self.fetched = False  # did this run contact the publisher? (politeness clock)

    @property
    def fresh(self) -> bool:
        """New data from the publisher (freshness SLO): a fetch, an out-of-band manual load or a curated build.
        A re-parse of archived bytes is not fresh."""
        return self.fetched or bool(self.options.get("manual_load")) or self.source.kind == "CURATED_LIST"

    def check_cancel(self) -> None:
        if self.cancel_event.is_set() or runs.cancel_requested(self.run_id):
            raise Cancelled("cancel requested")


def _adapter(source: SourceSpec) -> ListAdapter:
    cls = source.adapter_type.load()
    return cls(source.config, source.source_id)  # type: ignore[no-any-return]


# =============================================================================================
class PipelineRunner:
    def __init__(
        self,
        fetcher_factory: Callable[[SourceSpec], HttpFetcher] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.fetcher_factory = fetcher_factory or self._default_fetcher
        self.sleep = sleep

    @staticmethod
    def _default_fetcher(source: SourceSpec) -> HttpFetcher:
        fc = getattr(source.config, "fetch", None)
        hosts = (
            list(fc.allowed_hosts) if fc is not None else list(getattr(source.config, "allowed_hosts", []))
        )
        timeout = fc.timeout_s if fc is not None else None
        return HttpFetcher(UrlGuard(hosts), read_timeout=timeout)

    # -----------------------------------------------------------------------------------------
    def execute(self, run: dict[str, Any], cancel_event: threading.Event | None = None) -> str:
        cancel_event = cancel_event or threading.Event()
        with tx() as conn:
            source = get_source(conn, run["source_id"])
        ctx = RunContext(run, source, cancel_event)
        with log_context(run_id=ctx.run_id, source_id=source.source_id, run_kind=run["run_kind"]):
            log.info("run_start", trigger=run["trigger"], requested_by=run["requested_by"])
            try:
                if run["run_kind"] == "LIST_INGEST":
                    status = self._list_ingest(ctx)
                elif run["run_kind"] == "RELEASE_HELD":
                    status = self._release_held(ctx)
                elif run["run_kind"] in ("NOTICE_SYNC", "ENRICHMENT_BATCH", "ANNEX_BUILD"):
                    from sanctions_agent.enrichment.dispatch import run_level2

                    status = run_level2(ctx)
                else:
                    raise PipelineError(ErrorClass.CONFIG_ERROR, f"unknown run kind {run['run_kind']}")
            except Cancelled:
                status = self._finish_cancelled(ctx)
            except FetchError as e:
                status = self._finish_fetch_failure(ctx, e)
            except PipelineError as e:
                if e.error_class in (ErrorClass.CONFIG_ERROR, ErrorClass.INTERNAL) or ctx.run["run_kind"] in (
                    "NOTICE_SYNC",
                    "ENRICHMENT_BATCH",
                ):
                    status = self._finish_fetch_failure(ctx, FetchError(e.error_class, e.detail))
                else:
                    status = self._finish_data_failure(ctx, e.error_class, e.detail, "QUARANTINED")
            except Exception as e:
                log.exception("run_internal_error")
                status = self._finish_internal(ctx, e)
            finally:
                shutil.rmtree(ctx.work, ignore_errors=True)
            log.info("run_end", status=status)
            return status

    # -----------------------------------------------------------------------------------------
    def _list_ingest(self, ctx: RunContext) -> str:
        src = ctx.source
        resumed: dict[str, dict[str, Any]] = {}
        if ctx.run.get("resumed_from_run_id"):
            with tx() as conn:
                resumed = runs.completed_steps(conn, str(ctx.run["resumed_from_run_id"]))
        # ---- FETCH / ARCHIVE (or reuse archived bytes) ------------------------------------------
        reparse = ctx.options.get("reparse_sha256")
        archived = resumed.get("ARCHIVE", {}).get("sha256") or reparse
        if src.kind == "CURATED_LIST" and not archived:
            from sanctions_agent.sources.l1.eu_annex import build_curated_artifact

            ctx.progress.step("FETCH")
            a = runs.step_start(ctx.run_id, "BUILD")
            path, info = build_curated_artifact(src, ctx.work)
            with tx() as conn:
                runs.step_finish(conn, ctx.run_id, "BUILD", a, detail=info)
            ctx.raw_path = path
            status = self._archive(ctx, path, fetch=None)
        elif archived:
            ctx.progress.step("ARCHIVE")
            a = runs.step_start(ctx.run_id, "ARCHIVE")
            with tx() as conn:
                row = fetch_one(conn, "SELECT * FROM raw_artifact WHERE sha256 = %s", (archived,))
                if row is None:
                    raise PipelineError(ErrorClass.CONFIG_ERROR, f"archived artifact {archived} not found")
                ctx.raw_path = get_blob_store().materialize(row["blob_uri"], ctx.work / "raw")
                ctx.raw_sha256 = archived
                conn.execute(
                    "UPDATE ingestion_run SET raw_sha256 = %s WHERE run_id = %s", (archived, ctx.run_id)
                )
                runs.step_finish(
                    conn,
                    ctx.run_id,
                    "ARCHIVE",
                    a,
                    detail={"sha256": archived, "reused": True, "reason": "reparse" if reparse else "resume"},
                )
            status = "CONTINUE"
            if reparse is None and resumed and "ARCHIVE" in resumed:
                log.info("resume_from_archive", sha256=archived)
        else:
            fetch = self._fetch(ctx)
            if fetch.not_modified:
                return self._finish_no_change(ctx, "HTTP 304 Not Modified", fetched=True)
            assert fetch.path is not None
            status = self._archive(ctx, fetch.path, fetch=fetch)
        if status != "CONTINUE":
            return status
        ctx.check_cancel()
        # ---- VALIDATE_FILE ----------------------------------------------------------------------
        adapter = _adapter(src)
        assert ctx.raw_path is not None
        self._validate_file(ctx, adapter)
        ctx.check_cancel()
        # ---- PARSE ------------------------------------------------------------------------------
        stats = self._parse(ctx, adapter)
        ctx.check_cancel()
        # ---- VALIDATE_DATA ----------------------------------------------------------------------
        outcome = self._validate_data(ctx, adapter, stats)
        if outcome == "QUARANTINE":
            return "QUARANTINED"
        if ctx.options.get("dry_run"):
            return self._finish_dry_run(ctx)
        if outcome == "HOLD":
            return "HELD"
        ctx.check_cancel()
        # ---- DIFF_PUBLISH -----------------------------------------------------------------------
        return self._publish(ctx)

    # -----------------------------------------------------------------------------------------
    def _fetch(self, ctx: RunContext) -> FetchResult:
        src = ctx.source
        fc = src.config.fetch  # type: ignore[attr-defined]
        ctx.progress.step("FETCH")
        attempt_no = runs.step_start(ctx.run_id, "FETCH")
        ctx.fetched = True
        with (
            tx() as conn
        ):  # record the contact now, so politeness holds even if this process dies mid-download
            conn.execute("UPDATE source SET last_attempt_at = now() WHERE source_id = %s", (src.source_id,))
        conditional = None
        if fc.conditional_get and not ctx.options.get("force_refetch") and src.current_version_id:
            with tx() as conn:
                last = fetch_one(
                    conn,
                    """SELECT response_headers FROM fetch_evidence WHERE source_id = %s AND sha256 =
                         (SELECT raw_sha256 FROM list_version WHERE version_id = %s) ORDER BY fetched_at DESC LIMIT 1""",
                    (src.source_id, src.current_version_id),
                )
            if last:
                h = last["response_headers"]
                conditional = {"etag": h.get("etag"), "last_modified": h.get("last-modified")}
        policy = RetryPolicy(fc.retry.max_attempts, fc.retry.base_delay_s, fc.retry.max_delay_s)
        dest = ctx.work / "raw"

        def on_attempt(n: int, res: FetchResult | None, err: FetchError | None) -> None:
            ev = res.evidence() if res else err.evidence if err else {}
            with tx() as conn:
                conn.execute(
                    """INSERT INTO fetch_evidence (run_id, source_id, attempt, requested_url, final_url, redirect_chain,
                           http_status, response_headers, tls_chain_pem, tls_leaf_sha256, duration_ms, not_modified,
                           conditional, sha256, size_bytes, error_class, error_detail)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        ctx.run_id,
                        src.source_id,
                        n,
                        fc.url,
                        ev.get("final_url"),
                        jsonb(ev.get("redirect_chain", [])),
                        ev.get("http_status") or (err.http_status if err else None),
                        jsonb(ev.get("headers", {})),
                        ev.get("tls_chain_pem"),
                        ev.get("tls_leaf_sha256"),
                        ev.get("duration_ms"),
                        bool(ev.get("not_modified")),
                        jsonb(ev.get("conditional", {})),
                        res.sha256 if res else None,
                        ev.get("size_bytes"),
                        err.error_class.value if err else None,
                        err.detail[:2000] if err else None,
                    ),
                )
            if err is not None:
                ctx.progress.update(force=True, retries=n, last_error=err.error_class.value)

        with self.fetcher_factory(src) as fetcher:
            result, attempts = fetch_with_retry(
                lambda: fetcher.fetch_to_file(
                    fc.url,
                    dest,
                    conditional=conditional,
                    max_bytes=fc.max_mb * 1024 * 1024,
                    extra_headers=fc.extra_headers or None,
                    progress=lambda done, total: ctx.progress.update(bytes_done=done, bytes_total=total),
                    should_cancel=ctx.cancel_event.is_set,
                ),
                policy,
                on_attempt=on_attempt,
                sleep=self.sleep or __import__("time").sleep,
                should_cancel=lambda: runs.cancel_requested(ctx.run_id),
            )
        ctx.http_last_modified = result.headers.get("last-modified")
        with tx() as conn:
            runs.step_finish(
                conn,
                ctx.run_id,
                "FETCH",
                attempt_no,
                detail={
                    "attempts": attempts,
                    "http_status": result.http_status,
                    "size_bytes": result.size_bytes,
                    "not_modified": result.not_modified,
                    "final_url": result.final_url,
                    "sha256": result.sha256,
                    "duration_ms": result.duration_ms,
                },
            )
        return result

    def _archive(self, ctx: RunContext, path: Path, fetch: FetchResult | None) -> str:
        src = ctx.source
        ctx.progress.step("ARCHIVE")
        a = runs.step_start(ctx.run_id, "ARCHIVE")
        from sanctions_agent.storage.blobstore import sha256_file

        sha = fetch.sha256 if fetch and fetch.sha256 else sha256_file(path)
        uri = get_blob_store().put_file(path, sha)
        with tx() as conn:
            conn.execute(
                "INSERT INTO raw_artifact (sha256, blob_uri, size_bytes, content_type, compression)"
                " VALUES (%s, %s, %s, %s, 'gzip') ON CONFLICT DO NOTHING",
                (sha, uri, path.stat().st_size, fetch.content_type if fetch else "application/json"),
            )
            conn.execute("UPDATE ingestion_run SET raw_sha256 = %s WHERE run_id = %s", (sha, ctx.run_id))
            runs.step_finish(
                conn,
                ctx.run_id,
                "ARCHIVE",
                a,
                detail={"sha256": sha, "blob_uri": uri, "size_bytes": path.stat().st_size},
            )
            ctx.raw_sha256 = sha
            ctx.raw_path = path
            if not ctx.options.get("force_refetch") and not ctx.options.get("dry_run"):
                cur = fetch_one(
                    conn,
                    "SELECT version_id, raw_sha256 FROM list_version WHERE source_id = %s"
                    " AND status = 'PUBLISHED'",
                    (src.source_id,),
                )
                if cur and cur["raw_sha256"] == sha:
                    return self._finish_no_change_conn(
                        conn, ctx, "same SHA-256 as the published version", fetched=fetch is not None
                    )
                pending = fetch_one(
                    conn,
                    "SELECT version_id, seq, status FROM list_version WHERE source_id = %s AND raw_sha256 = %s"
                    " AND status IN ('HELD','QUARANTINED') ORDER BY seq DESC LIMIT 1",
                    (src.source_id, sha),
                )
                if pending:
                    runs.finish_run(
                        conn,
                        ctx.run_id,
                        pending["status"],
                        error_class="UNCHANGED_PENDING",
                        error_detail=f"same content as version seq {pending['seq']}, still {pending['status']}",
                        summary={"same_as_version_id": pending["version_id"]},
                    )
                    source_state.on_data_problem(conn, src.source_id, fetched=ctx.fetched)
                    return str(pending["status"])
        return "CONTINUE"

    def _validate_file(self, ctx: RunContext, adapter: ListAdapter) -> None:
        src = ctx.source
        assert ctx.raw_path is not None and ctx.raw_sha256 is not None
        ctx.progress.step("VALIDATE_FILE")
        a = runs.step_start(ctx.run_id, "VALIDATE_FILE")
        issues = adapter.validate_file(ctx.raw_path)
        info = adapter.read_info(ctx.raw_path) if not any(i.severity == "FAIL" for i in issues) else None
        published_at = info.published_at if info else None
        marker = info.publication_marker if info else None
        if info and not marker and ctx.http_last_modified:
            marker = f"http-last-modified: {ctx.http_last_modified}"
        with tx() as conn:
            seq = int(
                fetch_val(
                    conn,
                    "SELECT coalesce(max(seq), 0) + 1 FROM list_version WHERE source_id = %s",
                    (src.source_id,),
                )
            )
            prev = fetch_one(
                conn,
                "SELECT version_id, published_at_source, publication_marker FROM list_version"
                " WHERE source_id = %s AND status = 'PUBLISHED'",
                (src.source_id,),
            )
            if (
                prev
                and published_at
                and prev["published_at_source"]
                and published_at < prev["published_at_source"]
            ):
                issues.append(
                    Issue(
                        "PUBLICATION_MARKER_REGRESSION",
                        "FAIL",
                        f"publication marker {marker} is older than the published {prev['publication_marker']}",
                    )
                )
            val_cfg: ValidationConfig | None = getattr(src.config, "validation", None)
            if val_cfg and val_cfg.require_publication_marker and not marker:
                issues.append(Issue("MISSING_REQUIRED_FIELD", "FAIL", "file has no publication marker"))
            version_id = int(
                fetch_val(
                    conn,
                    """INSERT INTO list_version (source_id, seq, run_id, raw_sha256, publication_marker, published_at_source,
                       status, previous_version_id, validation_report)
                   VALUES (%s,%s,%s,%s,%s,%s,'CANDIDATE',%s,%s) RETURNING version_id""",
                    (
                        src.source_id,
                        seq,
                        ctx.run_id,
                        ctx.raw_sha256,
                        marker,
                        published_at,
                        prev["version_id"] if prev else None,
                        jsonb({"file_issues": [i.__dict__ for i in issues]}),
                    ),
                )
            )
            ctx.version_id = version_id
            conn.execute(
                "UPDATE ingestion_run SET version_id = %s WHERE run_id = %s", (version_id, ctx.run_id)
            )
            record_issues(conn, src.source_id, version_id, ctx.run_id, issues)
            fails = [i for i in issues if i.severity == "FAIL"]
            runs.step_finish(
                conn,
                ctx.run_id,
                "VALIDATE_FILE",
                a,
                status="FAILED" if fails else "DONE",
                detail={"issues": len(issues), "marker": marker, "seq": seq},
            )
        if fails:
            ec = (
                ErrorClass.PUBLICATION_MARKER_REGRESSION
                if fails[0].category == "PUBLICATION_MARKER_REGRESSION"
                else ErrorClass.SCHEMA_INVALID
            )
            raise PipelineError(ec, fails[0].message)

    def _parse(self, ctx: RunContext, adapter: ListAdapter) -> ParseStats:
        src = ctx.source
        assert ctx.raw_path is not None
        ctx.progress.step("PARSE")
        a = runs.step_start(ctx.run_id, "PARSE")
        with tx() as conn:
            pub.purge_staging(conn, [ctx.run_id])
            prev_count = fetch_val(
                conn,
                "SELECT record_count FROM list_version WHERE source_id = %s AND status = 'PUBLISHED'",
                (src.source_id,),
            )
        ctx.progress.update(force=True, records_done=0, records_expected=prev_count)
        stats = ParseStats()
        seen: set[str] = set()
        duplicates = 0
        batch: list[CanonicalRecord] = []
        done = 0

        def flush() -> None:
            nonlocal batch
            if not batch:
                return
            with (
                tx() as conn,
                conn.cursor() as cur,
                cur.copy(
                    "COPY sanctions.staging_record (run_id, source_key, entity_type, content_hash, primary_name, doc)"
                    " FROM STDIN"
                ) as cp,
            ):
                cp.set_types(
                    ["text", "text", "text", "text", "text", "text"]
                )  # doc: JSON text parsed server-side
                for r in batch:
                    cp.write_row(
                        (
                            ctx.run_id,
                            r.source_key,
                            r.entity_type,
                            r.content_hash(),
                            r.primary_name,
                            r.canonical_json(),
                        )
                    )
            batch = []

        try:
            for rec in adapter.parse(ctx.raw_path, stats):
                if rec.source_key in seen:
                    duplicates += 1
                    continue
                seen.add(rec.source_key)
                batch.append(rec)
                done += 1
                if len(batch) >= BATCH:
                    flush()
                    ctx.progress.update(records_done=done)
                    ctx.check_cancel()
            flush()
        except Cancelled:
            raise
        except Exception as e:
            raise PipelineError(ErrorClass.PARSER_ERROR, f"parser failed after {done} records: {e!r}") from e
        known = adapter.known_paths
        with tx() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO staging_path_stats (run_id, xml_path, occurrences, known) VALUES (%s,%s,%s,%s)",
                    [(ctx.run_id, p, c, (p in known) if known else True) for p, c in stats.paths.items()],
                )
            runs.step_finish(
                conn,
                ctx.run_id,
                "PARSE",
                a,
                detail={
                    "records": done,
                    "duplicates": duplicates,
                    "skipped": stats.skipped_records,
                    "warnings": dict(stats.warnings),
                    "unmapped_countries": sum(stats.unmapped_countries.values()),
                    "paths": len(stats.paths),
                },
            )
        ctx.progress.update(force=True, records_done=done)
        stats.warnings["__duplicates__"] = duplicates
        return stats

    def _validate_data(self, ctx: RunContext, adapter: ListAdapter, stats: ParseStats) -> str:
        src = ctx.source
        assert ctx.version_id is not None
        ctx.progress.step("VALIDATE_DATA")
        a = runs.step_start(ctx.run_id, "VALIDATE_DATA")
        cfg: ValidationConfig = getattr(src.config, "validation", None) or ValidationConfig()
        with tx() as conn:
            m = qm.VersionMetrics()
            with conn.cursor(name=f"stg_{ctx.run_id.replace('-', '')}") as cur:
                cur.itersize = 2000
                cur.execute("SELECT doc FROM staging_record WHERE run_id = %s", (ctx.run_id,))
                for (doc,) in ((r["doc"],) for r in cur):
                    m.add(doc)
            unknown = [
                r["xml_path"]
                for r in fetch_all(
                    conn,
                    "SELECT xml_path FROM staging_path_stats WHERE run_id = %s AND NOT known ORDER BY xml_path",
                    (ctx.run_id,),
                )
            ]
            dp = pub.diff_preview(conn, src.source_id, ctx.run_id)
            prev_fill = {
                (r["entity_type"], r["field"]): float(r["value"])
                for r in fetch_all(
                    conn,
                    """SELECT m.entity_type, m.field, m.value FROM dq_metric m JOIN list_version v USING (version_id)
                   WHERE v.source_id = %s AND v.status = 'PUBLISHED' AND m.metric = 'fill_rate'""",
                    (src.source_id,),
                )
            }
            duplicates = stats.warnings.pop("__duplicates__", 0)
            res = validate_data(
                cfg=cfg,
                metrics=m,
                unknown_paths=unknown,
                duplicates=duplicates,
                skipped_records=stats.skipped_records,
                parse_warnings={k: v for k, v in stats.warnings.items() if v},
                diff=DiffPreview(
                    previous_count=dp["previous_count"],
                    added=dp["added"],
                    changed=dp["changed"],
                    removed=dp["removed"],
                    unchanged=dp["unchanged"],
                ),
                previous_fill=prev_fill,
            )
            record_metrics(conn, src.source_id, ctx.version_id, m, prev_fill, cfg)
            record_issues(conn, src.source_id, ctx.version_id, ctx.run_id, res.issues)
            status = {"PASS": "VALIDATED", "HOLD": "HELD", "QUARANTINE": "QUARANTINED"}[res.outcome]
            conn.execute(
                "UPDATE list_version SET status = %s, record_count = %s, counts_by_type = %s,"
                " validation_report = validation_report || %s WHERE version_id = %s",
                (status, m.record_count, jsonb(dict(m.counts_by_type)), jsonb(res.report), ctx.version_id),
            )
            runs.step_finish(
                conn,
                ctx.run_id,
                "VALIDATE_DATA",
                a,
                status="FAILED" if res.outcome == "QUARANTINE" else "DONE",
                detail={"outcome": res.outcome, "diff_preview": dp, "issues": len(res.issues)},
            )
            if res.outcome == "QUARANTINE":
                self._close_data_problem(
                    conn,
                    ctx,
                    "QUARANTINED",
                    res.primary_error or "SCHEMA_INVALID",
                    "; ".join(i.message for i in res.issues if i.severity == "FAIL")[:1000],
                    {"diff_preview": dp},
                )
            elif res.outcome == "HOLD" and not ctx.options.get("dry_run"):
                change_id = fetch_val(
                    conn,
                    """INSERT INTO proposed_change (kind, source_id, subject_ref, title, payload, rationale, proposed_by,
                           dedupe_key)
                       VALUES ('LARGE_CHANGE_RELEASE', %s, %s, %s, %s, %s, 'system:validator', %s)
                       ON CONFLICT DO NOTHING RETURNING change_id""",
                    (
                        src.source_id,
                        f"version:{ctx.version_id}",
                        f"Release held {src.source_id} version (+{dp['added']} / -{dp['removed']} / ~{dp['changed']})",
                        jsonb({"version_id": ctx.version_id, "run_id": ctx.run_id, "diff_preview": dp}),
                        "Change volume exceeded the configured circuit-breaker bounds; confirm against the publisher's "
                        "notices before release.",
                        f"release:{src.source_id}:{ctx.version_id}",
                    ),
                )
                self._close_data_problem(
                    conn,
                    ctx,
                    "HELD",
                    "COUNT_ANOMALY",
                    f"large change held for review (+{dp['added']}/-{dp['removed']})",
                    {"diff_preview": dp, "proposed_change_id": change_id},
                )
        return res.outcome

    def _publish(self, ctx: RunContext) -> str:
        src = ctx.source
        assert ctx.version_id is not None
        ctx.progress.step("DIFF_PUBLISH")
        a = runs.step_start(ctx.run_id, "DIFF_PUBLISH")
        with tx(actor=f"run:{ctx.run_id}") as conn:
            summary = pub.publish_version(
                conn, source_id=src.source_id, run_id=ctx.run_id, version_id=ctx.version_id
            )
            runs.step_finish(conn, ctx.run_id, "DIFF_PUBLISH", a, detail=summary)
            source_state.on_success(conn, src.source_id, changed=True, fetched=ctx.fetched, fresh=ctx.fresh)
            self._consume_signals(conn, ctx)
            incidents.resolve(
                conn,
                source_id=src.source_id,
                error_classes=[e.value for e in ErrorClass if e.value != "CORE_SOURCE_DISABLED"],
                note=f"version {ctx.version_id} published by run {ctx.run_id}",
            )
            runs.finish_run(conn, ctx.run_id, "SUCCEEDED", summary=summary)
        with tx() as conn:
            pub.purge_staging(conn, [ctx.run_id])
        return "SUCCEEDED"

    # -----------------------------------------------------------------------------------------
    def _release_held(self, ctx: RunContext) -> str:
        """Publish a HELD version from its retained staging after a reviewer approved the release."""
        version_id = int(ctx.options["version_id"])
        with tx() as conn:
            ver = fetch_one(conn, "SELECT * FROM list_version WHERE version_id = %s", (version_id,))
            if ver is None or ver["status"] != "HELD":
                raise PipelineError(ErrorClass.CONFIG_ERROR, f"version {version_id} is not HELD")
            staged = fetch_val(
                conn, "SELECT count(*) FROM staging_record WHERE run_id = %s", (ver["run_id"],)
            )
        if staged != ver["record_count"]:
            raise PipelineError(
                ErrorClass.INTERNAL,
                f"retained staging for version {version_id} has {staged} rows, expected {ver['record_count']};"
                " re-run the source instead",
            )
        ctx.progress.step("DIFF_PUBLISH")
        a = runs.step_start(ctx.run_id, "DIFF_PUBLISH")
        with tx(actor=ctx.run["requested_by"]) as conn:
            summary = pub.publish_version(
                conn,
                source_id=ctx.source.source_id,
                run_id=ctx.run_id,
                version_id=version_id,
                staging_run_id=str(ver["run_id"]),
                released_by=ctx.run["requested_by"],
            )
            runs.step_finish(conn, ctx.run_id, "DIFF_PUBLISH", a, detail=summary)
            source_state.on_success(conn, ctx.source.source_id, changed=True, fetched=False)
            conn.execute(
                "UPDATE ingestion_run SET version_id = %s, raw_sha256 = %s WHERE run_id = %s",
                (version_id, ver["raw_sha256"], ctx.run_id),
            )
            incidents.resolve(
                conn,
                source_id=ctx.source.source_id,
                error_classes=["COUNT_ANOMALY"],
                note=f"held version {version_id} released by {ctx.run['requested_by']}",
            )
            runs.finish_run(
                conn, ctx.run_id, "SUCCEEDED", summary={**summary, "released_version_id": version_id}
            )
        with tx() as conn:
            pub.purge_staging(conn, [str(ver["run_id"])])
        return "SUCCEEDED"

    # -----------------------------------------------------------------------------------------
    def _consume_signals(self, conn: Any, ctx: RunContext) -> None:
        conn.execute(
            "UPDATE signal SET consumed_by_run_id = %s, consumed_at = now()"
            " WHERE target_source_id = %s AND consumed_at IS NULL",
            (ctx.run_id, ctx.source.source_id),
        )

    def _finish_no_change(self, ctx: RunContext, why: str, fetched: bool) -> str:
        with tx() as conn:
            return self._finish_no_change_conn(conn, ctx, why, fetched)

    def _finish_no_change_conn(self, conn: Any, ctx: RunContext, why: str, fetched: bool) -> str:
        source_state.on_success(
            conn, ctx.source.source_id, changed=False, fetched=fetched, fresh=fetched or ctx.fresh
        )
        unconsumed = fetch_val(
            conn,
            "SELECT count(*) FROM signal WHERE target_source_id = %s AND consumed_at IS NULL"
            " AND observed_at < now() - interval '6 hours'",
            (ctx.source.source_id,),
        )
        if unconsumed:
            incidents.open_incident(
                conn,
                source_id=ctx.source.source_id,
                error_class="SIGNAL_WITHOUT_CHANGE",
                severity="WARN",
                title=f"{ctx.source.source_id}: notifications arrived but the file has not changed for 6h+",
                summary=f"{unconsumed} signal(s) older than 6h with no new file version (NFR-04)",
            )
        incidents.resolve(
            conn,
            source_id=ctx.source.source_id,
            error_classes=[e.value for e in ErrorClass if e.value not in ("SIGNAL_WITHOUT_CHANGE",)],
            note=f"healthy fetch ({why})",
        )
        runs.finish_run(conn, ctx.run_id, "NO_CHANGE", summary={"reason": why})
        return "NO_CHANGE"

    def _finish_dry_run(self, ctx: RunContext) -> str:
        with tx() as conn:
            conn.execute(
                "UPDATE list_version SET status = 'REJECTED', validation_report = validation_report || %s"
                " WHERE version_id = %s",
                (jsonb({"dry_run": True}), ctx.version_id),
            )
            runs.finish_run(
                conn,
                ctx.run_id,
                "DRY_RUN_OK",
                summary={"version_id": ctx.version_id, "note": "dry run: validated, not published"},
            )
        return "DRY_RUN_OK"

    def _close_data_problem(
        self, conn: Any, ctx: RunContext, status: str, error_class: str, detail: str, summary: dict[str, Any]
    ) -> None:
        src = ctx.source
        sev = "WARN" if status == "HELD" else ("PAGE" if src.is_core else "WARN")
        iid, _ = incidents.open_incident(
            conn,
            source_id=src.source_id,
            error_class=error_class,
            severity=sev,
            title=f"{src.source_id}: new file {status.lower()} ({error_class})",
            summary=f"{detail} (run {ctx.run_id}); last good version remains live",
        )
        incidents.add_action(
            conn,
            iid,
            {
                "at": datetime.now(UTC).isoformat(),
                "by": "system",
                "action": f"version {ctx.version_id} {status}",
                "run_id": ctx.run_id,
            },
        )
        source_state.on_data_problem(conn, src.source_id, fetched=ctx.fetched)
        runs.finish_run(
            conn,
            ctx.run_id,
            status,
            error_class=error_class,
            error_detail=detail,
            summary={**summary, "version_id": ctx.version_id, "incident_id": iid},
        )

    def _finish_data_failure(self, ctx: RunContext, error_class: ErrorClass, detail: str, status: str) -> str:
        with tx() as conn:
            if ctx.version_id:
                conn.execute(
                    "UPDATE list_version SET status = 'QUARANTINED' WHERE version_id = %s"
                    " AND status IN ('CANDIDATE','VALIDATED')",
                    (ctx.version_id,),
                )
            self._close_data_problem(conn, ctx, status, error_class.value, detail, {})
        return status

    def _finish_fetch_failure(self, ctx: RunContext, e: FetchError) -> str:
        if e.error_class == ErrorClass.CANCELLED:
            return self._finish_cancelled(ctx)
        src = ctx.source
        with tx() as conn:
            state = source_state.on_failure(conn, src.source_id)
            stale_hours = fetch_val(
                conn,
                "SELECT extract(epoch FROM now() - coalesce(last_success_at, created_at))/3600"
                " FROM source WHERE source_id = %s",
                (src.source_id,),
            )
            sev = (
                "PAGE"
                if stale_hours and stale_hours >= src.schedule.hard_max_staleness.total_seconds() / 3600
                else "WARN"
            )
            iid, _ = incidents.open_incident(
                conn,
                source_id=src.source_id,
                error_class=e.error_class.value,
                severity=sev,
                title=f"{src.source_id}: fetch failing ({e.error_class.value})",
                summary=f"{e.detail[:500]} | failures in a row: {state['consecutive_failures']}, "
                f"breaker {state['breaker_state']}, next attempt {state['next_due_at']}",
            )
            runs.finish_run(
                conn,
                ctx.run_id,
                "FAILED",
                error_class=e.error_class.value,
                error_detail=e.detail,
                summary={**state, "incident_id": iid, "http_status": e.http_status},
            )
        return "FAILED"

    def _finish_cancelled(self, ctx: RunContext) -> str:
        with tx() as conn:
            if ctx.version_id:
                conn.execute(
                    "UPDATE list_version SET status = 'REJECTED', validation_report = validation_report || %s"
                    " WHERE version_id = %s AND status IN ('CANDIDATE','VALIDATED')",
                    (jsonb({"cancelled": True}), ctx.version_id),
                )
            pub.purge_staging(conn, [ctx.run_id])
            runs.finish_run(
                conn, ctx.run_id, "CANCELLED", error_class="CANCELLED", error_detail="cancelled on request"
            )
            conn.execute(
                "UPDATE source SET last_attempt_at = now() WHERE source_id = %s", (ctx.source.source_id,)
            )
        return "CANCELLED"

    def _finish_internal(self, ctx: RunContext, e: Exception) -> str:
        with tx() as conn:
            if ctx.version_id:
                conn.execute(
                    "UPDATE list_version SET status = 'REJECTED' WHERE version_id = %s"
                    " AND status IN ('CANDIDATE','VALIDATED')",
                    (ctx.version_id,),
                )
            state = source_state.on_failure(conn, ctx.source.source_id)
            incidents.open_incident(
                conn,
                source_id=ctx.source.source_id,
                error_class="INTERNAL",
                severity="WARN",
                title=f"{ctx.source.source_id}: internal error",
                summary=repr(e)[:1000],
            )
            runs.finish_run(
                conn, ctx.run_id, "FAILED", error_class="INTERNAL", error_detail=repr(e), summary=state
            )
        return "FAILED"


def run_once(
    source_id: str,
    *,
    trigger: str = "MANUAL",
    requested_by: str = "cli",
    reason: str | None = None,
    options: dict[str, Any] | None = None,
    runner: PipelineRunner | None = None,
    run_kind: str | None = None,
) -> tuple[str, str]:
    """Queue and synchronously execute a run (CLI / tests). Returns (run_id, status)."""
    with tx(actor=requested_by) as conn:
        kind = (
            run_kind
            or {
                "STRUCTURED_LIST": "LIST_INGEST",
                "CURATED_LIST": "LIST_INGEST",
                "NOTICE_FEED": "NOTICE_SYNC",
                "ENRICHMENT": "ENRICHMENT_BATCH",
            }[get_source(conn, source_id).kind]
        )
        run_id = runs.enqueue_run(
            conn,
            source_id=source_id,
            run_kind=kind,
            trigger=trigger,
            requested_by=requested_by,
            reason=reason,
            options=options,
        )
    with tx() as conn:
        row = fetch_one(
            conn,
            "UPDATE ingestion_run SET status = 'RUNNING', lease_owner = 'inline', started_at = now(),"
            " lease_expires_at = now() + interval '1 hour' WHERE run_id = %s RETURNING *",
            (run_id,),
        )
    assert row is not None
    status = (runner or PipelineRunner()).execute(row)
    return run_id, status
