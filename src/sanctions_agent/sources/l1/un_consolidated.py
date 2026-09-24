"""UN Security Council Consolidated List (XML).

Stable key: REFERENCE_NUMBER (e.g. ``QDi.001``); DATAID kept as an extra id. Per-record change markers
VERSIONNUM / LAST_DAY_UPDATED are preserved. Alias QUALITY Good/Low -> STRONG/WEAK (FR-11; about 29% of
individual aliases are "Low"). The file does not state measures per entry, so none are invented.
The publisher's declared XSD returns 404 (BRD 8.1), so the structural contract lives here.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from lxml import etree

from sanctions_agent.canonical.model import (
    Address,
    BirthDate,
    BirthPlace,
    CanonicalRecord,
    Country,
    Listing,
    Name,
)
from sanctions_agent.canonical.normalize.countries import to_iso2
from sanctions_agent.canonical.normalize.dates import parse_birth_date, parse_date
from sanctions_agent.canonical.normalize.identifiers import make_identifier
from sanctions_agent.canonical.normalize.names import clean, join_name
from sanctions_agent.sources.base import (
    FieldDoc,
    FileInfo,
    ListAdapter,
    ParseStats,
    iter_records,
    kid_text,
    kids,
    root_info,
    txt,
)

_QUALITY = {"good": "STRONG", "low": "WEAK", "a.k.a.": "UNKNOWN", "f.k.a.": "UNKNOWN"}
_ALIAS_TYPE = {"f.k.a.": "FKA", "a.k.a.": "AKA"}


class UnConsolidatedAdapter(ListAdapter):
    type_id = "un_consolidated_xml"
    authority = "UN"
    field_docs = [
        FieldDoc(
            "*.source_key",
            "INDIVIDUAL|ENTITY/REFERENCE_NUMBER",
            "UN permanent reference number (e.g. QDi.001)",
        ),
        FieldDoc("*.name", "FIRST_NAME..FOURTH_NAME", "Name parts in UN order, joined"),
        FieldDoc(
            "*.alias", "INDIVIDUAL_ALIAS|ENTITY_ALIAS/ALIAS_NAME", "Aliases; QUALITY Good/Low -> STRONG/WEAK"
        ),
        FieldDoc(
            "PERSON.dob",
            "INDIVIDUAL_DATE_OF_BIRTH",
            "EXACT date, YEAR, BETWEEN FROM_YEAR/TO_YEAR, APPROXIMATELY",
        ),
        FieldDoc("PERSON.birth_place", "INDIVIDUAL_PLACE_OF_BIRTH", "City / state / country of birth"),
        FieldDoc("PERSON.nationality", "NATIONALITY/VALUE", "Country names mapped to ISO-2; raw kept"),
        FieldDoc("PERSON.passport", "INDIVIDUAL_DOCUMENT[TYPE_OF_DOCUMENT=Passport]", "Passport numbers"),
        FieldDoc(
            "PERSON.national_id", "INDIVIDUAL_DOCUMENT[TYPE_OF_DOCUMENT=National ...]", "National ID numbers"
        ),
        FieldDoc("*.address", "INDIVIDUAL_ADDRESS|ENTITY_ADDRESS", "Street / city / state / zip / country"),
        FieldDoc("*.listed_on", "LISTED_ON", "Date first listed"),
        FieldDoc("*.program", "UN_LIST_TYPE", "Committee / regime (e.g. Al-Qaida, DPRK)"),
        FieldDoc("*.reason", "COMMENTS1", "Narrative comments (free text)"),
        FieldDoc(
            "*.measures",
            "(none)",
            "Not stated per entry in the UN file",
            notes="measures fill rate is 0 by design",
        ),
    ]

    def read_info(self, path: Path) -> FileInfo:
        root, attrs, _ = root_info(path)
        marker = attrs.get("dateGenerated")
        published = None
        if marker:
            try:
                published = datetime.fromisoformat(marker.replace("Z", "+00:00")).astimezone(UTC)
            except ValueError:
                published = None
        return FileInfo(publication_marker=marker, published_at=published, extra={"root": root})

    def parse(self, path: Path, stats: ParseStats) -> Iterator[CanonicalRecord]:
        for el, _parent in iter_records(str(path), ("INDIVIDUAL", "ENTITY"), stats):
            try:
                rec = self._record(el, stats)
            except Exception as e:  # one bad record must not sink the file; it is counted
                stats.skipped_records += 1
                stats.warn("PARSE_WARNING", f"UN record skipped: {e!r}")
                continue
            if rec is None:
                stats.skipped_records += 1
                continue
            stats.records += 1
            yield rec

    # ------------------------------------------------------------------------------------------
    def _record(self, el: etree._Element, stats: ParseStats) -> CanonicalRecord | None:
        is_person = el.tag.endswith("INDIVIDUAL") if isinstance(el.tag, str) else False
        ref = kid_text(el, "REFERENCE_NUMBER")
        if not ref:
            stats.warn("MISSING_REQUIRED_FIELD", "REFERENCE_NUMBER missing")
            return None
        parts = [kid_text(el, f) for f in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")]
        primary = join_name(*parts)
        if not primary:
            stats.warn("MISSING_REQUIRED_FIELD", f"{ref}: no name")
            return None
        names = [
            Name(
                name_type="PRIMARY",
                full_name=primary,
                quality="STRONG",
                parts={f"name{i + 1}": p for i, p in enumerate(parts) if p},
            )
        ]
        orig = kid_text(el, "NAME_ORIGINAL_SCRIPT")
        if orig:
            names.append(
                Name(name_type="OTHER", full_name=orig, quality="STRONG", raw_quality="original script")
            )
        for a in kids(el, "INDIVIDUAL_ALIAS" if is_person else "ENTITY_ALIAS"):
            an = clean(kid_text(a, "ALIAS_NAME"))
            if not an:
                continue
            q = (kid_text(a, "QUALITY") or "").strip()
            names.append(
                Name(
                    name_type=_ALIAS_TYPE.get(q.lower(), "AKA"),
                    full_name=an,  # type: ignore[arg-type]
                    quality=_QUALITY.get(q.lower(), "UNKNOWN"),
                    raw_quality=q or None,
                )
            )  # type: ignore[arg-type]

        identifiers = [
            i
            for i in [
                make_identifier("UN Reference Number", ref, id_type="UN_REF"),
                make_identifier("UN DATAID", kid_text(el, "DATAID") or "", id_type="OTHER"),
            ]
            if i
        ]
        addresses: list[Address] = []
        for ad in kids(el, "INDIVIDUAL_ADDRESS" if is_person else "ENTITY_ADDRESS"):
            country_raw = kid_text(ad, "COUNTRY")
            iso = to_iso2(country_raw)
            stats.country(country_raw, iso)
            fields = {k: kid_text(ad, k) for k in ("STREET", "CITY", "STATE_PROVINCE", "ZIP_CODE", "NOTE")}
            if not any(fields.values()) and not country_raw:
                continue
            addresses.append(
                Address(
                    street=fields["STREET"],
                    city=fields["CITY"],
                    region=fields["STATE_PROVINCE"],
                    postal_code=fields["ZIP_CODE"],
                    country_iso2=iso,
                    country_raw=country_raw,
                    full_raw=join_name(*fields.values(), country_raw) or None,
                )
            )
        birth_dates: list[BirthDate] = []
        birth_places: list[BirthPlace] = []
        countries: list[Country] = []
        if is_person:
            for d in kids(el, "INDIVIDUAL_DATE_OF_BIRTH"):
                bd = self._dob(d)
                if bd:
                    if bd.precision == "UNKNOWN":
                        stats.unparseable_dates += 1
                    birth_dates.append(bd)
            for p in kids(el, "INDIVIDUAL_PLACE_OF_BIRTH"):
                c_raw = kid_text(p, "COUNTRY")
                iso = to_iso2(c_raw)
                stats.country(c_raw, iso)
                place = join_name(kid_text(p, "CITY"), kid_text(p, "STATE_PROVINCE"))
                if place or c_raw:
                    birth_places.append(
                        BirthPlace(place=place or None, country_iso2=iso, raw=join_name(place, c_raw) or None)
                    )
            for v in self._values(el, "NATIONALITY"):
                iso = to_iso2(v)
                stats.country(v, iso)
                countries.append(Country(kind="NATIONALITY", country_iso2=iso, country_raw=v))
            for doc in kids(el, "INDIVIDUAL_DOCUMENT"):
                number = kid_text(doc, "NUMBER")
                if not number:
                    continue
                label = (
                    join_name(kid_text(doc, "TYPE_OF_DOCUMENT"), kid_text(doc, "TYPE_OF_DOCUMENT2")) or None
                )
                ident = make_identifier(
                    label,
                    number,
                    country_raw=kid_text(doc, "ISSUING_COUNTRY") or kid_text(doc, "COUNTRY_OF_ISSUE"),
                    issued_on=kid_text(doc, "DATE_OF_ISSUE"),
                )
                if ident:
                    stats.country(ident.country_raw, ident.country_iso2)
                    identifiers.append(ident)
        updated = [parse_date(v) for v in self._values(el, "LAST_DAY_UPDATED")]
        updated_dates = [u for u in updated if u]
        listing = Listing(
            authority="UN",
            list_name=(self._values(el, "LIST_TYPE") or ["UN List"])[0],
            program_code=kid_text(el, "UN_LIST_TYPE"),
            listed_on=parse_date(kid_text(el, "LISTED_ON")),
            reference_no=ref,
            measures=[],
            reason=kid_text(el, "COMMENTS1"),
        )
        return CanonicalRecord(
            source_key=ref,
            entity_type="PERSON" if is_person else "ORGANIZATION",
            names=names,
            identifiers=identifiers,
            addresses=addresses,
            birth_dates=birth_dates,
            birth_places=birth_places,
            countries=countries,
            listings=[listing],
            titles=self._values(el, "TITLE"),
            positions=self._values(el, "DESIGNATION"),
            gender=kid_text(el, "GENDER"),
            source_updated_on=max(updated_dates) if updated_dates else None,
            extra={"dataid": kid_text(el, "DATAID"), "versionnum": kid_text(el, "VERSIONNUM")},
        )

    @staticmethod
    def _values(el: etree._Element, name: str) -> list[str]:
        out: list[str] = []
        for c in kids(el, name):
            vals = [txt(v) for v in kids(c, "VALUE")]
            out.extend(v for v in vals if v)
        return out

    @staticmethod
    def _dob(d: etree._Element) -> BirthDate | None:
        kind = (kid_text(d, "TYPE_OF_DATE") or "").upper()
        date_s, year = kid_text(d, "DATE"), kid_text(d, "YEAR")
        fy, ty = kid_text(d, "FROM_YEAR"), kid_text(d, "TO_YEAR")
        if kind == "BETWEEN" and fy and ty and fy.isdigit() and ty.isdigit():
            return BirthDate(
                year_from=int(fy), year_to=int(ty), precision="RANGE", raw=f"between {fy} and {ty}"
            )
        raw = date_s or year
        if not raw:
            return None
        bd = parse_birth_date(raw)
        if bd and kind == "APPROXIMATELY":
            bd.raw = f"approximately {raw}"
        return bd
