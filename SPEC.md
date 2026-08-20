# СПЕЦИФИКАЦИЯ ПРОЕКТА: Студия 3D — Система управления заказами

## 1. КОНТЕКСТ ПРОЕКТА

### Кто является пользователем
Студия 3D-печати и 3D-сканирования. Основные роли:
- **Оператор** — принимает заказы, меняет статусы, уведомляет клиентов
- **Клиент** — получает уведомления о статусе заказа

### Какие задачи решает
1. Приём заказов от клиентов (через веб-интерфейс или бота MAX)
2. Отслеживание жизненного цикла заказа (принят → в работе → готов → выдан)
3. Автоматические уведомления клиентов о смене статуса
4. Фото-документирование (фото поломки → 3D-скан → чертёж → готовое изделие)
5. Учёт доп. услуг к каждому заказу

### Стек технологий
- **Backend**: Python 3.11+, Flask, SQLite
- **Бот MAX**: Long Polling ( платформа MAX,替代 Telegram в РФ)
- **Бот Telegram**: python-telegram-bot (для оператора)
- **Среда**: Orange Pi, ARM64, systemd
- **Нет**: DORM, ORM, фронтенд-фреймворков — всё максимально просто

---

## 2. МОДЕЛЬ ДАННЫХ (SQLite, 3НФ)

### Таблица clients
```sql
CREATE TABLE clients (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name     TEXT    NOT NULL,
    phone         TEXT,              -- "+79001234567"
    telegram_id   TEXT,              -- numeric user_id в Telegram
    vk_id         TEXT,              -- numeric user_id в VK
    max_id        TEXT,              -- numeric user_id в MAX
    notes         TEXT               -- произвольные заметки
);
```

### Таблица services (каталог услуг)
```sql
CREATE TABLE services (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL UNIQUE,   -- "3D-сканирование", "3D-печать", "Постобработка"
    unit     TEXT,                   -- "шт", "г", "см"
    price    REAL                    -- цена за единицу
);
```

### Таблица statuses (справочник статусов)
```sql
CREATE TABLE statuses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    order_rank INTEGER NOT NULL DEFAULT 0  -- для сортировки
);
```
Начальные значения:
```sql
INSERT INTO statuses (name, order_rank) VALUES
    ('принят', 1),
    ('в работе', 2),
    ('готов', 3),
    ('выдан', 4),
    ('отменён', 5);
```

### Таблица orders
```sql
CREATE TABLE orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   INTEGER NOT NULL REFERENCES clients(id),
    service_id  INTEGER NOT NULL REFERENCES services(id),
    status_id   INTEGER NOT NULL REFERENCES statuses(id),
    description TEXT,
    model_file  TEXT,       -- путь к файлу модели или ссылка
    price       REAL,      -- итоговая цена (может не совпадать с service.price)
    deadline    TEXT,      -- строка "2024-03-15" или ""
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### Таблица order_status_history (аудит-журнал)
```sql
CREATE TABLE order_status_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   INTEGER NOT NULL REFERENCES orders(id),
    status_id  INTEGER NOT NULL REFERENCES statuses(id),
    changed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```
Каждая смена статуса записывается сюда.

### Таблица notification_channels (какие каналы включены у клиента)
```sql
CREATE TABLE notification_channels (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    channel   TEXT NOT NULL CHECK (channel IN ('telegram', 'vk', 'max')),
    enabled   INTEGER NOT NULL DEFAULT 1,
    UNIQUE (client_id, channel)
);
```

### Таблица order_photos (фото привязаны к статусу)
```sql
CREATE TABLE order_photos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    status_id    INTEGER NOT NULL REFERENCES statuses(id),
    photo_data   BLOB    NOT NULL,
    mime_type    TEXT    NOT NULL DEFAULT 'image/jpeg',
    caption      TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
```

### Таблица order_services (доп. услуги к заказу, M:N)
```sql
CREATE TABLE order_services (
    order_id   INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    service_id INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    quantity   REAL    NOT NULL DEFAULT 1,
    price      REAL,  -- переопределение цены (NULL = цена из services)
    PRIMARY KEY (order_id, service_id)
);
```

### Индексы
```sql
CREATE INDEX idx_orders_status ON orders(status_id);
CREATE INDEX idx_orders_client ON orders(client_id);
CREATE INDEX idx_history_order ON order_status_history(order_id);
CREATE INDEX idx_photos_order  ON order_photos(order_id);
```

---

## 3. ВЕБ-ИНТЕРФЕЙС (Flask, Jinja2)

### Структура маршрутов

| Метод  | URL                                  | Назначение                    |
|--------|--------------------------------------|-------------------------------|
| GET    | `/`                                  | Главная — список заказов      |
| GET    | `/clients`                           | Список клиентов               |
| GET    | `/clients/new`                       | Форма создания клиента        |
| POST   | `/clients/new`                       | Создание клиента              |
| GET    | `/clients/<id>/edit`                 | Форма редактирования          |
| POST   | `/clients/<id>/edit`                 | Обновление клиента            |
| POST   | `/clients/<id>/delete`               | Удаление клиента              |
| GET    | `/services`                          | Список услуг                  |
| POST   | `/services/new`                      | Создание услуги               |
| POST   | `/services/<id>/update`              | Обновление услуги             |
| POST   | `/services/<id>/delete`              | Удаление услуги               |
| GET    | `/orders`                            | Список заказов (?status=&client=) |
| GET    | `/orders/new`                        | Форма создания заказа         |
| POST   | `/orders/new`                        | Создание заказа               |
| GET    | `/orders/<id>`                       | Детали заказа                 |
| GET    | `/orders/<id>/edit`                  | Форма редактирования заказа   |
| POST   | `/orders/<id>/edit`                  | Обновление заказа             |
| POST   | `/orders/<id>/status`                | Смена статуса (+ фото)        |
| POST   | `/orders/<id>/delete`                | Удаление заказа               |
| POST   | `/orders/<id>/extra/add`             | Добавить доп. услугу          |
| POST   | `/orders/<id>/extra/remove/<sid>`    | Удалить доп. услугу           |
| GET    | `/orders/photo/<id>`                 | Отдать фото заказа (BLOB)     |

### Поведение маршрутов

**POST /clients/new:**
- Валидация: `full_name` не пустой → flash "Укажите ФИО"
- Создание клиента → `add_client()`
- Установка каналов: `set_channel(client_id, channel, enabled)`
- Редирект на `/clients`

**POST /orders/new:**
- Валидация: `client_id` и `service_id` существуют
- Если `client_id` или `service_id` невалидны → flash + редирект
- Фото опционально: читаем `request.files.get("photo")`
- Создание через `OrderService.create_order()` → автоматическое уведомление клиента

**POST /orders/<id>/status:**
- Валидация `status_id`: положительное целое число
- Проверка существования статуса
- Проверка не изменился ли статус
- Фото опционально: читаем `request.files.get("status_photo")`
- Смена через `OrderService.change_status()` → уведомление клиента

**POST /clients/<id>/delete:**
- Проверка: `has_orders_for_client()` → если да, flash "Нельзя удалить"
- Иначе: `delete_client()`

**POST /services/<id>/delete:**
- Проверка: `has_orders_for_service()` → если да, flash "Нельзя удалить"
- Проверка уникальности имени при создании/обновлении

### Шаблоны (Jinja2)
```
templates/
├── base.html           -- базовый layout с навигацией
├── index.html          -- список заказов с фильтрами
├── clients.html        -- список клиентов
├── client_form.html    -- форма создания/редактирования клиента
├── services.html       -- список услуг
├── orders.html         -- список заказов с фильтрами
└── order_detail.html   -- детали заказа + история + фото + доп. услуги
```

### Ключевые UI-паттерны
- Flash-сообщения для feedback (успех/ошибка)
- Формы с POST + redirect (PRG pattern)
- Фильтрация заказов по статусу и клиенту через query params
- Формат цен: `{{ "%.0f"|format(price) }} ₽`

---

## 4. БОТ MAX (Оператор, Long Polling)

### Архитектура
- Long Polling: `GET /updates` каждые 30 секунд
- Inline-кнопки (callback data), без текстовых команд
- FSM через dict: `user_id → {"state": State, "data": {...}}`

### FSM States (перечисление)
```
MAIN_MENU
CHOOSING_CLIENT → CHOOSING_SERVICE → ENTERING_DESCRIPTION → AWAITING_PHOTO → CONFIRMING_ORDER
CHOOSING_STATUS → AWAITING_STATUS_PHOTO → CONFIRMING_STATUS_CHANGE
VIEWING_ORDER
CHOOSING_CLIENT_ACTION → ENTERING_CLIENT_NAME → ... → CONFIRMING_CLIENT_CREATE/UPDATE/DELETE
CHOOSING_ORDER_EDIT_FIELD → EDITING_ORDER_* → CONFIRMING_ORDER_EDIT/DELETE
```

### Callback-форматы
```
menu:main              -- главное меню
menu:orders            -- список заказов
menu:new_order         -- начало создания заказа
menu:clients           -- список клиентов
menu:services          -- список услуг

client:<id>            -- выбор клиента (при создании заказа)
client:view:<id>       -- просмотр клиента
client:edit:<id>       -- редактирование клиента
client:delete:<id>     -- удаление клиента
client:create          -- создание клиента

service:<id>           -- выбор услуги (при создании заказа)

order:<id>             -- просмотр заказа
order:edit:<id>        -- редактирование заказа
order:delete:<id>      -- удаление заказа

status:change:<id>     -- смена статуса заказа
status:photos:<id>     -- история фото заказа
status:extra:<id>      -- доп. услуги заказа

neworder:status:<oid>:<sid>  -- выбор нового статуса

extra:add:<oid>:<sid>  -- добавить доп. услугу

confirm:create_order   -- подтвердить создание заказа
confirm:change_status  -- подтвердить смену статуса
confirm:create_client  -- подтвердить создание клиента
confirm:delete_client  -- подтвердить удаление клиента
confirm:edit_order     -- подтвердить редактирование заказа
confirm:delete_order   -- подтвердить удаление заказа

edit_field:<field>     -- выбор поля для редактирования
client_ch:<channel>    -- переключение канала (при создании клиента)
client_ch_edit:<channel> -- переключение канала (при редактировании)
skip_photo             -- пропустить фото
skip_desc              -- пропустить описание

request_contact        -- запрос контакта
skip_contact           -- пропустить контакт
bind_client:<id>       -- привязка клиента по номеру
```

### Создание заказа (4 шага)
1. Выбор клиента (список из БД, кнопки)
2. Выбор услуги (список из БД с ценами)
3. Ввод описания (текст или «Пропустить»)
4. Фото (отправка фото или «Пропустить»)
5. Подтверждение (сводка + кнопки Да/Нет)

### Смена статуса
1. Просмотр заказа → кнопка «Сменить статус»
2. Выбор нового статуса (кнопки, текущий пропущен)
3. Фото для нового статуса (опционально)
4. Подтверждение

### Клиенты в боте
- Создание: послойно: ФИО → Телефон → TG ID → VK ID → MAX ID → Заметки → Каналы → Подтверждение
- Редактирование: выбор поля → ввод нового значения
- Удаление: проверка на заказы → подтверждение

---

## 5. УВЕДОМЛЕНИЯ (notifier.py)

### Архитектура
```python
class Notifier(Protocol):
    name: str
    def send(self, recipient_id: str, message: str) -> bool: ...
    def send_photo(self, recipient_id: str, photo_data: bytes, 
                   caption: str = "", mime_type: str = "image/jpeg") -> bool: ...
```

### Три реализации
1. **TelegramNotifier** — через API Telegram (с SOCKS5 прокси опционально)
2. **VKNotifier** — через VK Bot API (загрузка фото через 3 шага: get upload URL → upload → save → send)
3. **MaxNotifier** — через MAX Platform API (upload file → send message)

### Отправка уведомлений
```python
def send_notifications(recipient_channels, message, notifiers, 
                       photo_data=None, photo_caption="", photo_mime="image/jpeg",
                       on_error=None) -> dict[str, bool]:
```
- Проходит по всем каналам клиента
- Если канал включён и есть ID → отправляет
- Ошибка одного канала НЕ ломает остальные
- Возвращает `{канал: успех}`

### Формат сообщения
```python
def order_status_message(order, status_name) -> str:
    return (
        f"Ваш заказ #{order['id']} ({order['service_name']}): "
        f"статус изменился на «{status_name}».\n"
        f"Описание: {order['description'] or '—'}"
    )
```

---

## 6. BUSINESS LOGIC (OrderService)

### Единая точка логики (используется и вебом, и ботами)
```python
class OrderService:
    def __init__(self, db: Database):
        self.db = db

    def create_order(client_id, service_id, description, model_file, price, deadline, 
                     status_id, photo_data, photo_caption, photo_mime) -> int:
        # 1. Создать заказ
        # 2. Сохранить фото (если есть)
        # 3. Уведомить клиента
        # return order_id

    def change_status(order_id, new_status_id, photo_data, photo_caption, photo_mime) -> bool:
        # 1. Проверить существование заказа и статуса
        # 2. Проверить что статус изменился
        # 3. Обновить статус + записать в историю
        # 4. Сохранить фото (если есть)
        # 5. Уведомить клиента
        # return success

    def notify_status_change(order_id, status_name, payload) -> dict[str, bool]:
        # 1. Получить заказ и клиента
        # 2. Определить включённые каналы
        # 3. Отправить уведомление

    def add_extra_service(order_id, service_id, quantity, price) -> bool: ...
    def remove_extra_service(order_id, service_id) -> bool: ...
    def get_order_detail(order_id) -> dict | None: ...
    def list_orders(status_id, client_id) -> Sequence: ...
```

---

## 7. DEPLOYMENT (Orange Pi, systemd)

### Структура деплоя
```
deploy/
├── install.sh                 -- скрипт установки
├── orders.service             -- systemd юнит для веб-приложения
└── orders-max-bot.service     -- systemd юнит для бота MAX
```

### install.sh (порядок действий)
1. Установить системные пакеты: `python3 python3-venv python3-pip sqlite3`
2. Создать venv: `python3 -m venv /opt/orders_app/venv`
3. Установить зависимости: `pip install -r requirements.txt`
4. Скопировать файлы проекта
5. Скопировать systemd-юниты
6. Включить автозапуск

### systemd юниты
```ini
# orders.service
[Service]
User=pi
WorkingDirectory=/opt/orders_app
ExecStart=/opt/orders_app/venv/bin/python app.py
EnvironmentFile=/opt/orders_app/.env

# orders-max-bot.service
[Service]
User=pi
WorkingDirectory=/opt/orders_app
ExecStart=/opt/orders_app/venv/bin/python max_bot.py
EnvironmentFile=/opt/orders_app/.env
```

### .env (переменные окружения)
```
TG_TOKEN=...
VK_TOKEN=...
VK_GROUP_ID=...
MAX_TOKEN=...
MAX_ENDPOINT=https://platform-api2.max.ru
MAX_ADMIN_ID=12345,67890    # через запятую
SECRET_KEY=change-me
ORDERS_DB=instance/orders.db
```

---

## 8. ИЗВЕСТНЫЕ ПРОБЛЕМЫ И РЕШЕНИЯ

### Проблема: FK violation при удалении клиента с заказами
**Решение:** Проверка `has_orders_for_client()` ПЕРЕД удалением, возвращаем flash + redirect.

### Проблема: Некорректный status_id приводит к 500
**Решение:** Явная валидация: целое положительное число + проверка существования в БД.

### Проблема: Дубликат услуги (UNIQUE constraint) → 500
**Решение:** Проверка `get_service_by_name()` ПЕРЕД вставкой, flash "уже существует".

### Проблема: Удаление используемой услуги → FK violation
**Решение:** Проверка `has_orders_for_service()` ПЕРЕД удалением.

### Проблема: Создание заказа с несуществующим клиентом/услугой
**Решение:** Валидация `client_id` и `service_id` в маршруте, flash "Выберите существующего...".

### Проблема: Пустое ФИО при создании клиента
**Решение:** Проверка `.strip()` + flash "Укажите ФИО".

### Проблема: Уведомления ломаются при недоступном API
**Решение:** `try/except` в `send_notifications()` — ошибка одного канала не влияет на другие.

---

## 9. ТЕСТЫ

### Структура
```
tests/
├── conftest.py          -- добавляет корень проекта в sys.path
├── test_database.py     -- CRUD операции с БД (unit)
├── test_edge_cases.py   -- edge cases (500 ошибки, валидация)
├── test_max_bot.py      -- MAX бот (моки HTTP)
├── test_notifier.py     -- уведомления (моки API)
├── test_order_service.py -- бизнес-логика (моки DB + notifier)
└── integration_test.py  -- полный цикл через Flask test_client
```

### Паттерны тестирования
- Временные БД в `tmp_path` для изоляции
- `monkeypatch.setattr` для мока `build_notifiers`
- Flask `test_client()` для интеграционных тестов
- Проверка `r.status_code != 500` для edge cases
- Проверка flash-сообщений через `r.get_data(as_text=True)`

### Ключевые edge cases ( должны проходить)
1. Несуществующий статус (999) → flash, не 500
2. Отрицательный статус (-1) → flash, не 500
3. Нечисловой статус ("abc") → flash, не 500
4. Несуществующий клиент (9999) → flash, не 500
5. Несуществующая услуга (9999) → flash, не 500
6. Удаление клиента с заказами → flash, не 500
7. Удаление используемой услуги → flash, не 500
8. Несуществующий клиент при редактировании → 404
9. Несуществующий заказ → 404
10. Отключённый канал → уведомление не отправляется
11. Пустой ID канала → уведомление не отправляется
12. Пустое ФИО → flash, клиент не создан
13. Дубликат услуги → flash "уже существует", не 500

---

## 10. СТРУКТУРА ФАЙЛОВ

```
sunduk_app/
├── app.py              -- Flask: app factory + routes (≈400 строк)
├── database.py         -- Database class: все SQL запросы (≈370 строк)
├── db_schema.py        -- DDL + seed_defaults + migrate_db (≈150 строк)
├── notifier.py         -- Notifier protocol + 3 реализации + send_notifications (≈280 строк)
├── max_bot.py          -- MaxBot class: Long Polling + FSM (≈1300 строк)
├── tg_bot.py           -- TgBot class: ConversationHandler (≈1000 строк)
├── services/
│   └── order_service.py -- OrderService: бизнес-логика (≈170 строк)
├── templates/
│   ├── base.html
│   ├── index.html
│   ├── clients.html
│   ├── client_form.html
│   ├── services.html
│   ├── orders.html
│   └── order_detail.html
├── static/
│   └── style.css
├── tests/
│   ├── conftest.py
│   ├── test_database.py
│   ├── test_edge_cases.py
│   ├── test_max_bot.py
│   ├── test_notifier.py
│   ├── test_order_service.py
│   └── integration_test.py
├── deploy/
│   ├── install.sh
│   ├── orders.service
│   └── orders-max-bot.service
├── .env.example
├── requirements.txt
├── AGENTS.md
└── instance/           -- SQLite файл (gitignored)
```

---

## 11. ТРЕБОВАНИЯ К НОВОМУ ПРОЕКТУ

### Что должно быть
- ✅ Чистая архитектура с разделением ответственности
- ✅ Database class — изолированный слой для SQL
- ✅ OrderService — единая точка бизнес-логики
- ✅ Notifier protocol — заменяемые реализации
- ✅ Валидация ВСЕХ входных данных до БД
- ✅ Flash-сообщения для пользовательского feedback
- ✅ Тесты для каждого слоя
- ✅ Тесты edge cases (каждый 500-сценарий)
- ✅ Интеграционные тесты полного цикла

### Чего ИЗБЕГАТЬ
- ❌ Дублирование логики (веб и бот должны вызывать OrderService)
- ❌ Прямые SQL запросы из маршрутов (всё через Database class)
- ❌ Хранение токенов в коде (только через .env)
- ❌ Обработка ошибок через bare `except:` (всегда конкретный Exception)
- ❌ Незахваченные FK violations → 500 ошибки
- ❌ Отсутствие валидации перед DB операциями
- ❌ Смешение бота MAX и Notifier MaxNotifier (это разные вещи!)

### Именование
- `max_bot.py` — бот оператора в MAX (Long Polling)
- `notifier.py` → `MaxNotifier` — уведомление клиента через MAX
- НЕ ПУТАТЬ: `MaxBot` ≠ `MaxNotifier`
