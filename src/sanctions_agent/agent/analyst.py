"""Natural-language Q&A about ingestion status, progress, fields, counts and data quality.

Aggregate-only by construction (three layers):
1. input guardrail - deterministic, declines row-level requests ("is X sanctioned?", "list the names...")
2. tools          - only aggregate queries over ``analytics`` views
3. database       - the tools connect as ``sanctions_analyst``, which can read nothing else
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    ModelSettings,
    RunConfig,
    RunContextWrapper,
    Runner,
    gen_trace_id,
    input_guardrail,
)
from agents.models.interface import Model
from openai.types.shared import Reasoning
from pydantic import BaseModel, Field

from sanctions_agent.agent.analyst_tools import analyst_tools
from sanctions_agent.agent.chat_session import PostgresSession
from sanctions_agent.agent.context import (
    AgentContext,
    AgentUnavailable,
    BudgetExceeded,
    BudgetHooks,
    spent_today,
)
from sanctions_agent.agent.tracing import install_tracing
from sanctions_agent.db.engine import jsonb, tx
from sanctions_agent.settings import get_settings

REFUSAL = (
    "I can only answer questions about the ingestion itself - status, progress, runs, fields, counts, "
    "trends and data-quality issues - not about individual listed parties or records. For a specific "
    "name, vessel or company please use the screening system."
)

_ROW_LEVEL = [
    re.compile(
        r"\bis\s+[\w .,'&-]{2,60}\s+(sanctioned|listed|designated|on\s+(the\s+)?(sdn|list|uk|un|eu))\b", re.I
    ),
    re.compile(
        r"\b(list|show|give|print|export|dump|display)\s+(me\s+)?(all\s+|the\s+|every\s+)?"
        r"(names?|entities|individuals|people|persons|records|entries|parties|aliases|addresses|identifiers|ids|"
        r"(imo|mmsi|passport|call[- ]sign|registration|tail)\s+numbers?|imos|mmsis|leis?|lei\s+codes?)\b",
        re.I,
    ),
    re.compile(r"\bwho\s+(is|are|was|were)\s+(on|in|added|removed|listed|designated|delisted)\b", re.I),
    re.compile(
        r"\b(passport|date of birth|birth date|dob|home address|national id)\s+(number\s+)?(of|for)\s+(?!the\s+)",
        re.I,
    ),
    re.compile(
        r"\b(which|what)\s+(people|persons|individuals|companies|vessels|entities)\s+(were|are|got)\s+"
        r"(added|removed|listed|delisted|designated)\b",
        re.I,
    ),
]
_AGGREGATE_HINTS = re.compile(
    r"\b(how many|count|number of|rate|share|percent|%|trend|fill|coverage|average|total)\b", re.I
)


_STRONG = (0, 3)  # "is X sanctioned", "passport / DOB of X": identity questions
_METRIC_WORDS = re.compile(r"\b(fill|rate|coverage|how many|percent|share|%|average|trend)\b", re.I)


def is_row_level(question: str) -> bool:
    if any(_ROW_LEVEL[i].search(question) for i in _STRONG) and not _METRIC_WORDS.search(question):
        return True
    if _AGGREGATE_HINTS.search(question):
        return False
    return any(p.search(question) for p in _ROW_LEVEL)


class Citation(BaseModel):
    source: str = Field(description="Tool or analytics view the figure came from")
    as_of: str = Field(description="Timestamp of the data (UTC ISO)")


class ChartSeries(BaseModel):
    name: str
    values: list[float]


class ChartSpec(BaseModel):
    type: Literal["bar", "line"]
    title: str
    y_label: str
    labels: list[str] = Field(description="Category or time labels, at most 24")
    series: list[ChartSeries] = Field(description="1-3 series, each with one value per label")


class Answer(BaseModel):
    answer_markdown: str = Field(
        description="Concise answer in markdown; numbers must come from tool results"
    )
    citations: list[Citation]
    chart: ChartSpec | None = Field(description="Optional chart when a comparison or trend helps; else null")


INSTRUCTIONS = f"""You answer questions from compliance and operations staff about a sanctions-list ingestion
platform: source health and freshness, run progress and history, errors, record counts and trends, field
coverage (fill rates), data-quality issues, enrichment coverage, review queues and what the agents did.

Rules:
- Use the tools for every figure. Never estimate or invent numbers, dates or field meanings. If the data
  is not there, say so.
- Aggregate information only. If asked about specific listed people, companies, vessels or records,
  reply exactly: "{REFUSAL}"
- Be concise: lead with the answer, then 2-5 supporting bullets. Give units (%, hours, records).
- Freshness: compare hours since success with the warn/hard thresholds. Fill rates are shares 0-1; show
  them as percentages.
- Cite each figure's source (tool or view name) with the time you retrieved it: {{now}}.
- Add a chart only when comparing several sources/fields or showing a trend (<= 24 labels).
- Explain data-quality terms plainly (e.g. "weak alias", "schema drift", "fill-rate floor") when used.
"""


@input_guardrail
async def aggregate_only(
    ctx: RunContextWrapper[AgentContext], agent: Agent[AgentContext], inp: Any
) -> GuardrailFunctionOutput:
    text = (
        inp
        if isinstance(inp, str)
        else " ".join(str(i.get("content")) for i in inp if isinstance(i, dict) and i.get("role") == "user")[
            -2000:
        ]
    )
    return GuardrailFunctionOutput(
        output_info={"row_level": is_row_level(text)}, tripwire_triggered=is_row_level(text)
    )


class AnalystAgent:
    agent_name = "analyst"

    def __init__(self, model: Model | None = None) -> None:
        self._model = model
        install_tracing()

    def ask(self, question: str, *, user: str, session_id: str | None = None) -> dict[str, Any]:
        s = get_settings()
        session_id = session_id or f"chat-{uuid.uuid4().hex[:12]}"
        cycle_id = str(uuid.uuid4())
        trace_id = gen_trace_id()
        with tx() as conn:
            conn.execute(
                "INSERT INTO agent_cycle (cycle_id, agent_name, trigger, gate_reasons, model, trace_id)"
                " VALUES (%s, 'analyst', 'question', %s, %s, %s)",
                (cycle_id, [user], s.agent_model, trace_id),
            )
        ctx = AgentContext(
            cycle_id=cycle_id,
            actor=f"agent:analyst:{user}",
            daily_budget_usd=s.agent_daily_budget_usd,
            spent_before_usd=spent_today(),
            read_only=True,
        )
        status, error = "SUCCEEDED", None
        result: dict[str, Any]
        if is_row_level(question):
            status = "SUCCEEDED"
            result = {"answer_markdown": REFUSAL, "citations": [], "chart": None, "refused": True}
        else:
            try:
                if ctx.spent_before_usd >= ctx.daily_budget_usd:
                    raise BudgetExceeded("daily LLM budget spent - try again tomorrow or raise the budget")
                from sanctions_agent.agent.supervisor import build_model

                model = self._model or build_model(s.agent_model)
                out = asyncio.run(self._run(ctx, model, question, session_id, user, trace_id))
                result = {**out.model_dump(), "refused": False}
            except InputGuardrailTripwireTriggered:
                result = {"answer_markdown": REFUSAL, "citations": [], "chart": None, "refused": True}
            except (BudgetExceeded, AgentUnavailable) as e:
                status, error = ("BUDGET_EXCEEDED" if isinstance(e, BudgetExceeded) else "SKIPPED"), str(e)
                result = {
                    "answer_markdown": f"The assistant is unavailable: {e}",
                    "citations": [],
                    "chart": None,
                    "refused": False,
                }
            except Exception as e:
                status, error = "FAILED", f"{type(e).__name__}: {e}"
                result = {
                    "answer_markdown": "Sorry - I could not answer that. The error has been logged.",
                    "citations": [],
                    "chart": None,
                    "refused": False,
                }
        with tx() as conn:
            conn.execute(
                """UPDATE agent_cycle SET finished_at = now(), status = %s, input_tokens = %s, cached_tokens = %s,
                       output_tokens = %s, cost_usd = %s, report = %s, error = %s WHERE cycle_id = %s""",
                (
                    status,
                    ctx.input_tokens,
                    ctx.cached_tokens,
                    ctx.output_tokens,
                    ctx.cost_usd,
                    jsonb(
                        {
                            "question": question[:500],
                            "refused": result.get("refused"),
                            "summary": (result.get("answer_markdown") or "")[:300],
                        }
                    ),
                    error,
                    cycle_id,
                ),
            )
        return {**result, "session_id": session_id, "cycle_id": cycle_id, "status": status}

    async def _run(
        self, ctx: AgentContext, model: Model | str, question: str, session_id: str, user: str, trace_id: str
    ) -> Answer:
        s = get_settings()
        agent = Agent[AgentContext](
            name="Sanctions ingestion analyst",
            instructions=INSTRUCTIONS.replace("{now}", datetime.now(UTC).isoformat(timespec="seconds")),
            tools=analyst_tools(),
            model=model,
            output_type=Answer,
            input_guardrails=[aggregate_only],
            model_settings=ModelSettings(
                reasoning=Reasoning(effort="low"), verbosity="low", parallel_tool_calls=True
            ),
        )
        result = await asyncio.wait_for(
            Runner.run(
                agent,
                question,
                context=ctx,
                max_turns=12,
                hooks=BudgetHooks(),
                session=PostgresSession(session_id, user),
                run_config=RunConfig(
                    workflow_name="sanctions-analyst",
                    trace_id=trace_id,
                    group_id=session_id,
                    trace_metadata={"user": user},
                ),
            ),
            timeout=s.agent_cycle_timeout_seconds,
        )
        out = result.final_output
        if not isinstance(out, Answer):
            return Answer(answer_markdown=str(out), citations=[], chart=None)
        return out
