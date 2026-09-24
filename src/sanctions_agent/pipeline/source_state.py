"""Per-source health bookkeeping after each run: freshness timestamps, next due time and the persisted
circuit breaker (CLOSED -> OPEN after repeated failures -> HALF_OPEN probe -> CLOSED)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from sanctions_agent.db.engine import fetch_one
from sanctions_agent.scheduling.schedule import Schedule

BREAKER_THRESHOLD = 3
BREAKER_BASE = timedelta(minutes=30)
BREAKER_MAX = timedelta(hours=6)
RETRY_BASE = timedelta(minutes=10)
RETRY_MAX = timedelta(hours=2)


def on_success(
    conn: psycopg.Connection[Any],
    source_id: str,
    *,
    changed: bool,
    fetched: bool = True,
    fresh: bool | None = None,
) -> None:
    """``fetched``: the publisher was contacted (politeness clock). ``fresh``: the data is new from the publisher
    (freshness SLO) - true for fetches, manual loads and curated builds, false for re-parses of archived bytes."""
    fresh = fetched if fresh is None else fresh
    row = fetch_one(conn, "SELECT * FROM source WHERE source_id = %s FOR UPDATE", (source_id,))
    assert row is not None
    now = datetime.now(UTC)
    nxt = Schedule.from_row(row).next_after(now)
    conn.execute(
        """UPDATE source SET last_success_at = CASE WHEN %s THEN %s ELSE last_success_at END,
               last_attempt_at = CASE WHEN %s THEN %s ELSE last_attempt_at END,
               last_fetch_at = CASE WHEN %s THEN %s ELSE last_fetch_at END,
               last_change_at = CASE WHEN %s THEN %s ELSE last_change_at END,
               consecutive_failures = 0, breaker_state = 'CLOSED', breaker_opened_at = NULL, breaker_retry_at = NULL,
               next_due_at = %s
           WHERE source_id = %s""",
        (fresh, now, fetched, now, fetched, now, changed, now, nxt, source_id),
    )


def on_data_problem(conn: psycopg.Connection[Any], source_id: str, *, fetched: bool = True) -> None:
    """File not publishable (quarantined / held): the source is reachable, data is stale.

    ``fetched`` is False for re-parses of archived bytes: the publisher was not contacted, so the politeness
    clock (``last_attempt_at``) and the schedule are left alone."""
    if not fetched:
        return
    row = fetch_one(conn, "SELECT * FROM source WHERE source_id = %s FOR UPDATE", (source_id,))
    assert row is not None
    now = datetime.now(UTC)
    conn.execute(
        "UPDATE source SET last_attempt_at = %s, last_fetch_at = %s, next_due_at = %s WHERE source_id = %s",
        (now, now, Schedule.from_row(row).next_after(now), source_id),
    )


def on_failure(conn: psycopg.Connection[Any], source_id: str) -> dict[str, Any]:
    """Fetch-level failure: bump failures, open the breaker after N in a row, schedule a sooner retry."""
    row = fetch_one(conn, "SELECT * FROM source WHERE source_id = %s FOR UPDATE", (source_id,))
    assert row is not None
    now = datetime.now(UTC)
    failures = row["consecutive_failures"] + 1
    schedule = Schedule.from_row(row)
    retry = min(RETRY_MAX, RETRY_BASE * (2 ** (failures - 1)))
    retry = max(retry, schedule.min_interval)
    next_due = min(schedule.next_after(now), now + retry)
    state, opened, retry_at = row["breaker_state"], row["breaker_opened_at"], row["breaker_retry_at"]
    if failures >= BREAKER_THRESHOLD or state == "HALF_OPEN":
        backoff = min(BREAKER_MAX, BREAKER_BASE * (2 ** max(0, failures - BREAKER_THRESHOLD)))
        state, opened, retry_at = "OPEN", opened or now, now + backoff
        next_due = retry_at
    conn.execute(
        """UPDATE source SET last_attempt_at = %s, consecutive_failures = %s, breaker_state = %s,
               breaker_opened_at = %s, breaker_retry_at = %s, next_due_at = %s WHERE source_id = %s""",
        (now, failures, state, opened, retry_at, next_due, source_id),
    )
    return {"consecutive_failures": failures, "breaker_state": state, "next_due_at": next_due.isoformat()}


def breaker_allows(conn: psycopg.Connection[Any], source_id: str) -> tuple[bool, str]:
    """Whether a run may start now. An OPEN breaker past its retry time becomes HALF_OPEN (one probe)."""
    row = fetch_one(
        conn,
        "SELECT breaker_state, breaker_retry_at FROM source WHERE source_id = %s FOR UPDATE",
        (source_id,),
    )
    assert row is not None
    if row["breaker_state"] == "OPEN":
        if row["breaker_retry_at"] and row["breaker_retry_at"] <= datetime.now(UTC):
            conn.execute("UPDATE source SET breaker_state = 'HALF_OPEN' WHERE source_id = %s", (source_id,))
            return True, "half-open probe"
        return False, f"breaker open until {row['breaker_retry_at']}"
    return True, row["breaker_state"].lower()
