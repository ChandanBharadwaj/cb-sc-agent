"""Runtime settings, loaded from environment variables (prefix ``SANCTIONS_``) or a ``.env`` file.

Source definitions are *not* here: they live in the database (seeded from ``config/sources.yaml``)
so they can be managed from the UI. The global host allow-list is deployment configuration
(``config/allowed_hosts.yaml``) and deliberately cannot be changed from the UI (NFR-09).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SANCTIONS_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    environment: Literal["dev", "test", "prod"] = "dev"

    # --- Database -------------------------------------------------------------------------------
    database_url: str = "postgresql://sanctions:sanctions@localhost:5432/sanctions"
    # Read-only, aggregate-only role used by the natural-language Q&A agent (see migration 0002).
    analyst_database_url: str = "postgresql://sanctions_analyst:sanctions_analyst@localhost:5432/sanctions"
    db_pool_min: int = 1
    db_pool_max: int = 10

    # --- Storage --------------------------------------------------------------------------------
    blob_backend: Literal["fs", "s3"] = "fs"
    blob_root: Path = PROJECT_ROOT / "data" / "blobs"
    s3_bucket: str | None = None
    s3_prefix: str = "sanctions-raw/"
    s3_object_lock_days: int | None = None
    work_dir: Path = PROJECT_ROOT / "data" / "work"

    # --- Configuration files --------------------------------------------------------------------
    sources_seed_file: Path = PROJECT_ROOT / "config" / "sources.yaml"
    allowed_hosts_file: Path = PROJECT_ROOT / "config" / "allowed_hosts.yaml"
    schemas_dir: Path = PROJECT_ROOT / "schemas"

    # --- HTTP -----------------------------------------------------------------------------------
    user_agent: str = (
        "cb-sc-agent/0.1 (sanctions list ingestion; compliance engineering; +https://example.invalid/contact)"
    )
    http_connect_timeout: float = 20.0
    http_read_timeout: float = 120.0
    # Allow private/loopback addresses (only for tests and local fixture servers).
    http_allow_private_networks: bool = False

    # --- Scheduler / worker ---------------------------------------------------------------------
    tick_seconds: int = 60
    worker_concurrency: int = 3
    lease_seconds: int = 300
    heartbeat_seconds: int = 30
    progress_min_interval_seconds: float = 2.0
    staging_retention_days: int = 7

    # --- Agent (OpenAI) -------------------------------------------------------------------------
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    agent_model: str = "gpt-5-mini"
    agent_reasoning_effort: Literal["minimal", "low", "medium", "high"] = "low"
    agent_max_turns: int = 20
    agent_daily_budget_usd: float = 2.0
    agent_health_review_minutes: int = 360
    agent_cycle_timeout_seconds: int = 180
    # Export traces to the OpenAI dashboard as well as Postgres (off by default).
    agent_export_traces_to_openai: bool = False
    # Price per 1M tokens used for cost accounting (update if OpenAI pricing changes).
    price_input_per_mtok: float = 0.25
    price_cached_input_per_mtok: float = 0.025
    price_output_per_mtok: float = 2.00

    # --- Enrichment credentials -----------------------------------------------------------------
    companies_house_api_key: SecretStr | None = None

    # --- Alerts ---------------------------------------------------------------------------------
    alert_webhook_url: str | None = None
    alert_email_to: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str = "sanctions-agent@localhost"

    # --- API / UI auth --------------------------------------------------------------------------
    # basic: users from ``ui_users`` ("user:password:role,..."); proxy: trust X-Forwarded-User and
    # X-Forwarded-Roles from an SSO gateway; none: everyone is admin (dev only).
    auth_mode: Literal["none", "basic", "proxy"] = "basic"
    ui_users: SecretStr = SecretStr(
        "admin:admin:admin,ops:ops:operator,review:review:reviewer,view:view:viewer"
    )
    api_host: str = "0.0.0.0"  # noqa: S104 - container default; put behind a reverse proxy
    api_port: int = 8080


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
