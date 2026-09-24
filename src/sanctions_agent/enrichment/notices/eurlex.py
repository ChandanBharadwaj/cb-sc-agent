"""EUR-Lex Official Journal (L series) RSS: detects new sanctions acts (restrictive measures, Reg. 833/2014,
Reg. 269/2014 ...). The act text is archived so annex entries can be extracted and reviewed (Annex XLII/IV)."""

from __future__ import annotations

from datetime import date

from sanctions_agent.enrichment.notices.base import NoticeFeed, NoticeItem, classify_actions, html_to_text
from sanctions_agent.enrichment.notices.rss import item_date, parse_feed
from sanctions_agent.http.client import HttpFetcher


class EurLexOjFeed(NoticeFeed):
    provider = "eurlex_oj"

    def list_items(self, fetcher: HttpFetcher, since: date) -> list[NoticeItem]:
        body, _ = fetcher.get(self.config.base_url)
        out = []
        for rec in parse_feed(body):
            d = item_date(rec)
            if d and d < since:
                continue
            title = str(rec.get("title") or "")
            desc = html_to_text(str(rec["description"])) if rec.get("description") else ""
            item = NoticeItem(
                external_id=str(rec.get("guid") or rec.get("link") or title),
                title=title,
                url=str(rec.get("link") or "") or None,
                published_on=d,
                text=desc or None,
                action_types=classify_actions(f"{title}\n{desc}"),
            )
            if self.keep(item):
                out.append(item)
        return out[: self.config.max_items]

    def fetch_text(self, fetcher: HttpFetcher, item: NoticeItem) -> str | None:
        if not item.url:
            return item.text
        body, _ = fetcher.get(item.url, max_bytes=30 * 1024 * 1024)
        return html_to_text(body)
