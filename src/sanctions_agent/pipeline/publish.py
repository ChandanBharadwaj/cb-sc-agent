"""DIFF_PUBLISH: promote a validated staging set to published data in ONE transaction.

Screening never sees half-published data: either every SCD2 row, child row, change event, removal
candidate, cross-list key and the new screening snapshot become visible together, or none do.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import psycopg

from sanctions_agent.canonical.normalize.names import normalize_name
from sanctions_agent.db.engine import fetch_all, fetch_one, fetch_val, jsonb

HOLDING_STATUSES = ("PENDING", "EVIDENCE_FOUND", "REJECTED_PARSER")


def _iso2(v: Any) -> str | None:
    return v if isinstance(v, str) and len(v) == 2 else None


def child_rows(rv_id: int, d: dict[str, Any]) -> dict[str, list[tuple[Any, ...]]]:
    rows: dict[str, list[tuple[Any, ...]]] = {
        k: []
        for k in (
            "rv_name",
            "rv_identifier",
            "rv_address",
            "rv_birth",
            "rv_nationality",
            "rv_listing",
            "rv_relationship",
            "rv_vessel",
            "rv_aircraft",
        )
    }
    for i, n in enumerate(d.get("names", [])):
        rows["rv_name"].append(
            (
                rv_id,
                i,
                n["name_type"],
                n["full_name"],
                json.dumps(n.get("parts") or {}),
                n.get("script"),
                n.get("language"),
                n.get("quality", "UNKNOWN"),
                n.get("raw_quality"),
                normalize_name(n["full_name"]),
            )
        )
    for i, x in enumerate(d.get("identifiers", [])):
        rows["rv_identifier"].append(
            (
                rv_id,
                i,
                x["id_type"],
                x.get("label"),
                x["value"],
                x["value_norm"],
                _iso2(x.get("country_iso2")),
                x.get("country_raw"),
                x.get("issued_on"),
                x.get("expires_on"),
                x.get("checksum_valid"),
            )
        )
    for i, a in enumerate(d.get("addresses", [])):
        rows["rv_address"].append(
            (
                rv_id,
                i,
                a.get("street"),
                a.get("city"),
                a.get("region"),
                a.get("postal_code"),
                _iso2(a.get("country_iso2")),
                a.get("country_raw"),
                a.get("full_raw"),
            )
        )
    k = 0
    for b in d.get("birth_dates", []):
        rows["rv_birth"].append(
            (
                rv_id,
                k,
                "DOB",
                b.get("date_value"),
                b.get("year"),
                b.get("year_from"),
                b.get("year_to"),
                b.get("precision"),
                None,
                None,
                b.get("raw"),
            )
        )
        k += 1
    for b in d.get("birth_places", []):
        rows["rv_birth"].append(
            (
                rv_id,
                k,
                "POB",
                None,
                None,
                None,
                None,
                None,
                b.get("place"),
                _iso2(b.get("country_iso2")),
                b.get("raw"),
            )
        )
        k += 1
    for i, c in enumerate(d.get("countries", [])):
        rows["rv_nationality"].append(
            (rv_id, i, c["kind"], _iso2(c.get("country_iso2")), c.get("country_raw"))
        )
    for i, lst in enumerate(d.get("listings", [])):
        rows["rv_listing"].append(
            (
                rv_id,
                i,
                lst["authority"],
                lst.get("list_name"),
                lst.get("program_code"),
                lst.get("listed_on"),
                lst.get("legal_basis"),
                lst.get("reference_no"),
                list(lst.get("measures") or []),
                lst.get("reason"),
                lst.get("remarks"),
                lst.get("evidence_url"),
            )
        )
    for i, r in enumerate(d.get("relationships", [])):
        rows["rv_relationship"].append(
            (rv_id, i, r["rel_type"], r.get("target_source_key"), r.get("target_name"), r.get("raw"))
        )
    v = d.get("vessel")
    if v:
        rows["rv_vessel"].append(
            (
                rv_id,
                v.get("vessel_type"),
                _iso2(v.get("flag_iso2")),
                v.get("flag_raw"),
                v.get("tonnage"),
                v.get("build_year"),
                v.get("owner_operator_raw"),
            )
        )
    a = d.get("aircraft")
    if a:
        rows["rv_aircraft"].append(
            (rv_id, a.get("model"), a.get("manufacturer"), a.get("operator_raw"), a.get("build_year"))
        )
    return rows


_COPY: dict[str, tuple[str, list[str]]] = {
    "rv_name": (
        "record_version_id, ord, name_type, full_name, name_parts, script, language, quality, raw_quality,"
        " normalized_name",
        ["int8", "int2", "text", "text", "text", "text", "text", "text", "text", "text"],
    ),
    "rv_identifier": (
        "record_version_id, ord, id_type, id_label, value_raw, value_norm, country_iso2, country_raw,"
        " issued_on, expires_on, checksum_valid",
        ["int8", "int2", "text", "text", "text", "text", "text", "text", "text", "text", "bool"],
    ),
    "rv_address": (
        "record_version_id, ord, street, city, region, postal_code, country_iso2, country_raw, full_raw",
        ["int8", "int2", "text", "text", "text", "text", "text", "text", "text"],
    ),
    "rv_birth": (
        "record_version_id, ord, kind, date_value, year, year_from, year_to, precision, place, country_iso2, raw",
        ["int8", "int2", "text", "date", "int4", "int4", "int4", "text", "text", "text", "text"],
    ),
    "rv_nationality": (
        "record_version_id, ord, kind, country_iso2, country_raw",
        ["int8", "int2", "text", "text", "text"],
    ),
    "rv_listing": (
        "record_version_id, ord, authority, list_name, program_code, listed_on, legal_basis, reference_no,"
        " measures, reason, remarks, evidence_url",
        ["int8", "int2", "text", "text", "text", "date", "text", "text", "text[]", "text", "text", "text"],
    ),
    "rv_relationship": (
        "record_version_id, ord, rel_type, target_source_key, target_name_raw, raw",
        ["int8", "int2", "text", "text", "text", "text"],
    ),
    "rv_vessel": (
        "record_version_id, vessel_type, flag_iso2, flag_raw, tonnage, build_year, owner_operator_raw",
        ["int8", "text", "text", "text", "text", "int4", "text"],
    ),
    "rv_aircraft": (
        "record_version_id, model, manufacturer, operator_raw, build_year",
        ["int8", "text", "text", "text", "int4"],
    ),
}


def copy_children(conn: psycopg.Connection[Any], all_rows: dict[str, list[tuple[Any, ...]]]) -> None:
    with conn.cursor() as cur:
        for table, rows in all_rows.items():
            if not rows:
                continue
            cols, types = _COPY[table]
            with cur.copy(f"COPY sanctions.{table} ({cols}) FROM STDIN") as cp:
                cp.set_types(types)
                for r in rows:
                    cp.write_row(r)


def field_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    fields = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
    out: dict[str, Any] = {"fields": fields}
    for key, attr in (("names", "full_name"), ("identifiers", "value_norm")):
        if key in fields:
            o = {x.get(attr) for x in old.get(key, [])}
            n = {x.get(attr) for x in new.get(key, [])}
            out[f"{key}_added"] = sorted(str(v) for v in n - o)[:20]
            out[f"{key}_removed"] = sorted(str(v) for v in o - n)[:20]
    return out


def diff_preview(conn: psycopg.Connection[Any], source_id: str, run_id: str) -> dict[str, int]:
    row = fetch_one(
        conn,
        """SELECT count(*) FILTER (WHERE c.source_key IS NULL) AS added,
                  count(*) FILTER (WHERE s.source_key IS NULL) AS removed,
                  count(*) FILTER (WHERE s.source_key IS NOT NULL AND c.source_key IS NOT NULL
                                   AND s.content_hash <> c.content_hash) AS changed,
                  count(*) FILTER (WHERE s.content_hash = c.content_hash) AS unchanged,
                  count(c.source_key) AS previous_count
           FROM (SELECT source_key, content_hash FROM sanctions.staging_record WHERE run_id = %s) s
           FULL OUTER JOIN (SELECT source_key, content_hash FROM sanctions.record_version
                            WHERE source_id = %s AND valid_to_seq IS NULL) c ON c.source_key = s.source_key""",
        (run_id, source_id),
    )
    assert row is not None
    return {k: int(v) for k, v in row.items()}


def create_snapshot(conn: psycopg.Connection[Any], *, trigger_version_id: int | None, note: str) -> int:
    held = [
        r["candidate_id"]
        for r in fetch_all(
            conn,
            "SELECT candidate_id FROM removal_candidate WHERE status = ANY(%s) ORDER BY candidate_id",
            (list(HOLDING_STATUSES),),
        )
    ]
    snap = int(
        fetch_val(
            conn,
            "INSERT INTO screening_snapshot (trigger_version_id, held_removal_ids, note)"
            " VALUES (%s, %s, %s) RETURNING snapshot_id",
            (trigger_version_id, held, note),
        )
    )
    conn.execute(
        "INSERT INTO snapshot_version (snapshot_id, source_id, version_id, seq)"
        " SELECT %s, source_id, version_id, seq FROM list_version WHERE status = 'PUBLISHED'",
        (snap,),
    )
    return snap


def refresh_crosslist(conn: psycopg.Connection[Any], source_id: str) -> int:
    conn.execute("DELETE FROM crosslist_key WHERE source_id = %s", (source_id,))
    cur = conn.execute(
        """INSERT INTO crosslist_key (key_type, key_value, source_id, source_key, record_version_id)
           SELECT DISTINCT
             CASE i.id_type WHEN 'REGISTRATION' THEN 'REG_COUNTRY' WHEN 'PASSPORT' THEN 'PASSPORT_COUNTRY' ELSE i.id_type END,
             CASE WHEN i.id_type IN ('REGISTRATION','PASSPORT') THEN i.value_norm || '|' || i.country_iso2 ELSE i.value_norm END,
             rv.source_id, rv.source_key, rv.record_version_id
           FROM record_version rv JOIN rv_identifier i USING (record_version_id)
           WHERE rv.source_id = %s AND rv.valid_to_seq IS NULL
             AND (i.id_type IN ('UN_REF','LEI','OFAC_UID')
                  OR (i.id_type = 'IMO' AND i.checksum_valid)
                  OR (i.id_type IN ('REGISTRATION','PASSPORT') AND i.country_iso2 IS NOT NULL))
           ON CONFLICT DO NOTHING""",
        (source_id,),
    )
    return cur.rowcount


def publish_version(
    conn: psycopg.Connection[Any],
    *,
    source_id: str,
    run_id: str,
    version_id: int,
    staging_run_id: str | None = None,
    released_by: str | None = None,
) -> dict[str, Any]:
    """Apply the diff between staging (run) and current published rows, then publish ``version_id``.

    Must be called inside a transaction. Locks the source row to serialise publishers.
    """
    staging_run = staging_run_id or run_id
    conn.execute("SELECT 1 FROM source WHERE source_id = %s FOR UPDATE", (source_id,))
    ver = fetch_one(conn, "SELECT * FROM list_version WHERE version_id = %s FOR UPDATE", (version_id,))
    if ver is None or ver["status"] not in ("CANDIDATE", "VALIDATED", "HELD"):
        raise RuntimeError(f"version {version_id} cannot be published from status {ver and ver['status']}")
    newer = fetch_val(
        conn,
        "SELECT max(seq) FROM list_version WHERE source_id = %s AND status IN ('PUBLISHED','SUPERSEDED')",
        (source_id,),
    )
    if newer is not None and newer > ver["seq"]:
        raise RuntimeError(
            f"a newer version (seq {newer}) is already published; refusing to publish seq {ver['seq']}"
        )
    seq = ver["seq"]

    conn.execute("DROP TABLE IF EXISTS _diff")
    conn.execute(
        """CREATE TEMP TABLE _diff ON COMMIT DROP AS
           SELECT coalesce(s.source_key, c.source_key) AS source_key,
                  CASE WHEN c.source_key IS NULL THEN 'ADD' WHEN s.source_key IS NULL THEN 'REMOVE'
                       WHEN s.content_hash <> c.content_hash THEN 'CHANGE' ELSE 'SAME' END AS change_type,
                  c.record_version_id AS old_rv_id,
                  coalesce(s.entity_type, c.entity_type) AS entity_type
           FROM (SELECT source_key, content_hash, entity_type FROM sanctions.staging_record WHERE run_id = %s) s
           FULL OUTER JOIN (SELECT source_key, content_hash, entity_type, record_version_id FROM sanctions.record_version
                            WHERE source_id = %s AND valid_to_seq IS NULL) c ON c.source_key = s.source_key""",
        (staging_run, source_id),
    )
    conn.execute(
        """UPDATE _diff d SET change_type = 'RELIST' WHERE change_type = 'ADD'
           AND EXISTS (SELECT 1 FROM sanctions.record_version rv WHERE rv.source_id = %s AND rv.source_key = d.source_key)""",
        (source_id,),
    )
    # close old rows (CHANGE, REMOVE)
    conn.execute(
        """UPDATE record_version SET valid_to_seq = %s WHERE record_version_id IN
             (SELECT old_rv_id FROM _diff WHERE change_type IN ('CHANGE','REMOVE'))""",
        (seq,),
    )
    new_rows = fetch_all(
        conn,
        """INSERT INTO record_version (source_id, source_key, entity_type, valid_from_seq, content_hash, primary_name, doc)
           SELECT %s, s.source_key, s.entity_type, %s, s.content_hash, s.primary_name, s.doc
           FROM staging_record s JOIN _diff d ON d.source_key = s.source_key
           WHERE s.run_id = %s AND d.change_type IN ('ADD','CHANGE','RELIST')
           RETURNING record_version_id, source_key, doc""",
        (source_id, seq, staging_run),
    )
    all_children: dict[str, list[tuple[Any, ...]]] = {k: [] for k in _COPY}
    new_by_key: dict[str, int] = {}
    for r in new_rows:
        new_by_key[r["source_key"]] = r["record_version_id"]
        for t, rows in child_rows(r["record_version_id"], r["doc"]).items():
            all_children[t].extend(rows)
    copy_children(conn, all_children)

    diff_rows = fetch_all(conn, "SELECT * FROM _diff WHERE change_type <> 'SAME'")
    changed_old_ids = [d["old_rv_id"] for d in diff_rows if d["change_type"] == "CHANGE"]
    old_docs = (
        {
            r["record_version_id"]: r["doc"]
            for r in fetch_all(
                conn,
                "SELECT record_version_id, doc FROM record_version WHERE record_version_id = ANY(%s)",
                (changed_old_ids,),
            )
        }
        if changed_old_ids
        else {}
    )
    new_docs = {r["source_key"]: r["doc"] for r in new_rows}
    events: list[tuple[Any, ...]] = []
    counts = {"ADD": 0, "CHANGE": 0, "REMOVE": 0, "RELIST": 0}
    for d in diff_rows:
        ct = d["change_type"]
        counts[ct] += 1
        fd = (
            field_diff(old_docs.get(d["old_rv_id"], {}), new_docs.get(d["source_key"], {}))
            if ct == "CHANGE"
            else None
        )
        events.append(
            (
                source_id,
                version_id,
                d["source_key"],
                ct,
                d["entity_type"],
                d["old_rv_id"],
                new_by_key.get(d["source_key"]),
                jsonb(fd) if fd else None,
            )
        )
    if events:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO change_event (source_id, version_id, source_key, change_type, entity_type,"
                " old_record_version_id, new_record_version_id, field_diff) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                events,
            )
    # removals keep blocking until confirmed (BRD 11 / FR-10)
    conn.execute(
        """INSERT INTO removal_candidate (source_id, source_key, last_record_version_id, detected_in_version_id)
           SELECT %s, source_key, old_rv_id, %s FROM _diff WHERE change_type = 'REMOVE'
           ON CONFLICT DO NOTHING""",
        (source_id, version_id),
    )
    relisted = conn.execute(
        """UPDATE removal_candidate SET status = 'RELISTED', decided_by = 'system', decided_at = now(),
               rationale = 'record reappeared in version ' || %s::text
           WHERE source_id = %s AND status = ANY(%s)
             AND source_key IN (SELECT source_key FROM _diff WHERE change_type IN ('RELIST','ADD'))""",
        (version_id, source_id, list(HOLDING_STATUSES)),
    ).rowcount
    xl = refresh_crosslist(conn, source_id)

    summary = {
        "added": counts["ADD"],
        "changed": counts["CHANGE"],
        "removed": counts["REMOVE"],
        "relisted": counts["RELIST"],
        "removal_candidates_relisted": relisted,
        "crosslist_keys": xl,
    }
    conn.execute(
        "UPDATE list_version SET status = 'SUPERSEDED' WHERE source_id = %s AND status = 'PUBLISHED'",
        (source_id,),
    )
    conn.execute(
        """UPDATE list_version SET status = 'SUPERSEDED', validation_report = validation_report || %s
           WHERE source_id = %s AND status = 'HELD' AND seq < %s""",
        (jsonb({"superseded_by_seq": seq}), source_id, seq),
    )
    conn.execute(
        """UPDATE proposed_change SET status = 'EXPIRED', review_comment = 'superseded by a newer published version'
           WHERE kind = 'LARGE_CHANGE_RELEASE' AND source_id = %s AND status = 'PENDING'
             AND (payload->>'version_id')::bigint <> %s""",
        (source_id, version_id),
    )
    conn.execute(
        "UPDATE list_version SET status = 'PUBLISHED', published_at = now(), change_summary = %s, released_by = %s"
        " WHERE version_id = %s",
        (jsonb(summary), released_by, version_id),
    )
    snap = create_snapshot(conn, trigger_version_id=version_id, note=f"{source_id} seq {seq} published")
    summary["snapshot_id"] = snap
    conn.execute(
        """UPDATE source SET current_version_id = %s, last_change_at = now(), last_success_at = now(),
               consecutive_failures = 0, breaker_state = 'CLOSED', breaker_opened_at = NULL, breaker_retry_at = NULL
           WHERE source_id = %s""",
        (version_id, source_id),
    )
    return summary


def purge_staging(conn: psycopg.Connection[Any], run_ids: Iterable[str]) -> int:
    ids = list(run_ids)
    if not ids:
        return 0
    conn.execute("DELETE FROM staging_path_stats WHERE run_id = ANY(%s::uuid[])", (ids,))
    return conn.execute("DELETE FROM staging_record WHERE run_id = ANY(%s::uuid[])", (ids,)).rowcount
