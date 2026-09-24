"""Browser smoke test of the operations + management console (real Chromium, real server, real Postgres).

Skipped when no Chromium is available. Set SANCTIONS_UI_SHOT_DIR to keep the screenshots."""

from __future__ import annotations

import glob
import json
import os
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import respx
import uvicorn
from agents.testing import ModelStep, ScriptedModel, assistant_message
from agents.usage import Usage

from sanctions_agent.db.engine import fetch_one, notify, tx
from sanctions_agent.pipeline import runs
from sanctions_agent.pipeline.runner import PipelineRunner
from sanctions_agent.settings import get_settings
from tests.helpers import load_fixture
from tests.integration.test_pipeline import BLOB, FX, UN_URL, seeded  # noqa: F401

pytestmark = pytest.mark.ui
playwright = pytest.importorskip("playwright.sync_api")

USERS = "admin:a-pass:admin,admin2:a2-pass:admin,op:o-pass:operator"
ANSWER = {
    "answer_markdown": "**2 lists** are below a fill-rate floor:\n\n- UK: passport 50% vs 90% floor\n- none elsewhere",
    "citations": [{"source": "get_fill_rates", "as_of": "2026-09-24T10:00:00Z"}],
    "chart": None,
}


def _chromium() -> str | None:
    env = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if env:
        return env
    found = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
    return found[-1] if found else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def drain() -> list[str]:
    """Execute every queued run (what the supervisor's workers do), publishers mocked."""
    out = []
    with respx.mock(assert_all_called=False) as rx:
        rx.get(UN_URL).mock(return_value=httpx.Response(302, headers={"Location": BLOB}))
        rx.get(BLOB).mock(return_value=httpx.Response(200, content=(FX / "un/consolidated_v2.xml").read_bytes()))
        while True:
            with tx() as conn:
                row = runs.claim_next(conn, "ui-test", 300)
            if row is None:
                return out
            out.append(PipelineRunner(sleep=lambda s: None).execute(row))


@pytest.fixture()
def server(seeded, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("SANCTIONS_UI_USERS", USERS)
    get_settings.cache_clear()
    load_fixture("un_sc", "un/consolidated_v1.xml", manual_load=True)
    load_fixture("uk_fcdo", "uk/uk_sanctions_list_v1.xml", manual_load=True)
    with tx() as conn:
        conn.execute(
            """UPDATE source SET config = jsonb_set(config, '{validation,fill_floors}', '{"PERSON.passport": 0.9}')
               WHERE source_id = 'uk_fcdo'"""
        )
    load_fixture("uk_fcdo", "uk/uk_sanctions_list_v1.xml")  # -> QUARANTINED, last good kept

    from sanctions_agent.agent.analyst import AnalystAgent
    from sanctions_agent.api.app import create_app

    app = create_app()
    app.state.analyst_factory = lambda: AnalystAgent(
        model=ScriptedModel(
            [ModelStep(output=[assistant_message(json.dumps(ANSWER))], usage=Usage(requests=1, input_tokens=500,
                                                                                    output_tokens=80, total_tokens=580))]
        )
    )
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=10)
    get_settings.cache_clear()


@pytest.fixture()
def page(server):
    exe = _chromium()
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no Chromium available: {e}")
        ctx = browser.new_context(
            base_url=server, http_credentials={"username": "admin", "password": "a-pass"},
            viewport={"width": 1400, "height": 1000},
        )
        pg = ctx.new_page()
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        yield pg
        browser.close()
        assert not errors, errors


def shot(pg, name: str) -> None:
    target = os.environ.get("SANCTIONS_UI_SHOT_DIR")
    if target:
        Path(target).mkdir(parents=True, exist_ok=True)
        pg.screenshot(path=str(Path(target) / f"{name}.png"), full_page=True)


def expect(locator):
    return playwright.expect(locator)


def test_operations_console(page):
    pg = page
    # overview: tiles + per-source health from the database
    pg.goto("/ui")
    expect(pg.locator("#kpis")).to_contain_text("Healthy sources")
    un_row = pg.locator("#sources-table tr", has_text="UN Security Council")
    expect(un_row).to_contain_text("Healthy")
    expect(pg.locator("#incidents")).to_contain_text("FILL_RATE_BELOW_FLOOR")
    shot(pg, "overview")

    # live progress over SSE: a queued run appears, progress notifications move the bar and the step
    pg.goto("/ui/runs")
    expect(pg.locator("#runs-table")).to_contain_text("uk_fcdo")
    expect(pg.locator("body")).to_have_attribute("data-live", "on")  # SSE stream is listening
    with tx() as conn:
        run_id = runs.enqueue_run(conn, source_id="un_sc", run_kind="LIST_INGEST", trigger="MANUAL",
                                  requested_by="admin", reason="ui test")
    card = pg.locator(f'.run-live[data-run-id="{run_id}"]')
    expect(card).to_contain_text("Queued")
    with tx() as conn:
        notify(conn, "run_progress", json.dumps({"run_id": run_id, "source_id": "un_sc", "status": "RUNNING",
                                                 "step": "PARSE", "pct": 42, "records_done": 2100}))
    expect(card.locator(".progress")).to_have_attribute("aria-valuenow", "42")
    expect(card.locator(".step.current")).to_have_text("parse")
    expect(card).to_contain_text("2,100 records")
    assert drain() == ["SUCCEEDED"]
    expect(card).to_have_count(0)
    expect(pg.locator("#runs-table tbody tr").first).to_contain_text("Succeeded")

    # data quality: the quarantined UK candidate shows the failing field in red
    pg.goto("/ui/quality")
    pg.select_option("#q-source", "uk_fcdo")
    row = pg.locator("#fill-rates tr", has_text="passport")
    expect(row).to_contain_text("Below floor")
    expect(row.locator(".meter.fail")).to_have_count(1)
    expect(pg.locator("#issues")).to_contain_text("fill rate below floor")
    shot(pg, "quality")

    # ask: aggregate answer rendered from markdown, with sources
    pg.goto("/ui/ask")
    pg.fill("#ask-input", "Which lists are below a fill-rate floor?")
    pg.click("#ask-form button")
    bot = pg.locator(".msg.bot").last
    expect(bot).to_contain_text("2 lists")
    expect(bot.locator("strong")).to_have_text("2 lists")
    expect(bot.locator("li")).to_have_count(2)
    expect(bot).to_contain_text("Sources: get_fill_rates")
    pg.fill("#ask-input", "Is Viktor Bout sanctioned?")
    pg.click("#ask-form button")
    expect(pg.locator(".msg.bot").last).to_contain_text("screening system")
    shot(pg, "ask")


def test_management_console(page):
    pg = page
    pg.goto("/ui/manage")
    row = pg.locator("#manage-table tr", has_text="UN Security Council")
    # pause needs a reason; the row reflects it
    row.get_by_role("button", name="Pause").click()
    pg.click("dialog[open] button.primary")
    expect(pg.locator("dialog[open] .error")).to_contain_text("reason")
    pg.fill("dialog[open] #dlg-pause_hours", "2")
    pg.fill("dialog[open] #dlg-reason", "publisher maintenance window")
    pg.click("dialog[open] button.primary")
    expect(row).to_contain_text("Paused")
    expect(row).to_contain_text("publisher maintenance window")
    expect(pg.locator("#banners")).to_contain_text("Core list un_sc is paused")
    row.get_by_role("button", name="Resume").click()
    pg.click("dialog[open] button.primary")
    expect(row).to_contain_text("Active")

    # schedule editor: live preview, politeness floor enforced, save creates a new config version
    pg.goto("/ui/manage/sources/un_sc")
    pg.fill("#s-cadence", "300")
    expect(pg.locator("#s-preview")).to_contain_text("every 5 h")
    expect(pg.locator("#s-preview")).to_contain_text("Next runs")
    pg.fill("#s-cadence", "10")
    expect(pg.locator("#s-preview")).to_contain_text("politeness floor")
    pg.fill("#s-cadence", "300")
    pg.fill("#s-reason", "UN publishes rarely")
    pg.click("#s-save")
    expect(pg.locator("#s-error")).to_contain_text("Saved as configuration version 2")
    pg.click("#tabs button[data-tab=history]")
    expect(pg.locator("#h-list")).to_contain_text("UN publishes rarely")

    # ad-hoc dry run from the source page -> run detail follows it live to DRY_RUN_OK
    pg.get_by_role("button", name="Run now").click()
    pg.select_option("dialog[open] #dlg-mode", "dry_run")
    pg.fill("dialog[open] #dlg-reason", "check parser against today's file")
    pg.click("dialog[open] button.primary")
    pg.wait_for_url("**/ui/runs/*")
    expect(pg.locator("#run-head")).to_contain_text("Queued")
    assert drain() == ["DRY_RUN_OK"]
    expect(pg.locator("#run-head")).to_contain_text("Dry run OK")
    expect(pg.locator("#run-evidence")).to_contain_text("200")
    with tx() as conn:
        assert fetch_one(conn, "SELECT count(*) AS n FROM list_version WHERE source_id = 'un_sc'"
                               " AND status = 'PUBLISHED'")["n"] == 1  # dry run never publishes
    shot(pg, "run_detail")

    # add-source wizard: DRAFT -> dry run -> request activation
    with tx() as conn:
        cfg = fetch_one(conn, "SELECT config FROM source WHERE source_id = 'un_sc'")["config"]
    pg.goto("/ui/manage/new")
    pg.locator(".type-card", has_text="un_consolidated_xml").click()
    pg.fill("#n-id", "un_sc_copy")
    pg.fill("#n-name", "UN list (second feed)")
    pg.fill("#n-config", json.dumps(cfg))
    pg.fill("#n-reason", "evaluate a mirror")
    pg.click("#n-create")
    expect(pg.locator("#n-status")).to_contain_text("Created un_sc_copy as DRAFT")
    pg.click("#n-dry")
    expect(pg.locator("#n-run")).to_contain_text("Queued")
    assert drain() == ["DRY_RUN_OK"]
    expect(pg.locator("#n-run")).to_contain_text("Dry run OK")
    expect(pg.locator("#n-run")).to_contain_text("Records parsed")
    pg.click("#n-activate")
    pg.fill("dialog[open] #dlg-reason", "dry run clean")
    pg.click("dialog[open] button.primary")
    expect(pg.locator("#n-status")).to_contain_text("Activation requested")
    shot(pg, "add_source")
