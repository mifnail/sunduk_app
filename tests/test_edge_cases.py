import os
import tempfile

import pytest


@pytest.fixture
def app(tmp_path, monkeypatch):
    db_path = str(tmp_path / "edge.db")
    monkeypatch.setenv("ORDERS_DB", db_path)
    from app import create_app
    return create_app()


@pytest.fixture
def db(app):
    from database import Database
    return Database(app.config["DATABASE"])


def test_set_status_with_nonexistent_status_returns_error(app):
    """BUG: статус 999 не существует -> status['name'] -> TypeError -> 500."""
    c = app.test_client()
    c.post("/clients/new", data={"full_name": "Тест"})
    c.post("/orders/new", data={"client_id": "1", "service_id": "1"})
    r = c.post("/orders/1/status", data={"status_id": "999"})
    assert r.status_code != 500


def test_set_status_with_negative_status(app):
    c = app.test_client()
    c.post("/clients/new", data={"full_name": "Тест"})
    c.post("/orders/new", data={"client_id": "1", "service_id": "1"})
    r = c.post("/orders/1/status", data={"status_id": "-1"})
    assert r.status_code != 500


def test_create_order_with_bad_client_returns_error(app):
    """BUG: client_id=9999 -> FK violation -> 500 (необработанное исключение)."""
    c = app.test_client()
    r = c.post("/orders/new", data={"client_id": "9999", "service_id": "1"})
    assert r.status_code != 500


def test_create_order_with_bad_service(app):
    c = app.test_client()
    c.post("/clients/new", data={"full_name": "Тест"})
    r = c.post("/orders/new", data={"client_id": "1", "service_id": "9999"})
    assert r.status_code != 500


def test_create_order_with_non_numeric_client(app):
    c = app.test_client()
    r = c.post("/orders/new", data={"client_id": "abc", "service_id": "1"})
    assert r.status_code != 500


def test_delete_client_with_orders_no_500(app):
    """BUG: клиент с заказами удаляется -> FK violation -> 500."""
    c = app.test_client()
    c.post("/clients/new", data={"full_name": "Тест"})
    c.post("/orders/new", data={"client_id": "1", "service_id": "1"})
    r = c.post("/clients/1/delete", follow_redirects=True)
    assert r.status_code == 200


def test_delete_service_in_use_no_500(app):
    c = app.test_client()
    c.post("/clients/new", data={"full_name": "Тест"})
    c.post("/orders/new", data={"client_id": "1", "service_id": "1"})
    r = c.post("/services/1/delete", follow_redirects=True)
    assert r.status_code == 200


def test_edit_nonexistent_client_404(app):
    c = app.test_client()
    r = c.get("/clients/9999/edit")
    assert r.status_code == 404


def test_nonexistent_order_detail_404(app):
    c = app.test_client()
    assert c.get("/orders/9999").status_code == 404


def test_disabled_channel_not_notified(app, monkeypatch):
    """Отключённый канал не должен получать уведомление."""
    c = app.test_client()
    c.post("/clients/new", data={"full_name": "Тест", "telegram_id": "tg1"})
    c.post("/orders/new", data={"client_id": "1", "service_id": "1"})

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
    c.post("/orders/1/status", data={"status_id": "3"})
    assert calls == []


def test_empty_channel_field_not_notified(app, monkeypatch):
    """Пустой telegram_id не должен вызывать send."""
    c = app.test_client()
    c.post("/clients/new", data={"full_name": "Тест", "ch_telegram": "on"})
    c.post("/orders/new", data={"client_id": "1", "service_id": "1"})

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
    c.post("/orders/1/status", data={"status_id": "3"})
    assert calls == []


def test_empty_full_name_rejected(app):
    """BUG: пустое ФИО создавало клиента."""
    c = app.test_client()
    r = c.post("/clients/new", data={"full_name": ""}, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "Укажите ФИО" in body
    # клиент не создан
    clients = c.get("/clients").get_data(as_text=True)
    assert "<td>Иванов" not in clients


def test_empty_full_name_on_edit_rejected(app):
    """BUG: редактирование клиента с пустым ФИО сохраняло пустоту."""
    c = app.test_client()
    c.post("/clients/new", data={"full_name": "Старый"})
    r = c.post("/clients/1/edit", data={"full_name": ""}, follow_redirects=True)
    assert "Укажите ФИО" in r.get_data(as_text=True)
    # имя не изменилось
    clients = c.get("/clients").get_data(as_text=True)
    assert "Старый" in clients


def test_duplicate_service_rejected_no_500(app):
    """BUG: дубликат услуги давал 500 (UNIQUE constraint)."""
    c = app.test_client()
    r1 = c.post("/services/new", data={"name": "Тест-услуга"})
    r2 = c.post("/services/new", data={"name": "Тест-услуга"}, follow_redirects=True)
    assert r1.status_code != 500
    assert r2.status_code == 200
    body = r2.get_data(as_text=True)
    assert "уже существует" in body
    # в списке услуга одна
    svc = c.get("/services").get_data(as_text=True)
    assert svc.count("Тест-услуга") == 1