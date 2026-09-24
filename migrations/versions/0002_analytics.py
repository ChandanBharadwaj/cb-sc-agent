"""Aggregate-only analytics views and the read-only sanctions_analyst role.

Revision ID: 0002_analytics
Revises: 0001_init
Create Date: 2026-09-24

If SANCTIONS_ANALYST_PASSWORD is set, the role is given LOGIN with that password (dev/test convenience).
In production a DBA should manage the password (``ALTER ROLE sanctions_analyst LOGIN PASSWORD ...``).
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alembic import op  # noqa: E402
from migrations_helpers import run_sql_file  # noqa: E402

revision = "0002_analytics"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0002_analytics.sql")
    password = os.environ.get("SANCTIONS_ANALYST_PASSWORD")
    if password:
        from psycopg import sql

        driver_conn = op.get_bind().connection.driver_connection
        driver_conn.execute(  # type: ignore[union-attr]
            sql.SQL("ALTER ROLE sanctions_analyst LOGIN PASSWORD {}").format(sql.Literal(password))
        )


def downgrade() -> None:
    raise NotImplementedError("forward-only migrations")
