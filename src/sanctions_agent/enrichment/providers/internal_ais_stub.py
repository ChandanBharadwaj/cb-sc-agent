"""Interface stub for internal AIS / vessel-master data (port calls, positions, our own vessel records).

Connect this to the internal service that owns AIS data: implement ``enrich`` to look up by IMO / MMSI
and return owner, manager, flag history and recent port calls as enrichment data."""

from __future__ import annotations

from sanctions_agent.enrichment.base import Enricher, EnrichResult, Subject
from sanctions_agent.http.errors import ErrorClass, PipelineError


class InternalAisEnricher(Enricher):
    provider = "internal_ais"
    licence = "Internal"

    def enrich(self, subject: Subject) -> EnrichResult:
        raise PipelineError(
            ErrorClass.CONFIG_ERROR, "internal AIS enrichment is a stub: connect the internal service"
        )
