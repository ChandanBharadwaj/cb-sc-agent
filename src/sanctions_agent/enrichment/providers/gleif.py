"""GLEIF (CC0): LEI, legal form, registration authority + number, addresses, status, parents.

Matching ladder (most to least certain):
1. LEI printed on the list            -> exact, auto-accepted
2. registration number + country      -> exact, auto-accepted when a single record matches
3. legal name + country (exact filter, then fuzzy completions) -> scored; auto >= 0.95, review >= 0.85
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from sanctions_agent.enrichment.base import Enricher, EnrichResult, Subject, compact, name_score
from sanctions_agent.http.errors import ErrorClass, FetchError


def _entity(rec: dict[str, Any]) -> dict[str, Any]:
    a = rec.get("attributes", {})
    e = a.get("entity", {})
    return {
        "lei": a.get("lei") or rec.get("id"),
        "name": (e.get("legalName") or {}).get("name"),
        "legal_form": (e.get("legalForm") or {}).get("id"),
        "registered_as": e.get("registeredAs"),
        "registration_authority": (e.get("registeredAt") or {}).get("id"),
        "jurisdiction": e.get("jurisdiction"),
        "status": e.get("status"),
        "registration_status": (a.get("registration") or {}).get("status"),
        "legal_address": e.get("legalAddress"),
        "hq_address": e.get("headquartersAddress"),
        "country": (e.get("legalAddress") or {}).get("country"),
    }


class GleifEnricher(Enricher):
    provider = "gleif"
    licence = "CC0 1.0 (GLEIF)"

    @property
    def base(self) -> str:
        return self.config.base_url.rstrip("/")

    def _parents(self, lei: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for rel in ("direct-parent", "ultimate-parent"):
            try:
                data, _, _ = self.get_json(f"{self.base}/lei-records/{lei}/{rel}")
            except FetchError as e:
                if e.error_class == ErrorClass.HTTP_404:
                    continue  # no parent reported (or a reporting exception)
                raise
            if data and data.get("data"):
                ent = _entity(data["data"])
                out[rel.replace("-", "_")] = {
                    "lei": ent["lei"],
                    "name": ent["name"],
                    "country": ent["country"],
                }
        return out

    def _accept(
        self, rec: dict[str, Any], method: str, score: float, raw: bytes, url: str, status: str | None = None
    ) -> EnrichResult:
        ent = _entity(rec)
        st = status or self.classify(score)
        if st == "AUTO_ACCEPTED" and ent["lei"]:
            ent["parents"] = self._parents(ent["lei"])
        return EnrichResult(
            status=st,
            match_method=method,
            provider_key=ent["lei"],
            score=score,
            data=ent,
            source_url=f"https://search.gleif.org/#/record/{ent['lei']}",
            raw=raw,
        )

    def enrich(self, subject: Subject) -> EnrichResult:
        for lei in subject.ids("LEI"):
            if lei.get("checksum_valid"):
                try:
                    data, raw, url = self.get_json(f"{self.base}/lei-records/{lei['value_norm']}")
                except FetchError as e:
                    if e.error_class != ErrorClass.HTTP_404:
                        raise
                    continue
                return self._accept(data["data"], "LEI", 1.0, raw, url, status="AUTO_ACCEPTED")
        countries = subject.countries()
        for reg in subject.ids("REGISTRATION", "TAX"):
            country = reg.get("country_iso2") or (next(iter(countries)) if len(countries) == 1 else None)
            if not country:
                continue
            data, raw, url = self.get_json(
                f"{self.base}/lei-records",
                params={
                    "filter[entity.registeredAs]": reg["value"],
                    "filter[entity.legalAddress.country]": country,
                    "page[size]": 5,
                },
            )
            hits = (data or {}).get("data") or []
            exact = [h for h in hits if compact(_entity(h)["registered_as"]) == compact(reg["value"])]
            if len(exact) == 1:
                return self._accept(exact[0], "REGISTRATION_NUMBER", 1.0, raw, url, status="AUTO_ACCEPTED")
        # name + country
        best: tuple[float, dict[str, Any], bytes, str] | None = None
        country_list: list[str | None] = [*sorted(countries)] or [None]
        for country in country_list:
            params: dict[str, Any] = {"filter[entity.legalName]": subject.name, "page[size]": 5}
            if country:
                params["filter[entity.legalAddress.country]"] = country
            data, raw, url = self.get_json(f"{self.base}/lei-records", params=params)
            for h in (data or {}).get("data") or []:
                ent = _entity(h)
                score = max(name_score(n, ent["name"] or "") for n in subject.all_names())
                if country and ent["country"] != country:
                    score -= 0.1
                if best is None or score > best[0]:
                    best = (score, h, raw, url)
        if best is None or best[0] < self.config.review_score:
            data, raw, url = self.get_json(
                f"{self.base}/fuzzycompletions", params={"field": "entity.legalName", "q": subject.name}
            )
            leis = [
                ((c.get("relationships") or {}).get("lei-records") or {}).get("data", {}).get("id")
                for c in (data or {}).get("data") or []
            ]
            for lei in [x for x in leis if x][:3]:
                d, r, u = self.get_json(f"{self.base}/lei-records/{quote(lei)}")
                ent = _entity(d["data"])
                score = max(name_score(n, ent["name"] or "") for n in subject.all_names())
                if countries and ent["country"] not in countries:
                    score -= 0.15
                if best is None or score > best[0]:
                    best = (score, d["data"], r, u)
        if best is None:
            return EnrichResult(status="NO_MATCH", match_method="NAME_COUNTRY", score=0.0)
        score, rec, raw, url = best
        res = self._accept(rec, "NAME_COUNTRY", round(max(0.0, score), 4), raw, url)
        if res.status == "NO_MATCH":
            res.provider_key, res.data = None, {"best_candidate": res.data.get("name"), "score": score}
        return res
