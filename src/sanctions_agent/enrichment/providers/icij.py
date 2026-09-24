"""ICIJ Offshore Leaks (ODbL / CC BY-SA, share-alike): offshore entities as *leads* only.

Disabled by default until Legal signs off on the licence (NFR-06). Matches are never auto-accepted:
an exact core-name match produces a NEEDS_REVIEW lead for an analyst."""

from __future__ import annotations

import csv
import io
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from sanctions_agent.enrichment.base import Enricher, EnrichResult, Subject, core_name
from sanctions_agent.http.client import retry_call


class IcijEnricher(Enricher):
    provider = "icij"
    licence = "ODbL 1.0 / CC BY-SA (share-alike) - leads only"

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.hits: dict[str, list[dict[str, Any]]] = {}

    def prepare(self, subjects: list[Subject]) -> dict[str, Any]:
        wanted = {core_name(n) for s in subjects for n in s.all_names() if len(core_name(n)) >= 6}
        url = self.config.download_url or ""
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "oldb.zip"
            retry_call(
                lambda: self.fetcher.fetch_to_file(url, dest, max_bytes=2048 * 1024 * 1024), self.retry
            )
            with zipfile.ZipFile(dest) as zf:
                member = next((n for n in zf.namelist() if n.endswith("nodes-entities.csv")), None)
                if member is None:
                    return {"error": "nodes-entities.csv not found"}
                with zf.open(member) as fh:
                    for r in csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8", errors="replace")):
                        key = core_name(r.get("name") or "")
                        if key in wanted:
                            self.hits.setdefault(key, []).append(
                                {
                                    "node_id": r.get("node_id"),
                                    "name": r.get("name"),
                                    "jurisdiction": r.get("jurisdiction_description"),
                                    "countries": r.get("countries"),
                                    "source": r.get("sourceID"),
                                    "status": r.get("status"),
                                }
                            )
        return {"candidate_names": len(wanted), "hits": len(self.hits)}

    def enrich(self, subject: Subject) -> EnrichResult:
        for n in subject.all_names():
            found = self.hits.get(core_name(n))
            if found:
                lead = found[0]
                return EnrichResult(
                    "NEEDS_REVIEW",
                    "EXACT_CORE_NAME",
                    lead["node_id"],
                    0.9,
                    {**lead, "other_candidates": len(found) - 1},
                    f"https://offshoreleaks.icij.org/nodes/{lead['node_id']}",
                    review_note="ICIJ lead (name only, share-alike licence) - analyst confirmation needed",
                )
        return EnrichResult("NO_MATCH", "EXACT_CORE_NAME", score=0.0)
