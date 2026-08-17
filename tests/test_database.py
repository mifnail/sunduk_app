import pytest

from db_schema import init_db, seed_defaults
from database import Database


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test.db")
    init_db(path)
    seed_defaults(path)
    return Database(path)


def test_statuses_seeded(db):
    statuses = db.list_statuses()
    names = [s["name"] for s in statuses]
    assert names == ["принят", "в работе", "готов", "выдан", "отменён"]


def test_services_seeded(db):
    services = db.list_services()
    assert any(s["name"] == "3D-печать" for s in services)


def test_client_crud(db):
    cid = db.add_client("Иванов Иван", phone="+79000000000",
                        telegram_id="123", vk_id="vk123", max_id="m1")
    client = db.get_client(cid)
    assert client["full_name"] == "Иванов Иван"
    assert client["vk_id"] == "vk123"

    db.update_client(cid, full_name="Иванов Иван Иванович", phone="+79999999999")
    client = db.get_client(cid)
    assert client["full_name"] == "Иванов Иван Иванович"
    assert client["phone"] == "+79999999999"

    assert len(db.list_clients()) == 1
    db.delete_client(cid)
    assert db.get_client(cid) is None


def test_channels(db):
    cid = db.add_client("Тест")
    db.set_channel(cid, "telegram", True)
    db.set_channel(cid, "vk", True)
    db.set_channel(cid, "telegram", False)
    channels = {c["channel"]: c["enabled"] for c in db.list_channels(cid)}
    assert channels == {"telegram": 0, "vk": 1}


def test_service_crud(db):
    sid = db.add_service("Покраска", "шт", 300)
    svc = db.get_service(sid)
    assert svc["name"] == "Покраска"
    assert svc["price"] == 300
    db.update_service(sid, price=350)
    assert db.get_service(sid)["price"] == 350
    db.delete_service(sid)
    assert db.get_service(sid) is None


def test_order_flow_and_history(db):
    cid = db.add_client("Петров Пётр", telegram_id="999")
    sid = db.get_service_by_name("3D-печать")["id"]
    oid = db.add_order(cid, sid, description="Ключница",
                       price=800, status_id=1)

    order = db.get_order(oid)
    assert order["status_name"] == "принят"
    assert order["client_name"] == "Петров Пётр"
    assert order["service_name"] == "3D-печать"

    db.set_order_status(oid, 2)
    db.set_order_status(oid, 3)
    order = db.get_order(oid)
    assert order["status_name"] == "готов"

    history = db.order_history(oid)
    assert [h["status_name"] for h in history] == ["принят", "в работе", "готов"]


def test_filter_by_status_and_client(db):
    c1 = db.add_client("Клиент А")
    c2 = db.add_client("Клиент Б")
    sid = db.get_service_by_name("3D-сканирование")["id"]
    o1 = db.add_order(c1, sid, status_id=1)
    o2 = db.add_order(c1, sid, status_id=2)
    o3 = db.add_order(c2, sid, status_id=2)

    by_status = db.list_orders(status_id=2)
    assert {o["id"] for o in by_status} == {o2, o3}

    by_client = db.list_orders(client_id=c1)
    assert {o["id"] for o in by_client} == {o1, o2}


def test_foreign_keys_enforced(db):
    with pytest.raises(Exception):
        db.add_order(client_id=9999, service_id=1)
