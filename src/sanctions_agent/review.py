"""Human-in-the-loop review of proposed changes (FR-20).

Every agent output and every risky system action is a *proposal*; nothing reaches screening until a
human with the right role approves it. The same person cannot approve their own proposal.
"""

from __future__ import annotations

from typing import Any

import psycopg

from sanctions_agent.db.engine import fetch_one, fetch_val, jsonb
from sanctions_agent.pipeline import removals, runs
from sanctions_agent.sources.config_service import ConfigService, PermissionDenied, ValidationFailed

REVIEW_ROLES = {"reviewer", "admin"}
ADMIN_KINDS = {"CONFIG_CHANGE", "SOURCE_ACTIVATION"}


def decide(
    conn: psycopg.Connection[Any],
    change_id: int,
    *,
    reviewer: str,
    role: str,
    approve: bool,
    comment: str | None = None,
    resolution: str | None = None,
) -> dict[str, Any]:
    ch = fetch_one(conn, "SELECT * FROM proposed_change WHERE change_id = %s FOR UPDATE", (change_id,))
    if ch is None:
        raise KeyError(change_id)
    if ch["status"] != "PENDING":
        raise ValidationFailed(f"proposal {change_id} is already {ch['status']}")
    kind = ch["kind"]
    if kind in ADMIN_KINDS:
        if role != "admin":
            raise PermissionDenied("configuration changes need an admin")
        status = ConfigService(conn).decide_change(
            change_id, reviewer=reviewer, approve=approve, comment=comment
        )
        return {"change_id": change_id, "status": status}
    if role not in REVIEW_ROLES:
        raise PermissionDenied("reviewer or admin role required")
    if ch["proposed_by"] == reviewer:
        raise PermissionDenied("maker-checker: you cannot approve your own proposal")

    result: dict[str, Any] = {"change_id": change_id}
    payload = ch["payload"]
    if not approve:
        _close(conn, change_id, "REJECTED", reviewer, comment)
        if kind == "NOTICE_LINK" and payload.get("link_id"):
            conn.execute(
                "UPDATE notice_link SET status = 'REJECTED' WHERE link_id = %s", (payload["link_id"],)
            )
        if kind == "ENRICHMENT_MATCH" and payload.get("enrichment_id"):
            conn.execute(
                "UPDATE enrichment_record SET status = 'REJECTED' WHERE enrichment_id = %s",
                (payload["enrichment_id"],),
            )
        result["status"] = "REJECTED"
        return result

    if kind == "LARGE_CHANGE_RELEASE":
        run_id = runs.enqueue_run(
            conn,
            source_id=ch["source_id"],
            run_kind="RELEASE_HELD",
            trigger="MANUAL",
            requested_by=reviewer,
            reason=f"release approved in proposal {change_id}",
            options={"version_id": payload["version_id"]},
        )
        _close(conn, change_id, "APPLIED", reviewer, comment, run_id)
        result.update(status="APPLIED", run_id=run_id)
    elif kind == "REMOVAL_CONFIRMATION":
        res = resolution or payload.get("recommendation") or "CONFIRMED"
        snap = removals.resolve(conn, int(payload["candidate_id"]), res, reviewer=reviewer, rationale=comment)
        _close(conn, change_id, "APPLIED", reviewer, comment)
        result.update(status="APPLIED", resolution=res, snapshot_id=snap)
    elif kind == "NOTICE_LINK":
        conn.execute("UPDATE notice_link SET status = 'APPROVED' WHERE link_id = %s", (payload["link_id"],))
        _close(conn, change_id, "APPLIED", reviewer, comment)
        result["status"] = "APPLIED"
    elif kind == "ENRICHMENT_MATCH":
        conn.execute(
            "UPDATE enrichment_record SET status = 'APPROVED' WHERE enrichment_id = %s",
            (payload["enrichment_id"],),
        )
        _close(conn, change_id, "APPLIED", reviewer, comment)
        result["status"] = "APPLIED"
    elif kind == "ANNEX_ENTRY":
        from sanctions_agent.sources.l1.eu_annex import apply_annex_proposal

        result.update(apply_annex_proposal(conn, ch, reviewer))
        _close(conn, change_id, "APPLIED", reviewer, comment, result.get("run_id"))
        result["status"] = "APPLIED"
    else:
        raise ValidationFailed(f"unsupported proposal kind {kind}")
    return result


def _close(
    conn: psycopg.Connection[Any],
    change_id: int,
    status: str,
    reviewer: str,
    comment: str | None,
    run_id: str | None = None,
) -> None:
    conn.execute(
        "UPDATE proposed_change SET status = %s, reviewed_by = %s, reviewed_at = now(), review_comment = %s,"
        " applied_in_run_id = %s WHERE change_id = %s",
        (status, reviewer, comment, run_id, change_id),
    )


def pending_count(conn: psycopg.Connection[Any]) -> int:
    return int(fetch_val(conn, "SELECT count(*) FROM proposed_change WHERE status = 'PENDING'"))


def add_proposal(
    conn: psycopg.Connection[Any],
    *,
    kind: str,
    title: str,
    payload: dict[str, Any],
    proposed_by: str,
    source_id: str | None = None,
    subject_ref: str | None = None,
    evidence_urls: list[str] | None = None,
    verbatim_quotes: list[str] | None = None,
    verification: dict[str, Any] | None = None,
    rationale: str | None = None,
    agent_cycle_id: str | None = None,
    dedupe_key: str | None = None,
) -> int | None:
    return fetch_val(  # type: ignore[no-any-return]
        conn,
        """INSERT INTO proposed_change (kind, source_id, subject_ref, title, payload, evidence_urls, verbatim_quotes,
               verification, rationale, proposed_by, agent_cycle_id, dedupe_key)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING change_id""",
        (
            kind,
            source_id,
            subject_ref,
            title,
            jsonb(payload),
            evidence_urls or [],
            jsonb(verbatim_quotes or []),
            jsonb(verification or {}),
            rationale,
            proposed_by,
            agent_cycle_id,
            dedupe_key,
        ),
    )
