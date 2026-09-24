"""Typed configuration per adapter type.

These pydantic models validate every config change (seed import, UI edit, rollback) and produce the
JSON Schema the management UI renders as a form. Fields whose path contains a name in
``SENSITIVE_KEYS`` (URLs, hosts, headers) are security-sensitive (NFR-09): changing them requires a
second admin's approval (maker-checker).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SENSITIVE_KEYS = frozenset(
    {"url", "base_url", "allowed_hosts", "extra_headers", "rss_url", "download_url", "sparql_url"}
)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetryConfig(_Base):
    max_attempts: int = Field(4, ge=1, le=12, description="Attempts per run for retryable errors")
    base_delay_s: float = Field(10.0, ge=0, le=3600, description="First backoff delay (doubles each attempt)")
    max_delay_s: float = Field(600.0, ge=0, le=7200, description="Cap on a single backoff delay")


AlertRoute = Literal["log", "webhook", "email"]


class AlertConfig(_Base):
    routes: list[AlertRoute] = Field(default_factory=lambda: list[AlertRoute](["log", "webhook"]))
    min_severity: Literal["INFO", "WARN", "PAGE"] = "WARN"


class ValidationConfig(_Base):
    min_records: int = Field(1, ge=0, description="Fewer parsed records than this quarantines the file")
    max_removed_pct: float = Field(
        5.0, ge=0, le=100, description="Removals above this % of the last version HOLD"
    )
    max_removed_abs: int = Field(50, ge=0, description="...and above this absolute number")
    max_added_pct: float = Field(30.0, ge=0, description="Additions above this % of the last version HOLD")
    max_added_abs: int = Field(1500, ge=0, description="...and above this absolute number")
    fill_floors: dict[str, float] = Field(
        default_factory=dict,
        description="Minimum fill rate per ENTITY_TYPE.field, e.g. {'PERSON.dob': 0.85}; below floor quarantines",
    )
    fill_warn_margin: float = Field(
        0.05, ge=0, le=1, description="Warn when within this margin above a floor"
    )
    drift_policy: Literal["warn", "quarantine"] = Field(
        "warn", description="What to do when unknown XML element paths appear"
    )
    schema_file: str | None = Field(None, description="Pinned XSD / JSON schema under schemas/ (optional)")
    require_publication_marker: bool = False

    @field_validator("fill_floors")
    @classmethod
    def _floors(cls, v: dict[str, float]) -> dict[str, float]:
        for k, f in v.items():
            if "." not in k:
                raise ValueError(f"fill floor key {k!r} must be ENTITY_TYPE.field")
            if not 0 <= f <= 1:
                raise ValueError(f"fill floor {k} must be between 0 and 1")
        return v


class FetchConfig(_Base):
    url: str = Field(..., description="Download URL (https only)")
    allowed_hosts: list[str] = Field(
        ..., min_length=1, description="Hosts this source may reach, incl. redirects"
    )
    timeout_s: float = Field(120.0, ge=5, le=1800)
    max_mb: int = Field(400, ge=1, le=4096)
    conditional_get: bool = Field(True, description="Send If-None-Match / If-Modified-Since when available")
    extra_headers: dict[str, str] = Field(default_factory=dict)
    retry: RetryConfig = Field(default_factory=RetryConfig)

    @field_validator("url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.lower().startswith("https://"):
            raise ValueError("url must use https")
        return v


class ListSourceConfig(_Base):
    fetch: FetchConfig
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    alerts: AlertConfig = Field(default_factory=AlertConfig)


class CslSourceConfig(ListSourceConfig):
    include_list_codes: list[str] = Field(
        default_factory=lambda: ["EL", "DPL", "UVL", "MEU", "DTC", "ISN"],
        description="CSL source codes to keep (OFAC lists are ingested directly, so excluded by default)",
    )


class CuratedListConfig(_Base):
    annex: Literal["XLII", "IV"]
    regulation_celex: str = Field("32014R0833", description="Base act whose annex is tracked")
    sanctions_map: FetchConfig | None = Field(
        None, description="Optional EU Sanctions Map export used as a lead"
    )
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    alerts: AlertConfig = Field(default_factory=AlertConfig)


class NoticeFeedConfig(_Base):
    base_url: str
    allowed_hosts: list[str] = Field(..., min_length=1)
    lookback_days: int = Field(14, ge=1, le=365)
    max_items: int = Field(200, ge=1, le=5000)
    params: dict[str, str | list[str]] = Field(default_factory=dict)
    signal_for: list[str] = Field(
        default_factory=list, description="Sources to pull early when a new item appears"
    )
    keywords: list[str] = Field(
        default_factory=list, description="Only keep items whose title matches one of these"
    )
    retry: RetryConfig = Field(default_factory=RetryConfig)
    alerts: AlertConfig = Field(default_factory=AlertConfig)

    @field_validator("base_url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.lower().startswith("https://"):
            raise ValueError("base_url must use https")
        return v


class EnrichmentConfig(_Base):
    base_url: str
    allowed_hosts: list[str] = Field(..., min_length=1)
    rate_limit_calls: int = Field(60, ge=1)
    rate_limit_window_s: float = Field(60.0, gt=0)
    batch_size: int = Field(200, ge=1, le=100000, description="Max subjects per batch run")
    subject_sources: list[str] = Field(
        default_factory=list, description="List sources whose records are enriched"
    )
    entity_types: list[str] = Field(default_factory=lambda: ["ORGANIZATION"])
    auto_accept_score: float = Field(0.95, ge=0, le=1)
    review_score: float = Field(0.85, ge=0, le=1)
    refresh_days: int = Field(30, ge=1)
    personal_data_on_demand_only: bool = Field(
        True, description="NFR-07: never bulk-copy personal data; person-level lookups only on request"
    )
    download_url: str | None = None
    retry: RetryConfig = Field(default_factory=RetryConfig)
    alerts: AlertConfig = Field(default_factory=AlertConfig)


def is_sensitive_path(path: str) -> bool:
    return any(part in SENSITIVE_KEYS for part in path.split("."))


def diff_paths(old: object, new: object, prefix: str = "") -> list[str]:
    """Dotted paths whose values differ between two JSON-like structures."""
    if isinstance(old, dict) and isinstance(new, dict):
        out: list[str] = []
        for k in sorted(set(old) | set(new)):
            p = f"{prefix}.{k}" if prefix else str(k)
            out.extend(diff_paths(old.get(k), new.get(k), p))
        return out
    return [] if old == new else [prefix or "<root>"]
