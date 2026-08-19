import sqlite3
from typing import Any, Optional, Sequence


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ---------- Клиенты ----------

    def add_client(self, full_name: str, phone: str = "", telegram_id: str = "",
                   vk_id: str = "", max_id: str = "", notes: str = "") -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO clients (full_name, phone, telegram_id, vk_id, max_id, notes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (full_name, phone, telegram_id, vk_id, max_id, notes),
            )
            return int(cur.lastrowid)

    def list_clients(self) -> Sequence[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM clients ORDER BY full_name").fetchall()

    def get_client(self, client_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()

    def update_client(self, client_id: int, **fields: Any) -> None:
        allowed = {"full_name", "phone", "telegram_id", "vk_id", "max_id", "notes"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        with self.connect() as conn:
            cols = ", ".join(f"{k} = ?" for k in sets)
            conn.execute(f"UPDATE clients SET {cols} WHERE id = ?", (*sets.values(), client_id))

    def delete_client(self, client_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))

    def has_orders_for_client(self, client_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM orders WHERE client_id = ?", (client_id,)
            ).fetchone()
            return row["n"] > 0

    # ---------- Каналы уведомлений клиента ----------

    def set_channel(self, client_id: int, channel: str, enabled: bool = True) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO notification_channels (client_id, channel, enabled) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(client_id, channel) DO UPDATE SET enabled = excluded.enabled",
                (client_id, channel, int(enabled)),
            )

    def list_channels(self, client_id: int) -> Sequence[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM notification_channels WHERE client_id = ?", (client_id,)
            ).fetchall()

    # ---------- Услуги ----------

    def add_service(self, name: str, unit: str = "", price: Optional[float] = None) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO services (name, unit, price) VALUES (?, ?, ?)",
                (name, unit, price),
            )
            return int(cur.lastrowid)

    def list_services(self) -> Sequence[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM services ORDER BY name").fetchall()

    def get_service(self, service_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()

    def get_service_by_name(self, name: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM services WHERE name = ?", (name,)).fetchone()

    def update_service(self, service_id: int, **fields: Any) -> None:
        allowed = {"name", "unit", "price"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        with self.connect() as conn:
            cols = ", ".join(f"{k} = ?" for k in sets)
            conn.execute(f"UPDATE services SET {cols} WHERE id = ?", (*sets.values(), service_id))

    def delete_service(self, service_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM services WHERE id = ?", (service_id,))

    def has_orders_for_service(self, service_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM orders WHERE service_id = ?", (service_id,)
            ).fetchone()
            return row["n"] > 0

    # ---------- Статусы ----------

    def list_statuses(self) -> Sequence[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM statuses ORDER BY order_rank").fetchall()

    def get_status(self, status_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM statuses WHERE id = ?", (status_id,)).fetchone()

    # ---------- Заказы ----------

    def add_order(self, client_id: int, service_id: int, description: str = "",
                  model_file: str = "", price: Optional[float] = None,
                  deadline: str = "", status_id: int = 1) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO orders (client_id, service_id, status_id, description, "
                "model_file, price, deadline) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (client_id, service_id, status_id, description, model_file, price, deadline),
            )
            order_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO order_status_history (order_id, status_id) VALUES (?, ?)",
                (order_id, status_id),
            )
            return order_id

    def list_orders(self, status_id: Optional[int] = None,
                    client_id: Optional[int] = None) -> Sequence[sqlite3.Row]:
        query = (
            "SELECT o.*, c.full_name AS client_name, s.name AS service_name, "
            "st.name AS status_name "
            "FROM orders o "
            "JOIN clients c ON c.id = o.client_id "
            "JOIN services s ON s.id = o.service_id "
            "JOIN statuses st ON st.id = o.status_id "
        )
        where, params = [], []
        if status_id is not None:
            where.append("o.status_id = ?")
            params.append(status_id)
        if client_id is not None:
            where.append("o.client_id = ?")
            params.append(client_id)
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY o.created_at DESC"
        with self.connect() as conn:
            return conn.execute(query, params).fetchall()

    def get_order(self, order_id: int) -> Optional[sqlite3.Row]:
        query = (
            "SELECT o.*, c.full_name AS client_name, c.phone, c.telegram_id, "
            "c.vk_id, c.max_id, s.name AS service_name, st.name AS status_name "
            "FROM orders o "
            "JOIN clients c ON c.id = o.client_id "
            "JOIN services s ON s.id = o.service_id "
            "JOIN statuses st ON st.id = o.status_id "
            "WHERE o.id = ?"
        )
        with self.connect() as conn:
            return conn.execute(query, (order_id,)).fetchone()

    def set_order_status(self, order_id: int, status_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE orders SET status_id = ?, updated_at = datetime('now') WHERE id = ?",
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
                "WHERE h.order_id = ? ORDER BY h.changed_at",
                (order_id,),
            ).fetchall()

    def update_order(self, order_id: int, **fields: Any) -> None:
        allowed = {"client_id", "service_id", "description", "model_file", "price", "deadline"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        with self.connect() as conn:
            cols = ", ".join(f"{k} = ?" for k in sets)
            conn.execute(
                f"UPDATE orders SET {cols}, updated_at = datetime('now') WHERE id = ?",
                (*sets.values(), order_id),
            )

    def delete_order(self, order_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM order_status_history WHERE order_id = ?", (order_id,))
            conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))

    # ---------- Фото заказов ----------

    def add_order_photo(self, order_id: int, status_id: int, photo_data: bytes,
                        mime_type: str = "image/jpeg", caption: str = "") -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO order_photos (order_id, status_id, photo_data, mime_type, caption) "
                "VALUES (?, ?, ?, ?, ?)",
                (order_id, status_id, photo_data, mime_type, caption),
            )
            return int(cur.lastrowid)

    def get_order_photos(self, order_id: int) -> Sequence[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT p.*, s.name AS status_name FROM order_photos p "
                "JOIN statuses s ON s.id = p.status_id "
                "WHERE p.order_id = ? ORDER BY p.created_at",
                (order_id,)
            ).fetchall()

    def get_latest_photo(self, order_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT p.*, s.name AS status_name FROM order_photos p "
                "JOIN statuses s ON s.id = p.status_id "
                "WHERE p.order_id = ? ORDER BY p.created_at DESC LIMIT 1",
                (order_id,)
            ).fetchone()

    # ---------- Дополнительные услуги заказа ----------

    def add_service_to_order(self, order_id: int, service_id: int,
                             quantity: float = 1, price: Optional[float] = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO order_services (order_id, service_id, quantity, price) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(order_id, service_id) DO UPDATE SET "
                "quantity = excluded.quantity, price = excluded.price",
                (order_id, service_id, quantity, price),
            )

    def get_order_services(self, order_id: int) -> Sequence[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT os.*, s.name, s.unit, s.price AS default_price "
                "FROM order_services os "
                "JOIN services s ON s.id = os.service_id "
                "WHERE os.order_id = ?",
                (order_id,)
            ).fetchall()

    def remove_service_from_order(self, order_id: int, service_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM order_services WHERE order_id = ? AND service_id = ?",
                (order_id, service_id),
            )

    def calculate_order_total(self, order_id: int) -> float:
        """Итоговая сумма: основная услуга + доп. услуги."""
        with self.connect() as conn:
            # Основная услуга
            row = conn.execute(
                "SELECT price FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            total = row["price"] if row and row["price"] else 0.0

            # Доп. услуги
            rows = conn.execute(
                "SELECT COALESCE(os.price, s.price) * quantity AS subtotal "
                "FROM order_services os "
                "JOIN services s ON s.id = os.service_id "
                "WHERE os.order_id = ?",
                (order_id,)
            ).fetchall()
            total += sum(r["subtotal"] for r in rows if r["subtotal"])
            return total

    def get_order_photo(self, photo_id: int) -> Optional[sqlite3.Row]:
        """Получает фото по ID для отдачи клиенту."""
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM order_photos WHERE id = ?", (photo_id,)
            ).fetchone()

    def list_orders_with_photos(self, status_id: Optional[int] = None,
                                 client_id: Optional[int] = None) -> Sequence[sqlite3.Row]:
        """Список заказов с последним фото для превью."""
        query = (
            "SELECT o.*, c.full_name AS client_name, s.name AS service_name, "
            "st.name AS status_name, "
            "(SELECT p.id FROM order_photos p WHERE p.order_id = o.id ORDER BY p.created_at DESC LIMIT 1) AS latest_photo_id "
            "FROM orders o "
            "JOIN clients c ON c.id = o.client_id "
            "JOIN services s ON s.id = o.service_id "
            "JOIN statuses st ON st.id = o.status_id "
        )
        where, params = [], []
        if status_id is not None:
            where.append("o.status_id = ?")
            params.append(status_id)
        if client_id is not None:
            where.append("o.client_id = ?")
            params.append(client_id)
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY o.created_at DESC"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
            # Convert to list to add latest_photo attribute
            result = []
            for row in rows:
                row_dict = dict(row)
                if row_dict["latest_photo_id"]:
                    photo = self.get_order_photo(row_dict["latest_photo_id"])
                    row_dict["latest_photo"] = photo
                else:
                    row_dict["latest_photo"] = None
                result.append(row_dict)
            return result

    def calculate_extra_total(self, order_id: int) -> float:
        """Сумма только доп. услуг."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT COALESCE(os.price, s.price) * quantity AS subtotal "
                "FROM order_services os "
                "JOIN services s ON s.id = os.service_id "
                "WHERE os.order_id = ?",
                (order_id,)
            ).fetchall()
            return sum(r["subtotal"] for r in rows if r["subtotal"])
