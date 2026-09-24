"""Helpers shared by migration scripts."""

from __future__ import annotations

from pathlib import Path

from alembic import op

SQL_DIR = Path(__file__).resolve().parent / "sql"


def run_sql_file(name: str) -> None:
    """Execute a SQL file verbatim on the migration connection (same transaction).

    Uses the raw psycopg connection so the SQL is not parsed for bind parameters (``:name``/``%``).
    """
    sql = (SQL_DIR / name).read_text(encoding="utf-8")
    driver_conn = op.get_bind().connection.driver_connection
    driver_conn.execute(sql)  # type: ignore[union-attr]
