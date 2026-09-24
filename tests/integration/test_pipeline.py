"""End-to-end pipeline tests against real Postgres with mocked publisher endpoints."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from sanctions_agent.db.engine import fetch_all, fetch_one, fetch_val, tx
from sanctions_agent.pipeline import runs
from sanctions_agent.pipeline.runner import PipelineRunner, run_once
from sanctions_agent.settings import get_settings
from sanctions_agent.sources.registry import import_seed

FX = Path(__file__).resolve().parents[1] / "fixtures"
UN_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
BLOB = "https://unsc.blob.core.windows.net/list/consolidated.xml?sv=2024&sig=SECRET"
OFAC_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML"


@pytest.fixture()
def seeded(db):
    with tx(actor="seed") as conn:
        import_seed(conn, get_settings().sources_seed_file, actor="seed")
        # fixtures are small: relax volume floors for the test sources
        conn.execute("""UPDATE source SET config = jsonb_set(config, '{validation,min_records}', '1')
                        WHERE source_id IN ('un_sc','ofac_sdn','uk_fcdo','eu_fsf','us_csl')""")
        conn.execute("""UPDATE source SET config = jsonb_set(config, '{validation,schema_file}', 'null')
                        WHERE source_id IN ('ofac_sdn','uk_fcdo','eu_fsf')""")
        # a single removal is 20% of a 5-record fixture: loosen the percentage bound (the hold test tightens it)
        conn.execute("""UPDATE source SET config = jsonb_set(config, '{validation,max_removed_pct}', '50')
                        WHERE source_id = 'un_sc'""")


def runner() -> PipelineRunner:
    return PipelineRunner(sleep=lambda s: None)


def serve_un(body: bytes, status: int = 200):
    respx.get(UN_URL).mock(return_value=httpx.Response(302, headers={"Location": BLOB}))
    return respx.get(BLOB).mock(return_value=httpx.Response(status, content=body))


@respx.mock
def test_first_load_publishes_with_evidence_and_snapshot(seeded):
    serve_un((FX / "un/consolidated_v1.xml").read_bytes())
    run_id, status = run_once("un_sc", runner=runner())
    assert status == "SUCCEEDED"
    with tx() as conn:
        ver = fetch_one(conn, "SELECT * FROM list_version WHERE source_id = 'un_sc'")
        assert ver["status"] == "PUBLISHED" and ver["record_count"] == 5
        assert ver["publication_marker"] == "2026-09-20T10:00:05.000Z"
        assert ver["change_summary"]["added"] == 5
        ev = fetch_one(conn, "SELECT * FROM fetch_evidence WHERE run_id = %s", (run_id,))
        assert ev["http_status"] == 200 and ev["sha256"] == ver["raw_sha256"]
        assert "SECRET" not in ev["final_url"] and len(ev["redirect_chain"]) == 2
        steps = [
            r["step"]
            for r in fetch_all(
                conn,
                "SELECT step FROM run_step WHERE run_id = %s AND status = 'DONE' ORDER BY started_at",
                (run_id,),
            )
        ]
        assert steps == ["FETCH", "ARCHIVE", "VALIDATE_FILE", "PARSE", "VALIDATE_DATA", "DIFF_PUBLISH"]
        assert fetch_val(conn, "SELECT count(*) FROM rv_name") >= 10
        assert fetch_val(conn, "SELECT count(*) FROM rv_name WHERE quality = 'WEAK'") == 2
        assert fetch_val(conn, "SELECT count(*) FROM staging_record") == 0  # purged after publish
        snap = fetch_val(conn, "SELECT max(snapshot_id) FROM screening_snapshot")
        assert fetch_val(conn, "SELECT count(*) FROM records_as_of(%s)", (snap,)) == 5
        assert fetch_val(conn, "SELECT count(*) FROM crosslist_key WHERE key_type = 'UN_REF'") == 5
        assert (
            fetch_val(
                conn,
                "SELECT count(*) FROM dq_metric WHERE version_id = %s AND metric = 'fill_rate'",
                (ver["version_id"],),
            )
            > 10
        )
        src = fetch_one(conn, "SELECT * FROM source WHERE source_id = 'un_sc'")
        assert (
            src["current_version_id"] == ver["version_id"] and src["last_success_at"] and src["next_due_at"]
        )


@respx.mock
def test_unchanged_file_is_no_change(seeded):
    serve_un((FX / "un/consolidated_v1.xml").read_bytes())
    run_once("un_sc", runner=runner())
    _, status = run_once("un_sc", runner=runner())
    assert status == "NO_CHANGE"
    with tx() as conn:
        assert fetch_val(conn, "SELECT count(*) FROM list_version WHERE source_id = 'un_sc'") == 1


@respx.mock
def test_diff_add_change_remove_and_removal_keeps_blocking(seeded):
    serve_un((FX / "un/consolidated_v1.xml").read_bytes())
    run_once("un_sc", runner=runner())
    s1 = None
    with tx() as conn:
        s1 = fetch_val(conn, "SELECT max(snapshot_id) FROM screening_snapshot")
    respx.reset()
    serve_un((FX / "un/consolidated_v2.xml").read_bytes())
    _, status = run_once("un_sc", runner=runner())
    assert status == "SUCCEEDED"
    with tx() as conn:
        ev = {
            r["source_key"]: r["change_type"]
            for r in fetch_all(
                conn,
                "SELECT source_key, change_type FROM change_event WHERE version_id ="
                " (SELECT max(version_id) FROM list_version WHERE source_id = 'un_sc')",
            )
        }
        assert ev == {"TAi.080": "CHANGE", "CDi.099": "REMOVE", "QDi.500": "ADD"}
        diff = fetch_val(
            conn,
            "SELECT field_diff FROM change_event WHERE source_key = 'TAi.080' AND change_type = 'CHANGE'",
        )
        assert "names" in diff["fields"] and "Abdul Rahman Zahid Mullah" in diff["names_added"]
        cand = fetch_one(conn, "SELECT * FROM removal_candidate WHERE source_key = 'CDi.099'")
        assert cand["status"] == "PENDING"
        s2 = fetch_val(conn, "SELECT max(snapshot_id) FROM screening_snapshot")
        rows = {
            r["source_key"]: r["removal_pending"]
            for r in fetch_all(conn, "SELECT * FROM records_as_of(%s)", (s2,))
        }
        assert rows["CDi.099"] is True  # removed from file but still blocking until confirmed
        assert "QDi.500" in rows and len(rows) == 6
        # the old snapshot still reproduces exactly what screening used before
        old = {r["source_key"] for r in fetch_all(conn, "SELECT * FROM records_as_of(%s)", (s1,))}
        assert old == {"KPi.033", "TAi.080", "CDi.099", "QDe.109", "KPe.001"}
        assert fetch_val(conn, "SELECT count(*) FROM record_version WHERE source_key = 'TAi.080'") == 2


@respx.mock
def test_relist_closes_removal_candidate(seeded):
    serve_un((FX / "un/consolidated_v1.xml").read_bytes())
    run_once("un_sc", runner=runner())
    respx.reset()
    serve_un((FX / "un/consolidated_v2.xml").read_bytes())
    run_once("un_sc", runner=runner())
    respx.reset()
    v3 = (FX / "un/consolidated_v1.xml").read_text().replace("2026-09-20T10:00:05", "2026-09-23T10:00:00")
    serve_un(v3.encode())
    _, status = run_once("un_sc", runner=runner())
    assert status == "SUCCEEDED"
    with tx() as conn:
        assert (
            fetch_val(
                conn,
                "SELECT change_type FROM change_event WHERE source_key = 'CDi.099' ORDER BY event_id DESC"
                " LIMIT 1",
            )
            == "RELIST"
        )
        assert (
            fetch_val(conn, "SELECT status FROM removal_candidate WHERE source_key = 'CDi.099'") == "RELISTED"
        )


@respx.mock
def test_large_removal_is_held_and_release_publishes(seeded):
    serve_un((FX / "un/consolidated_v1.xml").read_bytes())
    run_once("un_sc", runner=runner())
    with tx() as conn:
        conn.execute("""UPDATE source SET config = jsonb_set(jsonb_set(config, '{validation,max_removed_abs}', '1'),
                        '{validation,max_removed_pct}', '10') WHERE source_id = 'un_sc'""")
    respx.reset()
    src = (FX / "un/consolidated_v1.xml").read_text()
    start = src.index("<ENTITIES>")
    gutted = src[:start] + "<ENTITIES/>\n</CONSOLIDATED_LIST>\n"
    serve_un(gutted.replace("2026-09-20T10:00:05", "2026-09-21T10:00:00").encode())
    run_id, status = run_once("un_sc", runner=runner())
    assert status == "HELD"
    with tx() as conn:
        held = fetch_one(conn, "SELECT * FROM list_version WHERE status = 'HELD'")
        assert held["record_count"] == 3
        prop = fetch_one(conn, "SELECT * FROM proposed_change WHERE kind = 'LARGE_CHANGE_RELEASE'")
        assert prop["status"] == "PENDING" and prop["payload"]["diff_preview"]["removed"] == 2
        assert (
            fetch_val(conn, "SELECT count(*) FROM record_version WHERE valid_to_seq IS NULL") == 5
        )  # last good live
        assert (
            fetch_val(conn, "SELECT count(*) FROM staging_record WHERE run_id = %s", (run_id,)) == 3
        )  # retained
        inc = fetch_one(conn, "SELECT * FROM incident WHERE source_id = 'un_sc' AND status = 'OPEN'")
        assert inc["error_class"] == "COUNT_ANOMALY"
    _, status = run_once(
        "un_sc",
        runner=runner(),
        run_kind="RELEASE_HELD",
        requested_by="reviewer:bob",
        options={"version_id": held["version_id"]},
    )
    assert status == "SUCCEEDED"
    with tx() as conn:
        v = fetch_one(conn, "SELECT * FROM list_version WHERE version_id = %s", (held["version_id"],))
        assert v["status"] == "PUBLISHED" and v["released_by"] == "reviewer:bob"
        assert fetch_val(conn, "SELECT count(*) FROM removal_candidate WHERE status = 'PENDING'") == 2


@respx.mock
def test_malformed_file_is_quarantined_and_last_good_kept(seeded):
    serve_un((FX / "un/consolidated_v1.xml").read_bytes())
    run_once("un_sc", runner=runner())
    respx.reset()
    serve_un(b"<CONSOLIDATED_LIST><INDIVIDUALS><INDIVIDUAL>")
    _, status = run_once("un_sc", runner=runner())
    assert status == "QUARANTINED"
    with tx() as conn:
        assert fetch_val(conn, "SELECT count(*) FROM list_version WHERE status = 'PUBLISHED'") == 1
        assert fetch_val(conn, "SELECT status FROM list_version ORDER BY seq DESC LIMIT 1") == "QUARANTINED"
        inc = fetch_one(conn, "SELECT * FROM incident WHERE source_id = 'un_sc' AND status = 'OPEN'")
        assert inc["severity"] == "PAGE" and inc["error_class"] == "SCHEMA_INVALID"
        assert fetch_val(conn, "SELECT count(*) FROM dq_issue WHERE category = 'SCHEMA_INVALID'") == 1


@respx.mock
def test_fill_rate_floor_quarantines(seeded):
    serve_un((FX / "un/consolidated_v1.xml").read_bytes())
    with tx() as conn:
        conn.execute("""UPDATE source SET config = jsonb_set(config, '{validation,fill_floors}', '{"PERSON.passport": 0.9}')
                        WHERE source_id = 'un_sc'""")
    _, status = run_once("un_sc", runner=runner())
    assert status == "QUARANTINED"
    with tx() as conn:
        issue = fetch_one(conn, "SELECT * FROM dq_issue WHERE category = 'FILL_RATE_BELOW_FLOOR'")
        assert issue["severity"] == "FAIL" and issue["field"] == "passport"


@respx.mock
def test_schema_drift_warns_but_publishes(seeded):
    body = (
        (FX / "un/consolidated_v1.xml")
        .read_text()
        .replace("<VERSIONNUM>1</VERSIONNUM>", "<VERSIONNUM>1</VERSIONNUM><RISK_SCORE>9</RISK_SCORE>", 1)
    )
    serve_un(body.encode())
    _, status = run_once("un_sc", runner=runner())
    assert status == "SUCCEEDED"
    with tx() as conn:
        issue = fetch_one(conn, "SELECT * FROM dq_issue WHERE category = 'SCHEMA_DRIFT'")
        assert issue["severity"] == "WARN" and "RISK_SCORE" in str(issue["detail"])


@respx.mock
def test_eu_style_5xx_burst_recovers_with_evidence_per_attempt(seeded):
    respx.get(UN_URL).mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(502),
            httpx.Response(302, headers={"Location": BLOB}),
        ]
    )
    respx.get(BLOB).mock(
        return_value=httpx.Response(200, content=(FX / "un/consolidated_v1.xml").read_bytes())
    )
    run_id, status = run_once("un_sc", runner=runner())
    assert status == "SUCCEEDED"
    with tx() as conn:
        rows = fetch_all(
            conn,
            "SELECT attempt, http_status, error_class FROM fetch_evidence WHERE run_id = %s ORDER BY attempt",
            (run_id,),
        )
        assert [(r["http_status"], r["error_class"]) for r in rows] == [
            (500, "HTTP_5XX"),
            (502, "HTTP_5XX"),
            (200, None),
        ]


@respx.mock
def test_persistent_403_fails_opens_breaker_after_threshold(seeded):
    respx.get(OFAC_URL).mock(return_value=httpx.Response(403, text="Forbidden: no user agent"))
    for _ in range(3):
        _, status = run_once("ofac_sdn", runner=runner())
        assert status == "FAILED"
    with tx() as conn:
        src = fetch_one(conn, "SELECT * FROM source WHERE source_id = 'ofac_sdn'")
        assert src["consecutive_failures"] == 3 and src["breaker_state"] == "OPEN" and src["breaker_retry_at"]
        inc = fetch_one(conn, "SELECT * FROM incident WHERE source_id = 'ofac_sdn' AND status = 'OPEN'")
        assert inc["error_class"] == "HTTP_403" and inc["occurrences"] == 3


@respx.mock
def test_abandoned_run_resumes_from_archive_without_redownload(seeded):
    route = serve_un((FX / "un/consolidated_v1.xml").read_bytes())

    class Crash(Exception):
        pass

    class CrashingRunner(PipelineRunner):
        def _parse(self, ctx, adapter):  # simulate the process dying mid-parse
            super()._parse(ctx, adapter)
            raise SystemExit("worker killed")

    with tx() as conn:
        rid = runs.enqueue_run(
            conn, source_id="un_sc", run_kind="LIST_INGEST", trigger="MANUAL", requested_by="t"
        )
        row = runs.claim_next(conn, "w1", 1)
    with pytest.raises(SystemExit):
        CrashingRunner(sleep=lambda s: None).execute(row)
    with tx() as conn:
        conn.execute(
            "UPDATE ingestion_run SET lease_expires_at = now() - interval '1 second' WHERE run_id = %s",
            (rid,),
        )
        out = runs.reclaim_abandoned(conn)
    assert out[0]["abandoned_run_id"] == rid and out[0]["resumed_run_id"]
    with tx() as conn:
        row2 = runs.claim_next(conn, "w2", 60)
    assert str(row2["resumed_from_run_id"]) == rid
    assert runner().execute(row2) == "SUCCEEDED"
    assert route.call_count == 1  # archived bytes reused, publisher not hit again
    with tx() as conn:
        assert fetch_val(conn, "SELECT status FROM ingestion_run WHERE run_id = %s", (rid,)) == "ABANDONED"
        assert fetch_val(conn, "SELECT count(*) FROM list_version WHERE status = 'PUBLISHED'") == 1
        assert fetch_val(conn, "SELECT count(*) FROM staging_record") == 0


@respx.mock
def test_cancel_during_parse_leaves_no_staging(seeded, monkeypatch):
    import sanctions_agent.pipeline.runner as runner_mod

    serve_un((FX / "un/consolidated_v1.xml").read_bytes())
    monkeypatch.setattr(runner_mod, "BATCH", 2)

    class CancelMidParse(PipelineRunner):
        def _parse(self, ctx, adapter):
            original = ctx.progress.update

            def update(force=False, **fields):
                if fields.get("records_done", 0) >= 2:
                    with tx() as c:
                        runs.request_cancel(c, ctx.run_id, "operator")
                original(force=force, **fields)

            ctx.progress.update = update
            return super()._parse(ctx, adapter)

    run_id, status = run_once("un_sc", runner=CancelMidParse(sleep=lambda s: None))
    assert status == "CANCELLED"
    with tx() as conn:
        assert fetch_val(conn, "SELECT count(*) FROM staging_record WHERE run_id = %s", (run_id,)) == 0
        assert fetch_val(conn, "SELECT count(*) FROM list_version WHERE status = 'PUBLISHED'") == 0
        assert fetch_val(conn, "SELECT status FROM list_version WHERE run_id = %s", (run_id,)) == "REJECTED"
        assert (
            fetch_val(conn, "SELECT cancelled_by FROM ingestion_run WHERE run_id = %s", (run_id,))
            == "operator"
        )


@respx.mock
def test_dry_run_never_publishes(seeded):
    serve_un((FX / "un/consolidated_v1.xml").read_bytes())
    _, status = run_once("un_sc", runner=runner(), options={"dry_run": True})
    assert status == "DRY_RUN_OK"
    with tx() as conn:
        assert fetch_val(conn, "SELECT count(*) FROM list_version WHERE status = 'PUBLISHED'") == 0
        assert fetch_val(conn, "SELECT count(*) FROM record_version") == 0


@respx.mock
@pytest.mark.parametrize(
    ("source_id", "url", "fixture", "expected"),
    [
        ("ofac_sdn", OFAC_URL, "ofac/sdn_advanced_v1.xml", 4),
        (
            "uk_fcdo",
            "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml",
            "uk/uk_sanctions_list_v1.xml",
            4,
        ),
        (
            "eu_fsf",
            "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw",
            "eu/fsf_v1.xml",
            3,
        ),
        (
            "us_csl",
            "https://data.trade.gov/downloadable_consolidated_screening_list/v1/consolidated.json",
            "csl/consolidated_v1.json",
            5,
        ),
    ],
)
def test_all_core_lists_publish(seeded, source_id, url, fixture, expected):
    respx.get(url).mock(return_value=httpx.Response(200, content=(FX / fixture).read_bytes()))
    _, status = run_once(source_id, runner=runner())
    assert status == "SUCCEEDED"
    with tx() as conn:
        assert (
            fetch_val(
                conn,
                "SELECT record_count FROM list_version WHERE source_id = %s AND status = 'PUBLISHED'",
                (source_id,),
            )
            == expected
        )


@respx.mock
def test_cross_list_keys_link_un_uk_eu(seeded):
    serve_un((FX / "un/consolidated_v1.xml").read_bytes())
    respx.get("https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml").mock(
        return_value=httpx.Response(200, content=(FX / "uk/uk_sanctions_list_v1.xml").read_bytes())
    )
    respx.get(OFAC_URL).mock(
        return_value=httpx.Response(200, content=(FX / "ofac/sdn_advanced_v1.xml").read_bytes())
    )
    for s in ("un_sc", "uk_fcdo", "ofac_sdn"):
        assert run_once(s, runner=runner())[1] == "SUCCEEDED"
    with tx() as conn:
        linked = fetch_all(
            conn,
            "SELECT source_id FROM crosslist_key WHERE key_type = 'UN_REF' AND key_value = 'TAI080'"
            " ORDER BY source_id",
        )
        assert [r["source_id"] for r in linked] == ["uk_fcdo", "un_sc"]
        imo = fetch_all(
            conn,
            "SELECT source_id FROM crosslist_key WHERE key_type = 'IMO' AND key_value = '9074729'"
            " ORDER BY source_id",
        )
        assert [r["source_id"] for r in imo] == ["ofac_sdn", "uk_fcdo"]


def test_reparse_does_not_touch_politeness_or_freshness_but_manual_load_is_fresh(seeded):
    from tests.helpers import load_fixture

    with tx() as conn:
        conn.execute(
            "UPDATE source SET last_attempt_at = now() - interval '3 days', last_success_at = now() - interval '3 days'"
            " WHERE source_id = 'un_sc'"
        )
    _, status, _ = load_fixture("un_sc", "un/consolidated_v1.xml")  # re-parse of archived bytes
    assert status == "SUCCEEDED"
    with tx() as conn:
        s = fetch_one(
            conn,
            "SELECT now() - last_attempt_at AS a, now() - last_success_at AS s FROM source"
            " WHERE source_id = 'un_sc'",
        )
        assert s["a"].days >= 2 and s["s"].days >= 2  # publisher not contacted, data not new
    _, status, _ = load_fixture(
        "un_sc", "un/consolidated_v2.xml", manual_load=True
    )  # out-of-band file (NFR-08)
    assert status == "SUCCEEDED"
    with tx() as conn:
        s = fetch_one(
            conn,
            "SELECT now() - last_attempt_at AS a, now() - last_success_at AS s FROM source"
            " WHERE source_id = 'un_sc'",
        )
        assert s["a"].days >= 2 and s["s"].total_seconds() < 60


@respx.mock
def test_fetch_records_publisher_contact_before_download_finishes(seeded):
    serve_un(b"", status=503)
    run_once("un_sc", runner=runner())
    with tx() as conn:
        assert fetch_val(
            conn, "SELECT now() - last_attempt_at < interval '1 minute' FROM source WHERE source_id = 'un_sc'"
        )
