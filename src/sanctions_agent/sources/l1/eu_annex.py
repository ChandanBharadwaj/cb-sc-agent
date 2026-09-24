"""EU Regulation 833/2014 annexes as curated Level-1 lists.

Annex XLII (banned vessels - 673 after the 21st package) and Annex IV (export-restricted entities) are
not in the EU FSF; they exist only in the Official Journal (and, non-authoritatively, the Sanctions Map).

Flow (FR-03, FR-20, BRD 18):
1. an OJ act is detected (``eurlex_oj`` notice feed) or an analyst supplies a CSV of the annex
2. entries are extracted - by the verified LLM extractor or deterministically from the CSV
3. an ANNEX_ENTRY proposal is reviewed by a human; only verified entries are applied
4. approved entries live in ``curated_entry``; each build exports them to a deterministic JSON artifact
   that goes through the normal pipeline (archive -> validate -> diff -> publish), so versions,
   change events, removals and snapshots work exactly like every other list
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg

from sanctions_agent.canonical.model import CanonicalRecord, Country, Listing, Name, VesselInfo
from sanctions_agent.canonical.normalize.countries import to_iso2
from sanctions_agent.canonical.normalize.dates import parse_date
from sanctions_agent.canonical.normalize.identifiers import extract_imo, imo_checksum_ok, make_identifier
from sanctions_agent.canonical.normalize.names import normalize_name
from sanctions_agent.db.engine import fetch_all, fetch_one, fetch_val, jsonb, tx
from sanctions_agent.logs import get_logger
from sanctions_agent.pipeline import runs
from sanctions_agent.review import add_proposal
from sanctions_agent.sources.base import FieldDoc, FileInfo, Issue, ListAdapter, ParseStats
from sanctions_agent.sources.registry import SourceSpec

log = get_logger(__name__)


def entry_key(annex: str, entry: dict[str, Any]) -> str:
    if annex == "XLII":
        imo = extract_imo(str(entry.get("imo") or ""))
        if not imo:
            raise ValueError(f"vessel entry without IMO: {entry.get('name')}")
        return imo
    return normalize_name(str(entry["name"]))


class EuAnnexAdapter(ListAdapter):
    type_id = "eu_annex_curated"
    file_kind = "json"
    authority = "EU"
    field_docs = [
        FieldDoc("*.source_key", "entry_key", "IMO number (Annex XLII) or normalised name (Annex IV)"),
        FieldDoc("VESSEL.imo", "entries[].imo", "IMO on every row of the annex (checksum validated)"),
        FieldDoc("*.name", "entries[].name", "Name exactly as in the Official Journal"),
        FieldDoc("*.legal_evidence", "entries[].act.url", "OJ act that added the entry (FR-14)"),
        FieldDoc("*.measures", "(annex)", "XLII: PORT_BAN (+ services ban); IV: EXPORT_RESTRICTION"),
    ]

    def validate_file(self, path: Path) -> list[Issue]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            assert isinstance(data.get("entries"), list)
        except (ValueError, AssertionError) as e:
            return [Issue("SCHEMA_INVALID", "FAIL", f"curated artifact invalid: {e}")]
        return []

    def read_info(self, path: Path) -> FileInfo:
        data = json.loads(path.read_text(encoding="utf-8"))
        return FileInfo(
            publication_marker=f"curated as of change {data.get('as_of_change_id')}",
            extra={"annex": data.get("annex")},
        )

    def parse(self, path: Path, stats: ParseStats) -> Iterator[CanonicalRecord]:
        data = json.loads(path.read_text(encoding="utf-8"))
        annex = data["annex"]
        stats.paths["/entries"] += 1
        for e in data["entries"]:
            doc = e["doc"]
            for k in doc:
                stats.paths[f"/entries/doc/{k}"] += 1
            act = doc.get("act") or {}
            names = [Name(name_type="PRIMARY", full_name=doc["name"], quality="STRONG")]
            names += [Name(name_type="AKA", full_name=a, quality="UNKNOWN") for a in doc.get("aliases") or []]
            identifiers = []
            if doc.get("imo"):
                ident = make_identifier("IMO number", str(doc["imo"]), id_type="IMO")
                if ident:
                    identifiers.append(ident)
            flag = doc.get("flag")
            listing = Listing(
                authority="EU",
                list_name=f"Reg. 833/2014 Annex {annex}",
                program_code=f"833/2014-{annex}",
                listed_on=parse_date(act.get("published_on")),
                legal_basis=act.get("title") or act.get("celex"),
                reference_no=act.get("celex"),
                measures=["PORT_BAN", "OTHER"] if annex == "XLII" else ["EXPORT_RESTRICTION"],
                evidence_url=act.get("url"),
            )
            stats.records += 1
            yield CanonicalRecord(
                source_key=e["entry_key"],
                entity_type="VESSEL" if annex == "XLII" else "ORGANIZATION",
                names=names,
                identifiers=identifiers,
                listings=[listing],
                countries=[
                    Country(
                        kind="COUNTRY",
                        country_iso2=to_iso2(doc.get("country")),
                        country_raw=doc.get("country"),
                    )
                ]
                if doc.get("country")
                else [],
                vessel=VesselInfo(flag_raw=flag, flag_iso2=to_iso2(flag)) if annex == "XLII" else None,
                extra={"approved_change_id": e.get("approved_change_id")},
            )


def build_curated_artifact(src: SourceSpec, work: Path) -> tuple[Path, dict[str, Any]]:
    annex = src.config.annex  # type: ignore[attr-defined]
    with tx() as conn:
        rows = fetch_all(
            conn,
            "SELECT entry_key, doc, approved_change_id FROM curated_entry WHERE source_id = %s"
            " AND active ORDER BY entry_key",
            (src.source_id,),
        )
        as_of = fetch_val(
            conn, "SELECT max(approved_change_id) FROM curated_entry WHERE source_id = %s", (src.source_id,)
        )
    info: dict[str, Any] = {"entries": len(rows), "as_of_change_id": as_of}
    lead_cfg = getattr(src.config, "sanctions_map", None)
    if lead_cfg is not None:
        try:
            info["sanctions_map"] = sanctions_map_leads(src, {r["entry_key"] for r in rows})
        except Exception as e:  # leads are best-effort
            info["sanctions_map_error"] = str(e)[:300]
    payload = {
        "annex": annex,
        "as_of_change_id": as_of,
        "entries": [
            {"entry_key": r["entry_key"], "doc": r["doc"], "approved_change_id": r["approved_change_id"]}
            for r in rows
        ],
    }
    path = work / "curated.json"
    path.write_text(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    return path, info


def sanctions_map_leads(src: SourceSpec, curated_imos: set[str]) -> dict[str, Any]:
    """Compare IMO numbers on the (non-authoritative) EU Sanctions Map export with the curated annex and
    raise a lead proposal for a human when they differ. Never adds entries by itself."""
    from sanctions_agent.http.client import HttpFetcher
    from sanctions_agent.http.guard import UrlGuard

    cfg = src.config.sanctions_map  # type: ignore[attr-defined]
    with HttpFetcher(UrlGuard(cfg.allowed_hosts)) as f:
        body, res = f.get(cfg.url, max_bytes=cfg.max_mb * 1024 * 1024)
    found = {
        m for m in re.findall(r"(?<!\d)(\d{7})(?!\d)", body.decode("utf-8", "replace")) if imo_checksum_ok(m)
    }
    missing, extra = sorted(found - curated_imos), sorted(curated_imos - found)
    if missing or extra:
        with tx() as conn:
            add_proposal(
                conn,
                kind="ANNEX_ENTRY",
                source_id=src.source_id,
                subject_ref="sanctions-map-lead",
                title=f"Sanctions Map lists {len(missing)} IMO(s) not in curated {src.source_id}; {len(extra)} curated not on map",
                payload={
                    "operation": "LEAD",
                    "missing_imos": missing[:500],
                    "extra_imos": extra[:500],
                    "map_url": res.final_url,
                },
                proposed_by="system:sanctions-map",
                evidence_urls=[res.final_url],
                rationale="Lead only: confirm each IMO against the Official Journal before adding.",
                dedupe_key=f"sanctions-map:{src.source_id}:{len(missing)}:{len(extra)}",
            )
    return {"map_imos": len(found), "missing_from_curated": len(missing), "not_on_map": len(extra)}


# ---------------------------------------------------------------------------------------------
def propose_entries(
    conn: psycopg.Connection[Any],
    *,
    source_id: str,
    operation: str,
    entries: list[dict[str, Any]],
    act: dict[str, Any],
    proposed_by: str,
    agent_cycle_id: str | None = None,
    count_check: dict[str, Any] | None = None,
) -> int | None:
    annex = fetch_val(conn, "SELECT config->>'annex' FROM source WHERE source_id = %s", (source_id,))
    ok = [e for e in entries if e.get("ok")]
    bad = [e for e in entries if not e.get("ok")]
    title = (
        f"{operation} {len(ok)} verified entr{'y' if len(ok) == 1 else 'ies'} to Annex {annex}"
        f" from {act.get('celex') or act.get('title') or 'act'}"
        + (f" ({len(bad)} failed verification)" if bad else "")
    )
    return add_proposal(
        conn,
        kind="ANNEX_ENTRY",
        source_id=source_id,
        subject_ref=act.get("celex") or act.get("url"),
        title=title,
        payload={
            "operation": operation,
            "annex": annex,
            "act": act,
            "entries": entries,
            "count_check": count_check or {},
        },
        proposed_by=proposed_by,
        agent_cycle_id=agent_cycle_id,
        evidence_urls=[act["url"]] if act.get("url") else [],
        verbatim_quotes=[str(e["verbatim_quote"]) for e in ok if e.get("verbatim_quote")],
        verification={"verified": len(ok), "failed": len(bad), "count_check": count_check or {}},
        rationale="Entries extracted from the Official Journal; only verified entries are applied on approval.",
        dedupe_key=f"annex:{source_id}:{operation}:{act.get('celex') or act.get('url')}",
    )


def apply_annex_proposal(conn: psycopg.Connection[Any], ch: dict[str, Any], reviewer: str) -> dict[str, Any]:
    p = ch["payload"]
    source_id = ch["source_id"]
    if p.get("operation") == "LEAD":
        return {"applied": 0, "note": "lead acknowledged; add entries from the Official Journal"}
    annex = p["annex"]
    applied = 0
    for e in p.get("entries", []):
        if not e.get("ok"):
            continue
        key = entry_key(annex, e)
        conn.execute(
            "UPDATE curated_entry SET active = false, deactivated_at = now()"
            " WHERE source_id = %s AND entry_key = %s AND active",
            (source_id, key),
        )
        if p["operation"] in ("ADD", "REPLACE"):
            doc = {k: e.get(k) for k in ("name", "imo", "flag", "country", "aliases", "address") if e.get(k)}
            doc["act"] = p.get("act") or {}
            conn.execute(
                "INSERT INTO curated_entry (source_id, entry_key, doc, approved_change_id) VALUES (%s,%s,%s,%s)",
                (source_id, key, jsonb(doc), ch["change_id"]),
            )
        applied += 1
    run_id = None
    try:
        run_id = runs.enqueue_run(
            conn,
            source_id=source_id,
            run_kind="LIST_INGEST",
            trigger="MANUAL",
            requested_by=reviewer,
            reason=f"annex proposal {ch['change_id']} approved",
        )
    except runs.RunAlreadyActive as e:
        run_id = e.run_id
    return {"applied": applied, "run_id": run_id}


def entries_from_csv(path: Path, annex: str) -> list[dict[str, Any]]:
    """Analyst-supplied CSV (columns: name, imo, flag, country, aliases) -> deterministically verified entries."""
    out = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            problems = []
            if not row.get("name"):
                problems.append("missing name")
            if annex == "XLII":
                imo = extract_imo(row.get("imo", ""))
                if not imo or not imo_checksum_ok(imo):
                    problems.append(f"IMO {row.get('imo')!r} missing or fails checksum")
                row["imo"] = imo or row.get("imo")
            out.append(
                {
                    "name": row.get("name"),
                    "imo": row.get("imo") or None,
                    "flag": row.get("flag") or None,
                    "country": row.get("country") or None,
                    "aliases": [a.strip() for a in (row.get("aliases") or "").split(";") if a.strip()],
                    "verbatim_quote": None,
                    "ok": not problems,
                    "problems": problems,
                }
            )
    return out


def entries_from_extraction(extraction: Any, verification: Any) -> list[dict[str, Any]]:
    out = []
    for ve in verification.entries:
        e = ve.entry
        imo = next((i.value for i in e.identifiers if i.id_type == "IMO"), None)
        out.append(
            {
                "name": e.name,
                "imo": extract_imo(imo) if imo else None,
                "flag": None,
                "country": None,
                "aliases": [],
                "verbatim_quote": e.verbatim_quote,
                "action": e.action,
                "ok": ve.ok,
                "problems": ve.problems,
            }
        )
    return out


def act_from_notice(conn: psycopg.Connection[Any], notice_id: int) -> dict[str, Any]:
    n = fetch_one(conn, "SELECT * FROM legal_notice WHERE notice_id = %s", (notice_id,))
    if n is None:
        raise KeyError(notice_id)
    celex = re.search(r"CELEX[:%3A]*([0-9A-Z]+)", n["url"] or "", re.I)
    return {
        "notice_id": notice_id,
        "title": n["title"],
        "url": n["url"],
        "published_on": str(n["published_on"]) if n["published_on"] else None,
        "celex": celex.group(1) if celex else n["external_id"],
    }
