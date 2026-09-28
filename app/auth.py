"""
Pishak Home — authentication.

Deliberately dependency-free: password hashing uses stdlib PBKDF2-HMAC
(200k iterations, per-user random salt), and sessions are opaque random
tokens kept in an in-memory store (session loss on restart is an
acceptable trade-off for a single-user home appliance; nothing about
camera/recording state depends on it).

Default credentials are username "pishak" / password "pishak", exactly as
requested — changeable from the Settings page immediately after first
login, and the login page nudges the user to do so while the default
password is still active.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

from app.settings_manager import SettingsManager

DEFAULT_USERNAME = "pishak"
DEFAULT_PASSWORD = "pishak"

PBKDF2_ITERATIONS = 200_000
SESSION_TTL_SECONDS = 7 * 24 * 3600  # 7 days
SESSION_COOKIE_NAME = "pishak_session"


def hash_password(password: str, salt: Optional[bytes] = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return digest.hex(), salt.hex()


def verify_password(password: str, hash_hex: str, salt_hex: str) -> bool:
    if not hash_hex or not salt_hex:
        return False
    salt = bytes.fromhex(salt_hex)
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, hash_hex)


@dataclass
class Session:
    username: str
    csrf_token: str
    created_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > SESSION_TTL_SECONDS


class SessionStore:
    """Plain in-memory session store. Fine for a single-user local app;
    swap for a persisted store if multi-device "remember me" across
    restarts ever becomes a real requirement."""

    def __init__(self):
        self._sessions: dict[str, Session] = {}

    def create(self, username: str) -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        session = Session(username=username, csrf_token=secrets.token_urlsafe(24))
        self._sessions[token] = session
        return token, session

    def get(self, token: Optional[str]) -> Optional[Session]:
        if not token:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        if session.is_expired():
            self._sessions.pop(token, None)
            return None
        return session

    def destroy(self, token: Optional[str]) -> None:
        if token:
            self._sessions.pop(token, None)


class AuthManager:
    """Ties together SettingsManager (where the password hash lives) and
    SessionStore (who's currently logged in)."""

    MAX_FAILED_ATTEMPTS = 5
    LOCKOUT_SECONDS = 30

    def __init__(self, settings_manager: SettingsManager):
        self.settings_manager = settings_manager
        self.sessions = SessionStore()
        self._failed_attempts: dict[str, list[float]] = {}
        self._ensure_default_credentials()

    def _ensure_default_credentials(self) -> None:
        security = self.settings_manager.get_section("security")
        if not security.get("password_hash"):
            password_hash, password_salt = hash_password(DEFAULT_PASSWORD)
            self.settings_manager.update_section(
                "security",
                {
                    "username": security.get("username") or DEFAULT_USERNAME,
                    "password_hash": password_hash,
                    "password_salt": password_salt,
                },
            )

    def is_default_password_active(self) -> bool:
        security = self.settings_manager.get_section("security")
        return verify_password(
            DEFAULT_PASSWORD, security.get("password_hash", ""), security.get("password_salt", "")
        )

    def authenticate(self, username: str, password: str) -> bool:
        security = self.settings_manager.get_section("security")
        if not hmac.compare_digest(username, security.get("username", DEFAULT_USERNAME)):
            return False
        return verify_password(password, security.get("password_hash", ""), security.get("password_salt", ""))

    def is_locked_out(self, client_key: str) -> bool:
        attempts = self._failed_attempts.get(client_key, [])
        recent = [t for t in attempts if (time.time() - t) < self.LOCKOUT_SECONDS]
        self._failed_attempts[client_key] = recent
        return len(recent) >= self.MAX_FAILED_ATTEMPTS

    def seconds_until_unlocked(self, client_key: str) -> float:
        attempts = self._failed_attempts.get(client_key, [])
        if len(attempts) < self.MAX_FAILED_ATTEMPTS:
            return 0.0
        oldest_relevant = sorted(attempts)[-self.MAX_FAILED_ATTEMPTS]
        remaining = self.LOCKOUT_SECONDS - (time.time() - oldest_relevant)
        return max(0.0, remaining)

    def login(self, username: str, password: str, client_key: str = "global") -> Optional[tuple[str, Session]]:
        if self.is_locked_out(client_key):
            return None
        if not self.authenticate(username, password):
            self._failed_attempts.setdefault(client_key, []).append(time.time())
            return None
        self._failed_attempts.pop(client_key, None)
        return self.sessions.create(username)

    def logout(self, token: Optional[str]) -> None:
        self.sessions.destroy(token)

    def change_password(self, current_password: str, new_password: str) -> bool:
        security = self.settings_manager.get_section("security")
        if not verify_password(
            current_password, security.get("password_hash", ""), security.get("password_salt", "")
        ):
            return False
        if not new_password or len(new_password) < 4:
            return False
        password_hash, password_salt = hash_password(new_password)
        self.settings_manager.update_section(
            "security", {"password_hash": password_hash, "password_salt": password_salt}
        )
        return True

    def change_username(self, new_username: str) -> bool:
        if not new_username or len(new_username) < 2:
            return False
        self.settings_manager.update_section("security", {"username": new_username})
        return True

    def reset_credentials_to_default(self) -> None:
        """Restores username/password to pishak/pishak. Used by the
        "reset all settings" flow — callers are responsible for requiring
        re-confirmation (e.g. the current password) before calling this,
        since it's a meaningful security-relevant action."""
        password_hash, password_salt = hash_password(DEFAULT_PASSWORD)
        self.settings_manager.update_section(
            "security",
            {"username": DEFAULT_USERNAME, "password_hash": password_hash, "password_salt": password_salt},
        )
