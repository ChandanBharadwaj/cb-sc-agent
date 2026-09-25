"""Tools available to the supervisor agent.

Read tools return compact, aggregate state. Act tools go through the same guards the UI uses
(status, maintenance mode, politeness floor, circuit breaker, one run per source) and are rate-limited
per cycle. There is deliberately NO tool to approve proposals, publish held versions, edit URLs /
allow-lists, change configuration directly, or delete anything - the agent can only *propose*.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from agents import RunContextWrapper, Tool, function_tool

from sanctions_agent.agent.context import AgentContext
from sanctions_agent.agent.guards import ToolDenied, guarded
from sanctions_agent.agent.state_snapshot import snapshot
from sanctions_agent.db.engine import fetch_all, fetch_one, jsonb, tx
from sanctions_agent.ops import autopilot, incidents
from sanctions_agent.pipeline import removals, runs
from sanctions_agent.sources.config_service import ConfigService, ValidationFailed
from sanctions_agent.sources.registry import get_source

Ctx = RunContextWrapper[AgentContext]


# ------------------------------------------------------------------ read ----------------------
@guarded("read")
def get_system_status(rc: Ctx) -> dict[str, Any]:
    """Current health of every source (freshness vs SLO, breaker, failures, current version, active run),
    open incidents, recent failures, pending reviews, removal holds, signals and open data-quality issues."""
    with tx() as conn:
        return snapshot(conn)


@guarded("read")
def list_recent_runs(rc: Ctx, source_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Most recent runs of one source with status, trigger, step, error class and summary."""
    with tx() as conn:
        return fetch_all(
            conn,
            """SELECT run_id::text, run_kind, trigger, requested_by, status, current_step, attempt,
                error_class, left(error_detail, 300) AS error_detail, summary, queued_at::text, finished_at::text
            FROM ingestion_run WHERE source_id = %s ORDER BY queued_at DESC LIMIT %s""",
            (source_id, min(limit, 30)),
        )


@guarded("read")
def get_run(rc: Ctx, run_id: str) -> dict[str, Any]:
    """One run in detail: steps with their checkpoints, fetch evidence per attempt, validation outcome."""
    with tx() as conn:
        run = fetch_one(
            conn, "SELECT *, run_id::text AS run_id FROM ingestion_run WHERE run_id = %s", (run_id,)
        )
        if run is None:
            raise ValueError(f"unknown run {run_id}")
        steps = fetch_all(
            conn,
            "SELECT step, attempt, status, detail, error FROM run_step WHERE run_id = %s ORDER BY started_at",
            (run_id,),
        )
        fetches = fetch_all(
            conn,
            """SELECT attempt, http_status, final_url, error_class, left(error_detail, 300) AS error,
                duration_ms, size_bytes, not_modified FROM fetch_evidence WHERE run_id = %s ORDER BY attempt""",
            (run_id,),
        )
        version = fetch_one(
            conn,
            "SELECT version_id, seq, status, record_count, validation_report FROM list_version"
            " WHERE run_id = %s",
            (run_id,),
        )
    return {
        "run": {
            k: run[k]
            for k in (
                "run_id",
                "source_id",
                "run_kind",
                "trigger",
                "status",
                "error_class",
                "error_detail",
                "summary",
                "options",
            )
        },
        "steps": steps,
        "fetch_attempts": fetches,
        "version": version,
    }


@guarded("read")
def get_error_history(rc: Ctx, source_id: str, hours: int = 72) -> dict[str, Any]:
    """Failure pattern for a source: error classes by hour and HTTP statuses seen, to tell transient
    bursts (e.g. EU FSF 500s) from persistent blocks (403) or moved files (404)."""
    with tx() as conn:
        classes = fetch_all(
            conn,
            """SELECT error_class, count(*) AS n, min(fetched_at)::text AS first, max(fetched_at)::text AS last
            FROM fetch_evidence WHERE source_id = %s AND fetched_at > now() - make_interval(hours => %s)
              AND error_class IS NOT NULL GROUP BY 1 ORDER BY n DESC""",
            (source_id, min(hours, 720)),
        )
        ok = fetch_one(
            conn,
            """SELECT count(*) AS n, max(fetched_at)::text AS last FROM fetch_evidence
            WHERE source_id = %s AND fetched_at > now() - make_interval(hours => %s) AND error_class IS NULL""",
            (source_id, min(hours, 720)),
        )
        samples = fetch_all(
            conn,
            """SELECT http_status, left(error_detail, 200) AS detail, fetched_at::text FROM fetch_evidence
            WHERE source_id = %s AND error_class IS NOT NULL ORDER BY fetched_at DESC LIMIT 5""",
            (source_id,),
        )
    return {"error_classes": classes, "successful_fetches": ok, "latest_errors": samples}


@guarded("read")
def get_validation_report(rc: Ctx, version_id: int) -> dict[str, Any]:
    """Validation report of a list version: outcome, counts, fill rates vs floors, drift paths, diff preview,
    and the open data-quality issues it raised."""
    with tx() as conn:
        v = fetch_one(
            conn,
            "SELECT version_id, source_id, seq, status, record_count, counts_by_type, validation_report"
            " FROM list_version WHERE version_id = %s",
            (version_id,),
        )
        if v is None:
            raise ValueError(f"unknown version {version_id}")
        issues = fetch_all(
            conn,
            "SELECT category, severity, field, count, detail FROM dq_issue WHERE version_id = %s",
            (version_id,),
        )
    return {"version": v, "issues": issues}


@guarded("read")
def list_removal_candidates(rc: Ctx, source_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Records that disappeared from a list and are still blocking, with evidence status (no personal details)."""
    with tx() as conn:
        return fetch_all(
            conn,
            """SELECT candidate_id, source_id, source_key, status, evidence_notice_id IS NOT NULL AS has_evidence,
                jsonb_array_length(crosslist_hits) AS crosslist_hits, created_at::text
            FROM removal_candidate WHERE status IN ('PENDING','EVIDENCE_FOUND','REJECTED_PARSER')
              AND (%s::text IS NULL OR source_id = %s) ORDER BY created_at LIMIT %s""",
            (source_id, source_id, min(limit, 50)),
        )


@guarded("read")
def search_notices(
    rc: Ctx, text: str | None = None, provider: str | None = None, days: int = 14
) -> list[dict[str, Any]]:
    """Official notices fetched recently (Federal Register, OFAC Recent Actions, UN updates, UK notices, EUR-Lex)."""
    with tx() as conn:
        return fetch_all(
            conn,
            """SELECT notice_id, provider, title, published_on::text, url, extraction_status, action_types
            FROM legal_notice WHERE fetched_at > now() - make_interval(days => %s)
              AND (%s::text IS NULL OR provider = %s) AND (%s::text IS NULL OR title ILIKE '%%' || %s || '%%')
            ORDER BY published_on DESC NULLS LAST LIMIT 30""",
            (min(days, 90), provider, provider, text, text),
        )


# ------------------------------------------------------------------ act -----------------------
@guarded("act", limit=("enqueue", "max_enqueues"))
def run_ingestion(rc: Ctx, source_id: str, reason: str, force_refetch: bool = False) -> dict[str, Any]:
    """Queue a pull now. Refused if the source is paused/disabled, in maintenance, within the politeness
    interval, breaker-open, or already running. ``force_refetch`` ignores conditional GET / same-hash checks."""
    with tx(actor=rc.context.actor) as conn:
        batch = runs.batch_for_cycle(conn, rc.context.cycle_id, rc.context.actor)
        res = autopilot.enqueue_guarded(
            conn,
            source_id,
            trigger="AGENT",
            requested_by=rc.context.actor,
            reason=reason,
            agent_cycle_id=rc.context.cycle_id,
            options={"force_refetch": True} if force_refetch else None,
            batch_id=batch,
        )
        if not res["ok"]:  # visible in the agent's batch: what it asked for and why it was refused
            runs.record_refused(conn, batch, [{"source_id": source_id, "why": res["why"]}])
    if not res["ok"]:
        raise ToolDenied(res["why"])
    return res


@guarded("act", limit=("enqueue", "max_enqueues"))
def schedule_retry(rc: Ctx, source_id: str, delay_minutes: int, reason: str) -> dict[str, Any]:
    """Queue a pull after a delay (5-720 minutes) - e.g. to back off from a publisher's burst of HTTP 500s."""
    if not 5 <= delay_minutes <= 720:
        raise ToolDenied("delay_minutes must be between 5 and 720")
    with tx(actor=rc.context.actor) as conn:
        src = get_source(conn, source_id)
        if src.status != "ACTIVE":
            raise ToolDenied(f"source is {src.status}")
        try:
            rid = runs.enqueue_run(
                conn,
                source_id=source_id,
                run_kind=autopilot.KIND_TO_RUN[src.kind],
                trigger="AGENT",
                requested_by=rc.context.actor,
                reason=reason,
                agent_cycle_id=rc.context.cycle_id,
                not_before=datetime.now(UTC) + timedelta(minutes=delay_minutes),
                batch_id=runs.batch_for_cycle(conn, rc.context.cycle_id, rc.context.actor),
            )
        except runs.RunAlreadyActive as e:
            raise ToolDenied(f"already has an active run {e.run_id}") from e
    return {"ok": True, "run_id": rid, "starts_after_minutes": delay_minutes}


@guarded("act", limit=("enqueue", "max_enqueues"))
def reparse_archived(rc: Ctx, source_id: str, sha256: str, reason: str) -> dict[str, Any]:
    """Re-run validation and parsing on an already archived file (no download) - e.g. after a parser fix,
    or to retry a version quarantined by a transient internal error."""
    with tx(actor=rc.context.actor) as conn:
        ok = fetch_one(
            conn,
            "SELECT 1 FROM list_version WHERE source_id = %s AND raw_sha256 = %s LIMIT 1",
            (source_id, sha256),
        )
        if ok is None:
            raise ToolDenied("that archived file does not belong to this source")
        res = autopilot.enqueue_guarded(
            conn,
            source_id,
            trigger="AGENT",
            requested_by=rc.context.actor,
            reason=reason,
            agent_cycle_id=rc.context.cycle_id,
            options={"reparse_sha256": sha256},
            ignore_min_interval=True,
            batch_id=(batch := runs.batch_for_cycle(conn, rc.context.cycle_id, rc.context.actor)),
        )
        if not res["ok"]:
            runs.record_refused(conn, batch, [{"source_id": source_id, "why": res["why"]}])
    if not res["ok"]:
        raise ToolDenied(res["why"])
    return res


@guarded("act", limit=("proposals", "max_proposals"))
def assess_removal(rc: Ctx, candidate_id: int) -> dict[str, Any]:
    """Gather delisting evidence, cross-list hits and ID-change suspects for a removal candidate and file a
    REMOVAL_CONFIRMATION proposal for human review when there is something to decide."""
    with tx(actor=rc.context.actor) as conn:
        return removals.assess(
            conn, candidate_id, proposed_by=rc.context.actor, agent_cycle_id=rc.context.cycle_id
        )


@guarded("act", limit=("incident", "max_incident_writes"))
def record_diagnosis(
    rc: Ctx,
    incident_id: int,
    diagnosis: str,
    action_taken: str | None = None,
    severity: Literal["INFO", "WARN", "PAGE"] | None = None,
) -> dict[str, Any]:
    """Write your diagnosis (root cause, evidence, next step) on an incident; optionally raise/lower severity."""
    with tx(actor=rc.context.actor) as conn:
        row = fetch_one(conn, "SELECT status FROM incident WHERE incident_id = %s", (incident_id,))
        if row is None:
            raise ValueError(f"unknown incident {incident_id}")
        incidents.set_diagnosis(conn, incident_id, diagnosis[:4000])
        if severity:
            conn.execute("UPDATE incident SET severity = %s WHERE incident_id = %s", (severity, incident_id))
        if action_taken:
            incidents.add_action(
                conn,
                incident_id,
                {
                    "at": datetime.now(UTC).isoformat(),
                    "by": rc.context.actor,
                    "action": action_taken[:1000],
                    "cycle_id": rc.context.cycle_id,
                },
            )
    return {"ok": True}


@guarded("act", limit=("incident", "max_incident_writes"))
def raise_incident(
    rc: Ctx,
    title: str,
    summary: str,
    severity: Literal["INFO", "WARN", "PAGE"],
    source_id: str | None = None,
    error_class: str = "AGENT_OBSERVATION",
) -> dict[str, Any]:
    """Open (or bump) an incident so on-call is alerted - for problems the deterministic checks do not cover."""
    with tx(actor=rc.context.actor) as conn:
        iid, new = incidents.open_incident(
            conn,
            source_id=source_id,
            error_class=error_class,
            severity=severity,
            title=title[:300],
            summary=summary[:2000],
            dedupe_key=f"{source_id or 'global'}:{error_class}:{title[:80]}",
        )
    return {"incident_id": iid, "new": new}


@guarded("act", limit=("incident", "max_incident_writes"))
def resolve_incident(rc: Ctx, incident_id: int, resolution: str) -> dict[str, Any]:
    """Resolve an incident you have verified is fixed (e.g. the latest run succeeded)."""
    with tx(actor=rc.context.actor) as conn:
        n = incidents.resolve(conn, incident_id=incident_id, note=resolution[:500])
    return {"resolved": bool(n)}


@guarded("act", limit=("proposals", "max_proposals"))
def propose_config_change(
    rc: Ctx, source_id: str, json_patch: str, rationale: str, evidence_url: str | None = None
) -> dict[str, Any]:
    """Propose a configuration change (e.g. a new download URL you found, a retry policy, a threshold).
    ``json_patch`` is a JSON object (as text) merged into the source config. It is NEVER applied directly: an admin must approve,
    and URL/host changes need a second admin. Hosts must already be on the deployment allow-list."""
    with tx(actor=rc.context.actor) as conn:
        src = get_source(conn, source_id)
        try:
            patch = json.loads(json_patch)
        except json.JSONDecodeError as e:
            raise ToolDenied(f"json_patch is not valid JSON: {e}") from e
        if not isinstance(patch, dict):
            raise ToolDenied("json_patch must be a JSON object")
        merged = _merge(dict(src.config_raw), patch)
        try:
            res = ConfigService(conn).update_source(
                source_id,
                actor=rc.context.actor,
                reason=f"{rationale} (evidence: {evidence_url or 'n/a'})",
                expected_version=src.config_version,
                config=merged,
                force_review=True,
            )
        except ValidationFailed as e:
            raise ToolDenied(f"invalid proposal: {e}") from e
    return {
        "pending_change_id": res.pending_change_id,
        "changed_paths": res.changed_paths,
        "message": res.message,
    }


@guarded("read")
def discover_download_links(rc: Ctx, source_id: str, landing_page_url: str) -> dict[str, Any]:
    """Fetch an allow-listed landing page and list links that look like data files (xml/json/csv/xlsx/zip),
    to find a moved download URL. Returns candidates only; use propose_config_change to suggest one."""
    import re

    from sanctions_agent.http.client import HttpFetcher
    from sanctions_agent.http.guard import UrlGuard

    with tx() as conn:
        src = get_source(conn, source_id)
    hosts = list(
        getattr(getattr(src.config, "fetch", None), "allowed_hosts", None)
        or getattr(src.config, "allowed_hosts", [])
    )
    with HttpFetcher(UrlGuard(hosts)) as f:
        body, res = f.get(landing_page_url, max_bytes=5 * 1024 * 1024)
    html = body.decode("utf-8", "replace")
    links = sorted(
        {
            m
            for m in re.findall(r'href="([^"]+)"', html)
            if re.search(r"\.(xml|json|csv|xlsx|zip|ods)(\?|$)", m, re.I) or "download" in m.lower()
        }
    )[:40]
    return {"final_url": res.final_url, "http_status": res.http_status, "candidate_links": links}


@guarded("act", limit=("extractions", "max_extractions"))
def extract_notice(rc: Ctx, notice_id: int) -> dict[str, Any]:
    """Run the verified LLM extractor on an official notice that could not be matched deterministically
    (e.g. an OFAC press release announcing removals). Verified entries that exactly match a current record
    or a pending removal become NOTICE_LINK proposals for human review. Unverified entries are reported only."""
    from sanctions_agent.agent.extractors import Extractor
    from sanctions_agent.canonical.normalize.names import normalize_name
    from sanctions_agent.enrichment.notices.base import notice_text
    from sanctions_agent.review import add_proposal

    with tx() as conn:
        n = fetch_one(conn, "SELECT * FROM legal_notice WHERE notice_id = %s", (notice_id,))
        if n is None:
            raise ValueError(f"unknown notice {notice_id}")
        text = n["title"] + "\n" + notice_text(conn, notice_id)
    extraction, verification, cycle_id = Extractor().extract(
        text, purpose="notice", subject_ref=f"notice:{notice_id}"
    )
    proposals, unmatched = 0, 0
    with tx(actor=rc.context.actor) as conn:
        for ve in verification.entries:
            if not ve.ok or ve.entry.action not in ("LISTING", "DELISTING", "AMENDMENT"):
                continue
            norm = normalize_name(ve.entry.name)
            recs = fetch_all(
                conn,
                """SELECT DISTINCT rv.source_id, rv.source_key FROM rv_name nm
                JOIN record_version rv USING (record_version_id)
                WHERE nm.normalized_name = %s AND rv.source_id = ANY(%s)
                  AND (rv.valid_to_seq IS NULL OR EXISTS (SELECT 1 FROM removal_candidate c WHERE c.source_id = rv.source_id
                       AND c.source_key = rv.source_key AND c.status IN ('PENDING','EVIDENCE_FOUND')))""",
                (norm, n["related_sources"] or []),
            )
            if not recs:
                unmatched += 1
                continue
            for r in recs:
                link = fetch_one(
                    conn,
                    """INSERT INTO notice_link (notice_id, source_id, source_key, link_type, method,
                        confidence, verbatim_quote, status) VALUES (%s,%s,%s,%s,'AGENT',0.8,%s,'PENDING')
                        ON CONFLICT DO NOTHING RETURNING link_id""",
                    (notice_id, r["source_id"], r["source_key"], ve.entry.action, ve.entry.verbatim_quote),
                )
                if link is None:
                    continue
                cid = add_proposal(
                    conn,
                    kind="NOTICE_LINK",
                    source_id=r["source_id"],
                    subject_ref=f"{r['source_id']}:{r['source_key']}",
                    title=f"{ve.entry.action.title()} evidence for {ve.entry.name} in notice {notice_id}",
                    payload={"link_id": link["link_id"], "notice_id": notice_id, "action": ve.entry.action},
                    proposed_by=rc.context.actor,
                    agent_cycle_id=rc.context.cycle_id,
                    evidence_urls=[n["url"]] if n["url"] else [],
                    verbatim_quotes=[ve.entry.verbatim_quote],
                    verification={"ok": True},
                    dedupe_key=f"noticelink:{link['link_id']}",
                )
                conn.execute(
                    "UPDATE notice_link SET proposed_change_id = %s WHERE link_id = %s",
                    (cid, link["link_id"]),
                )
                proposals += 1
        conn.execute(
            "UPDATE legal_notice SET extraction_status = 'EXTRACTED', extracted = %s WHERE notice_id = %s",
            (
                jsonb(
                    {
                        "cycle_id": cycle_id,
                        "entries": len(extraction.entries),
                        "verified": sum(1 for e in verification.entries if e.ok),
                    }
                ),
                notice_id,
            ),
        )
    return {
        "entries": len(extraction.entries),
        "verified": sum(1 for e in verification.entries if e.ok),
        "proposals": proposals,
        "unmatched_verified_entries": unmatched,
        "failed_verification": [p for e in verification.entries for p in e.problems][:10],
    }


@guarded("act", limit=("extractions", "max_extractions"))
def extract_annex_act(rc: Ctx, notice_id: int, annex: Literal["XLII", "IV"]) -> dict[str, Any]:
    """Extract Annex XLII (vessels) or Annex IV (entities) entries from an EU Official Journal act and file an
    ANNEX_ENTRY proposal. Every entry is verified against the act text (verbatim quote, IMO checksum, no
    dropped IMO numbers); a human approves before anything reaches screening."""
    from sanctions_agent.agent.extractors import Extractor
    from sanctions_agent.enrichment.notices.base import notice_text
    from sanctions_agent.sources.l1.eu_annex import act_from_notice, entries_from_extraction, propose_entries

    source_id = "eu_annex_xlii" if annex == "XLII" else "eu_annex_iv"
    with tx() as conn:
        text = notice_text(conn, notice_id)
        act = act_from_notice(conn, notice_id)
    if f"annex {annex}".lower() not in text.lower():
        raise ToolDenied(f"the act text does not mention Annex {annex}")
    hint = (
        f"Extract ONLY the entries being added to or removed from Annex {annex} of Regulation 833/2014. "
        + (
            "Each vessel entry has a name and an IMO number."
            if annex == "XLII"
            else "Each entry is an entity name."
        )
    )
    extraction, verification, cycle_id = Extractor().extract(
        text, purpose=f"annex_{annex}", subject_ref=act["celex"], expect_imo_rows=annex == "XLII", hint=hint
    )
    entries = entries_from_extraction(extraction, verification)
    ops = {e["action"] for e in entries}
    operation = "REMOVE" if ops == {"DELISTING"} else "ADD"
    with tx(actor=rc.context.actor) as conn:
        cid = propose_entries(
            conn,
            source_id=source_id,
            operation=operation,
            entries=entries,
            act=act,
            proposed_by=rc.context.actor,
            agent_cycle_id=rc.context.cycle_id,
            count_check=verification.count_check,
        )
        conn.execute(
            "UPDATE legal_notice SET extraction_status = 'EXTRACTED', extracted = %s WHERE notice_id = %s",
            (jsonb({"cycle_id": cycle_id, "annex": annex, "proposal": cid}), notice_id),
        )
    return {
        "proposal_id": cid,
        "entries": len(entries),
        "verified": sum(1 for e in entries if e["ok"]),
        "count_check": verification.count_check,
    }


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = _merge(dict(base[k]), v)
        else:
            base[k] = v
    return base


READ_TOOLS = [
    get_system_status,
    list_recent_runs,
    get_run,
    get_error_history,
    get_validation_report,
    list_removal_candidates,
    search_notices,
]
ACT_TOOLS = [
    run_ingestion,
    schedule_retry,
    reparse_archived,
    assess_removal,
    record_diagnosis,
    raise_incident,
    resolve_incident,
    propose_config_change,
    discover_download_links,
    extract_notice,
    extract_annex_act,
]


def supervisor_tools() -> list[Tool]:
    return [function_tool(f) for f in READ_TOOLS + ACT_TOOLS]  # type: ignore[call-overload]
