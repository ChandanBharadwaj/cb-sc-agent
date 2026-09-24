from datetime import UTC, datetime, timedelta

import pytest

from sanctions_agent.db.engine import fetch_all, fetch_one, fetch_val, tx
from sanctions_agent.scheduling.schedule import Schedule
from sanctions_agent.settings import get_settings
from sanctions_agent.sources.config_service import (
    ConfigConflict,
    ConfigService,
    PermissionDenied,
    ValidationFailed,
)
from sanctions_agent.sources.registry import export_seed, get_source, import_seed, list_sources


@pytest.fixture()
def seeded(db):
    with tx(actor="seed") as conn:
        res = import_seed(conn, get_settings().sources_seed_file, actor="seed")
    return res


def test_seed_import_creates_all_sources_and_versions(seeded):
    assert "ofac_sdn" in seeded["created"] and "icij" in seeded["created"]
    with tx() as conn:
        srcs = {s.source_id: s for s in list_sources(conn)}
        assert srcs["ofac_sdn"].status == "ACTIVE" and srcs["ofac_sdn"].is_core
        assert srcs["icij"].status == "DISABLED" and srcs["icij"].licence_status == "LEGAL_REVIEW_REQUIRED"
        assert srcs["eu_fsf"].config.fetch.retry.max_attempts == 7  # type: ignore[attr-defined]
        assert fetch_val(conn, "SELECT count(*) FROM source_config_version WHERE status = 'ACTIVE'") == len(
            srcs
        )
        assert srcs["ofac_sdn"].next_due_at is not None
    with tx(actor="seed") as conn:
        again = import_seed(conn, get_settings().sources_seed_file, actor="seed")
    assert again["created"] == [] and "ofac_sdn" in again["skipped"]


def test_non_sensitive_edit_applies_and_is_versioned(seeded):
    with tx(actor="alice") as conn:
        s = get_source(conn, "un_sc")
        sched = Schedule.from_seed(
            {
                "kind": "INTERVAL",
                "cadence_minutes": 180,
                "min_interval_minutes": 60,
                "warn_staleness_hours": 8,
                "hard_max_staleness_hours": 12,
            }
        )
        cfg = dict(s.config_raw)
        cfg["validation"] = {**cfg["validation"], "max_removed_abs": 30}
        r = ConfigService(conn).update_source(
            "un_sc",
            actor="alice",
            reason="tune",
            expected_version=s.config_version,
            config=cfg,
            schedule=sched,
        )
    assert r.applied and r.version == 2
    assert "validation.max_removed_abs" in r.changed_paths and "schedule.cadence_minutes" in r.changed_paths
    with tx() as conn:
        s2 = get_source(conn, "un_sc")
        assert s2.config_version == 2 and s2.schedule.cadence == timedelta(hours=3)
        statuses = [
            r["status"]
            for r in fetch_all(
                conn, "SELECT status FROM source_config_version WHERE source_id='un_sc' ORDER BY version"
            )
        ]
        assert statuses == ["SUPERSEDED", "ACTIVE"]


def test_stale_edit_conflicts(seeded):
    with tx(actor="alice") as conn, pytest.raises(ConfigConflict):
        ConfigService(conn).update_source("un_sc", actor="alice", reason="x", expected_version=99, priority=3)


def test_politeness_floor_enforced(seeded):
    with tx(actor="alice") as conn, pytest.raises(ValidationFailed, match="politeness floor"):
        ConfigService(conn).update_source(
            "ofac_sdn",
            actor="alice",
            reason="faster",
            expected_version=1,
            schedule=Schedule.from_seed(
                {"kind": "INTERVAL", "cadence_minutes": 5, "min_interval_minutes": 5}
            ),
        )


def test_url_change_requires_second_admin(seeded):
    with tx(actor="alice") as conn:
        s = get_source(conn, "uk_fcdo")
        cfg = dict(s.config_raw)
        cfg["fetch"] = {
            **cfg["fetch"],
            "url": "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List-v2.xml",
        }
        r = ConfigService(conn).update_source(
            "uk_fcdo", actor="alice", reason="new URL", expected_version=1, config=cfg
        )
    assert not r.applied and r.pending_change_id
    with tx() as conn:
        assert get_source(conn, "uk_fcdo").config_version == 1  # not applied yet
    with tx(actor="alice") as conn, pytest.raises(PermissionDenied, match="maker-checker"):
        ConfigService(conn).decide_change(r.pending_change_id, reviewer="alice", approve=True)
    with tx(actor="bob") as conn:
        assert (
            ConfigService(conn).decide_change(r.pending_change_id, reviewer="bob", approve=True) == "APPLIED"
        )
    with tx() as conn:
        s = get_source(conn, "uk_fcdo")
        assert s.config_version == 2 and s.config_raw["fetch"]["url"].endswith("v2.xml")


def test_host_outside_global_allow_list_refused(seeded):
    with tx(actor="alice") as conn, pytest.raises(ValidationFailed, match="global allow-list"):
        s = get_source(conn, "uk_fcdo")
        cfg = dict(s.config_raw)
        cfg["fetch"] = {
            **cfg["fetch"],
            "url": "https://evil.example.com/x.xml",
            "allowed_hosts": ["evil.example.com"],
        }
        ConfigService(conn).update_source(
            "uk_fcdo", actor="alice", reason="x", expected_version=1, config=cfg
        )


def test_status_rules_for_core_sources(seeded):
    with tx(actor="op") as conn, pytest.raises(ValidationFailed, match="reason"):
        ConfigService(conn).set_status("ofac_sdn", "PAUSED", actor="op", role="operator", reason="")
    with tx(actor="op") as conn, pytest.raises(PermissionDenied):
        ConfigService(conn).set_status(
            "ofac_sdn", "DISABLED", actor="op", role="operator", reason="maintenance"
        )
    until = datetime.now(UTC) + timedelta(hours=2)
    with tx(actor="op") as conn:
        ConfigService(conn).set_status(
            "ofac_sdn", "PAUSED", actor="op", role="operator", reason="vendor window", paused_until=until
        )
        inc = fetch_one(conn, "SELECT * FROM incident WHERE source_id = 'ofac_sdn'")
        assert inc["error_class"] == "CORE_SOURCE_DISABLED" and inc["status"] == "OPEN"
    with tx(actor="admin") as conn:
        ConfigService(conn).set_status("ofac_sdn", "ACTIVE", actor="admin", role="admin", reason="done")
        assert fetch_val(conn, "SELECT status FROM incident WHERE source_id = 'ofac_sdn'") == "RESOLVED"


def test_new_source_draft_activation_and_rollback(seeded):
    with tx(actor="alice") as conn:
        svc = ConfigService(conn)
        svc.create_source(
            source_id="ofac_sdn_mirror",
            display_name="OFAC SDN test",
            adapter_type="ofac_advanced_xml",
            config={
                "fetch": {
                    "url": "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML",
                    "allowed_hosts": ["sanctionslistservice.ofac.treas.gov"],
                }
            },
            schedule=Schedule.from_seed(
                {"kind": "INTERVAL", "cadence_minutes": 240, "min_interval_minutes": 60}
            ),
            actor="alice",
            reason="test",
        )
        with pytest.raises(PermissionDenied):
            svc.set_status("ofac_sdn_mirror", "ACTIVE", actor="alice", role="admin", reason="go")
        cid = svc.request_activation("ofac_sdn_mirror", actor="alice", reason="ready")
    with tx(actor="bob") as conn:
        ConfigService(conn).decide_change(cid, reviewer="bob", approve=True)
        assert get_source(conn, "ofac_sdn_mirror").status == "ACTIVE"
        ConfigService(conn).update_source(
            "ofac_sdn_mirror",
            actor="bob",
            reason="x",
            expected_version=1,
            config={**get_source(conn, "ofac_sdn_mirror").config_raw, "validation": {"min_records": 7}},
        )
    with tx(actor="bob") as conn:
        r = ConfigService(conn).rollback("ofac_sdn_mirror", 1, actor="bob", reason="undo", expected_version=2)
        assert r.applied and r.version == 3
        assert get_source(conn, "ofac_sdn_mirror").config_raw["validation"]["min_records"] == 1


def test_export_roundtrip(seeded):
    with tx() as conn:
        text = export_seed(conn)
    assert "ofac_sdn" in text and "SDN_ADVANCED.XML" in text
