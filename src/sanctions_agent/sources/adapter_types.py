"""Registry of adapter *types* (code). A source (DB row, UI-managed) is an instance of a type.

New file formats need a parser, i.e. code; new sources of an existing format can be added from the UI.
Each type declares its config model (-> UI form), a hard politeness floor for scheduling (the UI cannot
schedule more often than this) and its implementation class.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pydantic import BaseModel

from sanctions_agent.sources.config_models import (
    CslSourceConfig,
    CuratedListConfig,
    EnrichmentConfig,
    ListSourceConfig,
    NoticeFeedConfig,
)


@dataclass(frozen=True)
class AdapterType:
    type_id: str
    kind: str  # STRUCTURED_LIST | CURATED_LIST | NOTICE_FEED | ENRICHMENT
    level: int
    description: str
    config_model: type[BaseModel]
    hard_min_interval: timedelta
    impl: str  # "module:Class"

    def load(self) -> type[Any]:
        module, _, cls = self.impl.partition(":")
        return getattr(importlib.import_module(module), cls)  # type: ignore[no-any-return,unused-ignore]

    def validate_config(self, cfg: dict[str, Any]) -> BaseModel:
        return self.config_model.model_validate(cfg)

    def json_schema(self) -> dict[str, Any]:
        return self.config_model.model_json_schema()


_M = "sanctions_agent.sources"
_E = "sanctions_agent.enrichment"

ADAPTER_TYPES: dict[str, AdapterType] = {
    t.type_id: t
    for t in [
        # ---- Level 1: official lists (deterministic parsers) ----------------------------------
        AdapterType(
            "ofac_advanced_xml",
            "STRUCTURED_LIST",
            1,
            "OFAC Sanctions List Service Advanced XML (SDN_ADVANCED / CONS_ADVANCED)",
            ListSourceConfig,
            timedelta(minutes=30),
            f"{_M}.l1.ofac_advanced:OfacAdvancedAdapter",
        ),
        AdapterType(
            "un_consolidated_xml",
            "STRUCTURED_LIST",
            1,
            "UN Security Council Consolidated List XML",
            ListSourceConfig,
            timedelta(minutes=60),
            f"{_M}.l1.un_consolidated:UnConsolidatedAdapter",
        ),
        AdapterType(
            "uk_fcdo_xml",
            "STRUCTURED_LIST",
            1,
            "UK Sanctions List (FCDO) XML",
            ListSourceConfig,
            timedelta(minutes=30),
            f"{_M}.l1.uk_fcdo:UkFcdoAdapter",
        ),
        AdapterType(
            "eu_fsf_xml",
            "STRUCTURED_LIST",
            1,
            "EU Financial Sanctions File XML 1.1",
            ListSourceConfig,
            timedelta(minutes=30),
            f"{_M}.l1.eu_fsf:EuFsfAdapter",
        ),
        AdapterType(
            "us_csl_json",
            "STRUCTURED_LIST",
            1,
            "US Consolidated Screening List JSON (BIS / State lists)",
            CslSourceConfig,
            timedelta(minutes=60),
            f"{_M}.l1.us_csl:UsCslAdapter",
        ),
        AdapterType(
            "eu_annex_curated",
            "CURATED_LIST",
            1,
            "EU Reg. 833/2014 annex (XLII vessels / IV entities): agent-extracted, human-approved entries",
            CuratedListConfig,
            timedelta(minutes=60),
            f"{_M}.l1.eu_annex:EuAnnexAdapter",
        ),
        # ---- Level 2: official notices -------------------------------------------------------
        AdapterType(
            "federal_register",
            "NOTICE_FEED",
            2,
            "Federal Register API (OFAC, BIS documents)",
            NoticeFeedConfig,
            timedelta(minutes=30),
            f"{_E}.notices.federal_register:FederalRegisterFeed",
        ),
        AdapterType(
            "ofac_recent_actions",
            "NOTICE_FEED",
            2,
            "OFAC Recent Actions pages",
            NoticeFeedConfig,
            timedelta(minutes=30),
            f"{_E}.notices.ofac_recent_actions:OfacRecentActionsFeed",
        ),
        AdapterType(
            "un_list_updates",
            "NOTICE_FEED",
            2,
            "UN SC Consolidated List updates log",
            NoticeFeedConfig,
            timedelta(minutes=60),
            f"{_E}.notices.un_updates:UnListUpdatesFeed",
        ),
        AdapterType(
            "uk_sanctions_notices",
            "NOTICE_FEED",
            2,
            "gov.uk financial sanctions notices (search API)",
            NoticeFeedConfig,
            timedelta(minutes=30),
            f"{_E}.notices.uk_notices:UkNoticesFeed",
        ),
        AdapterType(
            "eurlex_oj_rss",
            "NOTICE_FEED",
            2,
            "EUR-Lex Official Journal L series RSS",
            NoticeFeedConfig,
            timedelta(minutes=15),
            f"{_E}.notices.eurlex:EurLexOjFeed",
        ),
        AdapterType(
            "rss_signal",
            "NOTICE_FEED",
            2,
            "Generic RSS feed used as an early-pull signal (e.g. EU FSF RSS)",
            NoticeFeedConfig,
            timedelta(minutes=15),
            f"{_E}.notices.rss_signal:RssSignalFeed",
        ),
        # ---- Level 2: free enrichment --------------------------------------------------------
        AdapterType(
            "gleif",
            "ENRICHMENT",
            2,
            "GLEIF LEI records and parents (CC0)",
            EnrichmentConfig,
            timedelta(hours=1),
            f"{_E}.providers.gleif:GleifEnricher",
        ),
        AdapterType(
            "companies_house",
            "ENRICHMENT",
            2,
            "UK Companies House company profiles (OGL)",
            EnrichmentConfig,
            timedelta(hours=1),
            f"{_E}.providers.companies_house:CompaniesHouseEnricher",
        ),
        AdapterType(
            "faa_registry",
            "ENRICHMENT",
            2,
            "FAA releasable aircraft database (public domain)",
            EnrichmentConfig,
            timedelta(hours=6),
            f"{_E}.providers.faa:FaaRegistryEnricher",
        ),
        AdapterType(
            "wikidata",
            "ENRICHMENT",
            2,
            "Wikidata SPARQL: vessels by IMO, organisations (CC0)",
            EnrichmentConfig,
            timedelta(hours=6),
            f"{_E}.providers.wikidata:WikidataEnricher",
        ),
        AdapterType(
            "icij_offshore_leaks",
            "ENRICHMENT",
            2,
            "ICIJ Offshore Leaks bulk data (ODbL - leads only)",
            EnrichmentConfig,
            timedelta(hours=24),
            f"{_E}.providers.icij:IcijEnricher",
        ),
        AdapterType(
            "internal_ais",
            "ENRICHMENT",
            2,
            "Internal AIS / vessel master data (interface stub)",
            EnrichmentConfig,
            timedelta(hours=1),
            f"{_E}.providers.internal_ais_stub:InternalAisEnricher",
        ),
    ]
}


def get_adapter_type(type_id: str) -> AdapterType:
    try:
        return ADAPTER_TYPES[type_id]
    except KeyError as e:
        raise ValueError(f"unknown adapter type {type_id!r}") from e
