"""UK Companies House (Open Government Licence): company number, status, type, incorporation, SIC,
registered office. Officers and persons with significant control are personal data - fetched only on
demand for a specific screening hit (NFR-07), never in batch."""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

from sanctions_agent.enrichment.base import Enricher, EnrichResult, Subject, compact, name_score


class CompaniesHouseEnricher(Enricher):
    provider = "companies_house"
    licence = "Open Government Licence v3.0"
    needs_api_key = True

    @property
    def base(self) -> str:
        return self.config.base_url.rstrip("/")

    @property
    def auth(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.api_key}:".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def wants(self, row: dict[str, Any]) -> bool:
        doc = row["doc"]
        countries = {c.get("country_iso2") for c in doc.get("countries", [])} | {
            a.get("country_iso2") for a in doc.get("addresses", [])
        }
        return "GB" in countries or row["source_id"] == "uk_fcdo"

    def _profile(self, number: str) -> tuple[dict[str, Any], bytes]:
        data, raw, _ = self.get_json(f"{self.base}/company/{quote(number)}", headers=self.auth)
        return {
            "company_number": data.get("company_number"),
            "name": data.get("company_name"),
            "status": data.get("company_status"),
            "type": data.get("type"),
            "incorporated_on": data.get("date_of_creation"),
            "dissolved_on": data.get("date_of_cessation"),
            "sic_codes": data.get("sic_codes"),
            "registered_office": data.get("registered_office_address"),
            "jurisdiction": data.get("jurisdiction"),
        }, raw

    def enrich(self, subject: Subject) -> EnrichResult:
        for reg in subject.ids("REGISTRATION"):
            if reg.get("country_iso2") in (None, "GB") and 6 <= len(compact(reg["value"])) <= 8:
                try:
                    prof, raw = self._profile(compact(reg["value"]))
                except Exception:  # noqa: S112 - not a CH number; fall back to name search
                    continue
                if prof.get("company_number"):
                    return EnrichResult(
                        "AUTO_ACCEPTED",
                        "REGISTRATION_NUMBER",
                        prof["company_number"],
                        1.0,
                        prof,
                        f"https://find-and-update.company-information.service.gov.uk/company/{prof['company_number']}",
                        raw,
                    )
        data, raw, _ = self.get_json(
            f"{self.base}/search/companies",
            params={"q": subject.name, "items_per_page": 5},
            headers=self.auth,
        )
        best: tuple[float, dict[str, Any]] | None = None
        for item in (data or {}).get("items") or []:
            score = max(name_score(n, item.get("title") or "") for n in subject.all_names())
            if best is None or score > best[0]:
                best = (score, item)
        if best is None:
            return EnrichResult("NO_MATCH", "NAME", score=0.0, raw=raw)
        score, item = best
        status = self.classify(score)
        if status == "NO_MATCH":
            return EnrichResult(
                "NO_MATCH", "NAME", score=score, data={"best_candidate": item.get("title")}, raw=raw
            )
        prof, praw = self._profile(item["company_number"])
        return EnrichResult(
            status,
            "NAME",
            item["company_number"],
            score,
            prof,
            f"https://find-and-update.company-information.service.gov.uk/company/{item['company_number']}",
            praw,
            review_note="name match only - confirm registered office / incorporation date",
        )

    # ---- on-demand, personal data -----------------------------------------------------------------
    def _accepted_number(self, subject: Subject) -> str:
        res = self.enrich(subject)
        if not res.provider_key or res.status != "AUTO_ACCEPTED":
            raise ValueError("no confidently matched Companies House company for this record")
        return res.provider_key

    def on_demand_psc(self, subject: Subject) -> EnrichResult:
        number = self._accepted_number(subject)
        data, raw, _ = self.get_json(
            f"{self.base}/company/{quote(number)}/persons-with-significant-control", headers=self.auth
        )
        items = [
            {
                "name": i.get("name"),
                "kind": i.get("kind"),
                "natures_of_control": i.get("natures_of_control"),
                "notified_on": i.get("notified_on"),
                "ceased_on": i.get("ceased_on"),
                "country_of_residence": i.get("country_of_residence"),
            }
            for i in (data or {}).get("items") or []
        ]
        return EnrichResult(
            "AUTO_ACCEPTED",
            "ON_DEMAND_PSC",
            number,
            1.0,
            {"company_number": number, "psc": items},
            f"https://find-and-update.company-information.service.gov.uk/company/{number}/persons-with-significant-control",
            raw,
        )

    def on_demand_officers(self, subject: Subject) -> EnrichResult:
        number = self._accepted_number(subject)
        data, raw, _ = self.get_json(f"{self.base}/company/{quote(number)}/officers", headers=self.auth)
        items = [
            {
                "name": i.get("name"),
                "role": i.get("officer_role"),
                "appointed_on": i.get("appointed_on"),
                "resigned_on": i.get("resigned_on"),
                "nationality": i.get("nationality"),
            }
            for i in (data or {}).get("items") or []
        ]
        return EnrichResult(
            "AUTO_ACCEPTED",
            "ON_DEMAND_OFFICERS",
            number,
            1.0,
            {"company_number": number, "officers": items},
            f"https://find-and-update.company-information.service.gov.uk/company/{number}/officers",
            raw,
        )
