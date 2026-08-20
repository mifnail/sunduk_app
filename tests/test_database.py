"""Unit-тесты Database: CRUD, FK-ограничения, уникальность, история."""

import sqlite3

import pytest

from database import Database


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


# --- Клиенты ---
def test_add_and_get_client(db):
    cid = db.add_client("Иван", phone="+79001234567")
    client = db.get_client(cid)
    assert client["full_name"] == "Иван"
    assert client["phone"] == "+79001234567"


def test_list_clients(db):
    db.add_client("Борис")
    db.add_client("Анна")
    names = [c["full_name"] for c in db.list_clients()]
    assert names == ["Анна", "Борис"]


def test_update_client(db):
    cid = db.add_client("Иван")
    db.update_client(cid, full_name="Пётр", notes="тест")
    client = db.get_client(cid)
    assert client["full_name"] == "Пётр"
    assert client["notes"] == "тест"


def test_update_client_ignores_unknown_fields(db):
    cid = db.add_client("Иван")
    db.update_client(cid, full_name="Пётр", evil="x")
    assert db.get_client(cid)["full_name"] == "Пётр"


def test_delete_client(db):
    cid = db.add_client("Иван")
    db.delete_client(cid)
    assert db.get_client(cid) is None


def test_get_client_by_phone(db):
    db.add_client("Иван", phone="+79001234567")
    found = db.get_client_by_phone("+79001234567")
    assert len(found) == 1 and found[0]["full_name"] == "Иван"


def test_has_orders_for_client(db):
    cid = db.add_client("Иван")
    sid = db.add_service("Печать")
    assert not db.has_orders_for_client(cid)
    db.add_order(cid, sid)
    assert db.has_orders_for_client(cid)


# --- Услуги ---
def test_service_unique_name(db):
    db.add_service("Печать")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_service("Печать")


def test_get_service_by_name(db):
    db.add_service("Печать", unit="г", price=10)
    s = db.get_service_by_name("Печать")
    assert s["price"] == 10 and s["unit"] == "г"


def test_update_service(db):
    sid = db.add_service("Печать", price=10)
    db.update_service(sid, price=12.5)
    assert db.get_service(sid)["price"] == 12.5


def test_has_orders_for_service(db):
    cid = db.add_client("Иван")
    sid = db.add_service("Печать")
    assert not db.has_orders_for_service(sid)
    db.add_order(cid, sid)
    assert db.has_orders_for_service(sid)


# --- Статусы ---
def test_statuses_seeded(db):
    statuses = db.list_statuses()
    assert [s["name"] for s in statuses] == [
        "принят", "в работе", "готов", "выдан", "отменён",
    ]


def test_get_status(db):
    assert db.get_status(1)["name"] == "принят"
    assert db.get_status(999) is None


# --- Заказы ---
def test_add_order_and_join(db):
    cid = db.add_client("Иван")
    sid = db.add_service("Печать")
    oid = db.add_order(cid, sid, description="тест", price=100)
    order = db.get_order(oid)
    assert order["client_name"] == "Иван"
    assert order["service_name"] == "Печать"
    assert order["status_name"] == "принят"
    assert order["price"] == 100


def test_fk_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.add_order(9999, 9999)


def test_order_status_history(db):
    cid = db.add_client("Иван")
    sid = db.add_service("Печать")
    oid = db.add_order(cid, sid, status_id=1)
    db.set_order_status(oid, 2)
    history = db.order_history(oid)
    assert [h["status_id"] for h in history] == [1, 2]
    assert history[1]["status_name"] == "в работе"


def test_list_orders_filters(db):
    cid = db.add_client("Иван")
    sid = db.add_service("Печать")
    db.add_order(cid, sid, status_id=1)
    db.add_order(cid, sid, status_id=3)
    assert len(db.list_orders()) == 2
    assert len(db.list_orders(status_id=1)) == 1
    assert len(db.list_orders(client_id=cid)) == 2
    assert len(db.list_orders(status_id=3, client_id=cid)) == 1


def test_update_order(db):
    cid = db.add_client("Иван")
    sid = db.add_service("Печать")
    oid = db.add_order(cid, sid, price=100)
    db.update_order(oid, price=150, description="новое")
    order = db.get_order(oid)
    assert order["price"] == 150
    assert order["description"] == "новое"


def test_delete_order_cascades(db):
    cid = db.add_client("Иван")
    sid = db.add_service("Печать")
    oid = db.add_order(cid, sid)
    db.add_order_photo(oid, 1, b"x")
    db.add_service_to_order(oid, sid)
    db.delete_order(oid)
    assert db.get_order(oid) is None
    assert len(db.get_order_photos(oid)) == 0
    assert len(db.get_order_services(oid)) == 0


# --- Фото ---
def test_photos(db):
    cid = db.add_client("Иван")
    sid = db.add_service("Печать")
    oid = db.add_order(cid, sid)
    pid = db.add_order_photo(oid, 1, b"data", "image/jpeg", "фото")
    photo = db.get_order_photo(pid)
    assert photo["photo_data"] == b"data"
    assert photo["caption"] == "фото"
    assert len(db.get_order_photos(oid)) == 1


# --- Доп. услуги ---
def test_extra_services_and_totals(db):
    cid = db.add_client("Иван")
    sid = db.add_service("Печать", price=10)
    sid2 = db.add_service("Постобработка", price=50)
    oid = db.add_order(cid, sid, price=100)
    db.add_service_to_order(oid, sid2, quantity=2, price=40)
    assert db.calculate_extra_total(oid) == 80
    assert db.calculate_order_total(oid) == 180
    db.remove_service_from_order(oid, sid2)
    assert db.calculate_extra_total(oid) == 0


def test_extra_service_upsert(db):
    cid = db.add_client("Иван")
    sid = db.add_service("Печать")
    sid2 = db.add_service("Постобработка")
    oid = db.add_order(cid, sid)
    db.add_service_to_order(oid, sid2, quantity=1)
    db.add_service_to_order(oid, sid2, quantity=3)
    extras = db.get_order_services(oid)
    assert len(extras) == 1 and extras[0]["quantity"] == 3


# --- Каналы ---
def test_channels_upsert(db):
    cid = db.add_client("Иван")
    db.set_channel(cid, "telegram", True)
    db.set_channel(cid, "telegram", False)
    assert db.get_client_channels(cid) == {"telegram": False}
    db.set_channel(cid, "vk", True)
    assert db.get_client_channels(cid) == {"telegram": False, "vk": True}


def test_channel_check_constraint(db):
    cid = db.add_client("Иван")
    with pytest.raises(sqlite3.IntegrityError):
        db.set_channel(cid, "sms", True)


# --- Удаление клиента с каналами ---
def test_delete_client_removes_channels(db):
    cid = db.add_client("Иван")
    db.set_channel(cid, "telegram", True)
    db.delete_client(cid)
    assert db.get_client(cid) is None
    assert db.list_channels(cid) == []