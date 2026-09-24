"""gov.uk search + content APIs: FCDO / OFSI financial sanctions notices."""

from __future__ import annotations

import json
from datetime import date

from sanctions_agent.canonical.normalize.dates import parse_date
from sanctions_agent.enrichment.notices.base import NoticeFeed, NoticeItem, classify_actions, html_to_text
from sanctions_agent.http.client import HttpFetcher


class UkNoticesFeed(NoticeFeed):
    provider = "uk_notices"

    def list_items(self, fetcher: HttpFetcher, since: date) -> list[NoticeItem]:
        body, _ = fetcher.get(self.config.base_url, params=dict(self.config.params))
        data = json.loads(body)
        out = []
        for r in data.get("results", []):
            published = parse_date((r.get("public_timestamp") or "")[:10])
            if published and published < since:
                continue
            title = r.get("title") or ""
            if "sanction" not in f"{title} {r.get('description') or ''}".lower():
                continue
            link = r.get("link") or ""
            out.append(
                NoticeItem(
                    external_id=link or title,
                    title=title,
                    url=f"https://www.gov.uk{link}" if link else None,
                    published_on=published,
                    text=r.get("description"),
                    action_types=classify_actions(f"{title}\n{r.get('description') or ''}"),
                    extra={"link": link},
                )
            )
        return out[: self.config.max_items]

    def fetch_text(self, fetcher: HttpFetcher, item: NoticeItem) -> str | None:
        link = item.extra.get("link")
        if not link:
            return item.text
        body, _ = fetcher.get(f"https://www.gov.uk/api/content{link}")
        data = json.loads(body)
        details = data.get("details") or {}
        html = details.get("body") or ""
        if not html:
            for doc in details.get("documents") or []:
                html += str(doc)
        return (item.text or "") + "\n\n" + (html_to_text(html) if html else "")
