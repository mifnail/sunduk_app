# AGENTS.md — инструкции для работы с репозиторием

## Общее
- **Проект**: Flask-приложение для управления заказами студии 3D-печати/сканирования + бот MAX для оператора.
- **Стек**: Python 3, Flask, SQLite (3НФ), Jinja2, pytest, systemd (Orange Pi ARM64).
- **Главные файлы**: `app.py` (веб), `max_bot.py` (бот), `database.py` (SQL), `db_schema.py` (схема), `notifier.py` (уведомления).

## Команды разработки
```bash
# Установка зависимостей
pip install -r requirements.txt

# Запуск веб-приложения (порт 5000)
python app.py

# Запуск бота MAX (Long Polling)
python max_bot.py

# Тесты
pytest              # все тесты
pytest tests/test_database.py  # конкретный файл
pytest -k "test_add_client"    # по паттерну

# Линтинг/форматирование (нет настроек — не запускай)
```

## Переменные окружения (`.env`, см. `.env.example`)
| Переменная | Назначение |
|------------|------------|
| `TG_TOKEN` | Токен Telegram-бота (@BotFather) |
| `VK_TOKEN`, `VK_GROUP_ID` | Токен и ID группы VK Bot API |
| `MAX_TOKEN`, `MAX_ENDPOINT` | Токен и эндпоинт бота MAX (по умолчанию `https://platform-api2.max.ru`) |
| `MAX_ADMIN_ID` | user_id операторов через запятую (доступ к боту) |
| `SECRET_KEY` | Секрет Flask |
| `ORDERS_DB` | Путь к БД (по умолчанию `instance/orders.db`) |

## Архитектура — что важно знать
- **БД**: SQLite, схема в `db_schema.py` (нормализация 3НФ). Таблицы: `clients`, `services`, `statuses`, `orders`, `order_status_history`, `notification_channels`.
- **Порядок инициализации**: `init_db()` → `seed_defaults()` (вызываются в `create_app()` и `max_bot.main()`).
- **Уведомления**: `notifier.build_notifiers()` создаёт три нотификатора (Telegram, VK, MAX). `send_notifications()` шлёт во все включённые каналы клиента — ошибка одного не ломает остальные.
- **Бот MAX**: работает через Long Polling (`GET /updates`), без вебхуков/HTTPS. Команды: `/orders`, `/order <id>`, `/status <id> [статус]`, `/clients`. Смена статуса через бота использует ту же логику уведомлений, что и веб.

## Тесты — нюансы
- `conftest.py` только добавляет корень проекта в `sys.path`.
- Тесты создают временные БД в `tmp_path` (см. `test_database.py`).
- Интеграционные тесты в `integration_test.py` проверяют полный цикл заказа + уведомления.
- Нет фикстур для внешних API — мокаются/пропускаются при отсутствии токенов.

## Деплой (Orange Pi)
```bash
# На целевой машине:
sudo bash deploy/install.sh
```
Скрипт: ставит системные пакеты → создаёт venv в `/opt/orders_app/venv` → ставит requirements → копирует systemd-юниты → включает автозапуск.
- Сервисы: `orders` (веб, порт 5000), `orders-max-bot` (бот).
- Юниты в `deploy/*.service` — пользователь `pi`, `EnvironmentFile=/opt/orders_app/.env`.

## Частые ошибки агентов
1. **Не путать** `max_bot.py` (бот оператора) с `notifier.MaxNotifier` (уведомления клиентам) — это разные классы.
2. **БД не мигрируется** — схема создаётся `init_db()` при старте. Изменения схемы = ручной SQL или пересоздание БД.
3. **Токены не в коде** — всегда через `.env` / `os.environ.get()`.
4. **Тесты не требуют запущенных сервисов** — они изолированы.
5. **Нет линтеров/форматтеров** — не пытайся запустить `ruff`, `black`, `mypy` и т.п.

## Структура каталогов
```
app.py              — Flask app factory + routes
max_bot.py          — Long Polling бот MAX
database.py         — класс Database (все SQL)
db_schema.py        — DDL + seed
notifier.py         — протокол Notifier + 3 реализации + отправка
templates/          — Jinja2 шаблоны
static/             — CSS/JS (минимум)
tests/              — pytest (unit + integration)
deploy/             — install.sh + systemd units
instance/           — SQLite файл (gitignored)
venv/               — виртуальное окружение (gitignored)
```