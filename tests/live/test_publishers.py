"""Live conformance against the real publishers. Run from the production/corporate network before go-live:

    uv run pytest -m live -v

Each Level 1 list is downloaded through the guarded client (allow-list, redirects, TLS), validated and parsed;
unknown element paths are reported as drift. Nothing is published. Deselected by default."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from sanctions_agent.cli import app
from tests.integration.test_pipeline import seeded  # noqa: F401

pytestmark = pytest.mark.live


@pytest.mark.parametrize("source_id", ["ofac_sdn", "ofac_cons", "un_sc", "uk_fcdo", "eu_fsf", "us_csl"])
def test_publisher_file_downloads_and_parses(seeded, source_id, tmp_path):  # noqa: F811
    import json

    out = tmp_path / "verify.json"
    r = CliRunner().invoke(app, ["verify-sources", "-s", source_id, "--out", str(out)])
    assert r.exit_code == 0, r.output
    report = json.loads(out.read_text())[source_id]
    assert report["http_status"] == 200 and report["records"] > 100, report
    # drift is reported, not failed: review report["unknown_paths"] and update schemas/known_paths if genuine
    print(
        source_id, report["records"], report["counts_by_type"], "unknown paths:", len(report["unknown_paths"])
    )
