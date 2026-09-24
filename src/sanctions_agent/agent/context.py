"""Per-cycle agent context: identity, action limits, token/cost accounting and the daily budget."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agents import Agent, RunContextWrapper, RunHooks
from agents.items import ModelResponse

from sanctions_agent.db.engine import fetch_val, tx
from sanctions_agent.settings import get_settings


class BudgetExceeded(Exception):
    pass


class AgentUnavailable(Exception):
    pass


def cost_usd(input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    s = get_settings()
    uncached = max(0, input_tokens - cached_tokens)
    return round(
        (
            uncached * s.price_input_per_mtok
            + cached_tokens * s.price_cached_input_per_mtok
            + output_tokens * s.price_output_per_mtok
        )
        / 1_000_000,
        6,
    )


def spent_today() -> float:
    with tx() as conn:
        return float(
            fetch_val(
                conn,
                "SELECT coalesce(sum(cost_usd), 0) FROM agent_cycle"
                " WHERE started_at >= date_trunc('day', now())",
            )
            or 0
        )


@dataclass
class ActionLimits:
    max_enqueues: int = 12
    max_incident_writes: int = 20
    max_proposals: int = 10
    max_extractions: int = 5


@dataclass
class AgentContext:
    cycle_id: str
    actor: str = "agent:supervisor"
    daily_budget_usd: float = 2.0
    spent_before_usd: float = 0.0
    limits: ActionLimits = field(default_factory=ActionLimits)
    seq: int = 0
    counters: dict[str, int] = field(default_factory=dict)
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0
    read_only: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return cost_usd(self.input_tokens, self.cached_tokens, self.output_tokens)

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def take(self, counter: str, limit: int) -> bool:
        n = self.counters.get(counter, 0)
        if n >= limit:
            return False
        self.counters[counter] = n + 1
        return True


class BudgetHooks(RunHooks[AgentContext]):
    """Accumulates usage after every LLM call and stops the run once the daily budget is spent."""

    async def on_llm_start(
        self,
        context: RunContextWrapper[AgentContext],
        agent: Agent[AgentContext],
        system_prompt: str | None,
        input_items: list[Any],
    ) -> None:
        ctx = context.context
        if ctx.spent_before_usd + ctx.cost_usd >= ctx.daily_budget_usd:
            raise BudgetExceeded(f"daily LLM budget ${ctx.daily_budget_usd:.2f} reached")

    async def on_llm_end(
        self, context: RunContextWrapper[AgentContext], agent: Agent[AgentContext], response: ModelResponse
    ) -> None:
        ctx = context.context
        u = response.usage
        ctx.llm_calls += 1
        ctx.input_tokens += int(u.input_tokens or 0)
        ctx.output_tokens += int(u.output_tokens or 0)
        details = getattr(u, "input_tokens_details", None)
        ctx.cached_tokens += int(getattr(details, "cached_tokens", 0) or 0)
