"""Run lifecycle: queue, claim (lease), heartbeat, step checkpoints, finish, reclaim abandoned runs.

The queue lives in Postgres (``ingestion_run``) - claims use ``FOR UPDATE SKIP LOCKED`` so several
workers can run safely, and a partial unique index guarantees one active run per source.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import psycopg

from sanctions_agent.db.engine import fetch_all, fetch_one, fetch_val, jsonb, notify, tx
from sanctions_agent.logs import get_logger

log = get_logger(__name__)

ACTIVE = ("QUEUED", "RUNNING")
TERMINAL = ("SUCCEEDED", "NO_CHANGE", "DRY_RUN_OK", "HELD", "QUARANTINED", "FAILED", "ABANDONED", "CANCELLED")
MAX_RESUMES = 3


# ---------------------------------------------------------------------------------------------
# Batches: the per-source runs started together (one scheduled cycle, one agent cycle, one ad-hoc request)
# ---------------------------------------------------------------------------------------------
def batch_trigger(run_trigger: str) -> str:
    return {"AGENT": "AGENT", "MANUAL": "MANUAL"}.get(run_trigger, "SCHEDULED")


def create_batch(
    conn: psycopg.Connection[Any],
    *,
    trigger: str,
    requested_by: str,
    reason: str | None = None,
    options: dict[str, Any] | None = None,
    agent_cycle_id: str | None = None,
    requested_sources: list[str] | tuple[str, ...] = (),
) -> str:
    return str(
        fetch_val(
            conn,
            """INSERT INTO run_batch (trigger, requested_by, reason, options, requested_sources, agent_cycle_id)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING batch_id""",
            (trigger, requested_by, reason, jsonb(options or {}), list(requested_sources), agent_cycle_id),
        )
    )


def _note_requested(conn: psycopg.Connection[Any], batch_id: str, source_ids: list[str]) -> None:
    conn.execute(
        """UPDATE run_batch SET requested_sources = requested_sources
               || ARRAY(SELECT unnest(%s::text[]) EXCEPT SELECT unnest(requested_sources))
           WHERE batch_id = %s""",
        (source_ids, batch_id),
    )


def record_refused(conn: psycopg.Connection[Any], batch_id: str, refused: list[dict[str, Any]]) -> None:
    """Sources the requester asked for that could not be queued, with the guard's reason."""
    if refused:
        _note_requested(conn, batch_id, [r["source_id"] for r in refused])
        conn.execute(
            "UPDATE run_batch SET refused = refused || %s WHERE batch_id = %s", (jsonb(refused), batch_id)
        )


def batch_for_cycle(conn: psycopg.Connection[Any], cycle_id: str, requested_by: str) -> str:
    """The one batch that groups every run an agent cycle starts."""
    existing = fetch_val(
        conn, "SELECT batch_id FROM run_batch WHERE agent_cycle_id = %s AND trigger = 'AGENT'", (cycle_id,)
    )
    if existing:
        return str(existing)
    return create_batch(
        conn, trigger="AGENT", requested_by=requested_by, reason="agent cycle", agent_cycle_id=cycle_id
    )


class BatchRef:
    """A batch created on first use, so a scheduler tick that queues nothing leaves no empty batch."""

    def __init__(self, *, trigger: str, requested_by: str, reason: str | None = None) -> None:
        self.trigger, self.requested_by, self.reason = trigger, requested_by, reason
        self.batch_id: str | None = None

    def get(self, conn: psycopg.Connection[Any]) -> str:
        if self.batch_id is None:
            self.batch_id = create_batch(
                conn, trigger=self.trigger, requested_by=self.requested_by, reason=self.reason
            )
        return self.batch_id


class RunAlreadyActive(Exception):
    def __init__(self, run_id: str) -> None:
        super().__init__(f"source already has an active run {run_id}")
        self.run_id = run_id


def enqueue_run(
    conn: psycopg.Connection[Any],
    *,
    source_id: str,
    run_kind: str,
    trigger: str,
    requested_by: str,
    reason: str | None = None,
    options: dict[str, Any] | None = None,
    not_before: datetime | None = None,
    agent_cycle_id: str | None = None,
    resumed_from_run_id: str | None = None,
    parent_run_id: str | None = None,
    attempt: int = 1,
    batch_id: str | BatchRef | None = None,
) -> str:
    """Queue a run. Raises RunAlreadyActive (with the active run id) if the source is busy.

    ``batch_id`` groups it with other runs started together; without one the run is a batch of one."""
    existing = fetch_val(
        conn,
        "SELECT run_id FROM ingestion_run WHERE source_id = %s AND status IN ('QUEUED','RUNNING')",
        (source_id,),
    )
    if existing:
        raise RunAlreadyActive(str(existing))
    if isinstance(batch_id, BatchRef):
        batch_id = batch_id.get(conn)
    if batch_id is not None:
        _note_requested(conn, batch_id, [source_id])
    else:
        batch_id = create_batch(
            conn,
            trigger=batch_trigger(trigger),
            requested_by=requested_by,
            reason=reason,
            options=options,
            agent_cycle_id=agent_cycle_id,
            requested_sources=[source_id],
        )
    cfg_version = fetch_val(conn, "SELECT config_version FROM source WHERE source_id = %s", (source_id,))
    try:
        with conn.transaction():
            run_id = fetch_val(
                conn,
                """INSERT INTO ingestion_run (source_id, run_kind, trigger, requested_by, reason, options, not_before,
                       agent_cycle_id, resumed_from_run_id, parent_run_id, attempt, config_version, batch_id)
                   VALUES (%s,%s,%s,%s,%s,%s,coalesce(%s, now()),%s,%s,%s,%s,%s,%s) RETURNING run_id""",
                (
                    source_id,
                    run_kind,
                    trigger,
                    requested_by,
                    reason,
                    jsonb(options or {}),
                    not_before,
                    agent_cycle_id,
                    resumed_from_run_id,
                    parent_run_id,
                    attempt,
                    cfg_version,
                    batch_id,
                ),
            )
    except psycopg.errors.UniqueViolation as e:
        active = fetch_val(
            conn,
            "SELECT run_id FROM ingestion_run WHERE source_id = %s AND status IN ('QUEUED','RUNNING')",
            (source_id,),
        )
        raise RunAlreadyActive(str(active)) from e
    notify(
        conn,
        "run_progress",
        json.dumps({"run_id": str(run_id), "source_id": source_id, "status": "QUEUED", "batch_id": batch_id}),
    )
    return str(run_id)


def claim_next(conn: psycopg.Connection[Any], worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
    row = fetch_one(
        conn,
        """SELECT r.run_id FROM ingestion_run r JOIN source s USING (source_id)
           WHERE r.status = 'QUEUED' AND r.not_before <= now()
           ORDER BY s.priority, r.queued_at
           FOR UPDATE OF r SKIP LOCKED LIMIT 1""",
    )
    if row is None:
        return None
    return fetch_one(
        conn,
        """UPDATE ingestion_run SET status = 'RUNNING', lease_owner = %s,
               lease_expires_at = now() + make_interval(secs => %s), heartbeat_at = now(),
               started_at = coalesce(started_at, now())
           WHERE run_id = %s RETURNING *""",
        (worker_id, lease_seconds, row["run_id"]),
    )


def heartbeat(run_id: str, worker_id: str, lease_seconds: int) -> bool:
    """Extend the lease. Returns True if cancellation was requested."""
    with tx() as conn:
        row = fetch_one(
            conn,
            """UPDATE ingestion_run SET heartbeat_at = now(), lease_expires_at = now() + make_interval(secs => %s)
               WHERE run_id = %s AND lease_owner = %s AND status = 'RUNNING' RETURNING cancel_requested_at""",
            (lease_seconds, run_id, worker_id),
        )
    return bool(row and row["cancel_requested_at"])


def cancel_requested(run_id: str) -> bool:
    with tx() as conn:
        return bool(
            fetch_val(
                conn, "SELECT cancel_requested_at IS NOT NULL FROM ingestion_run WHERE run_id = %s", (run_id,)
            )
        )


def request_cancel(conn: psycopg.Connection[Any], run_id: str, actor: str) -> str:
    row = fetch_one(conn, "SELECT status FROM ingestion_run WHERE run_id = %s FOR UPDATE", (run_id,))
    if row is None:
        raise KeyError(run_id)
    if row["status"] == "QUEUED":
        conn.execute(
            "UPDATE ingestion_run SET status = 'CANCELLED', cancelled_by = %s, cancel_requested_at = now(),"
            " finished_at = now(), error_class = 'CANCELLED' WHERE run_id = %s",
            (actor, run_id),
        )
        return "CANCELLED"
    if row["status"] == "RUNNING":
        conn.execute(
            "UPDATE ingestion_run SET cancel_requested_at = now(), cancelled_by = %s WHERE run_id = %s",
            (actor, run_id),
        )
        return "CANCEL_REQUESTED"
    return row["status"]  # type: ignore[no-any-return]


def step_start(run_id: str, step: str) -> int:
    with tx() as conn:
        attempt = int(
            fetch_val(
                conn,
                "SELECT coalesce(max(attempt), 0) + 1 FROM run_step WHERE run_id = %s AND step = %s",
                (run_id, step),
            )
        )
        conn.execute(
            "INSERT INTO run_step (run_id, step, attempt, status) VALUES (%s, %s, %s, 'RUNNING')",
            (run_id, step, attempt),
        )
        conn.execute("UPDATE ingestion_run SET current_step = %s WHERE run_id = %s", (step, run_id))
    return attempt


def step_finish(
    conn: psycopg.Connection[Any],
    run_id: str,
    step: str,
    attempt: int,
    *,
    status: str = "DONE",
    detail: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE run_step SET status = %s, finished_at = now(), detail = %s, error = %s"
        " WHERE run_id = %s AND step = %s AND attempt = %s",
        (status, jsonb(detail or {}), error, run_id, step, attempt),
    )


def completed_steps(conn: psycopg.Connection[Any], run_id: str) -> dict[str, dict[str, Any]]:
    rows = fetch_all(
        conn,
        "SELECT step, detail FROM run_step WHERE run_id = %s AND status = 'DONE' ORDER BY started_at",
        (run_id,),
    )
    return {r["step"]: r["detail"] for r in rows}


def finish_run(
    conn: psycopg.Connection[Any],
    run_id: str,
    status: str,
    *,
    error_class: str | None = None,
    error_detail: str | None = None,
    summary: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """UPDATE ingestion_run SET status = %s, finished_at = now(), error_class = %s, error_detail = %s,
               summary = summary || %s, lease_owner = NULL, lease_expires_at = NULL,
               progress = progress || jsonb_build_object('step', 'DONE', 'pct', 100)
           WHERE run_id = %s""",
        (status, error_class, (error_detail or "")[:4000] or None, jsonb(summary or {}), run_id),
    )
    row = fetch_one(conn, "SELECT source_id, batch_id FROM ingestion_run WHERE run_id = %s", (run_id,))
    notify(
        conn,
        "run_progress",
        json.dumps(
            {
                "run_id": run_id,
                "source_id": row["source_id"] if row else None,
                "status": status,
                "batch_id": str(row["batch_id"]) if row else None,
            }
        ),
    )


def reclaim_abandoned(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    """Runs whose lease expired (worker died) become ABANDONED and are resumed from their checkpoint.

    Returns a list of {abandoned_run_id, resumed_run_id | None, source_id}.
    """
    out = []
    rows = fetch_all(
        conn,
        """UPDATE ingestion_run SET status = 'ABANDONED', finished_at = now(), error_class = 'INTERNAL',
               error_detail = 'worker lease expired (process died or hung); resuming from checkpoint'
           WHERE status = 'RUNNING' AND lease_expires_at < now()
           RETURNING run_id, source_id, run_kind, trigger, requested_by, reason, options, attempt, agent_cycle_id,
                     resumed_from_run_id, batch_id""",
    )
    for r in rows:
        chain = int(
            fetch_val(
                conn,
                """WITH RECURSIVE c AS (SELECT run_id, resumed_from_run_id FROM ingestion_run WHERE run_id = %s
                 UNION ALL SELECT i.run_id, i.resumed_from_run_id FROM ingestion_run i JOIN c ON i.run_id = c.resumed_from_run_id)
               SELECT count(*) FROM c""",
                (r["run_id"],),
            )
        )
        status = fetch_val(conn, "SELECT status FROM source WHERE source_id = %s", (r["source_id"],))
        new_id = None
        if chain <= MAX_RESUMES and status == "ACTIVE":
            try:
                new_id = enqueue_run(
                    conn,
                    source_id=r["source_id"],
                    run_kind=r["run_kind"],
                    trigger="RESUME",
                    requested_by="system:reclaimer",
                    reason=f"resume abandoned run {r['run_id']}",
                    options=r["options"],
                    agent_cycle_id=r["agent_cycle_id"],
                    resumed_from_run_id=str(r["run_id"]),
                    attempt=r["attempt"] + 1,
                    batch_id=str(r["batch_id"]),  # the resumed attempt stays in its original batch
                )
            except RunAlreadyActive:
                new_id = None
        conn.execute(
            "UPDATE list_version SET status = 'REJECTED', validation_report = validation_report || %s"
            " WHERE run_id = %s AND status IN ('CANDIDATE','VALIDATED')",
            (jsonb({"rejected": "run abandoned"}), r["run_id"]),
        )
        # the resumed run re-parses from the archived bytes, so the dead run's partial staging is discarded
        conn.execute("DELETE FROM staging_path_stats WHERE run_id = %s", (r["run_id"],))
        conn.execute("DELETE FROM staging_record WHERE run_id = %s", (r["run_id"],))
        log.warning(
            "run_abandoned", run_id=str(r["run_id"]), source_id=r["source_id"], resumed_as=new_id, chain=chain
        )
        out.append(
            {
                "abandoned_run_id": str(r["run_id"]),
                "resumed_run_id": new_id,
                "source_id": r["source_id"],
                "resume_chain": chain,
            }
        )
    return out


def new_worker_id(prefix: str = "worker") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"
