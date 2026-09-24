"""LLM extraction from unstructured official text, with deterministic verification.

BRD section 18: some LLMs invented identifier values when extracting from list text - an invented
passport number creates false positives, a dropped vessel creates a violation. So:

* the model must return a verbatim quote for every entry
* ``verify`` checks every quote appears in the source text, the name appears in the quote, every
  identifier value appears in the text, IMO numbers pass their checksum, and (for vessel annexes) the
  number of entries matches a deterministic count of IMO numbers in the text
* results only ever become *proposals* for a human - never data
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from agents import Agent, ModelSettings, RunConfig, Runner, gen_trace_id
from agents.models.interface import Model
from openai.types.shared import Reasoning
from pydantic import BaseModel, Field

from sanctions_agent.agent.context import AgentContext, BudgetExceeded, BudgetHooks, spent_today
from sanctions_agent.agent.tracing import install_tracing
from sanctions_agent.canonical.normalize.identifiers import extract_imo, imo_checksum_ok
from sanctions_agent.db.engine import jsonb, tx
from sanctions_agent.settings import get_settings

MAX_CHARS = 48_000
# Test/integration hook: a Model used when Extractor() is created without one (e.g. from agent tools).
MODEL_OVERRIDE: Model | None = None
IdKind = Literal["IMO", "PASSPORT", "NATIONAL_ID", "REGISTRATION", "TAX", "UN_REF", "OFAC_UID", "OTHER"]


class ExtractedIdentifier(BaseModel):
    id_type: IdKind
    value: str = Field(description="Exactly as written in the text")


class ExtractedEntry(BaseModel):
    name: str = Field(description="Primary name exactly as written")
    entity_type: Literal["PERSON", "ORGANIZATION", "VESSEL", "AIRCRAFT", "UNKNOWN"]
    action: Literal["LISTING", "DELISTING", "AMENDMENT", "UNCLEAR"]
    identifiers: list[ExtractedIdentifier]
    program: str | None = Field(description="Programme / regime / annex if stated, else null")
    reason: str | None = Field(
        description="Stated reason for the action if present, else null (verbatim or close)"
    )
    verbatim_quote: str = Field(description="An exact, contiguous span of the text containing the name")


class Extraction(BaseModel):
    entries: list[ExtractedEntry]
    stated_total: int | None = Field(
        description="If the text states how many entries it adds/removes, that number"
    )
    notes: str = Field(description="Anything ambiguous; empty if none")


@dataclass
class VerifiedEntry:
    entry: ExtractedEntry
    ok: bool
    problems: list[str] = field(default_factory=list)


@dataclass
class VerificationResult:
    entries: list[VerifiedEntry]
    count_check: dict[str, Any]

    @property
    def all_ok(self) -> bool:
        return all(e.ok for e in self.entries) and self.count_check.get("ok", True)


_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", s.replace(" ", " ")).strip().casefold()


def _compact(s: str) -> str:
    return re.sub(r"[\s\-./]", "", s).casefold()


def verify(text: str, extraction: Extraction, *, expect_imo_rows: bool = False) -> VerificationResult:
    nt, ct = _norm(text), _compact(text)
    out: list[VerifiedEntry] = []
    for e in extraction.entries:
        problems: list[str] = []
        q = _norm(e.verbatim_quote)
        if not q or q not in nt:
            problems.append("verbatim quote not found in source text")
        if _norm(e.name) not in q and _norm(e.name) not in nt:
            problems.append("name not found in source text")
        for ident in e.identifiers:
            if _compact(ident.value) not in ct:
                problems.append(
                    f"{ident.id_type} {ident.value!r} not found in source text (possible hallucination)"
                )
            if ident.id_type == "IMO":
                imo = extract_imo(ident.value)
                if not imo or not imo_checksum_ok(imo):
                    problems.append(f"IMO {ident.value!r} fails the checksum")
        if e.action == "UNCLEAR":
            problems.append("model could not determine the action")
        out.append(VerifiedEntry(entry=e, ok=not problems, problems=problems))
    count: dict[str, Any] = {"ok": True, "extracted": len(extraction.entries)}
    if extraction.stated_total is not None and extraction.stated_total != len(extraction.entries):
        count.update(
            ok=False,
            stated_total=extraction.stated_total,
            problem="entry count differs from the total stated in the text",
        )
    if expect_imo_rows:
        imos = set(re.findall(r"IMO[\s:No.]*?(\d{7})", text, flags=re.I))
        extracted = {
            extract_imo(i.value) for e in extraction.entries for i in e.identifiers if i.id_type == "IMO"
        }
        missing = sorted(imos - extracted)
        count.update(imo_in_text=len(imos), imo_extracted=len(extracted - {None}))
        if missing:
            count.update(
                ok=False,
                problem="IMO numbers in the text were not extracted (possible dropped vessels)",
                missing_imos=missing[:50],
            )
    return VerificationResult(entries=out, count_check=count)


INSTRUCTIONS = """You extract sanctions actions from official government text (Federal Register notices,
OFAC Recent Actions, UN press releases, UK notices, EU Official Journal annexes).

Rules:
- Copy names and identifiers EXACTLY as written. Never infer, complete, translate or normalise them.
- Only include identifiers that literally appear next to the entry in the text. If none, return [].
- For every entry give a verbatim_quote: an exact contiguous span of the text that contains the name.
- action: LISTING (added/designated), DELISTING (removed/deleted/revoked), AMENDMENT (entry changed),
  UNCLEAR if the text does not say.
- If the text states how many entries are added or removed, put that number in stated_total.
- If the text contains no sanctions entries, return an empty list.
"""


def run_blocking(coro: Any) -> Any:
    """Run a coroutine to completion from sync code, even if called inside a running event loop
    (e.g. from an agent tool), by using a short-lived thread with its own loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class Extractor:
    def __init__(self, model: Model | None = None) -> None:
        self._model = model
        install_tracing()

    def extract(
        self,
        text: str,
        *,
        purpose: str,
        subject_ref: str,
        expect_imo_rows: bool = False,
        hint: str | None = None,
    ) -> tuple[Extraction, VerificationResult, str]:
        """Run extraction over (chunked) text. Returns (merged extraction, verification, cycle_id)."""
        s = get_settings()
        from sanctions_agent.agent.supervisor import build_model

        model = self._model or MODEL_OVERRIDE or build_model(s.agent_model)
        cycle_id = str(uuid.uuid4())
        trace_id = gen_trace_id()
        spent = spent_today()
        with tx() as conn:
            conn.execute(
                "INSERT INTO agent_cycle (cycle_id, agent_name, trigger, gate_reasons, model, trace_id)"
                " VALUES (%s, 'extractor', %s, %s, %s, %s)",
                (cycle_id, purpose, [subject_ref], s.agent_model, trace_id),
            )
        ctx = AgentContext(
            cycle_id=cycle_id,
            actor="agent:extractor",
            daily_budget_usd=s.agent_daily_budget_usd,
            spent_before_usd=spent,
            read_only=True,
        )
        status, error = "SUCCEEDED", None
        merged = Extraction(entries=[], stated_total=None, notes="")
        try:
            if spent >= s.agent_daily_budget_usd:
                raise BudgetExceeded("daily LLM budget spent")
            chunks = [text[i : i + MAX_CHARS] for i in range(0, max(1, len(text)), MAX_CHARS - 2000)] or [""]
            for i, chunk in enumerate(chunks):
                part = run_blocking(self._run(model, ctx, chunk, hint, trace_id, i))
                merged.entries.extend(part.entries)
                if part.stated_total is not None:
                    merged.stated_total = (merged.stated_total or 0) + part.stated_total
                if part.notes:
                    merged.notes = (merged.notes + " " + part.notes).strip()
        except BudgetExceeded as e:
            status, error = "BUDGET_EXCEEDED", str(e)
        except Exception as e:
            status, error = "FAILED", f"{type(e).__name__}: {e}"
        verification = verify(text, merged, expect_imo_rows=expect_imo_rows)
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
                            "entries": len(merged.entries),
                            "verified_ok": sum(1 for e in verification.entries if e.ok),
                            "count_check": verification.count_check,
                        }
                    ),
                    error,
                    cycle_id,
                ),
            )
        if status != "SUCCEEDED":
            raise RuntimeError(f"extraction {status}: {error}")
        return merged, verification, cycle_id

    async def _run(
        self, model: Model | str, ctx: AgentContext, chunk: str, hint: str | None, trace_id: str, i: int
    ) -> Extraction:
        s = get_settings()
        agent = Agent[AgentContext](
            name="Sanctions notice extractor",
            instructions=INSTRUCTIONS,
            model=model,
            output_type=Extraction,
            model_settings=ModelSettings(reasoning=Reasoning(effort="medium"), verbosity="low"),
        )
        prompt = (f"{hint}\n\n" if hint else "") + "TEXT:\n<<<\n" + chunk + "\n>>>"
        result = await asyncio.wait_for(
            Runner.run(
                agent,
                prompt,
                context=ctx,
                max_turns=2,
                hooks=BudgetHooks(),
                run_config=RunConfig(
                    workflow_name="sanctions-extractor",
                    trace_id=trace_id,
                    group_id=ctx.cycle_id,
                    trace_metadata={"chunk": str(i)},
                ),
            ),
            timeout=s.agent_cycle_timeout_seconds,
        )
        out = result.final_output
        if not isinstance(out, Extraction):
            raise ValueError("extractor returned no structured output")
        return out
