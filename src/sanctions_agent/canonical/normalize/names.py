"""Name normalisation for indexing and cross-list comparison (never replaces the published name)."""

from __future__ import annotations

import re
import unicodedata

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Casefolded, diacritic-free, punctuation-free, single-spaced."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if unicodedata.category(ch) not in ("Mn", "Lm", "Sk"))
    s = s.casefold()
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


def join_name(*parts: str | None) -> str:
    return _WS.sub(" ", " ".join(p.strip() for p in parts if p and p.strip())).strip()


def clean(value: str | None) -> str | None:
    if value is None:
        return None
    v = _WS.sub(" ", value).strip()
    return v or None
