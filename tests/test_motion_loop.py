"""Tests for the motion-detection worker in app.main: alert content,
camera-disconnect behaviour, and — most importantly — that the worker can't
die silently (the bug where motion stopped after the camera was replugged
and toggling the setting didn't bring it back)."""

import copy
import threading
import time
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from app import main
from app.detection import MotionDetector
from app.settings_manager import DEFAULTS


def _jpeg(color):
    buf = BytesIO()
    Image.new("RGB", (160, 90), color=color).save(buf, format="JPEG")
    return buf.getvalue()


BLACK, WHITE = _jpeg((0, 0, 0)), _jpeg((255, 255, 255))


@pytest.fixture
def env(monkeypatch):
    """Isolates _motion_tick from the real DB/camera/event bus."""
    sections = {k: copy.deepcopy(v) for k, v in DEFAULTS.items()}
    frames = []
    state = {"source": "device"}
    emitted, triggered = [], []

    monkeypatch.setattr(main.settings_manager, "get_section", lambda name: sections[name])
    monkeypatch.setattr(main.camera, "health", lambda: SimpleNamespace(source=state["source"]))
    monkeypatch.setattr(main.camera, "get_latest_frame", lambda: frames[-1] if frames else None)
    monkeypatch.setattr(main, "_save_snapshot", lambda frame: "/tmp/fake_snapshot.jpg")
    monkeypatch.setattr(main.event_bus, "emit", lambda *a, **k: emitted.append((a, k)) or 1)
    monkeypatch.setattr(
        main.recording_ctrl, "trigger_event", lambda **k: triggered.append(k) or True
    )
    return SimpleNamespace(
        sections=sections, frames=frames, state=state, emitted=emitted, triggered=triggered
    )


def _run_two_frames(env, detector):
    env.frames.append(BLACK)
    main._motion_tick(detector)  # first frame only primes the detector
    env.frames.append(WHITE)
    return main._motion_tick(detector)


def test_motion_emits_event_with_snapshot_bytes_for_telegram(env):
    env.sections["telegram"].update(enabled=True, send_snapshot_on_motion=True)
    _run_two_frames(env, MotionDetector())
    assert len(env.emitted) == 1
    args, kwargs = env.emitted[0]
    assert args[0] == "MotionDetected"
    assert kwargs["extra"] == {"snapshot_bytes": WHITE}
    assert kwargs["snapshot_path"] == "/tmp/fake_snapshot.jpg"


def test_no_snapshot_bytes_when_telegram_photo_option_off(env):
    env.sections["telegram"].update(enabled=True, send_snapshot_on_motion=False)
    _run_two_frames(env, MotionDetector())
    assert env.emitted[0][1]["extra"] is None


def test_no_snapshot_bytes_when_telegram_disabled(env):
    env.sections["telegram"].update(enabled=False, send_snapshot_on_motion=True)
    _run_two_frames(env, MotionDetector())
    assert env.emitted[0][1]["extra"] is None


def test_telegram_still_gets_photo_when_saving_snapshots_is_off(env):
    env.sections["telegram"].update(enabled=True, send_snapshot_on_motion=True)
    env.sections["motion"]["snapshot_on_motion"] = False
    _run_two_frames(env, MotionDetector())
    kwargs = env.emitted[0][1]
    assert kwargs["snapshot_path"] is None
    assert kwargs["extra"] == {"snapshot_bytes": WHITE}


def test_event_recording_mode_triggers_recorder(env):
    env.sections["recording"]["mode"] = "event"
    _run_two_frames(env, MotionDetector())
    assert len(env.triggered) == 1


def test_recording_not_triggered_when_mode_is_not_event(env):
    env.sections["recording"]["mode"] = "off"
    _run_two_frames(env, MotionDetector())
    assert env.triggered == []


def test_disabled_motion_does_nothing_and_slows_polling(env):
    env.sections["motion"]["enabled"] = False
    delay = _run_two_frames(env, MotionDetector())
    assert env.emitted == []
    assert delay == 1.0


def test_placeholder_frames_never_count_as_motion(env):
    """While the camera is disconnected the app serves placeholder frames;
    those must not produce fake motion alerts."""
    env.state["source"] = "synthetic-fallback"
    _run_two_frames(env, MotionDetector())
    assert env.emitted == []


def test_detector_reference_resets_across_a_camera_reconnect(env):
    """First real frame after a reconnect must be a fresh baseline, not
    compared against a stale pre-disconnect frame (which would fire a
    bogus alert) — and detection must work again afterwards."""
    detector = MotionDetector()
    env.frames.append(BLACK)
    main._motion_tick(detector)                  # baseline: black

    env.state["source"] = "synthetic-fallback"   # camera unplugged
    env.frames.append(_jpeg((10, 10, 10)))
    main._motion_tick(detector)
    assert env.emitted == []

    env.state["source"] = "device"               # camera replugged
    env.frames.append(WHITE)
    main._motion_tick(detector)                  # new baseline (white) — no bogus alert
    assert env.emitted == []

    env.frames.append(BLACK)
    main._motion_tick(detector)                  # real change → real alert
    assert len(env.emitted) == 1


# ---- resilience: the worker must never die silently -----------------------

def test_run_resilient_survives_exceptions_and_keeps_running():
    stop = threading.Event()
    calls = []

    def tick():
        calls.append(time.time())
        if len(calls) <= 2:
            raise RuntimeError("transient failure (e.g. sqlite hiccup)")
        return 0.01

    thread = threading.Thread(
        target=main._run_resilient, args=("test", tick, stop, 0.01), daemon=True
    )
    thread.start()
    deadline = time.time() + 3
    while time.time() < deadline and len(calls) < 6:
        time.sleep(0.02)
    stop.set()
    thread.join(timeout=2)
    assert len(calls) >= 6, "loop stopped after an exception instead of carrying on"
    assert not thread.is_alive()


def test_ensure_motion_thread_starts_and_revives_a_dead_worker(monkeypatch):
    started = []
    release = threading.Event()

    def fake_loop():
        started.append(1)
        release.wait(timeout=5)

    monkeypatch.setattr(main, "_motion_loop", fake_loop)
    monkeypatch.setattr(main, "_motion_stop_event", threading.Event())
    monkeypatch.setattr(main, "_motion_thread", None)

    main._ensure_motion_thread()
    time.sleep(0.1)
    assert len(started) == 1

    main._ensure_motion_thread()          # still alive → must not start a second one
    time.sleep(0.1)
    assert len(started) == 1

    release.set()                          # the worker "dies"
    main._motion_thread.join(timeout=2)
    main._ensure_motion_thread()          # → revived
    time.sleep(0.1)
    assert len(started) == 2


def test_ensure_motion_thread_respects_shutdown(monkeypatch):
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(main, "_motion_stop_event", stop)
    monkeypatch.setattr(main, "_motion_thread", None)
    main._ensure_motion_thread()
    assert main._motion_thread is None


def test_changing_motion_settings_revives_worker_and_updates_cooldown(monkeypatch):
    ensured = []
    monkeypatch.setattr(main, "_ensure_motion_thread", lambda: ensured.append(1))
    main._on_settings_changed("motion", {"event_cooldown_seconds": 7})
    assert ensured == [1]
    assert main.event_bus._cooldown_for("MotionDetected") == 7


def test_restarting_camera_settings_revives_worker(monkeypatch):
    ensured = []
    monkeypatch.setattr(main, "_ensure_motion_thread", lambda: ensured.append(1))
    monkeypatch.setattr(main.camera, "stop", lambda: None)
    monkeypatch.setattr(main.camera, "start", lambda: None)
    main._on_settings_changed(
        "camera", {"enabled": True, "device": "/dev/video0", "width": 640, "height": 480, "fps": 15}
    )
    assert ensured == [1]


def test_watchdog_tick_ensures_worker(monkeypatch):
    ensured = []
    monkeypatch.setattr(main, "_ensure_motion_thread", lambda: ensured.append(1))
    assert main._watchdog_tick() == 10.0
    assert ensured == [1]
