"""OFAC Recent Actions (HTML). Each action page lists additions / removals / updates with full entries."""

from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import urljoin

from lxml import html as lxml_html

from sanctions_agent.enrichment.notices.base import NoticeFeed, NoticeItem, classify_actions, html_to_text
from sanctions_agent.http.client import HttpFetcher

ACTION_RE = re.compile(r"/recent-actions/(\d{8})(?:_\d+)?/?$")


class OfacRecentActionsFeed(NoticeFeed):
    provider = "ofac_recent_actions"

    def list_items(self, fetcher: HttpFetcher, since: date) -> list[NoticeItem]:
        body, res = fetcher.get(self.config.base_url)
        doc = lxml_html.fromstring(body)
        seen: dict[str, NoticeItem] = {}
        for a in doc.xpath("//a[@href]"):
            href = a.get("href")
            m = ACTION_RE.search(href.split("?")[0])
            if not m:
                continue
            url = urljoin(res.final_url or self.config.base_url, href)
            ext = url.rstrip("/").rsplit("/", 1)[-1]
            try:
                published = datetime.strptime(m.group(1), "%Y%m%d").date()
            except ValueError:
                continue
            if published < since or ext in seen:
                continue
            title = re.sub(r"\s+", " ", a.text_content()).strip() or f"OFAC Recent Action {ext}"
            seen[ext] = NoticeItem(
                external_id=ext,
                title=title,
                url=url,
                published_on=published,
                action_types=classify_actions(title),
            )
        return sorted(seen.values(), key=lambda i: i.external_id, reverse=True)[: self.config.max_items]

    def fetch_text(self, fetcher: HttpFetcher, item: NoticeItem) -> str | None:
        assert item.url
        body, _ = fetcher.get(item.url, max_bytes=10 * 1024 * 1024)
        return html_to_text(body)
