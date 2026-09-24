"""Agents SDK tracing -> Postgres (``agent_span``), so every LLM call and tool call is traceable
alongside the runs it caused. Export to the OpenAI dashboard is off unless explicitly enabled."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

from agents import TracingProcessor, set_trace_processors
from agents.tracing import Span, Trace

from sanctions_agent.db.engine import jsonb, tx
from sanctions_agent.logs import get_logger
from sanctions_agent.settings import get_settings

log = get_logger(__name__)
_installed = False
_lock = threading.Lock()
_MAX_DATA = 20_000


def _ts(v: str | None) -> datetime:
    if not v:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return datetime.now(UTC)


def _trim(data: Any) -> Any:
    s = str(data)
    if len(s) <= _MAX_DATA:
        return data
    return {"truncated": True, "preview": s[:_MAX_DATA]}


class PostgresTraceProcessor(TracingProcessor):
    def on_trace_start(self, trace: Trace) -> None:
        pass

    def on_trace_end(self, trace: Trace) -> None:
        pass

    def on_span_start(self, span: Span[Any]) -> None:
        pass

    def on_span_end(self, span: Span[Any]) -> None:
        try:
            exp = span.export() or {}
            data = exp.get("span_data") or {}
            with tx() as conn:
                conn.execute(
                    """INSERT INTO agent_span (span_id, trace_id, parent_span_id, kind, name, started_at, ended_at, data,
                           error) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (
                        exp.get("id") or span.span_id,
                        exp.get("trace_id") or span.trace_id,
                        exp.get("parent_id"),
                        data.get("type", "span"),
                        data.get("name"),
                        _ts(exp.get("started_at")),
                        _ts(exp.get("ended_at")),
                        jsonb(_trim(data)),
                        jsonb(exp.get("error")) if exp.get("error") else None,
                    ),
                )
        except Exception as e:  # tracing must never break the agent
            log.warning("trace_write_failed", error=str(e))

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass


def install_tracing() -> None:
    global _installed
    with _lock:
        if _installed:
            return
        processors: list[TracingProcessor] = [PostgresTraceProcessor()]
        if get_settings().agent_export_traces_to_openai:
            from agents.tracing.processors import default_processor

            processors.append(default_processor())
        set_trace_processors(processors)
        _installed = True
