"""
Pishak Home — Telegram notifications.

Uses `httpx` (already a dependency) instead of raw `urllib` specifically
because it supports routing requests through a proxy — needed on networks
where Telegram's API is blocked directly and only reachable via a local
HTTP or SOCKS5 proxy (e.g. through this project's existing Xray/WireGuard
setup). SOCKS5 proxies require the optional `socksio` package
(`pip install httpx[socks]`); an HTTP proxy needs nothing extra.

Design notes, unchanged from the original version:
  - Disabled unless config.telegram.enabled is true AND a bot_token is set.
  - Only chat IDs listed in `authorized_chat_ids` are ever messaged, per
    the project's "Telegram bot authorization" requirement.
  - The actual HTTP call is isolated in `_call_api` so tests can inject a
    fake instead of hitting the real network.
  - Cooldown/anti-spam logic lives in app/events.py (EventBus), not here.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger("pishak.notifications")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

EVENT_LABELS = {
    "CatDetected": "🐱 Cat detected",
    "PersonDetected": "🧍 Person detected",
    "MotionDetected": "🚨 Motion detected",
    "CameraOffline": "📷 Camera offline",
    "CameraRecovered": "📷 Camera back online",
    "StorageLow": "💾 Storage low",
    "RecordingStopped": "⏺ Recording stopped",
    "SystemStarted": "✅ System started",
}


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str = "",
        authorized_chat_ids: Optional[list[int]] = None,
        enabled: bool = False,
        proxy_url: str = "",
        timeout: float = 10.0,
        http_call: Optional[Any] = None,
        send_snapshots: bool = False,
    ):
        self.bot_token = bot_token
        self.send_snapshots = send_snapshots
        self.authorized_chat_ids = authorized_chat_ids or []
        self.proxy_url = proxy_url or None
        self.timeout = timeout
        self.enabled = enabled and bool(bot_token) and bool(self.authorized_chat_ids)
        # Injectable for tests; defaults to the real network call.
        self._http_call = http_call or self._call_api

    # ---- low-level API ----------------------------------------------------

    def _call_api(
        self, method: str, payload: dict[str, Any], files: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        url = TELEGRAM_API.format(token=self.bot_token, method=method)
        # Uploading a photo (possibly through a slow proxy) needs more time
        # than a small JSON call.
        client_kwargs: dict[str, Any] = {"timeout": max(self.timeout, 30.0) if files else self.timeout}
        if self.proxy_url:
            client_kwargs["proxy"] = self.proxy_url
        try:
            with httpx.Client(**client_kwargs) as client:
                if files:
                    response = client.post(url, data=payload, files=files)
                else:
                    response = client.post(url, json=payload)
                return response.json()
        except httpx.HTTPError as exc:
            # str(exc) can contain the request URL, which embeds the bot
            # token — log only the exception type.
            logger.warning("Telegram API call failed (%s): %s", method, type(exc).__name__)
            return {"ok": False, "error": type(exc).__name__}
        except ImportError as exc:
            # Raised by httpx if a socks5:// proxy is configured without the
            # optional `socksio` package installed.
            logger.warning(
                "Telegram proxy configured but the required extra isn't "
                "installed (pip install httpx[socks] for SOCKS5 support): %s",
                exc,
            )
            return {"ok": False, "error": str(exc)}

    # ---- public sending helpers ------------------------------------------

    def send_message(self, chat_id: int, text: str) -> bool:
        if not self.enabled:
            return False
        result = self._http_call(
            "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        )
        return bool(result.get("ok"))

    def broadcast(self, text: str) -> None:
        if not self.enabled:
            return
        for chat_id in self.authorized_chat_ids:
            self.send_message(chat_id, text)

    def send_photo(self, chat_id: int, photo: bytes, caption: str = "") -> bool:
        if not self.enabled:
            return False
        payload = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"}
        files = {"photo": ("motion.jpg", photo, "image/jpeg")}
        result = self._http_call("sendPhoto", payload, files)
        return bool(result.get("ok"))

    @staticmethod
    def _load_snapshot(payload: dict[str, Any]) -> Optional[bytes]:
        photo = payload.get("snapshot_bytes")
        if photo:
            return photo
        path = payload.get("snapshot_path")
        if path:
            try:
                with open(path, "rb") as f:
                    return f.read()
            except OSError:
                return None
        return None

    def notify_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Matches the `Notifier` callable shape expected by app.events.EventBus.

        For MotionDetected, when snapshot sending is enabled, the snapshot
        is sent as a photo with the text as its caption. If the photo can't
        be sent (network hiccup, no image), it falls back to a plain text
        message so the alert itself is never lost.
        """
        if not self.enabled:
            return
        label = EVENT_LABELS.get(event_type, event_type)
        lines = ["<b>PISHAK HOME</b>", label]
        if payload.get("zone"):
            lines.append(f"Zone: {payload['zone']}")
        if payload.get("confidence") is not None:
            lines.append(f"Confidence: {payload['confidence']:.2f}")
        text = "\n".join(lines)

        photo = None
        if event_type == "MotionDetected" and self.send_snapshots:
            photo = self._load_snapshot(payload)

        if photo is None:
            self.broadcast(text)
            return
        for chat_id in self.authorized_chat_ids:
            if not self.send_photo(chat_id, photo, caption=text):
                self.send_message(chat_id, text)

    def test_connection(self) -> tuple[bool, str]:
        """Used by the Settings page's "Test" button. Calls Telegram's
        getMe endpoint, which works even with no authorized chat ids yet."""
        if not self.bot_token:
            return False, "No bot token configured."
        result = self._http_call("getMe", {})
        if result.get("ok"):
            username = result.get("result", {}).get("username", "unknown")
            return True, f"Connected as @{username}"
        return False, str(result.get("error") or result.get("description") or "Unknown error")

    def is_authorized(self, chat_id: int) -> bool:
        return chat_id in self.authorized_chat_ids


def build_notifier(telegram_config: dict[str, Any]) -> TelegramNotifier:
    proxy_url = telegram_config.get("proxy_url", "") if telegram_config.get("proxy_enabled") else ""
    return TelegramNotifier(
        bot_token=telegram_config.get("bot_token", ""),
        authorized_chat_ids=telegram_config.get("authorized_chat_ids", []),
        enabled=telegram_config.get("enabled", False),
        proxy_url=proxy_url,
        send_snapshots=telegram_config.get("send_snapshot_on_motion", False),
    )
