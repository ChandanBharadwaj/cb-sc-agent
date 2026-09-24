"""Partial / approximate date parsing for birth dates and listing dates (FR-13).

Lists express dates many ways: full ISO dates, ``dd/mm/yyyy`` with ``dd``/``00`` placeholders (UK),
year only, "circa", ranges ("Between 1965 and 1969", "1965 to 1969"), and OFAC's "01 Jan 1970". The
parser records *precision* instead of inventing missing parts, and returns the raw value unchanged.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from sanctions_agent.canonical.model import BirthDate

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
    )
}
_MONTHS.update(
    {
        "sept": 9,
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
)

_RANGE = re.compile(r"(?:between\s+)?(\d{4})\s*(?:-|–|to|and)\s*(\d{4})", re.I)
_YEAR_ONLY = re.compile(r"^(?:c(?:irca|a)?\.?\s*|approx(?:imately)?\.?\s*|about\s+)?(\d{4})$", re.I)
_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:T.*)?$")
_ISO_YM = re.compile(r"^(\d{4})-(\d{1,2})$")
_DMY = re.compile(r"^(\d{1,2}|dd|DD|00)[/.\-](\d{1,2}|mm|MM|00)[/.\-](\d{4})$")
_MDY_TEXT = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})$")
_MY_TEXT = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{4})$")


def _month(token: str) -> int | None:
    t = token.lower().rstrip(".")
    return _MONTHS.get(t) or _MONTHS.get(t[:3])


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_birth_date(raw: str | None) -> BirthDate | None:
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    s_clean = re.sub(r"\s+", " ", s)

    m = _ISO.match(s_clean)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if mo == 0:
            return BirthDate(year=y, precision="YEAR", raw=s)
        if d == 0:
            return BirthDate(year=y, date_value=_safe_date(y, mo, 1), precision="MONTH", raw=s)
        dv = _safe_date(y, mo, d)
        return BirthDate(date_value=dv, year=y, precision="DAY" if dv else "UNKNOWN", raw=s)
    m = _ISO_YM.match(s_clean)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return BirthDate(year=y, date_value=_safe_date(y, mo, 1), precision="MONTH", raw=s)
    m = _DMY.match(s_clean)
    if m:
        dd, mm, y = m.group(1), m.group(2), int(m.group(3))
        if not mm.isdigit() or int(mm) == 0:
            return BirthDate(year=y, precision="YEAR", raw=s)
        if not dd.isdigit() or int(dd) == 0:
            return BirthDate(year=y, date_value=_safe_date(y, int(mm), 1), precision="MONTH", raw=s)
        dv = _safe_date(y, int(mm), int(dd))
        return BirthDate(date_value=dv, year=y, precision="DAY" if dv else "UNKNOWN", raw=s)
    m = _MDY_TEXT.match(s_clean)
    if m and _month(m.group(2)):
        y = int(m.group(3))
        dv = _safe_date(y, _month(m.group(2)) or 1, int(m.group(1)))
        return BirthDate(date_value=dv, year=y, precision="DAY" if dv else "YEAR", raw=s)
    m = _MY_TEXT.match(s_clean)
    if m and _month(m.group(1)):
        y = int(m.group(2))
        return BirthDate(
            year=y, date_value=_safe_date(y, _month(m.group(1)) or 1, 1), precision="MONTH", raw=s
        )
    m = _YEAR_ONLY.match(s_clean)
    if m:
        return BirthDate(year=int(m.group(1)), precision="YEAR", raw=s)
    m = _RANGE.search(s_clean)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a <= b:
            return BirthDate(year_from=a, year_to=b, precision="RANGE", raw=s)
    return BirthDate(precision="UNKNOWN", raw=s)


def parse_date(raw: str | None) -> date | None:
    """Strict-ish parse for listing / update dates. Returns None when not a full date."""
    bd = parse_birth_date(raw)
    if bd and bd.precision == "DAY":
        return bd.date_value
    if raw:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%d-%b-%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(raw.strip()[:26], fmt).date()
            except ValueError:
                continue
    return None
