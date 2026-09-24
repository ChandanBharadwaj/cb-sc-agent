"""Shared test fixtures: a migrated Postgres test database, isolated blob store and settings."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

TEST_DB_URL = os.environ.get(
    "SANCTIONS_TEST_DATABASE_URL", "postgresql://sanctions:sanctions@localhost:5432/sanctions_test"
)
FIXTURES = Path(__file__).parent / "fixtures"

os.environ["SANCTIONS_DATABASE_URL"] = TEST_DB_URL
os.environ["SANCTIONS_ANALYST_DATABASE_URL"] = TEST_DB_URL.replace(
    "sanctions:sanctions@", "sanctions_analyst:sanctions_analyst@", 1
)
os.environ.setdefault("SANCTIONS_ANALYST_PASSWORD", "sanctions_analyst")
os.environ["SANCTIONS_ENVIRONMENT"] = "test"
os.environ["SANCTIONS_AUTH_MODE"] = "basic"
os.environ["SANCTIONS_HTTP_ALLOW_PRIVATE_NETWORKS"] = "true"  # skip DNS lookups for mocked hosts
os.environ.pop("OPENAI_API_KEY", None)


def _db_available() -> bool:
    import psycopg

    try:
        with psycopg.connect(TEST_DB_URL, connect_timeout=3):
            return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def migrated_db() -> str:
    if not _db_available():
        pytest.skip("test Postgres not available")
    import psycopg
    from alembic import command
    from alembic.config import Config

    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS sanctions CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS analytics CASCADE")
        conn.execute("DROP TABLE IF EXISTS public.alembic_version")
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.cmd_opts = type("o", (), {"x": [f"url={TEST_DB_URL}"]})()  # type: ignore[assignment]
    command.upgrade(cfg, "head")
    return TEST_DB_URL


@pytest.fixture()
def tmp_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from sanctions_agent import settings as settings_mod

    monkeypatch.setenv("SANCTIONS_BLOB_ROOT", str(tmp_path / "blobs"))
    monkeypatch.setenv("SANCTIONS_WORK_DIR", str(tmp_path / "work"))
    settings_mod.get_settings.cache_clear()
    yield
    settings_mod.get_settings.cache_clear()


@pytest.fixture()
def db(migrated_db: str, tmp_settings: None) -> Iterator[None]:
    """Clean database state for each test (TRUNCATE bypasses the append-only row triggers)."""
    import psycopg

    from sanctions_agent.db import engine

    with psycopg.connect(migrated_db, autocommit=True) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT format('%I.%I', schemaname, tablename) FROM pg_tables WHERE schemaname = 'sanctions'"
                " AND tablename NOT LIKE '%\\_2%' AND tablename NOT LIKE '%\\_default'"
            ).fetchall()
        ]
        conn.execute("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE")
    engine.reset_pool_for_tests()
    yield
    engine.reset_pool_for_tests()
