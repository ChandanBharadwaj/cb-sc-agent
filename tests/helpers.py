"""Test helpers: publish a fixture file through the real pipeline without HTTP (the manual-load path)."""

from __future__ import annotations

from pathlib import Path

from sanctions_agent.db.engine import tx
from sanctions_agent.pipeline.runner import PipelineRunner, run_once
from sanctions_agent.storage.blobstore import get_blob_store, sha256_file

FX = Path(__file__).resolve().parent / "fixtures"


def archive(rel: str) -> str:
    path = FX / rel
    sha = sha256_file(path)
    uri = get_blob_store().put_file(path, sha)
    with tx() as conn:
        conn.execute(
            "INSERT INTO raw_artifact (sha256, blob_uri, size_bytes, content_type, compression)"
            " VALUES (%s, %s, %s, 'application/octet-stream', 'gzip') ON CONFLICT DO NOTHING",
            (sha, uri, path.stat().st_size),
        )
    return sha


def load_fixture(source_id: str, rel: str, **options: object) -> tuple[str, str, str]:
    """Archive ``tests/fixtures/<rel>`` and run the pipeline on it. Returns (run_id, status, sha256)."""
    sha = archive(rel)
    run_id, status = run_once(
        source_id,
        requested_by="test",
        reason=f"fixture {rel}",
        options={"reparse_sha256": sha, **options},
        runner=PipelineRunner(sleep=lambda s: None),
    )
    return run_id, status, sha
