"""UK Sanctions List (FCDO) XML.

Since 28 January 2026 the FCDO list is the only UK list (the OFSI Consolidated List is closed - a
pipeline still reading it sees frozen data that looks like "no changes"). Stable key: UniqueID
(legacy OFSIGroupID kept as an extra id; the move between them is a known cause of false removals).

The FCDO changed its file format in December 2025 and publishes a versioned XSD. Element lookups
accept known alternative names, and unknown elements are reported as schema drift rather than
silently ignored. Run ``sanctions-agent verify-sources`` against a live file before go-live.
"""

from __future__ import annotations

import re
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
    Listing,
    Name,
    Relationship,
    VesselInfo,
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
    descendants,
    iter_records,
    kid,
    kid_text,
    kids,
    root_info,
    txt,
)

_MEASURE_FLAGS = {
    "assetfreeze": "ASSET_FREEZE",
    "travelban": "TRAVEL_BAN",
    "armsembargo": "ARMS_EMBARGO",
    "targetedarmsembargo": "ARMS_EMBARGO",
    "charteringofships": "CHARTERING_BAN",
    "prohibitionofportentry": "PORT_BAN",
    "trustservicessanctions": "TRUST_SERVICES_BAN",
    "directordisqualificationsanction": "DIRECTOR_DISQUALIFICATION",
    "deflag": "PORT_BAN",
    "crewservicingofshipsandaircraft": "OTHER",
    "preventionofbusinessarrangements": "OTHER",
    "closureofrepresentativeoffices": "OTHER",
    "preventionofcharteringofships": "CHARTERING_BAN",
}
_MEASURE_TEXT = [
    ("asset freeze", "ASSET_FREEZE"),
    ("travel ban", "TRAVEL_BAN"),
    ("arms embargo", "ARMS_EMBARGO"),
    ("port", "PORT_BAN"),
    ("chartering", "CHARTERING_BAN"),
    ("trust services", "TRUST_SERVICES_BAN"),
    ("director disqualification", "DIRECTOR_DISQUALIFICATION"),
]
_QUALITY = {"good quality": "STRONG", "good": "STRONG", "low quality": "WEAK", "low": "WEAK"}
_TYPES = {"individual": "PERSON", "entity": "ORGANIZATION", "ship": "VESSEL", "vessel": "VESSEL"}


def _texts(el: etree._Element | None, container: str, item: str) -> list[str]:
    if el is None:
        return []
    c = kid(el, container)
    if c is None:
        return []
    return [t for t in (txt(i) for i in kids(c, item)) if t]


class UkFcdoAdapter(ListAdapter):
    type_id = "uk_fcdo_xml"
    authority = "UK"
    field_docs = [
        FieldDoc("*.source_key", "Designation/UniqueID", "FCDO unique id (legacy OFSIGroupID kept)"),
        FieldDoc(
            "*.name", "Names/Name[NameType=Primary Name] Name1..Name6", "Name6 is the family / entity name"
        ),
        FieldDoc("*.alias", "Names/Name[NameType=Alias]", "AliasStrength Good/Low quality -> STRONG/WEAK"),
        FieldDoc("PERSON.dob", "IndividualDetails/Individual/DOBs/DOB", "dd/mm/yyyy with dd/mm placeholders"),
        FieldDoc("PERSON.birth_place", "BirthDetails/Location", "Town and country of birth"),
        FieldDoc("PERSON.nationality", "Nationalities/Nationality", "Country names"),
        FieldDoc("PERSON.passport", "PassportDetails/PassportDetail/PassportNumber", "Passport numbers"),
        FieldDoc(
            "PERSON.national_id", "NationalIdentifierDetails/.../NationalIdentifierNumber", "National IDs"
        ),
        FieldDoc(
            "ORGANIZATION.registration_id",
            "EntityDetails/.../BusinessRegistrationNumber",
            "Registration numbers",
        ),
        FieldDoc("VESSEL.imo", "ShipDetails/Ship/IMONumbers/IMONumber", "IMO (97% filled in 2022 sample)"),
        FieldDoc(
            "VESSEL.flag", "CurrentBelievedFlagOfShips", "Current flag; PreviousFlags kept as former flags"
        ),
        FieldDoc("VESSEL.owner_link", "CurrentOwnerOperators", "Owner / operator names"),
        FieldDoc("*.address", "Addresses/Address AddressLine1..6, AddressCountry", "Addresses"),
        FieldDoc(
            "*.measures", "SanctionsImposedIndicators / SanctionsImposed", "Asset freeze, travel ban, ..."
        ),
        FieldDoc("*.reason", "UKStatementofReasons", "UK statement of reasons"),
        FieldDoc("*.listed_on", "DateDesignated", "Designation date"),
        FieldDoc("*.program", "RegimeName", "Regulations the designation is made under"),
    ]

    def read_info(self, path: Path) -> FileInfo:
        root, attrs, shallow = root_info(path)
        marker = None
        for k in (
            "DateGenerated",
            "dateGenerated",
            "PublicationDate",
            "DateOfPublication",
            "GeneratedDate",
            "generationDate",
        ):
            marker = attrs.get(k) or shallow.get(k)
            if marker:
                break
        published = None
        if marker:
            try:
                published = datetime.fromisoformat(marker.replace("Z", "+00:00"))
                if published.tzinfo is None:
                    published = published.replace(tzinfo=UTC)
            except ValueError:
                published = None
        return FileInfo(publication_marker=marker, published_at=published, extra={"root": root})

    def parse(self, path: Path, stats: ParseStats) -> Iterator[CanonicalRecord]:
        for el, _ in iter_records(str(path), ("Designation",), stats):
            try:
                rec = self._record(el, stats)
            except Exception as e:
                stats.skipped_records += 1
                stats.warn("PARSE_WARNING", f"UK designation skipped: {e!r}")
                continue
            if rec is None:
                stats.skipped_records += 1
                continue
            stats.records += 1
            yield rec

    def _record(self, el: etree._Element, stats: ParseStats) -> CanonicalRecord | None:
        uid = kid_text(el, "UniqueID", "UniqueId", "UniqueIdentifier")
        group_id = kid_text(el, "OFSIGroupID", "OFSIGroupId")
        if not uid:
            stats.warn("MISSING_REQUIRED_FIELD", f"designation without UniqueID (group {group_id})")
            return None
        etype = _TYPES.get(
            (kid_text(el, "IndividualEntityShip", "EntityType") or "").strip().lower(), "UNKNOWN"
        )

        names: list[Name] = []
        names_el = kid(el, "Names")
        for n in kids(names_el, "Name") if names_el is not None else []:
            parts = {f"name{i}": v for i in range(1, 7) if (v := kid_text(n, f"Name{i}"))}
            full = join_name(*(parts.get(f"name{i}") for i in range(1, 7)))
            if not full:
                continue
            ntype_raw = (kid_text(n, "NameType") or "").lower()
            ntype = "PRIMARY" if "primary" in ntype_raw else "AKA"
            strength = (kid_text(n, "AliasStrength") or "").strip().lower()
            names.append(
                Name(
                    name_type=ntype,
                    full_name=full,
                    parts=parts,  # type: ignore[arg-type]
                    quality="STRONG" if ntype == "PRIMARY" else _QUALITY.get(strength, "UNKNOWN"),  # type: ignore[arg-type]
                    raw_quality=kid_text(n, "AliasStrength"),
                )
            )
        nl = kid(el, "NonLatinNames")
        for n in kids(nl, "NonLatinName") if nl is not None else []:
            v = kid_text(n, "NameNonLatinScript")
            if v:
                names.append(
                    Name(
                        name_type="OTHER",
                        full_name=v,
                        script=kid_text(n, "NonLatinScriptType"),
                        language=kid_text(n, "NonLatinScriptLanguage"),
                        quality="STRONG",
                    )
                )
        if not names:
            stats.warn("MISSING_REQUIRED_FIELD", f"{uid}: no names")
            return None
        names.sort(key=lambda x: 0 if x.name_type == "PRIMARY" else 1)

        identifiers: list[Identifier] = []
        un_ref = kid_text(el, "UNReferenceNumber", "UNRef")
        for label, value, t in (
            ("UK UniqueID", uid, "OTHER"),
            ("OFSI Group ID", group_id, "OTHER"),
            ("UN Reference Number", un_ref, "UN_REF"),
        ):
            if value:
                i = make_identifier(label, value, id_type=t)  # type: ignore[arg-type]
                if i:
                    identifiers.append(i)

        addresses: list[Address] = []
        addrs = kid(el, "Addresses")
        for a in kids(addrs, "Address") if addrs is not None else []:
            lines = [kid_text(a, f"AddressLine{i}") for i in range(1, 7)]
            country_raw = kid_text(a, "AddressCountry", "Country")
            iso = to_iso2(country_raw)
            stats.country(country_raw, iso)
            postal = kid_text(a, "AddressPostalCode", "PostalCode")
            if not any(lines) and not country_raw and not postal:
                continue
            addresses.append(
                Address(
                    street=join_name(*lines[:3]) or None,
                    city=lines[3] if lines[3] else None,
                    region=join_name(*lines[4:]) or None,
                    postal_code=postal,
                    country_iso2=iso,
                    country_raw=country_raw,
                    full_raw=join_name(*lines, postal, country_raw) or None,
                )
            )

        birth_dates: list[BirthDate] = []
        birth_places: list[BirthPlace] = []
        countries: list[Country] = []
        positions: list[str] = []
        gender = None
        vessel: VesselInfo | None = None
        relationships: list[Relationship] = []
        ind = next(iter(descendants(el, "Individual")), None)
        if ind is not None:
            for d in _texts(ind, "DOBs", "DOB"):
                bd = parse_birth_date(d)
                if bd:
                    if bd.precision == "UNKNOWN":
                        stats.unparseable_dates += 1
                    birth_dates.append(bd)
            for nat in _texts(ind, "Nationalities", "Nationality"):
                iso = to_iso2(nat)
                stats.country(nat, iso)
                countries.append(Country(kind="NATIONALITY", country_iso2=iso, country_raw=nat))
            for pd in descendants(ind, "PassportDetail"):
                num = kid_text(pd, "PassportNumber")
                if num:
                    i = make_identifier("Passport", num, id_type="PASSPORT")
                    if i:
                        i.label = (
                            join_name("Passport", kid_text(pd, "PassportAdditionalInformation")) or "Passport"
                        )
                        identifiers.append(i)
            for nd in descendants(ind, "NationalIdentifierDetail"):
                num = kid_text(nd, "NationalIdentifierNumber")
                if num:
                    i = make_identifier("National ID", num, id_type="NATIONAL_ID")
                    if i:
                        identifiers.append(i)
            positions = _texts(ind, "Positions", "Position")
            for loc in descendants(ind, "Location"):
                town, c_raw = kid_text(loc, "TownOfBirth"), kid_text(loc, "CountryOfBirth")
                if town or c_raw:
                    iso = to_iso2(c_raw)
                    stats.country(c_raw, iso)
                    birth_places.append(
                        BirthPlace(place=town, country_iso2=iso, raw=join_name(town, c_raw) or None)
                    )
            genders = _texts(ind, "Genders", "Gender")
            gender = genders[0] if genders else kid_text(ind, "Gender")
        ent = next(iter(descendants(el, "Entity")), None)
        if ent is not None:
            for brn in descendants(ent, "BusinessRegistrationNumber"):
                v = txt(brn)
                if v:
                    i = make_identifier("Business Registration Number", v, id_type="REGISTRATION")
                    if i:
                        identifiers.append(i)
            for parent in _texts(ent, "ParentCompanies", "ParentCompany"):
                relationships.append(Relationship(rel_type="PARENT_COMPANY", target_name=parent))
            for sub in _texts(ent, "Subsidiaries", "Subsidiary"):
                relationships.append(Relationship(rel_type="SUBSIDIARY", target_name=sub))
        ship = next(iter(descendants(el, "Ship")), None)
        if ship is not None or etype == "VESSEL":
            vessel = VesselInfo()
            if ship is not None:
                for imo in _texts(ship, "IMONumbers", "IMONumber"):
                    i = make_identifier("IMO number", imo, id_type="IMO")
                    if i:
                        identifiers.append(i)
                flags = _texts(ship, "CurrentBelievedFlagOfShips", "CurrentBelievedFlagOfShip")
                if flags:
                    vessel.flag_raw = flags[0]
                    vessel.flag_iso2 = to_iso2(flags[0])
                    stats.country(flags[0], vessel.flag_iso2)
                vessel.former_flags = _texts(ship, "PreviousFlags", "PreviousFlag")
                types = _texts(ship, "TypeOfShips", "TypeOfShip")
                vessel.vessel_type = types[0] if types else None
                tonnage = _texts(ship, "TonnageOfShips", "TonnageOfShip")
                vessel.tonnage = tonnage[0] if tonnage else None
                years = _texts(ship, "YearBuilts", "YearBuilt")
                m = re.search(r"\d{4}", years[0]) if years else None
                vessel.build_year = int(m.group(0)) if m else None
                owners = _texts(ship, "CurrentOwnerOperators", "CurrentOwnerOperator")
                vessel.owner_operator_raw = "; ".join(owners) or None
                for o in owners:
                    relationships.append(Relationship(rel_type="OWNER_OPERATOR", target_name=o))
                for hull in _texts(ship, "HullIdentificationNumbers", "HullIdentificationNumber"):
                    i = make_identifier("Hull Identification Number", hull, id_type="OTHER")
                    if i:
                        identifiers.append(i)

        measures: set[str] = set()
        ind_el = kid(el, "SanctionsImposedIndicators")
        if ind_el is not None:
            for flag in kids(ind_el):
                if (txt(flag) or "").strip().lower() == "true":
                    measures.add(_MEASURE_FLAGS.get(etree.QName(flag).localname.lower(), "OTHER"))
        if not measures:
            imposed = (kid_text(el, "SanctionsImposed") or "").lower()
            for needle, measure in _MEASURE_TEXT:
                if needle in imposed:
                    measures.add(measure)
        listing = Listing(
            authority="UK",
            list_name="UK Sanctions List",
            program_code=kid_text(el, "RegimeName"),
            listed_on=parse_date(kid_text(el, "DateDesignated")),
            reference_no=uid,
            legal_basis=kid_text(el, "RegimeName"),
            measures=sorted(measures),
            reason=clean(kid_text(el, "UKStatementofReasons", "UKStatementOfReasons")),
            remarks=clean(kid_text(el, "OtherInformation")),
        )
        emails = _texts(el, "EmailAddresses", "EmailAddress")
        for e in emails:
            i = make_identifier("Email", e, id_type="EMAIL")
            if i:
                identifiers.append(i)
        return CanonicalRecord(
            source_key=uid,
            entity_type=etype,
            names=names,
            identifiers=identifiers,  # type: ignore[arg-type]
            addresses=addresses,
            birth_dates=birth_dates,
            birth_places=birth_places,
            countries=countries,
            listings=[listing],
            relationships=relationships,
            vessel=vessel,
            gender=gender,
            positions=positions,
            source_updated_on=parse_date(kid_text(el, "LastUpdated")),
            extra={"ofsi_group_id": group_id, "designation_source": kid_text(el, "DesignationSource")},
        )
