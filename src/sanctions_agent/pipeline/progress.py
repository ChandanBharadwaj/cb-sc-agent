"""Throttled live progress for a run: written to ``ingestion_run.progress`` and broadcast via NOTIFY."""

from __future__ import annotations

import json
import time
from typing import Any

from sanctions_agent.db.engine import jsonb, notify, tx
from sanctions_agent.settings import get_settings

STEP_WEIGHTS = {
    "FETCH": 35,
    "ARCHIVE": 5,
    "VALIDATE_FILE": 10,
    "PARSE": 30,
    "VALIDATE_DATA": 10,
    "DIFF_PUBLISH": 10,
    "SYNC": 80,
    "ENRICH": 90,
    "BUILD": 10,
}
LIST_STEPS = ["FETCH", "ARCHIVE", "VALIDATE_FILE", "PARSE", "VALIDATE_DATA", "DIFF_PUBLISH"]


class ProgressReporter:
    def __init__(self, run_id: str, source_id: str, steps: list[str] | None = None) -> None:
        self.run_id = run_id
        self.source_id = source_id
        self.steps = steps or LIST_STEPS
        self.state: dict[str, Any] = {"step": None, "retries": 0}
        self._last_write = 0.0
        self._started = time.monotonic()
        self._min_interval = get_settings().progress_min_interval_seconds

    def _pct(self) -> float:
        step = self.state.get("step")
        if step not in self.steps:
            return float(self.state.get("pct", 0))
        idx = self.steps.index(step)
        total = sum(STEP_WEIGHTS.get(s, 10) for s in self.steps)
        done = sum(STEP_WEIGHTS.get(s, 10) for s in self.steps[:idx])
        frac = 0.0
        if step == "FETCH" and self.state.get("bytes_total"):
            frac = min(1.0, self.state.get("bytes_done", 0) / max(1, self.state["bytes_total"]))
        elif step == "PARSE" and self.state.get("records_expected"):
            frac = min(1.0, self.state.get("records_done", 0) / max(1, self.state["records_expected"]))
        elif step in ("SYNC", "ENRICH") and self.state.get("items_total"):
            frac = min(1.0, self.state.get("items_done", 0) / max(1, self.state["items_total"]))
        return round(100.0 * (done + frac * STEP_WEIGHTS.get(step, 10)) / total, 1)

    def update(self, force: bool = False, **fields: Any) -> None:
        self.state.update(fields)
        now = time.monotonic()
        if not force and now - self._last_write < self._min_interval:
            return
        self._last_write = now
        pct = self._pct()
        elapsed = now - self._started
        self.state["pct"] = pct
        self.state["elapsed_s"] = round(elapsed, 1)
        self.state["eta_s"] = round(elapsed * (100 - pct) / pct, 0) if pct >= 3 else None
        payload = dict(self.state)
        with tx() as conn:
            conn.execute(
                "UPDATE ingestion_run SET progress = %s WHERE run_id = %s", (jsonb(payload), self.run_id)
            )
            notify(
                conn,
                "run_progress",
                json.dumps(
                    {"run_id": self.run_id, "source_id": self.source_id, "status": "RUNNING", **payload},
                    default=str,
                ),
            )

    def step(self, name: str) -> None:
        self.update(force=True, step=name)
