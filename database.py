"""Database class: все SQL-запросы приложения.

Единственное место, где живут SQL-запросы. Используется
веб-маршрутами (app.py), бизнес-логикой (services/order_service.py)
и ботами (max_bot.py, tg_bot.py). Каждый метод — одна операция,
каждое соединение открывается в контекстном менеджере (автокоммит).

FK-ограничения включаются через PRAGMA foreign_keys = ON
для каждого соединения.
"""

import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from db_schema import init_db, seed_defaults

#: Допустимые поля для безопасного динамического UPDATE (client_id и др. —
#: имя ключа совпадает с именем колонки, поэтому whitelist достаточно).
_CLIENT_FIELDS = frozenset({"full_name", "phone", "telegram_id", "vk_id", "max_id", "notes"})
_SERVICE_FIELDS = frozenset({"name", "unit", "price"})
_ORDER_FIELDS = frozenset({"client_id", "service_id", "status_id", "description",
                           "model_file", "price", "deadline"})


def _normalize_phone(phone: Any) -> str | None:
    """Нормализация телефона: оставляет только цифры и ведущий '+'.

    Используется маршрутами для валидации ПЕРЕД записью в БД.
    Возвращает None для пустого/нечислового значения.
    """
    if phone is None:
        return None
    text = str(phone).strip()
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    return "+" + digits


class Database:
    """Слой доступа к данным SQLite (схема 3НФ, см. db_schema.py)."""

    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self.connect() as conn:
            init_db(conn)
            seed_defaults(conn)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Открывает соединение с автокоммитом и FK-ограничениями."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Клиенты
    # ------------------------------------------------------------------
    def add_client(self, full_name: str, phone: str | None = None,
                   telegram_id: str | None = None, vk_id: str | None = None,
                   max_id: str | None = None, notes: str | None = None) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO clients (full_name, phone, telegram_id, vk_id, max_id, notes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (full_name, phone, telegram_id, vk_id, max_id, notes),
            )
            return cur.lastrowid

    def list_clients(self) -> Sequence[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM clients ORDER BY full_name COLLATE NOCASE, id"
            ).fetchall()

    def get_client(self, client_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM clients WHERE id = ?", (client_id,)
            ).fetchone()

    def get_client_by_phone(self, phone: str) -> Sequence[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM clients WHERE phone = ? ORDER BY id", (phone,)
            ).fetchall()

    def update_client(self, client_id: int, **fields: Any) -> None:
        fields = {k: v for k, v in fields.items() if k in _CLIENT_FIELDS}
        if not fields:
            return
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE clients SET {assignments} WHERE id = ?",
                (*fields.values(), client_id),
            )

    def delete_client(self, client_id: int) -> None:
        """Удаляет клиента и его каналы уведомлений.

        Маршрут должен предварительно проверять has_orders_for_client() —
        заказы клиента удалять нельзя (FK без CASCADE).
        """
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM notification_channels WHERE client_id = ?", (client_id,)
            )
            conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))

    def has_orders_for_client(self, client_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM orders WHERE client_id = ? LIMIT 1", (client_id,)
            ).fetchone()
            return row is not None

    # ------------------------------------------------------------------
    # Каналы уведомлений
    # ------------------------------------------------------------------
    def set_channel(self, client_id: int, channel: str, enabled: bool) -> None:
        """UPSERT: включает/выключает канал уведомлений для клиента."""
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO notification_channels (client_id, channel, enabled) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(client_id, channel) DO UPDATE SET enabled = excluded.enabled",
                (client_id, channel, 1 if enabled else 0),
            )

    def list_channels(self, client_id: int) -> Sequence[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM notification_channels WHERE client_id = ? ORDER BY channel",
                (client_id,),
            ).fetchall()

    def get_client_channels(self, client_id: int) -> dict[str, bool]:
        """Возвращает {channel: enabled} для клиента (удобно для уведомлений)."""
        return {row["channel"]: bool(row["enabled"]) for row in self.list_channels(client_id)}

    # ------------------------------------------------------------------
    # Услуги
    # ------------------------------------------------------------------
    def add_service(self, name: str, unit: str | None = None,
                    price: float | None = None) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO services (name, unit, price) VALUES (?, ?, ?)",
                (name, unit, price),
            )
            return cur.lastrowid

    def list_services(self) -> Sequence[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM services ORDER BY name COLLATE NOCASE, id"
            ).fetchall()

    def get_service(self, service_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM services WHERE id = ?", (service_id,)
            ).fetchone()

    def get_service_by_name(self, name: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM services WHERE name = ?", (name,)
            ).fetchone()

    def update_service(self, service_id: int, **fields: Any) -> None:
        fields = {k: v for k, v in fields.items() if k in _SERVICE_FIELDS}
        if not fields:
            return
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE services SET {assignments} WHERE id = ?",
                (*fields.values(), service_id),
            )

    def delete_service(self, service_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM services WHERE id = ?", (service_id,))

    def has_orders_for_service(self, service_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM orders WHERE service_id = ? LIMIT 1", (service_id,)
            ).fetchone()
            return row is not None

    # ------------------------------------------------------------------
    # Статусы
    # ------------------------------------------------------------------
    def list_statuses(self) -> Sequence[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM statuses ORDER BY order_rank, id"
            ).fetchall()

    def get_status(self, status_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM statuses WHERE id = ?", (status_id,)
            ).fetchone()

    # ------------------------------------------------------------------
    # Заказы
    # ------------------------------------------------------------------
    def add_order(self, client_id: int, service_id: int,
                  description: str | None = None, model_file: str | None = None,
                  price: float | None = None, deadline: str | None = None,
                  status_id: int = 1) -> int:
        """Создаёт заказ и фиксирует начальный статус в истории."""
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO orders (client_id, service_id, status_id, description, "
                "model_file, price, deadline) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (client_id, service_id, status_id, description, model_file, price, deadline),
            )
            order_id = cur.lastrowid
            conn.execute(
                "INSERT INTO order_status_history (order_id, status_id) VALUES (?, ?)",
                (order_id, status_id),
            )
            return order_id

    def list_orders(self, status_id: int | None = None,
                    client_id: int | None = None) -> Sequence[sqlite3.Row]:
        """Список заказов с JOIN-данными и фильтрами."""
        sql = (
            "SELECT o.*, c.full_name AS client_name, s.name AS service_name, "
            "st.name AS status_name "
            "FROM orders o "
            "JOIN clients c ON c.id = o.client_id "
            "JOIN services s ON s.id = o.service_id "
            "JOIN statuses st ON st.id = o.status_id "
        )
        conditions: list[str] = []
        params: list[Any] = []
        if status_id is not None:
            conditions.append("o.status_id = ?")
            params.append(status_id)
        if client_id is not None:
            conditions.append("o.client_id = ?")
            params.append(client_id)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY o.id DESC"
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def list_orders_with_photos(self, status_id: int | None = None,
                                client_id: int | None = None) -> Sequence[sqlite3.Row]:
        """Список заказов + id последнего фото (для превью в списке)."""
        sql = (
            "SELECT o.*, c.full_name AS client_name, s.name AS service_name, "
            "st.name AS status_name, "
            "(SELECT p.id FROM order_photos p WHERE p.order_id = o.id "
            " ORDER BY p.created_at DESC, p.id DESC LIMIT 1) AS latest_photo_id "
            "FROM orders o "
            "JOIN clients c ON c.id = o.client_id "
            "JOIN services s ON s.id = o.service_id "
            "JOIN statuses st ON st.id = o.status_id "
        )
        conditions: list[str] = []
        params: list[Any] = []
        if status_id is not None:
            conditions.append("o.status_id = ?")
            params.append(status_id)
        if client_id is not None:
            conditions.append("o.client_id = ?")
            params.append(client_id)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY o.id DESC"
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def get_order(self, order_id: int) -> sqlite3.Row | None:
        """Заказ с JOIN clients/services/statuses."""
        with self.connect() as conn:
            return conn.execute(
                "SELECT o.*, c.full_name AS client_name, c.phone AS client_phone, "
                "s.name AS service_name, s.unit AS service_unit, "
                "st.name AS status_name, st.order_rank AS status_rank "
                "FROM orders o "
                "JOIN clients c ON c.id = o.client_id "
                "JOIN services s ON s.id = o.service_id "
                "JOIN statuses st ON st.id = o.status_id "
                "WHERE o.id = ?",
                (order_id,),
            ).fetchone()

    def set_order_status(self, order_id: int, status_id: int) -> None:
        """Меняет статус заказа и пишет запись в историю (атомарно)."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE orders SET status_id = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (status_id, order_id),
            )
            conn.execute(
                "INSERT INTO order_status_history (order_id, status_id) VALUES (?, ?)",
                (order_id, status_id),
            )

    def order_history(self, order_id: int) -> Sequence[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT h.*, s.name AS status_name "
                "FROM order_status_history h "
                "JOIN statuses s ON s.id = h.status_id "
                "WHERE h.order_id = ? "
                "ORDER BY h.id ASC",
                (order_id,),
            ).fetchall()

    def update_order(self, order_id: int, **fields: Any) -> None:
        fields = {k: v for k, v in fields.items() if k in _ORDER_FIELDS}
        if not fields:
            return
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE orders SET {assignments}, updated_at = datetime('now') "
                "WHERE id = ?",
                (*fields.values(), order_id),
            )

    def delete_order(self, order_id: int) -> None:
        """Удаляет заказ вместе с историей статусов.

        order_photos и order_services удаляются каскадом (ON DELETE CASCADE),
        а order_status_history — явно, т.к. в схеме у неё нет CASCADE.
        """
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM order_status_history WHERE order_id = ?", (order_id,)
            )
            conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))

    # ------------------------------------------------------------------
    # Фото заказов
    # ------------------------------------------------------------------
    def add_order_photo(self, order_id: int, status_id: int, photo_data: bytes,
                        mime_type: str = "image/jpeg", caption: str | None = None) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO order_photos (order_id, status_id, photo_data, mime_type, caption) "
                "VALUES (?, ?, ?, ?, ?)",
                (order_id, status_id, photo_data, mime_type, caption),
            )
            return cur.lastrowid

    def get_order_photos(self, order_id: int) -> Sequence[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM order_photos WHERE order_id = ? ORDER BY id ASC",
                (order_id,),
            ).fetchall()

    def get_order_photo(self, photo_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM order_photos WHERE id = ?", (photo_id,)
            ).fetchone()

    # ------------------------------------------------------------------
    # Доп. услуги к заказу (M:N)
    # ------------------------------------------------------------------
    def add_service_to_order(self, order_id: int, service_id: int,
                             quantity: float = 1, price: float | None = None) -> None:
        """UPSERT: добавляет/обновляет доп. услугу в заказе."""
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO order_services (order_id, service_id, quantity, price) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(order_id, service_id) DO UPDATE SET "
                "quantity = excluded.quantity, price = excluded.price",
                (order_id, service_id, quantity, price),
            )

    def get_order_services(self, order_id: int) -> Sequence[sqlite3.Row]:
        """Доп. услуги заказа с ценой (переопределённой или из каталога)."""
        with self.connect() as conn:
            return conn.execute(
                "SELECT os.order_id, os.service_id, os.quantity, os.price AS override_price, "
                "s.name AS service_name, s.unit AS service_unit, s.price AS catalog_price, "
                "COALESCE(os.price, s.price) AS effective_price "
                "FROM order_services os "
                "JOIN services s ON s.id = os.service_id "
                "WHERE os.order_id = ? "
                "ORDER BY s.name COLLATE NOCASE",
                (order_id,),
            ).fetchall()

    def remove_service_from_order(self, order_id: int, service_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM order_services WHERE order_id = ? AND service_id = ?",
                (order_id, service_id),
            )

    def calculate_extra_total(self, order_id: int) -> float:
        """Сумма по доп. услугам заказа (цена × количество)."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(COALESCE(os.price, s.price) * os.quantity), 0.0) AS total "
                "FROM order_services os "
                "JOIN services s ON s.id = os.service_id "
                "WHERE os.order_id = ?",
                (order_id,),
            ).fetchone()
            return float(row["total"])

    def calculate_order_total(self, order_id: int) -> float:
        """Итоговая стоимость заказа: цена заказа + доп. услуги."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(o.price, s.price, 0.0) AS base_price "
                "FROM orders o "
                "JOIN services s ON s.id = o.service_id "
                "WHERE o.id = ?",
                (order_id,),
            ).fetchone()
            base = float(row["base_price"]) if row else 0.0
            extra = self.calculate_extra_total(order_id)
            return base + extra
