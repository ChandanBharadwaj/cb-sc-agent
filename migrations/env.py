"""Alembic environment. Migrations are plain SQL files in migrations/sql, executed verbatim."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from sanctions_agent.settings import get_settings


def _sqlalchemy_url() -> str:
    url = context.get_x_argument(as_dictionary=True).get("url") or get_settings().database_url
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def run_migrations_offline() -> None:
    context.configure(url=_sqlalchemy_url(), literal_binds=True, version_table_schema="public")
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_sqlalchemy_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, version_table_schema="public", transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
