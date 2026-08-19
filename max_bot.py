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
from services.order_service import OrderService, NotificationPayload

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
    CONFIRMING_STATUS_CHANGE = "confirming_status_change"
    # View order
    VIEWING_ORDER = "viewing_order"
    # Client management
    CHOOSING_CLIENT_ACTION = "choosing_client_action"
    ENTERING_CLIENT_NAME = "entering_client_name"
    ENTERING_CLIENT_PHONE = "entering_client_phone"
    ENTERING_CLIENT_TG_ID = "entering_client_tg_id"
    ENTERING_CLIENT_VK_ID = "entering_client_vk_id"
    ENTERING_CLIENT_MAX_ID = "entering_client_max_id"
    ENTERING_CLIENT_NOTES = "entering_client_notes"
    CONFIRMING_CLIENT_CREATE = "confirming_client_create"
    CONFIRMING_CLIENT_UPDATE = "confirming_client_update"
    CONFIRMING_CLIENT_DELETE = "confirming_client_delete"
    # Order edit
    CHOOSING_ORDER_EDIT_FIELD = "choosing_order_edit_field"
    EDITING_ORDER_DESCRIPTION = "editing_order_description"
    EDITING_ORDER_PRICE = "editing_order_price"
    EDITING_ORDER_DEADLINE = "editing_order_deadline"
    CONFIRMING_ORDER_EDIT = "confirming_order_edit"
    CONFIRMING_ORDER_DELETE = "confirming_order_delete"

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


def cancel_button() -> list:
    return [[{"type": "callback", "text": "❌ Отмена", "payload": "menu:main"}]]


class MaxBot:
    def __init__(self, token: str = "", admin_ids: str = "",
                 db_path: str = "instance/orders.db",
                 base_url: str = BASE_URL,
                 poll_timeout: int = 30,
                 session: requests.Session | None = None,
                 order_service: OrderService | None = None):
        self.token = token or os.environ.get("MAX_TOKEN", "")
        self.base_url = base_url
        self.admin_ids = {x.strip() for x in (admin_ids or os.environ.get("MAX_ADMIN_ID", "")).split(",") if x.strip()}
        self.db = Database(db_path)
        self.order_service = order_service or OrderService(self.db)
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
        log.info("send_message ← %s %s", resp.status_code, resp.text[:200])
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
                        self._handle_photo(user_id, photo_data, state)
                    else:
                        self.send_message(user_id, "Не удалось загрузить фото. Попробуйте ещё раз.", cancel_button())
                    return

        # Handle text input based on state
        if state == State.ENTERING_DESCRIPTION:
            self._handle_description(user_id, text)
        elif state == State.ENTERING_CLIENT_NAME:
            self._handle_client_name(user_id, text)
        elif state == State.ENTERING_CLIENT_PHONE:
            self._handle_client_phone(user_id, text)
        elif state == State.ENTERING_CLIENT_TG_ID:
            self._handle_client_tg_id(user_id, text)
        elif state == State.ENTERING_CLIENT_VK_ID:
            self._handle_client_vk_id(user_id, text)
        elif state == State.ENTERING_CLIENT_MAX_ID:
            self._handle_client_max_id(user_id, text)
        elif state == State.ENTERING_CLIENT_NOTES:
            self._handle_client_notes(user_id, text)
        elif state == State.EDITING_ORDER_DESCRIPTION:
            self._handle_edit_description(user_id, text)
        elif state == State.EDITING_ORDER_PRICE:
            self._handle_edit_price(user_id, text)
        elif state == State.EDITING_ORDER_DEADLINE:
            self._handle_edit_deadline(user_id, text)
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
                self._handle_new_order_callback(user_id, callback_id, parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else "")
            elif action == "skip_photo":
                self._handle_skip_photo(user_id, callback_id)
            elif action == "confirm":
                self._handle_confirm(user_id, callback_id, parts[1] if len(parts) > 1 else "")
            elif action == "client_ch":
                self._handle_client_channel_toggle(user_id, callback_id, parts[1] if len(parts) > 1 else "")
            elif action == "client_ch_edit":
                self._handle_client_channel_edit_toggle(user_id, callback_id, parts[1] if len(parts) > 1 else "")
            elif action == "edit_field":
                self._handle_edit_field(user_id, callback_id, parts[1] if len(parts) > 1 else "")
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

    def _handle_client_callback(self, user_id: str, callback_id: str, sub_action: str) -> None:
        if sub_action == "create":
            self._start_create_client(user_id, callback_id)
        elif sub_action == "view":
            # This shouldn't happen directly, view requires client_id
            self._show_clients_list(user_id, callback_id)
        elif sub_action == "edit" or sub_action == "delete":
            # These need client_id, handled by _on_callback with full payload
            pass
        else:
            # Legacy: selecting client for new order
            if sub_action.isdigit():
                client_id = int(sub_action)
                client = self.db.get_client(client_id)
                if not client:
                    self.answer_callback(callback_id, "Клиент не найден")
                    return
                self._update_data(user_id, client_id=client_id, client_name=client["full_name"])
                self._show_services_for_order(user_id, callback_id)
            else:
                self.answer_callback(callback_id, "Неизвестное действие с клиентом")

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
        state = self._get_state(user_id)
        if state == State.AWAITING_STATUS_PHOTO:
            self._update_data(user_id, status_photo_data=None)
            self._show_status_change_confirmation(user_id, callback_id)
        elif state == State.AWAITING_PHOTO:
            self._update_data(user_id, photo_data=None)
            self._show_order_confirmation(user_id, callback_id)

    def _handle_photo(self, user_id: str, photo_data: bytes, state: State) -> None:
        if state == State.AWAITING_STATUS_PHOTO:
            self._update_data(user_id, status_photo_data=photo_data)
            self._show_status_change_confirmation(user_id, None)
        else:  # AWAITING_PHOTO
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

    def _show_status_change_confirmation(self, user_id: str, callback_id: str | None) -> None:
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
        if callback_id:
            self.answer_callback(callback_id, text, buttons)
        else:
            self.send_message(user_id, text, buttons)

    def _handle_confirm(self, user_id: str, callback_id: str, action: str) -> None:
        if action == "create_order":
            self._create_order(user_id, callback_id)
        elif action == "change_status":
            self._execute_status_change(user_id, callback_id)
        else:
            self.answer_callback(callback_id, "Неизвестное подтверждение")

    def _create_order(self, user_id: str, callback_id: str) -> None:
        data = self._get_data(user_id)
        required = ["client_id", "service_id"]
        if not all(k in data for k in required):
            self.answer_callback(callback_id, "❌ Не хватает данных. Начните заново.")
            self._clear_state(user_id)
            return

        order_id = self.order_service.create_order(
            client_id=data["client_id"],
            service_id=data["service_id"],
            description=data.get("description", ""),
            status_id=1,  # "принят"
            photo_data=data.get("photo_data"),
            photo_caption=f"Фото при создании заказа: {data.get('description', '')}",
            photo_mime="image/jpeg"
        )

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

    def _handle_order_callback(self, user_id: str, callback_id: str, sub_action: str) -> None:
        parts = sub_action.split(":")
        action = parts[0] if parts else ""
        
        if action == "edit" and len(parts) > 1:
            self._start_edit_order(user_id, callback_id, int(parts[1]))
        elif action == "delete" and len(parts) > 1:
            self._confirm_delete_order(user_id, callback_id, int(parts[1]))
        elif action.isdigit():
            # Legacy: view order by ID
            order = self.db.get_order(int(action))
            if not order:
                self.answer_callback(callback_id, "Заказ не найден")
                return
            self._show_order_detail(user_id, callback_id, order)
        else:
            self.answer_callback(callback_id, "Неизвестное действие с заказом")

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
            [{"type": "callback", "text": "✏️ Редактировать", "payload": f"order:edit:{order['id']}"}],
            [{"type": "callback", "text": "🗑 Удалить", "payload": f"order:delete:{order['id']}"}],
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
        # "set" action removed - was dead code referencing undefined 'parts'

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

    def _handle_new_order_callback(self, user_id: str, callback_id: str, action: str, value: str) -> None:
        """Handles callbacks with 'neworder:' prefix used in status selection."""
        if action != "status" or not value.isdigit():
            return
        parts = value.split(":")
        if len(parts) != 2:
            return
        order_id = int(parts[0])
        status_id = int(parts[1])
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

    def _execute_status_change(self, user_id: str, callback_id: str) -> None:
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

        success = self.order_service.change_status(
            order_id=order_id,
            new_status_id=new_status_id,
            photo_data=photo_data,
            photo_caption=f"Фото при смене на «{new_status_name}»",
            photo_mime="image/jpeg"
        )

        if not success:
            self.answer_callback(callback_id, "❌ Ошибка при смене статуса")
            self._clear_state(user_id)
            return

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
            buttons = [[{"type": "callback", "text": "➕ Создать клиента", "payload": "client:create"}],
                       [{"type": "callback", "text": "⬅️ Назад", "payload": "menu:main"}]]
        else:
            text = "👥 <b>Клиенты</b> (первые 20):"
            buttons = [[{"type": "callback", "text": "➕ Создать клиента", "payload": "client:create"}]]
            for c in clients[:20]:
                buttons.append([{"type": "callback", "text": f"👁 {c['full_name']} ({c['phone'] or '—'})", "payload": f"client:view:{c['id']}"}])
                if not self.db.has_orders_for_client(c['id']):
                    buttons.append([{"type": "callback", "text": "🗑 Удалить", "payload": f"client:delete:{c['id']}"}])
                else:
                    buttons.append([{"type": "callback", "text": "✏️ Редактировать", "payload": f"client:edit:{c['id']}"}])
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

    # ---------- Client Management ----------

    def _start_create_client(self, user_id: str, callback_id: str) -> None:
        self._set_state(user_id, State.ENTERING_CLIENT_NAME, {})
        self.answer_callback(callback_id, "👤 <b>Создание клиента</b>\nВведите ФИО (обязательно):", cancel_button())

    def _handle_client_name(self, user_id: str, text: str) -> None:
        text = text.strip()
        if not text:
            self.send_message(user_id, "ФИО не может быть пустым. Введите снова:", cancel_button())
            return
        self._update_data(user_id, full_name=text)
        self._set_state(user_id, State.ENTERING_CLIENT_PHONE)
        self.send_message(user_id, "📞 Телефон (или «пропустить»):", cancel_button())

    def _handle_client_phone(self, user_id: str, text: str) -> None:
        self._update_data(user_id, phone="" if text.lower() in ("пропустить", "skip", "-") else text.strip())
        self._set_state(user_id, State.ENTERING_CLIENT_TG_ID)
        self.send_message(user_id, "🤖 Telegram ID (или «пропустить»):", cancel_button())

    def _handle_client_tg_id(self, user_id: str, text: str) -> None:
        self._update_data(user_id, telegram_id="" if text.lower() in ("пропустить", "skip", "-") else text.strip())
        self._set_state(user_id, State.ENTERING_CLIENT_VK_ID)
        self.send_message(user_id, "🔵 VK ID (или «пропустить»):", cancel_button())

    def _handle_client_vk_id(self, user_id: str, text: str) -> None:
        self._update_data(user_id, vk_id="" if text.lower() in ("пропустить", "skip", "-") else text.strip())
        self._set_state(user_id, State.ENTERING_CLIENT_MAX_ID)
        self.send_message(user_id, "🟣 MAX ID (или «пропустить»):", cancel_button())

    def _handle_client_max_id(self, user_id: str, text: str) -> None:
        self._update_data(user_id, max_id="" if text.lower() in ("пропустить", "skip", "-") else text.strip())
        self._set_state(user_id, State.ENTERING_CLIENT_NOTES)
        self.send_message(user_id, "📝 Заметки (или «пропустить»):", cancel_button())

    def _handle_client_notes(self, user_id: str, text: str) -> None:
        self._update_data(user_id, notes="" if text.lower() in ("пропустить", "skip", "-") else text.strip())
        self._set_state(user_id, State.CONFIRMING_CLIENT_CREATE)
        self._show_client_confirmation(user_id, None)

    def _show_client_confirmation(self, user_id: str, callback_id: str | None) -> None:
        data = self._get_data(user_id)
        ch_tg = "✅" if data.get("ch_telegram") else "☐"
        ch_vk = "✅" if data.get("ch_vk") else "☐"
        ch_max = "✅" if data.get("ch_max") else "☐"
        text = (
            f"✅ <b>Подтвердите создание клиента</b>\n\n"
            f"ФИО: {data.get('full_name', '—')}\n"
            f"Телефон: {data.get('phone', '—') or '—'}\n"
            f"Telegram ID: {data.get('telegram_id', '—') or '—'}\n"
            f"VK ID: {data.get('vk_id', '—') or '—'}\n"
            f"MAX ID: {data.get('max_id', '—') or '—'}\n"
            f"Заметки: {data.get('notes', '—') or '—'}\n\n"
            f"Каналы уведомлений:\n"
            f"  {ch_tg} Telegram\n"
            f"  {ch_vk} VK\n"
            f"  {ch_max} MAX"
        )
        buttons = [
            [{"type": "callback", "text": f"{ch_tg} Telegram", "payload": "client_ch:telegram"},
             {"type": "callback", "text": f"{ch_vk} VK", "payload": "client_ch:vk"},
             {"type": "callback", "text": f"{ch_max} MAX", "payload": "client_ch:max"}],
            [{"type": "callback", "text": "✅ Подтвердить", "payload": "confirm:create_client"},
             {"type": "callback", "text": "❌ Отмена", "payload": "menu:main"}],
        ]
        if callback_id:
            self.answer_callback(callback_id, text, buttons)
        else:
            self.send_message(user_id, text, buttons)

    def _handle_client_channel_toggle(self, user_id: str, callback_id: str, channel: str) -> None:
        data = self._get_data(user_id)
        key = f"ch_{channel}"
        data[key] = not data.get(key, False)
        self._update_data(user_id, **{key: data[key]})
        self._show_client_confirmation(user_id, callback_id)

    def _confirm_create_client(self, user_id: str, callback_id: str) -> None:
        data = self._get_data(user_id)
        if "full_name" not in data:
            self.answer_callback(callback_id, "❌ Не хватает данных")
            return
        client_id = self.db.add_client(
            full_name=data["full_name"],
            phone=data.get("phone", ""),
            telegram_id=data.get("telegram_id", ""),
            vk_id=data.get("vk_id", ""),
            max_id=data.get("max_id", ""),
            notes=data.get("notes", ""),
        )
        for ch in ("telegram", "vk", "max"):
            if data.get(f"ch_{ch}"):
                self.db.set_channel(client_id, ch, True)
        self._clear_state(user_id)
        self.answer_callback(callback_id,
            f"✅ <b>Клиент #{client_id} создан!</b>\nФИО: {data['full_name']}",
            [[{"type": "callback", "text": "👥 К клиентам", "payload": "menu:clients"}]])

    def _show_client_detail(self, user_id: str, callback_id: str, client_id: int) -> None:
        client = self.db.get_client(client_id)
        if not client:
            self.answer_callback(callback_id, "Клиент не найден", back_button("menu:clients"))
            return
        text = (
            f"👤 <b>Клиент #{client_id}</b>\n"
            f"ФИО: {client['full_name']}\n"
            f"Телефон: {client['phone'] or '—'}\n"
            f"TG ID: {client['telegram_id'] or '—'}\n"
            f"VK ID: {client['vk_id'] or '—'}\n"
            f"MAX ID: {client['max_id'] or '—'}\n"
            f"Заметки: {client['notes'] or '—'}"
        )
        buttons = [
            [{"type": "callback", "text": "✏️ Редактировать", "payload": f"client:edit:{client_id}"}],
            [{"type": "callback", "text": "🗑 Удалить", "payload": f"client:delete:{client_id}"}],
            [{"type": "callback", "text": "⬅️ Назад", "payload": "menu:clients"}],
        ]
        self.answer_callback(callback_id, text, buttons)

    def _start_edit_client(self, user_id: str, callback_id: str, client_id: int) -> None:
        client = self.db.get_client(client_id)
        if not client:
            self.answer_callback(callback_id, "Клиент не найден")
            return
        self._update_data(user_id, client_id=client_id)
        self._set_state(user_id, State.CHOOSING_ORDER_EDIT_FIELD)
        buttons = [
            [{"type": "callback", "text": "ФИО", "payload": "edit_field:full_name"},
             {"type": "callback", "text": "Телефон", "payload": "edit_field:phone"},
             {"type": "callback", "text": "TG ID", "payload": "edit_field:telegram_id"}],
            [{"type": "callback", "text": "VK ID", "payload": "edit_field:vk_id"},
             {"type": "callback", "text": "MAX ID", "payload": "edit_field:max_id"},
             {"type": "callback", "text": "Заметки", "payload": "edit_field:notes"}],
            [{"type": "callback", "text": "Каналы уведомлений", "payload": "edit_field:channels"},
             {"type": "callback", "text": "❌ Отмена", "payload": f"client:view:{client_id}"}],
        ]
        self.answer_callback(callback_id, "✏️ Что редактировать?", buttons)

    def _show_client_channels_edit(self, user_id: str, callback_id: str) -> None:
        data = self._get_data(user_id)
        client_id = data.get("client_id")
        client = self.db.get_client(client_id) if client_id else None
        ch_tg = "✅" if client and client.get("notify_telegram") else "☐"
        ch_vk = "✅" if client and client.get("notify_vk") else "☐"
        ch_max = "✅" if client and client.get("notify_max") else "☐"
        text = "🔔 <b>Каналы уведомлений клиента</b>\nНажмите для переключения:"
        buttons = [
            [{"type": "callback", "text": f"{ch_tg} Telegram", "payload": "client_ch_edit:telegram"},
             {"type": "callback", "text": f"{ch_vk} VK", "payload": "client_ch_edit:vk"},
             {"type": "callback", "text": f"{ch_max} MAX", "payload": "client_ch_edit:max"}],
            [{"type": "callback", "text": "⬅️ Назад", "payload": f"client:edit:{client_id}"}],
        ]
        self.answer_callback(callback_id, text, buttons)

    def _handle_client_channel_edit_toggle(self, user_id: str, callback_id: str, channel: str) -> None:
        data = self._get_data(user_id)
        client_id = data.get("client_id")
        if not client_id:
            self.answer_callback(callback_id, "Ошибка: клиент не найден")
            return
        client = self.db.get_client(client_id)
        if not client:
            self.answer_callback(callback_id, "Клиент не найден")
            return
        
        current = client.get(f"notify_{channel}", False)
        new_value = not current
        self.db.set_channel(client_id, channel, new_value)
        
        # Refresh the view
        self._show_client_channels_edit(user_id, callback_id)

    def _confirm_delete_client(self, user_id: str, callback_id: str, client_id: int) -> None:
        if self.db.has_orders_for_client(client_id):
            self.answer_callback(callback_id, "❌ Нельзя удалить: у клиента есть заказы",
                                 [[{"type": "callback", "text": "⬅️ Назад", "payload": f"client:view:{client_id}"}]])
            return
        self._update_data(user_id, client_id=client_id)
        self._set_state(user_id, State.CONFIRMING_CLIENT_DELETE)
        self.answer_callback(callback_id,
            f"❗ Удалить клиента #{client_id}?",
            confirm_keyboard("confirm:delete_client", f"client:view:{client_id}"))

    def _execute_delete_client(self, user_id: str, callback_id: str) -> None:
        data = self._get_data(user_id)
        client_id = data.get("client_id")
        if client_id:
            self.db.delete_client(client_id)
        self._clear_state(user_id)
        self.answer_callback(callback_id, "✅ Клиент удалён",
                             [[{"type": "callback", "text": "👥 К клиентам", "payload": "menu:clients"}]])

    # ---------- Order Edit & Delete ----------

    def _start_edit_order(self, user_id: str, callback_id: str, order_id: int) -> None:
        order = self.db.get_order(order_id)
        if not order:
            self.answer_callback(callback_id, "Заказ не найден")
            return
        self._update_data(user_id, order_id=order_id)
        self._set_state(user_id, State.CHOOSING_ORDER_EDIT_FIELD)
        buttons = [
            [{"type": "callback", "text": "Описание", "payload": "edit_field:description"},
             {"type": "callback", "text": "Цена", "payload": "edit_field:price"},
             {"type": "callback", "text": "Дедлайн", "payload": "edit_field:deadline"}],
            [{"type": "callback", "text": "❌ Отмена", "payload": f"order:{order_id}"}],
        ]
        self.answer_callback(callback_id, f"✏️ Что редактировать в заказе #{order_id}?", buttons)

    def _handle_edit_description(self, user_id: str, text: str) -> None:
        self._update_data(user_id, edit_value=text.strip())
        self._set_state(user_id, State.CONFIRMING_ORDER_EDIT)
        self.send_message(user_id,
            f"Изменить описание на: {text.strip()}?",
            confirm_keyboard("confirm:edit_order", "menu:orders"))

    def _handle_edit_price(self, user_id: str, text: str) -> None:
        try:
            price = float(text.strip()) if text.strip() else None
        except ValueError:
            self.send_message(user_id, "Некорректная цена. Введите число:", cancel_button())
            return
        self._update_data(user_id, edit_value=price)
        self._set_state(user_id, State.CONFIRMING_ORDER_EDIT)
        self.send_message(user_id,
            f"Изменить цену на: {price}?",
            confirm_keyboard("confirm:edit_order", "menu:orders"))

    def _handle_edit_deadline(self, user_id: str, text: str) -> None:
        self._update_data(user_id, edit_value=text.strip())
        self._set_state(user_id, State.CONFIRMING_ORDER_EDIT)
        self.send_message(user_id,
            f"Изменить дедлайн на: {text.strip()}?",
            confirm_keyboard("confirm:edit_order", "menu:orders"))

    def _confirm_edit_order(self, user_id: str, callback_id: str) -> None:
        data = self._get_data(user_id)
        order_id = data.get("order_id")
        field = data.get("edit_field")
        value = data.get("edit_value")
        if order_id and field and value is not None:
            self.db.update_order(order_id, **{field: value})
        self._clear_state(user_id)
        self.answer_callback(callback_id, "✅ Заказ обновлён",
                             [[{"type": "callback", "text": "📦 К заказу", "payload": f"order:{order_id}"}]])

    def _confirm_delete_order(self, user_id: str, callback_id: str, order_id: int) -> None:
        self._update_data(user_id, order_id=order_id)
        self._set_state(user_id, State.CONFIRMING_ORDER_DELETE)
        self.answer_callback(callback_id,
            f"❗ Удалить заказ #{order_id}?",
            confirm_keyboard("confirm:delete_order", f"order:{order_id}"))

    def _execute_delete_order(self, user_id: str, callback_id: str) -> None:
        data = self._get_data(user_id)
        order_id = data.get("order_id")
        if order_id:
            self.db.delete_order(order_id)
        self._clear_state(user_id)
        self.answer_callback(callback_id, "✅ Заказ удалён",
                             [[{"type": "callback", "text": "📋 К заказам", "payload": "menu:orders"}]])

    # ---------- Handle Confirm & Edit Field ----------

    def _handle_confirm(self, user_id: str, callback_id: str, action: str) -> None:
        if action == "create_order":
            self._create_order(user_id, callback_id)
        elif action == "change_status":
            self._execute_status_change(user_id, callback_id)
        elif action == "create_client":
            self._confirm_create_client(user_id, callback_id)
        elif action == "delete_client":
            self._execute_delete_client(user_id, callback_id)
        elif action == "edit_order":
            self._confirm_edit_order(user_id, callback_id)
        elif action == "delete_order":
            self._execute_delete_order(user_id, callback_id)
        else:
            self.answer_callback(callback_id, "Неизвестное подтверждение")

    def _handle_edit_field(self, user_id: str, callback_id: str, field: str) -> None:
        data = self._get_data(user_id)
        client_id = data.get("client_id")
        order_id = data.get("order_id")

        if field in ("full_name", "phone", "telegram_id", "vk_id", "max_id", "notes"):
            self._update_data(user_id, edit_field=field)
            state_map = {
                "full_name": State.ENTERING_CLIENT_NAME,
                "phone": State.ENTERING_CLIENT_PHONE,
                "telegram_id": State.ENTERING_CLIENT_TG_ID,
                "vk_id": State.ENTERING_CLIENT_VK_ID,
                "max_id": State.ENTERING_CLIENT_MAX_ID,
                "notes": State.ENTERING_CLIENT_NOTES,
            }
            self._set_state(user_id, state_map[field])
            self.answer_callback(callback_id, f"Введите новое значение для {field} (или «пропустить»):", cancel_button())
        elif field == "channels":
            self._show_client_channels_edit(user_id, callback_id)
        elif field in ("description", "price", "deadline"):
            self._update_data(user_id, edit_field=field)
            state_map = {
                "description": State.EDITING_ORDER_DESCRIPTION,
                "price": State.EDITING_ORDER_PRICE,
                "deadline": State.EDITING_ORDER_DEADLINE,
            }
            self._set_state(user_id, state_map[field])
            self.answer_callback(callback_id, f"Введите новое значение для {field}:", cancel_button())
        else:
            self.answer_callback(callback_id, "Неизвестное поле")

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