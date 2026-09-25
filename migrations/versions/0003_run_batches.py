"""Run batches: group per-source runs started together (scheduled cycle, agent cycle, ad-hoc request).

Revision ID: 0003_run_batches
Revises: 0002_analytics
Create Date: 2026-09-25
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from migrations_helpers import run_sql_file  # noqa: E402

revision = "0003_run_batches"
down_revision = "0002_analytics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0003_run_batches.sql")


def downgrade() -> None:
    raise NotImplementedError("forward-only migrations")
