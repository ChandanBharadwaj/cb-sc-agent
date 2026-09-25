"""Dev / demo data - NEVER run against production.

Loads the parser fixtures through the real pipeline (manual-load path: archive -> validate -> parse -> publish)
so the console has published versions, changes, quality metrics and a quarantine to look at:

1. relaxes each Level 1 list's validation thresholds (fixtures hold a handful of records, production minimums
   are thousands) - via the audited config service, so it shows up in each source's history;
2. loads OFAC, UN (v1 then v2 -> real ADD/CHANGE/REMOVE events and a held removal), UK, EU FSF and CSL;
3. sets an impossible fill-rate floor on the UK list and re-parses the same file -> QUARANTINED, last good kept.

Usage:  SANCTIONS_DATABASE_URL=... uv run python scripts/dev_seed.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from sanctions_agent.db.engine import fetch_one, tx
from sanctions_agent.pipeline import runs
from sanctions_agent.pipeline.runner import run_once
from sanctions_agent.settings import get_settings
from sanctions_agent.sources.config_service import ConfigService
from sanctions_agent.storage.blobstore import get_blob_store, sha256_file

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
LOADS = [
    ("ofac_sdn", "ofac/sdn_advanced_v1.xml"),
    ("un_sc", "un/consolidated_v1.xml"),
    ("uk_fcdo", "uk/uk_sanctions_list_v1.xml"),
    ("eu_fsf", "eu/fsf_v1.xml"),
    ("us_csl", "csl/consolidated_v1.json"),
    ("un_sc", "un/consolidated_v2.xml"),
]


def _patch_validation(source_id: str, **changes: Any) -> None:
    with tx(actor="dev-seed") as conn:
        row = fetch_one(conn, "SELECT config, config_version FROM source WHERE source_id = %s", (source_id,))
        assert row is not None, f"unknown source {source_id} - run `sanctions-agent sources import` first"
        cfg = dict(row["config"])
        cfg["validation"] = {**cfg.get("validation", {}), **changes}
        ConfigService(conn).update_source(
            source_id,
            actor="dev-seed",
            reason="dev demo: fixture files are tiny",
            expected_version=row["config_version"],
            config=cfg,
            trusted=True,
        )


def _load(source_id: str, path: Path, reason: str, batch_id: str | None = None) -> tuple[str, str]:
    sha = sha256_file(path)
    uri = get_blob_store().put_file(path, sha)
    with tx(actor="dev-seed") as conn:
        conn.execute(
            "INSERT INTO raw_artifact (sha256, blob_uri, size_bytes, content_type, compression)"
            " VALUES (%s, %s, %s, 'application/octet-stream', 'gzip') ON CONFLICT DO NOTHING",
            (sha, uri, path.stat().st_size),
        )
    return run_once(
        source_id,
        requested_by="dev-seed",
        reason=reason,
        options={"reparse_sha256": sha, "manual_file": path.name, "manual_load": True},
        batch_id=batch_id,
    )


def main() -> None:
    if get_settings().environment in ("prod", "production"):
        sys.exit("refusing to seed demo data into a production environment")
    for sid in sorted({s for s, _ in LOADS}):
        _patch_validation(
            sid,
            min_records=1,
            fill_floors={},
            max_removed_pct=100.0,
            max_removed_abs=1000,
            drift_policy="warn",
        )
    # the first five files arrive as one batch (like one scheduled cycle), the UN update as a second
    with tx(actor="dev-seed") as conn:
        first = runs.create_batch(
            conn,
            trigger="MANUAL",
            requested_by="dev-seed",
            reason="initial load of the official lists",
            options={"mode": "normal"},
            requested_sources=[s for s, _ in LOADS[:5]],
        )
    for i, (sid, rel) in enumerate(LOADS):
        if i == 5:  # the UN update arrives later, as its own batch
            with tx(actor="dev-seed") as conn:
                first = runs.create_batch(
                    conn,
                    trigger="MANUAL",
                    requested_by="dev-seed",
                    reason="UN list update",
                    options={"mode": "normal"},
                    requested_sources=["un_sc"],
                )
        run_id, status = _load(sid, FIXTURES / rel, f"dev demo: {rel}", first)
        print(f"{sid:10s} {rel:32s} {status:12s} {run_id}")
    # a deliberate fill-rate quarantine: the published version stays in screening
    _patch_validation("uk_fcdo", fill_floors={"PERSON.dob": 0.8, "PERSON.passport": 0.9})
    run_id, status = _load("uk_fcdo", FIXTURES / "uk/uk_sanctions_list_v1.xml", "dev demo: floor raised")
    print(f"{'uk_fcdo':10s} {'(floors raised, re-parse)':32s} {status:12s} {run_id}")


if __name__ == "__main__":
    main()
