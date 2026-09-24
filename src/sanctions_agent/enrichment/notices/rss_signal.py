"""Generic RSS feed used purely as an early-pull signal (e.g. the EU FSF RSS announces new files)."""

from __future__ import annotations

from datetime import date

from sanctions_agent.enrichment.notices.base import NoticeFeed, NoticeItem
from sanctions_agent.enrichment.notices.rss import item_date, parse_feed
from sanctions_agent.http.client import HttpFetcher


class RssSignalFeed(NoticeFeed):
    provider = "rss_signal"

    def list_items(self, fetcher: HttpFetcher, since: date) -> list[NoticeItem]:
        body, _ = fetcher.get(self.config.base_url)
        out = []
        for rec in parse_feed(body):
            d = item_date(rec)
            if d and d < since:
                continue
            out.append(
                NoticeItem(
                    external_id=str(rec.get("guid") or rec.get("link") or rec.get("title")),
                    title=str(rec.get("title") or "feed item"),
                    url=str(rec.get("link") or "") or None,
                    published_on=d,
                    action_types=[],
                )
            )
        return out[: self.config.max_items]

    def fetch_text(self, fetcher: HttpFetcher, item: NoticeItem) -> str | None:
        return None
