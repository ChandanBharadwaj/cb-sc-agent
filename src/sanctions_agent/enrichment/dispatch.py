"""Dispatch Level-2 runs (notice sync, enrichment batches) to their adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sanctions_agent.http.errors import ErrorClass, PipelineError

if TYPE_CHECKING:
    from sanctions_agent.pipeline.runner import RunContext


def run_level2(ctx: RunContext) -> str:
    raise PipelineError(ErrorClass.CONFIG_ERROR, f"no Level-2 handler for {ctx.source.adapter_type.type_id}")
