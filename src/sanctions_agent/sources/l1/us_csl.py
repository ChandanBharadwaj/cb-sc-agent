"""US Consolidated Screening List (trade.gov), JSON bulk file.

Used only for the lists OFAC does not publish itself (BIS Entity List, DPL, UVL, MEU; State ITAR
debarred and nonproliferation lists) - OFAC is ingested directly. The CSL is explicitly non-authoritative
and the export-control lists give name + address only, often with no entity type (BRD 10.1).

Stable key: the CSL ``id``. If CSL ever re-generates ids, the large-change circuit breaker HOLDs the
version instead of publishing a mass remove/add.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sanctions_agent.canonical.model import (
    Address,
    BirthDate,
    BirthPlace,
    CanonicalRecord,
    Country,
    Identifier,
    Listing,
    Name,
    VesselInfo,
)
from sanctions_agent.canonical.normalize.countries import to_iso2
from sanctions_agent.canonical.normalize.dates import parse_birth_date, parse_date
from sanctions_agent.canonical.normalize.identifiers import make_identifier
from sanctions_agent.canonical.normalize.names import clean, join_name
from sanctions_agent.sources.base import FieldDoc, FileInfo, Issue, ListAdapter, ParseStats

_CODE_RE = re.compile(r"\(([A-Z\-]{2,8})\)")
_TYPES = {"individual": "PERSON", "entity": "ORGANIZATION", "vessel": "VESSEL", "aircraft": "AIRCRAFT"}
_MEASURES = {
    "EL": ["LICENSE_REQUIREMENT"],
    "MEU": ["LICENSE_REQUIREMENT"],
    "UVL": ["EXPORT_RESTRICTION"],
    "DPL": ["DENIAL_OF_EXPORT_PRIVILEGES"],
    "DTC": ["DEBARMENT"],
    "ISN": ["PROCUREMENT_BAN"],
}
_AUTHORITY = {"EL": "BIS", "MEU": "BIS", "UVL": "BIS", "DPL": "BIS", "DTC": "STATE", "ISN": "STATE"}


def list_code(source: str | None) -> str | None:
    if not source:
        return None
    m = _CODE_RE.search(source)
    return m.group(1) if m else None


class UsCslAdapter(ListAdapter):
    type_id = "us_csl_json"
    file_kind = "json"
    authority = "US"
    field_docs = [
        FieldDoc("*.source_key", "results[].id", "CSL entry id"),
        FieldDoc("*.name", "results[].name", "Name"),
        FieldDoc("*.alias", "results[].alt_names", "Alternate names (no quality flag)"),
        FieldDoc("*.address", "results[].addresses[]", "Address, city, state, postal code, country (ISO-2)"),
        FieldDoc("*.program", "results[].source (code) / programs", "List code: EL, DPL, UVL, MEU, DTC, ISN"),
        FieldDoc("*.measures", "license_requirement / list code", "Licence requirement, denial, debarment"),
        FieldDoc("*.listed_on", "results[].start_date", "Start date"),
        FieldDoc(
            "*.legal_evidence", "results[].federal_register_notice", "Federal Register citation (FR-14)"
        ),
        FieldDoc("*.entity_type", "results[].type", "Often missing for export-control lists -> UNKNOWN"),
    ]

    def __init__(self, config: Any = None, source_id: str | None = None) -> None:
        super().__init__(config, source_id)
        codes = getattr(config, "include_list_codes", None)
        self.include = set(codes) if codes else {"EL", "DPL", "UVL", "MEU", "DTC", "ISN"}

    @staticmethod
    def _load(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data, {}
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            meta = {k: v for k, v in data.items() if k != "results"}
            return data["results"], meta
        raise ValueError("unexpected CSL JSON shape: no results list")

    def validate_file(self, path: Path) -> list[Issue]:
        try:
            results, _ = self._load(path)
        except (ValueError, json.JSONDecodeError) as e:
            return [Issue("SCHEMA_INVALID", "FAIL", f"CSL JSON invalid: {e}")]
        bad = sum(1 for r in results[:500] if not isinstance(r, dict) or "name" not in r or "source" not in r)
        if results and bad > len(results[:500]) * 0.1:
            return [Issue("SCHEMA_INVALID", "FAIL", f"{bad} of first 500 CSL entries lack name/source")]
        return []

    def read_info(self, path: Path) -> FileInfo:
        _, meta = self._load(path)
        marker = meta.get("search_performed_at") or meta.get("generated_at") or meta.get("last_updated")
        return FileInfo(
            publication_marker=str(marker) if marker else None, extra={"total": meta.get("total")}
        )

    def parse(self, path: Path, stats: ParseStats) -> Iterator[CanonicalRecord]:
        results, _ = self._load(path)
        seen_keys = set(results[0].keys()) if results else set()
        for r in results:
            seen_keys |= set(r.keys()) if isinstance(r, dict) else set()
        for k in sorted(seen_keys):
            stats.paths[f"/results/{k}"] += 1
        for r in results:
            code = list_code(r.get("source"))
            if code is None or code not in self.include:
                continue
            try:
                rec = self._record(r, code, stats)
            except Exception as e:
                stats.skipped_records += 1
                stats.warn("PARSE_WARNING", f"CSL entry skipped: {e!r}")
                continue
            if rec is None:
                stats.skipped_records += 1
                continue
            stats.records += 1
            yield rec

    def _record(self, r: dict[str, Any], code: str, stats: ParseStats) -> CanonicalRecord | None:
        name = clean(r.get("name"))
        if not name:
            stats.warn("MISSING_REQUIRED_FIELD", "CSL entry without name")
            return None
        key = (
            r.get("id")
            or hashlib.sha1(  # noqa: S324 - fallback key, not security
                f"{code}|{name}|{r.get('federal_register_notice')}|{r.get('start_date')}".encode()
            ).hexdigest()
        )
        etype = _TYPES.get((r.get("type") or "").strip().lower(), "UNKNOWN")
        names = [Name(name_type="PRIMARY", full_name=name, quality="STRONG")]
        for alt in r.get("alt_names") or []:
            a = clean(alt)
            if a and a != name:
                names.append(Name(name_type="AKA", full_name=a, quality="UNKNOWN"))
        addresses: list[Address] = []
        for ad in r.get("addresses") or []:
            c_raw = ad.get("country")
            iso = to_iso2(c_raw)
            stats.country(c_raw, iso)
            street, city, state, postal = (
                ad.get("address"),
                ad.get("city"),
                ad.get("state"),
                ad.get("postal_code"),
            )
            if not any((street, city, state, postal, c_raw)):
                continue
            addresses.append(
                Address(
                    street=street,
                    city=city,
                    region=state,
                    postal_code=postal,
                    country_iso2=iso,
                    country_raw=c_raw,
                    full_raw=join_name(street, city, state, postal, c_raw) or None,
                )
            )
        identifiers: list[Identifier] = []
        for i in r.get("ids") or []:
            ident = make_identifier(
                i.get("type"),
                str(i.get("number") or ""),
                country_raw=i.get("country"),
                issued_on=i.get("issue_date"),
                expires_on=i.get("expiration_date"),
            )
            if ident:
                identifiers.append(ident)
        birth_dates: list[BirthDate] = [
            bd for d in r.get("dates_of_birth") or [] if (bd := parse_birth_date(d))
        ]
        birth_places = [BirthPlace(place=p, raw=p) for p in r.get("places_of_birth") or [] if p]
        countries: list[Country] = []
        for kind, fld in (("NATIONALITY", "nationalities"), ("CITIZENSHIP", "citizenships")):
            for c in r.get(fld) or []:
                iso = to_iso2(c)
                stats.country(c, iso)
                countries.append(Country(kind=kind, country_iso2=iso, country_raw=c))  # type: ignore[arg-type]
        vessel = None
        if etype == "VESSEL" or r.get("call_sign") or r.get("vessel_type"):
            vessel = VesselInfo(
                vessel_type=r.get("vessel_type"),
                flag_raw=r.get("vessel_flag"),
                flag_iso2=to_iso2(r.get("vessel_flag")),
                owner_operator_raw=r.get("vessel_owner"),
                tonnage=str(r.get("gross_tonnage") or r.get("gross_registered_tonnage") or "") or None,
            )
            if r.get("call_sign"):
                cs = make_identifier("Call Sign", r["call_sign"], id_type="CALL_SIGN")
                if cs:
                    identifiers.append(cs)
        remarks = (
            join_name(
                f"License requirement: {r['license_requirement']}." if r.get("license_requirement") else None,
                f"License policy: {r['license_policy']}." if r.get("license_policy") else None,
                r.get("remarks"),
            )
            or None
        )
        programs = r.get("programs") or []
        listing = Listing(
            authority=_AUTHORITY.get(code, "US"),
            list_name=r.get("source"),
            program_code=code,
            listed_on=parse_date(r.get("start_date")),
            legal_basis=r.get("federal_register_notice"),
            reference_no=r.get("entity_number") and str(r.get("entity_number")),
            measures=_MEASURES.get(code, ["OTHER"]),
            remarks=remarks,
            evidence_url=r.get("source_information_url") or r.get("source_list_url"),
        )
        return CanonicalRecord(
            source_key=str(key),
            entity_type=etype,
            names=names,
            identifiers=identifiers,  # type: ignore[arg-type]
            addresses=addresses,
            birth_dates=birth_dates,
            birth_places=birth_places,
            countries=countries,
            listings=[listing],
            vessel=vessel,
            titles=[r["title"]] if r.get("title") else [],
            extra={
                "programs": programs,
                "end_date": r.get("end_date"),
                "standard_order": r.get("standard_order"),
            },
        )
