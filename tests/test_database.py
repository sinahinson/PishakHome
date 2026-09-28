import tempfile
from pathlib import Path

import pytest

from app.database import Database


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        database = Database(Path(tmp) / "test.db")
        yield database
        database.close()


def test_add_and_get_event(db):
    event_id = db.add_event("MotionDetected", camera_id="/dev/video0", confidence=0.5)
    assert event_id > 0
    events = db.get_events(limit=10)
    assert len(events) == 1
    assert events[0]["event_type"] == "MotionDetected"
    assert events[0]["confidence"] == 0.5


def test_get_events_filters_by_type(db):
    db.add_event("MotionDetected")
    db.add_event("CatDetected")
    db.add_event("CatDetected")
    cat_events = db.get_events(event_type="CatDetected")
    assert len(cat_events) == 2
    assert all(e["event_type"] == "CatDetected" for e in cat_events)


def test_clear_events_removes_everything_and_returns_count(db):
    db.add_event("MotionDetected")
    db.add_event("CatDetected")
    count = db.clear_events()
    assert count == 2
    assert db.get_events() == []


def test_clear_events_on_empty_table(db):
    assert db.clear_events() == 0


def test_event_meta_roundtrip(db):
    db.add_event("StorageLow", meta={"path": "/mnt/sd", "free_mb": 12.3})
    events = db.get_events(limit=1)
    assert events[0]["meta"] == {"path": "/mnt/sd", "free_mb": 12.3}


def test_recordings_lifecycle(db):
    rec_id = db.start_recording("recordings/test.mkv", trigger="manual")
    recordings = db.get_recordings()
    assert recordings[0]["ended_at"] is None
    db.finish_recording(rec_id, size_bytes=1024)
    recordings = db.get_recordings()
    assert recordings[0]["ended_at"] is not None
    assert recordings[0]["size_bytes"] == 1024


def test_settings_roundtrip(db):
    assert db.get_setting("missing_key", default="fallback") == "fallback"
    db.set_setting("retention_days", 7)
    assert db.get_setting("retention_days") == 7
    db.set_setting("retention_days", 14)
    assert db.get_setting("retention_days") == 14


def test_snapshot_and_system_event(db):
    snap_id = db.add_snapshot("snapshots/a.jpg")
    assert snap_id > 0
    db.log_system_event("warning", "test warning")  # should not raise


def test_concurrent_access_from_many_threads_does_not_error(db):
    """Regression: the shared sqlite3 connection used to be touched by many
    threads at once (web requests, motion loop, retention loop, startup),
    producing 'bad parameter or other API misuse' and 'cannot commit - no
    transaction is active'. Every operation is now serialized by a lock."""
    import sys
    import threading

    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # maximize thread interleaving
    errors = []

    def writer():
        for i in range(150):
            try:
                db.add_event("Stress", meta={"i": i})
                db.set_setting("k", {"i": i})
            except Exception as exc:  # pragma: no cover - only on regression
                errors.append(repr(exc))

    def reader():
        for _ in range(150):
            try:
                db.get_setting("config:recording", default={})
                db.get_events(limit=5)
            except Exception as exc:  # pragma: no cover - only on regression
                errors.append(repr(exc))

    threads = [threading.Thread(target=writer) for _ in range(3)] + [
        threading.Thread(target=reader) for _ in range(4)
    ]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(old_interval)

    assert errors == []
    assert len(db.get_events(limit=1000)) == 3 * 150
