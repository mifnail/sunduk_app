import pytest

from notifier import send_notifications, order_status_message


class FakeNotifier:
    name = "fake"
    fail = False

    def send(self, recipient_id, message):
        if self.fail:
            raise RuntimeError("канал недоступен")
        return True


class SilentFail:
    """Канал, который возвращает False вместо исключения."""
    name = "silent"

    def send(self, recipient_id, message):
        return False


def test_send_to_all_channels():
    notifiers = {"tg": FakeNotifier(), "vk": FakeNotifier()}
    result = send_notifications(
        {"tg": "1", "vk": "2"}, "привет", notifiers=notifiers
    )
    assert result == {"tg": True, "vk": True}


def test_failing_channel_does_not_break_others():
    notifiers = {"tg": FakeNotifier(), "vk": FakeNotifier()}
    notifiers["tg"].fail = True
    result = send_notifications(
        {"tg": "1", "vk": "2"}, "привет", notifiers=notifiers
    )
    assert result["tg"] is False
    assert result["vk"] is True


def test_missing_recipient_skipped():
    result = send_notifications({"tg": ""}, "привет", notifiers={"tg": FakeNotifier()})
    assert result == {}


def test_silent_fail_reports_false():
    result = send_notifications(
        {"tg": "1"}, "привет", notifiers={"tg": SilentFail()}
    )
    assert result == {"tg": False}


def test_unknown_channel_false():
    result = send_notifications(
        {"nosuch": "1"}, "привет", notifiers={"tg": FakeNotifier()}
    )
    assert result == {"nosuch": False}


def test_message_building():
    order = {"id": 1, "service_name": "3D-печать", "description": "Ключница"}
    text = order_status_message(order, "готов")
    assert "заказ #1" in text
    assert "3D-печать" in text
    assert "готов" in text
    assert "Ключница" in text