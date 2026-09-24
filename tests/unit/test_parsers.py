"""Parser tests against schema-faithful fixtures (see tests/fixtures/*). Live conformance is checked
separately with `sanctions-agent verify-sources` on a network that can reach the publishers."""

from pathlib import Path

import pytest

from sanctions_agent.sources.base import ParseStats, load_known_paths
from sanctions_agent.sources.l1.eu_fsf import EuFsfAdapter
from sanctions_agent.sources.l1.ofac_advanced import OfacAdvancedAdapter
from sanctions_agent.sources.l1.uk_fcdo import UkFcdoAdapter
from sanctions_agent.sources.l1.un_consolidated import UnConsolidatedAdapter
from sanctions_agent.sources.l1.us_csl import UsCslAdapter, list_code

FX = Path(__file__).resolve().parents[1] / "fixtures"


def parse(adapter, path):
    st = ParseStats()
    return {r.source_key: r for r in adapter.parse(path, st)}, st


def test_ofac_entities_names_ids_and_measures():
    recs, st = parse(OfacAdvancedAdapter(), FX / "ofac/sdn_advanced_v1.xml")
    assert set(recs) == {"36001", "36002", "36003", "36004"} and st.skipped_records == 0
    p = recs["36001"]
    assert p.entity_type == "PERSON" and p.primary_name == "Sirajuddin HAQQANI"
    weak = [n for n in p.names if n.quality == "WEAK"]
    assert [n.full_name for n in weak] == ["KHALIFA"]  # LowQuality="true" preserved (FR-11)
    assert p.birth_dates[0].precision == "YEAR" and p.birth_dates[0].raw.startswith("circa")
    assert p.countries[0].country_iso2 == "AF" and p.addresses[0].city == "Kabul"
    passport = next(i for i in p.identifiers if i.id_type == "PASSPORT")
    assert (
        passport.value_norm == "OA123456"
        and passport.country_iso2 == "AF"
        and passport.expires_on == "2030-01-15"
    )
    assert p.listings[0].measures == ["ASSET_FREEZE"] and p.listings[0].program_code == "SDGT"
    org = recs["36002"]
    assert {lst.program_code for lst in org.listings} == {"IRAN-EO13846", "SDGT"}
    assert any(n.script == "Cyrl" for n in org.names) and any(n.name_type == "FKA" for n in org.names)
    assert next(i for i in org.identifiers if i.id_type == "LEI").checksum_valid
    v = recs["36003"]
    assert (
        v.entity_type == "VESSEL"
        and v.vessel.flag_iso2 == "PA"
        and v.vessel.vessel_type == "Crude Oil Tanker"
    )
    assert {i.id_type for i in v.identifiers} >= {"IMO", "MMSI", "CALL_SIGN"}
    assert v.relationships[0].target_source_key == "36002"
    a = recs["36004"]
    assert a.entity_type == "AIRCRAFT" and a.aircraft.model == "Airbus A340-642"
    assert {i.id_type for i in a.identifiers} >= {"AIRCRAFT_MSN", "AIRCRAFT_TAIL"}


def test_ofac_parser_survives_namespace_change(tmp_path):
    """OFAC changed XML namespaces in May 2024 and broke strict parsers (BRD 9.3)."""
    src = (FX / "ofac/sdn_advanced_v1.xml").read_text(encoding="utf-8")
    old = src.replace(
        "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ADVANCED_XML",
        "http://www.un.org/sanctions/1.0",
    )
    p = tmp_path / "old_ns.xml"
    p.write_text(old, encoding="utf-8")
    new_recs, _ = parse(OfacAdvancedAdapter(), FX / "ofac/sdn_advanced_v1.xml")
    old_recs, _ = parse(OfacAdvancedAdapter(), p)
    assert {k: r.content_hash() for k, r in new_recs.items()} == {
        k: r.content_hash() for k, r in old_recs.items()
    }


def test_ofac_marker():
    info = OfacAdvancedAdapter().read_info(FX / "ofac/sdn_advanced_v1.xml")
    assert info.publication_marker == "2026-09-23"


def test_un_quality_dates_and_marker():
    a = UnConsolidatedAdapter()
    recs, st = parse(a, FX / "un/consolidated_v1.xml")
    assert len(recs) == 5
    ri = recs["KPi.033"]
    assert [n.quality for n in ri.names if n.name_type == "AKA"] == ["STRONG", "WEAK"]
    assert ri.names[1].full_name == "리원호"
    assert recs["TAi.080"].birth_dates[0].precision == "RANGE"
    assert recs["CDi.099"].birth_dates[0].raw == "approximately 1975"
    assert recs["QDe.109"].entity_type == "ORGANIZATION"
    assert {n.name_type for n in recs["QDe.109"].names} == {"PRIMARY", "AKA", "FKA"}
    assert recs["TAi.080"].listings[0].measures == []  # UN file does not state measures; none invented
    assert st.unmapped_countries == {"Atlantis": 1}
    assert a.read_info(FX / "un/consolidated_v1.xml").publication_marker == "2026-09-20T10:00:05.000Z"


def test_uk_measures_placeholders_and_ship():
    recs, _ = parse(UkFcdoAdapter(), FX / "uk/uk_sanctions_list_v1.xml")
    p = recs["RUS1234"]
    assert p.listings[0].measures == ["ASSET_FREEZE", "DIRECTOR_DISQUALIFICATION", "TRAVEL_BAN"]
    assert [b.precision for b in p.birth_dates] == ["YEAR", "DAY"]
    assert [n.quality for n in p.names if n.name_type == "AKA"] == ["STRONG", "WEAK"]
    ship = recs["RUS3001"]
    assert (
        ship.entity_type == "VESSEL"
        and ship.vessel.former_flags == ["Gabon"]
        and ship.vessel.build_year == 2004
    )
    assert set(ship.listings[0].measures) == {"PORT_BAN", "CHARTERING_BAN"}
    assert next(i for i in recs["AFG0080"].identifiers if i.id_type == "UN_REF").value == "TAi.080"
    # UK (no indicators element) falls back to SanctionsImposed text
    assert recs["AFG0080"].listings[0].measures == ["ARMS_EMBARGO", "ASSET_FREEZE", "TRAVEL_BAN"]


def test_eu_strong_flag_and_evidence_links():
    recs, _ = parse(EuFsfAdapter(), FX / "eu/fsf_v1.xml")
    p = recs["130001"]
    assert [n.quality for n in p.names] == ["STRONG", "STRONG", "WEAK"]
    assert p.listings[0].evidence_url.startswith("https://eur-lex.europa.eu/")
    assert p.birth_places[0].place == "Leningrad"
    org = recs["130002"]
    assert next(i for i in org.identifiers if i.id_type == "UN_REF").value == "QDe.109"
    assert (
        next(i for i in org.identifiers if i.id_type == "REGISTRATION").country_iso2 is None
    )  # "00" = unknown


def test_csl_keeps_only_export_control_lists():
    recs, _ = parse(UsCslAdapter(), FX / "csl/consolidated_v1.json")
    codes = {r.listings[0].program_code for r in recs.values()}
    assert codes == {"EL", "DPL", "UVL", "MEU", "DTC"}  # SDN excluded: OFAC is ingested directly
    el = recs["e0f1a2b3c4d5"]
    assert el.entity_type == "UNKNOWN" and el.listings[0].measures == ["LICENSE_REQUIREMENT"]
    assert el.listings[0].legal_basis == "89 FR 12345"
    assert list_code("Entity List (EL) - Bureau of Industry and Security") == "EL"


@pytest.mark.parametrize(
    ("adapter", "fixture"),
    [
        (OfacAdvancedAdapter(), "ofac/sdn_advanced_v1.xml"),
        (UnConsolidatedAdapter(), "un/consolidated_v1.xml"),
        (UkFcdoAdapter(), "uk/uk_sanctions_list_v1.xml"),
        (EuFsfAdapter(), "eu/fsf_v1.xml"),
        (UsCslAdapter(), "csl/consolidated_v1.json"),
    ],
)
def test_fixture_paths_are_known(adapter, fixture):
    _, st = parse(adapter, FX / fixture)
    unknown = set(st.paths) - load_known_paths(adapter.type_id)
    assert not unknown


def test_drift_is_detected_for_new_elements(tmp_path):
    src = (FX / "un/consolidated_v1.xml").read_text(encoding="utf-8")
    p = tmp_path / "drift.xml"
    p.write_text(
        src.replace("<VERSIONNUM>1</VERSIONNUM>", "<VERSIONNUM>1</VERSIONNUM><RISK_SCORE>9</RISK_SCORE>", 1),
        encoding="utf-8",
    )
    _, st = parse(UnConsolidatedAdapter(), p)
    unknown = set(st.paths) - load_known_paths("un_consolidated_xml")
    assert unknown == {"/CONSOLIDATED_LIST/INDIVIDUALS/INDIVIDUAL/RISK_SCORE"}


def test_malformed_xml_fails_validation(tmp_path):
    p = tmp_path / "bad.xml"
    p.write_text("<CONSOLIDATED_LIST><INDIVIDUALS>", encoding="utf-8")
    issues = UnConsolidatedAdapter().validate_file(p)
    assert issues and issues[0].severity == "FAIL"


def test_xxe_entities_are_not_resolved(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET")
    p = tmp_path / "xxe.xml"
    p.write_text(
        f'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file://{secret}">]>'
        "<CONSOLIDATED_LIST><INDIVIDUALS><INDIVIDUAL><REFERENCE_NUMBER>X.1</REFERENCE_NUMBER>"
        "<FIRST_NAME>&x;</FIRST_NAME></INDIVIDUAL></INDIVIDUALS></CONSOLIDATED_LIST>",
        encoding="utf-8",
    )
    recs, _ = parse(UnConsolidatedAdapter(), p)
    assert all("TOPSECRET" not in r.canonical_json() for r in recs.values())
