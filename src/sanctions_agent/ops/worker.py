"""Worker pool: claims queued runs and executes them with a heartbeat that extends the lease and
propagates cancellation. If the process dies, the lease expires and the reclaimer resumes the run."""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from sanctions_agent.db.engine import tx
from sanctions_agent.logs import get_logger
from sanctions_agent.pipeline import runs
from sanctions_agent.pipeline.runner import PipelineRunner
from sanctions_agent.settings import get_settings

log = get_logger(__name__)


class WorkerPool:
    def __init__(self, runner: PipelineRunner | None = None, concurrency: int | None = None) -> None:
        s = get_settings()
        self.runner = runner or PipelineRunner()
        self.concurrency = concurrency or s.worker_concurrency
        self.lease = s.lease_seconds
        self.hb_every = s.heartbeat_seconds
        self.worker_id = runs.new_worker_id()
        self._pool = ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="run")
        self._inflight: dict[str, Future[str]] = {}
        self._lock = threading.Lock()

    def free_slots(self) -> int:
        with self._lock:
            self._inflight = {k: f for k, f in self._inflight.items() if not f.done()}
            return self.concurrency - len(self._inflight)

    def poll(self) -> list[str]:
        """Claim as many queued runs as there are free slots. Returns claimed run ids."""
        claimed = []
        for _ in range(self.free_slots()):
            with tx() as conn:
                row = runs.claim_next(conn, self.worker_id, self.lease)
            if row is None:
                break
            rid = str(row["run_id"])
            with self._lock:
                self._inflight[rid] = self._pool.submit(self._execute, row)
            claimed.append(rid)
        return claimed

    def _execute(self, row: dict[str, Any]) -> str:
        rid = str(row["run_id"])
        cancel = threading.Event()
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(self.hb_every):
                try:
                    if runs.heartbeat(rid, self.worker_id, self.lease):
                        cancel.set()
                except Exception as e:  # heartbeat failures must not kill the run; lease may expire
                    log.warning("heartbeat_failed", run_id=rid, error=str(e))

        hb = threading.Thread(target=beat, name=f"hb-{rid[:8]}", daemon=True)
        hb.start()
        try:
            return self.runner.execute(row, cancel)
        finally:
            stop.set()

    def wait_idle(self, timeout: float | None = None) -> None:
        with self._lock:
            futures = list(self._inflight.values())
        for f in futures:
            f.result(timeout=timeout)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)
