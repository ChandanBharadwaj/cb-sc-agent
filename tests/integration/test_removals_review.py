import httpx
import pytest
import respx

from sanctions_agent import review
from sanctions_agent.db.engine import fetch_all, fetch_one, fetch_val, tx
from sanctions_agent.pipeline import removals
from sanctions_agent.pipeline.runner import PipelineRunner, run_once
from sanctions_agent.sources.config_service import PermissionDenied
from tests.integration.test_pipeline import BLOB, FX, UN_URL, seeded  # noqa: F401  (fixture re-export)

UK_URL = "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml"


def _run(source_id):
    return run_once(source_id, runner=PipelineRunner(sleep=lambda s: None))


def _serve_un(body: bytes):
    respx.get(UN_URL).mock(return_value=httpx.Response(302, headers={"Location": BLOB}))
    respx.get(BLOB).mock(return_value=httpx.Response(200, content=body))


def _notice(conn, key, source_id="un_sc", provider="un_list_updates"):
    nid = fetch_val(
        conn,
        "INSERT INTO legal_notice (provider, external_id, title, published_on, url)"
        " VALUES (%s, %s, 'De-listing', current_date, 'https://main.un.org/x') RETURNING notice_id",
        (provider, f"n-{key}"),
    )
    conn.execute(
        "INSERT INTO notice_link (notice_id, source_id, source_key, link_type, method, confidence, status,"
        " verbatim_quote) VALUES (%s, %s, %s, 'DELISTING', 'ID_MATCH', 1.0, 'AUTO', %s)",
        (nid, source_id, key, f"{key} removed"),
    )
    return nid


@respx.mock
def test_removal_confirmed_with_evidence_releases_hold(seeded):  # noqa: F811
    _serve_un((FX / "un/consolidated_v1.xml").read_bytes())
    _run("un_sc")
    respx.reset()
    _serve_un((FX / "un/consolidated_v2.xml").read_bytes())
    _run("un_sc")
    with tx() as conn:
        cand = fetch_one(conn, "SELECT * FROM removal_candidate WHERE source_key = 'CDi.099'")
        out = removals.assess(conn, cand["candidate_id"])
        assert out["recommendation"] is None  # no evidence yet -> nothing to confirm, keeps blocking
        _notice(conn, "CDi.099")
        out = removals.assess(conn, cand["candidate_id"])
    assert out["status"] == "EVIDENCE_FOUND" and out["recommendation"] == "CONFIRMED"
    with tx() as conn, pytest.raises(PermissionDenied):
        review.decide(
            conn, out["proposed_change_id"], reviewer="system:removals", role="reviewer", approve=True
        )
    with tx() as conn, pytest.raises(PermissionDenied):
        review.decide(conn, out["proposed_change_id"], reviewer="ops", role="operator", approve=True)
    with tx(actor="bob") as conn:
        res = review.decide(
            conn,
            out["proposed_change_id"],
            reviewer="bob",
            role="reviewer",
            approve=True,
            comment="UN de-listing notice verified",
        )
    assert res["resolution"] == "CONFIRMED"
    with tx() as conn:
        rows = {
            r["source_key"] for r in fetch_all(conn, "SELECT * FROM records_as_of(%s)", (res["snapshot_id"],))
        }
        assert "CDi.099" not in rows and "QDi.500" in rows
        assert (
            fetch_val(conn, "SELECT decided_by FROM removal_candidate WHERE source_key = 'CDi.099'") == "bob"
        )


@respx.mock
def test_removal_still_listed_elsewhere(seeded):  # noqa: F811
    respx.get(UK_URL).mock(
        return_value=httpx.Response(200, content=(FX / "uk/uk_sanctions_list_v1.xml").read_bytes())
    )
    _serve_un((FX / "un/consolidated_v1.xml").read_bytes())
    _run("un_sc")
    _run("uk_fcdo")
    respx.reset()
    body = (FX / "un/consolidated_v1.xml").read_text()
    start = body.index("    <INDIVIDUAL>\n      <DATAID>2984562</DATAID>")
    end = body.index("</INDIVIDUAL>", start) + len("</INDIVIDUAL>\n")
    _serve_un((body[:start] + body[end:]).replace("2026-09-20T10", "2026-09-21T10").encode())
    assert _run("un_sc")[1] == "SUCCEEDED"
    with tx() as conn:
        cand = fetch_one(conn, "SELECT * FROM removal_candidate WHERE source_key = 'TAi.080'")
        _notice(conn, "TAi.080")
        out = removals.assess(conn, cand["candidate_id"])
    assert out["recommendation"] == "STILL_LISTED_ELSEWHERE" and out["crosslist_hits"] == 1
    with tx() as conn:
        prop = fetch_one(
            conn, "SELECT * FROM proposed_change WHERE change_id = %s", (out["proposed_change_id"],)
        )
        assert prop["payload"]["crosslist_hits"][0]["source_id"] == "uk_fcdo"


@respx.mock
def test_id_change_is_suspected_not_confirmed(seeded):  # noqa: F811
    _serve_un((FX / "un/consolidated_v1.xml").read_bytes())
    _run("un_sc")
    respx.reset()
    moved = (
        (FX / "un/consolidated_v1.xml")
        .read_text()
        .replace("CDi.099", "CDi.100")
        .replace("2026-09-20T10", "2026-09-21T10")
    )
    _serve_un(moved.encode())
    _run("un_sc")
    with tx() as conn:
        cand = fetch_one(conn, "SELECT * FROM removal_candidate WHERE source_key = 'CDi.099'")
        out = removals.assess(conn, cand["candidate_id"])
    assert out["recommendation"] == "REJECTED_ID_CHANGE" and out["id_change_suspects"] >= 1


@respx.mock
def test_large_change_release_via_review_queues_run(seeded):  # noqa: F811
    _serve_un((FX / "un/consolidated_v1.xml").read_bytes())
    _run("un_sc")
    with tx() as conn:
        conn.execute("""UPDATE source SET config = jsonb_set(config, '{validation,max_removed_abs}', '0')
                        WHERE source_id = 'un_sc'""")
    respx.reset()
    _serve_un((FX / "un/consolidated_v2.xml").read_bytes())
    assert _run("un_sc")[1] == "HELD"
    with tx() as conn:
        cid = fetch_val(conn, "SELECT change_id FROM proposed_change WHERE kind = 'LARGE_CHANGE_RELEASE'")
    with tx(actor="carol") as conn:
        res = review.decide(
            conn, cid, reviewer="carol", role="reviewer", approve=True, comment="matches UN press release"
        )
    assert res["run_id"]
    with tx() as conn:
        run = fetch_one(conn, "SELECT * FROM ingestion_run WHERE run_id = %s", (res["run_id"],))
        assert (
            run["run_kind"] == "RELEASE_HELD" and run["status"] == "QUEUED" and run["requested_by"] == "carol"
        )
