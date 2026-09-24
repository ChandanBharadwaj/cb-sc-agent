"""Canonical record model (FR-08): one shape for people, organisations, vessels and aircraft from every list.

Normalised values sit next to the raw values the publisher gave (FR-13). Alias quality (FR-11) and
measures (FR-12) are first-class fields. ``content_hash`` is computed over a canonical JSON
serialisation so that a change anywhere in the record is detected by the diff.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EntityType = Literal["PERSON", "ORGANIZATION", "VESSEL", "AIRCRAFT", "UNKNOWN"]
NameType = Literal["PRIMARY", "AKA", "FKA", "NKA", "OTHER"]
Quality = Literal["STRONG", "WEAK", "UNKNOWN"]
IdType = Literal[
    "PASSPORT",
    "NATIONAL_ID",
    "IMO",
    "MMSI",
    "CALL_SIGN",
    "LEI",
    "REGISTRATION",
    "TAX",
    "SWIFT",
    "UN_REF",
    "OFAC_UID",
    "EU_REF",
    "AIRCRAFT_MSN",
    "AIRCRAFT_TAIL",
    "EMAIL",
    "WEBSITE",
    "OTHER",
]
Precision = Literal["DAY", "MONTH", "YEAR", "RANGE", "UNKNOWN"]
CountryKind = Literal["NATIONALITY", "CITIZENSHIP", "REGISTRATION_COUNTRY", "FLAG", "COUNTRY"]

# Normalised measure vocabulary (FR-12). Adapters map publisher wording onto these.
MEASURES = (
    "ASSET_FREEZE",
    "TRAVEL_BAN",
    "ARMS_EMBARGO",
    "PORT_BAN",
    "EXPORT_RESTRICTION",
    "LICENSE_REQUIREMENT",
    "DENIAL_OF_EXPORT_PRIVILEGES",
    "SECTORAL",
    "TRUST_SERVICES_BAN",
    "DIRECTOR_DISQUALIFICATION",
    "CHARTERING_BAN",
    "PROCUREMENT_BAN",
    "DEBARMENT",
    "OTHER",
)


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Name(_M):
    name_type: NameType = "PRIMARY"
    full_name: str
    parts: dict[str, str] = Field(default_factory=dict)  # given, middle, family, patronymic, title, ...
    script: str | None = None
    language: str | None = None
    quality: Quality = "UNKNOWN"
    raw_quality: str | None = None


class Identifier(_M):
    id_type: IdType
    label: str | None = None  # the publisher's own label, e.g. "Passport", "Registration Number"
    value: str
    value_norm: str
    country_iso2: str | None = None
    country_raw: str | None = None
    issued_on: str | None = None
    expires_on: str | None = None
    checksum_valid: bool | None = None


class Address(_M):
    street: str | None = None
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country_iso2: str | None = None
    country_raw: str | None = None
    full_raw: str | None = None


class BirthDate(_M):
    date_value: date | None = None
    year: int | None = None
    year_from: int | None = None
    year_to: int | None = None
    precision: Precision = "UNKNOWN"
    raw: str | None = None


class BirthPlace(_M):
    place: str | None = None
    country_iso2: str | None = None
    raw: str | None = None


class Country(_M):
    kind: CountryKind
    country_iso2: str | None = None
    country_raw: str | None = None


class Listing(_M):
    authority: str  # OFAC, UN, UK, EU, BIS, STATE, ...
    list_name: str | None = None
    program_code: str | None = None
    listed_on: date | None = None
    legal_basis: str | None = None
    reference_no: str | None = None
    measures: list[str] = Field(default_factory=list)
    reason: str | None = None
    remarks: str | None = None
    evidence_url: str | None = None


class Relationship(_M):
    rel_type: str
    target_source_key: str | None = None
    target_name: str | None = None
    raw: str | None = None


class VesselInfo(_M):
    vessel_type: str | None = None
    flag_iso2: str | None = None
    flag_raw: str | None = None
    tonnage: str | None = None
    build_year: int | None = None
    owner_operator_raw: str | None = None
    former_flags: list[str] = Field(default_factory=list)
    former_names: list[str] = Field(default_factory=list)


class AircraftInfo(_M):
    model: str | None = None
    manufacturer: str | None = None
    operator_raw: str | None = None
    build_year: int | None = None


class CanonicalRecord(_M):
    source_key: str  # the list's stable id (OFAC UID, UN reference number, UK UniqueID, EU logicalId, ...)
    entity_type: EntityType
    names: list[Name]
    identifiers: list[Identifier] = Field(default_factory=list)
    addresses: list[Address] = Field(default_factory=list)
    birth_dates: list[BirthDate] = Field(default_factory=list)
    birth_places: list[BirthPlace] = Field(default_factory=list)
    countries: list[Country] = Field(default_factory=list)
    listings: list[Listing] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    vessel: VesselInfo | None = None
    aircraft: AircraftInfo | None = None
    gender: str | None = None
    titles: list[str] = Field(default_factory=list)
    positions: list[str] = Field(default_factory=list)
    remarks: str | None = None
    source_updated_on: date | None = None  # per-record "last updated" where the list provides it
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def primary_name(self) -> str:
        for n in self.names:
            if n.name_type == "PRIMARY":
                return n.full_name
        return self.names[0].full_name if self.names else ""

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
