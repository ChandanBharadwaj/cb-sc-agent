"""HTTP API + console: roles, optimistic locking, maker-checker, run-now modes, schedule preview, settings, CSP."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from sanctions_agent.db.engine import fetch_one, fetch_val, tx
from sanctions_agent.settings import get_settings
from tests.helpers import load_fixture
from tests.integration.test_pipeline import seeded  # noqa: F401

USERS = "admin:a-pass:admin,admin2:a2-pass:admin,rev:r-pass:reviewer,op:o-pass:operator,view:v-pass:viewer"
AUTH = {
    "admin": ("admin", "a-pass"),
    "admin2": ("admin2", "a2-pass"),
    "rev": ("rev", "r-pass"),
    "op": ("op", "o-pass"),
    "view": ("view", "v-pass"),
}


@pytest.fixture()
def client(seeded, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SANCTIONS_UI_USERS", USERS)
    get_settings.cache_clear()
    from sanctions_agent.api.app import create_app

    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def as_(user: str) -> dict[str, tuple[str, str]]:
    return {"auth": AUTH[user]}


# ------------------------------------------------------------------ auth & read ------------------------
def test_auth_required_and_roles_enforced(client):
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/overview", auth=("admin", "wrong")).status_code == 401
    r = client.get("/api/overview", **as_("view"))
    assert r.status_code == 200 and r.json()["user"] == {"name": "view", "role": "viewer"}
    body = {"status": "PAUSED", "reason": "viewer try"}
    assert client.post("/api/sources/un_sc/status", json=body, **as_("view")).status_code == 403
    assert client.post("/api/proposals/1/decide", json={"approve": True}, **as_("op")).status_code == 403
    assert client.put("/api/settings/agent_enabled", json={"value": False}, **as_("op")).status_code == 403


def test_overview_and_quality_return_real_numbers(client):
    load_fixture("un_sc", "un/consolidated_v1.xml", manual_load=True)
    ov = client.get("/api/overview", **as_("view")).json()
    un = next(s for s in ov["sources"] if s["source_id"] == "un_sc")
    assert un["health"] == "healthy" and un["record_count"] == 5
    assert isinstance(un["hours_since_success"], int | float)  # Decimal must not arrive as a string
    fr = client.get("/api/quality/fill-rates?source_id=un_sc", **as_("view")).json()
    assert fr and all(isinstance(r["fill_rate"], int | float) for r in fr)
    assert {r["version_status"] for r in fr} == {"PUBLISHED"}
    trend = client.get("/api/quality/trend?source_id=un_sc", **as_("view")).json()
    assert [t["record_count"] for t in trend] == [5]
    snap = client.get("/api/snapshots/current", **as_("view")).json()
    assert snap["snapshot"]["sources_pinned"] == 1


def test_quality_shows_quarantined_candidate_with_failing_field(client):
    load_fixture("uk_fcdo", "uk/uk_sanctions_list_v1.xml", manual_load=True)
    with tx() as conn:
        conn.execute(
            """UPDATE source SET config = jsonb_set(config, '{validation,fill_floors}', '{"PERSON.passport": 0.9}')
               WHERE source_id = 'uk_fcdo'"""
        )
    _, status, _ = load_fixture("uk_fcdo", "uk/uk_sanctions_list_v1.xml")
    assert status == "QUARANTINED"
    fr = client.get("/api/quality/fill-rates?source_id=uk_fcdo", **as_("view")).json()
    failing = [r for r in fr if r["status"] == "FAIL"]
    assert [(r["entity_type"], r["field"], r["version_status"]) for r in failing] == [
        ("PERSON", "passport", "QUARANTINED")
    ]
    issues = client.get("/api/quality/issues?source_id=uk_fcdo", **as_("view")).json()
    assert any(i["category"] == "FILL_RATE_BELOW_FLOOR" for i in issues)
    ov = client.get("/api/overview", **as_("view")).json()
    assert (
        next(s for s in ov["sources"] if s["source_id"] == "uk_fcdo")["health"] == "healthy"
    )  # last good kept


# ------------------------------------------------------------------ editing -----------------------------
def test_patch_needs_version_and_detects_conflicts(client):
    sched = {"kind": "INTERVAL", "cadence_minutes": 300, "min_interval_minutes": 60}
    r = client.patch("/api/sources/un_sc", json={"schedule": sched, "reason": "slower"}, **as_("admin"))
    assert r.status_code == 428
    r = client.patch(
        "/api/sources/un_sc",
        json={"schedule": sched, "reason": "slower"},
        headers={"If-Match": "1"},
        **as_("admin"),
    )
    assert r.status_code == 200 and r.json()["applied"] and r.json()["version"] == 2
    stale = client.patch(
        "/api/sources/un_sc",
        json={"schedule": sched, "reason": "again", "expected_version": 1},
        **as_("admin"),
    )
    assert stale.status_code == 409
    too_fast = {"kind": "INTERVAL", "cadence_minutes": 10, "min_interval_minutes": 10}
    r = client.patch(
        "/api/sources/un_sc",
        json={"schedule": too_fast, "reason": "faster", "expected_version": 2},
        **as_("admin"),
    )
    assert r.status_code == 422 and "politeness" in r.json()["detail"]
    detail = client.get("/api/sources/un_sc", **as_("view")).json()
    assert detail["source"]["schedule"]["cadence_minutes"] == 300 and len(detail["next_runs"]) == 5
    assert [v["version"] for v in detail["config_versions"]] == [2, 1]
    assert (
        client.patch(
            "/api/sources/un_sc", json={"priority": 2, "reason": "op", "expected_version": 2}, **as_("op")
        ).status_code
        == 403
    )


def test_url_change_is_maker_checker_through_the_review_queue(client):
    src = client.get("/api/sources/uk_fcdo", **as_("admin")).json()["source"]
    cfg = src["config"]
    cfg["fetch"]["url"] = "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List-v2.xml"
    r = client.patch(
        "/api/sources/uk_fcdo",
        json={"config": cfg, "reason": "publisher moved the file", "expected_version": src["config_version"]},
        **as_("admin"),
    )
    body = r.json()
    assert r.status_code == 200 and not body["applied"] and body["pending_change_id"]
    cid = body["pending_change_id"]
    pending = client.get("/api/proposals?kind=CONFIG_CHANGE", **as_("view")).json()
    assert [p["change_id"] for p in pending] == [cid]
    assert (
        client.post(f"/api/proposals/{cid}/decide", json={"approve": True}, **as_("rev")).status_code == 403
    )
    own = client.post(f"/api/proposals/{cid}/decide", json={"approve": True}, **as_("admin"))
    assert own.status_code == 403 and "maker-checker" in own.json()["detail"]
    ok = client.post(
        f"/api/proposals/{cid}/decide", json={"approve": True, "comment": "checked"}, **as_("admin2")
    )
    assert ok.status_code == 200 and ok.json()["status"] == "APPLIED"
    after = client.get("/api/sources/uk_fcdo", **as_("view")).json()["source"]
    assert after["config"]["fetch"]["url"].endswith("v2.xml")


def test_host_outside_allow_list_is_refused(client):
    src = client.get("/api/sources/un_sc", **as_("admin")).json()["source"]
    cfg = src["config"]
    cfg["fetch"]["url"] = "https://evil.example.com/list.xml"
    cfg["fetch"]["allowed_hosts"] = ["evil.example.com"]
    r = client.patch(
        "/api/sources/un_sc",
        json={"config": cfg, "reason": "test", "expected_version": src["config_version"]},
        **as_("admin"),
    )
    assert r.status_code in (403, 422) and "allow" in r.json()["detail"].lower()


def test_status_changes_follow_role_and_reason_rules(client):
    r = client.post(
        "/api/sources/un_sc/status",
        json={"status": "PAUSED", "reason": "publisher outage", "pause_hours": 2},
        **as_("op"),
    )
    assert r.status_code == 200 and r.json()["paused_until"]
    no_reason = client.post("/api/sources/un_sc/status", json={"status": "PAUSED"}, **as_("op"))
    assert no_reason.status_code == 422
    core = client.post(
        "/api/sources/ofac_sdn/status", json={"status": "DISABLED", "reason": "test"}, **as_("op")
    )
    assert core.status_code == 403
    r = client.post(
        "/api/sources/ofac_sdn/status",
        json={"status": "DISABLED", "reason": "planned migration"},
        **as_("admin"),
    )
    assert r.status_code == 200
    ov = client.get("/api/overview", **as_("view")).json()
    assert {c["source_id"] for c in ov["core_disabled"]} == {"un_sc", "ofac_sdn"}  # paused counts too: banner
    assert client.post("/api/sources/un_sc/status", json={"status": "ACTIVE"}, **as_("op")).status_code == 200


# ------------------------------------------------------------------ runs --------------------------------
def test_run_now_modes_and_guards(client):
    _, _, sha = load_fixture("un_sc", "un/consolidated_v1.xml")
    r = client.post("/api/sources/un_sc/runs", json={"mode": "normal", "reason": "check"}, **as_("op"))
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    dup = client.post("/api/sources/un_sc/runs", json={"mode": "normal", "reason": "again"}, **as_("op"))
    assert dup.status_code == 409 and "already running" in dup.json()["detail"]
    assert client.post(f"/api/runs/{run_id}/cancel", **as_("op")).json()["result"] == "CANCELLED"
    # politeness: a recent attempt blocks a normal run; only an admin may override, never below 5 minutes
    with tx() as conn:
        conn.execute("UPDATE source SET last_attempt_at = now() WHERE source_id = 'un_sc'")
    polite = client.post("/api/sources/un_sc/runs", json={"mode": "normal", "reason": "test"}, **as_("op"))
    assert polite.status_code == 409 and "politeness" in polite.json()["detail"]
    body = {"mode": "force_refetch", "reason": "test", "override_min_interval": True}
    assert client.post("/api/sources/un_sc/runs", json=body, **as_("op")).status_code == 403
    assert client.post("/api/sources/un_sc/runs", json=body, **as_("admin")).status_code == 429
    with tx() as conn:
        conn.execute(
            "UPDATE source SET last_attempt_at = now() - interval '10 minutes' WHERE source_id = 'un_sc'"
        )
    ok = client.post("/api/sources/un_sc/runs", json=body, **as_("admin"))
    assert ok.status_code == 202
    with tx() as conn:
        run = fetch_one(
            conn, "SELECT options, summary FROM ingestion_run WHERE run_id = %s", (ok.json()["run_id"],)
        )
        assert run["options"] == {"force_refetch": True} and run["summary"]["politeness_override"] == "admin"
        conn.execute("UPDATE ingestion_run SET status = 'CANCELLED' WHERE status = 'QUEUED'")
    # re-parse: only an artifact of this source, and it skips politeness (no download)
    bad = client.post(
        "/api/sources/un_sc/runs",
        json={"mode": "reparse", "reason": "test", "reparse_sha256": "0" * 64},
        **as_("op"),
    )
    assert bad.status_code == 422
    rp = client.post(
        "/api/sources/un_sc/runs",
        json={"mode": "reparse", "reason": "parser fix", "reparse_sha256": sha},
        **as_("op"),
    )
    assert rp.status_code == 202
    runs_ = client.get("/api/runs?source_id=un_sc", **as_("view")).json()
    assert runs_[0]["run_id"] == rp.json()["run_id"] and runs_[0]["requested_by"] == "op"


def test_new_source_is_draft_dry_run_only_until_activated(client):
    un = client.get("/api/sources/un_sc", **as_("admin")).json()["source"]
    body = {
        "source_id": "un_sc_mirror",
        "display_name": "UN list (mirror test)",
        "adapter_type": "un_consolidated_xml",
        "config": un["config"],
        "schedule": {"kind": "INTERVAL", "cadence_minutes": 240, "min_interval_minutes": 60},
        "reason": "second copy for testing",
    }
    assert client.post("/api/sources", json=body, **as_("op")).status_code == 403
    r = client.post("/api/sources", json=body, **as_("admin"))
    assert r.status_code == 201 and r.json()["status"] == "DRAFT"
    normal = client.post(
        "/api/sources/un_sc_mirror/runs", json={"mode": "normal", "reason": "test"}, **as_("op")
    )
    assert normal.status_code == 409 and "DRAFT" in normal.json()["detail"]
    dry = client.post(
        "/api/sources/un_sc_mirror/runs", json={"mode": "dry_run", "reason": "test"}, **as_("op")
    )
    assert dry.status_code == 202
    act = client.post(
        "/api/sources/un_sc_mirror/request-activation", json={"reason": "dry run ok"}, **as_("admin")
    )
    assert act.status_code == 200 and act.json()["proposal_id"]


def test_schedule_preview_validates_floor_cron_and_timezone(client):
    url = "/api/schedule/preview?adapter_type=un_consolidated_xml"
    low = client.post(
        url, json={"kind": "INTERVAL", "cadence_minutes": 10, "min_interval_minutes": 60}, **as_("view")
    )
    assert low.json()["valid"] is False and low.json()["hard_min_interval_minutes"] == 60
    bad = client.post(
        url, json={"kind": "CRON", "cron_expr": "not a cron", "min_interval_minutes": 60}, **as_("view")
    )
    assert bad.json()["valid"] is False
    cron = {
        "kind": "CRON",
        "cron_expr": "0 */6 * * *",
        "timezone": "Europe/London",
        "min_interval_minutes": 60,
    }
    ok = client.post(url, json=cron, **as_("view")).json()
    assert ok["valid"] and len(ok["next_runs"]) == 5
    for t in ok["next_runs"]:
        local = datetime.fromisoformat(t).astimezone(ZoneInfo("Europe/London"))
        assert local.minute == 0 and local.hour % 6 == 0
    tz = client.post(url, json={**cron, "timezone": "Mars/Base"}, **as_("view")).json()
    assert tz["valid"] is False and "timezone" in tz["error"]


def test_suggest_floors_from_history(client):
    load_fixture("un_sc", "un/consolidated_v1.xml")
    load_fixture("un_sc", "un/consolidated_v2.xml")
    sug = client.get("/api/sources/un_sc/suggest-floors", **as_("view")).json()
    assert sug and all(v["suggested_floor"] <= v["lowest"] and v["versions"] == 2 for v in sug.values())
    assert "PERSON.dob" in sug


def test_settings_validation_and_maintenance_mode(client):
    r = client.put("/api/settings/agent_daily_budget_usd", json={"value": 5000}, **as_("admin"))
    assert r.status_code == 422
    assert client.put("/api/settings/no_such_key", json={"value": 1}, **as_("admin")).status_code == 404
    on = {"value": {"enabled": True, "reason": "DB upgrade"}}
    assert client.put("/api/settings/maintenance_mode", json=on, **as_("admin")).status_code == 200
    r = client.post("/api/sources/un_sc/runs", json={"mode": "normal", "reason": "test"}, **as_("op"))
    assert r.status_code == 409 and "maintenance" in r.json()["detail"]
    assert client.get("/api/overview", **as_("view")).json()["maintenance"]["enabled"] is True
    with tx() as conn:
        assert fetch_val(conn, "SELECT count(*) FROM audit_log WHERE table_name = 'system_setting'") >= 1


# ------------------------------------------------------------------ console -------------------------------
@pytest.mark.parametrize(
    "path",
    [
        "/ui",
        "/ui/runs",
        "/ui/quality",
        "/ui/enrichment",
        "/ui/agent",
        "/ui/review",
        "/ui/ask",
        "/ui/manage",
        "/ui/manage/sources/un_sc",
        "/ui/manage/new",
        "/ui/manage/settings",
        "/ui/runs/00000000-0000-0000-0000-000000000000",
    ],
)
def test_console_pages_render_with_strict_csp(client, path):
    r = client.get(path, **as_("view"))
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", r.text), "inline scripts would violate the CSP"
    assert 'data-role="viewer"' in r.text


def test_unknown_page_is_404(client):
    assert client.get("/ui/nope", **as_("view")).status_code == 404


def test_ask_refuses_row_level_questions_without_calling_a_model(client):
    class Boom:
        def ask(self, *a, **k):  # the real agent short-circuits before any model call
            raise AssertionError("should not be reached")

    r = client.post(
        "/api/ask", json={"question": "What is the passport number of John Smith?"}, **as_("view")
    )
    body = r.json()
    assert r.status_code == 200 and body["refused"] is True and body["citations"] == []
    assert "screening" in body["answer_markdown"].lower()


def test_health_ready_metrics(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json()["schema"] == "0003_run_batches"
    assert "sanctions_db_up 1.0" in client.get("/metrics").text


# ------------------------------------------------------------------ batches -----------------------------
def test_multi_source_ad_hoc_run_is_one_batch_with_refusals_recorded(client):
    client.post(
        "/api/sources/uk_fcdo/status", json={"status": "PAUSED", "reason": "publisher outage"}, **as_("op")
    )
    body = {"source_ids": ["un_sc", "eu_fsf", "uk_fcdo"], "mode": "dry_run", "reason": "check parsers"}
    assert client.post("/api/batches", json=body, **as_("view")).status_code == 403
    r = client.post("/api/batches", json=body, **as_("op"))
    assert r.status_code == 202
    res = r.json()
    assert sorted(q["source_id"] for q in res["queued"]) == ["eu_fsf", "un_sc"]
    assert [(x["source_id"], "PAUSED" in x["why"]) for x in res["refused"]] == [("uk_fcdo", True)]
    b = client.get(f"/api/batches/{res['batch_id']}", **as_("view")).json()
    assert b["trigger"] == "MANUAL" and b["requested_by"] == "op" and b["mode"] == "dry_run"
    assert b["sources_requested"] == 3 and b["sources_run"] == 2 and b["sources_refused"] == 1
    assert b["status"] == "RUNNING" and {r_["source_id"] for r_ in b["runs"]} == {"un_sc", "eu_fsf"}
    assert all(r_["batch_id"] == res["batch_id"] for r_ in b["runs"])
    with tx() as conn:
        opts = fetch_val(conn, "SELECT options FROM ingestion_run WHERE source_id = 'un_sc'")
    assert opts == {"dry_run": True}
    # cancel the whole batch
    out = client.post(f"/api/batches/{res['batch_id']}/cancel", **as_("op")).json()
    assert sorted(x["result"] for x in out["results"]) == ["CANCELLED", "CANCELLED"]
    assert client.get(f"/api/batches/{res['batch_id']}", **as_("view")).json()["status"] == "CANCELLED"


def test_batch_request_validation_and_override(client):
    bad = client.post("/api/batches", json={"source_ids": ["un_sc", "nope"], "reason": "test"}, **as_("op"))
    assert bad.status_code == 422 and "nope" in bad.json()["detail"]
    empty = client.post("/api/batches", json={"source_ids": [], "reason": "test"}, **as_("op"))
    assert empty.status_code == 422
    over = {"source_ids": ["un_sc"], "reason": "test", "override_min_interval": True}
    assert client.post("/api/batches", json=over, **as_("op")).status_code == 403
    with tx() as conn:
        conn.execute(
            "UPDATE source SET last_attempt_at = now() - interval '2 minutes' WHERE source_id = 'un_sc'"
        )
        conn.execute(
            "UPDATE source SET last_attempt_at = now() - interval '10 minutes' WHERE source_id = 'ofac_sdn'"
        )
    r = client.post("/api/batches", json={**over, "source_ids": ["un_sc", "ofac_sdn"]}, **as_("admin")).json()
    assert [q["source_id"] for q in r["queued"]] == ["ofac_sdn"]  # overridden
    assert r["refused"][0]["source_id"] == "un_sc" and "5 minutes" in r["refused"][0]["why"]
    polite = client.post(
        "/api/batches", json={"source_ids": ["uk_fcdo"], "reason": "test"}, **as_("op")
    ).json()
    assert polite["queued"] and not polite["refused"]  # never pulled -> allowed
    # a DRAFT source only runs in dry-run mode
    un = client.get("/api/sources/un_sc", **as_("admin")).json()["source"]
    client.post(
        "/api/sources",
        **as_("admin"),
        json={
            "source_id": "draft_src",
            "display_name": "Draft source",
            "adapter_type": "un_consolidated_xml",
            "config": un["config"],
            "schedule": {"kind": "INTERVAL", "cadence_minutes": 240, "min_interval_minutes": 60},
            "reason": "test",
        },
    )
    d1 = client.post("/api/batches", json={"source_ids": ["draft_src"], "reason": "test"}, **as_("op")).json()
    assert not d1["queued"] and "DRAFT" in d1["refused"][0]["why"]
    assert client.get(f"/api/batches/{d1['batch_id']}", **as_("view")).json()["status"] == "REFUSED"
    d2 = client.post(
        "/api/batches", json={"source_ids": ["draft_src"], "reason": "test", "mode": "dry_run"}, **as_("op")
    )
    assert d2.json()["queued"]


def test_batch_list_filters_nesting_and_paging(client):
    load_fixture("un_sc", "un/consolidated_v1.xml", manual_load=True)
    load_fixture("uk_fcdo", "uk/uk_sanctions_list_v1.xml", manual_load=True)
    r = client.post(
        "/api/sources/eu_fsf/runs", json={"mode": "dry_run", "reason": "single"}, **as_("op")
    ).json()
    assert r["batch_id"]  # the single-source endpoint also creates a batch (of one)
    all_ = client.get("/api/batches", **as_("view")).json()
    assert len(all_["batches"]) == 3 and all_["next_before"] is None
    newest = all_["batches"][0]
    assert newest["batch_id"] == r["batch_id"] and newest["runs"][0]["source_id"] == "eu_fsf"
    only_un = client.get("/api/batches?source_id=un_sc", **as_("view")).json()["batches"]
    assert [b["runs"][0]["source_id"] for b in only_un] == ["un_sc"]
    done = client.get("/api/batches?status=COMPLETED", **as_("view")).json()["batches"]
    assert {b["runs"][0]["source_id"] for b in done} == {"un_sc", "uk_fcdo"}
    page1 = client.get("/api/batches?limit=2", **as_("view")).json()
    assert len(page1["batches"]) == 2 and page1["next_before"]
    page2 = client.get(
        "/api/batches", params={"limit": 2, "before": page1["next_before"]}, **as_("view")
    ).json()
    assert len(page2["batches"]) == 1
    run_detail = client.get(f"/api/runs/{r['run_id']}", **as_("view")).json()
    assert run_detail["batch"]["batch_id"] == r["batch_id"] and run_detail["batch"]["trigger"] == "MANUAL"
