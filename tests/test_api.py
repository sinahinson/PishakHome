import re
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def app_module():
    from app import main
    with TestClient(main.app) as c:
        time.sleep(0.5)  # let the camera thread produce its first frame
        yield c, main


@pytest.fixture(scope="module")
def client(app_module):
    return app_module[0]


@pytest.fixture(scope="module")
def csrf(client):
    resp = client.post("/login", data={"username": "pishak", "password": "pishak"})
    assert resp.status_code == 200  # after redirect following
    home = client.get("/")
    match = re.search(r'csrf-token" content="([^"]*)"', home.text)
    assert match, "csrf token not found on page"
    return match.group(1)


# ---- auth ---------------------------------------------------------------

def test_dashboard_redirects_when_not_logged_in():
    from app import main
    anon = TestClient(main.app, follow_redirects=False)
    resp = anon.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_api_requires_auth_returns_401():
    from app import main
    anon = TestClient(main.app, follow_redirects=False)
    resp = anon.get("/api/status")
    assert resp.status_code == 401


def test_login_page_loads():
    from app import main
    anon = TestClient(main.app)
    resp = anon.get("/login")
    assert resp.status_code == 200
    assert "Pishak Home" in resp.text


def test_login_page_shows_pishak_avatar_instead_of_emoji():
    from app import main
    anon = TestClient(main.app)
    html = anon.get("/login").text
    assert 'class="login-avatar"' in html
    assert "/static/img/pishak.jpg" in html
    assert "\U0001F43E" not in html.split("<h1>")[0]  # the old paw emoji is gone


def test_avatar_image_is_public_and_small():
    """The login page must be able to load the image before signing in."""
    from app import main
    anon = TestClient(main.app, follow_redirects=False)
    resp = anon.get("/static/img/pishak.jpg")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content.startswith(b"\xff\xd8")
    assert len(resp.content) < 50_000  # minimal: stays light on a Pi


def test_login_page_has_project_link_and_version():
    from app import main
    anon = TestClient(main.app)
    resp = anon.get("/login")
    assert "github.com/sinahinson/PishakHome" in resp.text
    assert "v1.0.0" in resp.text


def test_login_wrong_password_shows_error():
    from app import main
    anon = TestClient(main.app)
    resp = anon.post("/login", data={"username": "pishak", "password": "wrong"})
    assert resp.status_code == 401
    assert "Invalid" in resp.text


def test_login_correct_credentials_redirects_home(client):
    resp = client.post(
        "/login", data={"username": "pishak", "password": "pishak"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_dashboard_loads_after_login(client, csrf):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Pishak Home" in resp.text


# ---- pages ---------------------------------------------------------------

def test_events_page_loads(client, csrf):
    assert client.get("/events").status_code == 200


def test_recordings_page_loads(client, csrf):
    assert client.get("/recordings").status_code == 200


def test_settings_page_loads(client, csrf):
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Security" in resp.text


# ---- status / camera / network -------------------------------------------

def test_api_status(client, csrf):
    resp = client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["app"] == "pishak-home"
    assert "system" in body and "camera" in body and "recording" in body


def test_api_status_reports_version_1_0_0(client, csrf):
    assert client.get("/api/status").json()["version"] == "1.0.0"


def test_api_camera_health(client, csrf):
    resp = client.get("/api/camera/health")
    assert resp.status_code == 200
    assert "recent_log" in resp.json()


def test_api_snapshot_returns_jpeg(client, csrf):
    for _ in range(20):
        resp = client.get("/api/snapshot")
        if resp.status_code == 200:
            break
        time.sleep(0.1)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content.startswith(b"\xff\xd8")


def test_api_network_status(client, csrf):
    resp = client.get("/api/network")
    assert resp.status_code == 200
    assert "interfaces" in resp.json()


def test_api_events_list(client, csrf):
    resp = client.get("/api/events")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ---- settings: CSRF + persistence ------------------------------------------

def test_settings_update_requires_csrf(client):
    resp = client.post("/api/settings/motion", json={"enabled": False})
    assert resp.status_code == 403


def test_settings_update_with_csrf_persists(client, csrf):
    resp = client.post(
        "/api/settings/motion",
        json={"pixel_threshold": 33},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    assert resp.json()["pixel_threshold"] == 33

    reloaded = client.get("/api/settings").json()
    assert reloaded["motion"]["pixel_threshold"] == 33


def test_settings_update_rejects_unknown_key(client, csrf):
    resp = client.post(
        "/api/settings/motion",
        json={"not_a_real_key": 1},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_settings_update_unknown_section_404(client, csrf):
    resp = client.post(
        "/api/settings/nonexistent", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 404


def test_settings_get_redacts_secrets(client, csrf):
    body = client.get("/api/settings").json()
    assert body["security"]["password_hash"] != ""
    assert "•" in body["security"]["password_hash"]


def test_telegram_settings_blank_token_does_not_erase_existing(client, csrf, app_module):
    _, main = app_module
    client.post(
        "/api/settings/telegram",
        json={"enabled": False, "bot_token": "keep-me-secret", "authorized_chat_ids": []},
        headers={"X-CSRF-Token": csrf},
    )
    # Simulate the frontend's "leave blank to keep current token" behavior:
    # it simply omits bot_token from the payload.
    client.post(
        "/api/settings/telegram",
        json={"authorized_chat_ids": [111]},
        headers={"X-CSRF-Token": csrf},
    )
    full = client.get("/api/settings").json()
    assert full["telegram"]["authorized_chat_ids"] == [111]
    # bot_token should be unchanged (still the secret set in the first call),
    # verified directly since the API always redacts it.
    unredacted = main.settings_manager.get_section("telegram")
    assert unredacted["bot_token"] == "keep-me-secret"


# ---- recording -------------------------------------------------------------

def test_manual_record_and_list(client, csrf):
    resp = client.post("/api/record/manual?duration=1", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    assert resp.json()["status"] == "recording_started"
    time.sleep(2)
    recordings = client.get("/api/recordings").json()
    assert any(r["name"].startswith("manual_") for r in recordings)


def test_continuous_start_and_stop(client, csrf):
    resp = client.post("/api/record/continuous/start", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    status = client.get("/api/record/status").json()
    assert status["mode"] == "continuous"
    assert status["active"] is True

    resp = client.post("/api/record/continuous/stop", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    status = client.get("/api/record/status").json()
    assert status["mode"] == "off"


def test_cannot_start_two_recordings_at_once(client, csrf):
    client.post("/api/record/continuous/start", headers={"X-CSRF-Token": csrf})
    resp = client.post("/api/record/manual?duration=1", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 409
    client.post("/api/record/continuous/stop", headers={"X-CSRF-Token": csrf})


# ---- media path safety ------------------------------------------------------

def test_media_recording_path_traversal_blocked(client, csrf):
    resp = client.get("/media/recordings/..%2F..%2Fapp%2Fmain.py")
    assert resp.status_code in (400, 404)


def test_media_recording_not_found(client, csrf):
    resp = client.get("/media/recordings/does-not-exist.mkv")
    assert resp.status_code == 404


# ---- camera restart, clear actions, full reset -----------------------------

def test_camera_restart_requires_csrf(client):
    resp = client.post("/api/camera/restart")
    assert resp.status_code == 403


def test_camera_restart_with_csrf(client, csrf):
    resp = client.post("/api/camera/restart", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    assert resp.json()["status"] == "restarting"
    # give it a moment to reconnect (synthetic fallback in this environment)
    time.sleep(0.5)
    health = client.get("/api/camera/health").json()
    assert health["source"] in ("synthetic-fallback", "device")


def test_clear_events(client, csrf):
    client.get("/api/events")  # warm up, harmless
    resp = client.post("/api/events/clear", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    assert "deleted" in resp.json()
    assert client.get("/api/events").json() == []


def test_clear_recordings_blocked_while_active(client, csrf):
    client.post("/api/record/continuous/start", headers={"X-CSRF-Token": csrf})
    resp = client.post("/api/recordings/clear", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 409
    client.post("/api/record/continuous/stop", headers={"X-CSRF-Token": csrf})


def test_clear_recordings_when_idle(client, csrf):
    resp = client.post("/api/recordings/clear", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    assert client.get("/api/recordings").json() == []


def test_clear_snapshots(client, csrf):
    resp = client.post("/api/snapshots/clear", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    assert client.get("/api/snapshots").json() == []


def test_reset_all_settings_requires_correct_password(client, csrf):
    resp = client.post(
        "/api/settings/reset",
        json={"current_password": "wrong"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_reset_all_settings_restores_defaults_and_credentials(client, csrf, app_module):
    _, main = app_module
    client.post(
        "/api/settings/motion",
        json={"pixel_threshold": 77},
        headers={"X-CSRF-Token": csrf},
    )
    resp = client.post(
        "/api/settings/reset",
        json={"current_password": "pishak"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    assert client.get("/api/settings").json()["motion"]["pixel_threshold"] == 25
    # login is back to the default now
    assert main.auth_manager.authenticate("pishak", "pishak") is True


def test_telegram_snapshot_option_defaults_on_and_is_editable(client, csrf):
    body = client.get("/api/settings").json()
    assert body["telegram"]["send_snapshot_on_motion"] is True

    resp = client.post(
        "/api/settings/telegram",
        json={"send_snapshot_on_motion": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    assert client.get("/api/settings").json()["telegram"]["send_snapshot_on_motion"] is False

    client.post(
        "/api/settings/telegram",
        json={"send_snapshot_on_motion": True},
        headers={"X-CSRF-Token": csrf},
    )


def test_settings_page_has_snapshot_toggle(client, csrf):
    assert "tg-send-photo" in client.get("/settings").text


# ---- logout -----------------------------------------------------------------

def test_logout_then_requires_login_again(client, csrf):
    resp = client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    resp2 = client.get("/", follow_redirects=False)
    assert resp2.status_code == 303
    # Log back in for any subsequent tests relying on the shared client.
    client.post("/login", data={"username": "pishak", "password": "pishak"})
