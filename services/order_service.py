"""Сервис заказов — бизнес-логика, общая для веб, MAX-бота и TG-бота.

Выносит дублирующуюся логику уведомлений, создания заказов, смены статусов
в один место. Зависит от абстракций (Database, Notifier protocol).
"""

from dataclasses import dataclass
from typing import Optional, Sequence

from database import Database
from notifier import build_notifiers, send_notifications, order_status_message


@dataclass
class NotificationPayload:
    """Данные для уведомления с фото."""
    photo_data: Optional[bytes] = None
    photo_caption: str = ""
    photo_mime: str = "image/jpeg"


class OrderService:
    """Единый сервис для операций с заказами."""

    def __init__(self, db: Database, notifiers: dict | None = None):
        self.db = db
        self.notifiers = notifiers or build_notifiers()

    # ---------- Уведомления ----------

    def notify_status_change(self, order_id: int, status_name: str,
                              payload: NotificationPayload = None) -> dict[str, bool]:
        """Отправляет уведомление клиенту об изменении статуса заказа."""
        order = self.db.get_order(order_id)
        if not order:
            return {}
        client = self.db.get_client(order["client_id"])
        channel_map = {"telegram": "telegram_id", "vk": "vk_id", "max": "max_id"}
        enabled = {c["channel"] for c in self.db.list_channels(order["client_id"]) if c["enabled"]}
        channels = {ch: client[channel_map[ch]] for ch in enabled if client[channel_map[ch]]}

        payload = payload or NotificationPayload()
        return send_notifications(
            channels,
            order_status_message(order, status_name),
            self.notifiers,
            photo_data=payload.photo_data,
            photo_caption=payload.photo_caption,
            photo_mime=payload.photo_mime
        )

    # ---------- Создание заказа ----------

    def create_order(self, client_id: int, service_id: int, description: str = "",
                     model_file: str = "", price: Optional[float] = None,
                     deadline: str = "", status_id: int = 1,
                     photo_data: Optional[bytes] = None,
                     photo_caption: str = "", photo_mime: str = "image/jpeg") -> int:
        """Создаёт заказ с опциональным фото и уведомляет клиента."""
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
            self.db.add_order_photo(
                order_id=order_id,
                status_id=status_id,
                photo_data=photo_data,
                mime_type=photo_mime,
                caption=photo_caption
            )

        # Уведомляем клиента
        self.notify_status_change(order_id, "принят", NotificationPayload(
            photo_data=photo_data,
            photo_caption="Новый заказ создан",
            photo_mime=photo_mime
        ))

        return order_id

    # ---------- Смена статуса ----------

    def change_status(self, order_id: int, new_status_id: int,
                      photo_data: Optional[bytes] = None,
                      photo_caption: str = "", photo_mime: str = "image/jpeg") -> bool:
        """Меняет статус заказа, сохраняет фото, уведомляет клиента."""
        order = self.db.get_order(order_id)
        if not order:
            return False

        status = self.db.get_status(new_status_id)
        if not status:
            return False

        if status["id"] == order["status_id"]:
            return False  # Статус не изменился

        self.db.set_order_status(order_id, new_status_id)

        if photo_data:
            self.db.add_order_photo(
                order_id=order_id,
                status_id=new_status_id,
                photo_data=photo_data,
                mime_type=photo_mime,
                caption=photo_caption
            )

        self.notify_status_change(order_id, status["name"], NotificationPayload(
            photo_data=photo_data,
            photo_caption=f"Статус: {status['name']}\n{photo_caption}" if photo_caption else f"Статус: {status['name']}",
            photo_mime=photo_mime
        ))

        return True

    # ---------- Дополнительные услуги ----------

    def add_extra_service(self, order_id: int, service_id: int,
                          quantity: float = 1, price: Optional[float] = None) -> bool:
        """Добавляет доп. услугу к заказу."""
        order = self.db.get_order(order_id)
        if not order:
            return False
        service = self.db.get_service(service_id)
        if not service:
            return False
        self.db.add_service_to_order(order_id, service_id, quantity, price)
        return True

    def remove_extra_service(self, order_id: int, service_id: int) -> bool:
        """Удаляет доп. услугу из заказа."""
        order = self.db.get_order(order_id)
        if not order:
            return False
        self.db.remove_service_from_order(order_id, service_id)
        return True

    # ---------- Чтение ----------

    def get_order_detail(self, order_id: int) -> dict | None:
        """Полные данные заказа для отображения."""
        order = self.db.get_order(order_id)
        if not order:
            return None
        return {
            "order": order,
            "history": self.db.order_history(order_id),
            "photos": self.db.get_order_photos(order_id),
            "extra_services": self.db.get_order_services(order_id),
            "extra_total": self.db.calculate_extra_total(order_id),
            "total": self.db.calculate_order_total(order_id),
        }

    def list_orders(self, status_id: Optional[int] = None,
                    client_id: Optional[int] = None) -> Sequence:
        """Список заказов с превью фото."""
        return self.db.list_orders_with_photos(status_id=status_id, client_id=client_id)