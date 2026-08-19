import sqlite3


# Схема базы данных, нормализованная до 3НФ.
#
# Обоснование 3НФ:
# - каждый неключевой атрибут зависит только от полного первичного ключа
#   своей таблицы (2НФ) и не зависит транзитивно через другой неключевой
#   атрибут (3НФ);
# - справочники услуг, статусов и каналов уведомлений вынесены в отдельные
#   таблицы — нет повторяющихся групп и дублирования текстовых значений;
# - история смены статусов хранится отдельно (заказ -> статус -> дата),
#   поэтому таблица orders не содержит повторяющихся неключевых атрибутов,
#   а у клиента нет списка каналов внутри одной строки.

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS clients (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name     TEXT    NOT NULL,
    phone         TEXT,
    telegram_id   TEXT,
    vk_id         TEXT,
    max_id        TEXT,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS services (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL UNIQUE,
    unit     TEXT,
    price    REAL
);

CREATE TABLE IF NOT EXISTS statuses (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL UNIQUE,
    order_rank INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   INTEGER NOT NULL REFERENCES clients(id),
    service_id  INTEGER NOT NULL REFERENCES services(id),
    status_id   INTEGER NOT NULL REFERENCES statuses(id),
    description TEXT,
    model_file  TEXT,
    price       REAL,
    deadline    TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS order_status_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   INTEGER NOT NULL REFERENCES orders(id),
    status_id  INTEGER NOT NULL REFERENCES statuses(id),
    changed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notification_channels (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    channel   TEXT NOT NULL CHECK (channel IN ('telegram', 'vk', 'max')),
    enabled   INTEGER NOT NULL DEFAULT 1,
    UNIQUE (client_id, channel)
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status_id);
CREATE INDEX IF NOT EXISTS idx_orders_client ON orders(client_id);
CREATE INDEX IF NOT EXISTS idx_history_order ON order_status_history(order_id);

-- Фото заказов, привязанные к статусу (история: фото поломки -> 3D-скан -> чертеж -> готовое)
CREATE TABLE IF NOT EXISTS order_photos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    status_id    INTEGER NOT NULL REFERENCES statuses(id),
    photo_data   BLOB    NOT NULL,
    mime_type    TEXT    NOT NULL DEFAULT 'image/jpeg',
    caption      TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_photos_order ON order_photos(order_id);

-- Дополнительные услуги к заказу (many-to-many)
CREATE TABLE IF NOT EXISTS order_services (
    order_id   INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    service_id INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    quantity   REAL    NOT NULL DEFAULT 1,
    price      REAL,  -- переопределение цены для этого заказа (опционально)
    PRIMARY KEY (order_id, service_id)
);
"""

def init_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def seed_defaults(db_path: str) -> None:
    """Заполняет справочники начальными значениями."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        INSERT OR IGNORE INTO statuses (id, name, order_rank) VALUES
            (1, 'принят', 1),
            (2, 'в работе', 2),
            (3, 'готов', 3),
            (4, 'выдан', 4),
            (5, 'отменён', 5);

        INSERT OR IGNORE INTO services (id, name, unit, price) VALUES
            (1, '3D-сканирование', 'шт', 1500),
            (2, '3D-печать', 'г', 4),
            (3, 'Постобработка', 'шт', 500);
        """
    )
    conn.commit()
    conn.close()


def migrate_db(db_path: str) -> None:
    """Создаёт новые таблицы, если их нет (для обновления существующей БД)."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS order_photos (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id     INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            status_id    INTEGER NOT NULL REFERENCES statuses(id),
            photo_data   BLOB    NOT NULL,
            mime_type    TEXT    NOT NULL DEFAULT 'image/jpeg',
            caption      TEXT,
            created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_photos_order ON order_photos(order_id);

        CREATE TABLE IF NOT EXISTS order_services (
            order_id   INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            service_id INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
            quantity   REAL    NOT NULL DEFAULT 1,
            price      REAL,
            PRIMARY KEY (order_id, service_id)
        );
        """
    )
    conn.commit()
    conn.close()
