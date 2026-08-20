"""Схема базы данных SQLite и инициализация.

DDL соответствует СПЕЦИФИКАЦИИ (SPEC.md, раздел 2):
таблицы clients, services, statuses, orders, order_status_history,
notification_channels, order_photos, order_services, а также индексы
idx_orders_status, idx_orders_client, idx_history_order, idx_photos_order.

Все операторы идемпотентны (IF NOT EXISTS / INSERT OR IGNORE),
поэтому init_db() и seed_defaults() можно безопасно вызывать
при каждом старте приложения.
"""

import sqlite3

#: Начальные статусы заказов. id фиксирован — status_id=1 ("принят")
#: используется по умолчанию при создании заказа.
DEFAULT_STATUSES = [
    ("принят", 1),
    ("в работе", 2),
    ("готов", 3),
    ("выдан", 4),
    ("отменён", 5),
]

SCHEMA_SQL = """
-- Клиенты студии
CREATE TABLE IF NOT EXISTS clients (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name     TEXT    NOT NULL,
    phone         TEXT,              -- "+79001234567"
    telegram_id   TEXT,              -- numeric user_id в Telegram
    vk_id         TEXT,              -- numeric user_id в VK
    max_id        TEXT,              -- numeric user_id в MAX
    notes         TEXT               -- произвольные заметки
);

-- Каталог услуг
CREATE TABLE IF NOT EXISTS services (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL UNIQUE,   -- "3D-сканирование", "3D-печать", "Постобработка"
    unit     TEXT,                   -- "шт", "г", "см"
    price    REAL                    -- цена за единицу
);

-- Справочник статусов заказа
CREATE TABLE IF NOT EXISTS statuses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    order_rank INTEGER NOT NULL DEFAULT 0  -- для сортировки
);

-- Заказы
CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   INTEGER NOT NULL REFERENCES clients(id),
    service_id  INTEGER NOT NULL REFERENCES services(id),
    status_id   INTEGER NOT NULL REFERENCES statuses(id),
    description TEXT,
    model_file  TEXT,       -- путь к файлу модели или ссылка
    price       REAL,       -- итоговая цена (может не совпадать с service.price)
    deadline    TEXT,       -- строка "2024-03-15" или ""
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Аудит-журнал смены статусов
CREATE TABLE IF NOT EXISTS order_status_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   INTEGER NOT NULL REFERENCES orders(id),
    status_id  INTEGER NOT NULL REFERENCES statuses(id),
    changed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Какие каналы уведомлений включены у клиента
CREATE TABLE IF NOT EXISTS notification_channels (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    channel   TEXT NOT NULL CHECK (channel IN ('telegram', 'vk', 'max')),
    enabled   INTEGER NOT NULL DEFAULT 1,
    UNIQUE (client_id, channel)
);

-- Фото, привязанные к заказу и статусу
CREATE TABLE IF NOT EXISTS order_photos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    status_id    INTEGER NOT NULL REFERENCES statuses(id),
    photo_data   BLOB    NOT NULL,
    mime_type    TEXT    NOT NULL DEFAULT 'image/jpeg',
    caption      TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Доп. услуги к заказу (M:N)
CREATE TABLE IF NOT EXISTS order_services (
    order_id   INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    service_id INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    quantity   REAL    NOT NULL DEFAULT 1,
    price      REAL,  -- переопределение цены (NULL = цена из services)
    PRIMARY KEY (order_id, service_id)
);

-- Индексы для частых фильтраций
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status_id);
CREATE INDEX IF NOT EXISTS idx_orders_client ON orders(client_id);
CREATE INDEX IF NOT EXISTS idx_history_order ON order_status_history(order_id);
CREATE INDEX IF NOT EXISTS idx_photos_order  ON order_photos(order_id);
"""


def init_db(conn: sqlite3.Connection) -> None:
    """Создаёт таблицы и индексы (идемпотентно)."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def seed_defaults(conn: sqlite3.Connection) -> None:
    """Заполняет справочник статусов значениями по умолчанию.

    INSERT OR IGNORE с фиксированными id (1..5), чтобы status_id=1
    гарантированно соответствовал статусу «принят».
    """
    for name, rank in DEFAULT_STATUSES:
        conn.execute(
            "INSERT OR IGNORE INTO statuses (id, name, order_rank) VALUES (?, ?, ?)",
            (rank, name, rank),
        )
    conn.commit()


def migrate_db(conn: sqlite3.Connection) -> None:
    """Миграции схемы.

    Текущая схема идемпотентна и пересоздаётся операторами
    CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS,
    поэтому миграция сводится к init_db() + seed_defaults().
    Новые миграции добавляются здесь по мере развития схемы.
    """
    init_db(conn)
    seed_defaults(conn)
