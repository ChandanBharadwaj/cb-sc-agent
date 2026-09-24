"""ENRICHMENT_BATCH runs and on-demand (per-hit) enrichment."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import psycopg

from sanctions_agent.db.engine import fetch_one, fetch_val, jsonb, tx
from sanctions_agent.enrichment.base import Enricher, EnrichResult, Subject, archive_raw
from sanctions_agent.http.client import HttpFetcher
from sanctions_agent.http.errors import ErrorClass, FetchError, PipelineError
from sanctions_agent.http.guard import UrlGuard
from sanctions_agent.logs import get_logger
from sanctions_agent.pipeline import runs, source_state
from sanctions_agent.review import add_proposal
from sanctions_agent.settings import get_settings
from sanctions_agent.sources.config_models import EnrichmentConfig
from sanctions_agent.sources.registry import SourceSpec

if TYPE_CHECKING:
    from sanctions_agent.pipeline.runner import RunContext

log = get_logger(__name__)
MAX_REVIEW_PROPOSALS_PER_RUN = 50


def build_enricher(src: SourceSpec, fetcher: HttpFetcher) -> Enricher:
    cfg: EnrichmentConfig = src.config  # type: ignore[assignment]
    cls = src.adapter_type.load()
    key = None
    if getattr(cls, "needs_api_key", False):
        s = get_settings()
        secret = s.companies_house_api_key if src.adapter_type.type_id == "companies_house" else None
        if secret is None:
            raise PipelineError(
                ErrorClass.CONFIG_ERROR,
                f"{src.source_id} needs an API key (SANCTIONS_COMPANIES_HOUSE_API_KEY) - not configured",
            )
        key = secret.get_secret_value()
    return cls(cfg, src.source_id, fetcher, api_key=key)  # type: ignore[no-any-return]


def store_result(
    conn: psycopg.Connection[Any],
    enricher: Enricher,
    subject: Subject,
    res: EnrichResult,
    *,
    proposals_left: int,
) -> tuple[int, bool]:
    raw_sha = archive_raw(conn, res.raw)
    prev = fetch_val(
        conn,
        """SELECT enrichment_id FROM enrichment_record WHERE subject_source_id = %s
                              AND subject_source_key = %s AND provider = %s AND superseded_by IS NULL""",
        (subject.source_id, subject.source_key, enricher.provider),
    )
    eid = int(
        fetch_val(
            conn,
            """INSERT INTO enrichment_record (subject_source_id, subject_source_key, subject_entity_type, provider,
               provider_key, match_method, match_score, status, data, source_url, licence, raw_sha256)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING enrichment_id""",
            (
                subject.source_id,
                subject.source_key,
                subject.entity_type,
                enricher.provider,
                res.provider_key,
                res.match_method,
                res.score,
                res.status,
                jsonb(res.data),
                res.source_url,
                enricher.licence,
                raw_sha,
            ),
        )
    )
    if prev:
        conn.execute("UPDATE enrichment_record SET superseded_by = %s WHERE enrichment_id = %s", (eid, prev))
    proposed = False
    if res.status == "NEEDS_REVIEW" and proposals_left > 0:
        cid = add_proposal(
            conn,
            kind="ENRICHMENT_MATCH",
            source_id=subject.source_id,
            subject_ref=f"{subject.source_id}:{subject.source_key}",
            title=f"{enricher.provider}: is '{res.data.get('name') or res.provider_key}' the listed '{subject.name}'?",
            payload={
                "enrichment_id": eid,
                "provider": enricher.provider,
                "provider_key": res.provider_key,
                "score": res.score,
                "method": res.match_method,
                "candidate": res.data,
            },
            proposed_by=f"system:{enricher.provider}",
            evidence_urls=[res.source_url] if res.source_url else [],
            rationale=res.review_note or "name similarity between the auto-accept and review thresholds",
            dedupe_key=f"enrich:{enricher.provider}:{subject.source_id}:{subject.source_key}:{res.provider_key}",
        )
        if cid:
            conn.execute(
                "UPDATE enrichment_record SET proposed_change_id = %s WHERE enrichment_id = %s", (cid, eid)
            )
            proposed = True
    return eid, proposed


def enrichment_batch(ctx: RunContext) -> str:
    src = ctx.source
    cfg: EnrichmentConfig = src.config  # type: ignore[assignment]
    ctx.progress.steps = ["ENRICH"]
    ctx.progress.step("ENRICH")
    a = runs.step_start(ctx.run_id, "ENRICH")
    counts: dict[str, Any] = {
        "subjects": 0,
        "AUTO_ACCEPTED": 0,
        "NEEDS_REVIEW": 0,
        "NO_MATCH": 0,
        "errors": 0,
        "proposals": 0,
    }
    with HttpFetcher(UrlGuard(cfg.allowed_hosts)) as fetcher:
        enricher = build_enricher(src, fetcher)
        with tx() as conn:
            limit = int(ctx.options.get("limit") or cfg.batch_size)
            subjects = enricher.subjects(conn, limit=limit)
        counts["subjects"] = len(subjects)
        ctx.progress.update(force=True, items_total=len(subjects), items_done=0)
        if subjects:
            counts["prepare"] = enricher.prepare(subjects)
        for i, subject in enumerate(subjects, start=1):
            ctx.check_cancel()
            try:
                res = enricher.enrich(subject)
            except FetchError as e:
                counts["errors"] += 1
                log.warning("enrich_failed", subject=subject.source_key, error=str(e))
                if e.error_class in (
                    ErrorClass.HTTP_403,
                    ErrorClass.HOST_NOT_ALLOWED,
                    ErrorClass.TLS_ERROR,
                ) or counts["errors"] > max(10, len(subjects) // 4):
                    raise
                continue
            except (KeyError, ValueError, TypeError, AttributeError) as e:
                # an unexpected response shape for one subject must not sink the batch - but it is counted
                counts["errors"] += 1
                log.warning("enrich_parse_failed", subject=subject.source_key, error=repr(e))
                if counts["errors"] > max(10, len(subjects) // 4):
                    raise PipelineError(
                        ErrorClass.PARSER_ERROR, f"too many enrichment errors, last: {e!r}"
                    ) from e
                continue
            with tx(actor=f"run:{ctx.run_id}") as conn:
                _, proposed = store_result(
                    conn,
                    enricher,
                    subject,
                    res,
                    proposals_left=MAX_REVIEW_PROPOSALS_PER_RUN - counts["proposals"],
                )
            counts[res.status] = counts.get(res.status, 0) + 1
            counts["proposals"] += int(proposed)
            ctx.progress.update(items_done=i)
        counts["api_calls"] = enricher.calls
    with tx() as conn:
        runs.step_finish(conn, ctx.run_id, "ENRICH", a, detail=counts)
        source_state.on_success(
            conn, src.source_id, changed=bool(counts["AUTO_ACCEPTED"] or counts["NEEDS_REVIEW"])
        )
        status = "SUCCEEDED" if counts["subjects"] else "NO_CHANGE"
        runs.finish_run(conn, ctx.run_id, status, summary=counts)
    return status


def enrich_on_demand(
    source_id: str,
    source_key: str,
    provider_source_id: str,
    *,
    requested_by: str,
    reason: str,
    kind: str = "default",
) -> dict[str, Any]:
    """Per-hit enrichment (NFR-07): personal-data lookups happen only here, logged with a reason."""
    from sanctions_agent.sources.registry import get_source

    with tx(actor=requested_by) as conn:
        psrc = get_source(conn, provider_source_id)
        rid = int(
            fetch_val(
                conn,
                """INSERT INTO enrichment_request (requested_by, subject_source_id, subject_source_key,
                provider, reason) VALUES (%s,%s,%s,%s,%s) RETURNING request_id""",
                (requested_by, source_id, source_key, psrc.adapter_type.type_id, reason),
            )
        )
        row = fetch_one(
            conn,
            """SELECT source_id, source_key, record_version_id, entity_type, primary_name, doc
            FROM record_version WHERE source_id = %s AND source_key = %s AND valid_to_seq IS NULL""",
            (source_id, source_key),
        )
    if row is None:
        with tx() as conn:
            conn.execute(
                "UPDATE enrichment_request SET status = 'FAILED', finished_at = now() WHERE request_id = %s",
                (rid,),
            )
        raise KeyError(f"{source_id}:{source_key} is not a current record")
    subject = Subject(
        row["source_id"],
        row["source_key"],
        row["record_version_id"],
        row["entity_type"],
        row["primary_name"] or "",
        row["doc"],
    )
    cfg: EnrichmentConfig = psrc.config  # type: ignore[assignment]
    with HttpFetcher(UrlGuard(cfg.allowed_hosts)) as fetcher:
        enricher = build_enricher(psrc, fetcher)
        fn = getattr(enricher, f"on_demand_{kind}", None) if kind != "default" else enricher.enrich
        if fn is None:
            raise ValueError(f"{psrc.adapter_type.type_id} has no on-demand lookup {kind!r}")
        res = fn(subject)
    with tx(actor=requested_by) as conn:
        eid, _ = store_result(conn, enricher, subject, res, proposals_left=1)
        conn.execute(
            "UPDATE enrichment_request SET status = 'DONE', finished_at = now(), result_enrichment_id = %s"
            " WHERE request_id = %s",
            (eid, rid),
        )
    return {
        "request_id": rid,
        "enrichment_id": eid,
        "status": res.status,
        "provider_key": res.provider_key,
        "data": res.data,
    }
