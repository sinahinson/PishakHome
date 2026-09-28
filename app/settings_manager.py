"""
Pishak Home — settings manager.

Every setting the dashboard can edit lives here, backed by the `settings`
table in SQLite (see app/database.py). This replaces the old "edit the
YAML file and restart" model: config/settings.example.yaml now only seeds
sane defaults the very first time the app runs. After that, the database
is the single source of truth, and the GUI settings page is the intended
way to change anything.

Sections are plain dicts validated against a known key set — unknown keys
are rejected so a bad request can't silently inject arbitrary config.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Callable, Optional

from app.database import Database

SETTINGS_KEY_PREFIX = "config:"

DEFAULTS: dict[str, dict[str, Any]] = {
    "general": {
        "site_title": "Pishak Home",
    },
    "security": {
        "username": "pishak",
        # password_hash / password_salt are added by app.auth on first boot
    },
    "camera": {
        "enabled": True,
        "device": "/dev/video0",
        "width": 1920,
        "height": 1080,
        "fps": 30,
    },
    "motion": {
        "enabled": True,
        "poll_interval_seconds": 1.0,
        "pixel_threshold": 25,
        "area_threshold": 0.02,
        "event_cooldown_seconds": 30,
        "snapshot_on_motion": True,
    },
    "recording": {
        "mode": "off",  # "off" | "event" | "continuous"
        "codec": "copy",  # "copy" (no re-encode, largest files) | "h264" (smaller, more CPU)
        "width": 0,  # 0 = same as live camera resolution
        "height": 0,
        "fps": 0,  # 0 = same as live camera fps
        "bitrate_kbps": 1500,
        "segment_minutes": 15,
        "retention_days": 7,
        "manual_clip_seconds": 10,
        "post_event_seconds": 15,
        "max_event_seconds": 120,
    },
    "storage": {
        "low_space_warning_mb": 500,
    },
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "authorized_chat_ids": [],
        "send_snapshot_on_motion": True,  # attach the snapshot to MotionDetected alerts
        "proxy_enabled": False,
        "proxy_url": "",  # e.g. http://127.0.0.1:8080 or socks5h://127.0.0.1:1080
    },
}

# Keys that must never be handed back to the browser as-is.
SECRET_KEYS = {
    ("security", "password_hash"),
    ("security", "password_salt"),
    ("telegram", "bot_token"),
}


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class SettingsManager:
    def __init__(self, db: Database, seed: dict[str, dict[str, Any]] | None = None):
        self.db = db
        self._seed = seed or {}
        self._on_change: list[Callable[[str, dict[str, Any]], None]] = []
        self._ensure_seeded()

    def _ensure_seeded(self) -> None:
        for section, defaults in DEFAULTS.items():
            key = SETTINGS_KEY_PREFIX + section
            existing = self.db.get_setting(key)
            if existing is None:
                seed_values = self._seed.get(section, {})
                initial = _deep_merge(defaults, seed_values)
                self.db.set_setting(key, initial)

    def on_change(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        """Register a callback invoked as `callback(section, new_values)`
        whenever a section is updated, so live components (camera thread,
        notifier) can react without a restart."""
        self._on_change.append(callback)

    def get_section(self, section: str) -> dict[str, Any]:
        if section not in DEFAULTS:
            raise KeyError(f"Unknown settings section: {section}")
        stored = self.db.get_setting(SETTINGS_KEY_PREFIX + section, default={})
        if not isinstance(stored, dict):
            # Defensive: a corrupted or unexpectedly-null stored value must
            # never take down a background loop. Fall back to defaults and
            # self-heal the row so this doesn't recur.
            logging.getLogger("pishak.settings").warning(
                "Stored settings for '%s' were not a dict (%r) — resetting to defaults",
                section, stored,
            )
            stored = {}
            self.db.set_setting(SETTINGS_KEY_PREFIX + section, _deep_merge(DEFAULTS[section], {}))
        return _deep_merge(DEFAULTS[section], stored)

    def get_all(self, redact_secrets: bool = True) -> dict[str, dict[str, Any]]:
        result = {section: self.get_section(section) for section in DEFAULTS}
        if redact_secrets:
            result = copy.deepcopy(result)
            for section, key in SECRET_KEYS:
                if key in result.get(section, {}) and result[section][key]:
                    result[section][key] = "••••••••"
        return result

    def update_section(self, section: str, updates: dict[str, Any]) -> dict[str, Any]:
        if section not in DEFAULTS:
            raise KeyError(f"Unknown settings section: {section}")
        allowed_keys = set(DEFAULTS[section].keys())
        # security carries a couple of runtime-managed keys not in DEFAULTS
        if section == "security":
            allowed_keys |= {"password_hash", "password_salt"}
        unknown = set(updates.keys()) - allowed_keys
        if unknown:
            raise ValueError(f"Unknown setting(s) for '{section}': {sorted(unknown)}")

        current = self.get_section(section)
        merged = _deep_merge(current, updates)
        self.db.set_setting(SETTINGS_KEY_PREFIX + section, merged)
        for callback in self._on_change:
            try:
                callback(section, merged)
            except Exception:  # pragma: no cover - defensive, callbacks are best-effort
                pass
        return merged

    def reset_section(self, section: str) -> dict[str, Any]:
        """Resets one section back to its hard-coded defaults (not the
        YAML seed, which only ever applies once on first boot). Note:
        resetting "security" this way clears the password hash/salt —
        callers must re-establish valid credentials afterward (see
        AuthManager.reset_credentials_to_default)."""
        if section not in DEFAULTS:
            raise KeyError(f"Unknown settings section: {section}")
        fresh = copy.deepcopy(DEFAULTS[section])
        self.db.set_setting(SETTINGS_KEY_PREFIX + section, fresh)
        for callback in self._on_change:
            try:
                callback(section, fresh)
            except Exception:  # pragma: no cover
                pass
        return fresh

    def reset_all(self, sections: Optional[list[str]] = None) -> dict[str, dict[str, Any]]:
        targets = sections if sections is not None else list(DEFAULTS.keys())
        for section in targets:
            self.reset_section(section)
        return self.get_all()
