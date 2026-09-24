"""Level-2 tests: official notices (sync, signals, deterministic evidence links, agent extraction),
EU annex curated lists, and enrichment providers - all against mocked endpoints."""

from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest
import respx
from agents import RunContextWrapper
from agents.testing import ModelStep, ScriptedModel, assistant_message
from agents.usage import Usage

from sanctions_agent import review
from sanctions_agent.agent import extractors
from sanctions_agent.agent.context import AgentContext
from sanctions_agent.db.engine import fetch_all, fetch_one, fetch_val, tx
from sanctions_agent.pipeline import removals
from sanctions_agent.pipeline.runner import PipelineRunner, run_once
from sanctions_agent.sources.l1.eu_annex import entries_from_csv, propose_entries
from tests.integration.test_pipeline import BLOB, FX, OFAC_URL, UN_URL, seeded  # noqa: F401

FR_URL = "https://www.federalregister.gov/api/v1/documents.json"
UK_URL = "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml"


def run(source_id, **kw):
    return run_once(source_id, runner=PipelineRunner(sleep=lambda s: None), **kw)


def serve_un(body: bytes):
    respx.get(UN_URL).mock(return_value=httpx.Response(302, headers={"Location": BLOB}))
    respx.get(BLOB).mock(return_value=httpx.Response(200, content=body))


def ofac_without_haqqani() -> bytes:
    src = (FX / "ofac/sdn_advanced_v1.xml").read_text()
    a = src.index('    <DistinctParty FixedRef="36001">')
    b = src.index("</DistinctParty>", a) + len("</DistinctParty>\n")
    src = src[:a] + src[b:]
    a = src.index('    <SanctionsEntry ID="70001"')
    b = src.index("</SanctionsEntry>", a) + len("</SanctionsEntry>\n")
    return (src[:a] + src[b:]).replace("<Day>23</Day>", "<Day>24</Day>").encode()


# ------------------------------------------------------------------ notices -------------------
@respx.mock
def test_federal_register_sync_links_removal_by_name_and_raises_signals(seeded):  # noqa: F811
    respx.get(OFAC_URL).mock(
        side_effect=[
            httpx.Response(200, content=(FX / "ofac/sdn_advanced_v1.xml").read_bytes()),
            httpx.Response(200, content=ofac_without_haqqani()),
        ]
    )
    with tx() as conn:
        conn.execute(
            "UPDATE source SET config = jsonb_set(config, '{validation,max_removed_pct}', '50')"
            " WHERE source_id = 'ofac_sdn'"
        )
    assert run("ofac_sdn")[1] == "SUCCEEDED"
    assert run("ofac_sdn")[1] == "SUCCEEDED"
    today = __import__("datetime").date.today().isoformat()
    respx.get(url__startswith=FR_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "document_number": "2026-19001",
                        "title": "Notice of OFAC Sanctions Actions",
                        "publication_date": today,
                        "html_url": "https://www.federalregister.gov/d/2026-19001",
                        "raw_text_url": "https://www.federalregister.gov/documents/full_text/txt/2026/09/24/2026-19001.txt",
                        "abstract": "OFAC is publishing the removal of Sirajuddin HAQQANI from the SDN List.",
                        "type": "Notice",
                        "agencies": [{"slug": "foreign-assets-control-office"}],
                    }
                ]
            },
        )
    )
    respx.get("https://www.federalregister.gov/documents/full_text/txt/2026/09/24/2026-19001.txt").mock(
        return_value=httpx.Response(
            200,
            text="The following individual has been removed from the SDN List: "
            "HAQQANI, Sirajuddin (Sirajuddin HAQQANI); DOB 1973.",
        )
    )
    _, status = run("fr_notices")
    assert status == "SUCCEEDED"
    with tx() as conn:
        n = fetch_one(conn, "SELECT * FROM legal_notice WHERE provider = 'fr_notices'")
        assert "DELISTING" in n["action_types"] and n["raw_sha256"]
        link = fetch_one(conn, "SELECT * FROM notice_link WHERE notice_id = %s", (n["notice_id"],))
        assert (link["source_key"], link["link_type"], link["method"]) == ("36001", "DELISTING", "NAME_MATCH")
        assert fetch_val(conn, "SELECT count(*) FROM signal WHERE target_source_id = 'ofac_sdn'") == 1
        cand = fetch_one(conn, "SELECT * FROM removal_candidate WHERE source_key = '36001'")
        out = removals.assess(conn, cand["candidate_id"])
        assert out["recommendation"] == "CONFIRMED" and out["evidence"] == 1
    # re-running the feed does not duplicate notices
    assert run("fr_notices")[1] == "NO_CHANGE"


@respx.mock
def test_un_updates_log_matches_by_reference_number(seeded):  # noqa: F811
    serve_un((FX / "un/consolidated_v1.xml").read_bytes())
    run("un_sc")
    respx.reset()
    serve_un((FX / "un/consolidated_v2.xml").read_bytes())
    run("un_sc")
    import datetime as dt

    d = dt.date.today().strftime("%-d %B %Y")
    html = f"""<html><body><h1>List updates</h1>
      <p>{d}</p><p>The Security Council Committee removed the entry CDi.099 (JEAN DUPONT) from its sanctions list.
      De-listing.</p>
      <p>1 January 2020</p><p>Old amendment QDi.001</p></body></html>"""
    respx.get("https://main.un.org/securitycouncil/en/content/list-updates-unsc-consolidated-list").mock(
        return_value=httpx.Response(200, text=html)
    )
    assert run("un_list_updates")[1] == "SUCCEEDED"
    with tx() as conn:
        link = fetch_one(conn, "SELECT * FROM notice_link")
        assert (link["source_key"], link["link_type"], link["method"]) == ("CDi.099", "DELISTING", "ID_MATCH")
        assert fetch_val(conn, "SELECT count(*) FROM legal_notice") == 1  # 2020 block is outside the lookback


@respx.mock
def test_eurlex_rss_keyword_filter_and_eu_fsf_signal(seeded):  # noqa: F811
    today = __import__("email.utils").utils.format_datetime(
        __import__("datetime").datetime.now(__import__("datetime").UTC)
    )
    rss = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>OJ L</title>
      <item><title>Council Regulation (EU) 2026/2001 amending Regulation (EU) No 833/2014 concerning restrictive measures</title>
        <link>https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32026R2001</link><guid>CELEX:32026R2001</guid>
        <pubDate>{today}</pubDate><description>Annex XLII is amended</description></item>
      <item><title>Commission Regulation on fishery quotas</title><link>https://eur-lex.europa.eu/x</link><guid>X</guid>
        <pubDate>{today}</pubDate></item></channel></rss>"""
    respx.get("https://eur-lex.europa.eu/EN/display-feed.rss?rssId=222").mock(
        return_value=httpx.Response(200, text=rss)
    )
    respx.get("https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32026R2001").mock(
        return_value=httpx.Response(
            200, text="<html><body>ANNEX XLII ... entries added: ARCTIC DAWN, IMO 9074729</body></html>"
        )
    )
    assert run("eurlex_oj")[1] == "SUCCEEDED"
    fsf_rss = f"""<?xml version="1.0"?><rss version="2.0"><channel><item><title>New FSF file</title>
        <link>https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content</link>
        <guid>fsf-2026-09-24</guid><pubDate>{today}</pubDate></item></channel></rss>"""
    respx.get("https://webgate.ec.europa.eu/fsd/fsf/public/rss").mock(
        return_value=httpx.Response(200, text=fsf_rss)
    )
    assert run("eu_fsf_rss")[1] == "SUCCEEDED"
    with tx() as conn:
        assert [
            r["title"][:25]
            for r in fetch_all(conn, "SELECT title FROM legal_notice WHERE provider = 'eurlex_oj'")
        ] == ["Council Regulation (EU) 2"]
        targets = {r["target_source_id"] for r in fetch_all(conn, "SELECT target_source_id FROM signal")}
        assert targets == {"eu_fsf", "eu_annex_xlii", "eu_annex_iv"}


@respx.mock
def test_uk_notices_and_ofac_recent_actions(seeded):  # noqa: F811
    today = __import__("datetime").date.today()
    respx.get(url__startswith="https://www.gov.uk/api/search.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Financial sanctions notice: Russia",
                        "link": "/government/publications/fsn-russia",
                        "public_timestamp": f"{today.isoformat()}T09:00:00Z",
                        "description": "Asset freeze additions",
                    }
                ]
            },
        )
    )
    respx.get("https://www.gov.uk/api/content/government/publications/fsn-russia").mock(
        return_value=httpx.Response(
            200,
            json={
                "details": {
                    "body": "<p>The following designations have been added: Ivan Petrovich IVANOV</p>"
                }
            },
        )
    )
    assert run("uk_notices")[1] == "SUCCEEDED"
    stamp = today.strftime("%Y%m%d")
    respx.get("https://ofac.treasury.gov/recent-actions").mock(
        return_value=httpx.Response(
            200,
            text=f'<html><body><a href="/recent-actions/{stamp}">Counter Terrorism Designations; Iran-related Designation Removal</a>'
            '<a href="/recent-actions/20100101">Old</a><a href="/about">About</a></body></html>',
        )
    )
    respx.get(f"https://ofac.treasury.gov/recent-actions/{stamp}").mock(
        return_value=httpx.Response(
            200, text="<html><body><h2>Deletions</h2><p>GULF PETRO TRADING FZE removed</p></body></html>"
        )
    )
    assert run("ofac_recent_actions")[1] == "SUCCEEDED"
    with tx() as conn:
        rows = {r["provider"]: r for r in fetch_all(conn, "SELECT * FROM legal_notice")}
        assert set(rows) == {"uk_notices", "ofac_recent_actions"}
        assert "DELISTING" in rows["ofac_recent_actions"]["action_types"]


@respx.mock
def test_notice_feed_failure_opens_incident(seeded):  # noqa: F811
    respx.get(url__startswith=FR_URL).mock(return_value=httpx.Response(403, text="blocked"))
    assert run("fr_notices")[1] == "FAILED"
    with tx() as conn:
        assert (
            fetch_val(conn, "SELECT error_class FROM incident WHERE source_id = 'fr_notices'") == "HTTP_403"
        )


# ------------------------------------------------------------------ agent extraction ----------
def _ctx_wrapper(conn_cycle: str) -> RunContextWrapper:
    return RunContextWrapper(context=AgentContext(cycle_id=conn_cycle))


def _cycle() -> str:
    with tx() as conn:
        return str(fetch_val(conn, "INSERT INTO agent_cycle (trigger) VALUES ('test') RETURNING cycle_id"))


@respx.mock
def test_annex_extraction_proposal_review_and_publish(seeded, monkeypatch):  # noqa: F811
    from sanctions_agent.agent.tools import extract_annex_act

    act_text = (
        "COUNCIL REGULATION (EU) 2026/2001 amending Regulation (EU) No 833/2014. In Annex XLII the following "
        "entries are added: 674. ARCTIC DAWN, IMO 9074729 675. OCEAN STAR, IMO 9321483"
    )
    with tx() as conn:
        nid = fetch_val(
            conn,
            """INSERT INTO legal_notice (provider, external_id, title, published_on, url, text_excerpt)
            VALUES ('eurlex_oj', 'CELEX:32026R2001', 'Council Regulation 2026/2001', current_date,
                    'https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32026R2001', %s) RETURNING notice_id""",
            (act_text,),
        )
    payload = {
        "entries": [
            {
                "name": "ARCTIC DAWN",
                "entity_type": "VESSEL",
                "action": "LISTING",
                "identifiers": [{"id_type": "IMO", "value": "9074729"}],
                "program": "XLII",
                "reason": None,
                "verbatim_quote": "ARCTIC DAWN, IMO 9074729",
            },
            {
                "name": "OCEAN STAR",
                "entity_type": "VESSEL",
                "action": "LISTING",
                "identifiers": [{"id_type": "IMO", "value": "9321483"}],
                "program": "XLII",
                "reason": None,
                "verbatim_quote": "OCEAN STAR, IMO 9321483",
            },
        ],
        "stated_total": None,
        "notes": "",
    }
    monkeypatch.setattr(
        extractors,
        "MODEL_OVERRIDE",
        ScriptedModel(
            [
                ModelStep(
                    output=[assistant_message(json.dumps(payload))],
                    usage=Usage(requests=1, input_tokens=500, output_tokens=80, total_tokens=580),
                )
            ]
        ),
    )
    out = json.loads(extract_annex_act(_ctx_wrapper(_cycle()), notice_id=nid, annex="XLII"))
    assert out["verified"] == 2 and out["count_check"]["ok"]
    with tx(actor="reviewer") as conn:
        res = review.decide(conn, out["proposal_id"], reviewer="reviewer", role="reviewer", approve=True)
    assert res["applied"] == 2 and res["run_id"]
    with tx() as conn:
        row = fetch_one(
            conn,
            "UPDATE ingestion_run SET status = 'RUNNING' WHERE run_id = %s RETURNING *",
            (res["run_id"],),
        )
    assert PipelineRunner(sleep=lambda s: None).execute(row) == "SUCCEEDED"
    with tx() as conn:
        v = fetch_one(
            conn, "SELECT * FROM list_version WHERE source_id = 'eu_annex_xlii' AND status = 'PUBLISHED'"
        )
        assert v["record_count"] == 2
        imo = fetch_all(
            conn, "SELECT source_id FROM crosslist_key WHERE key_type = 'IMO' AND key_value = '9074729'"
        )
        assert {r["source_id"] for r in imo} == {"eu_annex_xlii"}
        ev = fetch_val(
            conn,
            """SELECT l.evidence_url FROM rv_listing l JOIN record_version rv USING (record_version_id)
                                WHERE rv.source_id = 'eu_annex_xlii' LIMIT 1""",
        )
        assert "32026R2001" in ev
    # rebuilding with no changes is NO_CHANGE (deterministic artifact)
    assert run("eu_annex_xlii")[1] == "NO_CHANGE"


def test_annex_csv_import_rejects_bad_imo(seeded, tmp_path):  # noqa: F811
    p = tmp_path / "annex.csv"
    p.write_text("name,imo,flag\nARCTIC DAWN,9074729,Cameroon\nBAD SHIP,1234568,Gabon\n")
    entries = entries_from_csv(p, "XLII")
    assert [e["ok"] for e in entries] == [True, False]
    with tx() as conn:
        cid = propose_entries(
            conn,
            source_id="eu_annex_xlii",
            operation="ADD",
            entries=entries,
            act={"celex": "32014R0833", "url": "https://eur-lex.europa.eu/eli/reg/2014/833/oj"},
            proposed_by="analyst:ann",
        )
    with tx() as conn, pytest.raises(Exception, match="own proposal"):
        review.decide(conn, cid, reviewer="analyst:ann", role="reviewer", approve=True)
    with tx() as conn:
        res = review.decide(conn, cid, reviewer="bob", role="reviewer", approve=True)
        assert res["applied"] == 1
        assert (
            fetch_val(conn, "SELECT count(*) FROM curated_entry WHERE source_id = 'eu_annex_xlii' AND active")
            == 1
        )


# ------------------------------------------------------------------ enrichment ----------------
def _publish_ofac_and_eu():
    respx.get(OFAC_URL).mock(
        return_value=httpx.Response(200, content=(FX / "ofac/sdn_advanced_v1.xml").read_bytes())
    )
    respx.get(
        "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw"
    ).mock(return_value=httpx.Response(200, content=(FX / "eu/fsf_v1.xml").read_bytes()))
    assert run("ofac_sdn")[1] == "SUCCEEDED"
    assert run("eu_fsf")[1] == "SUCCEEDED"


def _lei_record(lei, name, country, reg=None):
    return {
        "type": "lei-records",
        "id": lei,
        "attributes": {
            "lei": lei,
            "entity": {
                "legalName": {"name": name},
                "legalAddress": {"country": country, "city": "X"},
                "registeredAs": reg,
                "jurisdiction": country,
                "status": "ACTIVE",
                "legalForm": {"id": "8888"},
                "registeredAt": {"id": "RA000001"},
            },
            "registration": {"status": "ISSUED"},
        },
    }


@respx.mock
def test_gleif_exact_lei_with_parents_and_fuzzy_name_review(seeded):  # noqa: F811
    _publish_ofac_and_eu()
    lei = "5493001KJTIIGC8Y1R12"
    base = "https://api.gleif.org/api/v1"
    respx.get(f"{base}/lei-records/{lei}").mock(
        return_value=httpx.Response(200, json={"data": _lei_record(lei, "GULF PETRO TRADING FZE", "AE")})
    )
    respx.get(f"{base}/lei-records/{lei}/direct-parent").mock(return_value=httpx.Response(404, json={}))
    respx.get(f"{base}/lei-records/{lei}/ultimate-parent").mock(
        return_value=httpx.Response(
            200, json={"data": _lei_record("529900ABCDEFGHIJ1234", "GULF HOLDINGS LTD", "AE")}
        )
    )
    respx.get(url__regex=r".*/lei-records/[^/]+/(direct|ultimate)-parent$").mock(
        return_value=httpx.Response(404, json={})
    )
    respx.get(url__startswith=f"{base}/lei-records?").mock(
        return_value=httpx.Response(
            200,
            json={"data": [_lei_record("984500AAAAAAAAAAAA11", "AL HARAMAIN ISLAMIC FOUNDATION INC", "BA")]},
        )
    )
    respx.get(url__startswith=f"{base}/fuzzycompletions").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    assert run("gleif")[1] == "SUCCEEDED"
    with tx() as conn:
        recs = {
            r["subject_source_key"]: r
            for r in fetch_all(conn, "SELECT * FROM enrichment_record WHERE provider = 'gleif'")
        }
        gulf = recs["36002"]
        assert gulf["status"] == "AUTO_ACCEPTED" and gulf["match_method"] == "LEI"
        assert (
            gulf["data"]["parents"]["ultimate_parent"]["name"] == "GULF HOLDINGS LTD" and gulf["raw_sha256"]
        )
        har = recs["130002"]
        assert har["status"] in ("AUTO_ACCEPTED", "NEEDS_REVIEW") and har["match_method"] == "NAME_COUNTRY"
        if har["status"] == "NEEDS_REVIEW":
            assert (
                fetch_val(
                    conn,
                    "SELECT kind FROM proposed_change WHERE change_id = %s",
                    (har["proposed_change_id"],),
                )
                == "ENRICHMENT_MATCH"
            )


@respx.mock
def test_companies_house_requires_key_then_matches(seeded, monkeypatch):  # noqa: F811
    respx.get(UK_URL).mock(
        return_value=httpx.Response(200, content=(FX / "uk/uk_sanctions_list_v1.xml").read_bytes())
    )
    assert run("uk_fcdo")[1] == "SUCCEEDED"
    assert run("companies_house")[1] == "FAILED"
    with tx() as conn:
        assert "API key" in fetch_val(
            conn, "SELECT error_detail FROM ingestion_run WHERE source_id = 'companies_house'"
        )
        conn.execute(
            "UPDATE source SET last_attempt_at = NULL, consecutive_failures = 0 WHERE source_id = 'companies_house'"
        )
    from sanctions_agent.settings import get_settings

    monkeypatch.setenv("SANCTIONS_COMPANIES_HOUSE_API_KEY", "test-key")
    get_settings.cache_clear()
    ch = "https://api.company-information.service.gov.uk"
    search = respx.get(url__startswith=f"{ch}/search/companies").mock(
        return_value=httpx.Response(
            200, json={"items": [{"title": "SEVERNY SHIPPING LIMITED", "company_number": "12345678"}]}
        )
    )
    respx.get(f"{ch}/company/12345678").mock(
        return_value=httpx.Response(
            200,
            json={
                "company_name": "SEVERNY SHIPPING LIMITED",
                "company_number": "12345678",
                "company_status": "active",
                "type": "ltd",
                "date_of_creation": "2019-01-01",
                "sic_codes": ["50200"],
            },
        )
    )
    assert run("companies_house")[1] == "SUCCEEDED"
    assert search.calls[0].request.headers["authorization"].startswith("Basic ")
    with tx() as conn:
        rec = fetch_one(conn, "SELECT * FROM enrichment_record WHERE provider = 'companies_house'")
        assert (
            rec["provider_key"] == "12345678" and rec["status"] == "AUTO_ACCEPTED"
        )  # LLC ~ LIMITED after legal-form strip


@respx.mock
def test_faa_matches_aircraft_by_serial_and_model(seeded):  # noqa: F811
    _publish_ofac_and_eu()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "MASTER.txt",
            "N-NUMBER,SERIAL NUMBER,MFR MDL CODE,YEAR MFR,NAME,STREET,CITY,STATE,COUNTRY,MODE S CODE HEX,STATUS CODE\n"
            "12345,1028,A34,2004,EXAMPLE LEASING LLC,1 AIRPORT RD,MIAMI,FL,US,A0B1C2,V\n"
            "99999,5555,B73,1999,OTHER,,,,US,,V\n",
        )
        zf.writestr("ACFTREF.txt", "CODE,MFR,MODEL\nA34,AIRBUS,A340-642\nB73,BOEING,737\n")
    respx.get("https://registry.faa.gov/database/ReleasableAircraft.zip").mock(
        return_value=httpx.Response(200, content=buf.getvalue())
    )
    assert run("faa_registry")[1] == "SUCCEEDED"
    with tx() as conn:
        rec = fetch_one(conn, "SELECT * FROM enrichment_record WHERE provider = 'faa_registry'")
        assert rec["subject_source_key"] == "36004" and rec["status"] == "AUTO_ACCEPTED"
        assert rec["data"]["registrant"] == "EXAMPLE LEASING LLC" and rec["provider_key"] == "N12345"


@respx.mock
def test_wikidata_vessel_by_imo(seeded):  # noqa: F811
    _publish_ofac_and_eu()
    respx.get(url__startswith="https://query.wikidata.org/sparql").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": {
                    "bindings": [
                        {
                            "imo": {"value": "9074729"},
                            "item": {"value": "http://www.wikidata.org/entity/Q123"},
                            "itemLabel": {"value": "Ocean Star"},
                            "flagLabel": {"value": "Panama"},
                            "ownerLabel": {"value": "Gulf Petro Trading"},
                        }
                    ]
                }
            },
        )
    )
    assert run("wikidata")[1] == "SUCCEEDED"
    with tx() as conn:
        rec = fetch_one(
            conn, "SELECT * FROM enrichment_record WHERE provider = 'wikidata' AND status = 'AUTO_ACCEPTED'"
        )
        assert rec["provider_key"] == "Q123" and rec["data"]["owners"] == ["Gulf Petro Trading"]
