"""Regenerate schemas/known_paths/<adapter>.txt from sample files.

Usage:  uv run python scripts/gen_known_paths.py <adapter_type> <sample file> [<sample file> ...]

Known paths are the element paths a parser has been reviewed against. New paths in a live file are
reported as SCHEMA_DRIFT (warn by default) so a human can decide whether the parser must change.
Run this against *live* files (``sanctions-agent verify-sources --write-known-paths`` does the same)
and review the diff in a pull request.
"""

from __future__ import annotations

import sys
from pathlib import Path

from sanctions_agent.sources.adapter_types import get_adapter_type
from sanctions_agent.sources.base import ParseStats

# Optional elements documented by publishers but absent from our fixtures.
EXTRAS: dict[str, list[str]] = {
    "un_consolidated_xml": [
        f"/CONSOLIDATED_LIST/INDIVIDUALS/INDIVIDUAL/{p}"
        for p in (
            "FOURTH_NAME",
            "GENDER",
            "SUBMITTED_BY",
            "NAME_ORIGINAL_SCRIPT",
            "TITLE",
            "TITLE/VALUE",
            "INDIVIDUAL_ALIAS/DATE_OF_BIRTH",
            "INDIVIDUAL_ALIAS/CITY_OF_BIRTH",
            "INDIVIDUAL_ALIAS/COUNTRY_OF_BIRTH",
            "INDIVIDUAL_ALIAS/NOTE",
            "INDIVIDUAL_ADDRESS/STREET",
            "INDIVIDUAL_ADDRESS/STATE_PROVINCE",
            "INDIVIDUAL_ADDRESS/ZIP_CODE",
            "INDIVIDUAL_ADDRESS/NOTE",
            "INDIVIDUAL_DATE_OF_BIRTH/NOTE",
            "INDIVIDUAL_PLACE_OF_BIRTH/NOTE",
            "INDIVIDUAL_DOCUMENT/TYPE_OF_DOCUMENT2",
            "INDIVIDUAL_DOCUMENT/DATE_OF_ISSUE",
            "INDIVIDUAL_DOCUMENT/CITY_OF_ISSUE",
            "INDIVIDUAL_DOCUMENT/COUNTRY_OF_ISSUE",
            "INDIVIDUAL_DOCUMENT/NOTE",
            "NATIONALITY2",
            "COMMENTS1",
            "DESIGNATION",
            "DESIGNATION/VALUE",
        )
    ]
    + [
        f"/CONSOLIDATED_LIST/ENTITIES/ENTITY/{p}"
        for p in (
            "SUBMITTED_BY",
            "NAME_ORIGINAL_SCRIPT",
            "SORT_KEY_LAST_MOD",
            "ENTITY_ALIAS/NOTE",
            "ENTITY_ADDRESS/STATE_PROVINCE",
            "ENTITY_ADDRESS/ZIP_CODE",
            "ENTITY_ADDRESS/NOTE",
        )
    ],
}


def main() -> None:
    type_id, files = sys.argv[1], [Path(f) for f in sys.argv[2:]]
    at = get_adapter_type(type_id)
    adapter = at.load()()
    paths: set[str] = set(EXTRAS.get(type_id, []))
    for f in files:
        st = ParseStats()
        for _ in adapter.parse(f, st):
            pass
        paths |= set(st.paths)
    out = Path(__file__).resolve().parents[1] / "schemas" / "known_paths" / f"{type_id}.txt"
    header = f"# Known element paths for {type_id} (reviewed). Regenerate with scripts/gen_known_paths.py\n"
    out.write_text(header + "\n".join(sorted(paths)) + "\n", encoding="utf-8")
    print(f"wrote {len(paths)} paths to {out}")


if __name__ == "__main__":
    main()
