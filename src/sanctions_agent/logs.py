"""Structured JSON logging with correlation ids (run_id, source_id, cycle_id, trace_id)."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

_configured = False


def configure_logging(level: str = "INFO", json: bool = True) -> None:
    global _configured
    if _configured:
        return
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=getattr(logging, level.upper()))
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer())
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        cache_logger_on_first_use=True,
    )
    # Quieten chatty libraries.
    for name in ("httpx", "httpcore", "openai", "agents"):
        logging.getLogger(name).setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return,unused-ignore]


@contextmanager
def log_context(**values: Any) -> Iterator[None]:
    """Bind correlation ids for the duration of a block (async- and thread-safe via contextvars)."""
    tokens = structlog.contextvars.bind_contextvars(**{k: v for k, v in values.items() if v is not None})
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
