"""Core schema: control plane, evidence, staging, versioned list data, L2, review, agent, quality, Q&A.

Revision ID: 0001_init
Revises:
Create Date: 2026-09-24
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from migrations_helpers import run_sql_file  # noqa: E402

revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0001_init.sql")


def downgrade() -> None:
    raise NotImplementedError("forward-only migrations; restore from backup to roll back")
