"""Guard wrapper for agent tools.

Every tool call is written to ``agent_action`` (append-only) with its arguments, the guard verdict,
a result summary and duration. Mutating tools are rate-limited per cycle and refused for read-only
agents. Denials and errors are returned to the model as data (never raised) so it can adapt.
"""

from __future__ import annotations

import functools
import inspect
import json
import time
from collections.abc import Callable
from typing import Any, TypeVar

from agents import RunContextWrapper

from sanctions_agent.agent.context import AgentContext
from sanctions_agent.db.engine import dumps, jsonb, tx
from sanctions_agent.logs import get_logger

log = get_logger(__name__)
F = TypeVar("F", bound=Callable[..., Any])
MAX_RESULT_CHARS = 12_000


class ToolDenied(Exception):
    pass


def _summary(result: Any) -> Any:
    s = dumps(result)
    if len(s) <= 2000:
        return json.loads(s)
    return {"truncated": True, "preview": s[:2000]}


def guarded(kind: str = "read", limit: tuple[str, str] | None = None) -> Callable[[F], F]:
    """``kind``: read | act. ``limit``: (counter name, ActionLimits attribute) for per-cycle caps."""

    def deco(fn: F) -> F:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(rc: RunContextWrapper[AgentContext], *args: Any, **kwargs: Any) -> str:
            ctx = rc.context
            seq = ctx.next_seq()
            bound = sig.bind_partial(rc, *args, **kwargs)
            call_args = {k: v for k, v in bound.arguments.items() if k != next(iter(sig.parameters))}
            started = time.monotonic()
            verdict, error = "ALLOWED", None
            try:
                if kind == "act" and ctx.read_only:
                    raise ToolDenied("this agent is read-only")
                if limit is not None and not ctx.take(limit[0], getattr(ctx.limits, limit[1])):
                    raise ToolDenied(f"per-cycle limit reached for {limit[0]}")
                result = fn(rc, *args, **kwargs)
            except ToolDenied as e:
                verdict, result = "DENIED", {"denied": str(e)}
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                result = {"error": error}
                log.warning("agent_tool_error", tool=fn.__name__, error=error)
            duration = int((time.monotonic() - started) * 1000)
            try:
                with tx(actor=ctx.actor) as conn:
                    conn.execute(
                        """INSERT INTO agent_action (cycle_id, seq, tool, args, guard_verdict, result_summary, error,
                               duration_ms) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            ctx.cycle_id,
                            seq,
                            fn.__name__,
                            jsonb(call_args),
                            verdict,
                            jsonb(_summary(result)),
                            error,
                            duration,
                        ),
                    )
            except Exception as e:  # audit write failure must not hide the tool result from the model
                log.error("agent_action_write_failed", error=str(e))
            out = dumps(result)
            return out if len(out) <= MAX_RESULT_CHARS else out[:MAX_RESULT_CHARS] + '..."(truncated)"'

        return wrapper  # type: ignore[return-value]

    return deco
