"""Source management rules: versioned config, maker-checker, status changes, activation, rollback.

Everything here runs inside the caller's transaction and is audited (audit_log trigger + the
append-only ``source_config_version`` history). The agent never calls the mutating methods; it can only
file CONFIG_CHANGE proposals.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import psycopg
from pydantic import BaseModel, ValidationError

from sanctions_agent.db.engine import fetch_one, fetch_val, jsonb
from sanctions_agent.http.guard import global_allow_list, host_matches
from sanctions_agent.ops import incidents
from sanctions_agent.scheduling.schedule import Schedule, ScheduleError
from sanctions_agent.sources.adapter_types import get_adapter_type
from sanctions_agent.sources.config_models import diff_paths, is_sensitive_path

ROLES = ("viewer", "operator", "reviewer", "admin")


class ConfigConflict(Exception):
    """Optimistic-lock failure: the source changed since the editor loaded it."""


class PermissionDenied(Exception):
    pass


class ValidationFailed(ValueError):
    pass


@dataclass
class UpdateResult:
    applied: bool
    version: int
    pending_change_id: int | None = None
    changed_paths: list[str] | None = None
    message: str = ""


def _collect_urls_and_hosts(cfg: dict[str, Any]) -> tuple[list[str], list[list[str]]]:
    urls: list[str] = []
    host_lists: list[list[str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("allowed_hosts"), list):
                host_lists.append([str(h) for h in node["allowed_hosts"]])
            for k, v in node.items():
                if k in ("url", "base_url", "download_url", "rss_url", "sparql_url") and isinstance(v, str):
                    urls.append(v)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(cfg)
    return urls, host_lists


def check_hosts(cfg: dict[str, Any]) -> None:
    """Every configured host must be on the global allow-list; every URL must use an allowed host."""
    gl = global_allow_list()
    urls, host_lists = _collect_urls_and_hosts(cfg)
    all_hosts = [h for hl in host_lists for h in hl]
    for h in all_hosts:
        ok = (h in gl.hosts) if h.startswith("*.") else gl.allows(h)
        if not ok:
            raise ValidationFailed(
                f"host {h!r} is not on the deployment's global allow-list (config/allowed_hosts.yaml)"
            )
    for u in urls:
        host = (urlsplit(u).hostname or "").lower()
        if not any(host_matches(host, p) for p in all_hosts):
            raise ValidationFailed(f"URL host {host!r} is not in this source's allowed_hosts")
        if not gl.allows(host):
            raise ValidationFailed(f"URL host {host!r} is not on the global allow-list")


class ConfigService:
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self.conn = conn

    # ------------------------------------------------------------------------------------------
    def _validate(
        self, adapter_type: str, config: dict[str, Any], schedule: Schedule
    ) -> tuple[BaseModel, Any]:
        at = get_adapter_type(adapter_type)
        try:
            model = at.validate_config(config)
        except ValidationError as e:
            raise ValidationFailed(f"invalid config: {e}") from e
        try:
            schedule.validate(at.hard_min_interval)
        except ScheduleError as e:
            raise ValidationFailed(str(e)) from e
        return model, at

    def _write_version(
        self,
        source_id: str,
        version: int,
        config: dict[str, Any],
        schedule: Schedule,
        diff: list[str] | None,
        actor: str,
        reason: str | None,
        status: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO source_config_version (source_id, version, config, schedule, diff, changed_by, reason, status)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                source_id,
                version,
                jsonb(config),
                jsonb(schedule.to_json()),
                jsonb(diff or []),
                actor,
                reason,
                status,
            ),
        )

    def _apply(self, source_id: str, version: int, config: dict[str, Any], schedule: Schedule) -> None:
        self.conn.execute(
            "UPDATE source_config_version SET status = 'SUPERSEDED' WHERE source_id = %s AND status = 'ACTIVE'",
            (source_id,),
        )
        self.conn.execute(
            "UPDATE source_config_version SET status = 'ACTIVE' WHERE source_id = %s AND version = %s",
            (source_id, version),
        )
        self.conn.execute(
            """UPDATE source SET config = %s, config_version = %s, schedule_kind = %s, cadence = %s, cron_expr = %s,
                   timezone = %s, min_interval = %s, warn_staleness = %s, hard_max_staleness = %s, updated_at = now(),
                   next_due_at = CASE WHEN status = 'ACTIVE'
                                      THEN least(coalesce(next_due_at, now()), now() + coalesce(%s, interval '1 hour'))
                                      ELSE next_due_at END
               WHERE source_id = %s""",
            (
                jsonb(config),
                version,
                schedule.kind,
                schedule.cadence,
                schedule.cron_expr,
                schedule.timezone,
                schedule.min_interval,
                schedule.warn_staleness,
                schedule.hard_max_staleness,
                schedule.cadence,
                source_id,
            ),
        )

    # ------------------------------------------------------------------------------------------
    def create_source(
        self,
        *,
        source_id: str,
        display_name: str,
        adapter_type: str,
        config: dict[str, Any],
        schedule: Schedule,
        actor: str,
        reason: str | None,
        is_core: bool = False,
        priority: int = 5,
        licence: str | None = None,
        licence_status: str = "OK",
        initial_status: str = "DRAFT",
        status_reason: str | None = None,
        seed: bool = False,
    ) -> None:
        model, at = self._validate(adapter_type, config, schedule)
        cfg = model.model_dump(mode="json")
        if initial_status == "ACTIVE":
            if not seed:
                raise PermissionDenied("new sources start as DRAFT and need activation approval")
            check_hosts(cfg)
        elif initial_status not in ("DRAFT", "DISABLED", "PAUSED"):
            raise ValidationFailed(f"invalid initial status {initial_status}")
        self.conn.execute(
            """INSERT INTO source (source_id, display_name, adapter_type, level, kind, is_core, status, status_reason,
                   status_changed_by, status_changed_at, config, config_version, schedule_kind, cadence, cron_expr,
                   timezone, min_interval, warn_staleness, hard_max_staleness, priority, licence, licence_status,
                   next_due_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                       CASE WHEN %s = 'ACTIVE' THEN now() END)""",
            (
                source_id,
                display_name,
                adapter_type,
                at.level,
                at.kind,
                is_core,
                initial_status,
                status_reason,
                actor,
                jsonb(cfg),
                schedule.kind,
                schedule.cadence,
                schedule.cron_expr,
                schedule.timezone,
                schedule.min_interval,
                schedule.warn_staleness,
                schedule.hard_max_staleness,
                priority,
                licence,
                licence_status,
                initial_status,
            ),
        )
        self._write_version(source_id, 1, cfg, schedule, None, actor, reason, "ACTIVE")

    # ------------------------------------------------------------------------------------------
    def update_source(
        self,
        source_id: str,
        *,
        actor: str,
        reason: str | None,
        expected_version: int,
        config: dict[str, Any] | None = None,
        schedule: Schedule | None = None,
        priority: int | None = None,
        display_name: str | None = None,
        trusted: bool = False,
        force_review: bool = False,
    ) -> UpdateResult:
        row = fetch_one(self.conn, "SELECT * FROM source WHERE source_id = %s FOR UPDATE", (source_id,))
        if row is None:
            raise KeyError(source_id)
        if row["config_version"] != expected_version:
            raise ConfigConflict(
                f"{source_id} is at config version {row['config_version']}, you edited version {expected_version}; reload"
            )
        if priority is not None or display_name is not None:
            self.conn.execute(
                "UPDATE source SET priority = coalesce(%s, priority), display_name = coalesce(%s, display_name),"
                " updated_at = now() WHERE source_id = %s",
                (priority, display_name, source_id),
            )
        cur_schedule = Schedule.from_row(row)
        new_schedule = schedule or cur_schedule
        new_config_in = config if config is not None else row["config"]
        model, _ = self._validate(row["adapter_type"], new_config_in, new_schedule)
        new_config = model.model_dump(mode="json")
        changed = diff_paths(row["config"], new_config)
        if new_schedule.to_json() != cur_schedule.to_json():
            changed += [f"schedule.{p}" for p in diff_paths(cur_schedule.to_json(), new_schedule.to_json())]
        if not changed:
            return UpdateResult(
                applied=True, version=row["config_version"], changed_paths=[], message="no changes"
            )
        if row["status"] == "ACTIVE":
            check_hosts(new_config)
        next_version = int(
            fetch_val(
                self.conn,
                "SELECT coalesce(max(version), 0) + 1 FROM source_config_version WHERE source_id = %s",
                (source_id,),
            )
        )
        sensitive = [p for p in changed if is_sensitive_path(p)]
        if (sensitive or force_review) and not trusted:
            review_paths = sensitive or changed
            self._write_version(
                source_id, next_version, new_config, new_schedule, changed, actor, reason, "PENDING_APPROVAL"
            )
            change_id = fetch_val(
                self.conn,
                """INSERT INTO proposed_change (kind, source_id, subject_ref, title, payload, rationale, proposed_by, dedupe_key)
                   VALUES ('CONFIG_CHANGE', %s, %s, %s, %s, %s, %s, %s) RETURNING change_id""",
                (
                    source_id,
                    f"{source_id}@v{next_version}",
                    (
                        f"Security-sensitive config change for {source_id}: {', '.join(sensitive)}"
                        if sensitive
                        else f"Proposed config change for {source_id}: {', '.join(changed)}"
                    ),
                    jsonb(
                        {
                            "version": next_version,
                            "base_version": row["config_version"],
                            "changed_paths": changed,
                            "sensitive_paths": sensitive,
                            "old": {p: _get_path(row["config"], p) for p in review_paths},
                            "new": {p: _get_path(new_config, p) for p in review_paths},
                        }
                    ),
                    reason,
                    actor,
                    f"config:{source_id}:v{next_version}",
                ),
            )
            self.conn.execute(
                "UPDATE source_config_version SET approval_change_id = %s WHERE source_id = %s AND version = %s",
                (change_id, source_id, next_version),
            )
            return UpdateResult(
                applied=False,
                version=next_version,
                pending_change_id=int(change_id),
                changed_paths=changed,
                message="change submitted for admin approval"
                + (" (security-sensitive: second admin required)" if sensitive else ""),
            )
        # written as a transient PENDING_APPROVAL row and activated in the same transaction
        self._write_version(
            source_id, next_version, new_config, new_schedule, changed, actor, reason, "PENDING_APPROVAL"
        )
        self._apply(source_id, next_version, new_config, new_schedule)
        return UpdateResult(applied=True, version=next_version, changed_paths=changed, message="applied")

    def rollback(
        self, source_id: str, to_version: int, *, actor: str, reason: str, expected_version: int
    ) -> UpdateResult:
        v = fetch_one(
            self.conn,
            "SELECT config, schedule FROM source_config_version WHERE source_id = %s AND version = %s",
            (source_id, to_version),
        )
        if v is None:
            raise KeyError(f"{source_id} has no config version {to_version}")
        return self.update_source(
            source_id,
            actor=actor,
            reason=f"rollback to v{to_version}: {reason}",
            expected_version=expected_version,
            config=v["config"],
            schedule=Schedule.from_seed(v["schedule"]),
        )

    # ------------------------------------------------------------------------------------------
    def decide_change(
        self, change_id: int, *, reviewer: str, approve: bool, comment: str | None = None
    ) -> str:
        """Approve/reject a CONFIG_CHANGE or SOURCE_ACTIVATION proposal (maker-checker)."""
        ch = fetch_one(
            self.conn, "SELECT * FROM proposed_change WHERE change_id = %s FOR UPDATE", (change_id,)
        )
        if ch is None:
            raise KeyError(change_id)
        if ch["kind"] not in ("CONFIG_CHANGE", "SOURCE_ACTIVATION"):
            raise ValidationFailed("not a configuration proposal")
        if ch["status"] != "PENDING":
            raise ValidationFailed(f"proposal is already {ch['status']}")
        if ch["proposed_by"] == reviewer:
            raise PermissionDenied("maker-checker: a different admin must approve this change")
        source_id = ch["source_id"]
        if not approve:
            self.conn.execute(
                "UPDATE proposed_change SET status = 'REJECTED', reviewed_by = %s, reviewed_at = now(),"
                " review_comment = %s WHERE change_id = %s",
                (reviewer, comment, change_id),
            )
            if ch["kind"] == "CONFIG_CHANGE":
                self.conn.execute(
                    "UPDATE source_config_version SET status = 'REJECTED' WHERE approval_change_id = %s",
                    (change_id,),
                )
            return "REJECTED"
        if ch["kind"] == "SOURCE_ACTIVATION":
            src = fetch_one(self.conn, "SELECT * FROM source WHERE source_id = %s FOR UPDATE", (source_id,))
            assert src is not None
            check_hosts(src["config"])
            self.conn.execute(
                "UPDATE source SET status = 'ACTIVE', status_reason = %s, status_changed_by = %s, status_changed_at = now(),"
                " next_due_at = now(), updated_at = now() WHERE source_id = %s",
                (f"activation approved by {reviewer}", reviewer, source_id),
            )
        else:
            payload = ch["payload"]
            src = fetch_one(
                self.conn, "SELECT config_version FROM source WHERE source_id = %s FOR UPDATE", (source_id,)
            )
            assert src is not None
            if src["config_version"] != payload["base_version"]:
                self.conn.execute(
                    "UPDATE proposed_change SET status = 'EXPIRED', reviewed_by = %s, reviewed_at = now(),"
                    " review_comment = 'source changed since proposal' WHERE change_id = %s",
                    (reviewer, change_id),
                )
                raise ConfigConflict("the source changed after this proposal was made; it has been expired")
            v = fetch_one(
                self.conn,
                "SELECT config, schedule FROM source_config_version WHERE source_id = %s AND version = %s",
                (source_id, payload["version"]),
            )
            assert v is not None
            check_hosts(v["config"])
            self._apply(source_id, payload["version"], v["config"], Schedule.from_seed(v["schedule"]))
        self.conn.execute(
            "UPDATE proposed_change SET status = 'APPLIED', reviewed_by = %s, reviewed_at = now(),"
            " review_comment = %s WHERE change_id = %s",
            (reviewer, comment, change_id),
        )
        return "APPLIED"

    def request_activation(self, source_id: str, *, actor: str, reason: str | None) -> int:
        src = fetch_one(self.conn, "SELECT status, config FROM source WHERE source_id = %s", (source_id,))
        if src is None:
            raise KeyError(source_id)
        if src["status"] != "DRAFT":
            raise ValidationFailed("only DRAFT sources need activation")
        check_hosts(src["config"])
        return int(
            fetch_val(
                self.conn,
                """INSERT INTO proposed_change (kind, source_id, subject_ref, title, payload, rationale, proposed_by, dedupe_key)
               VALUES ('SOURCE_ACTIVATION', %s, %s, %s, %s, %s, %s, %s) RETURNING change_id""",
                (
                    source_id,
                    source_id,
                    f"Activate new source {source_id}",
                    jsonb({"config": src["config"]}),
                    reason,
                    actor,
                    f"activate:{source_id}",
                ),
            )
        )

    # ------------------------------------------------------------------------------------------
    def set_status(
        self,
        source_id: str,
        status: str,
        *,
        actor: str,
        role: str,
        reason: str | None,
        paused_until: datetime | None = None,
    ) -> None:
        if status not in ("ACTIVE", "PAUSED", "DISABLED"):
            raise ValidationFailed(f"invalid status {status}")
        src = fetch_one(self.conn, "SELECT * FROM source WHERE source_id = %s FOR UPDATE", (source_id,))
        if src is None:
            raise KeyError(source_id)
        if src["status"] == "DRAFT" and status == "ACTIVE":
            raise PermissionDenied("DRAFT sources are activated through an approval (request activation)")
        if status in ("PAUSED", "DISABLED") and not (reason and reason.strip()):
            raise ValidationFailed("a reason is required to pause or disable a source")
        if status == "DISABLED" and src["is_core"] and role != "admin":
            raise PermissionDenied("only an admin can disable a core list")
        if status == "DISABLED" and role not in ("admin",):
            raise PermissionDenied("only an admin can disable sources")
        if status == "ACTIVE" and src["status"] == "DISABLED" and role != "admin":
            raise PermissionDenied("only an admin can re-enable a disabled source")
        if status == "ACTIVE":
            check_hosts(src["config"])
        self.conn.execute(
            """UPDATE source SET status = %s, status_reason = %s, status_changed_by = %s, status_changed_at = now(),
                   paused_until = %s, updated_at = now(),
                   next_due_at = CASE WHEN %s = 'ACTIVE' THEN coalesce(next_due_at, now()) ELSE next_due_at END
               WHERE source_id = %s""",
            (status, reason, actor, paused_until if status == "PAUSED" else None, status, source_id),
        )
        if src["is_core"] and status in ("PAUSED", "DISABLED"):
            incidents.open_incident(
                self.conn,
                source_id=source_id,
                error_class="CORE_SOURCE_DISABLED",
                severity="WARN",
                title=f"Core list {source_id} {status.lower()} - screening coverage reduced",
                summary=f"{status} by {actor}: {reason}"
                + (f" until {paused_until.isoformat()}" if paused_until else ""),
            )
        elif status == "ACTIVE":
            incidents.resolve(
                self.conn,
                source_id=source_id,
                error_classes=["CORE_SOURCE_DISABLED"],
                note=f"re-enabled by {actor}",
            )


def _get_path(d: Any, path: str) -> Any:
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur
