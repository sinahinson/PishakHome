from app.notifications import TelegramNotifier, build_notifier


def test_disabled_without_token():
    notifier = TelegramNotifier(bot_token="", authorized_chat_ids=[123], enabled=True)
    assert notifier.enabled is False
    assert notifier.send_message(123, "hi") is False


def test_disabled_without_chat_ids():
    notifier = TelegramNotifier(bot_token="abc", authorized_chat_ids=[], enabled=True)
    assert notifier.enabled is False


def test_enabled_sends_via_injected_http_call():
    calls = []

    def fake_call(method, payload):
        calls.append((method, payload))
        return {"ok": True}

    notifier = TelegramNotifier(
        bot_token="abc", authorized_chat_ids=[111, 222], enabled=True, http_call=fake_call
    )
    assert notifier.enabled is True
    ok = notifier.send_message(111, "hello")
    assert ok is True
    assert calls[0][0] == "sendMessage"
    assert calls[0][1]["chat_id"] == 111


def test_broadcast_sends_to_all_authorized_chats():
    calls = []
    notifier = TelegramNotifier(
        bot_token="abc",
        authorized_chat_ids=[1, 2, 3],
        enabled=True,
        http_call=lambda m, p: calls.append(p) or {"ok": True},
    )
    notifier.broadcast("test message")
    assert len(calls) == 3


def test_notify_event_formats_message():
    calls = []
    notifier = TelegramNotifier(
        bot_token="abc",
        authorized_chat_ids=[1],
        enabled=True,
        http_call=lambda m, p: calls.append(p) or {"ok": True},
    )
    notifier.notify_event("CatDetected", {"zone": "Living Room", "confidence": 0.87})
    assert len(calls) == 1
    assert "Cat detected" in calls[0]["text"]
    assert "Living Room" in calls[0]["text"]


def test_build_notifier_disabled_by_default():
    notifier = build_notifier({"enabled": False, "bot_token": "", "authorized_chat_ids": []})
    assert notifier.enabled is False


def test_build_notifier_applies_proxy_only_when_enabled():
    cfg = {
        "enabled": True,
        "bot_token": "abc",
        "authorized_chat_ids": [1],
        "proxy_enabled": True,
        "proxy_url": "http://127.0.0.1:8080",
    }
    notifier = build_notifier(cfg)
    assert notifier.proxy_url == "http://127.0.0.1:8080"

    cfg["proxy_enabled"] = False
    notifier2 = build_notifier(cfg)
    assert notifier2.proxy_url is None


def test_is_authorized():
    notifier = TelegramNotifier(bot_token="x", authorized_chat_ids=[42], enabled=True)
    assert notifier.is_authorized(42) is True
    assert notifier.is_authorized(99) is False


def test_test_connection_without_token():
    notifier = TelegramNotifier(bot_token="", authorized_chat_ids=[], enabled=False)
    ok, message = notifier.test_connection()
    assert ok is False
    assert "token" in message.lower()


def test_test_connection_success():
    notifier = TelegramNotifier(
        bot_token="abc",
        authorized_chat_ids=[],
        enabled=False,
        http_call=lambda m, p: {"ok": True, "result": {"username": "pishak_bot"}},
    )
    ok, message = notifier.test_connection()
    assert ok is True
    assert "pishak_bot" in message


def test_test_connection_failure_reports_reason():
    notifier = TelegramNotifier(
        bot_token="abc",
        authorized_chat_ids=[],
        enabled=False,
        http_call=lambda m, p: {"ok": False, "description": "Unauthorized"},
    )
    ok, message = notifier.test_connection()
    assert ok is False
    assert "Unauthorized" in message


def test_proxy_url_passed_to_httpx_client(monkeypatch):
    captured = {}

    class FakeResponse:
        def json(self):
            return {"ok": True}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json):
            return FakeResponse()

    import httpx as httpx_module
    monkeypatch.setattr(httpx_module, "Client", FakeClient)

    notifier = TelegramNotifier(
        bot_token="abc", authorized_chat_ids=[1], enabled=True, proxy_url="socks5h://127.0.0.1:1080"
    )
    notifier.send_message(1, "hi")
    assert captured.get("proxy") == "socks5h://127.0.0.1:1080"


# ---- snapshot photos on motion ---------------------------------------------

def _photo_notifier(sent, send_snapshots=True, photo_ok=True):
    def fake_call(method, payload, files=None):
        sent.append((method, payload, files))
        if method == "sendPhoto":
            return {"ok": photo_ok}
        return {"ok": True}

    return TelegramNotifier(
        bot_token="abc", authorized_chat_ids=[1, 2], enabled=True,
        http_call=fake_call, send_snapshots=send_snapshots,
    )


def test_motion_with_snapshot_bytes_sends_photo_with_caption():
    sent = []
    n = _photo_notifier(sent)
    n.notify_event("MotionDetected", {"confidence": 0.12, "snapshot_bytes": b"\xff\xd8jpeg\xff\xd9"})
    assert [m for m, _, _ in sent] == ["sendPhoto", "sendPhoto"]  # one per chat
    method, payload, files = sent[0]
    assert "Motion detected" in payload["caption"]
    assert files["photo"][1] == b"\xff\xd8jpeg\xff\xd9"
    assert files["photo"][2] == "image/jpeg"


def test_motion_photo_read_from_snapshot_path(tmp_path):
    snap = tmp_path / "s.jpg"
    snap.write_bytes(b"\xff\xd8fromdisk\xff\xd9")
    sent = []
    n = _photo_notifier(sent)
    n.notify_event("MotionDetected", {"snapshot_path": str(snap)})
    assert sent[0][0] == "sendPhoto"
    assert sent[0][2]["photo"][1] == b"\xff\xd8fromdisk\xff\xd9"


def test_motion_photo_disabled_sends_text_only():
    sent = []
    n = _photo_notifier(sent, send_snapshots=False)
    n.notify_event("MotionDetected", {"snapshot_bytes": b"\xff\xd8x\xff\xd9"})
    assert [m for m, _, _ in sent] == ["sendMessage", "sendMessage"]


def test_motion_without_any_snapshot_sends_text_only():
    sent = []
    n = _photo_notifier(sent)
    n.notify_event("MotionDetected", {"snapshot_path": "/does/not/exist.jpg"})
    assert [m for m, _, _ in sent] == ["sendMessage", "sendMessage"]


def test_failed_photo_falls_back_to_text_so_alert_is_not_lost():
    sent = []
    n = _photo_notifier(sent, photo_ok=False)
    n.notify_event("MotionDetected", {"snapshot_bytes": b"\xff\xd8x\xff\xd9"})
    methods = [m for m, _, _ in sent]
    assert methods == ["sendPhoto", "sendMessage", "sendPhoto", "sendMessage"]


def test_non_motion_events_never_send_photos():
    sent = []
    n = _photo_notifier(sent)
    n.notify_event("StorageLow", {"snapshot_bytes": b"\xff\xd8x\xff\xd9"})
    assert all(m == "sendMessage" for m, _, _ in sent)


def test_build_notifier_reads_send_snapshot_option():
    cfg = {"enabled": True, "bot_token": "abc", "authorized_chat_ids": [1],
           "send_snapshot_on_motion": True}
    assert build_notifier(cfg).send_snapshots is True
    cfg["send_snapshot_on_motion"] = False
    assert build_notifier(cfg).send_snapshots is False


def test_photo_upload_uses_multipart_and_error_never_leaks_token(monkeypatch):
    import httpx as httpx_module

    captured = {}

    class FakeResponse:
        def json(self):
            return {"ok": True}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kwargs):
            captured["post"] = kwargs
            return FakeResponse()

    monkeypatch.setattr(httpx_module, "Client", FakeClient)
    n = TelegramNotifier(bot_token="abc", authorized_chat_ids=[1], enabled=True)
    assert n.send_photo(1, b"\xff\xd8x\xff\xd9", caption="hi") is True
    assert "files" in captured["post"] and "json" not in captured["post"]
    assert captured["kwargs"]["timeout"] >= 30

    class BoomClient(FakeClient):
        def post(self, url, **kwargs):
            raise httpx_module.ConnectError(f"cannot connect to {url}")

    monkeypatch.setattr(httpx_module, "Client", BoomClient)
    result = n._call_api("sendMessage", {"chat_id": 1, "text": "x"})
    assert result["ok"] is False
    assert "abc" not in result["error"]  # bot token must never leak


def test_send_photo_produces_real_multipart_request(monkeypatch):
    """Runs the real httpx serialization (no network) and checks what would
    actually be sent to Telegram's sendPhoto endpoint."""
    import httpx as httpx_module

    seen = {}

    def handler(request: httpx_module.Request) -> httpx_module.Response:
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers["content-type"]
        seen["body"] = request.read()
        return httpx_module.Response(200, json={"ok": True, "result": {}})

    real_client = httpx_module.Client
    monkeypatch.setattr(
        httpx_module, "Client",
        lambda **kw: real_client(transport=httpx_module.MockTransport(handler), **kw),
    )
    jpeg = b"\xff\xd8REALPHOTOBYTES\xff\xd9"
    n = TelegramNotifier(bot_token="TOKEN123", authorized_chat_ids=[42], enabled=True, send_snapshots=True)
    n.notify_event("MotionDetected", {"confidence": 0.31, "snapshot_bytes": jpeg})

    assert seen["url"].endswith("/botTOKEN123/sendPhoto")
    assert seen["content_type"].startswith("multipart/form-data")
    assert jpeg in seen["body"]
    assert b'name="chat_id"' in seen["body"] and b"42" in seen["body"]
    assert b'name="caption"' in seen["body"] and b"Motion detected" in seen["body"]
    assert b'name="photo"' in seen["body"]


def test_bot_token_not_written_to_logs_by_httpx():
    """httpx logs full request URLs at INFO, and Telegram's URL contains the
    bot token — importing the app must silence that."""
    import importlib
    import logging

    importlib.import_module("app.main")  # importing the app applies the logger levels

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
