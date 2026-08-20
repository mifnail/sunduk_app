"""Unit-тесты notifier: моки HTTP для Telegram/VK/MAX, изоляция ошибок."""

import pytest
import requests

from notifier import (MaxNotifier, TelegramNotifier, VKNotifier,
                      build_notifiers, order_status_message, send_notifications)


class FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json = json_data if json_data is not None else {}
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


# --- Telegram ---
def test_telegram_send(monkeypatch):
    calls = {}

    def fake_post(url, **kwargs):
        calls["url"] = url
        calls["data"] = kwargs.get("data")
        return FakeResponse({"ok": True})

    monkeypatch.setattr(requests, "post", fake_post)
    n = TelegramNotifier(token="t")
    assert n.send("123", "hi") is True
    assert "sendMessage" in calls["url"]
    assert calls["data"]["chat_id"] == "123"
    assert calls["data"]["text"] == "hi"


def test_telegram_send_photo(monkeypatch):
    calls = {}

    def fake_post(url, **kwargs):
        calls["files"] = kwargs.get("files")
        return FakeResponse({"ok": True})

    monkeypatch.setattr(requests, "post", fake_post)
    n = TelegramNotifier(token="t")
    assert n.send_photo("123", b"img", caption="фото") is True
    assert calls["files"]["photo"][1] == b"img"


def test_telegram_failure_raises(monkeypatch):
    def fake_post(url, **kwargs):
        return FakeResponse({"ok": False}, status_code=500)

    monkeypatch.setattr(requests, "post", fake_post)
    n = TelegramNotifier(token="t")
    with pytest.raises(requests.HTTPError):
        n.send("123", "hi")


def test_telegram_requires_token():
    with pytest.raises(ValueError):
        TelegramNotifier(token=None)


# --- VK ---
def test_vk_send(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs.get("data")))
        if url.endswith("messages.send"):
            return FakeResponse({"response": 1})
        return FakeResponse({})

    monkeypatch.setattr(requests, "post", fake_post)
    n = VKNotifier(token="t")
    assert n.send("123", "hi") is True
    assert calls[0][0].endswith("messages.send")
    assert calls[0][1]["user_id"] == "123"
    assert calls[0][1]["message"] == "hi"


def test_vk_send_photo_four_steps(monkeypatch):
    responses = iter([
        FakeResponse({"response": {"upload_url": "http://upload"}}),
        FakeResponse({"photo": "p", "server": 1, "hash": "h"}),
        FakeResponse({"response": [{"owner_id": 1, "id": 2}]}),
        FakeResponse({"response": 1}),
    ])

    def fake_post(url, **kwargs):
        return next(responses)

    monkeypatch.setattr(requests, "post", fake_post)
    n = VKNotifier(token="t")
    assert n.send_photo("123", b"img") is True


def test_vk_api_error_raises(monkeypatch):
    def fake_post(url, **kwargs):
        return FakeResponse({"error": {"error_code": 5, "error_msg": "auth"}})

    monkeypatch.setattr(requests, "post", fake_post)
    n = VKNotifier(token="t")
    with pytest.raises(RuntimeError):
        n.send("123", "hi")


# --- MAX ---
def test_max_send(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs.get("json")))
        return FakeResponse({})

    monkeypatch.setattr(requests, "post", fake_post)
    n = MaxNotifier(token="t", endpoint="https://max.example")
    assert n.send("123", "hi") is True
    assert calls[0][0] == "https://max.example/messages"
    assert calls[0][1]["recipient_id"] == "123"
    assert calls[0][1]["text"] == "hi"


def test_max_send_photo(monkeypatch):
    responses = iter([
        FakeResponse({"file_id": "f1"}),
        FakeResponse({}),
    ])

    def fake_post(url, **kwargs):
        return next(responses)

    monkeypatch.setattr(requests, "post", fake_post)
    n = MaxNotifier(token="t", endpoint="https://max.example")
    assert n.send_photo("123", b"img") is True


def test_max_default_endpoint():
    n = MaxNotifier(token="t")
    assert n.endpoint == "https://platform-api2.max.ru"


# --- build_notifiers ---
def test_build_notifiers(monkeypatch):
    monkeypatch.setenv("TG_TOKEN", "t")
    monkeypatch.setenv("VK_TOKEN", "v")
    monkeypatch.setenv("MAX_TOKEN", "m")
    notifiers = build_notifiers()
    assert set(notifiers) == {"telegram", "vk", "max"}
    assert isinstance(notifiers["telegram"], TelegramNotifier)
    assert isinstance(notifiers["vk"], VKNotifier)
    assert isinstance(notifiers["max"], MaxNotifier)


def test_build_notifiers_empty(monkeypatch):
    monkeypatch.delenv("TG_TOKEN", raising=False)
    monkeypatch.delenv("VK_TOKEN", raising=False)
    monkeypatch.delenv("MAX_TOKEN", raising=False)
    assert build_notifiers() == {}


# --- send_notifications ---
class _Ok:
    def __init__(self, name="ok"):
        self.name = name
        self.sent = []

    def send(self, recipient_id, message):
        self.sent.append(recipient_id)
        return True


class _Fail:
    def send(self, recipient_id, message):
        raise RuntimeError("boom")


def test_send_all_channels():
    notifiers = {"telegram": _Ok(), "vk": _Ok()}
    res = send_notifications({"telegram": "1", "vk": "2"}, "msg", notifiers)
    assert res == {"telegram": True, "vk": True}


def test_send_failure_isolation():
    notifiers = {"telegram": _Ok(), "vk": _Fail()}
    errors = []
    res = send_notifications({"telegram": "1", "vk": "2"}, "msg", notifiers,
                             on_error=lambda ch, exc: errors.append(ch))
    assert res == {"telegram": True, "vk": False}
    assert errors == ["vk"]


def test_send_empty_id_skipped():
    class _Never:
        def send(self, recipient_id, message):
            raise AssertionError("не должен вызываться")

    res = send_notifications({"telegram": ""}, "msg", {"telegram": _Never()})
    assert res == {"telegram": False}


def test_send_missing_notifier_skipped():
    res = send_notifications({"telegram": "1"}, "msg", {})
    assert res == {"telegram": False}


def test_send_with_photo():
    class _Fake:
        def __init__(self):
            self.sent = []
            self.photos = []

        def send(self, recipient_id, message):
            self.sent.append(message)
            return True

        def send_photo(self, recipient_id, photo_data, caption="", mime_type="image/jpeg"):
            self.photos.append((photo_data, caption, mime_type))
            return True

    f = _Fake()
    res = send_notifications({"telegram": "1"}, "msg", {"telegram": f},
                             photo_data=b"img", photo_caption="фото")
    assert res == {"telegram": True}
    assert f.sent == ["msg"]
    assert f.photos == [(b"img", "фото", "image/jpeg")]


def test_send_photo_failure_marks_channel_false():
    class _Fake:
        def send(self, recipient_id, message):
            return True

        def send_photo(self, recipient_id, photo_data, caption="", mime_type="image/jpeg"):
            raise RuntimeError("photo fail")

    res = send_notifications({"telegram": "1"}, "msg", {"telegram": _Fake()},
                             photo_data=b"img")
    assert res == {"telegram": False}


# --- order_status_message ---
def test_order_status_message():
    order = {"id": 1, "service_name": "Печать", "description": "деталь"}
    msg = order_status_message(order, "готов")
    assert "заказ #1" in msg
    assert "Печать" in msg
    assert "«готов»" in msg
    assert "деталь" in msg


def test_order_status_message_no_description():
    order = {"id": 2, "service_name": "Скан", "description": None}
    msg = order_status_message(order, "принят")
    assert "—" in msg