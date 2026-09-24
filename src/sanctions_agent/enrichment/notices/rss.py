"""Hardened RSS/Atom parsing shared by EUR-Lex and the EU FSF signal feed."""

from __future__ import annotations

from datetime import date
from email.utils import parsedate_to_datetime

from lxml import etree

from sanctions_agent.canonical.normalize.dates import parse_date

_P = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)


def parse_feed(body: bytes) -> list[dict[str, object]]:
    root = etree.fromstring(body, _P)
    items = []
    for it in root.iter():
        name = etree.QName(it).localname if isinstance(it.tag, str) else ""
        if name not in ("item", "entry"):
            continue
        rec: dict[str, object] = {}
        for c in it:
            if not isinstance(c.tag, str):
                continue
            ln = etree.QName(c).localname
            if ln == "link":
                rec["link"] = c.get("href") or (c.text or "").strip()
            elif ln in ("title", "description", "summary", "guid", "id", "pubDate", "updated", "published"):
                rec[ln] = (c.text or "").strip()
        items.append(rec)
    return items


def item_date(rec: dict[str, object]) -> date | None:
    for k in ("pubDate", "published", "updated"):
        v = rec.get(k)
        if not v:
            continue
        try:
            return parsedate_to_datetime(str(v)).date()
        except (TypeError, ValueError):
            d = parse_date(str(v)[:10])
            if d:
                return d
    return None
