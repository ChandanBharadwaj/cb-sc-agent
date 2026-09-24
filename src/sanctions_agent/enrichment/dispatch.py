"""Level-2 run execution: NOTICE_SYNC (official notices) and ENRICHMENT_BATCH (free enrichment)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING

from sanctions_agent.db.engine import fetch_val, jsonb, tx
from sanctions_agent.enrichment.notices.base import (
    NoticeFeed,
    match_notice,
    raise_signals,
    recent_cutoff,
    store_notice,
)
from sanctions_agent.http.client import HttpFetcher, retry_call
from sanctions_agent.http.errors import ErrorClass, FetchError, PipelineError
from sanctions_agent.http.guard import UrlGuard
from sanctions_agent.logs import get_logger
from sanctions_agent.pipeline import runs, source_state
from sanctions_agent.sources.config_models import NoticeFeedConfig

if TYPE_CHECKING:
    from sanctions_agent.pipeline.runner import RunContext

log = get_logger(__name__)
SIGNAL_MAX_AGE = timedelta(days=2)


def run_level2(ctx: RunContext) -> str:
    kind = ctx.run["run_kind"]
    if kind == "NOTICE_SYNC":
        return notice_sync(ctx)
    if kind == "ENRICHMENT_BATCH":
        from sanctions_agent.enrichment.batch import enrichment_batch

        return enrichment_batch(ctx)
    raise PipelineError(ErrorClass.CONFIG_ERROR, f"no Level-2 handler for run kind {kind}")


def _sleep(ctx: RunContext) -> object:
    return getattr(ctx, "sleep", None) or time.sleep


def notice_sync(ctx: RunContext) -> str:
    src = ctx.source
    cfg: NoticeFeedConfig = src.config  # type: ignore[assignment]
    feed: NoticeFeed = src.adapter_type.load()(cfg, src.source_id)
    ctx.progress.steps = ["SYNC"]
    ctx.progress.step("SYNC")
    a = runs.step_start(ctx.run_id, "SYNC")
    since = recent_cutoff(cfg)
    counts = {"items": 0, "new": 0, "signals": 0, "id_links": 0, "name_links": 0, "text_failures": 0}
    with HttpFetcher(UrlGuard(cfg.allowed_hosts)) as fetcher:
        started = time.monotonic()
        try:
            items = retry_call(lambda: feed.list_items(fetcher, since), feed.retry)
        except FetchError as e:
            _evidence(ctx, cfg.base_url, None, e, started)
            raise
        _evidence(ctx, cfg.base_url, 200, None, started)
        counts["items"] = len(items)
        ctx.progress.update(force=True, items_total=len(items), items_done=0)
        now = datetime.now(UTC).date()
        for i, item in enumerate(items, start=1):
            ctx.check_cancel()
            with tx() as conn:
                known = fetch_val(
                    conn,
                    "SELECT 1 FROM legal_notice WHERE provider = %s AND external_id = %s",
                    (src.source_id, item.external_id),
                )
            if known:
                continue
            try:
                item.text = retry_call(partial(feed.fetch_text, fetcher, item), feed.retry) or item.text
            except FetchError as e:
                counts["text_failures"] += 1
                log.warning("notice_text_failed", notice=item.external_id, error=str(e))
            with tx(actor=f"run:{ctx.run_id}") as conn:
                nid = store_notice(conn, src.source_id, item, cfg.signal_for)
                if nid is None:
                    continue
                counts["new"] += 1
                if item.published_on is None or now - item.published_on <= SIGNAL_MAX_AGE:
                    counts["signals"] += raise_signals(conn, src.source_id, item, cfg.signal_for)
                m = match_notice(conn, nid, [s for s in cfg.signal_for])
                counts["id_links"] += m["ID_MATCH"]
                counts["name_links"] += m["NAME_MATCH"]
            ctx.progress.update(items_done=i)
    with tx() as conn:
        runs.step_finish(conn, ctx.run_id, "SYNC", a, detail=counts)
        source_state.on_success(conn, src.source_id, changed=counts["new"] > 0, fetched=True)
        status = "SUCCEEDED" if counts["new"] else "NO_CHANGE"
        runs.finish_run(conn, ctx.run_id, status, summary=counts)
    return status


def _evidence(ctx: RunContext, url: str, status: int | None, err: FetchError | None, started: float) -> None:
    with tx() as conn:
        conn.execute(
            """INSERT INTO fetch_evidence (run_id, source_id, attempt, requested_url, final_url, http_status, redirect_chain,
                   response_headers, duration_ms, error_class, error_detail)
               VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                ctx.run_id,
                ctx.source.source_id,
                url,
                url,
                status or (err.http_status if err else None),
                jsonb([]),
                jsonb({}),
                int((time.monotonic() - started) * 1000),
                err.error_class.value if err else None,
                err.detail[:2000] if err else None,
            ),
        )
