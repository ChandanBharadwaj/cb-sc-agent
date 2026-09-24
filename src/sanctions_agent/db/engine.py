"""Postgres access: a psycopg 3 connection pool plus small transaction helpers.

The code uses explicit SQL (no ORM) so every statement that touches compliance data is visible and
reviewable. ``tx(actor=...)`` sets ``sanctions.actor`` for the audit trigger.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from sanctions_agent.settings import get_settings

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()

Row = dict[str, Any]


def _configure(conn: psycopg.Connection[Any]) -> None:
    conn.execute("SET TIME ZONE 'UTC'")
    conn.execute("SET search_path = sanctions, public")
    conn.commit()


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                s = get_settings()
                _pool = ConnectionPool(
                    s.database_url,
                    min_size=s.db_pool_min,
                    max_size=s.db_pool_max,
                    kwargs={"row_factory": dict_row, "autocommit": False},
                    configure=_configure,
                    open=True,
                    name="sanctions",
                )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def reset_pool_for_tests() -> None:
    close_pool()


@contextmanager
def tx(actor: str | None = None) -> Iterator[psycopg.Connection[Row]]:
    """A transaction: commits on success, rolls back on exception."""
    with get_pool().connection() as conn:
        with conn.transaction():
            if actor:
                conn.execute("SELECT set_config('sanctions.actor', %s, true)", (actor,))
            yield conn  # type: ignore[misc]


def fetch_all(conn: psycopg.Connection[Any], sql: str, params: Sequence[Any] | dict[str, Any] | None = None) -> list[Row]:
    return list(conn.execute(sql, params).fetchall())  # type: ignore[arg-type]


def fetch_one(conn: psycopg.Connection[Any], sql: str, params: Sequence[Any] | dict[str, Any] | None = None) -> Row | None:
    return conn.execute(sql, params).fetchone()  # type: ignore[arg-type, no-any-return]


def fetch_val(conn: psycopg.Connection[Any], sql: str, params: Sequence[Any] | dict[str, Any] | None = None) -> Any:
    row = conn.execute(sql, params).fetchone()  # type: ignore[arg-type]
    if row is None:
        return None
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value)


def notify(conn: psycopg.Connection[Any], channel: str, payload: str) -> None:
    conn.execute("SELECT pg_notify(%s, %s)", (channel, payload[:7900]))
