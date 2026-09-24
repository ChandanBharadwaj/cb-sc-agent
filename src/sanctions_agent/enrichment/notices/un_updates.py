"""UN SC Consolidated List - "List updates" log (HTML). Entries name reference numbers and the action
(Listing / Amendment / De-listing / Re-listing), so removals can be matched by UN reference number."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime

from sanctions_agent.enrichment.notices.base import (
    UN_REF_RE,
    NoticeFeed,
    NoticeItem,
    classify_actions,
    html_to_text,
)
from sanctions_agent.http.client import HttpFetcher

DATE_RE = re.compile(
    r"\b(\d{1,2} (?:January|February|March|April|May|June|July|August|September|October|November|"
    r"December) \d{4})\b"
)


class UnListUpdatesFeed(NoticeFeed):
    provider = "un_list_updates"

    def list_items(self, fetcher: HttpFetcher, since: date) -> list[NoticeItem]:
        body, res = fetcher.get(self.config.base_url)
        text = html_to_text(body)
        # split the log into dated blocks
        parts = DATE_RE.split(text)
        items: list[NoticeItem] = []
        for i in range(1, len(parts) - 1, 2):
            try:
                d = datetime.strptime(parts[i], "%d %B %Y").date()
            except ValueError:
                continue
            block = parts[i + 1].strip()
            if d < since or not UN_REF_RE.search(block):
                continue
            digest = hashlib.sha1(block.encode()).hexdigest()[:10]  # noqa: S324 - id, not security
            first_line = block.split("\n", 1)[0][:200]
            items.append(
                NoticeItem(
                    external_id=f"{d.isoformat()}-{digest}",
                    title=f"UN list update {parts[i]}: {first_line}",
                    url=res.final_url,
                    published_on=d,
                    text=block[:20000],
                    action_types=classify_actions(block),
                )
            )
        return items[: self.config.max_items]
