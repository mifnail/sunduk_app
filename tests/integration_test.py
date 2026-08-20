"""Интеграционный тест: полный цикл заказа через Flask + уведомления."""

import io

import pytest

from app import create_app
from services.order_service import OrderService


class FakeNotifier:
    """Записывает уведомления, всегда успешен."""

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
def app(tmp_path, monkeypatch):
    app = create_app(str(tmp_path / "test.db"))
    app.config["TESTING"] = True
    fake = FakeNotifier()
    # Подменяем build_notifiers, чтобы маршруты слали уведомления в фейк
    monkeypatch.setattr("services.order_service.build_notifiers",
                        lambda: {"telegram": fake})
    app.extensions["fake_notifier"] = fake
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def test_full_order_lifecycle(client, app):
    fake = app.extensions["fake_notifier"]

    # 1. Клиент с включённым Telegram-каналом
    r = client.post("/clients/new", data={
        "full_name": "Иван",
        "telegram_id": "111",
        "channel_telegram": "on",
    }, follow_redirects=True)
    assert r.status_code == 200

    # 2. Услуга
    client.post("/services/new", data={"name": "3D-печать", "price": "100"},
                follow_redirects=True)

    # 3. Заказ с фото -> уведомление «принят»
    photo = (io.BytesIO(b"img1"), "photo.jpg")
    r = client.post("/orders/new", data={
        "client_id": "1", "service_id": "1", "description": "Фигурка",
        "price": "100", "photo": photo,
    }, content_type="multipart/form-data", follow_redirects=True)
    assert r.status_code == 200 and "Заказ #1" in r.get_data(as_text=True)
    assert len(fake.sent) == 1
    assert "принят" in fake.sent[0][1]
    assert fake.photos == [("111", b"img1", "", "image/jpeg")]

    # 4. Смена статуса -> уведомление «в работе»
    fake.sent.clear()
    r = client.post("/orders/1/status", data={"status_id": "2"}, follow_redirects=True)
    assert r.status_code == 200 and "в работе" in r.get_data(as_text=True)
    assert len(fake.sent) == 1
    assert "в работе" in fake.sent[0][1]

    # 5. Доп. услуга
    client.post("/services/new", data={"name": "Постобработка", "price": "50"},
                follow_redirects=True)
    client.post("/orders/1/extra/add", data={"service_id": "2", "quantity": "2"},
                follow_redirects=True)

    # 6. Детали заказа содержат всё
    r = client.get("/orders/1")
    body = r.get_data(as_text=True)
    assert "Фигурка" in body
    assert "Постобработка" in body
    assert "в работе" in body
    assert "принят" in body  # история статусов

    # 7. Фото отдаётся
    assert client.get("/orders/photo/1").data == b"img1"

    # 8. Удаление заказа
    r = client.post("/orders/1/delete", follow_redirects=True)
    assert "Заказ удалён" in r.get_data(as_text=True)
    assert client.get("/orders/1").status_code == 404


def test_notification_not_sent_for_disabled_channel(client, app):
    fake = app.extensions["fake_notifier"]
    client.post("/clients/new", data={
        "full_name": "Пётр",
        "telegram_id": "222",
        # канал не включён
    }, follow_redirects=True)
    client.post("/services/new", data={"name": "Печать"}, follow_redirects=True)
    client.post("/orders/new", data={"client_id": "1", "service_id": "1"},
                follow_redirects=True)
    assert fake.sent == []


def test_notification_not_sent_for_empty_id(client, app):
    fake = app.extensions["fake_notifier"]
    client.post("/clients/new", data={
        "full_name": "Пётр",
        "telegram_id": "",
        "channel_telegram": "on",
    }, follow_redirects=True)
    client.post("/services/new", data={"name": "Печать"}, follow_redirects=True)
    client.post("/orders/new", data={"client_id": "1", "service_id": "1"},
                follow_redirects=True)
    assert fake.sent == []