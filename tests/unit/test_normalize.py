from datetime import date

import pytest

from sanctions_agent.canonical.model import CanonicalRecord, Name
from sanctions_agent.canonical.normalize.countries import to_iso2
from sanctions_agent.canonical.normalize.dates import parse_birth_date, parse_date
from sanctions_agent.canonical.normalize.identifiers import (
    classify_label,
    imo_checksum_ok,
    lei_checksum_ok,
    make_identifier,
)
from sanctions_agent.canonical.normalize.names import normalize_name


@pytest.mark.parametrize(
    ("raw", "iso"),
    [
        ("Iran (Islamic Republic of)", "IR"),
        ("Iran", "IR"),
        ("Russian Federation", "RU"),
        ("RUS", "RU"),
        ("Democratic People's Republic of Korea", "KP"),
        ("Korea, North", "KP"),
        ("Syrian Arab Republic", "SY"),
        ("Türkiye", "TR"),
        ("Burma", "MM"),
        ("United Kingdom", "GB"),
        ("GB", "GB"),
        ("Viet Nam", "VN"),
        ("Côte d'Ivoire", "CI"),
        ("Congo, Democratic Republic of the", "CD"),
        ("Tehran, Iran", "IR"),
        ("Kosovo", "XK"),
        ("Crimea", "UA"),
        ("  germany ", "DE"),
        ("Hong Kong", "HK"),
        ("Atlantis", None),
        ("", None),
        (None, None),
    ],
)
def test_country_mapping(raw, iso):
    assert to_iso2(raw) == iso


@pytest.mark.parametrize(
    ("raw", "precision", "value", "year"),
    [
        ("1970-01-31", "DAY", date(1970, 1, 31), 1970),
        ("31/01/1970", "DAY", date(1970, 1, 31), 1970),
        ("dd/01/1970", "MONTH", date(1970, 1, 1), 1970),  # UK placeholder
        ("00/00/1970", "YEAR", None, 1970),  # UK placeholder
        ("dd/mm/1970", "YEAR", None, 1970),
        ("01 Jan 1970", "DAY", date(1970, 1, 1), 1970),  # OFAC style
        ("Sept 1965", "MONTH", date(1965, 9, 1), 1965),
        ("1962", "YEAR", None, 1962),
        ("circa 1958", "YEAR", None, 1958),
        ("1970-00-00", "YEAR", None, 1970),
        ("31/02/1970", "UNKNOWN", None, 1970),  # impossible date is not invented
    ],
)
def test_birth_dates(raw, precision, value, year):
    bd = parse_birth_date(raw)
    assert bd.precision == precision and bd.date_value == value and bd.year == year and bd.raw == raw


def test_birth_date_ranges_and_garbage():
    bd = parse_birth_date("Between 1965 and 1969")
    assert bd.precision == "RANGE" and (bd.year_from, bd.year_to) == (1965, 1969)
    assert parse_birth_date("1965 to 1969").precision == "RANGE"
    assert parse_birth_date("approximately in the sixties").precision == "UNKNOWN"
    assert parse_birth_date("") is None
    assert parse_date("2024-05-01T00:00:00") == date(2024, 5, 1)


def _lei(base18: str) -> str:
    n = int("".join(str(int(c, 36)) for c in base18 + "00"))
    return base18 + f"{98 - n % 97:02d}"


def test_identifier_checksums():
    assert imo_checksum_ok("9074729") and not imo_checksum_ok("9074728") and not imo_checksum_ok("907472")
    good = _lei("5493001KJTIIGC8Y1R")
    assert lei_checksum_ok(good) and not lei_checksum_ok(good[:-1] + str((int(good[-1]) + 1) % 10))
    imo = make_identifier("Vessel Registration Identification", "IMO 9074729")
    assert imo.id_type == "IMO" and imo.value_norm == "9074729" and imo.checksum_valid
    bad = make_identifier(None, "IMO 1234568", id_type="IMO")
    assert bad.checksum_valid is False


@pytest.mark.parametrize(
    ("label", "t"),
    [
        ("Passport", "PASSPORT"),
        ("National ID No.", "NATIONAL_ID"),
        ("Registration Number", "REGISTRATION"),
        ("Tax ID No.", "TAX"),
        ("SWIFT/BIC", "SWIFT"),
        ("MMSI", "MMSI"),
        ("Call Sign", "CALL_SIGN"),
        ("Legal Entity Number", "LEI"),
        ("Aircraft Manufacturer's Serial Number (MSN)", "AIRCRAFT_MSN"),
        ("Aircraft Tail Number", "AIRCRAFT_TAIL"),
        ("Email Address", "EMAIL"),
        ("Gender", "OTHER"),
        ("Vessel Registration Identification", "IMO"),
        ("Linked To", "OTHER"),
    ],
)
def test_label_classification(label, t):
    assert classify_label(label) == t


def test_name_normalisation_and_hash_stability():
    assert normalize_name("  Muḥammad  AL-ʿAlī ") == "muhammad al ali"
    r1 = CanonicalRecord(source_key="1", entity_type="PERSON", names=[Name(full_name="A B")])
    r2 = CanonicalRecord(source_key="1", entity_type="PERSON", names=[Name(full_name="A B")])
    assert r1.content_hash() == r2.content_hash()
    r2.names[0].quality = "WEAK"
    assert r1.content_hash() != r2.content_hash()
