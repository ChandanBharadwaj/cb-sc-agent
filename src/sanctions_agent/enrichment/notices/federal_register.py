"""Federal Register API (no key): OFAC and BIS documents - the legal trail for US listings/removals."""

from __future__ import annotations

import json
from datetime import date

from sanctions_agent.canonical.normalize.dates import parse_date
from sanctions_agent.enrichment.notices.base import NoticeFeed, NoticeItem, classify_actions
from sanctions_agent.http.client import HttpFetcher

FIELDS = [
    "document_number",
    "title",
    "publication_date",
    "html_url",
    "raw_text_url",
    "abstract",
    "type",
    "agencies",
]


class FederalRegisterFeed(NoticeFeed):
    provider = "federal_register"

    def list_items(self, fetcher: HttpFetcher, since: date) -> list[NoticeItem]:
        params: dict[str, object] = {
            **self.config.params,
            "conditions[publication_date][gte]": since.isoformat(),
            "per_page": min(self.config.max_items, 100),
            "order": "newest",
            "fields[]": FIELDS,
        }
        body, _ = fetcher.get(self.config.base_url, params=params)
        data = json.loads(body)
        out = []
        for r in data.get("results", []):
            title = r.get("title") or ""
            out.append(
                NoticeItem(
                    external_id=r["document_number"],
                    title=title,
                    url=r.get("html_url"),
                    published_on=parse_date(r.get("publication_date")),
                    text=r.get("abstract"),
                    action_types=classify_actions(f"{title}\n{r.get('abstract') or ''}"),
                    extra={
                        "raw_text_url": r.get("raw_text_url"),
                        "type": r.get("type"),
                        "agencies": [a.get("slug") for a in r.get("agencies") or [] if isinstance(a, dict)],
                    },
                )
            )
        return out

    def fetch_text(self, fetcher: HttpFetcher, item: NoticeItem) -> str | None:
        url = item.extra.get("raw_text_url")
        if not url:
            return item.text
        body, _ = fetcher.get(url, max_bytes=20 * 1024 * 1024)
        from sanctions_agent.enrichment.notices.base import html_to_text

        text = body.decode("utf-8", "replace")
        return html_to_text(text) if "<html" in text[:500].lower() or "<pre" in text[:500].lower() else text
