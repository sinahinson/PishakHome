import tempfile
from pathlib import Path

import pytest

from app.database import Database
from app.events import EventBus


@pytest.fixture
def bus():
    with tempfile.TemporaryDirectory() as tmp:
        database = Database(Path(tmp) / "events_test.db")
        yield EventBus(database, default_cooldown_seconds=100)
        database.close()


def test_emit_persists_event(bus):
    event_id = bus.emit("MotionDetected", zone="Living Room")
    events = bus.db.get_events()
    assert len(events) == 1
    assert events[0]["id"] == event_id


def test_cooldown_suppresses_repeated_notifications(bus):
    calls = []
    bus.register_notifier(lambda event_type, payload: calls.append(payload))

    bus.emit("MotionDetected")
    bus.emit("MotionDetected")  # within cooldown window -> should NOT notify again

    assert len(calls) == 1
    # But both events are still stored in the database.
    assert len(bus.db.get_events()) == 2


def test_per_type_cooldown_override(bus):
    calls = []
    bus.register_notifier(lambda event_type, payload: calls.append(payload))
    bus.set_cooldown("CatDetected", 0)  # effectively no cooldown

    bus.emit("CatDetected")
    bus.emit("CatDetected")

    assert len(calls) == 2


def test_notifier_exception_does_not_break_emit(bus):
    def bad_notifier(event_type, payload):
        raise RuntimeError("boom")

    bus.register_notifier(bad_notifier)
    # Should not raise even though the notifier is broken.
    event_id = bus.emit("CameraOffline")
    assert event_id > 0


def test_extra_is_delivered_to_notifiers_but_not_stored(bus):
    seen = []
    bus.register_notifier(lambda event_type, payload: seen.append(payload))
    bus.emit("MotionDetected", extra={"snapshot_bytes": b"\xff\xd8abc"})
    assert seen[0]["snapshot_bytes"] == b"\xff\xd8abc"
    stored = bus.db.get_events(limit=1)[0]
    assert "snapshot_bytes" not in stored
    assert stored["meta"] is None


def test_async_dispatch_does_not_block_emit_and_still_delivers():
    import threading
    import time
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        database = Database(Path(tmp) / "async.db")
        async_bus = EventBus(database, default_cooldown_seconds=0, async_dispatch=True)
        gate = threading.Event()
        delivered = []

        def slow_notifier(event_type, payload):
            gate.wait(timeout=5)  # simulates a slow/unreachable Telegram
            delivered.append(event_type)

        async_bus.register_notifier(slow_notifier)
        start = time.time()
        async_bus.emit("MotionDetected")
        assert time.time() - start < 0.5  # emit returned without waiting for the notifier
        assert delivered == []

        gate.set()
        deadline = time.time() + 3
        while time.time() < deadline and not delivered:
            time.sleep(0.02)
        assert delivered == ["MotionDetected"]
        async_bus.close()
        database.close()


def test_async_bus_restarts_worker_after_close():
    import time
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        database = Database(Path(tmp) / "async2.db")
        async_bus = EventBus(database, default_cooldown_seconds=0, async_dispatch=True)
        delivered = []
        async_bus.register_notifier(lambda t, p: delivered.append(t))
        async_bus.emit("A")
        async_bus.close()
        async_bus.emit("B")  # must transparently restart the worker
        deadline = time.time() + 3
        while time.time() < deadline and len(delivered) < 2:
            time.sleep(0.02)
        assert delivered == ["A", "B"]
        async_bus.close()
        database.close()
