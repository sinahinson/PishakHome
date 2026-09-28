"""
Pishak Home — camera module.

Captures the webcam's native MJPEG stream via a single long-running
`ffmpeg` subprocess (stream copy, no transcoding — see project rules) and
keeps only the *latest* decoded JPEG frame in memory. Every consumer
(live view, snapshot, motion detection) reads that shared latest frame
instead of each opening its own ffmpeg process.

Two things this version is careful about, because USB UVC webcams
generally only allow ONE process to have the device open at a time:

  1. If the live-capture ffmpeg process dies for any reason, we capture
     its real stderr (not /dev/null) so the actual cause shows up in
     `health().error` and the logs, and we always fall back to visible
     synthetic frames + automatic retry with backoff — never silent
     nothing.
  2. Recording (`app/recorder.py`'s `record_clip`) needs the device too.
     `acquire_for_recording()` / `release_after_recording()` pause the
     live-capture loop and release the device first, so the two ffmpeg
     processes never fight over `/dev/video0` at the same time.
"""

from __future__ import annotations

import io
import logging
import os
import select
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("pishak.camera")

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"

INITIAL_BACKOFF_SECONDS = 2
MAX_BACKOFF_SECONDS = 30
READ_POLL_TIMEOUT_SECONDS = 1.0   # how often we re-check stop/pause/stall while blocked on I/O
STARTUP_STALL_SECONDS = 8.0       # no frame at all yet since process start
RUNNING_STALL_SECONDS = 6.0       # had frames before, then they stopped coming


@dataclass
class CameraHealth:
    available: bool
    source: str  # "device" | "synthetic-fallback" | "stopped" | "paused"
    device: str
    width: int
    height: int
    fps: int
    last_frame_age_seconds: Optional[float]
    error: Optional[str] = None
    recent_log: Optional[str] = None


def _make_synthetic_jpeg(width: int, height: int, tick: int, label: str = "NO CAMERA SIGNAL") -> bytes:
    """Cheap placeholder frame used whenever no real camera frame is available."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), color=(17, 17, 17))
    draw = ImageDraw.Draw(img)
    draw.text((10, height // 2 - 10), f"{label}  ({tick})", fill=(230, 184, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=60)
    return buf.getvalue()


class CameraManager:
    def __init__(
        self,
        device: str = "/dev/video0",
        width: int = 1920,
        height: int = 1080,
        fps: int = 30,
    ):
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps

        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set = please release the device
        self._device_free_event = threading.Event()
        self._device_free_event.set()  # nothing to free until we actually open it

        self._lock = threading.Lock()
        self._latest_frame: Optional[bytes] = None
        self._latest_frame_time: float = 0.0
        self._source = "stopped"
        self._error: Optional[str] = None
        self._synthetic_tick = 0
        self._stderr_lines: deque[str] = deque(maxlen=40)

        self._subscribers_lock = threading.Lock()
        self._frame_subscribers: list = []

    # ---- device / binary detection -------------------------------------

    def device_exists(self) -> bool:
        return Path(self.device).exists()

    @staticmethod
    def ffmpeg_available() -> bool:
        return shutil.which("ffmpeg") is not None

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._pause_event.clear()
        if self.device_exists() and self.ffmpeg_available():
            self._thread = threading.Thread(target=self._supervisor_loop, daemon=True)
        else:
            reason = "device not found" if not self.device_exists() else "ffmpeg not found"
            logger.warning(
                "Camera %s unavailable (%s); using synthetic fallback frames",
                self.device,
                reason,
            )
            self._error = reason
            self._thread = threading.Thread(target=self._run_synthetic_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._pause_event.clear()
        self._terminate_proc()
        if self._thread:
            self._thread.join(timeout=5)
        self._source = "stopped"

    def restart(self) -> None:
        """Forces an immediate reconnect attempt instead of waiting out
        the current backoff delay — e.g. after unplugging/replugging the
        USB webcam. A fresh supervisor thread also resets the backoff
        timer back to its initial value."""
        self.stop()
        self._error = None
        self.start()

    # ---- exclusive access for recording ----------------------------------

    def acquire_for_recording(self, timeout: float = 5.0) -> bool:
        """Pauses live capture and waits for the device to actually be free.

        Call this before opening a second ffmpeg process against the same
        device (e.g. for a manual recording), and always pair it with
        `release_after_recording()` in a `finally` block.
        """
        self._pause_event.set()
        return self._device_free_event.wait(timeout=timeout)

    def release_after_recording(self) -> None:
        self._pause_event.clear()

    # ---- frame subscribers (used by recording, without a second device open) --

    def add_frame_subscriber(self, callback) -> None:
        with self._subscribers_lock:
            if callback not in self._frame_subscribers:
                self._frame_subscribers.append(callback)

    def remove_frame_subscriber(self, callback) -> None:
        with self._subscribers_lock:
            if callback in self._frame_subscribers:
                self._frame_subscribers.remove(callback)

    def _publish_frame(self, frame: bytes) -> None:
        with self._lock:
            self._latest_frame = frame
            self._latest_frame_time = time.time()
        with self._subscribers_lock:
            subscribers = list(self._frame_subscribers)
        for callback in subscribers:
            try:
                callback(frame)
            except Exception:  # pragma: no cover - a broken subscriber must not kill capture
                logger.exception("Frame subscriber raised an exception")

    # ---- capture loops ----------------------------------------------------

    def _run_synthetic_loop(self) -> None:
        """Used when there is no real device at all (dev/test environments,
        or the webcam unplugged). Runs until stop()."""
        self._source = "synthetic-fallback"
        interval = 1.0 / max(self.fps, 1)
        while not self._stop_event.is_set():
            self._emit_synthetic_frame()
            time.sleep(interval)

    def _run_synthetic_for(self, seconds: float) -> None:
        """Bounded synthetic-frame period used between reconnect attempts
        after the real ffmpeg process has failed."""
        deadline = time.time() + seconds
        interval = 1.0 / max(self.fps, 1)
        while (
            time.time() < deadline
            and not self._stop_event.is_set()
            and not self._pause_event.is_set()
        ):
            self._emit_synthetic_frame()
            time.sleep(interval)

    def _emit_synthetic_frame(self) -> None:
        self._synthetic_tick += 1
        label = "CAMERA ERROR" if self._error else "NO CAMERA SIGNAL"
        frame = _make_synthetic_jpeg(
            min(self.width, 640), min(self.height, 360), self._synthetic_tick, label
        )
        self._publish_frame(frame)

    def _supervisor_loop(self) -> None:
        """Owns the device's lifetime: opens ffmpeg, restarts it with
        backoff if it dies unexpectedly, and releases it whenever a
        recording needs exclusive access."""
        backoff = INITIAL_BACKOFF_SECONDS
        while not self._stop_event.is_set():
            if self._pause_event.is_set():
                self._source = "paused"
                self._device_free_event.set()
                time.sleep(0.2)
                continue

            self._device_free_event.clear()
            exited_cleanly = self._capture_once()
            self._device_free_event.set()

            if self._stop_event.is_set() or self._pause_event.is_set():
                continue  # top of loop will handle stop/pause correctly

            if not exited_cleanly:
                tail = self._stderr_tail()
                logger.warning(
                    "Live camera capture stopped unexpectedly, retrying in %ss. ffmpeg said: %s",
                    backoff, tail or "(no stderr captured)",
                )
                with self._lock:
                    self._error = tail or "ffmpeg exited unexpectedly"
                    self._source = "synthetic-fallback"
                self._run_synthetic_for(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            else:
                backoff = INITIAL_BACKOFF_SECONDS

        self._device_free_event.set()

    def _capture_once(self) -> bool:
        """Runs one ffmpeg capture session until it exits, stop() is called,
        a recording asks to pause, or the process stalls (spawns but never
        produces frames — e.g. permission denied, device busy, or a bad
        format negotiation that hangs instead of erroring out).

        Returns True only if it stopped because *we* asked it to
        (stop/pause), False if ffmpeg died or was killed for stalling.
        """
        if not os.access(self.device, os.R_OK | os.W_OK):
            self._stderr_lines.append(
                f"permission denied opening {self.device} "
                f"(is this user in the 'video' group? try: sudo usermod -aG video $USER, then re-login)"
            )
            return False

        cmd = [
            "ffmpeg",
            "-hide_banner", "-nostats", "-loglevel", "warning",
            "-f", "v4l2",
            "-input_format", "mjpeg",
            "-video_size", f"{self.width}x{self.height}",
            "-framerate", str(self.fps),
            "-i", self.device,
            "-c:v", "copy",
            "-f", "mjpeg",
            "-",
        ]
        self._source = "device"
        self._error = None
        self._stderr_lines.clear()

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=10**6
            )
        except OSError as exc:  # ffmpeg binary missing/unrunnable
            self._stderr_lines.append(str(exc))
            return False

        # IMPORTANT: from here on, use this local `proc` reference, never
        # `self._proc`, inside this function's read loop. `self._proc` can
        # be concurrently cleared by stop()/acquire_for_recording() running
        # on another thread (that's how they signal/terminate this session);
        # racing that against reads here caused an
        # AttributeError: 'NoneType' object has no attribute 'poll'
        # during shutdown. `self._proc` is only set so external callers can
        # find and terminate the process; this loop must not depend on it.
        self._proc = proc

        stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(proc,), daemon=True
        )
        stderr_thread.start()

        buffer = b""
        requested_stop = False
        session_start = time.time()
        last_frame_received = session_start
        got_any_frame = False

        try:
            assert proc.stdout is not None
            stdout_fd = proc.stdout
            while True:
                if self._stop_event.is_set() or self._pause_event.is_set():
                    requested_stop = True
                    break

                ready, _, _ = select.select([stdout_fd], [], [], READ_POLL_TIMEOUT_SECONDS)

                if not ready:
                    # No data arrived this tick — check for a stall before looping again.
                    if proc.poll() is not None:
                        break
                    stall_limit = RUNNING_STALL_SECONDS if got_any_frame else STARTUP_STALL_SECONDS
                    if time.time() - last_frame_received > stall_limit:
                        self._stderr_lines.append(
                            f"no frames received for {stall_limit:.0f}s "
                            f"({'stream stalled' if got_any_frame else 'nothing received since start'}) "
                            f"— killing and retrying"
                        )
                        break
                    continue

                chunk = stdout_fd.read1(4096)
                if not chunk:
                    if proc.poll() is not None:
                        break
                    continue

                buffer += chunk
                start = buffer.find(JPEG_SOI)
                end = buffer.find(JPEG_EOI, start + 2) if start != -1 else -1
                if start != -1 and end != -1:
                    frame = buffer[start : end + 2]
                    buffer = buffer[end + 2 :]
                    self._publish_frame(frame)
                    last_frame_received = time.time()
                    got_any_frame = True
                if len(buffer) > 5_000_000:
                    # Safety valve: never let an unsynced buffer grow forever.
                    buffer = b""
        except Exception as exc:  # pragma: no cover - defensive, hardware-dependent
            logger.exception("Camera capture loop failed: %s", exc)
            self._stderr_lines.append(str(exc))
        finally:
            exit_code = self._terminate_proc(proc)
            stderr_thread.join(timeout=1)
            if exit_code not in (None, 0) and not requested_stop:
                self._stderr_lines.append(f"ffmpeg exited with code {exit_code}")

        if requested_stop:
            return True
        # A clean stop was requested via stop/pause above; anything else
        # (stall, non-zero exit, exception) counts as a real failure.
        return False

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            assert proc.stderr is not None
            for raw_line in iter(proc.stderr.readline, b""):
                line = raw_line.decode(errors="replace").rstrip()
                if line:
                    self._stderr_lines.append(line)
        except Exception:  # pragma: no cover - best-effort diagnostics only
            pass

    def _stderr_tail(self, n: int = 5) -> str:
        return " | ".join(list(self._stderr_lines)[-n:])

    def _terminate_proc(self, proc: Optional[subprocess.Popen] = None) -> Optional[int]:
        """Terminates a capture process. Called both by external code
        (stop()/acquire_for_recording(), on whatever process self._proc
        currently points to) and by _capture_once's own finally block (on
        the specific process it started, via the `proc` argument) — these
        two call sites can race, so this must be safe to invoke twice on
        the same, or on an already-dead, process without raising.
        """
        if proc is None:
            proc, self._proc = self._proc, None
        elif self._proc is proc:
            self._proc = None

        if proc is None:
            return None
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            return proc.returncode
        except (OSError, ValueError):
            # Process already reaped/gone by the time we got here (the other
            # call site won the race) — nothing left to do.
            return proc.returncode if proc.returncode is not None else None

    # ---- public reads -------------------------------------------------

    def get_latest_frame(self) -> Optional[bytes]:
        with self._lock:
            return self._latest_frame

    def health(self) -> CameraHealth:
        with self._lock:
            age = (
                (time.time() - self._latest_frame_time)
                if self._latest_frame_time
                else None
            )
        return CameraHealth(
            available=self._latest_frame is not None,
            source=self._source,
            device=self.device,
            width=self.width,
            height=self.height,
            fps=self.fps,
            last_frame_age_seconds=round(age, 2) if age is not None else None,
            error=self._error,
            recent_log=self._stderr_tail() or None,
        )

    def mjpeg_multipart_generator(self, target_fps: Optional[int] = None):
        """Yields multipart/x-mixed-replace chunks for a browser <img> tag."""
        interval = 1.0 / max(target_fps or self.fps, 1)
        boundary = b"--frame"
        while True:
            frame = self.get_latest_frame()
            if frame is not None:
                yield (
                    boundary
                    + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode()
                    + b"\r\n\r\n"
                    + frame
                    + b"\r\n"
                )
            time.sleep(interval)
