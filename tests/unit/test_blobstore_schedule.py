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
