# Changelog

## 1.0.0 — first release

**Monitoring**
- Live MJPEG view from a USB webcam (no transcoding), shared by every feature.
- Motion detection (lightweight frame differencing) with adjustable sensitivity.
- Self-healing camera: automatic reconnect with backoff, live ffmpeg diagnostics
  (`/api/camera/health`), and a **Restart camera** button.
- Live network card: per-interface IP, link state, speed and real-time RX/TX.

**Recording**
- Three modes: off · record on motion · continuous 24/7 until stopped.
- Recording never opens the camera a second time (shares the live stream).
- Copy (original quality) or H.264 with configurable bitrate / resolution / FPS.
- Segmented files, retention by days, low-storage warning.
- Browse/download recordings and snapshots; bulk-delete recordings, snapshots, events.

**Telegram**
- Motion alerts with the snapshot photo attached (optional; falls back to text).
- Authorized chat IDs, per-event cooldowns, optional HTTP/SOCKS5 proxy, test button.
- Alerts are sent from a background worker, so a slow proxy can't stall motion detection.
- The bot token is never written to logs.

**Panel & security**
- Login (default `pishak` / `pishak`, changeable), PBKDF2 hashing, CSRF protection,
  brute-force lockout, `SameSite=Strict` cookies.
- Every setting editable from the UI and stored in the database; password-confirmed
  "Reset all settings".
- Responsive orange/white UI.

**Reliability fixes included in this release**
- SQLite access is serialized with a lock (fixes intermittent
  `bad parameter or other API misuse` / `cannot commit - no transaction is active`,
  including rare startup failures that disappeared after a reload).
- The motion worker can no longer die silently: each pass is exception-safe, a
  watchdog revives it, and it ignores placeholder frames while the camera is
  disconnected (no fake alerts, clean baseline after a reconnect).
- Camera capture: race-free shutdown, `read1()`-based reads, stall detection,
  permission preflight.
- Quieter logs: ffmpeg progress spam removed.
