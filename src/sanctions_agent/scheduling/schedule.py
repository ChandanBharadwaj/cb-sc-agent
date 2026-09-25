"""Source schedules: fixed interval or cron (with timezone), pause-until, and next-run previews.

The politeness floor (``AdapterType.hard_min_interval``) is enforced here so neither the UI nor the
agent can schedule a publisher more often than is polite (BRD 9.2 / NFR-02).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class ScheduleError(ValueError):
    pass


def politeness_grace(min_interval: timedelta) -> timedelta:
    """Slack for scheduler-tick jitter: a clock-aligned pull that starts a few seconds after its slot must not
    make the next slot look impolite. Never more than 10% of the interval."""
    return min(timedelta(minutes=2), min_interval / 10)


@dataclass(frozen=True)
class Schedule:
    kind: Literal["INTERVAL", "CRON"]
    cadence: timedelta | None
    cron_expr: str | None
    timezone: str
    min_interval: timedelta
    warn_staleness: timedelta
    hard_max_staleness: timedelta

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Schedule:
        return cls(
            kind=row["schedule_kind"],
            cadence=row.get("cadence"),
            cron_expr=row.get("cron_expr"),
            timezone=row.get("timezone") or "UTC",
            min_interval=row["min_interval"],
            warn_staleness=row["warn_staleness"],
            hard_max_staleness=row["hard_max_staleness"],
        )

    @classmethod
    def from_seed(cls, d: dict[str, Any]) -> Schedule:
        kind = d.get("kind", "INTERVAL")
        return cls(
            kind=kind,
            cadence=timedelta(minutes=d["cadence_minutes"]) if d.get("cadence_minutes") else None,
            cron_expr=d.get("cron_expr"),
            timezone=d.get("timezone", "UTC"),
            min_interval=timedelta(minutes=d.get("min_interval_minutes", 30)),
            warn_staleness=timedelta(hours=d.get("warn_staleness_hours", 6)),
            hard_max_staleness=timedelta(hours=d.get("hard_max_staleness_hours", 12)),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "cadence_minutes": int(self.cadence.total_seconds() // 60) if self.cadence else None,
            "cron_expr": self.cron_expr,
            "timezone": self.timezone,
            "min_interval_minutes": int(self.min_interval.total_seconds() // 60),
            "warn_staleness_hours": self.warn_staleness.total_seconds() / 3600,
            "hard_max_staleness_hours": self.hard_max_staleness.total_seconds() / 3600,
        }

    # ------------------------------------------------------------------------------------------
    def validate(self, hard_floor: timedelta) -> None:
        try:
            tz = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ScheduleError(f"unknown timezone {self.timezone!r}") from e
        if self.min_interval < hard_floor:
            raise ScheduleError(
                f"min interval {self.min_interval} is below this adapter's politeness floor {hard_floor}"
            )
        if self.kind == "INTERVAL":
            if not self.cadence:
                raise ScheduleError("interval schedules need a cadence")
            if self.cadence < hard_floor:
                raise ScheduleError(f"cadence {self.cadence} is below the politeness floor {hard_floor}")
        elif self.kind == "CRON":
            if not self.cron_expr or not croniter.is_valid(self.cron_expr):
                raise ScheduleError(f"invalid cron expression {self.cron_expr!r}")
            runs = self.preview(20, after=datetime(2026, 1, 5, tzinfo=tz))
            gaps = [b - a for a, b in pairwise(runs)]
            if gaps and min(gaps) < hard_floor:
                raise ScheduleError(
                    f"cron runs as often as every {min(gaps)}, below the politeness floor {hard_floor}"
                )
        else:
            raise ScheduleError(f"unknown schedule kind {self.kind}")
        if self.warn_staleness > self.hard_max_staleness:
            raise ScheduleError("warn staleness must not exceed hard max staleness")
        period = self.cadence if self.kind == "INTERVAL" else None
        if period and self.hard_max_staleness < period:
            raise ScheduleError("hard max staleness is shorter than the cadence; every run would page")

    def next_after(self, when: datetime) -> datetime:
        if self.kind == "INTERVAL":
            assert self.cadence is not None
            # Clock-aligned: the next multiple of the cadence since the UTC epoch, strictly after ``when``.
            # Every source with the same cadence falls due in the same scheduler tick, so each scheduled cycle
            # is one run batch ("every 2 h" = 00:00, 02:00, ... UTC).
            n = (when.astimezone(UTC) - _EPOCH) // self.cadence + 1
            return _EPOCH + self.cadence * n
        tz = ZoneInfo(self.timezone)
        it = croniter(self.cron_expr, when.astimezone(tz))
        nxt: datetime = it.get_next(datetime)
        return nxt.astimezone(UTC)

    def next_due(self, now: datetime) -> datetime:
        """Next slot after a completed pull: the first slot that also respects the politeness interval, so a
        manual pull at 13:55 skips the 14:00 slot instead of being refused and running alone at 14:25."""
        return self.next_after(now + self.min_interval - politeness_grace(self.min_interval))

    def preview(self, n: int = 5, after: datetime | None = None) -> list[datetime]:
        cur = after or datetime.now(UTC)
        out: list[datetime] = []
        for _ in range(n):
            cur = self.next_after(cur)
            out.append(cur)
        return out

    def describe(self) -> str:
        if self.kind == "INTERVAL" and self.cadence:
            mins = int(self.cadence.total_seconds() // 60)
            if mins % 1440 == 0:
                every = f"every {mins // 1440} d"
            elif mins % 60 == 0:
                every = f"every {mins // 60} h"
            else:
                every = f"every {mins} min"
            return f"{every} (UTC-aligned)"
        return f"cron {self.cron_expr} {self.timezone}"
