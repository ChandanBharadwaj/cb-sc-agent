"""The supervisor process: one tick every ``tick_seconds``.

Each tick:
1. reclaim abandoned runs (resume from checkpoint)
2. housekeeping + watchdog (staleness incidents, forced pulls) - always deterministic
3. decide whether the LLM supervisor is needed (gate); if yes and it is enabled, within budget and
   healthy, it plans and acts through guarded tools; otherwise - or if it fails - autopilot plans
4. workers claim and execute queued runs
5. alerts are dispatched
"""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from sanctions_agent.db.engine import fetch_val, tx
from sanctions_agent.logs import get_logger
from sanctions_agent.ops import alerts, autopilot, system_settings
from sanctions_agent.ops.worker import WorkerPool
from sanctions_agent.pipeline import runs
from sanctions_agent.settings import get_settings

log = get_logger(__name__)


class AgentCycleRunner(Protocol):
    def run_cycle(self, trigger: str, gate_reasons: list[str]) -> dict[str, Any]: ...


class Supervisor:
    def __init__(
        self,
        pool: WorkerPool | None = None,
        agent: AgentCycleRunner | None = None,
        alert_sender: Callable[..., Any] | None = None,
    ) -> None:
        self.pool = pool or WorkerPool()
        self.agent = agent
        self.alert_sender = alert_sender or alerts.send
        self._stop = threading.Event()
        self._last_partition_day: str | None = None

    # -----------------------------------------------------------------------------------------
    def tick(self) -> dict[str, Any]:
        out: dict[str, Any] = {"at": datetime.now(UTC).isoformat()}
        with tx(actor="system:reclaimer") as conn:
            out["reclaimed"] = runs.reclaim_abandoned(conn)
        self._daily()
        with tx() as conn:
            cfg = system_settings.get_all(conn)
            maintenance = bool((cfg.get("maintenance_mode") or {}).get("enabled"))
            last_review = fetch_val(
                conn,
                "SELECT max(started_at) FROM agent_cycle WHERE agent_name = 'supervisor'"
                " AND status IN ('SUCCEEDED','FAILED','TIMEOUT','BUDGET_EXCEEDED')",
            )
            reasons = (
                []
                if maintenance
                else autopilot.gate(
                    conn, last_review_at=last_review, review_minutes=int(cfg["agent_health_review_minutes"])
                )
            )
        out["maintenance"] = maintenance
        out["gate_reasons"] = reasons
        agent_used = False
        if reasons and self.agent is not None and cfg.get("agent_enabled", True) and not maintenance:
            try:
                out["agent"] = self.agent.run_cycle("tick", reasons)
                agent_used = out["agent"].get("status") == "SUCCEEDED"
            except Exception as e:  # the LLM must never stop ingestion
                log.exception("agent_cycle_failed")
                out["agent"] = {"status": "FAILED", "error": repr(e)}
        # The deterministic cycle always runs: chores + watchdog are unconditional, and scheduled pulls
        # the agent did not already queue are picked up here (enqueue is idempotent per source).
        rep = autopilot.autopilot_cycle("autopilot" if not agent_used else "autopilot:after-agent")
        out["autopilot"] = {
            "enqueued": rep.enqueued,
            "skipped": len(rep.skipped),
            "chores": rep.chores,
            "new_incidents": rep.incidents,
        }
        out["claimed"] = self.pool.poll()
        with tx() as conn:
            out["alerts_sent"] = alerts.dispatch(conn, self.alert_sender)
        return out

    def _daily(self) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if self._last_partition_day == today:
            return
        with tx() as conn:
            conn.execute("SELECT sanctions.ensure_month_partitions(2)")
            conn.execute(
                """DELETE FROM staging_record WHERE run_id IN (
                     SELECT r.run_id FROM ingestion_run r WHERE r.finished_at < now() - make_interval(days => %s)
                     AND NOT EXISTS (SELECT 1 FROM list_version v WHERE v.run_id = r.run_id AND v.status = 'HELD'))""",
                (get_settings().staging_retention_days,),
            )
        self._last_partition_day = today

    # -----------------------------------------------------------------------------------------
    def run_forever(self) -> None:
        s = get_settings()
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        signal.signal(signal.SIGINT, lambda *_: self._stop.set())
        log.info(
            "supervisor_start",
            worker_id=self.pool.worker_id,
            tick_seconds=s.tick_seconds,
            agent=bool(self.agent),
        )
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                out = self.tick()
                if out.get("autopilot", {}).get("enqueued") or out.get("claimed") or out.get("reclaimed"):
                    log.info(
                        "tick",
                        enqueued=len(out["autopilot"]["enqueued"]),
                        claimed=len(out["claimed"]),
                        reclaimed=len(out["reclaimed"]),
                        gate=out["gate_reasons"],
                    )
            except Exception:
                log.exception("tick_failed")
            # poll for work more often than the planning tick so queued/manual runs start promptly
            while not self._stop.is_set() and time.monotonic() - started < s.tick_seconds:
                try:
                    self.pool.poll()
                except Exception:
                    log.exception("poll_failed")
                self._stop.wait(2.0)
        log.info("supervisor_stop")
        self.pool.shutdown()
