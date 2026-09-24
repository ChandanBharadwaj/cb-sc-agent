"""Wikidata (CC0) via SPARQL: vessels by IMO number (P458) and organisations by LEI (P1278).

Only exact identifier joins are used - name matching against Wikidata is too noisy for compliance.
People are never enriched in batch (NFR-07)."""

from __future__ import annotations

from typing import Any

from sanctions_agent.enrichment.base import Enricher, EnrichResult, Subject

VESSEL_Q = """SELECT ?imo ?item ?itemLabel ?flagLabel ?ownerLabel ?operatorLabel ?inception WHERE {
  VALUES ?imo { %s }
  ?item wdt:P458 ?imo .
  OPTIONAL { ?item wdt:P8047 ?flag . } OPTIONAL { ?item wdt:P127 ?owner . }
  OPTIONAL { ?item wdt:P137 ?operator . } OPTIONAL { ?item wdt:P571 ?inception . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } }"""
ORG_Q = """SELECT ?lei ?item ?itemLabel ?countryLabel ?parentLabel ?ownerLabel WHERE {
  VALUES ?lei { %s }
  ?item wdt:P1278 ?lei .
  OPTIONAL { ?item wdt:P17 ?country . } OPTIONAL { ?item wdt:P749 ?parent . } OPTIONAL { ?item wdt:P127 ?owner . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } }"""


def _lit(v: str) -> str:
    return '"' + "".join(ch for ch in v if ch.isalnum()) + '"'


class WikidataEnricher(Enricher):
    provider = "wikidata"
    licence = "CC0 1.0 (Wikidata)"

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.cache: dict[tuple[str, str], dict[str, Any]] = {}

    def _query(self, q: str) -> list[dict[str, Any]]:
        data, _, _ = self.get_json(
            self.config.base_url,
            params={"query": q, "format": "json"},
            headers={"Accept": "application/sparql-results+json"},
        )
        return [
            {k: v.get("value") for k, v in b.items()}
            for b in (data or {}).get("results", {}).get("bindings", [])
        ]

    def _fold(self, key_name: str, key_type: str, rows: list[dict[str, Any]]) -> None:
        for r in rows:
            if not r.get(key_name) or not r.get("item"):
                continue
            k = (key_type, r[key_name])
            cur = self.cache.setdefault(
                k,
                {
                    "qid": r["item"].rsplit("/", 1)[-1],
                    "label": r.get("itemLabel"),
                    "owners": set(),
                    "operators": set(),
                    "parents": set(),
                },
            )
            for fld, col in (
                ("owners", "ownerLabel"),
                ("operators", "operatorLabel"),
                ("parents", "parentLabel"),
            ):
                if r.get(col):
                    cur[fld].add(r[col])
            for fld, col in (("flag", "flagLabel"), ("country", "countryLabel"), ("inception", "inception")):
                if r.get(col):
                    cur[fld] = r[col]

    def prepare(self, subjects: list[Subject]) -> dict[str, Any]:
        imos = sorted({i["value_norm"] for s in subjects for i in s.ids("IMO") if i.get("checksum_valid")})
        leis = sorted({i["value_norm"] for s in subjects for i in s.ids("LEI") if i.get("checksum_valid")})
        for chunk in (imos[i : i + 150] for i in range(0, len(imos), 150)):
            self._fold("imo", "IMO", self._query(VESSEL_Q % " ".join(_lit(x) for x in chunk)))
        for chunk in (leis[i : i + 150] for i in range(0, len(leis), 150)):
            self._fold("lei", "LEI", self._query(ORG_Q % " ".join(_lit(x) for x in chunk)))
        return {"imos_queried": len(imos), "leis_queried": len(leis), "hits": len(self.cache)}

    def enrich(self, subject: Subject) -> EnrichResult:
        for kind in ("IMO", "LEI"):
            for i in subject.ids(kind):
                hit = self.cache.get((kind, i["value_norm"]))
                if hit:
                    data = {k: sorted(v) if isinstance(v, set) else v for k, v in hit.items()}
                    return EnrichResult(
                        "AUTO_ACCEPTED",
                        kind,
                        hit["qid"],
                        1.0,
                        data,
                        f"https://www.wikidata.org/wiki/{hit['qid']}",
                    )
        return EnrichResult("NO_MATCH", "IMO_OR_LEI", score=0.0)
