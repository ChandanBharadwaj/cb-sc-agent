"""Country names/codes -> ISO 3166-1 alpha-2 (FR-13). Raw values are always kept alongside.

Exact matching only (code, name, official/common name, curated alias table). Fuzzy matching is
deliberately not used: a wrong country is worse than an unmapped one, and unmapped values are counted
as a data-quality metric (UNMAPPED_COUNTRY) so they can be added to the alias table.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

import pycountry

# Variants seen on sanctions lists (UN, UK, EU, OFAC, CSL) that pycountry does not match exactly.
_ALIASES: dict[str, str] = {
    "iran": "IR",
    "iran islamic republic of": "IR",
    "islamic republic of iran": "IR",
    "iran (islamic republic of)": "IR",
    "russia": "RU",
    "russian federation": "RU",
    "north korea": "KP",
    "korea north": "KP",
    "dprk": "KP",
    "democratic people's republic of korea": "KP",
    "korea democratic people's republic of": "KP",
    "korea, democratic people's republic of": "KP",
    "south korea": "KR",
    "korea south": "KR",
    "republic of korea": "KR",
    "korea republic of": "KR",
    "syria": "SY",
    "syrian arab republic": "SY",
    "turkey": "TR",
    "türkiye": "TR",
    "turkiye": "TR",
    "burma": "MM",
    "myanmar": "MM",
    "myanmar (burma)": "MM",
    "venezuela": "VE",
    "venezuela bolivarian republic of": "VE",
    "venezuela (bolivarian republic of)": "VE",
    "bolivia": "BO",
    "bolivia (plurinational state of)": "BO",
    "united kingdom": "GB",
    "uk": "GB",
    "great britain": "GB",
    "britain": "GB",
    "united kingdom of great britain and northern ireland": "GB",
    "england": "GB",
    "scotland": "GB",
    "united states": "US",
    "usa": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "united states of america": "US",
    "viet nam": "VN",
    "vietnam": "VN",
    "laos": "LA",
    "lao people's democratic republic": "LA",
    "lao pdr": "LA",
    "democratic republic of the congo": "CD",
    "congo democratic republic of the": "CD",
    "drc": "CD",
    "congo (democratic republic)": "CD",
    "congo, democratic republic of the": "CD",
    "republic of the congo": "CG",
    "congo": "CG",
    "congo republic of the": "CG",
    "cote d'ivoire": "CI",
    "côte d'ivoire": "CI",
    "ivory coast": "CI",
    "palestine": "PS",
    "palestinian territory": "PS",
    "occupied palestinian territory": "PS",
    "state of palestine": "PS",
    "west bank": "PS",
    "gaza": "PS",
    "gaza strip": "PS",
    "kosovo": "XK",
    "crimea": "UA",
    "crimea region of ukraine": "UA",
    "moldova": "MD",
    "republic of moldova": "MD",
    "moldova republic of": "MD",
    "tanzania": "TZ",
    "united republic of tanzania": "TZ",
    "czech republic": "CZ",
    "czechia": "CZ",
    "macedonia": "MK",
    "north macedonia": "MK",
    "the former yugoslav republic of macedonia": "MK",
    "brunei": "BN",
    "brunei darussalam": "BN",
    "cape verde": "CV",
    "cabo verde": "CV",
    "eswatini": "SZ",
    "swaziland": "SZ",
    "hong kong": "HK",
    "hong kong sar": "HK",
    "hong kong, china": "HK",
    "macau": "MO",
    "macao": "MO",
    "taiwan": "TW",
    "taiwan province of china": "TW",
    "chinese taipei": "TW",
    "vatican": "VA",
    "holy see": "VA",
    "micronesia": "FM",
    "micronesia federated states of": "FM",
    "st kitts and nevis": "KN",
    "saint kitts and nevis": "KN",
    "st vincent and the grenadines": "VC",
    "saint vincent and the grenadines": "VC",
    "st lucia": "LC",
    "saint lucia": "LC",
    "the bahamas": "BS",
    "bahamas": "BS",
    "the gambia": "GM",
    "gambia": "GM",
    "uae": "AE",
    "united arab emirates": "AE",
    "curacao": "CW",
    "curaçao": "CW",
    "sudan": "SD",
    "south sudan": "SS",
    "libya": "LY",
    "libyan arab jamahiriya": "LY",
    "yemen": "YE",
    "afghanistan": "AF",
    "belarus": "BY",
    "byelorussia": "BY",
    "timor-leste": "TL",
    "east timor": "TL",
}

_ISO3_TO_2 = {c.alpha_3: c.alpha_2 for c in pycountry.countries}
_ISO2 = {c.alpha_2 for c in pycountry.countries} | {"XK"}


def _key(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" .;")
    s = re.sub(r"^the ", "", s)
    return s


@lru_cache(maxsize=4096)
def _index() -> dict[str, str]:
    idx: dict[str, str] = {}
    for c in pycountry.countries:
        for attr in ("name", "official_name", "common_name"):
            v = getattr(c, attr, None)
            if v:
                idx[_key(v)] = c.alpha_2
    for k, v in _ALIASES.items():
        idx[_key(k)] = v
        idx[_key(k.replace(",", ""))] = v
    return idx


@lru_cache(maxsize=8192)
def to_iso2(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    if len(s) == 2 and s.upper() in _ISO2:
        return s.upper()
    if len(s) == 3 and s.upper() in _ISO3_TO_2:
        return _ISO3_TO_2[s.upper()]
    k = _key(s)
    idx = _index()
    if k in idx:
        return idx[k]
    k2 = k.replace(",", "")
    if k2 in idx:
        return idx[k2]
    # "Iran (Islamic Republic of)" / "Tehran, Iran" style values: try the parenthesised / last segment
    m = re.match(r"^(.*?)\s*\((.*)\)$", k)
    if m and (m.group(1) in idx):
        return idx[m.group(1)]
    last = k.split(",")[-1].strip()
    if last and last != k and last in idx:
        return idx[last]
    return None
