"""Removal (delisting) handling - BRD section 11 / 17.4, FR-10.

A record missing from a new file is only a *possible* removal. It keeps blocking (held in every
screening snapshot) until a human confirms it against an official notice. Absence can also mean a
parser bug, an ID change, or removal from one programme while still listed under another.

``assess`` gathers the evidence deterministically:
* official notices already linked to the record as DELISTING (Federal Register, OFAC Recent Actions,
  UN list-updates log by reference number, UK notices, EU amending acts)
* cross-list hits on strong keys (UN ref, IMO, LEI, registration/passport + country)
* ID-change suspects: another current record in the same list with the same strong ids or the same
  normalised name + birth date / country
and files one REMOVAL_CONFIRMATION proposal for the reviewer.
"""

from __future__ import annotations

from typing import Any

import psycopg

from sanctions_agent.db.engine import fetch_all, fetch_one, fetch_val, jsonb
from sanctions_agent.pipeline.publish import HOLDING_STATUSES, create_snapshot

RESOLUTIONS = ("CONFIRMED", "STILL_LISTED_ELSEWHERE", "REJECTED_ID_CHANGE", "REJECTED_PARSER")


def crosslist_hits(conn: psycopg.Connection[Any], cand: dict[str, Any]) -> list[dict[str, Any]]:
    return fetch_all(
        conn,
        """SELECT DISTINCT k2.source_id, k2.source_key, k2.key_type, k2.key_value
           FROM rv_identifier i
           JOIN LATERAL (SELECT CASE i.id_type WHEN 'REGISTRATION' THEN 'REG_COUNTRY'
                                               WHEN 'PASSPORT' THEN 'PASSPORT_COUNTRY' ELSE i.id_type END AS kt,
                                CASE WHEN i.id_type IN ('REGISTRATION','PASSPORT') THEN i.value_norm || '|' || i.country_iso2
                                     ELSE i.value_norm END AS kv) k ON true
           JOIN crosslist_key k2 ON k2.key_type = k.kt AND k2.key_value = k.kv
           WHERE i.record_version_id = %s AND NOT (k2.source_id = %s AND k2.source_key = %s)
           ORDER BY 1, 2""",
        (cand["last_record_version_id"], cand["source_id"], cand["source_key"]),
    )


def id_change_suspects(conn: psycopg.Connection[Any], cand: dict[str, Any]) -> list[dict[str, Any]]:
    """Current records in the SAME list that look like the removed one under a new key."""
    by_id = fetch_all(
        conn,
        """SELECT DISTINCT rv.source_key, 'identifier' AS reason
           FROM rv_identifier old JOIN rv_identifier cur ON cur.id_type = old.id_type AND cur.value_norm = old.value_norm
           JOIN record_version rv ON rv.record_version_id = cur.record_version_id
           WHERE old.record_version_id = %s AND rv.source_id = %s AND rv.valid_to_seq IS NULL
             AND rv.source_key <> %s AND old.id_type NOT IN ('OTHER','EMAIL','WEBSITE')""",
        (cand["last_record_version_id"], cand["source_id"], cand["source_key"]),
    )
    by_name = fetch_all(
        conn,
        """SELECT DISTINCT rv.source_key, 'same primary name' AS reason
           FROM rv_name old JOIN rv_name cur ON cur.normalized_name = old.normalized_name AND cur.name_type = 'PRIMARY'
           JOIN record_version rv ON rv.record_version_id = cur.record_version_id
           WHERE old.record_version_id = %s AND old.name_type = 'PRIMARY' AND rv.source_id = %s
             AND rv.valid_to_seq IS NULL AND rv.source_key <> %s""",
        (cand["last_record_version_id"], cand["source_id"], cand["source_key"]),
    )
    seen: dict[str, dict[str, Any]] = {}
    for r in by_id + by_name:
        seen.setdefault(r["source_key"], r)
    return list(seen.values())


def delisting_evidence(conn: psycopg.Connection[Any], cand: dict[str, Any]) -> list[dict[str, Any]]:
    return fetch_all(
        conn,
        """SELECT n.notice_id, n.provider, n.title, n.url, n.published_on, l.method, l.confidence, l.status,
                  l.verbatim_quote
           FROM notice_link l JOIN legal_notice n USING (notice_id)
           WHERE l.source_id = %s AND l.source_key = %s AND l.link_type = 'DELISTING' AND l.status <> 'REJECTED'
             AND (n.published_on IS NULL OR n.published_on >= (SELECT created_at::date - 60 FROM removal_candidate
                                                                WHERE candidate_id = %s))
           ORDER BY l.confidence DESC, n.published_on DESC""",
        (cand["source_id"], cand["source_key"], cand["candidate_id"]),
    )


def assess(
    conn: psycopg.Connection[Any],
    candidate_id: int,
    *,
    proposed_by: str = "system:removals",
    agent_cycle_id: str | None = None,
) -> dict[str, Any]:
    cand = fetch_one(
        conn, "SELECT * FROM removal_candidate WHERE candidate_id = %s FOR UPDATE", (candidate_id,)
    )
    if cand is None:
        raise KeyError(candidate_id)
    if cand["status"] not in HOLDING_STATUSES:
        return {"candidate_id": candidate_id, "status": cand["status"], "note": "already decided"}
    evidence = delisting_evidence(conn, cand)
    hits = crosslist_hits(conn, cand)
    suspects = id_change_suspects(conn, cand)
    name = fetch_val(
        conn,
        "SELECT primary_name FROM record_version WHERE record_version_id = %s",
        (cand["last_record_version_id"],),
    )
    new_status = "EVIDENCE_FOUND" if evidence else cand["status"]
    conn.execute(
        "UPDATE removal_candidate SET status = %s, crosslist_hits = %s, evidence_notice_id = %s"
        " WHERE candidate_id = %s",
        (new_status, jsonb(hits), evidence[0]["notice_id"] if evidence else None, candidate_id),
    )
    recommendation = (
        "REJECTED_ID_CHANGE"
        if suspects and not evidence
        else "STILL_LISTED_ELSEWHERE"
        if evidence and hits
        else "CONFIRMED"
        if evidence
        else None
    )
    change_id = None
    if recommendation:
        title = {
            "CONFIRMED": f"Confirm delisting of {name} from {cand['source_id']}",
            "STILL_LISTED_ELSEWHERE": f"Release {cand['source_id']} listing of {name} (still listed on "
            f"{', '.join(sorted({h['source_id'] for h in hits}))})",
            "REJECTED_ID_CHANGE": f"{name} disappeared from {cand['source_id']} but a matching record exists under a "
            f"new key - confirm ID change",
        }[recommendation]
        change_id = fetch_val(
            conn,
            """INSERT INTO proposed_change (kind, source_id, subject_ref, title, payload, evidence_urls, verbatim_quotes,
                   rationale, proposed_by, agent_cycle_id, dedupe_key)
               VALUES ('REMOVAL_CONFIRMATION', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING RETURNING change_id""",
            (
                cand["source_id"],
                f"{cand['source_id']}:{cand['source_key']}",
                title,
                jsonb(
                    {
                        "candidate_id": candidate_id,
                        "recommendation": recommendation,
                        "crosslist_hits": hits,
                        "id_change_suspects": suspects,
                        "evidence": [dict(e, published_on=str(e["published_on"])) for e in evidence],
                    }
                ),
                [e["url"] for e in evidence if e.get("url")],
                jsonb([e["verbatim_quote"] for e in evidence if e.get("verbatim_quote")]),
                "Removal detected by snapshot diff; evidence gathered automatically. Unblocking requires human approval.",
                proposed_by,
                agent_cycle_id,
                f"removal:{candidate_id}",
            ),
        )
    return {
        "candidate_id": candidate_id,
        "status": new_status,
        "evidence": len(evidence),
        "crosslist_hits": len(hits),
        "id_change_suspects": len(suspects),
        "recommendation": recommendation,
        "proposed_change_id": change_id,
    }


def resolve(
    conn: psycopg.Connection[Any], candidate_id: int, resolution: str, *, reviewer: str, rationale: str | None
) -> int | None:
    """Apply a reviewer's decision and publish a new screening snapshot reflecting the released hold."""
    if resolution not in RESOLUTIONS:
        raise ValueError(f"resolution must be one of {RESOLUTIONS}")
    conn.execute(
        "UPDATE removal_candidate SET status = %s, decided_by = %s, decided_at = now(), rationale = %s"
        " WHERE candidate_id = %s AND status = ANY(%s)",
        (resolution, reviewer, rationale, candidate_id, list(HOLDING_STATUSES)),
    )
    if resolution == "REJECTED_PARSER":
        return None  # still held: the record must come back after the parser fix / reparse
    return create_snapshot(
        conn, trigger_version_id=None, note=f"removal {candidate_id} resolved: {resolution}"
    )
