"""
Pishak Home — recording subsystem.

Key design decision: recording NEVER opens a second connection to the
camera device. Every recording (manual, motion-triggered, or continuous
24/7) is fed from the exact same frame stream `CameraManager` is already
capturing for live view, via `CameraManager.add_frame_subscriber()`. This
is both simpler and more robust than opening `ffmpeg` against
`/dev/video0` a second time — most USB UVC webcams only allow one process
to hold the device open at all, which was the root cause of an earlier bug
in this project (see README changelog).

`StreamRecorder` wraps one `ffmpeg` process that reads MJPEG frames from
stdin and writes them out, optionally re-encoded to control file size.
`ffmpeg`'s segment muxer handles file rotation, which is what makes
continuous 24/7 recording practical on limited storage — and what makes
the Recordings page just "list files in a directory" rather than needing
its own bookkeeping for every clip.

`RecordingController` is the single point main.py talks to: it decides
whether a `StreamRecorder` should exist right now (continuous / event /
manual), and guarantees only one is ever active, since running two
`ffmpeg` re-encodes at once would be rough on a Pi 3B+.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("pishak.recorder")


@dataclass
class RecordingFile:
    name: str
    path: str
    size_bytes: int
    modified_at: float


def list_recording_files(directory: str) -> list[RecordingFile]:
    """Filesystem is the source of truth for what recordings exist —
    this naturally includes continuous-mode segments that ffmpeg creates
    directly, with no extra bookkeeping needed."""
    d = Path(directory)
    if not d.exists():
        return []
    files = []
    for f in d.iterdir():
        if f.is_file() and f.suffix.lower() in (".mkv", ".mp4"):
            stat = f.stat()
            files.append(RecordingFile(f.name, str(f), stat.st_size, stat.st_mtime))
    files.sort(key=lambda r: r.modified_at, reverse=True)
    return files


def enforce_retention(
    recordings_dir: str, retention_days: int, protect_latest: bool = False
) -> list[str]:
    """Deletes recording files older than `retention_days`.

    protect_latest=True skips the single most-recently-modified file even
    if it's technically past the cutoff — used while a recording is
    actively in progress, since its current segment file is still being
    written to.
    """
    cutoff = time.time() - (retention_days * 86400)
    deleted: list[str] = []
    files = list_recording_files(recordings_dir)
    if protect_latest and files:
        files = files[1:]  # first entry is the newest (sorted desc above)
    for f in files:
        if f.modified_at < cutoff:
            try:
                Path(f.path).unlink()
                deleted.append(f.path)
            except OSError as exc:
                logger.warning("Could not remove %s: %s", f.path, exc)
    if deleted:
        logger.info("Retention cleanup removed %d recording(s)", len(deleted))
    return deleted


def delete_all_recordings(recordings_dir: str) -> int:
    """Deletes every recording file. Caller is responsible for checking
    that no recording is currently active before calling this — deleting
    a file an active ffmpeg process still has open is asking for trouble."""
    files = list_recording_files(recordings_dir)
    count = 0
    for f in files:
        try:
            Path(f.path).unlink()
            count += 1
        except OSError as exc:
            logger.warning("Could not remove %s: %s", f.path, exc)
    if count:
        logger.info("Deleted %d recording(s) at user request", count)
    return count


def delete_all_snapshots(snapshots_dir: str) -> int:
    d = Path(snapshots_dir)
    if not d.exists():
        return 0
    count = 0
    for f in d.glob("*.jpg"):
        try:
            f.unlink()
            count += 1
        except OSError as exc:
            logger.warning("Could not remove %s: %s", f, exc)
    if count:
        logger.info("Deleted %d snapshot(s) at user request", count)
    return count


def record_clip(
    device: str,
    output_dir: str,
    duration_seconds: int,
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
    filename_prefix: str = "clip",
) -> "ClipResult":
    """Standalone helper kept for the manual hardware sanity check (see
    README) — opens the device directly for a fixed duration. The running
    application does NOT use this for its own recording features (see
    module docstring); use StreamRecorder/RecordingController instead.
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{filename_prefix}_{int(time.time())}.mkv"
    out_path = out_dir / filename

    if not Path(device).exists():
        return ClipResult(
            path=str(out_path), duration_seconds=0, size_bytes=0,
            success=False, error=f"camera device {device} not found",
        )

    cmd = [
        "ffmpeg", "-y",
        "-f", "v4l2",
        "-input_format", "mjpeg",
        "-video_size", f"{width}x{height}",
        "-framerate", str(fps),
        "-i", device,
        "-t", str(duration_seconds),
        "-c:v", "copy",
        str(out_path),
    ]
    start = time.time()
    try:
        subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=duration_seconds + 15, check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.error("Recording failed: %s", exc)
        return ClipResult(
            path=str(out_path), duration_seconds=time.time() - start,
            size_bytes=0, success=False, error=str(exc),
        )
    size = out_path.stat().st_size if out_path.exists() else 0
    return ClipResult(
        path=str(out_path), duration_seconds=round(time.time() - start, 2),
        size_bytes=size, success=size > 0,
    )


@dataclass
class ClipResult:
    path: str
    duration_seconds: float
    size_bytes: int
    success: bool
    error: Optional[str] = None


class StreamRecorder:
    """Owns one ffmpeg process that receives already-captured MJPEG frames
    over stdin and writes them to disk, optionally re-encoded to control
    file size, and automatically segmented into multiple files."""

    def __init__(
        self,
        output_dir: str,
        codec: str = "copy",
        width: int = 0,
        height: int = 0,
        fps: int = 0,
        bitrate_kbps: int = 1500,
        segment_minutes: float = 15,
        filename_prefix: str = "rec",
    ):
        self.output_dir = Path(output_dir)
        self.codec = codec
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate_kbps = bitrate_kbps
        self.segment_minutes = max(segment_minutes, 0.25)
        self.filename_prefix = filename_prefix

        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._started_at: Optional[float] = None
        self._write_errors = 0

    def is_active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _build_command(self) -> list[str]:
        pattern = str(self.output_dir / f"{self.filename_prefix}_%Y%m%d_%H%M%S.mkv")
        cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "warning",
               "-f", "mjpeg", "-i", "pipe:0"]

        if self.width and self.height:
            cmd += ["-vf", f"scale={self.width}:{self.height}"]
        if self.fps:
            cmd += ["-r", str(self.fps)]

        if self.codec == "h264":
            cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-b:v", f"{self.bitrate_kbps}k"]
        else:
            cmd += ["-c:v", "copy"]

        cmd += [
            "-f", "segment",
            "-segment_time", str(int(self.segment_minutes * 60)),
            "-reset_timestamps", "1",
            "-strftime", "1",
            pattern,
        ]
        return cmd

    def start(self) -> bool:
        with self._lock:
            if self.is_active():
                return False
            self.output_dir.mkdir(parents=True, exist_ok=True)
            cmd = self._build_command()
            try:
                self._proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            except OSError as exc:
                logger.error("Failed to start recorder: %s", exc)
                return False
            self._started_at = time.time()
            self._write_errors = 0
            threading.Thread(target=self._drain_stderr, args=(self._proc,), daemon=True).start()
            return True

    def feed(self, frame: bytes) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(frame)
        except (BrokenPipeError, OSError):
            self._write_errors += 1

    def stop(self) -> Optional[float]:
        with self._lock:
            proc, self._proc = self._proc, None
            started_at, self._started_at = self._started_at, None
        if proc is None:
            return None
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        return (time.time() - started_at) if started_at else None

    def status(self) -> dict[str, Any]:
        return {
            "active": self.is_active(),
            "elapsed_seconds": round(time.time() - self._started_at, 1) if self._started_at else 0,
            "write_errors": self._write_errors,
        }

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            assert proc.stderr is not None
            for _ in iter(proc.stderr.readline, b""):
                pass  # discarded; StreamRecorder failures surface via is_active()/write_errors
        except Exception:  # pragma: no cover - best-effort
            pass


class RecordingController:
    """The single point of control for recording. Guarantees at most one
    StreamRecorder is active at a time, across manual/event/continuous
    triggers, and manages the camera subscription lifecycle for it."""

    def __init__(
        self,
        camera: Any,  # app.camera.CameraManager — typed loosely to avoid an import cycle in tests
        recordings_dir: str,
        config_provider: Callable[[], dict[str, Any]],
        on_stop: Optional[Callable[[str, Optional[float]], None]] = None,
    ):
        self.camera = camera
        self.recordings_dir = recordings_dir
        self.config_provider = config_provider
        self.on_stop = on_stop

        self._lock = threading.Lock()
        self._recorder: Optional[StreamRecorder] = None
        self._mode = "off"  # off | continuous | event | manual
        self._event_start: Optional[float] = None
        self._event_deadline: Optional[float] = None
        self._stop_monitor = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None

    def _build_recorder(self, prefix: str, segment_minutes: float) -> StreamRecorder:
        cfg = self.config_provider()
        width = cfg.get("width") or 0
        height = cfg.get("height") or 0
        fps = cfg.get("fps") or 0
        return StreamRecorder(
            output_dir=self.recordings_dir,
            codec=cfg.get("codec", "copy"),
            width=width,
            height=height,
            fps=fps,
            bitrate_kbps=cfg.get("bitrate_kbps", 1500),
            segment_minutes=segment_minutes,
            filename_prefix=prefix,
        )

    def status(self) -> dict[str, Any]:
        with self._lock:
            mode = self._mode
            rec = self._recorder
        base = rec.status() if rec else {"active": False, "elapsed_seconds": 0, "write_errors": 0}
        return {"mode": mode, **base}

    def start_continuous(self) -> bool:
        with self._lock:
            if self._recorder is not None:
                return False
            cfg = self.config_provider()
            rec = self._build_recorder("continuous", cfg.get("segment_minutes", 15))
            if not rec.start():
                return False
            self.camera.add_frame_subscriber(rec.feed)
            self._recorder = rec
            self._mode = "continuous"
        return True

    def stop_continuous(self) -> bool:
        with self._lock:
            if self._mode != "continuous":
                return False
        return self._stop_current()

    def start_manual(self, duration_seconds: int) -> bool:
        with self._lock:
            if self._recorder is not None:
                return False
            segment_minutes = max(1.0, (duration_seconds / 60.0) + 0.5)
            rec = self._build_recorder("manual", segment_minutes)
            if not rec.start():
                return False
            self.camera.add_frame_subscriber(rec.feed)
            self._recorder = rec
            self._mode = "manual"
        threading.Timer(duration_seconds, self._stop_current).start()
        return True

    def trigger_event(self, post_event_seconds: float, max_event_seconds: float) -> bool:
        """Called by the motion-detection loop. If an event recording is
        already running, extends its deadline instead of starting a new
        one; if continuous/manual recording already owns the recorder,
        does nothing (that recording already covers this moment)."""
        with self._lock:
            now = time.time()
            if self._mode == "event" and self._recorder is not None:
                capped_deadline = (self._event_start or now) + max_event_seconds
                self._event_deadline = min(now + post_event_seconds, capped_deadline)
                return True
            if self._recorder is not None:
                return False  # continuous/manual already active; don't preempt it

            segment_minutes = max(1.0, (max_event_seconds / 60.0) + 0.5)
            rec = self._build_recorder("event", segment_minutes)
            if not rec.start():
                return False
            self.camera.add_frame_subscriber(rec.feed)
            self._recorder = rec
            self._mode = "event"
            self._event_start = now
            self._event_deadline = now + post_event_seconds

        self._stop_monitor.clear()
        self._monitor_thread = threading.Thread(target=self._event_monitor_loop, daemon=True)
        self._monitor_thread.start()
        return True

    def _event_monitor_loop(self) -> None:
        while not self._stop_monitor.is_set():
            with self._lock:
                deadline = self._event_deadline
                still_event_mode = self._mode == "event"
            if deadline is None or not still_event_mode:
                return
            if time.time() >= deadline:
                self._stop_current()
                return
            time.sleep(0.5)

    def _stop_current(self) -> bool:
        with self._lock:
            rec = self._recorder
            mode = self._mode
            if rec is None:
                return False
            self.camera.remove_frame_subscriber(rec.feed)
            self._recorder = None
            self._mode = "off"
            self._event_start = None
            self._event_deadline = None
        elapsed = rec.stop()
        self._stop_monitor.set()
        if self.on_stop:
            try:
                self.on_stop(mode, elapsed)
            except Exception:  # pragma: no cover - defensive
                logger.exception("on_stop callback failed")
        return True

    def stop(self) -> None:
        """Unconditional stop, regardless of current mode — used on shutdown."""
        self._stop_current()
