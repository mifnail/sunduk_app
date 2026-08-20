"""OrderService: единая точка бизнес-логики заказов.

Используется и веб-маршрутами (app.py), и ботами (max_bot.py, tg_bot.py),
чтобы логика создания заказа и смены статуса не дублировалась.
"""

from collections.abc import Sequence

from database import Database
from notifier import Notifier, build_notifiers, order_status_message, send_notifications


class OrderService:
    """Бизнес-логика заказов поверх Database."""

    def __init__(self, db: Database, notifiers: dict[str, Notifier] | None = None) -> None:
        self.db = db
        self._notifiers = notifiers

    def _get_notifiers(self) -> dict[str, Notifier]:
        """Нотификаторы: инжектированные (для тестов) или из окружения."""
        if self._notifiers is not None:
            return self._notifiers
        return build_notifiers()

    def create_order(self, client_id: int, service_id: int, description: str = "",
                     model_file: str = "", price: float | None = None,
                     deadline: str = "", status_id: int = 1,
                     photo_data: bytes | None = None,
                     photo_caption: str = "", photo_mime: str = "image/jpeg") -> int:
        """Создаёт заказ, сохраняет фото (если есть) и уведомляет клиента."""
        order_id = self.db.add_order(
            client_id=client_id,
            service_id=service_id,
            description=description,
            model_file=model_file,
            price=price,
            deadline=deadline,
            status_id=status_id,
        )
        if photo_data:
            self.db.add_order_photo(order_id, status_id, photo_data, photo_mime, photo_caption)
        self.notify_status_change(
            order_id,
            photo_data=photo_data,
            photo_caption=photo_caption,
            photo_mime=photo_mime,
        )
        return order_id

    def change_status(self, order_id: int, new_status_id: int,
                      photo_data: bytes | None = None,
                      photo_caption: str = "", photo_mime: str = "image/jpeg") -> bool:
        """Меняет статус, пишет историю, сохраняет фото и уведомляет клиента.

        Возвращает False, если заказ/статус не существуют или статус не изменился.
        """
        order = self.db.get_order(order_id)
        if order is None:
            return False
        status = self.db.get_status(new_status_id)
        if status is None:
            return False
        if order["status_id"] == new_status_id:
            return False  # статус не изменился — ничего не делаем
        self.db.set_order_status(order_id, new_status_id)
        if photo_data:
            self.db.add_order_photo(order_id, new_status_id, photo_data, photo_mime, photo_caption)
        self.notify_status_change(
            order_id,
            status_name=status["name"],
            photo_data=photo_data,
            photo_caption=photo_caption,
            photo_mime=photo_mime,
        )
        return True

    def notify_status_change(self, order_id: int, status_name: str | None = None,
                             photo_data: bytes | None = None,
                             photo_caption: str = "", photo_mime: str = "image/jpeg") -> dict[str, bool]:
        """Отправляет уведомление клиенту во все включённые каналы.

        Возвращает {channel: успех}. Пустой dict — если заказ/клиент
        не найдены или у клиента нет включённых каналов с ID.
        """
        order = self.db.get_order(order_id)
        if order is None:
            return {}
        if status_name is None:
            status = self.db.get_status(order["status_id"])
            status_name = status["name"] if status else ""
        client = self.db.get_client(order["client_id"])
        if client is None:
            return {}
        channels = self._enabled_channels(client)
        if not channels:
            return {}
        message = order_status_message(order, status_name)
        return send_notifications(
            channels,
            message,
            self._get_notifiers(),
            photo_data=photo_data,
            photo_caption=photo_caption,
            photo_mime=photo_mime,
        )

    def _enabled_channels(self, client) -> dict[str, str]:
        """Включённые каналы клиента с непустыми ID: {channel: recipient_id}.

        Отключённый канал или канал без ID клиента не попадает в результат —
        уведомление по нему не отправляется.
        """
        channel_ids = {
            "telegram": client["telegram_id"],
            "vk": client["vk_id"],
            "max": client["max_id"],
        }
        channels: dict[str, str] = {}
        for channel, enabled in self.db.get_client_channels(client["id"]).items():
            if enabled and channel_ids.get(channel):
                channels[channel] = channel_ids[channel]
        return channels

    def add_extra_service(self, order_id: int, service_id: int,
                          quantity: float = 1, price: float | None = None) -> bool:
        """Добавляет доп. услугу к заказу. False — если заказ/услуга не существуют."""
        if self.db.get_order(order_id) is None or self.db.get_service(service_id) is None:
            return False
        self.db.add_service_to_order(order_id, service_id, quantity, price)
        return True

    def remove_extra_service(self, order_id: int, service_id: int) -> bool:
        """Удаляет доп. услугу из заказа. False — если заказ не существует."""
        if self.db.get_order(order_id) is None:
            return False
        self.db.remove_service_from_order(order_id, service_id)
        return True

    def get_order_detail(self, order_id: int) -> dict | None:
        """Полные детали заказа: история, фото, доп. услуги, суммы."""
        order = self.db.get_order(order_id)
        if order is None:
            return None
        detail = dict(order)
        detail["history"] = self.db.order_history(order_id)
        detail["photos"] = self.db.get_order_photos(order_id)
        detail["extra_services"] = self.db.get_order_services(order_id)
        detail["extra_total"] = self.db.calculate_extra_total(order_id)
        detail["total"] = self.db.calculate_order_total(order_id)
        return detail

    def list_orders(self, status_id: int | None = None,
                    client_id: int | None = None) -> Sequence:
        """Список заказов с фильтрами и превью последнего фото."""
        return self.db.list_orders_with_photos(status_id=status_id, client_id=client_id)