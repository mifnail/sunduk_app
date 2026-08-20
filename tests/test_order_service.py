"""Unit-тесты OrderService: бизнес-логика, история, уведомления (моки)."""

import pytest

from database import Database
from services.order_service import OrderService


class FakeNotifier:
    """Записывает вызовы send/send_photo, всегда успешен."""

    def __init__(self, name="telegram"):
        self.name = name
        self.sent = []
        self.photos = []

    def send(self, recipient_id, message):
        self.sent.append((recipient_id, message))
        return True

    def send_photo(self, recipient_id, photo_data, caption="", mime_type="image/jpeg"):
        self.photos.append((recipient_id, photo_data, caption, mime_type))
        return True


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


@pytest.fixture
def service(db):
    return OrderService(db, notifiers={"telegram": FakeNotifier()})


def _client_with_channel(db, telegram_id="111"):
    cid = db.add_client("Иван", telegram_id=telegram_id)
    db.set_channel(cid, "telegram", True)
    return cid


# --- Создание заказа ---
def test_create_order_notifies(db, service):
    cid = _client_with_channel(db)
    sid = db.add_service("Печать")
    oid = service.create_order(cid, sid, description="тест")
    assert oid == 1
    assert db.get_order(oid)["status_name"] == "принят"
    notifier = service._get_notifiers()["telegram"]
    assert len(notifier.sent) == 1
    assert "принят" in notifier.sent[0][1]


def test_create_order_with_photo(db, service):
    cid = _client_with_channel(db)
    sid = db.add_service("Печать")
    oid = service.create_order(cid, sid, photo_data=b"img", photo_caption="скан")
    photos = db.get_order_photos(oid)
    assert len(photos) == 1
    assert photos[0]["photo_data"] == b"img"
    notifier = service._get_notifiers()["telegram"]
    assert notifier.photos == [("111", b"img", "скан", "image/jpeg")]


def test_create_order_initial_history(db, service):
    cid = db.add_client("Иван")
    sid = db.add_service("Печать")
    oid = service.create_order(cid, sid, status_id=1)
    assert [h["status_id"] for h in db.order_history(oid)] == [1]


# --- Смена статуса ---
def test_change_status_history_and_notify(db, service):
    cid = _client_with_channel(db)
    sid = db.add_service("Печать")
    oid = db.add_order(cid, sid, status_id=1)
    assert service.change_status(oid, 2) is True
    assert db.get_order(oid)["status_name"] == "в работе"
    assert [h["status_id"] for h in db.order_history(oid)] == [1, 2]
    notifier = service._get_notifiers()["telegram"]
    assert len(notifier.sent) == 1
    assert "в работе" in notifier.sent[0][1]


def test_change_status_same_status_noop(db, service):
    cid = db.add_client("Иван")
    sid = db.add_service("Печать")
    oid = db.add_order(cid, sid, status_id=1)
    assert service.change_status(oid, 1) is False
    assert len(db.order_history(oid)) == 1


def test_change_status_unknown(db, service):
    cid = db.add_client("Иван")
    sid = db.add_service("Печать")
    oid = db.add_order(cid, sid)
    assert service.change_status(oid, 999) is False
    assert service.change_status(9999, 2) is False


def test_change_status_with_photo(db, service):
    cid = _client_with_channel(db)
    sid = db.add_service("Печать")
    oid = db.add_order(cid, sid, status_id=1)
    assert service.change_status(oid, 3, photo_data=b"img") is True
    photos = db.get_order_photos(oid)
    assert len(photos) == 1 and photos[0]["status_id"] == 3


# --- Уведомления ---
def test_notify_disabled_channel_skipped(db, service):
    cid = db.add_client("Иван", telegram_id="111")
    db.set_channel(cid, "telegram", False)  # выключен
    sid = db.add_service("Печать")
    oid = db.add_order(cid, sid)
    service.notify_status_change(oid)
    assert service._get_notifiers()["telegram"].sent == []


def test_notify_empty_id_skipped(db, service):
    cid = db.add_client("Иван", telegram_id="")
    db.set_channel(cid, "telegram", True)
    sid = db.add_service("Печать")
    oid = db.add_order(cid, sid)
    service.notify_status_change(oid)
    assert service._get_notifiers()["telegram"].sent == []


def test_notify_unknown_order_returns_empty(db, service):
    assert service.notify_status_change(9999) == {}


# --- Доп. услуги ---
def test_extra_services(db, service):
    cid = db.add_client("Иван")
    sid = db.add_service("Печать")
    sid2 = db.add_service("Постобработка")
    oid = db.add_order(cid, sid)
    assert service.add_extra_service(oid, sid2, quantity=2) is True
    assert service.add_extra_service(9999, sid2) is False
    assert service.add_extra_service(oid, 9999) is False
    assert service.remove_extra_service(oid, sid2) is True
    assert service.remove_extra_service(9999, sid2) is False


# --- Детали и список ---
def test_get_order_detail(db, service):
    cid = db.add_client("Иван")
    sid = db.add_service("Печать", price=10)
    oid = db.add_order(cid, sid, price=100)
    detail = service.get_order_detail(oid)
    assert detail["client_name"] == "Иван"
    assert detail["total"] == 100
    assert detail["history"] and detail["photos"] == []
    assert service.get_order_detail(9999) is None


def test_list_orders(db, service):
    cid = db.add_client("Иван")
    sid = db.add_service("Печать")
    db.add_order(cid, sid, status_id=1)
    db.add_order(cid, sid, status_id=3)
    assert len(service.list_orders()) == 2
    assert len(service.list_orders(status_id=3)) == 1
    assert len(service.list_orders(client_id=cid)) == 2