"""Identifier normalisation and checksums (IMO, LEI, MMSI) and mapping of publisher ID labels."""

from __future__ import annotations

import re

from sanctions_agent.canonical.model import Identifier, IdType
from sanctions_agent.canonical.normalize.countries import to_iso2

_IMO_RE = re.compile(r"(?<!\d)(\d{7})(?!\d)")


def normalize_id(value: str) -> str:
    return re.sub(r"[\s\-./]", "", value).upper()


def imo_checksum_ok(imo: str) -> bool:
    """IMO ship number: 7 digits; weighted sum of first six (7..2) mod 10 equals the last digit."""
    if not re.fullmatch(r"\d{7}", imo):
        return False
    total = sum(int(d) * w for d, w in zip(imo[:6], range(7, 1, -1), strict=True))
    return total % 10 == int(imo[6])


def extract_imo(raw: str) -> str | None:
    m = _IMO_RE.search(raw.replace(" ", "") if raw.upper().startswith("IMO") else raw)
    return m.group(1) if m else None


def lei_checksum_ok(lei: str) -> bool:
    """ISO 17442: 20 alphanumerics, ISO 7064 MOD 97-10 check (numeric value mod 97 == 1)."""
    lei = lei.upper()
    if not re.fullmatch(r"[0-9A-Z]{18}[0-9]{2}", lei):
        return False
    digits = "".join(str(int(ch, 36)) for ch in lei)
    return int(digits) % 97 == 1


def mmsi_ok(mmsi: str) -> bool:
    return bool(re.fullmatch(r"\d{9}", mmsi))


# Publisher labels -> canonical identifier types (lower-cased substring rules, first match wins).
_LABEL_RULES: list[tuple[str, IdType]] = [
    ("imo", "IMO"),
    ("vessel registration identification", "IMO"),
    ("mmsi", "MMSI"),
    ("call sign", "CALL_SIGN"),
    ("legal entity number", "LEI"),
    ("lei", "LEI"),
    ("swift", "SWIFT"),
    ("bic", "SWIFT"),
    ("passport", "PASSPORT"),
    ("national id", "NATIONAL_ID"),
    ("national identification", "NATIONAL_ID"),
    ("identity card", "NATIONAL_ID"),
    ("personal id", "NATIONAL_ID"),
    ("cedula", "NATIONAL_ID"),
    ("tax", "TAX"),
    ("inn", "TAX"),
    ("kpp", "TAX"),
    ("vat", "TAX"),
    ("registration", "REGISTRATION"),
    ("company number", "REGISTRATION"),
    ("ogrn", "REGISTRATION"),
    ("business number", "REGISTRATION"),
    ("unified social credit", "REGISTRATION"),
    ("serial number", "AIRCRAFT_MSN"),
    ("msn", "AIRCRAFT_MSN"),
    ("tail number", "AIRCRAFT_TAIL"),
    ("email", "EMAIL"),
    ("website", "WEBSITE"),
]


def classify_label(label: str | None) -> IdType:
    if not label:
        return "OTHER"
    low = label.lower()
    if "aircraft" in low and "serial" in low:
        return "AIRCRAFT_MSN"
    for needle, t in _LABEL_RULES:
        if re.search(rf"(?<![a-z]){re.escape(needle)}(?![a-z])", low):
            return t
    return "OTHER"


def make_identifier(
    label: str | None,
    value: str,
    *,
    id_type: IdType | None = None,
    country_raw: str | None = None,
    issued_on: str | None = None,
    expires_on: str | None = None,
) -> Identifier | None:
    value = (value or "").strip()
    if not value:
        return None
    t = id_type or classify_label(label)
    norm = normalize_id(value)
    checksum: bool | None = None
    if t == "IMO":
        imo = extract_imo(value)
        norm = imo or norm
        checksum = imo_checksum_ok(imo) if imo else False
    elif t == "LEI":
        checksum = lei_checksum_ok(norm)
    elif t == "MMSI":
        checksum = mmsi_ok(norm)
    return Identifier(
        id_type=t,
        label=label,
        value=value,
        value_norm=norm,
        country_iso2=to_iso2(country_raw),
        country_raw=country_raw,
        issued_on=issued_on,
        expires_on=expires_on,
        checksum_valid=checksum,
    )
