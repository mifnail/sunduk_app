"""Чат-бот MAX для оператора студии 3D-печати/сканирования.

Бот работает в режиме Long Polling (GET /updates).
Полностью кнопочный интерфейс — команды вводить не нужно.
Поддерживает: создание заказа с фото, смену статуса с фото, просмотр заказов/клиентов.
"""

import logging
import os
import time
from enum import Enum
from typing import Any

import requests
from dotenv import load_dotenv

from database import Database
from db_schema import init_db, seed_defaults, migrate_db
from notifier import build_notifiers, order_status_message, send_notifications

log = logging.getLogger("max_bot")

BASE_URL = "https://platform-api2.max.ru"

# ---------- FSM States ----------

class State(Enum):
    MAIN_MENU = "main_menu"
    # Order creation flow
    CHOOSING_CLIENT = "choosing_client"
    CHOOSING_SERVICE = "choosing_service"
    ENTERING_DESCRIPTION = "entering_description"
    AWAITING_PHOTO = "awaiting_photo"
    CONFIRMING_ORDER = "confirming_order"
    # Status change flow
    CHOOSING_STATUS = "choosing_status"
    AWAITING_STATUS_PHOTO = "awaiting_status_photo"
    # View order
    VIEWING_ORDER = "viewing_order"

# ---------- Keyboards ----------

def main_menu_keyboard() -> list:
    return [
        [{"type": "callback", "text": "📋 Заказы", "payload": "menu:orders"}],
        [{"type": "callback", "text": "➕ Новый заказ", "payload": "menu:new_order"}],
        [{"type": "callback", "text": "👥 Клиенты", "payload": "menu:clients"}],
        [{"type": "callback", "text": "⚙️ Услуги", "payload": "menu:services"}],
    ]

def back_button(target: str = "menu:main") -> list:
    return [[{"type": "callback", "text": "⬅️ Назад", "payload": target}]]

def cancel_button() -> list:
    return [[{"type": "callback", "text": "❌ Отмена", "payload": "menu:main"}]]

def skip_photo_button() -> list:
    return [[{"type": "callback", "text": "⏭ Пропустить фото", "payload": "skip_photo"}]]

def confirm_keyboard(confirm_payload: str, cancel_payload: str = "menu:main") -> list:
    return [
        [{"type": "callback", "text": "✅ Подтвердить", "payload": confirm_payload}],
        [{"type": "callback", "text": "❌ Отмена", "payload": cancel_payload}],
    ]


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
        # FSM: user_id -> {"state": State, "data": {...}}
        self.user_states: dict[str, dict[str, Any]] = {}

    # ---------- HTTP ----------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self.token}

    def send_message(self, user_id: str, text: str, buttons: list | None = None) -> bool:
        body: dict = {"text": text}
        if buttons:
            body["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]
        log.info("send_message → user_id=%s, text=%.80s", user_id, text)
        resp = self.session.post(
            f"{self.base_url}/messages",
            params={"user_id": user_id},
            headers=self._headers(),
            json=body,
            timeout=15,
        )
        log.info("send_message ← %s %s", resp.status_code, resp.text[:200] if hasattr(resp, 'text') else str(resp))
        resp.raise_for_status()
        return True

    def send_photo(self, user_id: str, photo_data: bytes, caption: str = "",
                   buttons: list | None = None) -> bool:
        """Отправка фото пользователю (через загрузку файла + сообщение с вложением)."""
        # 1. Загружаем файл
        files = {"file": ("photo.jpg", photo_data, "image/jpeg")}
        resp = self.session.post(
            f"{self.base_url}/files",
            headers=self._headers(),
            files=files,
            timeout=30,
        )
        resp.raise_for_status()
        file_data = resp.json()
        file_id = file_data.get("file_id") or file_data.get("id")
        if not file_id:
            log.error("Не получен file_id при загрузке фото: %s", file_data)
            return False

        # 2. Отправляем сообщение с прикреплённым фото
        body: dict = {"text": caption}
        if buttons:
            body["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]
        body["attachments"] = body.get("attachments", []) + [
            {"type": "photo", "payload": {"file_id": file_id}}
        ]
        resp = self.session.post(
            f"{self.base_url}/messages",
            params={"user_id": user_id},
            headers=self._headers(),
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        return True

    def download_photo(self, attachment: dict) -> bytes | None:
        """Скачивает фото из MAX по attachment payload."""
        payload = attachment.get("payload") or {}
        file_id = payload.get("file_id") or payload.get("id")
        if not file_id:
            return None
        resp = self.session.get(
            f"{self.base_url}/files/{file_id}",
            headers=self._headers(),
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.content
        log.warning("Не удалось скачать фото file_id=%s: %s", file_id, resp.status_code)
        return None

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
        updates = data.get("updates") or []
        if updates:
            log.info("get_updates: %d updates, marker=%s", len(updates), self.marker)
        return updates

    # ---------- State Management ----------

    def _get_state(self, user_id: str) -> State:
        return self.user_states.get(user_id, {}).get("state", State.MAIN_MENU)

    def _set_state(self, user_id: str, state: State, data: dict | None = None) -> None:
        if user_id not in self.user_states:
            self.user_states[user_id] = {}
        self.user_states[user_id]["state"] = state
        if data:
            self.user_states[user_id]["data"] = data
        elif "data" not in self.user_states[user_id]:
            self.user_states[user_id]["data"] = {}

    def _get_data(self, user_id: str) -> dict:
        return self.user_states.get(user_id, {}).get("data", {})

    def _update_data(self, user_id: str, **kwargs) -> None:
        if user_id not in self.user_states:
            self.user_states[user_id] = {"data": {}}
        self.user_states[user_id]["data"].update(kwargs)

    def _clear_state(self, user_id: str) -> None:
        self.user_states.pop(user_id, None)

    # ---------- Main Entry Points ----------

    def handle_update(self, update: dict) -> None:
        up_type = update.get("update_type")
        sender = self._sender_id(update)
        log.info("handle_update: type=%s sender=%s", up_type, sender)
        if not sender or not self._is_admin(sender):
            if sender:
                self.send_message(sender, "Доступ запрещён: вы не оператор студии.")
            return

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
        if sender.get("user_id") is not None:
            return str(sender["user_id"])
        callback = update.get("callback") or {}
        cb_user = callback.get("user") or {}
        if cb_user.get("user_id") is not None:
            return str(cb_user["user_id"])
        if update.get("chat_id") is not None:
            return str(update["chat_id"])
        return None

    def _is_admin(self, user_id: str) -> bool:
        return bool(self.admin_ids) and user_id in self.admin_ids

    def _on_start(self, update: dict) -> None:
        user_id = self._sender_id(update)
        if user_id:
            self._clear_state(user_id)
            self._show_main_menu(user_id)

    def _show_main_menu(self, user_id: str) -> None:
        self._set_state(user_id, State.MAIN_MENU)
        self.send_message(user_id, "🏠 <b>Главное меню</b>\nВыберите действие:", main_menu_keyboard())

    # ---------- Message Handler (text + photo) ----------

    def _on_message(self, update: dict) -> None:
        user_id = self._sender_id(update)
        if not user_id:
            return

        state = self._get_state(user_id)
        message = update.get("message") or {}
        body = message.get("body") or {}
        attachments = body.get("attachments") or []
        text = (body.get("text") or "").strip()

        # Handle photo attachments in any state expecting photo
        if attachments and state in (State.AWAITING_PHOTO, State.AWAITING_STATUS_PHOTO):
            for att in attachments:
                if att.get("type") == "photo":
                    photo_data = self.download_photo(att)
                    if photo_data:
                        self._handle_photo(user_id, photo_data)
                    else:
                        self.send_message(user_id, "Не удалось загрузить фото. Попробуйте ещё раз.", cancel_button())
                    return

        # Handle text input based on state
        if state == State.ENTERING_DESCRIPTION:
            self._handle_description(user_id, text)
        elif state == State.MAIN_MENU:
            # Ignore random text in main menu, show help
            self._show_main_menu(user_id)

    def _on_callback(self, update: dict) -> None:
        callback = update.get("callback") or {}
        callback_id = callback.get("callback_id")
        payload = str(callback.get("payload") or "")
        user_id = self._sender_id(update)
        if not user_id or not callback_id:
            return

        log.info("callback: user=%s payload=%s state=%s", user_id, payload, self._get_state(user_id))

        parts = payload.split(":")
        action = parts[0]

        try:
            if action == "menu":
                self._handle_menu_callback(user_id, callback_id, parts[1] if len(parts) > 1 else "main")
            elif action == "client":
                self._handle_client_callback(user_id, callback_id, parts[1] if len(parts) > 1 else "")
            elif action == "service":
                self._handle_service_callback(user_id, callback_id, parts[1] if len(parts) > 1 else "")
            elif action == "status":
                self._handle_status_callback(user_id, callback_id, parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else "")
            elif action == "order":
                self._handle_order_callback(user_id, callback_id, parts[1] if len(parts) > 1 else "")
            elif action == "neworder":
                self._handle_new_order_callback(user_id, callback_id, parts[1] if len(parts) > 1 else "")
            elif action == "skip_photo":
                self._handle_skip_photo(user_id, callback_id)
            elif action == "confirm":
                self._handle_confirm(user_id, callback_id, parts[1] if len(parts) > 1 else "")
            elif action == "cancel":
                self._handle_cancel(user_id, callback_id)
        except Exception as exc:
            log.exception("Ошибка обработки callback: %s", exc)
            self.answer_callback(callback_id, "❌ Произошла ошибка. Попробуйте снова.")

    # ---------- Menu Callbacks ----------

    def _handle_menu_callback(self, user_id: str, callback_id: str, action: str) -> None:
        if action == "main":
            self._clear_state(user_id)
            self._show_main_menu(user_id)
        elif action == "orders":
            self._show_orders_list(user_id, callback_id)
        elif action == "new_order":
            self._start_new_order(user_id, callback_id)
        elif action == "clients":
            self._show_clients_list(user_id, callback_id)
        elif action == "services":
            self._show_services_list(user_id, callback_id)
        else:
            self.answer_callback(callback_id, "Неизвестное действие")

    # ---------- Order Creation Flow ----------

    def _start_new_order(self, user_id: str, callback_id: str) -> None:
        clients = self.db.list_clients()
        if not clients:
            self.answer_callback(callback_id, "Сначала добавьте клиентов через веб-интерфейс.")
            return

        buttons = []
        for c in clients[:20]:
            buttons.append([{"type": "callback", "text": f"{c['full_name']} ({c['phone'] or '—'})", "payload": f"client:{c['id']}"}])
        buttons.append([{"type": "callback", "text": "❌ Отмена", "payload": "menu:main"}])

        self._set_state(user_id, State.CHOOSING_CLIENT, {})
        self.answer_callback(callback_id, "👤 <b>Шаг 1/4: Выберите клиента</b>", buttons)

    def _handle_client_callback(self, user_id: str, callback_id: str, client_id_str: str) -> None:
        if not client_id_str.isdigit():
            self.answer_callback(callback_id, "Неверный клиент")
            return
        client_id = int(client_id_str)
        client = self.db.get_client(client_id)
        if not client:
            self.answer_callback(callback_id, "Клиент не найден")
            return

        self._update_data(user_id, client_id=client_id, client_name=client["full_name"])
        self._show_services_for_order(user_id, callback_id)

    def _show_services_for_order(self, user_id: str, callback_id: str) -> None:
        services = self.db.list_services()
        if not services:
            self.answer_callback(callback_id, "Нет доступных услуг. Добавьте через веб-интерфейс.")
            return

        buttons = []
        for s in services:
            price_str = f" — {s['price']} ₽/{s['unit'] or 'шт'}" if s["price"] else ""
            buttons.append([{"type": "callback", "text": f"{s['name']}{price_str}", "payload": f"service:{s['id']}"}])
        buttons.append([{"type": "callback", "text": "❌ Отмена", "payload": "menu:main"}])

        self._set_state(user_id, State.CHOOSING_SERVICE)
        self.answer_callback(callback_id, "🔧 <b>Шаг 2/4: Выберите основную услугу</b>", buttons)

    def _handle_service_callback(self, user_id: str, callback_id: str, service_id_str: str) -> None:
        if not service_id_str.isdigit():
            self.answer_callback(callback_id, "Неверная услуга")
            return
        service_id = int(service_id_str)
        service = self.db.get_service(service_id)
        if not service:
            self.answer_callback(callback_id, "Услуга не найдена")
            return

        self._update_data(user_id, service_id=service_id, service_name=service["name"])
        self._set_state(user_id, State.ENTERING_DESCRIPTION)
        self.answer_callback(callback_id,
            f"📝 <b>Шаг 3/4: Введите описание заказа</b>\n"
            f"Услуга: {service['name']}\n"
            f"Напишите текст или нажмите «Пропустить»",
            [[{"type": "callback", "text": "⏭ Пропустить", "payload": "skip_desc"}],
             [{"type": "callback", "text": "❌ Отмена", "payload": "menu:main"}]])

    def _handle_description(self, user_id: str, text: str) -> None:
        data = self._get_data(user_id)
        if text.lower() in ("пропустить", "skip", "-"):
            text = ""
        self._update_data(user_id, description=text)
        self._set_state(user_id, State.AWAITING_PHOTO)
        self.send_message(user_id,
            f"📷 <b>Шаг 4/4: Пришлите фото</b>\n"
            f"Это может быть фото поломки, желаемой детали или эскиза.\n"
            f"Можно пропустить.",
            skip_photo_button() + cancel_button())

    def _handle_skip_photo(self, user_id: str, callback_id: str) -> None:
        self._update_data(user_id, photo_data=None)
        self._show_order_confirmation(user_id, callback_id)

    def _handle_photo(self, user_id: str, photo_data: bytes) -> None:
        self._update_data(user_id, photo_data=photo_data)
        self._show_order_confirmation(user_id, None)

    def _show_order_confirmation(self, user_id: str, callback_id: str | None) -> None:
        data = self._get_data(user_id)
        text = (
            f"✅ <b>Подтвердите заказ</b>\n\n"
            f"Клиент: {data.get('client_name', '—')}\n"
            f"Услуга: {data.get('service_name', '—')}\n"
            f"Описание: {data.get('description', '—') or '—'}\n"
            f"Фото: {'✅ есть' if data.get('photo_data') else '❌ нет'}"
        )
        buttons = confirm_keyboard("confirm:create_order", "menu:main")
        if callback_id:
            self.answer_callback(callback_id, text, buttons)
        else:
            self.send_message(user_id, text, buttons)

    def _handle_confirm(self, user_id: str, callback_id: str, action: str) -> None:
        if action == "create_order":
            self._create_order(user_id, callback_id)
        elif action == "change_status":
            self._confirm_status_change(user_id, callback_id)
        else:
            self.answer_callback(callback_id, "Неизвестное подтверждение")

    def _create_order(self, user_id: str, callback_id: str) -> None:
        data = self._get_data(user_id)
        required = ["client_id", "service_id"]
        if not all(k in data for k in required):
            self.answer_callback(callback_id, "❌ Не хватает данных. Начните заново.")
            self._clear_state(user_id)
            return

        order_id = self.db.add_order(
            client_id=data["client_id"],
            service_id=data["service_id"],
            description=data.get("description", ""),
            status_id=1,  # "принят"
        )

        # Save photo if provided
        if data.get("photo_data"):
            self.db.add_order_photo(
                order_id=order_id,
                status_id=1,
                photo_data=data["photo_data"],
                caption=f"Фото при создании заказа: {data.get('description', '')}"
            )

        # Notify client with photo
        self._notify_client_for_order(order_id, "принят", data.get("photo_data"), "Новый заказ создан")

        self._clear_state(user_id)
        self.answer_callback(callback_id,
            f"✅ <b>Заказ #{order_id} создан!</b>\n"
            f"Статус: принят\n"
            f"Клиент уведомлён.",
            [[{"type": "callback", "text": "📋 К заказам", "payload": "menu:orders"}]])

    # ---------- Status Change Flow ----------

    def _show_orders_list(self, user_id: str, callback_id: str | None = None) -> None:
        orders = self.db.list_orders()
        if not orders:
            text = "📋 Заказов пока нет."
            buttons = back_button()
        else:
            text = "📋 <b>Список заказов</b> (последние 20):"
            buttons = []
            for order in orders[:20]:
                status_emoji = self._status_emoji(order["status_name"])
                buttons.append([{"type": "callback",
                    "text": f"{status_emoji} #{order['id']} {order['client_name']} — {order['service_name']} · {order['status_name']}",
                    "payload": f"order:{order['id']}"}])
            buttons.append([{"type": "callback", "text": "⬅️ Назад", "payload": "menu:main"}])

        if callback_id:
            self.answer_callback(callback_id, text, buttons)
        else:
            self.send_message(user_id, text, buttons)

    def _handle_order_callback(self, user_id: str, callback_id: str, order_id_str: str) -> None:
        if not order_id_str.isdigit():
            self.answer_callback(callback_id, "Неверный заказ")
            return
        order = self.db.get_order(int(order_id_str))
        if not order:
            self.answer_callback(callback_id, "Заказ не найден")
            return

        self._show_order_detail(user_id, callback_id, order)

    def _show_order_detail(self, user_id: str, callback_id: str | None, order) -> None:
        photos = self.db.get_order_photos(order["id"])
        photo_info = ""
        if photos:
            latest = photos[-1]
            photo_info = f"\n📷 Последнее фото: {latest['status_name']} ({latest['caption'] or 'без описания'})"

        text = (
            f"📦 <b>Заказ #{order['id']}</b>\n"
            f"Клиент: {order['client_name']}\n"
            f"Услуга: {order['service_name']}\n"
            f"Статус: {self._status_emoji(order['status_name'])} {order['status_name']}\n"
            f"Описание: {order['description'] or '—'}\n"
            f"Цена: {order['price'] or '—'} ₽\n"
            f"Дедлайн: {order['deadline'] or '—'}"
            f"{photo_info}"
        )

        buttons = [
            [{"type": "callback", "text": "🔄 Сменить статус", "payload": f"status:change:{order['id']}"}],
            [{"type": "callback", "text": "📸 История фото", "payload": f"status:photos:{order['id']}"}],
            [{"type": "callback", "text": "➕ Доп. услуги", "payload": f"status:extra:{order['id']}"}],
            [{"type": "callback", "text": "⬅️ Назад", "payload": "menu:orders"}],
        ]

        if callback_id:
            self.answer_callback(callback_id, text, buttons)
        else:
            self.send_message(user_id, text, buttons)

    def _handle_status_callback(self, user_id: str, callback_id: str, action: str, value: str) -> None:
        if action == "change":
            self._start_status_change(user_id, callback_id, int(value))
        elif action == "photos":
            self._show_order_photos(user_id, callback_id, int(value))
        elif action == "extra":
            self._show_extra_services(user_id, callback_id, int(value))
        elif action == "set":
            order_id = int(parts[1]) if len(parts) > 1 else 0
            status_id = int(parts[2]) if len(parts) > 2 else 0
            # This is handled in _handle_new_order_callback for status selection during order creation
            pass

    def _start_status_change(self, user_id: str, callback_id: str, order_id: int) -> None:
        order = self.db.get_order(order_id)
        if not order:
            self.answer_callback(callback_id, "Заказ не найден")
            return

        statuses = self.db.list_statuses()
        buttons = []
        row = []
        for st in statuses:
            if st["id"] == order["status_id"]:
                continue  # Skip current status
            row.append({"type": "callback", "text": st["name"], "payload": f"neworder:status:{order_id}:{st['id']}"})
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([{"type": "callback", "text": "❌ Отмена", "payload": f"order:{order_id}"}])

        self._set_state(user_id, State.CHOOSING_STATUS, {"order_id": order_id})
        self.answer_callback(callback_id, f"🔄 Выберите новый статус для заказа #{order_id}:", buttons)

    def _handle_new_order_callback(self, user_id: str, callback_id: str, action: str) -> None:
        """Handles callbacks with 'neworder:' prefix used in status selection."""
        parts = action.split(":")
        if parts[0] == "status" and len(parts) == 3:
            order_id = int(parts[1])
            status_id = int(parts[2])
            status = self.db.get_status(status_id)
            if not status:
                self.answer_callback(callback_id, "Статус не найден")
                return
            self._update_data(user_id, new_status_id=status_id, new_status_name=status["name"])
            self._set_state(user_id, State.AWAITING_STATUS_PHOTO)
            self.answer_callback(callback_id,
                f"📷 <b>Пришлите фото для статуса «{status['name']}»</b>\n"
                f"Например: 3D-скан, чертеж, фото готового изделия.\n"
                f"Можно пропустить.",
                skip_photo_button() + [[{"type": "callback", "text": "❌ Отмена", "payload": f"order:{order_id}"}]])

    def _handle_skip_photo(self, user_id: str, callback_id: str) -> None:
        state = self._get_state(user_id)
        if state == State.AWAITING_STATUS_PHOTO:
            self._update_data(user_id, status_photo_data=None)
            self._confirm_status_change(user_id, callback_id)
        elif state == State.AWAITING_PHOTO:
            self._update_data(user_id, photo_data=None)
            self._show_order_confirmation(user_id, callback_id)

    def _confirm_status_change(self, user_id: str, callback_id: str) -> None:
        data = self._get_data(user_id)
        order_id = data.get("order_id")
        status_name = data.get("new_status_name", "—")
        has_photo = "✅ есть" if data.get("status_photo_data") else "❌ нет"

        text = (
            f"✅ <b>Подтвердите смену статуса</b>\n\n"
            f"Заказ: #{order_id}\n"
            f"Новый статус: {status_name}\n"
            f"Фото: {has_photo}"
        )
        buttons = confirm_keyboard("confirm:change_status", f"order:{order_id}")
        self.answer_callback(callback_id, text, buttons)

    def _confirm_status_change(self, user_id: str, callback_id: str) -> None:
        data = self._get_data(user_id)
        order_id = data.get("order_id")
        new_status_id = data.get("new_status_id")
        new_status_name = data.get("new_status_name")
        photo_data = data.get("status_photo_data")

        if not order_id or not new_status_id:
            self.answer_callback(callback_id, "❌ Ошибка: данные потеряны")
            self._clear_state(user_id)
            return

        order = self.db.get_order(order_id)
        if not order:
            self.answer_callback(callback_id, "Заказ не найден")
            self._clear_state(user_id)
            return

        # Update status
        self.db.set_order_status(order_id, new_status_id)

        # Save photo if provided
        photo_data = data.get("status_photo_data")
        if photo_data:
            self.db.add_order_photo(
                order_id=order_id,
                status_id=new_status_id,
                photo_data=photo_data,
                caption=f"Фото при смене на «{new_status_name}»"
            )

        # Notify client with photo
        self._notify_client_for_order(order_id, new_status_name, photo_data, f"Статус: {new_status_name}")

        self._clear_state(user_id)
        self.answer_callback(callback_id,
            f"✅ Статус заказа #{order_id} изменён на «{new_status_name}»\n"
            f"Клиент уведомлён.",
            [[{"type": "callback", "text": "📦 К заказу", "payload": f"order:{order_id}"}],
             [{"type": "callback", "text": "📋 К списку", "payload": "menu:orders"}]])

    def _show_order_photos(self, user_id: str, callback_id: str, order_id: int) -> None:
        photos = self.db.get_order_photos(order_id)
        if not photos:
            self.answer_callback(callback_id, "📷 Фото для этого заказа пока нет.", [[{"type": "callback", "text": "⬅️ Назад", "payload": f"order:{order_id}"}]])
            return

        text = f"📸 <b>История фото заказа #{order_id}</b>:\n\n"
        for i, p in enumerate(photos, 1):
            text += f"{i}. <b>{p['status_name']}</b> — {p['caption'] or 'без описания'}\n"

        # Send latest photo
        latest = photos[-1]
        self.send_photo(user_id, latest["photo_data"], caption=f"Заказ #{order_id} — {latest['status_name']}\n{latest['caption'] or ''}",
                        buttons=[[{"type": "callback", "text": "⬅️ Назад", "payload": f"order:{order_id}"}]])

    def _show_extra_services(self, user_id: str, callback_id: str, order_id: int) -> None:
        order = self.db.get_order(order_id)
        if not order:
            self.answer_callback(callback_id, "Заказ не найден")
            return

        extra = self.db.get_order_services(order_id)
        services = self.db.list_services()

        text = f"➕ <b>Доп. услуги заказа #{order_id}</b>\n\n"
        if extra:
            for e in extra:
                price = e["price"] if e["price"] is not None else e["default_price"]
                text += f"• {e['name']} × {e['quantity']} = {price * e['quantity']:.0f} ₽\n"
        else:
            text += "Пока нет доп. услуг.\n"

        buttons = []
        for s in services:
            buttons.append([{"type": "callback", "text": f"+ {s['name']}", "payload": f"extra:add:{order_id}:{s['id']}"}])
        buttons.append([{"type": "callback", "text": "⬅️ Назад", "payload": f"order:{order_id}"}])

        self.answer_callback(callback_id, text, buttons)

    # ---------- Clients & Services Lists ----------

    def _show_clients_list(self, user_id: str, callback_id: str | None = None) -> None:
        clients = self.db.list_clients()
        if not clients:
            text = "👥 Клиентов пока нет."
            buttons = back_button()
        else:
            text = "👥 <b>Клиенты</b> (первые 20):"
            buttons = []
            for c in clients[:20]:
                buttons.append([{"type": "callback", "text": f"#{c['id']} {c['full_name']} ({c['phone'] or '—'})", "payload": f"client:view:{c['id']}"}])
            buttons.append([{"type": "callback", "text": "⬅️ Назад", "payload": "menu:main"}])

        if callback_id:
            self.answer_callback(callback_id, text, buttons)
        else:
            self.send_message(user_id, text, buttons)

    def _show_services_list(self, user_id: str, callback_id: str | None = None) -> None:
        services = self.db.list_services()
        if not services:
            text = "⚙️ Услуг пока нет."
            buttons = back_button()
        else:
            text = "⚙️ <b>Услуги</b>:\nДля редактирования используйте веб-интерфейс."
            buttons = []
            for s in services:
                price_str = f" — {s['price']} ₽/{s['unit'] or 'шт'}" if s["price"] else ""
                buttons.append([{"type": "callback", "text": f"{s['name']}{price_str}", "payload": f"service:view:{s['id']}"}])
            buttons.append([{"type": "callback", "text": "⬅️ Назад", "payload": "menu:main"}])

        if callback_id:
            self.answer_callback(callback_id, text, buttons)
        else:
            self.send_message(user_id, text, buttons)

    # ---------- Notifications ----------

    def _notify_client_for_order(self, order_id: int, status_name: str,
                                  photo_data: bytes | None = None, photo_caption: str = "") -> None:
        order = self.db.get_order(order_id)
        if not order:
            return
        client = self.db.get_client(order["client_id"])
        channel_map = {"telegram": "telegram_id", "vk": "vk_id", "max": "max_id"}
        enabled = {c["channel"] for c in self.db.list_channels(order["client_id"]) if c["enabled"]}
        channels = {ch: client[channel_map[ch]] for ch in enabled if client[channel_map[ch]]}
        send_notifications(
            channels,
            order_status_message(order, status_name),
            build_notifiers(),
            photo_data=photo_data,
            photo_caption=photo_caption,
            photo_mime="image/jpeg"
        )

    def _status_emoji(self, status_name: str) -> str:
        emoji_map = {
            "принят": "📥",
            "в работе": "⚙️",
            "готов": "✅",
            "выдан": "📦",
            "отменён": "❌",
        }
        return emoji_map.get(status_name.lower(), "📌")

    def _handle_cancel(self, user_id: str, callback_id: str) -> None:
        self._clear_state(user_id)
        self.answer_callback(callback_id, "❌ Отменено", main_menu_keyboard())

    # ---------- Run ----------

    def run_forever(self) -> None:
        log.info("MAX-бот запущен (Long Polling)")
        while True:
            try:
                updates = self.get_updates()
                for update in updates:
                    try:
                        self.handle_update(update)
                    except Exception as exc:
                        log.warning("Ошибка обработки события: %s", exc, exc_info=True)
            except Exception as exc:
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
    migrate_db(db_path)
    bot = MaxBot(db_path=db_path)
    if not bot.token:
        log.error("MAX_TOKEN не задан — бот не запущен")
        return
    if not bot.admin_ids:
        log.warning("MAX_ADMIN_ID не задан — команды будут отклоняться")
    bot.run_forever()


if __name__ == "__main__":
    main()