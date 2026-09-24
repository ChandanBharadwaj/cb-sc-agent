"""Official notices (Level 2): legal evidence for listings and removals (FR-10, FR-14).

A NOTICE_SYNC run:
1. SYNC  - fetch the feed deterministically, store new notices (text archived for evidence), and raise
           early-pull *signals* for the lists the feed relates to (NFR-02: "extra pull on any notification")
2. MATCH - link notices to list records deterministically:
           * ID_MATCH   UN reference numbers (e.g. QDi.123) found in the text
           * NAME_MATCH exact normalised full names of records added/removed around the notice date
           Notices that announce removals but could not be matched are flagged NEEDS_AGENT so the
           supervisor can run the verified LLM extractor on them.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
from lxml import html as lxml_html

from sanctions_agent.canonical.normalize.names import normalize_name
from sanctions_agent.db.engine import fetch_all, fetch_val
from sanctions_agent.http.client import HttpFetcher, RetryPolicy
from sanctions_agent.sources.config_models import NoticeFeedConfig
from sanctions_agent.storage.blobstore import get_blob_store

UN_REF_RE = re.compile(r"\b([A-Z]{2}[ie]\.\d{3})\b")
DELIST_WORDS = (
    "de-listing",
    "delisting",
    "delisted",
    "removal",
    "removed",
    "deletion",
    "deleted",
    "unblock",
    "revoked",
    "revocation",
    "no longer",
)
LIST_WORDS = ("designation", "designated", "listing", "added", "addition", "identif")
AMEND_WORDS = ("amendment", "amended", "update", "revision", "correction")


@dataclass
class NoticeItem:
    external_id: str
    title: str
    url: str | None
    published_on: date | None
    text: str | None = None
    action_types: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def html_to_text(content: bytes | str) -> str:
    doc = lxml_html.fromstring(content if isinstance(content, bytes) else content.encode("utf-8"))
    for bad in doc.xpath("//script|//style|//nav|//header|//footer|//noscript"):
        bad.drop_tree()
    for el in doc.iter(
        "p",
        "div",
        "br",
        "li",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "table",
        "ul",
        "ol",
        "dt",
        "dd",
    ):
        el.tail = "\n" + (el.tail or "")
    text = doc.text_content()
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def classify_actions(text: str) -> list[str]:
    low = text.lower()
    out = []
    if any(w in low for w in DELIST_WORDS):
        out.append("DELISTING")
    if any(w in low for w in LIST_WORDS):
        out.append("LISTING")
    if any(w in low for w in AMEND_WORDS):
        out.append("AMENDMENT")
    return out


class NoticeFeed(ABC):
    provider: str = ""

    def __init__(self, config: NoticeFeedConfig, source_id: str) -> None:
        self.config = config
        self.source_id = source_id

    @property
    def retry(self) -> RetryPolicy:
        r = self.config.retry
        return RetryPolicy(r.max_attempts, r.base_delay_s, r.max_delay_s)

    @abstractmethod
    def list_items(self, fetcher: HttpFetcher, since: date) -> list[NoticeItem]:
        """Deterministically fetch and parse the feed index."""

    def fetch_text(self, fetcher: HttpFetcher, item: NoticeItem) -> str | None:
        """Full text for matching / extraction (default: none beyond the index)."""
        return item.text

    def keep(self, item: NoticeItem) -> bool:
        if not self.config.keywords:
            return True
        hay = f"{item.title} {item.text or ''}".lower()
        return any(k.lower() in hay for k in self.config.keywords)


# ---------------------------------------------------------------------------------------------
def store_notice(
    conn: psycopg.Connection[Any], provider: str, item: NoticeItem, related: list[str]
) -> int | None:
    """Insert a new notice (archiving its text). Returns notice_id, or None if already known."""
    exists = fetch_val(
        conn,
        "SELECT notice_id FROM legal_notice WHERE provider = %s AND external_id = %s",
        (provider, item.external_id),
    )
    if exists:
        return None
    raw_sha = None
    if item.text:
        sha, uri = get_blob_store().put_bytes(item.text.encode("utf-8"))
        conn.execute(
            "INSERT INTO raw_artifact (sha256, blob_uri, size_bytes, content_type, compression)"
            " VALUES (%s, %s, %s, 'text/plain', 'gzip') ON CONFLICT DO NOTHING",
            (sha, uri, len(item.text.encode("utf-8"))),
        )
        raw_sha = sha
    actions = item.action_types or classify_actions(f"{item.title}\n{item.text or ''}")
    return int(
        fetch_val(
            conn,
            """INSERT INTO legal_notice (provider, external_id, title, published_on, url, raw_sha256, text_excerpt,
               action_types, related_sources)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING notice_id""",
            (
                provider,
                item.external_id,
                item.title[:1000],
                item.published_on,
                item.url,
                raw_sha,
                (item.text or "")[:4000] or None,
                actions,
                related,
            ),
        )
    )


def notice_text(conn: psycopg.Connection[Any], notice_id: int) -> str:
    row = fetch_all(
        conn,
        """SELECT n.text_excerpt, a.blob_uri FROM legal_notice n LEFT JOIN raw_artifact a
                             ON a.sha256 = n.raw_sha256 WHERE n.notice_id = %s""",
        (notice_id,),
    )
    if not row:
        return ""
    if row[0]["blob_uri"]:
        with get_blob_store().open(row[0]["blob_uri"]) as fh:
            return fh.read().decode("utf-8", "replace")
    return row[0]["text_excerpt"] or ""


def match_notice(conn: psycopg.Connection[Any], notice_id: int, related_sources: list[str]) -> dict[str, int]:
    """Deterministic linking of one notice to records. Returns counts by method."""
    n = fetch_all(conn, "SELECT * FROM legal_notice WHERE notice_id = %s", (notice_id,))[0]
    text = notice_text(conn, notice_id)
    full = f"{n['title']}\n{text}"
    actions = n["action_types"] or []
    counts = {"ID_MATCH": 0, "NAME_MATCH": 0}
    # 1) UN reference numbers -> any list carrying that UN ref (UN, UK, EU)
    refs = sorted(set(UN_REF_RE.findall(full)))
    for ref in refs:
        norm = re.sub(r"[\s\-./]", "", ref).upper()
        for rec in fetch_all(
            conn,
            """SELECT DISTINCT k.source_id, k.source_key FROM crosslist_key k
                                      WHERE k.key_type = 'UN_REF' AND k.key_value = %s""",
            (norm,),
        ):
            link_type = _link_type_near(full, ref, actions)
            counts["ID_MATCH"] += _link(
                conn,
                notice_id,
                rec["source_id"],
                rec["source_key"],
                link_type,
                "ID_MATCH",
                1.0,
                _quote(full, ref),
            )
        # removed records no longer have current crosslist keys: match removal candidates by source_key
        for rc in fetch_all(
            conn,
            """SELECT source_id, source_key FROM removal_candidate
                                     WHERE source_key = %s AND status IN ('PENDING','EVIDENCE_FOUND')""",
            (ref,),
        ):
            counts["ID_MATCH"] += _link(
                conn,
                notice_id,
                rc["source_id"],
                rc["source_key"],
                "DELISTING",
                "ID_MATCH",
                1.0,
                _quote(full, ref),
            )
    # 2) exact normalised names of records changed within +/- 45 days of the notice
    if related_sources:
        norm_text = f" {normalize_name(full)} "
        pub = n["published_on"] or datetime.now(UTC).date()
        window = (pub - timedelta(days=45), pub + timedelta(days=45))
        events = fetch_all(
            conn,
            """
            SELECT e.source_id, e.source_key, e.change_type, rv.primary_name
            FROM change_event e JOIN record_version rv
              ON rv.record_version_id = coalesce(e.new_record_version_id, e.old_record_version_id)
            WHERE e.source_id = ANY(%s) AND e.change_type IN ('ADD','REMOVE','RELIST')
              AND e.created_at::date BETWEEN %s AND %s""",
            (related_sources, window[0], window[1]),
        )
        for ev in events:
            name = normalize_name(ev["primary_name"] or "")
            if len(name) < 6 or len(name.split()) < 2:
                continue  # too short/common for an exact-name link
            if f" {name} " in norm_text:
                lt = "DELISTING" if ev["change_type"] == "REMOVE" else "LISTING"
                if actions and lt not in actions:
                    continue  # the notice does not announce this kind of action
                counts["NAME_MATCH"] += _link(
                    conn,
                    notice_id,
                    ev["source_id"],
                    ev["source_key"],
                    lt,
                    "NAME_MATCH",
                    0.9,
                    _quote(full, ev["primary_name"] or ""),
                )
    matched = counts["ID_MATCH"] + counts["NAME_MATCH"]
    pending_removals = (
        fetch_val(
            conn,
            "SELECT count(*) FROM removal_candidate WHERE source_id = ANY(%s) AND status = 'PENDING'",
            (related_sources,),
        )
        if related_sources
        else 0
    )
    status = (
        "MATCHED"
        if matched
        else ("NEEDS_AGENT" if "DELISTING" in actions and pending_removals else "NO_MATCH")
    )
    conn.execute("UPDATE legal_notice SET extraction_status = %s WHERE notice_id = %s", (status, notice_id))
    return counts


def _link_type_near(text: str, needle: str, actions: list[str]) -> str:
    i = text.find(needle)
    window = text[max(0, i - 300) : i + 300].lower() if i >= 0 else ""
    if any(w in window for w in DELIST_WORDS):
        return "DELISTING"
    if any(w in window for w in AMEND_WORDS):
        return "AMENDMENT"
    if any(w in window for w in LIST_WORDS):
        return "LISTING"
    return actions[0] if len(actions) == 1 else "REFERENCE"


def _quote(text: str, needle: str) -> str | None:
    i = text.lower().find(needle.lower())
    if i < 0:
        return None
    return re.sub(r"\s+", " ", text[max(0, i - 120) : i + len(needle) + 120]).strip()


def _link(
    conn: psycopg.Connection[Any],
    notice_id: int,
    source_id: str,
    source_key: str,
    link_type: str,
    method: str,
    confidence: float,
    quote: str | None,
) -> int:
    cur = conn.execute(
        """INSERT INTO notice_link (notice_id, source_id, source_key, link_type, method, confidence, verbatim_quote, status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,'AUTO') ON CONFLICT DO NOTHING""",
        (notice_id, source_id, source_key, link_type, method, confidence, quote),
    )
    return cur.rowcount


def raise_signals(conn: psycopg.Connection[Any], provider: str, item: NoticeItem, targets: list[str]) -> int:
    n = 0
    for t in targets:
        n += conn.execute(
            """INSERT INTO signal (provider, external_id, title, url, target_source_id, payload)
               SELECT %s, %s, %s, %s, %s, '{}'::jsonb WHERE EXISTS (SELECT 1 FROM source WHERE source_id = %s)
               ON CONFLICT DO NOTHING""",
            (provider, f"{item.external_id}->{t}", item.title[:500], item.url, t, t),
        ).rowcount
    return n


def recent_cutoff(config: NoticeFeedConfig) -> date:
    return (datetime.now(UTC) - timedelta(days=config.lookback_days)).date()
