import os
import tempfile

import pytest


@pytest.fixture
def app(tmp_path, monkeypatch):
    db_path = str(tmp_path / "integration.db")
    monkeypatch.setenv("ORDERS_DB", db_path)
    from app import create_app
    return create_app()


def test_index_ok(app):
    c = app.test_client()
    r = c.get("/")
    assert r.status_code == 200


def test_client_create_and_list(app):
    c = app.test_client()
    r = c.post("/clients/new", data={
        "full_name": "Иванов Иван",
        "phone": "+79000000000",
        "telegram_id": "123",
        "ch_telegram": "on",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "Иванов Иван" in r.get_data(as_text=True)


def test_order_lifecycle(app):
    c = app.test_client()
    c.post("/clients/new", data={"full_name": "Петров Пётр", "telegram_id": "999"}, follow_redirects=True)
    r = c.post("/orders/new", data={
        "client_id": "1", "service_id": "1",
        "description": "Ключница", "price": "800",
    }, follow_redirects=True)
    assert r.status_code == 200

    r = c.post("/orders/1/status", data={"status_id": "3"}, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "готов" in body

    # история сохранилась
    r = c.get("/orders/1")
    body = r.get_data(as_text=True)
    assert body.count("готов") >= 1


def test_status_change_notifies_enabled_channels(app, monkeypatch):
    c = app.test_client()
    c.post("/clients/new", data={
        "full_name": "Тест", "telegram_id": "tg1", "ch_telegram": "on",
    }, follow_redirects=True)
    c.post("/orders/new", data={"client_id": "1", "service_id": "1"}, follow_redirects=True)

    calls = []

    class FakeTG:
        name = "telegram"

        def send(self, recipient_id, message):
            calls.append((recipient_id, message))
            return True

    monkeypatch.setattr(
        "app.build_notifiers",
        lambda: {"telegram": FakeTG(), "vk": None, "max": None},
    )

    r = c.post("/orders/1/status", data={"status_id": "3"}, follow_redirects=True)
    assert r.status_code == 200
    assert calls and calls[0][0] == "tg1"
    assert "готов" in calls[0][1]