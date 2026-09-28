"""
Pishak Home — main FastAPI application.

Wires together every module: camera, database, settings, auth, event bus,
motion detection, recording controller, network monitor, and Telegram
notifications. One process, modular internals — per the project's
"one app, not microservices" rule.
"""

from __future__ import annotations

import hmac
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth import SESSION_COOKIE_NAME, AuthManager, Session
from app.camera import CameraManager
from app.config import settings as static_settings
from app.config import load_seed_sections
from app.database import Database
from app.detection import MotionDetector
from app.events import EventBus
from app.network import NetworkMonitor
from app.notifications import TelegramNotifier, build_notifier
from app.recorder import (
    RecordingController,
    delete_all_recordings,
    delete_all_snapshots,
    enforce_retention,
    list_recording_files,
)
from app.settings_manager import SettingsManager
from app.system import get_system_status, is_storage_low

BASE_DIR = Path(__file__).resolve().parent.parent
RECORDINGS_DIR = BASE_DIR / static_settings["paths"]["recordings_dir"]
SNAPSHOTS_DIR = BASE_DIR / static_settings["paths"]["snapshots_dir"]

for sub in ("logs", "data"):
    (BASE_DIR / sub).mkdir(parents=True, exist_ok=True)
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=static_settings["app"]["log_level"].upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(BASE_DIR / "logs" / "pishak.log")],
)
logger = logging.getLogger("pishak.main")

# httpx logs every request URL at INFO — and Telegram's URL contains the bot
# token. Keep those loggers quiet so the token never lands in logs/pishak.log.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# ---- shared component instances --------------------------------------------

db = Database(BASE_DIR / static_settings["database"]["path"])
settings_manager = SettingsManager(db, seed=load_seed_sections())
auth_manager = AuthManager(settings_manager)
event_bus = EventBus(db, default_cooldown_seconds=60, async_dispatch=True)
event_bus.set_cooldown("MotionDetected", settings_manager.get_section("motion")["event_cooldown_seconds"])
network_monitor = NetworkMonitor()

_camera_cfg = settings_manager.get_section("camera")
camera = CameraManager(
    device=_camera_cfg["device"], width=_camera_cfg["width"],
    height=_camera_cfg["height"], fps=_camera_cfg["fps"],
)


class NotifierProxy:
    """Lets Telegram settings change at runtime without re-registering
    listeners on the event bus."""

    def __init__(self, notifier: TelegramNotifier):
        self._notifier = notifier

    def set(self, notifier: TelegramNotifier) -> None:
        self._notifier = notifier

    def get(self) -> TelegramNotifier:
        return self._notifier

    def notify_event(self, event_type: str, payload: dict) -> None:
        self._notifier.notify_event(event_type, payload)


notifier_proxy = NotifierProxy(build_notifier(settings_manager.get_section("telegram")))
event_bus.register_notifier(notifier_proxy.notify_event)


def _recording_config() -> dict:
    return settings_manager.get_section("recording")


def _on_recording_stopped(mode: str, elapsed: Optional[float]) -> None:
    db.log_system_event("info", f"{mode} recording stopped after {elapsed or 0:.1f}s")


recording_ctrl = RecordingController(
    camera=camera,
    recordings_dir=str(RECORDINGS_DIR),
    config_provider=_recording_config,
    on_stop=_on_recording_stopped,
)


def _on_settings_changed(section: str, values: dict) -> None:
    if section == "camera":
        camera.stop()
        camera.device = values["device"]
        camera.width = values["width"]
        camera.height = values["height"]
        camera.fps = values["fps"]
        if values.get("enabled", True):
            camera.start()
        _ensure_motion_thread()
    elif section == "motion":
        event_bus.set_cooldown("MotionDetected", values["event_cooldown_seconds"])
        _ensure_motion_thread()  # toggling motion off/on also revives a dead worker
    elif section == "telegram":
        notifier_proxy.set(build_notifier(values))
    elif section == "recording":
        if values["mode"] == "continuous":
            recording_ctrl.start_continuous()
        elif recording_ctrl.status()["mode"] == "continuous":
            recording_ctrl.stop_continuous()


settings_manager.on_change(_on_settings_changed)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_motion_stop_event = threading.Event()
_retention_stop_event = threading.Event()
_watchdog_stop_event = threading.Event()


def _save_snapshot(frame: bytes) -> str:
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOTS_DIR / f"snapshot_{int(time.time() * 1000)}.jpg"
    path.write_bytes(frame)
    db.add_snapshot(str(path))
    return str(path)


def _run_resilient(name: str, tick, stop_event: threading.Event, error_delay: float = 2.0) -> None:
    """Runs `tick()` (which returns how long to wait before the next call)
    until `stop_event` is set. An exception in one tick is logged and the
    loop carries on — a background worker must never die silently because
    of one transient error (a DB hiccup, a camera reconnect, ...)."""
    while not stop_event.is_set():
        try:
            delay = tick()
        except Exception:
            logger.exception("%s loop error (continuing)", name)
            delay = error_delay
        stop_event.wait(delay)


def _motion_tick(detector: MotionDetector) -> float:
    """One motion-detection pass. Returns seconds to wait before the next."""
    motion_cfg = settings_manager.get_section("motion")
    interval = max(0.2, float(motion_cfg["poll_interval_seconds"]))
    if not motion_cfg["enabled"]:
        detector.reset()
        return 1.0

    # Only real camera frames are meaningful. While the camera is
    # disconnected/reconnecting, the app serves placeholder frames — don't
    # treat those as motion, and drop the old reference frame so the first
    # real frame after a reconnect isn't compared against stale data.
    if camera.health().source != "device":
        detector.reset()
        return 1.0

    frame = camera.get_latest_frame()
    if frame is None:
        return interval

    detector.pixel_threshold = motion_cfg["pixel_threshold"]
    detector.area_threshold = motion_cfg["area_threshold"]
    result = detector.process_frame(frame)
    if not result.motion_detected:
        return interval

    telegram_cfg = settings_manager.get_section("telegram")
    send_photo = bool(telegram_cfg["enabled"] and telegram_cfg["send_snapshot_on_motion"])
    snapshot_path = _save_snapshot(frame) if motion_cfg["snapshot_on_motion"] else None
    event_bus.emit(
        "MotionDetected",
        camera_id=camera.device,
        confidence=result.score,
        snapshot_path=snapshot_path,
        # The exact frame that triggered the alert goes to Telegram even if
        # "save snapshots" is off (bytes are handed to the notifier only).
        extra={"snapshot_bytes": frame} if send_photo else None,
    )
    recording_cfg = settings_manager.get_section("recording")
    if recording_cfg["mode"] == "event":
        recording_ctrl.trigger_event(
            post_event_seconds=recording_cfg["post_event_seconds"],
            max_event_seconds=recording_cfg["max_event_seconds"],
        )
    return interval


def _motion_loop() -> None:
    detector = MotionDetector()
    _run_resilient("Motion detection", lambda: _motion_tick(detector), _motion_stop_event)


_motion_thread: Optional[threading.Thread] = None
_motion_thread_lock = threading.Lock()


def _ensure_motion_thread() -> None:
    """Starts the motion worker if it isn't running (never started, or it
    died). Called at startup, when motion/camera settings change, and by the
    watchdog."""
    global _motion_thread
    if _motion_stop_event.is_set():
        return
    with _motion_thread_lock:
        if _motion_thread is None or not _motion_thread.is_alive():
            _motion_thread = threading.Thread(target=_motion_loop, daemon=True, name="motion")
            _motion_thread.start()
            logger.info("Motion detection worker started")


def _watchdog_tick() -> float:
    _ensure_motion_thread()
    return 10.0


def _retention_loop() -> None:
    check_interval = 3600
    while not _retention_stop_event.is_set():
        try:
            recording_cfg = settings_manager.get_section("recording")
            storage_cfg = settings_manager.get_section("storage")
            protect_latest = recording_ctrl.status()["active"]
            enforce_retention(str(RECORDINGS_DIR), recording_cfg["retention_days"], protect_latest)
            if is_storage_low(str(BASE_DIR), storage_cfg["low_space_warning_mb"]):
                event_bus.emit("StorageLow", meta={"path": str(BASE_DIR)})
        except Exception:
            logger.exception("Retention loop error")
        _retention_stop_event.wait(check_interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Pishak Home starting up")
    _motion_stop_event.clear()
    _retention_stop_event.clear()
    _watchdog_stop_event.clear()
    if _camera_cfg.get("enabled", True):
        camera.start()
    network_monitor.start()
    _ensure_motion_thread()
    threading.Thread(
        target=_run_resilient, args=("Watchdog", _watchdog_tick, _watchdog_stop_event),
        daemon=True, name="watchdog",
    ).start()
    threading.Thread(target=_retention_loop, daemon=True).start()
    if settings_manager.get_section("recording")["mode"] == "continuous":
        recording_ctrl.start_continuous()
    try:
        event_bus.emit("SystemStarted", notify=True)
    except Exception:
        # Never let a failed "started" notice prevent the app from starting.
        logger.exception("Could not record the SystemStarted event")
    yield
    logger.info("Pishak Home shutting down")
    _motion_stop_event.set()
    _retention_stop_event.set()
    _watchdog_stop_event.set()
    recording_ctrl.stop()
    event_bus.close()
    network_monitor.stop()
    camera.stop()
    db.close()


app = FastAPI(title="Pishak Home", version="1.0.0", lifespan=lifespan)

static_dir = BASE_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

PUBLIC_PATH_PREFIXES = ("/static", "/favicon.ico")
PUBLIC_PATHS = {"/login"}


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PATH_PREFIXES):
            return await call_next(request)

        token = request.cookies.get(SESSION_COOKIE_NAME)
        session = auth_manager.sessions.get(token)
        if session is None:
            if path.startswith("/api/") or path.startswith("/media/"):
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            return RedirectResponse(url="/login", status_code=303)

        request.state.session = session
        return await call_next(request)


app.add_middleware(AuthMiddleware)


def get_session(request: Request) -> Session:
    session = getattr(request.state, "session", None)
    if session is None:  # pragma: no cover - middleware guarantees this in practice
        raise HTTPException(status_code=401, detail="Not authenticated")
    return session


def verify_csrf(request: Request, session: Session = Depends(get_session)) -> None:
    token = request.headers.get("X-CSRF-Token", "")
    if not hmac.compare_digest(token, session.csrf_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")


# ---------------------------------------------------------------------------
# Auth pages/routes
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(
        request, "login.html", {"default_password_active": auth_manager.is_default_password_active()}
    )


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    client_key = _client_key(request)

    if auth_manager.is_locked_out(client_key):
        wait_s = int(auth_manager.seconds_until_unlocked(client_key)) + 1
        return templates.TemplateResponse(
            request, "login.html",
            {"error": f"Too many failed attempts. Try again in {wait_s}s.",
             "default_password_active": False},
            status_code=429,
        )

    result = auth_manager.login(username, password, client_key=client_key)
    if result is None:
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Invalid username or password.",
             "default_password_active": auth_manager.is_default_password_active()},
            status_code=401,
        )
    token, _session = result
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME, token, httponly=True, samesite="strict", max_age=7 * 24 * 3600
    )
    return response


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    auth_manager.logout(token)
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def _page_context(session: Session, active_page: str = "") -> dict:
    return {
        "csrf_token": session.csrf_token,
        "github_url": "https://github.com/sinahinson/PishakHome",
        "active_page": active_page,
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "dashboard.html", _page_context(session, "live"))


@app.get("/events", response_class=HTMLResponse)
async def events_page(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "events.html", _page_context(session, "events"))


@app.get("/recordings", response_class=HTMLResponse)
async def recordings_page(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "recordings.html", _page_context(session, "recordings"))


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, session: Session = Depends(get_session)):
    ctx = _page_context(session, "settings")
    ctx["settings"] = settings_manager.get_all()
    ctx["default_password_active"] = auth_manager.is_default_password_active()
    return templates.TemplateResponse(request, "settings.html", ctx)


# ---------------------------------------------------------------------------
# Camera / live view
# ---------------------------------------------------------------------------

@app.get("/video_feed")
async def video_feed(_: Session = Depends(get_session)):
    return StreamingResponse(
        camera.mjpeg_multipart_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/api/snapshot")
async def api_snapshot(_: Session = Depends(get_session)):
    frame = camera.get_latest_frame()
    if frame is None:
        raise HTTPException(status_code=503, detail="No camera frame available yet")
    _save_snapshot(frame)
    return Response(content=frame, media_type="image/jpeg")


@app.get("/api/camera/health")
async def api_camera_health(_: Session = Depends(get_session)):
    return camera.health().__dict__


@app.post("/api/camera/restart")
async def api_camera_restart(__: None = Depends(verify_csrf)):
    """Forces an immediate reconnect instead of waiting out the backoff
    delay — useful right after unplugging/replugging the USB webcam."""
    camera.restart()
    _ensure_motion_thread()
    return {"status": "restarting"}


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

@app.post("/api/record/manual")
async def api_record_manual(duration: int = 10, __: None = Depends(verify_csrf)):
    cfg = settings_manager.get_section("recording")
    duration = min(max(duration, 1), cfg.get("manual_clip_seconds", 10) or duration, 3600)
    if not recording_ctrl.start_manual(duration):
        raise HTTPException(status_code=409, detail="A recording is already in progress")
    return {"status": "recording_started", "duration_seconds": duration}


@app.post("/api/record/continuous/start")
async def api_record_continuous_start(__: None = Depends(verify_csrf)):
    ok = recording_ctrl.start_continuous()
    if ok:
        settings_manager.update_section("recording", {"mode": "continuous"})
    else:
        raise HTTPException(status_code=409, detail="A recording is already in progress")
    return {"status": "started"}


@app.post("/api/record/continuous/stop")
async def api_record_continuous_stop(__: None = Depends(verify_csrf)):
    recording_ctrl.stop_continuous()
    settings_manager.update_section("recording", {"mode": "off"})
    return {"status": "stopped"}


@app.get("/api/record/status")
async def api_record_status(_: Session = Depends(get_session)):
    return recording_ctrl.status()


@app.get("/api/recordings")
async def api_recordings(_: Session = Depends(get_session)):
    files = list_recording_files(str(RECORDINGS_DIR))
    return [
        {
            "name": f.name, "size_bytes": f.size_bytes, "modified_at": f.modified_at,
            "download_url": f"/media/recordings/{f.name}",
        }
        for f in files
    ]


@app.post("/api/recordings/clear")
async def api_recordings_clear(__: None = Depends(verify_csrf)):
    if recording_ctrl.status()["active"]:
        raise HTTPException(status_code=409, detail="Stop the current recording first")
    count = delete_all_recordings(str(RECORDINGS_DIR))
    return {"status": "ok", "deleted": count}


@app.get("/api/snapshots")
async def api_snapshots(_: Session = Depends(get_session)):
    files = []
    if SNAPSHOTS_DIR.exists():
        for f in sorted(SNAPSHOTS_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True):
            stat = f.stat()
            files.append(
                {
                    "name": f.name, "size_bytes": stat.st_size, "modified_at": stat.st_mtime,
                    "download_url": f"/media/snapshots/{f.name}",
                }
            )
    return files


@app.post("/api/snapshots/clear")
async def api_snapshots_clear(__: None = Depends(verify_csrf)):
    count = delete_all_snapshots(str(SNAPSHOTS_DIR))
    return {"status": "ok", "deleted": count}


def _safe_media_path(directory: Path, filename: str) -> Path:
    candidate = (directory / filename).resolve()
    if directory.resolve() not in candidate.parents and candidate != directory.resolve():
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return candidate


@app.get("/media/recordings/{filename}")
async def media_recording(filename: str, _: Session = Depends(get_session)):
    path = _safe_media_path(RECORDINGS_DIR, filename)
    return FileResponse(path, filename=filename)


@app.get("/media/snapshots/{filename}")
async def media_snapshot(filename: str, _: Session = Depends(get_session)):
    path = _safe_media_path(SNAPSHOTS_DIR, filename)
    return FileResponse(path, filename=filename)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@app.get("/api/events")
async def api_events(limit: int = 50, event_type: Optional[str] = None, _: Session = Depends(get_session)):
    return db.get_events(limit=limit, event_type=event_type)


@app.post("/api/events/clear")
async def api_events_clear(__: None = Depends(verify_csrf)):
    count = db.clear_events()
    return {"status": "ok", "deleted": count}


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

@app.get("/api/network")
async def api_network(_: Session = Depends(get_session)):
    return network_monitor.get_status()


# ---------------------------------------------------------------------------
# System status
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def api_status(_: Session = Depends(get_session)):
    sys_status = get_system_status(str(BASE_DIR))
    cam_health = camera.health()
    return {
        "app": "pishak-home",
        "version": "1.0.0",
        "system": sys_status.__dict__,
        "camera": cam_health.__dict__,
        "recording": recording_ctrl.status(),
    }


# ---------------------------------------------------------------------------
# Settings (GUI-editable)
# ---------------------------------------------------------------------------

@app.get("/api/settings")
async def api_get_settings(_: Session = Depends(get_session)):
    return settings_manager.get_all()


@app.post("/api/settings/reset")
async def api_reset_all_settings(request: Request, __: None = Depends(verify_csrf)):
    """Resets every GUI-editable setting — camera, motion, recording,
    telegram, general/storage, AND login credentials — back to factory
    defaults (pishak/pishak). Gated behind re-entering the current
    password, since it's a meaningfully destructive action. Registered
    BEFORE the generic /api/settings/{section} route below: both match a
    single path segment, and route order decides which one wins."""
    body = await request.json()
    current_username = settings_manager.get_section("security")["username"]
    if not auth_manager.authenticate(current_username, str(body.get("current_password", ""))):
        raise HTTPException(status_code=400, detail="Current password incorrect")
    settings_manager.reset_all(sections=["camera", "motion", "recording", "telegram", "general", "storage"])
    auth_manager.reset_credentials_to_default()
    db.log_system_event("info", "All settings reset to defaults by user")
    return {"status": "ok"}


ALLOWED_SECTIONS = {"general", "camera", "motion", "recording", "telegram", "storage"}


@app.post("/api/settings/{section}")
async def api_update_settings(section: str, request: Request, __: None = Depends(verify_csrf)):
    if section not in ALLOWED_SECTIONS:
        raise HTTPException(status_code=404, detail="Unknown settings section")
    body = await request.json()
    try:
        updated = settings_manager.update_section(section, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return updated


@app.post("/api/settings/security/password")
async def api_change_password(request: Request, __: None = Depends(verify_csrf)):
    body = await request.json()
    ok = auth_manager.change_password(
        str(body.get("current_password", "")), str(body.get("new_password", ""))
    )
    if not ok:
        raise HTTPException(status_code=400, detail="Current password incorrect or new password too short")
    return {"status": "ok"}


@app.post("/api/settings/security/username")
async def api_change_username(request: Request, __: None = Depends(verify_csrf)):
    body = await request.json()
    ok = auth_manager.change_username(str(body.get("username", "")))
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid username")
    return {"status": "ok"}


@app.post("/api/telegram/test")
async def api_telegram_test(__: None = Depends(verify_csrf)):
    notifier = notifier_proxy.get()
    ok, message = notifier.test_connection()
    return {"ok": ok, "message": message}
