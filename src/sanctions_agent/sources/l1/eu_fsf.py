"""EU Financial Sanctions File, XML v1.1.

Owned by DG FISMA. Holds asset freezes only (Annex XLII vessels and Annex IV entities are *not* in
it - see ``eu_annex``). Stable key: sanctionEntity/@logicalId; EU reference number and UN id kept as
identifiers so cross-list links work. Every nameAlias carries ``strong`` (FR-11). Regulations carry
an OJ publication URL, which links the record to its legal evidence deterministically (FR-14).
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
    Identifier,
    IdType,
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
    kid,
    kid_text,
    kids,
    root_info,
)

_TYPES = {"person": "PERSON", "enterprise": "ORGANIZATION", "vessel": "VESSEL", "aircraft": "AIRCRAFT"}
_ID_TYPES: dict[str, IdType] = {
    "passport": "PASSPORT",
    "id": "NATIONAL_ID",
    "fiscalcode": "TAX",
    "regnumber": "REGISTRATION",
    "swiftbic": "SWIFT",
    "imo": "IMO",
    "other": "OTHER",
    "ssn": "NATIONAL_ID",
    "birthcert": "OTHER",
    "drivinglicence": "OTHER",
    "tradelicence": "REGISTRATION",
    "travel": "PASSPORT",
}


def _country(el: etree._Element, stats: ParseStats) -> tuple[str | None, str | None]:
    code = (el.get("countryIso2Code") or "").strip().upper()
    desc = (el.get("countryDescription") or "").strip() or None
    if code and code != "00" and len(code) == 2:
        return code, desc
    iso = to_iso2(desc) if desc and desc.upper() != "UNKNOWN" else None
    if desc and desc.upper() != "UNKNOWN":
        stats.country(desc, iso)
    return iso, desc


class EuFsfAdapter(ListAdapter):
    type_id = "eu_fsf_xml"
    authority = "EU"
    field_docs = [
        FieldDoc("*.source_key", "sanctionEntity/@logicalId", "EU logical id; @euReferenceNumber kept"),
        FieldDoc("*.name", "nameAlias/@wholeName", "Names; first/middle/last parts kept"),
        FieldDoc("*.alias", "nameAlias/@strong", "strong='false' -> WEAK"),
        FieldDoc(
            "PERSON.dob",
            "birthdate/@birthdate|@year|@monthOfYear|@dayOfMonth",
            "Full or partial dates, circa",
        ),
        FieldDoc("PERSON.birth_place", "birthdate/@city|@place|@countryIso2Code", "Place of birth"),
        FieldDoc("PERSON.nationality", "citizenship/@countryIso2Code", "Citizenship"),
        FieldDoc(
            "PERSON.passport", "identification[@identificationTypeCode='passport']/@number", "Passports"
        ),
        FieldDoc(
            "PERSON.national_id", "identification[@identificationTypeCode='id']/@number", "National IDs"
        ),
        FieldDoc(
            "ORGANIZATION.registration_id", "identification[regnumber|fiscalcode|swiftbic]", "Company ids"
        ),
        FieldDoc("*.address", "address/@street|@city|@zipCode|@countryIso2Code", "Addresses"),
        FieldDoc("*.program", "regulation/@programme", "Programme code (e.g. RUS, IRQ, TAQA)"),
        FieldDoc(
            "*.listed_on", "regulation/@publicationDate (earliest)", "Publication date of the first act"
        ),
        FieldDoc(
            "*.measures",
            "(implicit)",
            "The FSF is the asset-freeze list",
            notes="ASSET_FREEZE for every entry",
        ),
        FieldDoc("*.legal_evidence", "regulation/publicationUrl", "Official Journal link (FR-14)"),
    ]

    def read_info(self, path: Path) -> FileInfo:
        root, attrs, _ = root_info(path)
        marker = attrs.get("generationDate")
        published = None
        if marker:
            try:
                published = datetime.fromisoformat(marker.replace("Z", "+00:00")).astimezone(UTC)
            except ValueError:
                published = None
        return FileInfo(
            publication_marker=marker,
            published_at=published,
            extra={"root": root, "globalFileId": attrs.get("globalFileId")},
        )

    def parse(self, path: Path, stats: ParseStats) -> Iterator[CanonicalRecord]:
        for el, _ in iter_records(str(path), ("sanctionEntity",), stats):
            try:
                rec = self._record(el, stats)
            except Exception as e:
                stats.skipped_records += 1
                stats.warn("PARSE_WARNING", f"EU entity {el.get('logicalId')} skipped: {e!r}")
                continue
            if rec is None:
                stats.skipped_records += 1
                continue
            stats.records += 1
            yield rec

    def _record(self, el: etree._Element, stats: ParseStats) -> CanonicalRecord | None:
        lid = el.get("logicalId")
        if not lid:
            stats.warn("MISSING_REQUIRED_FIELD", "sanctionEntity without logicalId")
            return None
        st = kid(el, "subjectType")
        etype = _TYPES.get((st.get("code") if st is not None else "") or "", "UNKNOWN")
        names: list[Name] = []
        for na in kids(el, "nameAlias"):
            whole = clean(na.get("wholeName")) or join_name(
                na.get("firstName"), na.get("middleName"), na.get("lastName")
            )
            if not whole:
                continue
            strong = (na.get("strong") or "").lower()
            names.append(
                Name(
                    name_type="PRIMARY" if not names else "AKA",
                    full_name=whole,
                    parts={
                        k: v
                        for k, v in (
                            ("given", na.get("firstName")),
                            ("middle", na.get("middleName")),
                            ("family", na.get("lastName")),
                            ("title", na.get("title")),
                            ("function", na.get("function")),
                        )
                        if v
                    },
                    language=na.get("nameLanguage") or None,
                    quality="STRONG" if strong == "true" else ("WEAK" if strong == "false" else "UNKNOWN"),
                    raw_quality=f"strong={strong}" if strong else None,
                )
            )
        if not names:
            stats.warn("MISSING_REQUIRED_FIELD", f"EU {lid}: no names")
            return None

        identifiers: list[Identifier] = []
        for label, value, t in (
            ("EU reference number", el.get("euReferenceNumber"), "EU_REF"),
            ("UN id", el.get("unitedNationId"), "UN_REF"),
        ):
            if value:
                i = make_identifier(label, value, id_type=t)  # type: ignore[arg-type]
                if i:
                    identifiers.append(i)
        for idn in kids(el, "identification"):
            number = idn.get("number") or idn.get("latinNumber")
            if not number:
                continue
            code = (idn.get("identificationTypeCode") or "").lower()
            iso, raw = _country(idn, stats)
            i = make_identifier(
                idn.get("identificationTypeDescription") or code,
                number,
                id_type=_ID_TYPES.get(code),
                country_raw=raw,
            )
            if i:
                i.country_iso2 = iso or i.country_iso2
                if idn.get("knownFalse") == "true" or idn.get("knownExpired") == "true":
                    i.label = f"{i.label} (flagged: knownFalse={idn.get('knownFalse')}, knownExpired={idn.get('knownExpired')})"
                identifiers.append(i)

        addresses: list[Address] = []
        for ad in kids(el, "address"):
            iso, raw = _country(ad, stats)
            street, city, zipc = ad.get("street") or None, ad.get("city") or None, ad.get("zipCode") or None
            region = join_name(ad.get("region"), ad.get("place")) or None
            if not any((street, city, zipc, region, raw, iso)):
                continue
            addresses.append(
                Address(
                    street=street,
                    city=city,
                    region=region,
                    postal_code=zipc,
                    country_iso2=iso,
                    country_raw=raw,
                    full_raw=join_name(street, city, zipc, region, raw) or None,
                )
            )
        birth_dates: list[BirthDate] = []
        birth_places: list[BirthPlace] = []
        for b in kids(el, "birthdate"):
            bd: BirthDate | None = None
            if b.get("birthdate"):
                bd = parse_birth_date(b.get("birthdate"))
            elif b.get("year"):
                y, m, d = b.get("year"), b.get("monthOfYear"), b.get("dayOfMonth")
                raw = f"{y}-{int(m):02d}-{int(d):02d}" if (m and d) else (f"{y}-{int(m):02d}" if m else y)
                bd = parse_birth_date(raw)
            elif b.get("yearRangeFrom") and b.get("yearRangeTo"):
                bd = BirthDate(
                    year_from=int(b.get("yearRangeFrom")),
                    year_to=int(b.get("yearRangeTo")),  # type: ignore[arg-type]
                    precision="RANGE",
                    raw=f"{b.get('yearRangeFrom')}-{b.get('yearRangeTo')}",
                )
            if bd:
                if b.get("circa") == "true" and bd.raw:
                    bd.raw = "circa " + bd.raw
                if bd.precision == "UNKNOWN":
                    stats.unparseable_dates += 1
                birth_dates.append(bd)
            iso, raw = _country(b, stats)
            place = join_name(b.get("city"), b.get("place"), b.get("region")) or None
            if place or raw:
                birth_places.append(
                    BirthPlace(place=place, country_iso2=iso, raw=join_name(place, raw) or None)
                )
        countries: list[Country] = []
        for c in kids(el, "citizenship"):
            iso, raw = _country(c, stats)
            if iso or raw:
                countries.append(Country(kind="CITIZENSHIP", country_iso2=iso, country_raw=raw))

        listings: list[Listing] = []
        regs = list(kids(el, "regulation"))
        dates = [d for d in (parse_date(r.get("publicationDate")) for r in regs) if d]
        programmes = sorted({r.get("programme") for r in regs if r.get("programme")})
        first_reg = min(regs, key=lambda r: r.get("publicationDate") or "9999") if regs else None
        for prog in programmes or [None]:
            listings.append(
                Listing(
                    authority="EU",
                    list_name="EU Financial Sanctions File",
                    program_code=prog,
                    listed_on=min(dates) if dates else None,
                    legal_basis=first_reg.get("numberTitle") if first_reg is not None else None,
                    reference_no=el.get("euReferenceNumber"),
                    measures=["ASSET_FREEZE"],
                    reason=clean(el.get("designationDetails")) or None,
                    remarks=clean(kid_text(el, "remark")),
                    evidence_url=kid_text(first_reg, "publicationUrl") if first_reg is not None else None,
                )
            )
        gender = next((na.get("gender") for na in kids(el, "nameAlias") if na.get("gender")), None)
        return CanonicalRecord(
            source_key=lid,
            entity_type=etype,
            names=names,
            identifiers=identifiers,  # type: ignore[arg-type]
            addresses=addresses,
            birth_dates=birth_dates,
            birth_places=birth_places,
            countries=countries,
            listings=listings,
            gender=gender,
            extra={
                "regulations": [
                    {
                        "numberTitle": r.get("numberTitle"),
                        "publicationDate": r.get("publicationDate"),
                        "programme": r.get("programme"),
                        "url": kid_text(r, "publicationUrl"),
                    }
                    for r in regs
                ]
            },
        )
