"""Read-only connection for the Q&A agent, authenticated as ``sanctions_analyst``.

That role can read only the aggregate ``analytics`` views (migration 0002), so no tool bug or prompt
injection can reach record-level list data. Every query also runs in a READ ONLY transaction with a
statement timeout.
"""

from __future__ import annotations

import atexit
import threading
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from sanctions_agent.settings import get_settings

_pool: ConnectionPool | None = None
_lock = threading.Lock()
MAX_ROWS = 500


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = ConnectionPool(
                    get_settings().analyst_database_url,
                    min_size=0,
                    max_size=3,
                    kwargs={"row_factory": dict_row, "autocommit": False},
                    open=True,
                    name="analyst",
                )
                atexit.register(close)
    return _pool


def close() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def query(
    sql: str, params: tuple[Any, ...] | dict[str, Any] | None = None, *, max_rows: int = MAX_ROWS
) -> list[dict[str, Any]]:
    with pool().connection() as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '5s'")
        conn.execute("SET LOCAL search_path = analytics")
        cur = conn.execute(sql, params)  # type: ignore[arg-type]
        rows = cur.fetchmany(max_rows) if cur.description else []
        return [dict(r) for r in rows]
