import tempfile
from pathlib import Path

import pytest

from app.auth import AuthManager, DEFAULT_PASSWORD, DEFAULT_USERNAME, hash_password, verify_password
from app.database import Database
from app.settings_manager import SettingsManager


@pytest.fixture
def auth():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "auth_test.db")
        sm = SettingsManager(db)
        yield AuthManager(sm)
        db.close()


def test_hash_and_verify_roundtrip():
    h, s = hash_password("hunter2")
    assert verify_password("hunter2", h, s) is True
    assert verify_password("wrong", h, s) is False


def test_same_password_different_salts_produce_different_hashes():
    h1, s1 = hash_password("same-password")
    h2, s2 = hash_password("same-password")
    assert s1 != s2
    assert h1 != h2


def test_default_credentials_are_pishak_pishak(auth):
    assert auth.authenticate(DEFAULT_USERNAME, DEFAULT_PASSWORD) is True
    assert auth.is_default_password_active() is True


def test_wrong_password_rejected(auth):
    assert auth.authenticate(DEFAULT_USERNAME, "wrong-password") is False


def test_wrong_username_rejected(auth):
    assert auth.authenticate("not-pishak", DEFAULT_PASSWORD) is False


def test_login_creates_valid_session(auth):
    result = auth.login(DEFAULT_USERNAME, DEFAULT_PASSWORD)
    assert result is not None
    token, session = result
    assert auth.sessions.get(token) is session
    assert session.username == DEFAULT_USERNAME


def test_login_fails_with_bad_credentials(auth):
    assert auth.login(DEFAULT_USERNAME, "nope") is None


def test_logout_invalidates_session(auth):
    token, _ = auth.login(DEFAULT_USERNAME, DEFAULT_PASSWORD)
    auth.logout(token)
    assert auth.sessions.get(token) is None


def test_change_password_requires_correct_current_password(auth):
    assert auth.change_password("wrong-current", "newpassword123") is False
    assert auth.authenticate(DEFAULT_USERNAME, DEFAULT_PASSWORD) is True  # unchanged


def test_change_password_succeeds_and_updates_login(auth):
    assert auth.change_password(DEFAULT_PASSWORD, "newpassword123") is True
    assert auth.authenticate(DEFAULT_USERNAME, "newpassword123") is True
    assert auth.authenticate(DEFAULT_USERNAME, DEFAULT_PASSWORD) is False
    assert auth.is_default_password_active() is False


def test_change_password_rejects_too_short(auth):
    assert auth.change_password(DEFAULT_PASSWORD, "abc") is False


def test_change_username(auth):
    assert auth.change_username("sina") is True
    assert auth.authenticate("sina", DEFAULT_PASSWORD) is True
    assert auth.authenticate(DEFAULT_USERNAME, DEFAULT_PASSWORD) is False


def test_reset_credentials_to_default(auth):
    auth.change_username("sina")
    auth.change_password(DEFAULT_PASSWORD, "somethingnew123")
    assert auth.authenticate("sina", "somethingnew123") is True

    auth.reset_credentials_to_default()
    assert auth.authenticate(DEFAULT_USERNAME, DEFAULT_PASSWORD) is True
    assert auth.authenticate("sina", "somethingnew123") is False


def test_lockout_after_max_failed_attempts(auth):
    for _ in range(auth.MAX_FAILED_ATTEMPTS):
        assert auth.login(DEFAULT_USERNAME, "wrong", client_key="1.2.3.4") is None
    # Even correct credentials are now locked out for this client key.
    assert auth.login(DEFAULT_USERNAME, DEFAULT_PASSWORD, client_key="1.2.3.4") is None
    assert auth.seconds_until_unlocked("1.2.3.4") > 0


def test_lockout_is_scoped_per_client_key(auth):
    for _ in range(auth.MAX_FAILED_ATTEMPTS):
        auth.login(DEFAULT_USERNAME, "wrong", client_key="attacker-ip")
    # A different client key is unaffected.
    assert auth.login(DEFAULT_USERNAME, DEFAULT_PASSWORD, client_key="my-ip") is not None


def test_successful_login_resets_failed_attempts(auth):
    auth.login(DEFAULT_USERNAME, "wrong", client_key="5.5.5.5")
    auth.login(DEFAULT_USERNAME, "wrong", client_key="5.5.5.5")
    assert auth.login(DEFAULT_USERNAME, DEFAULT_PASSWORD, client_key="5.5.5.5") is not None
    assert auth.seconds_until_unlocked("5.5.5.5") == 0
