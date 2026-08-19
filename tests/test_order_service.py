"""Unit-тесты для OrderService."""

import pytest
from unittest.mock import Mock, patch

from db_schema import init_db, seed_defaults, migrate_db
from database import Database
from services.order_service import OrderService, NotificationPayload


@pytest.fixture
def order_service(tmp_path):
    """Создаёт OrderService с временной БД."""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    seed_defaults(db_path)
    migrate_db(db_path)
    db = Database(db_path)
    return OrderService(db)


@pytest.fixture
def mock_notifiers():
    """Мокает notifiers для изоляции тестов."""
    with patch("services.order_service.send_notifications") as mock_send:
        mock_send.return_value = {"telegram": True, "vk": True, "max": True}
        yield mock_send


class TestOrderServiceCreateOrder:
    """Тесты создания заказа."""

    def test_create_order_without_photo(self, order_service, mock_notifiers):
        services = order_service.db.list_services()
        client_id = order_service.db.add_client("Иван", phone="+7999", telegram_id="123")

        order_id = order_service.create_order(
            client_id=client_id,
            service_id=services[0]["id"],
            description="Тестовый заказ",
            price=1000.0,
        )

        assert order_id > 0
        order = order_service.db.get_order(order_id)
        assert order["description"] == "Тестовый заказ"
        assert order["price"] == 1000.0
        assert order["status_id"] == 1  # принят

        # Проверка уведомления
        mock_notifiers.assert_called_once()
        args, kwargs = mock_notifiers.call_args
        assert kwargs["photo_data"] is None

    def test_create_order_with_photo(self, order_service, mock_notifiers):
        services = order_service.db.list_services()
        client_id = order_service.db.add_client("Петр", phone="+7999", telegram_id="123")

        photo_data = b"fake_jpeg_data"
        order_id = order_service.create_order(
            client_id=client_id,
            service_id=services[0]["id"],
            description="Заказ с фото",
            photo_data=photo_data,
            photo_caption="Фото поломки",
        )

        assert order_id > 0
        photos = order_service.db.get_order_photos(order_id)
        assert len(photos) == 1
        assert photos[0]["photo_data"] == photo_data
        assert photos[0]["caption"] == "Фото поломки"

        # Проверка уведомления с фото
        mock_notifiers.assert_called_once()
        args, kwargs = mock_notifiers.call_args
        assert kwargs["photo_data"] == photo_data
        assert "Новый заказ" in kwargs["photo_caption"]


class TestOrderServiceChangeStatus:
    """Тесты смены статуса."""

    def test_change_status_without_photo(self, order_service, mock_notifiers):
        services = order_service.db.list_services()
        client_id = order_service.db.add_client("Тест", phone="+7999")
        order_id = order_service.create_order(client_id, services[0]["id"])

        result = order_service.change_status(order_id, 2)  # в работу
        assert result is True

        order = order_service.db.get_order(order_id)
        assert order["status_id"] == 2
        assert order["status_name"] == "в работе"

        # Уведомление без фото
        mock_notifiers.assert_called()
        args, kwargs = mock_notifiers.call_args
        assert kwargs["photo_data"] is None

    def test_change_status_with_photo(self, order_service, mock_notifiers):
        services = order_service.db.list_services()
        client_id = order_service.db.add_client("Тест", phone="+7999")
        # Создаём заказ БЕЗ фото
        order_id = order_service.create_order(client_id, services[0]["id"])

        photo_data = b"status_photo"
        result = order_service.change_status(
            order_id, 3, photo_data=photo_data, photo_caption="3D-скан готов"
        )
        assert result is True

        photos = order_service.db.get_order_photos(order_id)
        assert len(photos) == 1
        assert photos[0]["photo_data"] == photo_data
        assert photos[0]["caption"] == "3D-скан готов"

        # Уведомление с фото
        mock_notifiers.assert_called()
        args, kwargs = mock_notifiers.call_args
        assert kwargs["photo_data"] == photo_data
        assert "Статус: готов" in kwargs["photo_caption"]

    def test_change_status_same_status_returns_false(self, order_service):
        services = order_service.db.list_services()
        client_id = order_service.db.add_client("Тест", phone="+7999")
        order_id = order_service.create_order(client_id, services[0]["id"])

        # Попытка поставить тот же статус
        result = order_service.change_status(order_id, 1)
        assert result is False

    def test_change_status_nonexistent_order_returns_false(self, order_service):
        result = order_service.change_status(999, 2)
        assert result is False

    def test_change_status_nonexistent_status_returns_false(self, order_service):
        services = order_service.db.list_services()
        client_id = order_service.db.add_client("Тест", phone="+7999")
        order_id = order_service.create_order(client_id, services[0]["id"])

        result = order_service.change_status(order_id, 999)
        assert result is False


class TestOrderServiceNotifyStatusChange:
    """Тесты уведомлений."""

    def test_notify_status_change_calls_send_notifications(self, order_service, mock_notifiers):
        services = order_service.db.list_services()
        client_id = order_service.db.add_client("Тест", phone="+7999", telegram_id="tg123")
        order_id = order_service.create_order(client_id, services[0]["id"])

        # Reset mock to ignore create_order call
        mock_notifiers.reset_mock()

        order_service.notify_status_change(
            order_id=order_id,
            status_name="готов",
            payload=NotificationPayload(photo_data=b"photo", photo_caption="Готово"),
        )

        mock_notifiers.assert_called_once()
        args, kwargs = mock_notifiers.call_args
        assert kwargs["photo_data"] == b"photo"
        assert kwargs["photo_caption"] == "Готово"

    def test_notify_status_change_no_recipients_skipped(self, order_service, mock_notifiers):
        services = order_service.db.list_services()
        client_id = order_service.db.add_client("Без контактов", phone="+7999")
        order_id = order_service.create_order(client_id, services[0]["id"])

        # Reset mock to ignore create_order call
        mock_notifiers.reset_mock()

        order_service.notify_status_change(order_id, "готов")

        # Каналы есть, но идентификаторов нет — уведомление не отправляется
        mock_notifiers.assert_called_once()
        args, kwargs = mock_notifiers.call_args
        assert len(args[0]) == 0  # пустой dict каналов


class TestOrderServiceExtraServices:
    """Тесты доп. услуг."""

    def test_add_extra_service(self, order_service):
        services = order_service.db.list_services()
        client_id = order_service.db.add_client("Тест", phone="+7999")
        order_id = order_service.create_order(client_id, services[0]["id"], price=1000)

        extra_sid = services[1]["id"]
        result = order_service.add_extra_service(order_id, extra_sid, quantity=2, price=500)
        assert result is True

        extra = order_service.db.get_order_services(order_id)
        assert len(extra) == 1
        assert extra[0]["service_id"] == extra_sid
        assert extra[0]["quantity"] == 2
        assert extra[0]["price"] == 500

        total = order_service.db.calculate_order_total(order_id)
        # main: price + extra: 500 * 2
        assert total == 1000 + 1000

    def test_add_extra_service_updates_existing(self, order_service):
        services = order_service.db.list_services()
        client_id = order_service.db.add_client("Тест", phone="+7999")
        order_id = order_service.create_order(client_id, services[0]["id"])

        extra_sid = services[1]["id"]
        order_service.add_extra_service(order_id, extra_sid, quantity=1, price=300)
        order_service.add_extra_service(order_id, extra_sid, quantity=5, price=400)

        extra = order_service.db.get_order_services(order_id)
        assert len(extra) == 1
        assert extra[0]["quantity"] == 5
        assert extra[0]["price"] == 400

    def test_remove_extra_service(self, order_service):
        services = order_service.db.list_services()
        client_id = order_service.db.add_client("Тест", phone="+7999")
        order_id = order_service.create_order(client_id, services[0]["id"])

        extra_sid = services[1]["id"]
        order_service.add_extra_service(order_id, extra_sid, quantity=1)
        result = order_service.remove_extra_service(order_id, extra_sid)
        assert result is True

        extra = order_service.db.get_order_services(order_id)
        assert len(extra) == 0

    def test_remove_nonexistent_extra_service(self, order_service):
        services = order_service.db.list_services()
        client_id = order_service.db.add_client("Тест", phone="+7999")
        order_id = order_service.create_order(client_id, services[0]["id"])

        # Удаление несуществующей доп. услуги — DELETE в БД просто не найдёт запись, вернёт True
        result = order_service.remove_extra_service(order_id, 999)
        assert result is True

    def test_add_extra_service_nonexistent_order(self, order_service):
        services = order_service.db.list_services()
        result = order_service.add_extra_service(999, services[0]["id"])
        assert result is False

    def test_add_extra_service_nonexistent_service(self, order_service):
        services = order_service.db.list_services()
        client_id = order_service.db.add_client("Тест", phone="+7999")
        order_id = order_service.create_order(client_id, services[0]["id"])

        result = order_service.add_extra_service(order_id, 999)
        assert result is False


class TestOrderServiceGetOrderDetail:
    """Тесты получения деталей заказа."""

    def test_get_order_detail_structure(self, order_service):
        services = order_service.db.list_services()
        client_id = order_service.db.add_client("Тест", phone="+7999", telegram_id="tg1")
        order_id = order_service.create_order(client_id, services[0]["id"])

        detail = order_service.get_order_detail(order_id)

        assert detail is not None
        assert "order" in detail
        assert "history" in detail
        assert "photos" in detail
        assert "extra_services" in detail
        assert "extra_total" in detail
        assert "total" in detail

        assert detail["order"]["id"] == order_id
        assert isinstance(detail["history"], list)
        assert isinstance(detail["photos"], list)
        assert isinstance(detail["extra_services"], list)
        # extra_total can be int (0) or float
        assert isinstance(detail["extra_total"], (int, float))
        assert isinstance(detail["total"], (int, float))

    def test_get_order_detail_nonexistent(self, order_service):
        detail = order_service.get_order_detail(999)
        assert detail is None


class TestOrderServiceListOrders:
    """Тесты списка заказов."""

    def test_list_orders_with_photos(self, order_service):
        services = order_service.db.list_services()
        client_id = order_service.db.add_client("Тест", phone="+7999")

        # Заказ с фото
        oid1 = order_service.create_order(
            client_id, services[0]["id"], photo_data=b"photo1", photo_caption="Photo 1"
        )
        # Заказ без фото
        oid2 = order_service.create_order(client_id, services[0]["id"])

        orders = order_service.list_orders()
        assert len(orders) == 2

        # Проверка latest_photo
        order_with_photo = next(o for o in orders if o["id"] == oid1)
        assert order_with_photo["latest_photo"] is not None
        assert order_with_photo["latest_photo"]["id"] > 0

        order_without_photo = next(o for o in orders if o["id"] == oid2)
        assert order_without_photo["latest_photo"] is None

    def test_list_orders_filter_by_status(self, order_service):
        services = order_service.db.list_services()
        client_id = order_service.db.add_client("Тест", phone="+7999")

        oid1 = order_service.create_order(client_id, services[0]["id"])  # status 1
        order_service.create_order(client_id, services[0]["id"])  # status 1
        order_service.change_status(oid1, 2)  # status 2

        orders_status1 = order_service.list_orders(status_id=1)
        orders_status2 = order_service.list_orders(status_id=2)

        assert len(orders_status1) == 1
        assert len(orders_status2) == 1
        assert orders_status1[0]["status_id"] == 1
        assert orders_status2[0]["status_id"] == 2

    def test_list_orders_filter_by_client(self, order_service):
        services = order_service.db.list_services()
        client1 = order_service.db.add_client("Клиент 1", phone="+7999")
        client2 = order_service.db.add_client("Клиент 2", phone="+7998")

        order_service.create_order(client1, services[0]["id"])
        order_service.create_order(client2, services[0]["id"])

        orders_c1 = order_service.list_orders(client_id=client1)
        orders_c2 = order_service.list_orders(client_id=client2)

        assert len(orders_c1) == 1
        assert len(orders_c2) == 1
        assert orders_c1[0]["client_id"] == client1
        assert orders_c2[0]["client_id"] == client2


class TestOrderServiceEdgeCases:
    """Тесты граничных случаев."""

    def test_create_order_nonexistent_client(self, order_service):
        services = order_service.db.list_services()
        # FK constraint в БД упадёт с IntegrityError, но сервис не ловит — это ок для unit
        with pytest.raises(Exception):
            order_service.create_order(999, services[0]["id"])

    def test_create_order_nonexistent_service(self, order_service):
        client_id = order_service.db.add_client("Тест", phone="+7999")
        with pytest.raises(Exception):
            order_service.create_order(client_id, 999)

    def test_calculate_totals_zero_price(self, order_service):
        services = order_service.db.list_services()
        # Найдём услугу без цены или создадим
        free_service = None
        for s in services:
            if s["price"] is None or s["price"] == 0:
                free_service = s
                break
        if free_service is None:
            free_service_id = order_service.db.add_service("Бесплатно", "шт", 0)
            free_service = order_service.db.get_service(free_service_id)

        client_id = order_service.db.add_client("Тест", phone="+7999")
        order_id = order_service.create_order(client_id, free_service["id"], price=0)

        total = order_service.db.calculate_order_total(order_id)
        assert total == 0.0

    def test_extra_total_calculation(self, order_service):
        services = order_service.db.list_services()
        client_id = order_service.db.add_client("Тест", phone="+7999")
        order_id = order_service.create_order(client_id, services[0]["id"], price=1000)

        # Добавляем 2 доп. услуги
        order_service.add_extra_service(order_id, services[1]["id"], quantity=2, price=500)
        order_service.add_extra_service(order_id, services[2]["id"], quantity=1, price=300)

        detail = order_service.get_order_detail(order_id)
        assert detail["extra_total"] == 1300.0  # 2*500 + 1*300
        assert detail["total"] == 2300.0  # 1000 + 1300