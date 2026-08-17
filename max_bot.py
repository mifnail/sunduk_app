"""Чат-бот MAX для оператора студии.

Бот работает в режиме Long Polling (GET /updates) и позволяет оператору
управлять заказами из мессенджера MAX: список заказов, детали заказа,
смена статуса, список клиентов. Смена статуса через бота автоматически
уведомляет клиента во все включённые каналы.
"""

import logging
import os
import time

import requests
from dotenv import load_dotenv

from database import Database
from db_schema import init_db, seed_defaults
from notifier import build_notifiers, order_status_message, send_notifications

log = logging.getLogger("max_bot")

BASE_URL = "https://platform-api2.max.ru"

HELP_TEXT = (
    "Студия 3D — бот оператора.\n\n"
    "Команды:\n"
    "/help — помощь\n"
    "/orders — список заказов\n"
    "/order <номер> — детали заказа\n"
    "/status <номер> [статус] — сменить статус\n"
    "/clients — список клиентов"
)


class MaxBot:
    def __init__(self, token: str = "", admin_ids: str = "",
                 db_path: str = "instance/orders.db",
                 base_url: str = BASE_URL,
                 poll_timeout: int = 30,
                 session: requests.Session | None = None):
        self.token = token or os.environ.get("MAX_TOKEN", "")
        self.base_url = base_url
        self.admin_ids = {x.strip() for x in (admin_ids or os.environ.get("MAX_ADMIN_ID", "")).split(",") if x.strip()}
        self.db = Database(db_path)
        self.poll_timeout = poll_timeout
        self.marker = None
        self.session = session or requests.Session()

    # ---------- HTTP ----------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self.token}

    def send_message(self, user_id: str, text: str, buttons: list | None = None) -> bool:
        body: dict = {"text": text}
        if buttons:
            body["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]
        resp = self.session.post(
            f"{self.base_url}/messages",
            params={"user_id": user_id},
            headers=self._headers(),
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        return True

    def answer_callback(self, callback_id: str, text: str, buttons: list | None = None) -> bool:
        message: dict = {"text": text}
        if buttons:
            message["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]
        resp = self.session.post(
            f"{self.base_url}/answers",
            params={"callback_id": callback_id},
            headers=self._headers(),
            json={"message": message},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("success", False)

    def get_updates(self) -> list[dict]:
        params = {"limit": 100, "timeout": self.poll_timeout}
        if self.marker is not None:
            params["marker"] = self.marker
        resp = self.session.get(
            f"{self.base_url}/updates",
            params=params,
            headers=self._headers(),
            timeout=self.poll_timeout + 20,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("marker") is not None:
            self.marker = data["marker"]
        return data.get("updates") or []

    # ---------- Обработка событий ----------

    def handle_update(self, update: dict) -> None:
        up_type = update.get("update_type")
        if up_type == "message_created":
            self._on_message(update)
        elif up_type == "message_callback":
            self._on_callback(update)
        elif up_type in ("bot_started", "bot_added"):
            self._on_start(update)

    def _sender_id(self, update: dict) -> str | None:
        user = update.get("user") or {}
        if user.get("id") is not None:
            return str(user["id"])
        message = update.get("message") or {}
        sender = message.get("sender") or {}
        if sender.get("id") is not None:
            return str(sender["id"])
        callback = update.get("callback") or {}
        cb_user = callback.get("user") or {}
        if cb_user.get("id") is not None:
            return str(cb_user["id"])
        if update.get("chat_id") is not None:
            return str(update["chat_id"])
        return None

    def _is_admin(self, user_id: str) -> bool:
        return bool(self.admin_ids) and user_id in self.admin_ids

    def _on_start(self, update: dict) -> None:
        user_id = self._sender_id(update)
        if user_id:
            self.send_message(user_id, HELP_TEXT)

    def _on_message(self, update: dict) -> None:
        user_id = self._sender_id(update)
        if user_id is None:
            return
        if not self._is_admin(user_id):
            self.send_message(user_id, "Доступ запрещён: вы не являетесь оператором студии.")
            return
        message = update.get("message") or {}
        body = message.get("body") or {}
        text = (body.get("text") or "").strip()
        if not text:
            return
        self._dispatch(user_id, text)

    def _on_callback(self, update: dict) -> None:
        callback = update.get("callback") or {}
        callback_id = callback.get("callback_id")
        payload = str(callback.get("payload") or "")
        user_id = self._sender_id(update)
        if not self._is_admin(user_id or ""):
            return
        if callback_id is None:
            return
        parts = payload.split(":")
        if parts[0] == "status" and len(parts) == 3:
            self._set_status(user_id, parts[1], parts[2], callback_id=callback_id)
        elif parts[0] == "order" and len(parts) == 2:
            order = self.db.get_order(_to_int(parts[1]) or 0)
            if order:
                text, buttons = self._order_view(order)
                self.answer_callback(callback_id, text, buttons)

    # ---------- Команды ----------

    def _dispatch(self, user_id: str, text: str) -> None:
        parts = text.split()
        cmd = parts[0].lower().lstrip("/")
        args = parts[1:]
        if cmd in ("start", "help", "помощь"):
            self.send_message(user_id, HELP_TEXT)
        elif cmd == "orders":
            self._cmd_orders(user_id)
        elif cmd == "order":
            self._cmd_order(user_id, args)
        elif cmd == "status":
            self._cmd_status(user_id, args)
        elif cmd == "clients":
            self._cmd_clients(user_id)
        else:
            self.send_message(user_id, HELP_TEXT)

    def _cmd_orders(self, user_id: str) -> None:
        orders = self.db.list_orders()
        if not orders:
            self.send_message(user_id, "Заказов пока нет.")
            return
        lines = []
        buttons = []
        for order in orders[:30]:
            lines.append(
                f"#{order['id']} {order['client_name']} — {order['service_name']} · {order['status_name']}"
            )
            buttons.append([{"type": "callback", "text": f"Заказ #{order['id']}", "payload": f"order:{order['id']}"}])
        self.send_message(user_id, "Заказы:\n" + "\n".join(lines), buttons)

    def _cmd_order(self, user_id: str, args: list[str]) -> None:
        if not args or not args[0].isdigit():
            self.send_message(user_id, "Формат: /order <номер заказа>")
            return
        order = self.db.get_order(int(args[0]))
        if order is None:
            self.send_message(user_id, f"Заказ #{args[0]} не найден.")
            return
        text, buttons = self._order_view(order)
        self.send_message(user_id, text, buttons)

    def _cmd_status(self, user_id: str, args: list[str]) -> None:
        if not args or not args[0].isdigit():
            self.send_message(user_id, "Формат: /status <номер заказа> [статус]")
            return
        order = self.db.get_order(int(args[0]))
        if order is None:
            self.send_message(user_id, f"Заказ #{args[0]} не найден.")
            return
        if len(args) >= 2:
            self._set_status(user_id, args[0], args[1])
            return
        _, buttons = self._order_view(order)
        self.send_message(user_id, "Выберите новый статус:", buttons)

    def _cmd_clients(self, user_id: str) -> None:
        clients = self.db.list_clients()
        if not clients:
            self.send_message(user_id, "Клиентов пока нет.")
            return
        lines = [f"#{c['id']} {c['full_name']} ({c['phone'] or '—'})" for c in clients[:30]]
        self.send_message(user_id, "Клиенты:\n" + "\n".join(lines))

    # ---------- Логика заказов ----------

    def _order_view(self, order) -> tuple[str, list]:
        text = (
            f"Заказ #{order['id']}\n"
            f"Клиент: {order['client_name']}\n"
            f"Услуга: {order['service_name']}\n"
            f"Статус: {order['status_name']}\n"
            f"Описание: {order['description'] or '—'}\n"
            f"Цена: {order['price'] or '—'}\n"
            f"Дедлайн: {order['deadline'] or '—'}"
        )
        return text, self._status_buttons(order)

    def _status_buttons(self, order) -> list:
        buttons = []
        row = []
        for st in self.db.list_statuses():
            row.append({
                "type": "callback",
                "text": st["name"],
                "payload": f"status:{order['id']}:{st['id']}",
            })
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        return buttons

    def _set_status(self, user_id: str, order_ref: str, status_ref: str,
                    callback_id: str | None = None) -> None:
        order = self.db.get_order(_to_int(order_ref) or 0)
        if order is None:
            self._reply(user_id, callback_id, f"Заказ #{order_ref} не найден.")
            return
        status = None
        for st in self.db.list_statuses():
            if str(st["id"]) == str(status_ref) or st["name"].lower() == status_ref.lower():
                status = st
                break
        if status is None:
            self._reply(user_id, callback_id, f"Статус «{status_ref}» не найден.")
            return
        if status["id"] == order["status_id"]:
            self._reply(user_id, callback_id, f"Статус заказа #{order_ref} уже «{status['name']}».")
            return
        self.db.set_order_status(order["id"], status["id"])
        client = self.db.get_client(order["client_id"])
        channel_map = {"telegram": "telegram_id", "vk": "vk_id", "max": "max_id"}
        enabled = {c["channel"] for c in self.db.list_channels(order["client_id"]) if c["enabled"]}
        channels = {ch: client[channel_map[ch]] for ch in enabled if client[channel_map[ch]]}
        send_notifications(channels, order_status_message(order, status["name"]), build_notifiers())
        self._reply(user_id, callback_id, f"Статус заказа #{order_ref} изменён на «{status['name']}».")

    def _reply(self, user_id: str, callback_id: str | None, text: str) -> None:
        if callback_id:
            self.answer_callback(callback_id, text)
        else:
            self.send_message(user_id, text)

    # ---------- Запуск ----------

    def run_forever(self) -> None:
        log.info("MAX-бот запущен (Long Polling)")
        while True:
            try:
                updates = self.get_updates()
                for update in updates:
                    try:
                        self.handle_update(update)
                    except Exception as exc:  # noqa: BLE001 - изоляция событий
                        log.warning("Ошибка обработки события: %s", exc)
            except Exception as exc:  # noqa: BLE001 - пережидаем сбой API
                log.warning("Ошибка Long Polling: %s", exc)
                time.sleep(5)


def _to_int(value: str) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    instance = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance")
    os.makedirs(instance, exist_ok=True)
    db_path = os.environ.get("ORDERS_DB", os.path.join(instance, "orders.db"))
    init_db(db_path)
    seed_defaults(db_path)
    bot = MaxBot(db_path=db_path)
    if not bot.token:
        log.error("MAX_TOKEN не задан — бот не запущен")
        return
    if not bot.admin_ids:
        log.warning("MAX_ADMIN_ID не задан — команды будут отклоняться")
    bot.run_forever()


if __name__ == "__main__":
    main()