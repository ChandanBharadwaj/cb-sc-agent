import json

from agents.testing import ModelStep, ScriptedModel, assistant_message
from agents.usage import Usage

from sanctions_agent.agent.extractors import Extraction, Extractor, verify
from sanctions_agent.db.engine import fetch_one, tx
from tests.integration.test_pipeline import seeded  # noqa: F401

TEXT = """ANNEX XLII - List of vessels referred to in Article 3s
The following entries are added:
1. ARCTIC DAWN, IMO 9074729, flag: Cameroon
2. OCEAN STAR, IMO 9321483, flag: Gabon
(2 vessels added)"""


def ex(entries, stated=None):
    return Extraction.model_validate({"entries": entries, "stated_total": stated, "notes": ""})


def entry(name, imo, quote=None):
    return {
        "name": name,
        "entity_type": "VESSEL",
        "action": "LISTING",
        "identifiers": [{"id_type": "IMO", "value": imo}],
        "program": "XLII",
        "reason": None,
        "verbatim_quote": quote or f"{name}, IMO {imo}",
    }


def test_verifier_accepts_faithful_extraction():
    res = verify(
        TEXT,
        ex([entry("ARCTIC DAWN", "9074729"), entry("OCEAN STAR", "9321483")], stated=2),
        expect_imo_rows=True,
    )
    assert res.all_ok, [e.problems for e in res.entries]


def test_verifier_rejects_hallucinated_identifier_and_quote():
    res = verify(TEXT, ex([entry("ARCTIC DAWN", "9074728", quote="ARCTIC DAWN, IMO 9074728")]))
    problems = res.entries[0].problems
    assert not res.all_ok
    assert any("not found in source text" in p for p in problems) and any("checksum" in p for p in problems)


def test_verifier_detects_dropped_vessel_and_count_mismatch():
    res = verify(TEXT, ex([entry("ARCTIC DAWN", "9074729")], stated=2), expect_imo_rows=True)
    assert not res.count_check["ok"] and res.count_check["missing_imos"] == ["9321483"]


def test_extractor_runs_and_records_cycle(seeded):  # noqa: F811
    payload = {
        "entries": [entry("ARCTIC DAWN", "9074729"), entry("OCEAN STAR", "9321483")],
        "stated_total": 2,
        "notes": "",
    }
    model = ScriptedModel(
        [
            ModelStep(
                output=[assistant_message(json.dumps(payload))],
                usage=Usage(requests=1, input_tokens=900, output_tokens=200, total_tokens=1100),
            )
        ]
    )
    extraction, verification, cycle_id = Extractor(model=model).extract(
        TEXT, purpose="annex_xlii", subject_ref="celex:32026R0001", expect_imo_rows=True
    )
    assert len(extraction.entries) == 2 and verification.all_ok
    with tx() as conn:
        cyc = fetch_one(conn, "SELECT * FROM agent_cycle WHERE cycle_id = %s", (cycle_id,))
    assert (
        cyc["agent_name"] == "extractor"
        and cyc["status"] == "SUCCEEDED"
        and cyc["report"]["verified_ok"] == 2
    )
