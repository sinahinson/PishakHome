import tempfile
from pathlib import Path

import pytest

from app.database import Database
from app.settings_manager import SettingsManager


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        database = Database(Path(tmp) / "settings_test.db")
        yield database
        database.close()


def test_seeds_defaults_on_first_use(db):
    sm = SettingsManager(db)
    camera = sm.get_section("camera")
    assert camera["device"] == "/dev/video0"
    assert camera["width"] == 1920


def test_seed_overrides_defaults(db):
    sm = SettingsManager(db, seed={"camera": {"device": "/dev/video2"}})
    assert sm.get_section("camera")["device"] == "/dev/video2"
    # unspecified keys still get their normal defaults
    assert sm.get_section("camera")["width"] == 1920


def test_update_section_persists(db):
    sm = SettingsManager(db)
    sm.update_section("camera", {"width": 1280, "height": 720})
    fresh = SettingsManager(db)  # simulate reloading after a restart
    cam = fresh.get_section("camera")
    assert cam["width"] == 1280
    assert cam["height"] == 720
    assert cam["fps"] == 30  # untouched key keeps its default


def test_update_section_rejects_unknown_keys(db):
    sm = SettingsManager(db)
    with pytest.raises(ValueError):
        sm.update_section("camera", {"not_a_real_setting": 123})


def test_update_unknown_section_raises(db):
    sm = SettingsManager(db)
    with pytest.raises(KeyError):
        sm.update_section("nonexistent", {})
    with pytest.raises(KeyError):
        sm.get_section("nonexistent")


def test_get_all_redacts_secrets_by_default(db):
    sm = SettingsManager(db)
    sm.update_section("telegram", {"bot_token": "super-secret-token"})
    redacted = sm.get_all()
    assert redacted["telegram"]["bot_token"] != "super-secret-token"
    unredacted = sm.get_all(redact_secrets=False)
    assert unredacted["telegram"]["bot_token"] == "super-secret-token"


def test_on_change_callback_fires(db):
    sm = SettingsManager(db)
    calls = []
    sm.on_change(lambda section, values: calls.append((section, values)))
    sm.update_section("motion", {"enabled": False})
    assert len(calls) == 1
    assert calls[0][0] == "motion"
    assert calls[0][1]["enabled"] is False


def test_recording_defaults_present(db):
    sm = SettingsManager(db)
    rec = sm.get_section("recording")
    assert rec["mode"] == "off"
    assert rec["codec"] == "copy"
    assert rec["retention_days"] == 7


def test_get_section_self_heals_when_stored_value_is_corrupted(db):
    # Simulates a row whose stored JSON value is `null` (or any non-dict) —
    # this must never crash a background loop; it should fall back to
    # defaults and repair the row so it doesn't keep happening.
    sm = SettingsManager(db)
    db.set_setting("config:recording", None)  # raw None, as if corrupted
    result = sm.get_section("recording")
    assert result["mode"] == "off"
    assert result["retention_days"] == 7

    # And the row should now be healed for next time.
    healed = db.get_setting("config:recording")
    assert isinstance(healed, dict)


def test_get_section_self_heals_when_stored_value_is_a_list(db):
    sm = SettingsManager(db)
    db.set_setting("config:motion", ["not", "a", "dict"])
    result = sm.get_section("motion")
    assert result["enabled"] is True  # falls back to the default


def test_reset_section_restores_hard_defaults(db):
    sm = SettingsManager(db)
    sm.update_section("camera", {"width": 640, "height": 480})
    assert sm.get_section("camera")["width"] == 640
    fresh = sm.reset_section("camera")
    assert fresh["width"] == 1920
    assert sm.get_section("camera")["width"] == 1920


def test_reset_section_fires_on_change(db):
    sm = SettingsManager(db)
    calls = []
    sm.on_change(lambda section, values: calls.append(section))
    sm.reset_section("motion")
    assert calls == ["motion"]


def test_reset_all_resets_every_requested_section(db):
    sm = SettingsManager(db)
    sm.update_section("camera", {"width": 111})
    sm.update_section("motion", {"pixel_threshold": 99})
    sm.reset_all(sections=["camera", "motion"])
    assert sm.get_section("camera")["width"] == 1920
    assert sm.get_section("motion")["pixel_threshold"] == 25


def test_reset_all_default_resets_everything_including_security(db):
    sm = SettingsManager(db)
    sm.update_section("camera", {"width": 111})
    sm.reset_all()
    assert sm.get_section("camera")["width"] == 1920
    # security's hard default has no password hash — that's expected;
    # AuthManager is responsible for re-establishing valid credentials.
    assert sm.get_section("security").get("password_hash") is None


def test_telegram_snapshot_option_default(db):
    sm = SettingsManager(db)
    assert sm.get_section("telegram")["send_snapshot_on_motion"] is True


def test_existing_installs_get_new_telegram_option_via_defaults(db):
    # An install created before this option existed has no such key stored;
    # the default must still apply after upgrading.
    sm = SettingsManager(db)
    db.set_setting("config:telegram", {"enabled": True, "bot_token": "x"})
    assert sm.get_section("telegram")["send_snapshot_on_motion"] is True
    assert sm.get_section("telegram")["enabled"] is True
