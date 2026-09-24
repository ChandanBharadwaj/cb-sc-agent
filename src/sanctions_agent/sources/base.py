"""Adapter framework for list sources.

Parsers are deterministic (BRD section 18: the LLM never parses the structured lists). They:

* stream with ``lxml.iterparse`` (hardened: no entities, no network, no DTD) and match on *local names*
  so namespace changes (OFAC, May 2024) do not break them
* record every element path they see so schema drift can be detected against ``known_paths``
* declare a field catalogue (source element -> canonical field) that feeds the data dictionary
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from pathlib import Path
from typing import IO, Any, ClassVar

from lxml import etree

from sanctions_agent.canonical.model import CanonicalRecord
from sanctions_agent.settings import get_settings

MAX_WARNINGS = 200


@dataclass(frozen=True)
class FieldDoc:
    canonical_field: str  # ENTITY_TYPE.field or "*.field"
    source_path: str
    description: str
    notes: str = ""


@dataclass
class FileInfo:
    publication_marker: str | None = None
    published_at: datetime | None = None
    extra: dict[str, Any] = dc_field(default_factory=dict)


@dataclass
class Issue:
    category: str  # matches dq_issue.category
    severity: str  # INFO | WARN | FAIL
    message: str
    count: int = 1
    field: str | None = None
    detail: dict[str, Any] = dc_field(default_factory=dict)


@dataclass
class ParseStats:
    paths: Counter[str] = dc_field(default_factory=Counter)
    warnings: Counter[str] = dc_field(default_factory=Counter)
    warning_samples: list[str] = dc_field(default_factory=list)
    unmapped_countries: Counter[str] = dc_field(default_factory=Counter)
    unparseable_dates: int = 0
    records: int = 0
    skipped_records: int = 0

    def warn(self, category: str, sample: str | None = None) -> None:
        self.warnings[category] += 1
        if sample and len(self.warning_samples) < MAX_WARNINGS:
            self.warning_samples.append(f"{category}: {sample[:200]}")

    def country(self, raw: str | None, iso: str | None) -> None:
        if raw and raw.strip() and not iso:
            self.unmapped_countries[raw.strip()[:80]] += 1


# ---------------------------------------------------------------------------------------------
# Hardened, namespace-agnostic XML helpers
# ---------------------------------------------------------------------------------------------
_PARSER_KW: dict[str, Any] = {
    "resolve_entities": False,
    "no_network": True,
    "load_dtd": False,
    "huge_tree": True,
    "remove_comments": True,
}


def local(el: etree._Element) -> str:
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1] if tag.startswith("{") else tag


def kids(el: etree._Element, name: str | None = None) -> Iterator[etree._Element]:
    for c in el:
        if isinstance(c.tag, str) and (name is None or local(c) == name):
            yield c


def kid(el: etree._Element, *names: str) -> etree._Element | None:
    """First direct child matching any of ``names`` (alternatives tolerate renamed elements)."""
    for c in el:
        if isinstance(c.tag, str) and local(c) in names:
            return c
    return None


def txt(el: etree._Element | None) -> str | None:
    if el is None:
        return None
    t = "".join(el.itertext()).strip()
    return t or None


def kid_text(el: etree._Element, *names: str) -> str | None:
    return txt(kid(el, *names))


def descendants(el: etree._Element, name: str) -> Iterator[etree._Element]:
    for d in el.iter():
        if isinstance(d.tag, str) and local(d) == name:
            yield d


def record_paths(
    el: etree._Element, prefix: str, stats: ParseStats, depth: int = 0, max_depth: int = 8
) -> None:
    path = f"{prefix}/{local(el)}"
    stats.paths[path] += 1
    for a in el.attrib:
        stats.paths[f"{path}/@{a.rsplit('}', 1)[-1]}"] += 1
    if depth >= max_depth:
        return
    for c in el:
        if isinstance(c.tag, str):
            record_paths(c, path, stats, depth + 1, max_depth)


def iter_records(
    source: Path | str | IO[bytes],
    tags: Iterable[str],
    stats: ParseStats,
    root_path_prefix: str | None = None,
) -> Iterator[tuple[etree._Element, str]]:
    """Yield (element, parent_path) for every element whose local name is in ``tags``; clears as it goes."""
    tagset = set(tags)
    stack: list[str] = []
    ctx = etree.iterparse(source, events=("start", "end"), **_PARSER_KW)
    for event, el in ctx:
        name = local(el)
        if event == "start":
            stack.append(name)
            continue
        parent_path = "/" + "/".join(stack[:-1]) if len(stack) > 1 else ""
        stack.pop()
        if name in tagset:
            record_paths(el, parent_path if root_path_prefix is None else root_path_prefix, stats)
            yield el, parent_path
            el.clear(keep_tail=False)
            parent = el.getparent()
            if parent is not None:
                while el.getprevious() is not None:
                    del parent[0]
        elif len(stack) <= 2:
            # shallow structural elements (root children) are recorded without their (large) subtree
            stats.paths[f"{parent_path}/{name}"] += 1


def root_info(
    source: Path | IO[bytes], max_elements: int = 50000
) -> tuple[str, dict[str, str], dict[str, str]]:
    """Root local name, root attributes, and text of the first shallow leaf elements (for markers)."""
    root_name = ""
    attrs: dict[str, str] = {}
    shallow: dict[str, str] = {}
    depth = 0
    for n, (event, el) in enumerate(etree.iterparse(source, events=("start", "end"), **_PARSER_KW), start=1):
        if event == "start":
            depth += 1
            if depth == 1:
                root_name = local(el)
                attrs = {etree.QName(k).localname: v for k, v in el.attrib.items()}
        else:
            if depth <= 3 and len(el) == 0 and el.text and el.text.strip():
                shallow.setdefault(local(el), el.text.strip())
            if depth == 2 and len(shallow) > 20:
                break
            depth -= 1
        if n > max_elements:
            break
    return root_name, attrs, shallow


def check_well_formed(path: Path) -> Issue | None:
    try:
        for _ in etree.iterparse(str(path), events=("end",), **_PARSER_KW):
            pass
    except etree.XMLSyntaxError as e:
        return Issue("SCHEMA_INVALID", "FAIL", f"XML is not well-formed: {e}")
    return None


def validate_xsd(path: Path, xsd_rel: str | None) -> list[Issue]:
    """Validate against a pinned publisher XSD if one has been pinned (``sanctions-agent pin-schemas``)."""
    if not xsd_rel:
        return []
    xsd_path = get_settings().schemas_dir / xsd_rel
    if not xsd_path.exists():
        return [Issue("SCHEMA_INVALID", "INFO", f"schema {xsd_rel} not pinned yet; structural checks only")]
    try:
        schema = etree.XMLSchema(etree.parse(str(xsd_path), etree.XMLParser(**_PARSER_KW)))
    except (etree.XMLSchemaParseError, etree.XMLSyntaxError) as e:
        return [Issue("SCHEMA_INVALID", "WARN", f"pinned schema {xsd_rel} could not be loaded: {e}")]
    try:
        for _ in etree.iterparse(str(path), events=("end",), schema=schema, **_PARSER_KW):
            pass
    except etree.XMLSyntaxError as e:
        return [Issue("SCHEMA_INVALID", "FAIL", f"file does not validate against {xsd_rel}: {e}")]
    return []


def load_known_paths(type_id: str) -> frozenset[str]:
    p = get_settings().schemas_dir / "known_paths" / f"{type_id}.txt"
    if not p.exists():
        return frozenset()
    return frozenset(
        line.strip()
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )


def normalise_path(path: str) -> str:
    """Paths are compared namespace-free."""
    return path


# ---------------------------------------------------------------------------------------------
class ListAdapter(ABC):
    """Base for STRUCTURED_LIST / CURATED_LIST adapters."""

    type_id: ClassVar[str]
    file_kind: ClassVar[str] = "xml"
    required_paths: ClassVar[frozenset[str]] = frozenset()
    field_docs: ClassVar[list[FieldDoc]] = []
    authority: ClassVar[str] = ""

    def __init__(self, config: Any = None, source_id: str | None = None) -> None:
        self.config = config
        self.source_id = source_id

    @property
    def known_paths(self) -> frozenset[str]:
        return load_known_paths(self.type_id)

    def validate_file(self, path: Path) -> list[Issue]:
        if self.file_kind != "xml":
            return []
        bad = check_well_formed(path)
        if bad:
            return [bad]
        xsd = getattr(getattr(self.config, "validation", None), "schema_file", None)
        return validate_xsd(path, xsd)

    @abstractmethod
    def read_info(self, path: Path) -> FileInfo: ...

    @abstractmethod
    def parse(self, path: Path, stats: ParseStats) -> Iterator[CanonicalRecord]: ...
