"""
Pishak Home — event bus.

Central place where "things that happened" (MotionDetected, CameraOffline,
StorageLow, SystemStarted, ...) get persisted to the database and, subject
to a per-event-type cooldown, forwarded to any registered notifiers
(Telegram, future push notifications, etc).

Notifiers do network I/O (Telegram, possibly through a slow proxy), so in
the running app they are dispatched from a small background worker
(`async_dispatch=True`) instead of inline. Otherwise a slow or unreachable
Telegram would block whoever emitted the event — for MotionDetected that
would freeze motion detection itself. Tests use the default synchronous
mode so assertions can run right after `emit()`.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable, Optional

from app.database import Database

logger = logging.getLogger("pishak.events")

Notifier = Callable[[str, dict[str, Any]], None]


class EventBus:
    def __init__(
        self,
        db: Database,
        default_cooldown_seconds: float = 60.0,
        async_dispatch: bool = False,
        max_queue: int = 50,
    ):
        self.db = db
        self.default_cooldown_seconds = default_cooldown_seconds
        self._cooldowns: dict[str, float] = {}
        self._last_fired: dict[str, float] = {}
        self._notifiers: list[Notifier] = []

        self._async = async_dispatch
        self._queue: "queue.Queue[Optional[tuple[str, dict[str, Any]]]]" = queue.Queue(maxsize=max_queue)
        self._worker: Optional[threading.Thread] = None
        self._worker_lock = threading.Lock()

    def set_cooldown(self, event_type: str, seconds: float) -> None:
        self._cooldowns[event_type] = seconds

    def register_notifier(self, notifier: Notifier) -> None:
        self._notifiers.append(notifier)

    def _cooldown_for(self, event_type: str) -> float:
        return self._cooldowns.get(event_type, self.default_cooldown_seconds)

    def _should_notify(self, event_type: str) -> bool:
        now = time.time()
        last = self._last_fired.get(event_type)
        cooldown = self._cooldown_for(event_type)
        if last is not None and (now - last) < cooldown:
            return False
        self._last_fired[event_type] = now
        return True

    # ---- dispatch ----------------------------------------------------------

    def _run_notifiers(self, event_type: str, payload: dict[str, Any]) -> None:
        for notifier in list(self._notifiers):
            try:
                notifier(event_type, payload)
            except Exception:
                logger.exception("Notifier failed for event %s", event_type)

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._worker_loop, daemon=True, name="event-dispatch"
                )
                self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            event_type, payload = item
            self._run_notifiers(event_type, payload)

    def _dispatch(self, event_type: str, payload: dict[str, Any]) -> None:
        if not self._async:
            self._run_notifiers(event_type, payload)
            return
        self._ensure_worker()
        try:
            self._queue.put_nowait((event_type, payload))
        except queue.Full:
            logger.warning("Notification queue full — dropping %s notification", event_type)

    def close(self) -> None:
        """Stops the background worker (if any). Safe to call repeatedly;
        emitting again afterwards transparently restarts the worker."""
        with self._worker_lock:
            worker = self._worker
            self._worker = None
        if worker is not None and worker.is_alive():
            try:
                self._queue.put_nowait(None)
            except queue.Full:  # pragma: no cover - worker is daemon anyway
                return
            worker.join(timeout=3)

    # ---- public API --------------------------------------------------------

    def emit(
        self,
        event_type: str,
        camera_id: Optional[str] = None,
        zone: Optional[str] = None,
        confidence: Optional[float] = None,
        snapshot_path: Optional[str] = None,
        recording_path: Optional[str] = None,
        meta: Optional[dict[str, Any]] = None,
        notify: bool = True,
        extra: Optional[dict[str, Any]] = None,
    ) -> int:
        """`extra` is handed to notifiers only (e.g. the raw JPEG bytes of a
        snapshot for Telegram) and is never written to the database."""
        event_id = self.db.add_event(
            event_type=event_type,
            camera_id=camera_id,
            zone=zone,
            confidence=confidence,
            snapshot_path=snapshot_path,
            recording_path=recording_path,
            meta=meta,
        )
        logger.info("event: %s (id=%s, zone=%s)", event_type, event_id, zone)

        if notify and self._should_notify(event_type):
            payload: dict[str, Any] = {
                "event_id": event_id,
                "event_type": event_type,
                "camera_id": camera_id,
                "zone": zone,
                "confidence": confidence,
                "snapshot_path": snapshot_path,
            }
            if extra:
                payload.update(extra)
            self._dispatch(event_type, payload)
        return event_id
