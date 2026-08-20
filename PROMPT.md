# ПРОМПТ ДЛЯ РАЗРАБОТКИ: Система управления заказами студии 3D-печати

## Постановка задачи

Разработать систему управления заказами для студии 3D-печати и 3D-сканирования. Система состоит из трёх компонентов:
1. **Веб-интерфейс** (Flask + Jinja2) — для оператора
2. **Бот MAX** (Long Polling) — кнопочный интерфейс для оператора в мессенджере MAX
3. **Уведомления клиентов** — автоматическая отправка по Telegram / VK / MAX при смене статуса заказа

Среда: Orange Pi, ARM64, Python 3.11+, SQLite, systemd.

---

## Архитектура

### Принципы
- **3 слоя**: Routes (Flask) → Service (OrderService) → Database (SQLite)
- **Дублирование = зло**: логика создания заказа / смены статуса одна — в `OrderService`
- **Валидация ДО БД**: каждый POST-маршрут проверяет данные перед SQL
- **Одна ошибка не ломает другую**: уведомления отправляются в каждый канал независимо

### Структура файлов
```
├── app.py                  # Flask app factory + routes (~400 строк)
├── database.py             # Database class, все SQL (~370 строк)
├── db_schema.py            # DDL + seed + migrate (~150 строк)
├── notifier.py             # Protocol + 3 Notifier + send_notifications (~280 строк)
├── max_bot.py              # MaxBot, Long Polling + FSM (~1300 строк)
├── tg_bot.py               # TgBot, python-telegram-bot (~1000 строк)
├── services/order_service.py  # Бизнес-логика (~170 строк)
├── templates/              # 7 Jinja2 шаблонов
├── tests/                  # pytest, 6 файлов
├── deploy/                 # install.sh + 2 systemd юнита
└── .env.example
```

---

## Модель данных (SQLite)

### Таблица `clients`
| Поле         | Тип    | Описание                              |
|-------------|--------|---------------------------------------|
| id          | INTEGER PK | Autoincrement                     |
| full_name   | TEXT NOT NULL | ФИО клиента                      |
| phone       | TEXT   | "+79001234567"                        |
| telegram_id | TEXT   | Numeric ID в Telegram                 |
| vk_id       | TEXT   | Numeric ID в VK                       |
| max_id      | TEXT   | Numeric ID в MAX                      |
| notes       | TEXT   | Заметки                               |

### Таблица `services`
| Поле  | Тип    | Описание |
|-------|--------|----------|
| id    | INTEGER PK | Autoincrement |
| name  | TEXT NOT NULL UNIQUE | "3D-сканирование", "3D-печать", "Постобработка" |
| unit  | TEXT   | "шт", "г", "см" |
| price | REAL   | Цена за единицу |

### Таблица `statuses`
| Поле       | Тип    | Описание |
|-----------|--------|----------|
| id        | INTEGER PK | Autoincrement |
| name      | TEXT NOT NULL UNIQUE | "принят", "в работе", "готов", "выдан", "отменён" |
| order_rank| INTEGER | Для сортировки (1,2,3,4,5) |

### Таблица `orders`
| Поле        | Тип    | Описание |
|------------|--------|----------|
| id         | INTEGER PK | Autoincrement |
| client_id  | INTEGER FK → clients | Обязательный |
| service_id | INTEGER FK → services | Обязательный |
| status_id  | INTEGER FK → statuses | Обязательный |
| description| TEXT   | Описание заказа |
| model_file | TEXT   | Путь к файлу модели |
| price      | REAL   | Итоговая цена |
| deadline   | TEXT   | Строка даты |
| created_at | TEXT DEFAULT datetime('now') | |
| updated_at | TEXT DEFAULT datetime('now') | |

### Таблица `order_status_history`
| Поле      | Тип | Описание |
|----------|-----|----------|
| id       | INTEGER PK | |
| order_id | INTEGER FK → orders | |
| status_id| INTEGER FK → statuses | |
| changed_at| TEXT DEFAULT datetime('now') | |

### Таблица `notification_channels`
| Поле      | Тип | Описание |
|----------|-----|----------|
| id       | INTEGER PK | |
| client_id| INTEGER FK → clients | |
| channel  | TEXT CHECK IN ('telegram','vk','max') | |
| enabled  | INTEGER DEFAULT 1 | |
| UNIQUE(client_id, channel) | | |

### Таблица `order_photos`
| Поле      | Тип | Описание |
|----------|-----|----------|
| id       | INTEGER PK | |
| order_id | INTEGER FK → orders ON DELETE CASCADE | |
| status_id| INTEGER FK → statuses | |
| photo_data| BLOB NOT NULL | Двоичные данные фото |
| mime_type| TEXT DEFAULT 'image/jpeg' | |
| caption  | TEXT | Подпись |
| created_at| TEXT DEFAULT datetime('now') | |

### Таблица `order_services` (M:N, доп. услуги)
| Поле      | Тип | Описание |
|----------|-----|----------|
| order_id | INTEGER FK → orders ON DELETE CASCADE | |
| service_id| INTEGER FK → services ON DELETE CASCADE | |
| quantity | REAL DEFAULT 1 | |
| price    | REAL | Переопределение цены (NULL = цена из services) |
| PRIMARY KEY(order_id, service_id) | | |

---

## Веб-интерфейс (Flask)

### Маршруты

```
GET  /                              → Список заказов (фильтры: status, client)
GET  /clients                       → Список клиентов
GET  /clients/new                   → Форма создания
POST /clients/new                   → Создание (+ каналы уведомлений)
GET  /clients/<id>/edit             → Форма редактирования
POST /clients/<id>/edit             → Обновление
POST /clients/<id>/delete           → Удаление (с проверкой FK)

GET  /services                      → Список услуг
POST /services/new                  → Создание (с проверкой уникальности)
POST /services/<id>/update          → Обновление
POST /services/<id>/delete          → Удаление (с проверкой FK)

GET  /orders                        → Список заказов (фильтры)
GET  /orders/new                    → Форма создания
POST /orders/new                    → Создание через OrderService
GET  /orders/<id>                   → Детали заказа
GET  /orders/<id>/edit              → Форма редактирования
POST /orders/<id>/edit              → Обновление
POST /orders/<id>/status            → Смена статуса (+ фото)
POST /orders/<id>/delete            → Удаление
POST /orders/<id>/extra/add         → Добавить доп. услугу
POST /orders/<id>/extra/remove/<sid>→ Удалить доп. услугу
GET  /orders/photo/<id>             → Отдать фото (BLOB)
```

### Паттерн каждого POST-маршрута
```python
@app.post("/resource/<id>/action")
def action(id: int):
    d = db()
    # 1. Валидация входных данных
    value = _int(request.form.get("field"))
    if value is None or value <= 0:
        flash("Неверное значение")
        return redirect(url_for("detail", id=id))
    
    # 2. Проверка существования связанной сущности
    entity = d.get_entity(id)
    if entity is None:
        abort(404)
    
    related = d.get_related(value)
    if related is None:
        flash("Связанная сущность не найдена")
        return redirect(url_for("detail", id=id))
    
    # 3. Проверка бизнес-правил
    if d.has_dependents(id):
        flash("Нельзя выполнить: есть зависимости")
        return redirect(url_for("list_view"))
    
    # 4. Выполнение операции
    do_something()
    
    # 5. Feedback
    flash("Операция выполнена")
    return redirect(url_for("detail", id=id))
```

---

## Бот MAX

### Принципы
- **Длинное опросывание**: `GET /updates` каждые 30 секунд
- **Только кнопки**: пользователь не вводит текст (кроме описания заказа и полей клиента)
- **FSM через dict**: `user_states[user_id] = {"state": State, "data": {...}}`
- **Callback data формат**: `action:sub_action:param1:param2`

### FSM States (перечисление)
```python
class State(Enum):
    MAIN_MENU
    # Создание заказа
    CHOOSING_CLIENT, CHOOSING_SERVICE, ENTERING_DESCRIPTION, AWAITING_PHOTO, CONFIRMING_ORDER
    # Смена статуса
    CHOOSING_STATUS, AWAITING_STATUS_PHOTO, CONFIRMING_STATUS_CHANGE
    # Просмотр
    VIEWING_ORDER
    # Клиенты
    CHOOSING_CLIENT_ACTION, ENTERING_CLIENT_NAME, ENTERING_CLIENT_PHONE, 
    ENTERING_CLIENT_TG_ID, ENTERING_CLIENT_VK_ID, ENTERING_CLIENT_MAX_ID, 
    ENTERING_CLIENT_NOTES, CONFIRMING_CLIENT_CREATE, CONFIRMING_CLIENT_UPDATE, 
    CONFIRMING_CLIENT_DELETE
    # Редактирование заказа
    CHOOSING_ORDER_EDIT_FIELD, EDITING_ORDER_DESCRIPTION, EDITING_ORDER_PRICE, 
    EDITING_ORDER_DEADLINE, CONFIRMING_ORDER_EDIT, CONFIRMING_ORDER_DELETE
```

### Ключевые callback-цепочки
```
Создание заказа:
  menu:new_order → client:<id> → service:<id> → text description → skip_photo/photo → confirm:create_order

Смена статуса:
  status:change:<oid> → neworder:status:<oid>:<sid> → skip_photo/photo → confirm:change_status

Создание клиента:
  client:create → text name → text phone → text tg_id → text vk_id → text max_id → text notes → confirm:create_client
```

---

## Уведомления

### Protocol
```python
@runtime_checkable
class Notifier(Protocol):
    name: str
    def send(self, recipient_id: str, message: str) -> bool: ...
    def send_photo(self, recipient_id: str, photo_data: bytes, 
                   caption: str = "", mime_type: str = "image/jpeg") -> bool: ...
```

### 3 реализации
1. **TelegramNotifier**: `POST /bot{token}/sendMessage` и `sendPhoto`
2. **VKNotifier**: `messages.send` + 3-шаговая загрузка фото (get upload URL → upload → save → send attachment)
3. **MaxNotifier**: `POST /messages` + `POST /files` для фото

### Отправка
```python
def send_notifications(channels: dict[str, str], message: str, 
                       notifiers: dict[str, Notifier],
                       photo_data=None, photo_caption="", photo_mime="image/jpeg",
                       on_error=None) -> dict[str, bool]:
    """Каждый канал независим. Ошибка одного не влияет на другие."""
```

---

## OrderService

```python
class OrderService:
    def __init__(self, db: Database):
        self.db = db

    def create_order(client_id, service_id, description="",
                     model_file="", price=None, deadline="",
                     status_id=1, photo_data=None, 
                     photo_caption="", photo_mime="image/jpeg") -> int:
        """Создаёт заказ, сохраняет фото, уведомляет клиента."""

    def change_status(order_id, new_status_id, 
                      photo_data=None, photo_caption="",
                      photo_mime="image/jpeg") -> bool:
        """Меняет статус, сохраняет фото, уведомляет клиента."""

    def notify_status_change(order_id, status_name, payload=None) -> dict[str, bool]:
        """Отправляет уведомление во все включённые каналы."""

    def add_extra_service(order_id, service_id, quantity=1, price=None) -> bool: ...
    def remove_extra_service(order_id, service_id) -> bool: ...
    def get_order_detail(order_id) -> dict | None: ...
    def list_orders(status_id=None, client_id=None) -> Sequence: ...
```

---

## Database Class

Все SQL запросы в одном классе. Каждый метод — одна операция.

```python
class Database:
    def __init__(self, db_path: str): ...
    
    # Клиенты
    def add_client(full_name, phone, telegram_id, vk_id, max_id, notes) -> int: ...
    def list_clients() -> Sequence[Row]: ...
    def get_client(client_id) -> Row | None: ...
    def update_client(client_id, **fields) -> None: ...
    def delete_client(client_id) -> None: ...
    def has_orders_for_client(client_id) -> bool: ...
    def get_client_by_phone(phone) -> Sequence[Row]: ...
    
    # Каналы
    def set_channel(client_id, channel, enabled) -> None: ...  # UPSERT
    def list_channels(client_id) -> Sequence[Row]: ...
    
    # Услуги
    def add_service(name, unit, price) -> int: ...
    def list_services() -> Sequence[Row]: ...
    def get_service(service_id) -> Row | None: ...
    def get_service_by_name(name) -> Row | None: ...
    def update_service(service_id, **fields) -> None: ...
    def delete_service(service_id) -> None: ...
    def has_orders_for_service(service_id) -> bool: ...
    
    # Статусы
    def list_statuses() -> Sequence[Row]: ...
    def get_status(status_id) -> Row | None: ...
    
    # Заказы
    def add_order(client_id, service_id, description, model_file, price, deadline, status_id) -> int: ...
    def list_orders(status_id=None, client_id=None) -> Sequence[Row]: ...
    def get_order(order_id) -> Row | None: ...  # JOIN с clients, services, statuses
    def set_order_status(order_id, status_id) -> None: ...  # UPDATE + INSERT history
    def order_history(order_id) -> Sequence[Row]: ...
    def update_order(order_id, **fields) -> None: ...
    def delete_order(order_id) -> None: ...
    def list_orders_with_photos(status_id=None, client_id=None) -> Sequence: ...  # с превью фото
    
    # Фото
    def add_order_photo(order_id, status_id, photo_data, mime_type, caption) -> int: ...
    def get_order_photos(order_id) -> Sequence[Row]: ...
    def get_order_photo(photo_id) -> Row | None: ...
    
    # Доп. услуги
    def add_service_to_order(order_id, service_id, quantity, price) -> None: ...  # UPSERT
    def get_order_services(order_id) -> Sequence[Row]: ...
    def remove_service_from_order(order_id, service_id) -> None: ...
    def calculate_order_total(order_id) -> float: ...
    def calculate_extra_total(order_id) -> float: ...
```

### Важные SQL-паттерны

```python
# UPSERT для каналов и доп. услуг
INSERT INTO ... ON CONFLICT(...) DO UPDATE SET ...

# Заказ с JOIN для отображения
SELECT o.*, c.full_name AS client_name, s.name AS service_name, 
       st.name AS status_name 
FROM orders o 
JOIN clients c ON c.id = o.client_id
JOIN services s ON s.id = o.service_id
JOIN statuses st ON st.id = o.status_id

# Смена статуса: UPDATE + INSERT history в одном соединении
UPDATE orders SET status_id = ?, updated_at = datetime('now') WHERE id = ?
INSERT INTO order_status_history (order_id, status_id) VALUES (?, ?)

# Превью фото (подзапрос)
SELECT ... (SELECT p.id FROM order_photos p WHERE p.order_id = o.id 
            ORDER BY p.created_at DESC LIMIT 1) AS latest_photo_id
```

---

## Тесты

### Требования
- Все тесты изолированы: временные БД в `tmp_path`
- Внешние API замоканы: `monkeypatch.setattr("notifier.build_notifiers", lambda: {...})`
- Интеграционные тесты: Flask `test_client()` с `follow_redirects=True`

### Минимальный набор тестов (должны проходить)
1. CRUD клиентов (создание, чтение, обновление, удаление)
2. CRUD услуг (создание, чтение, обновление, удаление)
3. Создание заказа с проверкой FK constraints
4. Смена статуса с уведомлением
5. Отправка уведомлений в все каналы
6. Ошибка одного канала не ломает другие
7. Edge cases: несуществующие ID, отрицательные ID, нечисловые ID, пустые поля
8. Защита от удаления зависимых записей

### Запуск
```bash
pytest tests/ -v           # все тесты
pytest tests/test_edge_cases.py -v  # edge cases
```

---

## Важные замечания

### Не путать!
- `max_bot.py` — бот OPERATOR в MAX (FSM, кнопки)
- `notifier.py` → `MaxNotifier` — уведомление CLIENT через MAX
- Это **разные классы** для **разных целей**

### Валидация (всегда перед БД)
- `status_id`: положительное целое число + существует в БД
- `client_id`: существует в БД
- `service_id`: существует в БД
- `full_name`: не пустой после `.strip()`
- `phone`: нормализация через `_normalize_phone()`

### Безопасность
- Токены только в `.env`, never in code
- `os.environ.get()` для чтения
- FK constraints включены: `PRAGMA foreign_keys = ON`
- Каждое соединение с БД: `with self.connect() as conn:` (автокоммит)

### Деплой
- Python 3.11+ на Orange Pi ARM64
- venv в `/opt/orders_app/venv`
- 2 systemd юнита: `orders` (веб) + `orders-max-bot` (бот)
- `.env` файл рядом с проектом
