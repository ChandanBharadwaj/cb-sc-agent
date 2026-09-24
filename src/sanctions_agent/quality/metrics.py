"""Data-quality metrics per list version (BRD 10 / FR-07): counts, field fill rates, alias quality,
identifier checksum pass rates, unmapped countries and date precision.

Computed from canonical record docs, so they are identical whether computed during a first run or
after a resume, and they are the numbers behind fill-rate floors, the dashboard and the Q&A agent.
Only aggregates are produced - never record values.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

Doc = dict[str, Any]


def _ids(d: Doc, *types: str) -> list[Doc]:
    return [i for i in d.get("identifiers", []) if i.get("id_type") in types]


def _has_country(d: Doc) -> bool:
    if any(c.get("country_iso2") or c.get("country_raw") for c in d.get("countries", [])):
        return True
    return any(a.get("country_iso2") or a.get("country_raw") for a in d.get("addresses", []))


FIELDS: dict[str, dict[str, Callable[[Doc], bool]]] = {
    "*": {
        "alias": lambda d: any(n.get("name_type") != "PRIMARY" for n in d.get("names", [])),
        "address": lambda d: bool(d.get("addresses")),
        "address_country": lambda d: any(a.get("country_iso2") for a in d.get("addresses", [])),
        "street": lambda d: any(a.get("street") for a in d.get("addresses", [])),
        "listed_on": lambda d: any(lst.get("listed_on") for lst in d.get("listings", [])),
        "program": lambda d: any(lst.get("program_code") for lst in d.get("listings", [])),
        "measures": lambda d: any(lst.get("measures") for lst in d.get("listings", [])),
        "reason": lambda d: any(lst.get("reason") for lst in d.get("listings", [])),
        "legal_evidence": lambda d: any(
            lst.get("evidence_url") or lst.get("legal_basis") for lst in d.get("listings", [])
        ),
    },
    "PERSON": {
        "dob": lambda d: any(b.get("precision") not in (None, "UNKNOWN") for b in d.get("birth_dates", [])),
        "dob_full": lambda d: any(b.get("precision") == "DAY" for b in d.get("birth_dates", [])),
        "birth_place": lambda d: bool(d.get("birth_places")),
        "nationality": lambda d: any(
            c.get("kind") in ("NATIONALITY", "CITIZENSHIP") for c in d.get("countries", [])
        ),
        "passport": lambda d: bool(_ids(d, "PASSPORT")),
        "national_id": lambda d: bool(_ids(d, "NATIONAL_ID")),
        "gender": lambda d: bool(d.get("gender")),
    },
    "ORGANIZATION": {
        "country": _has_country,
        "registration_id": lambda d: bool(_ids(d, "REGISTRATION", "TAX", "LEI", "SWIFT")),
        "lei": lambda d: bool(_ids(d, "LEI")),
        "ownership_link": lambda d: bool(d.get("relationships")),
    },
    "VESSEL": {
        "imo": lambda d: bool(_ids(d, "IMO")),
        "imo_valid": lambda d: any(i.get("checksum_valid") for i in _ids(d, "IMO")),
        "mmsi": lambda d: bool(_ids(d, "MMSI")),
        "call_sign": lambda d: bool(_ids(d, "CALL_SIGN")),
        "flag": lambda d: bool(
            (d.get("vessel") or {}).get("flag_iso2") or (d.get("vessel") or {}).get("flag_raw")
        ),
        "former_flag": lambda d: bool((d.get("vessel") or {}).get("former_flags")),
        "vessel_type": lambda d: bool((d.get("vessel") or {}).get("vessel_type")),
        "build_year": lambda d: bool((d.get("vessel") or {}).get("build_year")),
        "owner_link": lambda d: bool(
            (d.get("vessel") or {}).get("owner_operator_raw") or d.get("relationships")
        ),
    },
    "AIRCRAFT": {
        "msn": lambda d: bool(_ids(d, "AIRCRAFT_MSN")),
        "tail": lambda d: bool(_ids(d, "AIRCRAFT_TAIL")),
        "model": lambda d: bool((d.get("aircraft") or {}).get("model")),
        "operator": lambda d: bool((d.get("aircraft") or {}).get("operator_raw")),
    },
}


@dataclass
class VersionMetrics:
    record_count: int = 0
    counts_by_type: Counter[str] = field(default_factory=Counter)
    fill_counts: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    aliases_total: int = 0
    aliases_weak: int = 0
    aliases_unknown_quality: int = 0
    imo_total: int = 0
    imo_valid: int = 0
    lei_total: int = 0
    lei_valid: int = 0
    unmapped_countries: Counter[str] = field(default_factory=Counter)
    dob_precision: Counter[str] = field(default_factory=Counter)
    metric_errors: int = 0

    def add(self, d: Doc) -> None:
        et = d.get("entity_type", "UNKNOWN")
        self.record_count += 1
        self.counts_by_type[et] += 1
        for group in ("*", et):
            for name, fn in FIELDS.get(group, {}).items():
                try:
                    hit = fn(d)
                except (TypeError, AttributeError, KeyError):  # a malformed doc must not break metrics
                    self.metric_errors += 1
                    continue
                if hit:
                    self.fill_counts[et][name] += 1
        for n in d.get("names", []):
            if n.get("name_type") != "PRIMARY":
                self.aliases_total += 1
                if n.get("quality") == "WEAK":
                    self.aliases_weak += 1
                elif n.get("quality") == "UNKNOWN":
                    self.aliases_unknown_quality += 1
        for i in d.get("identifiers", []):
            if i.get("id_type") == "IMO":
                self.imo_total += 1
                self.imo_valid += 1 if i.get("checksum_valid") else 0
            elif i.get("id_type") == "LEI":
                self.lei_total += 1
                self.lei_valid += 1 if i.get("checksum_valid") else 0
            if i.get("country_raw") and not i.get("country_iso2"):
                self.unmapped_countries[i["country_raw"][:80]] += 1
        for coll in ("addresses", "countries", "birth_places"):
            for c in d.get(coll, []):
                raw = c.get("country_raw")
                if raw and not c.get("country_iso2") and raw.strip().upper() not in ("UNKNOWN", "00"):
                    self.unmapped_countries[raw[:80]] += 1
        if et == "PERSON":
            precisions = [b.get("precision") for b in d.get("birth_dates", [])]
            best = next((p for p in ("DAY", "MONTH", "YEAR", "RANGE", "UNKNOWN") if p in precisions), "NONE")
            self.dob_precision[best] += 1

    def fill_rate(self, entity_type: str, fld: str) -> float | None:
        n = self.counts_by_type.get(entity_type, 0)
        if n == 0:
            return None
        return self.fill_counts[entity_type][fld] / n

    def rows(self) -> list[tuple[str, str, str, float]]:
        """(entity_type, metric, field, value) rows for dq_metric."""
        out: list[tuple[str, str, str, float]] = [("*", "record_count", "", float(self.record_count))]
        for et, n in self.counts_by_type.items():
            out.append((et, "count", "", float(n)))
            for group in ("*", et):
                for fld in FIELDS.get(group, {}):
                    out.append((et, "fill_rate", fld, round(self.fill_counts[et][fld] / n, 4)))
        if self.aliases_total:
            out.append(("*", "weak_alias_share", "", round(self.aliases_weak / self.aliases_total, 4)))
            out.append(
                (
                    "*",
                    "alias_quality_unknown_share",
                    "",
                    round(self.aliases_unknown_quality / self.aliases_total, 4),
                )
            )
        if self.imo_total:
            out.append(("VESSEL", "imo_checksum_pass", "", round(self.imo_valid / self.imo_total, 4)))
        if self.lei_total:
            out.append(("*", "lei_checksum_pass", "", round(self.lei_valid / self.lei_total, 4)))
        out.append(("*", "unmapped_country_values", "", float(sum(self.unmapped_countries.values()))))
        persons = self.counts_by_type.get("PERSON", 0)
        if persons:
            for p, c in self.dob_precision.items():
                out.append(("PERSON", "dob_precision_share", p, round(c / persons, 4)))
        return out


def compute(docs: Iterable[Doc]) -> VersionMetrics:
    m = VersionMetrics()
    for d in docs:
        m.add(d)
    return m


def field_names(entity_type: str) -> list[str]:
    return list(FIELDS["*"]) + list(FIELDS.get(entity_type, {}))
