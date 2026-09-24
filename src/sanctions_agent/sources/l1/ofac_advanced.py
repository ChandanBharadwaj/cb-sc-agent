"""OFAC Sanctions List Service - Advanced XML (``SDN_ADVANCED.XML`` / ``CONS_ADVANCED.XML``).

Advanced XML is used rather than CSV because the CSV squeezes DOB, IDs, IMO and links into a
1,000-character Remarks field and loses alias types, weak-alias flags, name parts and relationships
(BRD 8.2 / 10.3).

The file is normalised: reference value sets (types, countries, programmes) come first, then
Locations, IDRegDocuments, DistinctParties, ProfileRelationships and SanctionsEntries, linked by ids.
Pass 1 loads everything except DistinctParties; pass 2 streams DistinctParties and assembles one
canonical record per party. Matching is by local name so the May-2024 namespace change (and any
future one) does not matter.

Stable key: DistinctParty/@FixedRef (the SDN "UID").
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from lxml import etree

from sanctions_agent.canonical.model import (
    Address,
    AircraftInfo,
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
from sanctions_agent.canonical.normalize.identifiers import make_identifier
from sanctions_agent.canonical.normalize.names import join_name
from sanctions_agent.sources.base import (
    FieldDoc,
    FileInfo,
    ListAdapter,
    ParseStats,
    iter_records,
    kid,
    kid_text,
    kids,
    local,
    txt,
)

_ALIAS_TYPES = {"a.k.a.": "AKA", "f.k.a.": "FKA", "n.k.a.": "NKA", "name": "PRIMARY"}
_PART_KEYS = {
    "last name": "family",
    "first name": "given",
    "middle name": "middle",
    "maiden name": "maiden",
    "patronymic": "patronymic",
    "matronymic": "matronymic",
    "nickname": "nickname",
    "entity name": "entity",
    "vessel name": "vessel",
    "aircraft name": "aircraft",
}
_PERSON_ORDER = ("given", "middle", "patronymic", "matronymic", "family", "maiden", "nickname")


def _measure(type_text: str) -> str:
    t = type_text.lower()
    if t == "block" or "blocking" in t:
        return "ASSET_FREEZE"
    if (
        "directive" in t
        or "sectoral" in t
        or "capta" in t
        or "correspondent" in t
        or "menu" in t
        or "cmic" in t
        or "military-industrial" in t
    ):
        return "SECTORAL"
    if "export" in t:
        return "EXPORT_RESTRICTION"
    if "procurement" in t:
        return "PROCUREMENT_BAN"
    return "OTHER"


@dataclass
class _Ref:
    sets: dict[str, dict[str, tuple[str, dict[str, str]]]] = field(default_factory=lambda: defaultdict(dict))

    def text(self, item: str, id_: str | None) -> str | None:
        if not id_:
            return None
        v = self.sets.get(item, {}).get(id_)
        return v[0] if v else None

    def attr(self, item: str, id_: str | None, name: str) -> str | None:
        if not id_:
            return None
        v = self.sets.get(item, {}).get(id_)
        return v[1].get(name) if v else None


@dataclass
class _Pass1:
    ref: _Ref = field(default_factory=_Ref)
    date_of_issue: str | None = None
    locations: dict[str, dict[str, Any]] = field(default_factory=dict)
    docs_by_identity: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    rels_by_profile: dict[str, list[tuple[str, str | None, bool]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    entries_by_profile: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))


def _ymd(el: etree._Element | None) -> tuple[int, int | None, int | None] | None:
    if el is None:
        return None
    y = kid_text(el, "Year")
    if not y or not y.isdigit():
        return None
    m, d = kid_text(el, "Month"), kid_text(el, "Day")
    return int(y), int(m) if m and m.isdigit() else None, int(d) if d and d.isdigit() else None


def _date_period(dp: etree._Element) -> BirthDate | None:
    start_el, end_el = kid(dp, "Start"), kid(dp, "End")
    s = _ymd(kid(start_el, "From")) if start_el is not None else None
    e = _ymd(kid(end_el, "To")) if end_el is not None else None
    if e is None and end_el is not None:
        e = _ymd(kid(end_el, "From"))
    if s is None:
        return None
    if e is None:
        e = s
    approx = any((x is not None and x.get("Approximate") == "true") for x in (start_el, end_el))
    sy, sm, sd = s
    ey, em, ed = e
    raw = f"{sy:04d}-{sm or 0:02d}-{sd or 0:02d}..{ey:04d}-{em or 0:02d}-{ed or 0:02d}"
    if approx:
        raw = "circa " + raw
    if (sy, sm, sd) == (ey, em, ed) and sm and sd:
        return BirthDate(date_value=_safe(sy, sm, sd), year=sy, precision="DAY", raw=raw)
    if sy == ey and (sm in (None, 1)) and (sd in (None, 1)) and (em in (None, 12)) and (ed in (None, 31)):
        return BirthDate(year=sy, precision="YEAR", raw=raw)
    if (
        sy == ey
        and sm
        and sm == em
        and sd in (None, 1)
        and (ed is None or ed == calendar.monthrange(sy, sm)[1])
    ):
        return BirthDate(year=sy, date_value=_safe(sy, sm, 1), precision="MONTH", raw=raw)
    return BirthDate(year_from=sy, year_to=ey, precision="RANGE", raw=raw)


def _safe(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


class OfacAdvancedAdapter(ListAdapter):
    type_id = "ofac_advanced_xml"
    authority = "OFAC"
    field_docs = [
        FieldDoc("*.source_key", "DistinctParty/@FixedRef", "OFAC UID of the party"),
        FieldDoc(
            "*.name",
            "Profile/Identity/Alias[@Primary='true']/DocumentedName",
            "Primary name built from name parts",
        ),
        FieldDoc(
            "*.alias",
            "Identity/Alias (AliasTypeID A.K.A./F.K.A./N.K.A.)",
            "Aliases; @LowQuality='true' -> WEAK",
            notes="OFAC: weak aliases confirm a hit but should not be screened on alone (FAQ 5)",
        ),
        FieldDoc(
            "PERSON.dob",
            "Feature[Birthdate]/FeatureVersion/DatePeriod",
            "Start/End periods give DAY/MONTH/YEAR/RANGE",
        ),
        FieldDoc("PERSON.birth_place", "Feature[Place of Birth]", "Text or location"),
        FieldDoc(
            "PERSON.nationality", "Feature[Nationality Country|Citizenship Country]", "Via Location country"
        ),
        FieldDoc("*.address", "Feature[Location] -> Locations/Location", "Address parts + country"),
        FieldDoc(
            "PERSON.passport", "IDRegDocuments/IDRegDocument[IDRegDocType=Passport]", "Passport numbers"
        ),
        FieldDoc("PERSON.national_id", "IDRegDocument[National ID No.|Cedula No. ...]", "National IDs"),
        FieldDoc(
            "ORGANIZATION.registration_id",
            "IDRegDocument[Registration Number|Tax ID No.|...]",
            "Company identifiers",
        ),
        FieldDoc("ORGANIZATION.lei", "IDRegDocument[Legal Entity Number]", "LEI (rare: ~1.7%)"),
        FieldDoc(
            "VESSEL.imo",
            "IDRegDocument[Vessel Registration Identification]",
            "IMO number (checksum validated)",
        ),
        FieldDoc("VESSEL.mmsi", "Feature[MMSI]", "MMSI (OFAC only list with MMSI)"),
        FieldDoc("VESSEL.call_sign", "Feature[Vessel Call Sign]", "Call sign"),
        FieldDoc("VESSEL.flag", "Feature[Vessel Flag]", "Current flag"),
        FieldDoc(
            "VESSEL.owner_link",
            "Feature[Vessel Owner] / ProfileRelationships",
            "Owner/operator text or links",
        ),
        FieldDoc("AIRCRAFT.msn", "Feature[Aircraft Manufacturer's Serial Number (MSN)]", "Serial number"),
        FieldDoc("AIRCRAFT.tail", "Feature[Aircraft Tail Number]", "Tail number"),
        FieldDoc(
            "*.program",
            "SanctionsEntry/SanctionsMeasure[Program]/Comment",
            "Programme codes (SDGT, IRAN, ...)",
        ),
        FieldDoc(
            "*.measures",
            "SanctionsEntry/SanctionsMeasure/@SanctionsTypeID",
            "Block -> ASSET_FREEZE, directives -> SECTORAL",
        ),
        FieldDoc("*.listed_on", "SanctionsEntry/EntryEvent/Date", "Earliest entry event"),
        FieldDoc(
            "*.relationship", "ProfileRelationships/ProfileRelationship", "Links between listed parties"
        ),
    ]

    # ------------------------------------------------------------------------------------------
    def read_info(self, path: Path) -> FileInfo:
        for el, _ in iter_records(str(path), ("DateOfIssue",), ParseStats()):
            ymd = _ymd(el)
            if ymd:
                y, m, d = ymd
                marker = f"{y:04d}-{m or 1:02d}-{d or 1:02d}"
                return FileInfo(
                    publication_marker=marker, published_at=datetime(y, m or 1, d or 1, tzinfo=UTC)
                )
            break
        return FileInfo()

    def parse(self, path: Path, stats: ParseStats) -> Iterator[CanonicalRecord]:
        p1_stats = ParseStats()
        p1 = self._pass1(path, p1_stats)
        for pth, c in p1_stats.paths.items():
            if "/DistinctParties/" not in pth:
                stats.paths[pth] = max(stats.paths.get(pth, 0), c)
        for el, _ in iter_records(str(path), ("DistinctParty",), stats):
            try:
                rec = self._party(el, p1, stats)
            except Exception as e:
                stats.skipped_records += 1
                stats.warn("PARSE_WARNING", f"OFAC party {el.get('FixedRef')} skipped: {e!r}")
                continue
            if rec is None:
                stats.skipped_records += 1
                continue
            stats.records += 1
            yield rec

    # ------------------------------------------------------------------------------------------
    def _pass1(self, path: Path, stats: ParseStats) -> _Pass1:
        p = _Pass1()
        tags = (
            "DateOfIssue",
            "ReferenceValueSets",
            "Location",
            "IDRegDocument",
            "ProfileRelationship",
            "SanctionsEntry",
            "DistinctParty",
        )
        for el, _parent in iter_records(str(path), tags, stats):
            name = local(el)
            if name == "DistinctParty":
                continue
            if name == "DateOfIssue":
                ymd = _ymd(el)
                if ymd:
                    p.date_of_issue = f"{ymd[0]:04d}-{ymd[1] or 1:02d}-{ymd[2] or 1:02d}"
            elif name == "ReferenceValueSets":
                for value_set in kids(el):
                    for item in kids(value_set):
                        if item.get("ID"):
                            p.ref.sets[local(item)][item.get("ID")] = (txt(item) or "", dict(item.attrib))
            elif name == "Location":
                parts: dict[str, str] = {}
                for lp in kids(el, "LocationPart"):
                    ptype = (
                        p.ref.text("LocPartType", lp.get("LocPartTypeID")) or lp.get("LocPartTypeID") or "?"
                    )
                    for v in kids(lp, "LocationPartValue"):
                        val = kid_text(v, "Value")
                        if val:
                            parts[ptype.upper()] = val
                            break
                lc = kid(el, "LocationCountry")
                p.locations[el.get("ID", "")] = {
                    "parts": parts,
                    "country_id": lc.get("CountryID") if lc is not None else None,
                }
            elif name == "IDRegDocument":
                doc: dict[str, Any] = {
                    "type": p.ref.text("IDRegDocType", el.get("IDRegDocTypeID")),
                    "number": kid_text(el, "IDRegistrationNo"),
                    "country_id": el.get("IssuedBy-CountryID"),
                    "issued_on": None,
                    "expires_on": None,
                }
                for dd in kids(el, "DocumentDate"):
                    dtype = (p.ref.text("IDRegDocDateType", dd.get("IDRegDocDateTypeID")) or "").lower()
                    dp = kid(dd, "DatePeriod")
                    bd = _date_period(dp) if dp is not None else None
                    val = str(bd.date_value) if bd and bd.date_value else (bd.raw if bd else None)
                    if "issue" in dtype:
                        doc["issued_on"] = val
                    elif "expir" in dtype:
                        doc["expires_on"] = val
                p.docs_by_identity[el.get("IdentityID", "")].append(doc)
            elif name == "ProfileRelationship":
                p.rels_by_profile[el.get("From-ProfileID", "")].append(
                    (
                        el.get("To-ProfileID", ""),
                        p.ref.text("RelationType", el.get("RelationTypeID")),
                        el.get("Former") == "true",
                    )
                )
            elif name == "SanctionsEntry":
                events = []
                for ev in kids(el, "EntryEvent"):
                    ymd = _ymd(kid(ev, "Date"))
                    events.append(
                        {
                            "type": p.ref.text("EntryEventType", ev.get("EntryEventTypeID")),
                            "date": _safe(ymd[0], ymd[1] or 1, ymd[2] or 1) if ymd else None,
                            "legal_basis": p.ref.attr(
                                "LegalBasis", ev.get("LegalBasisID"), "LegalBasisShortRef"
                            )
                            or p.ref.text("LegalBasis", ev.get("LegalBasisID")),
                        }
                    )
                measures = []
                for sm in kids(el, "SanctionsMeasure"):
                    measures.append(
                        {
                            "type": p.ref.text("SanctionsType", sm.get("SanctionsTypeID")) or "",
                            "comment": kid_text(sm, "Comment"),
                        }
                    )
                p.entries_by_profile[el.get("ProfileID", "")].append(
                    {
                        "list": p.ref.text("List", el.get("ListID")),
                        "events": events,
                        "measures": measures,
                    }
                )
        return p

    # ------------------------------------------------------------------------------------------
    def _country(self, p: _Pass1, country_id: str | None, stats: ParseStats) -> tuple[str | None, str | None]:
        if not country_id:
            return None, None
        raw = p.ref.text("Country", country_id)
        iso = (p.ref.attr("Country", country_id, "ISO2") or "").upper() or to_iso2(raw)
        stats.country(raw, iso)
        return iso or None, raw

    def _address(self, p: _Pass1, loc_id: str | None, stats: ParseStats) -> Address | None:
        loc = p.locations.get(loc_id or "")
        if not loc:
            return None
        parts = loc["parts"]
        iso, raw = self._country(p, loc["country_id"], stats)
        street = join_name(parts.get("ADDRESS1"), parts.get("ADDRESS2"), parts.get("ADDRESS3")) or None
        region = parts.get("STATE/PROVINCE") or parts.get("REGION")
        return Address(
            street=street,
            city=parts.get("CITY"),
            region=region,
            postal_code=parts.get("POSTAL CODE"),
            country_iso2=iso,
            country_raw=raw,
            full_raw=join_name(street, parts.get("CITY"), region, parts.get("POSTAL CODE"), raw) or None,
        )

    def _entity_type(self, p: _Pass1, profile: etree._Element) -> str:
        sub_id = profile.get("PartySubTypeID")
        sub_text = (p.ref.text("PartySubType", sub_id) or "").lower()
        if sub_text == "vessel":
            return "VESSEL"
        if sub_text == "aircraft":
            return "AIRCRAFT"
        party_type = (
            p.ref.text("PartyType", p.ref.attr("PartySubType", sub_id, "PartyTypeID")) or ""
        ).lower()
        if party_type == "individual":
            return "PERSON"
        if party_type == "entity":
            return "ORGANIZATION"
        return "UNKNOWN"

    def _party(self, dp: etree._Element, p: _Pass1, stats: ParseStats) -> CanonicalRecord | None:
        uid = dp.get("FixedRef")
        profile = kid(dp, "Profile")
        if not uid or profile is None:
            stats.warn("MISSING_REQUIRED_FIELD", "DistinctParty without FixedRef/Profile")
            return None
        etype = self._entity_type(p, profile)
        names: list[Name] = []
        identifiers: list[Identifier] = [
            Identifier(id_type="OFAC_UID", label="OFAC UID", value=uid, value_norm=uid)
        ]
        identity_ids: list[str] = []
        for identity in kids(profile, "Identity"):
            identity_ids.append(identity.get("ID", ""))
            group_types: dict[str, str] = {}
            for npg in descendants_local(identity, "NamePartGroup"):
                group_types[npg.get("ID", "")] = (
                    p.ref.text("NamePartType", npg.get("NamePartTypeID")) or ""
                ).lower()
            for alias in kids(identity, "Alias"):
                atype_raw = (p.ref.text("AliasType", alias.get("AliasTypeID")) or "").lower()
                is_primary = alias.get("Primary") == "true" and identity.get("Primary", "true") == "true"
                ntype = "PRIMARY" if is_primary else _ALIAS_TYPES.get(atype_raw, "AKA")
                if ntype == "PRIMARY" and not is_primary:
                    ntype = "AKA"
                quality = "WEAK" if alias.get("LowQuality") == "true" else "STRONG"
                for dn in kids(alias, "DocumentedName"):
                    parts: dict[str, str] = {}
                    script = None
                    ordered: list[str] = []
                    for dnp in kids(dn, "DocumentedNamePart"):
                        npv = kid(dnp, "NamePartValue")
                        if npv is None or not txt(npv):
                            continue
                        part_value = txt(npv) or ""
                        ordered.append(part_value)
                        script = p.ref.attr("Script", npv.get("ScriptID"), "ScriptCode") or script
                        key = _PART_KEYS.get(group_types.get(npv.get("NamePartGroupID", ""), ""), "other")
                        parts[key] = join_name(parts.get(key), part_value)
                    if not ordered:
                        continue
                    if etype == "PERSON":
                        full = join_name(
                            *[parts.get(k) for k in _PERSON_ORDER], parts.get("other")
                        ) or join_name(*ordered)
                    else:
                        full = join_name(*ordered)
                    names.append(
                        Name(
                            name_type=ntype,
                            full_name=full,
                            parts=parts,
                            script=script,  # type: ignore[arg-type]
                            quality=quality if ntype != "PRIMARY" else "STRONG",
                            raw_quality="LowQuality" if quality == "WEAK" else None,
                        )
                    )
        if not names:
            stats.warn("MISSING_REQUIRED_FIELD", f"OFAC {uid}: no names")
            return None
        # primary first
        names.sort(key=lambda n: 0 if n.name_type == "PRIMARY" else 1)

        for iid in identity_ids:
            for doc in p.docs_by_identity.get(iid, []):
                if not doc["number"]:
                    continue
                c_iso, c_raw = self._country(p, doc["country_id"], stats)
                ident = make_identifier(
                    doc["type"],
                    doc["number"],
                    country_raw=c_raw,
                    issued_on=doc["issued_on"],
                    expires_on=doc["expires_on"],
                )
                if ident:
                    if c_iso and not ident.country_iso2:
                        ident.country_iso2 = c_iso
                    identifiers.append(ident)

        addresses: list[Address] = []
        birth_dates: list[BirthDate] = []
        birth_places: list[BirthPlace] = []
        countries: list[Country] = []
        vessel = VesselInfo() if etype == "VESSEL" else None
        aircraft = AircraftInfo() if etype == "AIRCRAFT" else None
        gender = None
        titles: list[str] = []
        extra_features: dict[str, list[str]] = defaultdict(list)
        remarks: list[str] = []
        for feat in kids(profile, "Feature"):
            ftype = (p.ref.text("FeatureType", feat.get("FeatureTypeID")) or "").strip()
            fl = ftype.lower()
            for fv in kids(feat, "FeatureVersion"):
                detail_texts: list[str] = []
                for vd in kids(fv, "VersionDetail"):
                    t = txt(vd) or p.ref.text("DetailReference", vd.get("DetailReferenceID"))
                    if t:
                        detail_texts.append(t)
                loc_ids = [vl.get("LocationID") for vl in kids(fv, "VersionLocation")]
                dp_el = kid(fv, "DatePeriod")
                value = detail_texts[0] if detail_texts else None
                if fl == "birthdate":
                    bd = _date_period(dp_el) if dp_el is not None else None
                    if bd:
                        birth_dates.append(bd)
                    else:
                        stats.unparseable_dates += 1
                elif fl == "place of birth":
                    ad = self._address(p, loc_ids[0], stats) if loc_ids else None
                    birth_places.append(
                        BirthPlace(
                            place=value or (ad.city if ad else None) or (ad.full_raw if ad else None),
                            country_iso2=ad.country_iso2 if ad else to_iso2(value),
                            raw=value or (ad.full_raw if ad else None),
                        )
                    )
                elif fl in ("nationality country", "citizenship country", "nationality of registration"):
                    kind = (
                        "CITIZENSHIP"
                        if fl.startswith("citizenship")
                        else ("REGISTRATION_COUNTRY" if "registration" in fl else "NATIONALITY")
                    )
                    for lid in loc_ids or [None]:
                        ad = self._address(p, lid, stats) if lid else None
                        raw = ad.country_raw if ad else value
                        iso = ad.country_iso2 if ad else to_iso2(value)
                        if raw or iso:
                            countries.append(Country(kind=kind, country_iso2=iso, country_raw=raw))  # type: ignore[arg-type]
                elif fl == "location":
                    for lid in loc_ids:
                        ad = self._address(p, lid, stats)
                        if ad:
                            addresses.append(ad)
                elif fl == "gender":
                    gender = value
                elif fl == "title":
                    if value:
                        titles.append(value)
                elif fl in ("vessel call sign",) and value:
                    ident = make_identifier("Call Sign", value, id_type="CALL_SIGN")
                    if ident:
                        identifiers.append(ident)
                elif fl == "mmsi" and value:
                    ident = make_identifier("MMSI", value, id_type="MMSI")
                    if ident:
                        identifiers.append(ident)
                elif fl == "vessel type" and vessel is not None:
                    vessel.vessel_type = value
                elif fl == "vessel flag" and vessel is not None:
                    vessel.flag_raw = value
                    vessel.flag_iso2 = to_iso2(value)
                    stats.country(value, vessel.flag_iso2)
                elif fl in ("other vessel flag", "former vessel flag") and vessel is not None and value:
                    vessel.former_flags.append(value)
                elif fl == "vessel owner" and vessel is not None:
                    vessel.owner_operator_raw = value
                elif (
                    fl in ("vessel tonnage", "vgt", "vto", "vessel gross registered tonnage")
                    and vessel is not None
                ):
                    vessel.tonnage = value
                elif fl == "vessel year of build" and vessel is not None and value and value[:4].isdigit():
                    vessel.build_year = int(value[:4])
                elif fl == "aircraft model" and aircraft is not None:
                    aircraft.model = value
                elif fl == "aircraft operator" and aircraft is not None:
                    aircraft.operator_raw = value
                elif fl == "aircraft manufacture date" and aircraft is not None:
                    bd = _date_period(dp_el) if dp_el is not None else None
                    aircraft.build_year = (
                        bd.year if bd else (int(value[:4]) if value and value[:4].isdigit() else None)
                    )
                elif "serial number" in fl and value:
                    ident = make_identifier(ftype, value, id_type="AIRCRAFT_MSN")
                    if ident:
                        identifiers.append(ident)
                elif fl == "aircraft tail number" and value:
                    ident = make_identifier(ftype, value, id_type="AIRCRAFT_TAIL")
                    if ident:
                        identifiers.append(ident)
                elif fl in ("website", "email address") and value:
                    ident = make_identifier(ftype, value, id_type="WEBSITE" if fl == "website" else "EMAIL")
                    if ident:
                        identifiers.append(ident)
                elif fl.startswith("additional sanctions information") and value:
                    remarks.append(value)
                elif value or detail_texts:
                    extra_features[ftype].extend(detail_texts)

        relationships = [
            Relationship(
                rel_type=rt or "RELATED",
                target_source_key=self._profile_uid(to_id),
                raw=("former " if former else "") + (rt or ""),
            )
            for to_id, rt, former in p.rels_by_profile.get(profile.get("ID", ""), [])
        ]
        listings = self._listings(p.entries_by_profile.get(profile.get("ID", ""), []), remarks)
        if vessel is not None and vessel.owner_operator_raw is None:
            owners = [
                r for r in relationships if "owned" in r.rel_type.lower() or "operat" in r.rel_type.lower()
            ]
            if owners:
                vessel.owner_operator_raw = f"linked:{owners[0].target_source_key}"
        return CanonicalRecord(
            source_key=uid,
            entity_type=etype,
            names=names,
            identifiers=identifiers,  # type: ignore[arg-type]
            addresses=addresses,
            birth_dates=birth_dates,
            birth_places=birth_places,
            countries=countries,
            listings=listings,
            relationships=relationships,
            vessel=vessel,
            aircraft=aircraft,
            gender=gender,
            titles=titles,
            remarks="; ".join(remarks) or None,
            extra={"features": dict(extra_features)} if extra_features else {},
        )

    @staticmethod
    def _profile_uid(profile_id: str) -> str:
        # In OFAC files the Profile ID equals the DistinctParty FixedRef.
        return profile_id

    @staticmethod
    def _listings(entries: list[dict[str, Any]], remarks: list[str]) -> list[Listing]:
        out: list[Listing] = []
        for e in entries:
            dates = [ev["date"] for ev in e["events"] if ev["date"]]
            basis = next((ev["legal_basis"] for ev in e["events"] if ev["legal_basis"]), None)
            measures = sorted({_measure(m["type"]) for m in e["measures"] if m["type"].lower() != "program"})
            programs = [
                m["comment"] for m in e["measures"] if m["type"].lower() == "program" and m["comment"]
            ]
            for prog in programs or [None]:
                out.append(
                    Listing(
                        authority="OFAC",
                        list_name=e["list"],
                        program_code=prog,
                        listed_on=min(dates) if dates else None,
                        legal_basis=basis,
                        measures=measures,
                        remarks="; ".join(remarks) or None,
                    )
                )
        return out


def descendants_local(el: etree._Element, name: str) -> Iterator[etree._Element]:
    for d in el.iter():
        if isinstance(d.tag, str) and local(d) == name:
            yield d
