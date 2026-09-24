"""LLM supervisor (gpt-5-mini via the OpenAI Agents SDK).

The agent is invoked by the scheduler only when the gate says something needs judgement. It reads the
compact state, diagnoses, acts through guarded tools and returns a structured report. Every cycle is
recorded in ``agent_cycle`` (tokens, cost, report, trace id); every tool call in ``agent_action``;
every span in ``agent_span``. Failures never stop ingestion - the scheduler falls back to autopilot.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Literal

from agents import Agent, ModelSettings, RunConfig, Runner, gen_trace_id
from agents.models.interface import Model
from openai.types.shared import Reasoning
from pydantic import BaseModel, Field

from sanctions_agent.agent.context import (
    ActionLimits,
    AgentContext,
    AgentUnavailable,
    BudgetExceeded,
    BudgetHooks,
    spent_today,
)
from sanctions_agent.agent.state_snapshot import snapshot
from sanctions_agent.agent.tools import supervisor_tools
from sanctions_agent.agent.tracing import install_tracing
from sanctions_agent.db.engine import dumps, jsonb, tx
from sanctions_agent.logs import get_logger, log_context
from sanctions_agent.ops import system_settings
from sanctions_agent.settings import get_settings

log = get_logger(__name__)
PROMPTS = Path(__file__).parent / "prompts"


class CycleReport(BaseModel):
    summary: str = Field(description="2-4 sentences: overall state and what was done")
    health: Literal["healthy", "degraded", "critical"]
    actions: list[str] = Field(description="Actions taken this cycle, one line each")
    escalations: list[str] = Field(description="Things a human must do, one line each")
    followups: list[str] = Field(description="What to check next cycle")


def build_model(model_name: str) -> Model | str:
    s = get_settings()
    if s.openai_api_key is None:
        raise AgentUnavailable("OPENAI_API_KEY is not configured")
    from agents import OpenAIResponsesModel
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=s.openai_api_key.get_secret_value(), max_retries=2, timeout=90)
    return OpenAIResponsesModel(model=model_name, openai_client=client)


class SupervisorAgent:
    """Implements the scheduler's AgentCycleRunner protocol."""

    agent_name = "supervisor"

    def __init__(self, model: Model | None = None, limits: ActionLimits | None = None) -> None:
        self._model = model
        self._limits = limits or ActionLimits()
        install_tracing()

    def _model_for(self, name: str) -> Model | str:
        return self._model if self._model is not None else build_model(name)

    def run_cycle(self, trigger: str, gate_reasons: list[str]) -> dict[str, Any]:
        s = get_settings()
        with tx() as conn:
            cfg = system_settings.get_all(conn)
        model_name = str(cfg.get("agent_model") or s.agent_model)
        raw_budget = cfg.get("agent_daily_budget_usd")
        budget = float(raw_budget if raw_budget is not None else s.agent_daily_budget_usd)
        cycle_id = str(uuid.uuid4())
        trace_id = gen_trace_id()
        spent = spent_today()
        with tx() as conn:
            conn.execute(
                "INSERT INTO agent_cycle (cycle_id, agent_name, trigger, gate_reasons, model, trace_id)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (cycle_id, self.agent_name, trigger, gate_reasons, model_name, trace_id),
            )
        ctx = AgentContext(
            cycle_id=cycle_id, daily_budget_usd=budget, spent_before_usd=spent, limits=self._limits
        )
        status, report, error = "SUCCEEDED", None, None
        with log_context(cycle_id=cycle_id, trace_id=trace_id):
            try:
                if spent >= budget:
                    raise BudgetExceeded(f"daily LLM budget ${budget:.2f} already spent (${spent:.2f})")
                model = self._model_for(model_name)
                report = asyncio.run(self._run(ctx, model, cfg, trigger, gate_reasons, trace_id))
            except BudgetExceeded as e:
                status, error = "BUDGET_EXCEEDED", str(e)
            except AgentUnavailable as e:
                status, error = "SKIPPED", str(e)
            except TimeoutError:
                status, error = "TIMEOUT", f"cycle exceeded {s.agent_cycle_timeout_seconds}s"
            except Exception as e:
                status, error = "FAILED", f"{type(e).__name__}: {e}"
                log.exception("agent_cycle_error")
        with tx() as conn:
            conn.execute(
                """UPDATE agent_cycle SET finished_at = now(), status = %s, input_tokens = %s, cached_tokens = %s,
                       output_tokens = %s, cost_usd = %s, report = %s, error = %s, fallback_used = %s
                   WHERE cycle_id = %s""",
                (
                    status,
                    ctx.input_tokens,
                    ctx.cached_tokens,
                    ctx.output_tokens,
                    ctx.cost_usd,
                    jsonb(report) if report else None,
                    error,
                    status != "SUCCEEDED",
                    cycle_id,
                ),
            )
        log.info(
            "agent_cycle",
            status=status,
            llm_calls=ctx.llm_calls,
            cost_usd=ctx.cost_usd,
            actions=ctx.seq,
            error=error,
        )
        return {
            "cycle_id": cycle_id,
            "status": status,
            "report": report,
            "error": error,
            "cost_usd": ctx.cost_usd,
            "tool_calls": ctx.seq,
        }

    async def _run(
        self,
        ctx: AgentContext,
        model: Model | str,
        cfg: dict[str, Any],
        trigger: str,
        gate_reasons: list[str],
        trace_id: str,
    ) -> dict[str, Any]:
        s = get_settings()
        agent = Agent[AgentContext](
            name="Sanctions ingestion supervisor",
            instructions=(PROMPTS / "supervisor.md").read_text(encoding="utf-8"),
            tools=supervisor_tools(),
            model=model,
            model_settings=ModelSettings(
                reasoning=Reasoning(effort=cfg.get("agent_reasoning_effort") or s.agent_reasoning_effort),
                parallel_tool_calls=False,
                verbosity="low",
            ),
            output_type=CycleReport,
        )
        with tx() as conn:
            state = snapshot(conn)
        user = (
            f"Cycle trigger: {trigger}. Why you were woken: {', '.join(gate_reasons) or 'none'}.\n"
            "Current system state (JSON, refreshed just now - call get_system_status only after you act):\n"
            f"{dumps(state)}"
        )
        result = await asyncio.wait_for(
            Runner.run(
                agent,
                user,
                context=ctx,
                max_turns=s.agent_max_turns,
                hooks=BudgetHooks(),
                run_config=RunConfig(
                    workflow_name="sanctions-supervisor",
                    trace_id=trace_id,
                    group_id=ctx.cycle_id,
                    trace_include_sensitive_data=True,
                    trace_metadata={"cycle_id": ctx.cycle_id, "trigger": trigger},
                ),
            ),
            timeout=s.agent_cycle_timeout_seconds,
        )
        out = result.final_output
        return out.model_dump() if isinstance(out, BaseModel) else {"summary": str(out)}


def cycle_report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2)
