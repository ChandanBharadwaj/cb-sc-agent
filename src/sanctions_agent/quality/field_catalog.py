"""Data dictionary (``field_catalog``), generated from each adapter's declared field mapping plus the
source's configured fill-rate floors, so it always matches what the parsers actually do."""

from __future__ import annotations

from typing import Any

import psycopg

from sanctions_agent.db.engine import fetch_all
from sanctions_agent.sources.adapter_types import get_adapter_type

ALL_TYPES = ["PERSON", "ORGANIZATION", "VESSEL", "AIRCRAFT"]

# Fields that only exist on some lists (BRD section 10) - surfaced so the Q&A agent can answer
# "which sources carry X?" from the catalogue.
NOTES = {
    "VESSEL.mmsi": "Only OFAC carries MMSI (about 27% of OFAC vessels, BRD 10.2).",
    "VESSEL.call_sign": "Only OFAC carries call signs (about 38%).",
    "ORGANIZATION.lei": "LEI is almost never on lists (1.7% OFAC, 0% UN/EU/UK); GLEIF enrichment fills the gap.",
    "*.measures": "UN entries do not state measures; EU FSF is asset freezes only.",
}


def sync_field_catalog(conn: psycopg.Connection[Any]) -> int:
    n = 0
    for s in fetch_all(conn, "SELECT source_id, adapter_type, config FROM source"):
        at = get_adapter_type(s["adapter_type"])
        cls = at.load()
        docs = getattr(cls, "field_docs", []) or []
        floors = ((s["config"] or {}).get("validation") or {}).get("fill_floors") or {}
        conn.execute("DELETE FROM field_catalog WHERE source_id = %s", (s["source_id"],))
        for d in docs:
            prefix = d.canonical_field.partition(".")[0]
            types = ALL_TYPES if prefix == "*" else [prefix]
            floor = floors.get(d.canonical_field) if prefix != "*" else None
            notes = " ".join(x for x in (d.notes, NOTES.get(d.canonical_field)) if x) or None
            conn.execute(
                """INSERT INTO field_catalog (source_id, canonical_field, entity_types, source_path, description,
                       fill_floor, notes) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (s["source_id"], d.canonical_field, types, d.source_path, d.description, floor, notes),
            )
            n += 1
    return n
