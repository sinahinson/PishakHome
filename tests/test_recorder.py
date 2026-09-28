import os
import tempfile
import time
from pathlib import Path

import pytest

from app.recorder import (
    RecordingController,
    StreamRecorder,
    delete_all_recordings,
    delete_all_snapshots,
    enforce_retention,
    list_recording_files,
    record_clip,
)


# ---- record_clip (legacy standalone helper) --------------------------------

def test_record_clip_fails_gracefully_without_device():
    with tempfile.TemporaryDirectory() as tmp:
        result = record_clip(device="/dev/does-not-exist", output_dir=tmp, duration_seconds=1)
        assert result.success is False
        assert "not found" in result.error


# ---- file listing & retention -----------------------------------------------

def test_list_recording_files_empty_dir():
    with tempfile.TemporaryDirectory() as tmp:
        assert list_recording_files(tmp) == []


def test_list_recording_files_sorted_newest_first():
    with tempfile.TemporaryDirectory() as tmp:
        old = Path(tmp) / "old.mkv"
        new = Path(tmp) / "new.mkv"
        old.write_bytes(b"x")
        time.sleep(0.05)
        new.write_bytes(b"xx")
        files = list_recording_files(tmp)
        assert [f.name for f in files] == ["new.mkv", "old.mkv"]


def test_list_recording_files_ignores_other_extensions():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "video.mkv").write_bytes(b"x")
        (Path(tmp) / "notes.txt").write_bytes(b"x")
        files = list_recording_files(tmp)
        assert len(files) == 1
        assert files[0].name == "video.mkv"


def test_enforce_retention_removes_old_files_only():
    with tempfile.TemporaryDirectory() as tmp:
        old_file = Path(tmp) / "old.mkv"
        new_file = Path(tmp) / "new.mkv"
        old_file.write_bytes(b"x")
        new_file.write_bytes(b"x")
        old_time = time.time() - (10 * 86400)
        os.utime(old_file, (old_time, old_time))

        deleted = enforce_retention(tmp, retention_days=7)
        assert str(old_file) in deleted
        assert new_file.exists()
        assert not old_file.exists()


def test_enforce_retention_protects_latest_file():
    with tempfile.TemporaryDirectory() as tmp:
        only_file = Path(tmp) / "current_segment.mkv"
        only_file.write_bytes(b"x")
        old_time = time.time() - (30 * 86400)
        os.utime(only_file, (old_time, old_time))

        deleted = enforce_retention(tmp, retention_days=7, protect_latest=True)
        assert deleted == []
        assert only_file.exists()


def test_enforce_retention_on_missing_dir():
    assert enforce_retention("/definitely/not/a/real/path", retention_days=7) == []


def test_delete_all_recordings_removes_everything():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "a.mkv").write_bytes(b"x")
        (Path(tmp) / "b.mkv").write_bytes(b"x")
        (Path(tmp) / "notes.txt").write_bytes(b"x")
        count = delete_all_recordings(tmp)
        assert count == 2
        assert list_recording_files(tmp) == []
        assert (Path(tmp) / "notes.txt").exists()  # untouched, not a recording


def test_delete_all_recordings_empty_dir():
    with tempfile.TemporaryDirectory() as tmp:
        assert delete_all_recordings(tmp) == 0


def test_delete_all_snapshots_removes_jpgs_only():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "snap1.jpg").write_bytes(b"x")
        (Path(tmp) / "snap2.jpg").write_bytes(b"x")
        (Path(tmp) / "readme.txt").write_bytes(b"x")
        count = delete_all_snapshots(tmp)
        assert count == 2
        remaining = list(Path(tmp).iterdir())
        assert len(remaining) == 1
        assert remaining[0].name == "readme.txt"


def test_delete_all_snapshots_missing_dir():
    assert delete_all_snapshots("/definitely/not/a/real/path") == 0


# ---- StreamRecorder (using a fake ffmpeg) -----------------------------------

@pytest.fixture
def fake_ffmpeg_ok(tmp_path, monkeypatch):
    """A fake ffmpeg that reads stdin until EOF then exits 0, simulating a
    real segment-writing session without needing real ffmpeg/libx264."""
    script = tmp_path / "ffmpeg"
    script.write_text("#!/bin/sh\ncat > /dev/null\nexit 0\n")
    script.chmod(0o755)

    import subprocess as sp
    original_popen = sp.Popen

    def patched(cmd, *args, **kwargs):
        return original_popen([str(script)] + cmd[1:], *args, **kwargs)

    monkeypatch.setattr(sp, "Popen", patched)
    return script


def test_stream_recorder_start_feed_stop(fake_ffmpeg_ok, tmp_path):
    out_dir = tmp_path / "out"
    rec = StreamRecorder(output_dir=str(out_dir), codec="copy", segment_minutes=1)
    assert rec.start() is True
    assert rec.is_active() is True
    rec.feed(b"\xff\xd8fakejpegdata\xff\xd9")
    elapsed = rec.stop()
    assert elapsed is not None
    assert rec.is_active() is False


def test_stream_recorder_double_start_returns_false(fake_ffmpeg_ok, tmp_path):
    rec = StreamRecorder(output_dir=str(tmp_path / "out"))
    assert rec.start() is True
    assert rec.start() is False
    rec.stop()


def test_stream_recorder_feed_after_stop_does_not_raise(fake_ffmpeg_ok, tmp_path):
    rec = StreamRecorder(output_dir=str(tmp_path / "out"))
    rec.start()
    rec.stop()
    rec.feed(b"some bytes")  # must not raise


def test_stream_recorder_status_reports_elapsed(fake_ffmpeg_ok, tmp_path):
    rec = StreamRecorder(output_dir=str(tmp_path / "out"))
    rec.start()
    time.sleep(0.2)
    status = rec.status()
    assert status["active"] is True
    assert status["elapsed_seconds"] > 0
    rec.stop()
    assert rec.status()["active"] is False


def test_stream_recorder_build_command_h264_includes_bitrate(tmp_path):
    rec = StreamRecorder(
        output_dir=str(tmp_path), codec="h264", width=640, height=360,
        fps=10, bitrate_kbps=800,
    )
    cmd = rec._build_command()
    assert "libx264" in cmd
    assert "800k" in cmd
    assert "scale=640:360" in cmd


def test_stream_recorder_build_command_copy_has_no_bitrate_flag(tmp_path):
    rec = StreamRecorder(output_dir=str(tmp_path), codec="copy")
    cmd = rec._build_command()
    assert "copy" in cmd
    assert "libx264" not in cmd


# ---- RecordingController (with a fake camera) -------------------------------

class FakeCamera:
    def __init__(self):
        self.subscribers = []

    def add_frame_subscriber(self, cb):
        self.subscribers.append(cb)

    def remove_frame_subscriber(self, cb):
        if cb in self.subscribers:
            self.subscribers.remove(cb)


@pytest.fixture
def controller(fake_ffmpeg_ok, tmp_path):
    camera = FakeCamera()
    config = {
        "codec": "copy", "width": 0, "height": 0, "fps": 0,
        "bitrate_kbps": 1500, "segment_minutes": 15,
    }
    stops = []
    ctrl = RecordingController(
        camera=camera,
        recordings_dir=str(tmp_path / "recordings"),
        config_provider=lambda: config,
        on_stop=lambda mode, elapsed: stops.append((mode, elapsed)),
    )
    ctrl._test_camera = camera
    ctrl._test_stops = stops
    yield ctrl
    ctrl.stop()


def test_start_continuous_subscribes_camera(controller):
    assert controller.start_continuous() is True
    assert len(controller._test_camera.subscribers) == 1
    status = controller.status()
    assert status["mode"] == "continuous"
    assert status["active"] is True


def test_cannot_start_two_recordings_at_once(controller):
    assert controller.start_continuous() is True
    assert controller.start_manual(5) is False
    assert controller.trigger_event(5, 30) is False


def test_stop_continuous_unsubscribes_and_calls_on_stop(controller):
    controller.start_continuous()
    assert controller.stop_continuous() is True
    assert len(controller._test_camera.subscribers) == 0
    assert controller.status()["mode"] == "off"
    assert controller._test_stops[-1][0] == "continuous"


def test_stop_continuous_when_not_continuous_is_noop(controller):
    assert controller.stop_continuous() is False


def test_manual_recording_stops_itself_after_duration(controller):
    assert controller.start_manual(1) is True
    assert controller.status()["mode"] == "manual"
    time.sleep(1.5)
    assert controller.status()["mode"] == "off"
    assert controller._test_stops[-1][0] == "manual"


def test_trigger_event_starts_and_extends(controller):
    assert controller.trigger_event(post_event_seconds=1, max_event_seconds=30) is True
    assert controller.status()["mode"] == "event"
    # A second motion event should extend the deadline rather than starting a new recorder.
    assert controller.trigger_event(post_event_seconds=1, max_event_seconds=30) is True
    assert controller.status()["mode"] == "event"


def test_event_recording_stops_after_post_event_window(controller):
    controller.trigger_event(post_event_seconds=0.5, max_event_seconds=30)
    assert controller.status()["mode"] == "event"
    time.sleep(1.2)
    assert controller.status()["mode"] == "off"


def test_event_recording_respects_max_event_seconds_cap(controller):
    controller.trigger_event(post_event_seconds=0.3, max_event_seconds=0.6)
    start = time.time()
    # Simulate continued motion *within* the cap window only — re-trigger a
    # few times but stop well before max_event_seconds is reached.
    while time.time() - start < 0.4:
        controller.trigger_event(post_event_seconds=0.3, max_event_seconds=0.6)
        time.sleep(0.1)
    assert controller.status()["mode"] == "event"  # still within the cap

    # No more motion after this point — the hard cap (0.6s from the first
    # trigger) must still end the recording on its own.
    deadline = time.time() + 1.5
    while time.time() < deadline and controller.status()["mode"] != "off":
        time.sleep(0.05)
    assert controller.status()["mode"] == "off"
    assert time.time() - start < 1.5
