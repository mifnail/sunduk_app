"""Тесты Flask-маршрутов: PRG, валидация, обработка ошибок, edge cases."""

import io

import pytest

from app import create_app


@pytest.fixture(autouse=True)
def no_real_notifications(monkeypatch):
    """Не даём тестам уходить в реальные API уведомлений."""
    monkeypatch.setattr("services.order_service.build_notifiers", lambda: {})


@pytest.fixture
def app(tmp_path):
    return create_app(str(tmp_path / "test.db"))


@pytest.fixture
def client(app):
    app.config["TESTING"] = True
    return app.test_client()


def _seed(client):
    client.post("/clients/new", data={"full_name": "Иван", "telegram_id": "1"})
    client.post("/services/new", data={"name": "Печать", "price": "10"})


# --- Главная ---
def test_index_redirects(client):
    r = client.get("/")
    assert r.status_code == 302
    assert "/orders" in r.headers["Location"]


# --- Клиенты ---
def test_clients_crud(client):
    r = client.post("/clients/new", data={"full_name": "Иван"}, follow_redirects=True)
    assert r.status_code == 200 and "Иван" in r.get_data(as_text=True)
    r = client.post("/clients/1/edit", data={"full_name": "Пётр"}, follow_redirects=True)
    assert "Пётр" in r.get_data(as_text=True)
    r = client.post("/clients/1/delete", follow_redirects=True)
    assert "Клиент удалён" in r.get_data(as_text=True)


def test_client_empty_name(client):
    r = client.post("/clients/new", data={"full_name": "   "}, follow_redirects=True)
    assert "Укажите ФИО" in r.get_data(as_text=True)


def test_client_edit_404(client):
    assert client.get("/clients/999/edit").status_code == 404


def test_client_channels_saved(client):
    client.post("/clients/new", data={
        "full_name": "Иван", "telegram_id": "1",
        "channel_telegram": "on", "channel_vk": "on",
    })
    r = client.get("/clients")
    body = r.get_data(as_text=True)
    assert "telegram" in body and "vk" in body


def test_delete_client_with_orders_blocked(client):
    _seed(client)
    client.post("/orders/new", data={"client_id": "1", "service_id": "1"})
    r = client.post("/clients/1/delete", follow_redirects=True)
    assert "Нельзя удалить" in r.get_data(as_text=True)


# --- Услуги ---
def test_services_crud(client):
    r = client.post("/services/new", data={"name": "Печать", "price": "10"},
                    follow_redirects=True)
    assert "Печать" in r.get_data(as_text=True)
    r = client.post("/services/1/update", data={"name": "Печать 2", "price": "20"},
                    follow_redirects=True)
    assert "Печать 2" in r.get_data(as_text=True)
    r = client.post("/services/1/delete", follow_redirects=True)
    assert "Услуга удалена" in r.get_data(as_text=True)


def test_service_duplicate(client):
    client.post("/services/new", data={"name": "Печать"})
    r = client.post("/services/new", data={"name": "Печать"}, follow_redirects=True)
    assert "уже существует" in r.get_data(as_text=True)


def test_delete_service_with_orders_blocked(client):
    _seed(client)
    client.post("/orders/new", data={"client_id": "1", "service_id": "1"})
    r = client.post("/services/1/delete", follow_redirects=True)
    assert "Нельзя удалить" in r.get_data(as_text=True)


# --- Заказы ---
def test_order_flow(client):
    _seed(client)
    r = client.post("/orders/new", data={
        "client_id": "1", "service_id": "1", "description": "тест", "price": "100",
    }, follow_redirects=True)
    assert "Заказ #1" in r.get_data(as_text=True)
    assert "тест" in r.get_data(as_text=True)


def test_order_invalid_client(client):
    _seed(client)
    r = client.post("/orders/new", data={"client_id": "9999", "service_id": "1"},
                    follow_redirects=True)
    assert "Выберите существующего клиента" in r.get_data(as_text=True)


def test_order_invalid_service(client):
    _seed(client)
    r = client.post("/orders/new", data={"client_id": "1", "service_id": "9999"},
                    follow_redirects=True)
    assert "Выберите существующую услугу" in r.get_data(as_text=True)


def test_order_404(client):
    assert client.get("/orders/999").status_code == 404


def test_order_list_filters(client):
    _seed(client)
    client.post("/orders/new", data={"client_id": "1", "service_id": "1"})
    r = client.get("/orders")
    assert "Иван" in r.get_data(as_text=True)
    r = client.get("/orders?status=1")
    assert "Иван" in r.get_data(as_text=True)
    r = client.get("/orders?status=3")
    assert "Заказов нет" in r.get_data(as_text=True)


def test_order_status_validation(client):
    _seed(client)
    client.post("/orders/new", data={"client_id": "1", "service_id": "1"})
    for bad in ("999", "-1", "abc", ""):
        r = client.post("/orders/1/status", data={"status_id": bad},
                        follow_redirects=True)
        assert r.status_code == 200, bad
        body = r.get_data(as_text=True)
        assert ("Неверный статус" in body) or ("Статус не найден" in body), bad


def test_order_status_change(client):
    _seed(client)
    client.post("/orders/new", data={"client_id": "1", "service_id": "1"})
    r = client.post("/orders/1/status", data={"status_id": "2"}, follow_redirects=True)
    assert "в работе" in r.get_data(as_text=True)


def test_order_status_same(client):
    _seed(client)
    client.post("/orders/new", data={"client_id": "1", "service_id": "1"})
    r = client.post("/orders/1/status", data={"status_id": "1"}, follow_redirects=True)
    assert "Статус не изменился" in r.get_data(as_text=True)


def test_order_edit(client):
    _seed(client)
    client.post("/orders/new", data={"client_id": "1", "service_id": "1"})
    r = client.post("/orders/1/edit", data={
        "client_id": "1", "service_id": "1", "description": "новое описание",
        "price": "200", "deadline": "2026-10-01",
    }, follow_redirects=True)
    assert "новое описание" in r.get_data(as_text=True)


def test_photo_upload_and_download(client):
    _seed(client)
    photo = (io.BytesIO(b"imgdata"), "photo.jpg")
    client.post("/orders/new", data={
        "client_id": "1", "service_id": "1", "photo": photo,
    }, content_type="multipart/form-data")
    r = client.get("/orders/photo/1")
    assert r.status_code == 200 and r.data == b"imgdata"
    assert client.get("/orders/photo/999").status_code == 404


def test_extra_services(client):
    _seed(client)
    client.post("/services/new", data={"name": "Постобработка"})
    client.post("/orders/new", data={"client_id": "1", "service_id": "1"})
    r = client.post("/orders/1/extra/add", data={"service_id": "2", "quantity": "2"},
                    follow_redirects=True)
    assert "Постобработка" in r.get_data(as_text=True)
    r = client.post("/orders/1/extra/add", data={"service_id": "9999"},
                    follow_redirects=True)
    assert "Выберите существующую услугу" in r.get_data(as_text=True)
    r = client.post("/orders/1/extra/remove/2", follow_redirects=True)
    assert "Доп. услуг нет" in r.get_data(as_text=True)


def test_delete_order(client):
    _seed(client)
    client.post("/orders/new", data={"client_id": "1", "service_id": "1"})
    r = client.post("/orders/1/delete", follow_redirects=True)
    assert "Заказ удалён" in r.get_data(as_text=True)
    assert client.get("/orders/1").status_code == 404