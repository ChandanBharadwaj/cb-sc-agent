import os
from datetime import UTC, datetime, timedelta

import pytest

from sanctions_agent.scheduling.schedule import Schedule, ScheduleError
from sanctions_agent.storage.blobstore import BlobIntegrityError, FsBlobStore, sha256_file


def test_fs_blobstore_is_write_once_and_roundtrips(tmp_path):
    store = FsBlobStore(tmp_path / "blobs")
    src = tmp_path / "f.xml"
    src.write_bytes(b"<a>hello</a>")
    sha = sha256_file(src)
    uri = store.put_file(src, sha)
    assert store.put_file(src, sha) == uri  # idempotent
    path = store._resolve(uri)
    assert not os.access(path, os.W_OK) or os.geteuid() == 0
    assert oct(path.stat().st_mode)[-3:] == "444"
    with store.open(uri) as fh:
        assert fh.read() == b"<a>hello</a>"
    with pytest.raises(BlobIntegrityError):
        store.put_file(src, "0" * 64)
    with pytest.raises(ValueError):
        store._resolve("fs://../../etc/passwd")


def _sched(**kw):
    base = dict(
        kind="INTERVAL",
        cadence=timedelta(hours=2),
        cron_expr=None,
        timezone="UTC",
        min_interval=timedelta(minutes=30),
        warn_staleness=timedelta(hours=6),
        hard_max_staleness=timedelta(hours=12),
    )
    base.update(kw)
    return Schedule(**base)


def test_interval_below_politeness_floor_rejected():
    with pytest.raises(ScheduleError, match="politeness floor"):
        _sched(cadence=timedelta(minutes=10)).validate(timedelta(minutes=30))
    _sched().validate(timedelta(minutes=30))


def test_cron_validation_and_timezone_preview():
    with pytest.raises(ScheduleError, match="invalid cron"):
        _sched(kind="CRON", cron_expr="not a cron").validate(timedelta(minutes=30))
    with pytest.raises(ScheduleError, match="politeness floor"):
        _sched(kind="CRON", cron_expr="*/5 * * * *").validate(timedelta(minutes=30))
    s = _sched(kind="CRON", cron_expr="0 6 * * *", timezone="America/New_York", cadence=None)
    s.validate(timedelta(minutes=30))
    runs = s.preview(2, after=datetime(2026, 9, 24, 0, 0, tzinfo=UTC))
    assert runs[0] == datetime(2026, 9, 24, 10, 0, tzinfo=UTC)  # 06:00 EDT = 10:00 UTC
    assert runs[1] - runs[0] == timedelta(days=1)
    assert s.describe() == "cron 0 6 * * * America/New_York"


def test_unknown_timezone_rejected():
    with pytest.raises(ScheduleError, match="timezone"):
        _sched(timezone="Mars/Olympus").validate(timedelta(minutes=30))


def test_interval_schedules_are_clock_aligned_to_utc():
    s = _sched(cadence=timedelta(hours=2))
    t = lambda h, m=0, d=24: datetime(2026, 9, d, h, m, tzinfo=UTC)  # noqa: E731
    assert s.next_after(t(13, 5)) == t(14)
    assert s.next_after(t(14)) == t(16)  # strictly after
    assert s.next_after(t(23, 59)) == t(0, d=25)
    assert [x.hour for x in s.preview(3, after=t(9, 30))] == [10, 12, 14]
    thirty = _sched(cadence=timedelta(minutes=30), min_interval=timedelta(minutes=15))
    assert thirty.next_after(t(12, 1)) == t(12, 30)
    weekly = _sched(
        cadence=timedelta(days=7), hard_max_staleness=timedelta(days=14), warn_staleness=timedelta(days=8)
    )
    nxt = weekly.next_after(t(12))
    assert nxt.hour == 0 and nxt.minute == 0 and nxt.weekday() == 3  # epoch-aligned: Thursdays 00:00 UTC
    assert s.describe() == "every 2 h (UTC-aligned)"


def test_next_due_respects_politeness_and_tolerates_tick_jitter():
    s = _sched(cadence=timedelta(hours=2), min_interval=timedelta(minutes=30))
    t = lambda h, m=0, sec=0: datetime(2026, 9, 24, h, m, sec, tzinfo=UTC)  # noqa: E731
    assert s.next_due(t(12, 0, 40)) == t(14)  # normal scheduled run
    assert s.next_due(t(13, 55)) == t(16)  # a manual pull just before a slot skips that slot
    hourly = _sched(
        cadence=timedelta(hours=1),
        min_interval=timedelta(hours=1),
        hard_max_staleness=timedelta(hours=3),
        warn_staleness=timedelta(hours=2),
    )
    assert hourly.next_due(t(12, 0, 30)) == t(13)  # cadence == min interval: jitter must not skip a slot
