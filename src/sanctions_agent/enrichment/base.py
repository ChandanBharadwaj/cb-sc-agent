"""Level-2 enrichment framework.

Enrichment never changes list data: results are stored beside it (``enrichment_record``) with provider
key, match method, score, source URL, licence and the raw response hash. Only exact-identifier matches
or very high-confidence name matches are auto-accepted; the rest are proposed for human review.
Personal data (people's details, company officers / PSC) is fetched on demand only (NFR-07).
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import psycopg
from rapidfuzz import fuzz

from sanctions_agent.canonical.normalize.names import normalize_name
from sanctions_agent.db.engine import fetch_all
from sanctions_agent.http.client import HttpFetcher, RetryPolicy, retry_call
from sanctions_agent.http.ratelimit import TokenBucket
from sanctions_agent.sources.config_models import EnrichmentConfig
from sanctions_agent.storage.blobstore import get_blob_store

LEGAL_FORMS = {
    "llc",
    "ltd",
    "limited",
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "co",
    "company",
    "plc",
    "jsc",
    "pjsc",
    "ojsc",
    "cjsc",
    "ooo",
    "ao",
    "pao",
    "zao",
    "oao",
    "gmbh",
    "ag",
    "sa",
    "sas",
    "srl",
    "spa",
    "bv",
    "nv",
    "fze",
    "fzco",
    "fzc",
    "llp",
    "lp",
    "pte",
    "pty",
    "sdn",
    "bhd",
    "kg",
    "as",
    "ab",
    "oy",
    "sarl",
    "the",
    "group",
    "holding",
    "holdings",
}


def core_name(name: str) -> str:
    toks = [t for t in normalize_name(name).split() if t not in LEGAL_FORMS]
    return " ".join(toks)


def name_score(a: str, b: str) -> float:
    ca, cb = core_name(a), core_name(b)
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 1.0
    return round(fuzz.token_sort_ratio(ca, cb) / 100.0, 4)


@dataclass
class Subject:
    source_id: str
    source_key: str
    record_version_id: int
    entity_type: str
    name: str
    doc: dict[str, Any]

    def countries(self) -> set[str]:
        out = {c.get("country_iso2") for c in self.doc.get("countries", [])}
        out |= {a.get("country_iso2") for a in self.doc.get("addresses", [])}
        return {c for c in out if c}

    def ids(self, *types: str) -> list[dict[str, Any]]:
        return [i for i in self.doc.get("identifiers", []) if i.get("id_type") in types]

    def all_names(self) -> list[str]:
        return [n["full_name"] for n in self.doc.get("names", [])]


@dataclass
class EnrichResult:
    status: str  # AUTO_ACCEPTED | NEEDS_REVIEW | NO_MATCH | REJECTED
    match_method: str
    provider_key: str | None = None
    score: float | None = None
    data: dict[str, Any] = field(default_factory=dict)
    source_url: str | None = None
    raw: bytes | None = None
    review_note: str | None = None


class Enricher(ABC):
    provider: str = ""
    licence: str = ""
    needs_api_key: bool = False

    def __init__(
        self, config: EnrichmentConfig, source_id: str, fetcher: HttpFetcher, api_key: str | None = None
    ) -> None:
        self.config = config
        self.source_id = source_id
        self.fetcher = fetcher
        self.api_key = api_key
        self.bucket = TokenBucket.per_window(config.rate_limit_calls, config.rate_limit_window_s)
        r = config.retry
        self.retry = RetryPolicy(r.max_attempts, r.base_delay_s, r.max_delay_s)
        self.calls = 0

    # -- helpers -----------------------------------------------------------------------------------
    def get_json(
        self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> tuple[Any, bytes, str]:
        def call() -> tuple[bytes, str]:
            self.bucket.acquire()
            self.calls += 1
            body, res = self.fetcher.get(url, params=params, headers=headers)
            return body, res.final_url

        body, final = retry_call(call, self.retry)
        return json.loads(body) if body else None, body, final

    def subjects(self, conn: psycopg.Connection[Any], limit: int | None = None) -> list[Subject]:
        rows = fetch_all(
            conn,
            """
            SELECT rv.source_id, rv.source_key, rv.record_version_id, rv.entity_type, rv.primary_name, rv.doc
            FROM record_version rv
            WHERE rv.source_id = ANY(%s) AND rv.valid_to_seq IS NULL AND rv.entity_type = ANY(%s)
              AND NOT EXISTS (SELECT 1 FROM enrichment_record e WHERE e.subject_source_id = rv.source_id
                    AND e.subject_source_key = rv.source_key AND e.provider = %s AND e.superseded_by IS NULL
                    AND e.retrieved_at > now() - make_interval(days => %s))
            ORDER BY rv.created_at DESC LIMIT %s""",
            (
                self.config.subject_sources,
                self.config.entity_types,
                self.provider,
                self.config.refresh_days,
                limit or self.config.batch_size,
            ),
        )
        return [
            Subject(
                r["source_id"],
                r["source_key"],
                r["record_version_id"],
                r["entity_type"],
                r["primary_name"] or "",
                r["doc"],
            )
            for r in rows
            if self.wants(r)
        ]

    def wants(self, row: dict[str, Any]) -> bool:
        return True

    def prepare(self, subjects: list[Subject]) -> dict[str, Any]:
        """One-off work before the batch (bulk download, batched queries). Returns info for the run log."""
        return {}

    def classify(self, score: float) -> str:
        if score >= self.config.auto_accept_score:
            return "AUTO_ACCEPTED"
        if score >= self.config.review_score:
            return "NEEDS_REVIEW"
        return "NO_MATCH"

    @abstractmethod
    def enrich(self, subject: Subject) -> EnrichResult: ...


def archive_raw(
    conn: psycopg.Connection[Any], raw: bytes | None, content_type: str = "application/json"
) -> str | None:
    if not raw:
        return None
    sha, uri = get_blob_store().put_bytes(raw)
    conn.execute(
        "INSERT INTO raw_artifact (sha256, blob_uri, size_bytes, content_type, compression)"
        " VALUES (%s,%s,%s,%s,'gzip') ON CONFLICT DO NOTHING",
        (sha, uri, len(raw), content_type),
    )
    return sha


def compact(s: str | None) -> str:
    return re.sub(r"[\s\-./]", "", s or "").upper()
