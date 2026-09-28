import time

from app.camera import CameraManager


def test_camera_falls_back_when_device_missing():
    cam = CameraManager(device="/dev/does-not-exist", width=320, height=240, fps=10)
    assert cam.device_exists() is False
    cam.start()
    try:
        # Give the synthetic loop a moment to produce a frame.
        for _ in range(50):
            if cam.get_latest_frame() is not None:
                break
            time.sleep(0.05)
        frame = cam.get_latest_frame()
        assert frame is not None
        assert frame.startswith(b"\xff\xd8")  # valid JPEG start-of-image marker

        health = cam.health()
        assert health.source == "synthetic-fallback"
        assert health.available is True
        assert health.error == "device not found"
    finally:
        cam.stop()


def test_camera_health_when_never_started():
    cam = CameraManager(device="/dev/does-not-exist")
    health = cam.health()
    assert health.available is False
    assert health.source == "stopped"


def test_acquire_for_recording_succeeds_immediately_without_real_device():
    # No real device is open, so there's nothing to release: this should
    # return True right away rather than timing out.
    cam = CameraManager(device="/dev/does-not-exist")
    cam.start()
    try:
        assert cam.acquire_for_recording(timeout=1.0) is True
        cam.release_after_recording()
    finally:
        cam.stop()


def test_capture_reports_diagnostics_when_ffmpeg_fails_immediately(monkeypatch, tmp_path):
    # Simulate a real device path existing but ffmpeg being broken/misconfigured,
    # and make sure the manager surfaces a real error instead of going silent.
    fake_device = tmp_path / "video0"
    fake_device.write_text("")  # just needs to exist

    cam = CameraManager(device=str(fake_device), width=320, height=240, fps=10)
    assert cam.device_exists() is True

    # Force ffmpeg_available() True so it takes the "device" branch, but point
    # at a script that exits immediately with an error on stderr.
    monkeypatch.setattr(CameraManager, "ffmpeg_available", staticmethod(lambda: True))

    fake_ffmpeg = tmp_path / "ffmpeg"
    fake_ffmpeg.write_text("#!/bin/sh\necho 'fake ffmpeg: device busy' >&2\nexit 1\n")
    fake_ffmpeg.chmod(0o755)

    import subprocess as sp

    original_popen = sp.Popen

    def patched_popen(cmd, *args, **kwargs):
        cmd = [str(fake_ffmpeg)] + cmd[1:]
        return original_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(sp, "Popen", patched_popen)

    cam.start()
    try:
        deadline = time.time() + 3
        while time.time() < deadline:
            health = cam.health()
            if health.error:
                break
            time.sleep(0.1)
        health = cam.health()
        assert health.source == "synthetic-fallback"
        assert health.error is not None
        assert "device busy" in health.error or "fake ffmpeg" in health.error
        # And it should still be producing *some* frame (the placeholder).
        assert cam.get_latest_frame() is not None
    finally:
        cam.stop()


def test_frame_subscribers_receive_every_frame():
    cam = CameraManager(device="/dev/does-not-exist", width=160, height=90, fps=20)
    received = []
    cam.add_frame_subscriber(lambda frame: received.append(frame))
    cam.start()
    try:
        deadline = time.time() + 2
        while time.time() < deadline and len(received) < 3:
            time.sleep(0.05)
        assert len(received) >= 3
        assert all(f.startswith(b"\xff\xd8") for f in received)
    finally:
        cam.stop()


def test_remove_frame_subscriber_stops_delivery():
    cam = CameraManager(device="/dev/does-not-exist", width=160, height=90, fps=20)
    received = []

    def cb(frame):
        received.append(frame)

    cam.add_frame_subscriber(cb)
    cam.start()
    try:
        time.sleep(0.2)
        cam.remove_frame_subscriber(cb)
        count_after_removal = len(received)
        time.sleep(0.3)
        # A little slack for a frame already in flight, but it must not keep growing.
        assert len(received) <= count_after_removal + 1
    finally:
        cam.stop()


def test_broken_subscriber_does_not_stop_capture():
    cam = CameraManager(device="/dev/does-not-exist", width=160, height=90, fps=20)

    def bad_subscriber(frame):
        raise RuntimeError("boom")

    cam.add_frame_subscriber(bad_subscriber)
    cam.start()
    try:
        deadline = time.time() + 1
        while time.time() < deadline and cam.get_latest_frame() is None:
            time.sleep(0.05)
        assert cam.get_latest_frame() is not None
    finally:
        cam.stop()


def test_permission_denied_is_reported_clearly(monkeypatch, tmp_path):
    fake_device = tmp_path / "video0"
    fake_device.write_text("")

    cam = CameraManager(device=str(fake_device), width=320, height=240, fps=10)
    monkeypatch.setattr(CameraManager, "ffmpeg_available", staticmethod(lambda: True))
    # Simulate a permissions problem regardless of the actual tmp file mode.
    monkeypatch.setattr("app.camera.os.access", lambda *a, **k: False)

    cam.start()
    try:
        deadline = time.time() + 3
        while time.time() < deadline and not cam.health().error:
            time.sleep(0.1)
        health = cam.health()
        assert health.error is not None
        assert "permission denied" in health.error

        for _ in range(20):
            if cam.health().source == "synthetic-fallback":
                break
            time.sleep(0.05)
        assert cam.health().source == "synthetic-fallback"

        # Give the synthetic fallback a moment to actually emit a frame.
        for _ in range(20):
            if cam.get_latest_frame() is not None:
                break
            time.sleep(0.05)
        assert cam.get_latest_frame() is not None  # still shows a placeholder, not nothing
    finally:
        cam.stop()


def test_stalled_ffmpeg_is_detected_and_recovered(monkeypatch, tmp_path):
    # ffmpeg starts and stays alive, but never writes anything to stdout —
    # this is the exact "available: false, error: null forever" symptom.
    import app.camera as camera_module
    monkeypatch.setattr(camera_module, "STARTUP_STALL_SECONDS", 0.3)
    monkeypatch.setattr(camera_module, "READ_POLL_TIMEOUT_SECONDS", 0.1)

    fake_device = tmp_path / "video0"
    fake_device.write_text("")

    cam = CameraManager(device=str(fake_device), width=320, height=240, fps=10)
    monkeypatch.setattr(CameraManager, "ffmpeg_available", staticmethod(lambda: True))

    fake_ffmpeg = tmp_path / "ffmpeg"
    # Sleeps well past the stall timeout without writing anything or exiting.
    fake_ffmpeg.write_text("#!/bin/sh\nsleep 5\n")
    fake_ffmpeg.chmod(0o755)

    import subprocess as sp
    original_popen = sp.Popen

    def patched_popen(cmd, *args, **kwargs):
        return original_popen([str(fake_ffmpeg)] + cmd[1:], *args, **kwargs)

    monkeypatch.setattr(sp, "Popen", patched_popen)

    cam.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not cam.health().error:
            time.sleep(0.1)
        health = cam.health()
        assert health.error is not None
        assert "no frames received" in health.error or "stall" in health.error.lower()
    finally:
        cam.stop()


def test_concurrent_stop_during_active_capture_does_not_crash(monkeypatch, tmp_path, caplog):
    """Regression test: calling stop() from the main thread while the
    capture thread is mid-loop used to race on self._proc, causing an
    'AttributeError: NoneType object has no attribute poll' to be logged
    (caught, non-fatal, but a real bug) during a fast startup/shutdown
    cycle. This reproduces that race directly."""
    fake_device = tmp_path / "video0"
    fake_device.write_text("")

    cam = CameraManager(device=str(fake_device), width=160, height=90, fps=20)
    monkeypatch.setattr(CameraManager, "ffmpeg_available", staticmethod(lambda: True))

    # A fake ffmpeg that streams JPEG-ish frames slowly and keeps running
    # until killed, so the capture loop is definitely mid-read when stop()
    # is called from the main thread.
    fake_ffmpeg = tmp_path / "ffmpeg"
    fake_ffmpeg.write_text(
        "#!/bin/sh\n"
        "while true; do printf '\\377\\330fakejpegdata\\377\\331'; sleep 0.02; done\n"
    )
    fake_ffmpeg.chmod(0o755)

    import subprocess as sp
    original_popen = sp.Popen

    def patched_popen(cmd, *args, **kwargs):
        return original_popen([str(fake_ffmpeg)] + cmd[1:], *args, **kwargs)

    monkeypatch.setattr(sp, "Popen", patched_popen)

    import logging
    caplog.set_level(logging.ERROR, logger="pishak.camera")

    cam.start()
    # Let it get well into an active capture session first.
    deadline = time.time() + 2
    while time.time() < deadline and cam.get_latest_frame() is None:
        time.sleep(0.02)
    assert cam.get_latest_frame() is not None

    # Now stop it repeatedly in quick succession from this thread while the
    # capture thread is actively reading — this is what triggered the race.
    for _ in range(5):
        cam.stop()
        cam.start()
        time.sleep(0.05)
    cam.stop()

    crashed = [r for r in caplog.records if "NoneType" in r.getMessage() or "has no attribute" in r.getMessage()]
    assert not crashed, f"camera logged an AttributeError race during stop(): {[r.getMessage() for r in crashed]}"


def test_restart_forces_fresh_reconnect_attempt():
    cam = CameraManager(device="/dev/does-not-exist", width=160, height=90, fps=20)
    cam.start()
    try:
        deadline = time.time() + 1
        while time.time() < deadline and cam.get_latest_frame() is None:
            time.sleep(0.02)
        assert cam.get_latest_frame() is not None
        first_source = cam.health().source
        assert first_source == "synthetic-fallback"

        cam.restart()
        deadline = time.time() + 1
        while time.time() < deadline and cam.get_latest_frame() is None:
            time.sleep(0.02)
        assert cam.get_latest_frame() is not None
        assert cam.health().error == "device not found"
    finally:
        cam.stop()
