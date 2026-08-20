"""Бот MAX для оператора: Long Polling + FSM + inline-кнопки.

ВАЖНО: это бот ОПЕРАТОРА (max_bot.py, класс MaxBot). Уведомления
КЛИЕНТАМ через MAX — это notifier.MaxNotifier. Не путать!

Работает через Long Polling (GET /updates), без вебхуков/HTTPS.
FSM хранится в dict: user_id -> {"state": State, "data": {...}}.
Callback data формат: action:sub_action:param1:param2.

Формат апдейта от MAX API (адаптируется под реальный ответ):
{
    "update_id": 123,
    "user_id": "6880711",
    "text": "привет",              # для текстовых сообщений
    "photo": {"data": b"..."},     # для фото (data или url)
    "callback_data": "menu:main",  # для нажатий на кнопки
    "callback_id": "abc",          # id колбэка для подтверждения
}
"""

import os
import time
from enum import Enum, auto

import requests

from database import Database
from services.order_service import OrderService


def _float(value, default=None):
    """Безопасное приведение к float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class State(Enum):
    """Состояния FSM бота."""

    MAIN_MENU = auto()
    # Создание заказа
    CHOOSING_CLIENT = auto()
    CHOOSING_SERVICE = auto()
    ENTERING_DESCRIPTION = auto()
    AWAITING_PHOTO = auto()
    CONFIRMING_ORDER = auto()
    # Смена статуса
    CHOOSING_STATUS = auto()
    AWAITING_STATUS_PHOTO = auto()
    CONFIRMING_STATUS_CHANGE = auto()
    # Просмотр
    VIEWING_ORDER = auto()
    # Клиенты
    CHOOSING_CLIENT_ACTION = auto()
    ENTERING_CLIENT_NAME = auto()
    ENTERING_CLIENT_PHONE = auto()
    ENTERING_CLIENT_TG_ID = auto()
    ENTERING_CLIENT_VK_ID = auto()
    ENTERING_CLIENT_MAX_ID = auto()
    ENTERING_CLIENT_NOTES = auto()
    CONFIRMING_CLIENT_CREATE = auto()
    CONFIRMING_CLIENT_UPDATE = auto()
    CONFIRMING_CLIENT_DELETE = auto()
    # Редактирование заказа
    CHOOSING_ORDER_EDIT_FIELD = auto()
    EDITING_ORDER_DESCRIPTION = auto()
    EDITING_ORDER_PRICE = auto()
    EDITING_ORDER_DEADLINE = auto()
    CONFIRMING_ORDER_EDIT = auto()
    CONFIRMING_ORDER_DELETE = auto()
    # Услуги
    ENTERING_SERVICE_NAME = auto()
    ENTERING_SERVICE_PRICE = auto()
    CONFIRMING_SERVICE_CREATE = auto()
    CONFIRMING_SERVICE_DELETE = auto()


class MaxBot:
    """Long Polling бот оператора в мессенджере MAX."""

    def __init__(self, token: str, endpoint: str, admin_ids,
                 db: Database, service: OrderService) -> None:
        self.token = token
        self.endpoint = endpoint.rstrip("/")
        self.admin_ids = {str(x) for x in admin_ids}
        self.db = db
        self.service = service
        self.user_states: dict[str, dict] = {}
        self.last_update_id = 0
        self.session = requests.Session()

    # ------------------------------------------------------------------
    # HTTP-обёртки (легко мокаются в тестах)
    # ------------------------------------------------------------------
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def get_updates(self, timeout: int = 30) -> list:
        """Long Polling: GET /updates."""
        resp = self.session.get(
            f"{self.endpoint}/updates",
            params={"timeout": timeout, "offset": self.last_update_id},
            headers=self._headers(),
            timeout=timeout + 10,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("updates", data.get("result", []))

    def send_message(self, user_id, text: str, buttons=None) -> dict:
        """Отправляет сообщение с опциональной inline-клавиатурой."""
        payload = {"recipient_id": str(user_id), "text": text}
        if buttons:
            payload["inline_keyboard"] = [
                [{"text": label, "callback_data": data} for label, data in row]
                for row in buttons
            ]
        resp = self.session.post(
            f"{self.endpoint}/messages",
            json=payload,
            headers=self._headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def send_photo(self, user_id, photo_data: bytes, caption: str = "",
                   buttons=None) -> dict:
        """Загружает файл и отправляет сообщение с фото."""
        resp = self.session.post(
            f"{self.endpoint}/files",
            files={"file": ("photo.jpg", photo_data, "image/jpeg")},
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        file_id = data.get("file_id") or data.get("id")
        if not file_id:
            raise RuntimeError("MAX API: не получен file_id")
        payload = {"recipient_id": str(user_id), "file_id": file_id, "caption": caption}
        if buttons:
            payload["inline_keyboard"] = [
                [{"text": label, "callback_data": data} for label, data in row]
                for row in buttons
            ]
        resp = self.session.post(
            f"{self.endpoint}/messages",
            json=payload,
            headers=self._headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def answer_callback(self, callback_id, text: str = "") -> None:
        """Подтверждает получение колбэка (ошибки игнорируем)."""
        try:
            self.session.post(
                f"{self.endpoint}/callbacks/answer",
                json={"callback_id": callback_id, "text": text},
                headers=self._headers(),
                timeout=10,
            )
        except requests.RequestException:
            pass

    # ------------------------------------------------------------------
    # Авторизация
    # ------------------------------------------------------------------
    def is_admin(self, user_id) -> bool:
        return str(user_id) in self.admin_ids

    # ------------------------------------------------------------------
    # Диспетчеризация апдейтов
    # ------------------------------------------------------------------
    def handle_update(self, update: dict) -> None:
        """Обрабатывает один апдейт от MAX API."""
        user_id = update.get("user_id") or update.get("sender_id")
        if not user_id:
            return
        user_id = str(user_id)
        update_id = update.get("update_id")
        if update_id:
            self.last_update_id = max(self.last_update_id, int(update_id))
        if not self.is_admin(user_id):
            self.send_message(user_id, "Доступ запрещён")
            return
        try:
            if update.get("callback_data") is not None:
                self.handle_callback(user_id, update["callback_data"],
                                     update.get("callback_id"))
            else:
                self.handle_message(user_id, update)
        except Exception as exc:  # noqa: BLE001 — одна ошибка не роняет цикл
            try:
                self.send_message(user_id, f"Ошибка: {exc}")
            except Exception:
                pass

    def handle_message(self, user_id: str, update: dict) -> None:
        """Обрабатывает текстовое сообщение или фото в контексте FSM."""
        text = update.get("text") or ""
        photo = update.get("photo")
        state = self.user_states.get(user_id, {}).get("state")

        if photo:
            photo_data = self._extract_photo_data(photo)
            if state == State.AWAITING_PHOTO:
                self._msg_photo(user_id, photo_data, caption=text)
            elif state == State.AWAITING_STATUS_PHOTO:
                self._msg_status_photo(user_id, photo_data, caption=text)
            else:
                self.send_message(user_id, "Фото не ожидается. Используйте меню.")
            return

        if state == State.ENTERING_DESCRIPTION:
            self._msg_description(user_id, text)
        elif state in (State.ENTERING_CLIENT_NAME, State.ENTERING_CLIENT_PHONE,
                       State.ENTERING_CLIENT_TG_ID, State.ENTERING_CLIENT_VK_ID,
                       State.ENTERING_CLIENT_MAX_ID, State.ENTERING_CLIENT_NOTES):
            self._msg_client_field(user_id, text)
        elif state in (State.EDITING_ORDER_DESCRIPTION, State.EDITING_ORDER_PRICE,
                       State.EDITING_ORDER_DEADLINE):
            self._msg_order_edit(user_id, text)
        elif state in (State.ENTERING_SERVICE_NAME, State.ENTERING_SERVICE_PRICE):
            self._msg_service_field(user_id, text)
        else:
            self._cmd_main_menu(user_id)

    def handle_callback(self, user_id: str, callback_data: str,
                        callback_id=None) -> None:
        """Разбирает callback_data вида action:sub:param1:param2."""
        if callback_id:
            self.answer_callback(callback_id)
        parts = callback_data.split(":")
        action = parts[0]
        args = parts[1:]
        if action == "menu":
            self._cb_menu(user_id, args)
        elif action == "client":
            self._cb_client(user_id, args)
        elif action == "service":
            self._cb_service(user_id, args)
        elif action == "order":
            self._cb_order(user_id, args)
        elif action == "status":
            self._cb_status(user_id, args)
        elif action == "neworder":
            self._cb_neworder(user_id, args)
        elif action == "extra":
            self._cb_extra(user_id, args)
        elif action == "confirm":
            self._cb_confirm(user_id, args)
        elif action == "edit_field":
            self._cb_edit_field(user_id, args)
        elif action == "client_ch":
            self._cb_client_ch(user_id, args)
        elif action == "client_ch_edit":
            self._cb_client_ch_edit(user_id, args)
        elif action == "skip_photo":
            self._cb_skip_photo(user_id)
        elif action == "skip_desc":
            self._cb_skip_desc(user_id)
        elif action == "skip_contact":
            self._cb_skip_contact(user_id)
        else:
            self.send_message(user_id, "Неизвестная команда")

    def _extract_photo_data(self, photo: dict) -> bytes | None:
        """Достаёт байты фото из апдейта (data или url)."""
        if not photo:
            return None
        data = photo.get("data") or photo.get("file_data")
        if data:
            return data
        url = photo.get("url") or photo.get("file_url")
        if url:
            resp = self.session.get(url, headers=self._headers(), timeout=30)
            resp.raise_for_status()
            return resp.content
        return None

    # ------------------------------------------------------------------
    # Меню
    # ------------------------------------------------------------------
    def _cmd_main_menu(self, user_id: str) -> None:
        self.user_states[user_id] = {"state": State.MAIN_MENU, "data": {}}
        self.send_message(user_id, "Главное меню", buttons=[
            [("📋 Заказы", "menu:orders")],
            [("➕ Новый заказ", "menu:new_order")],
            [("👥 Клиенты", "menu:clients")],
            [("🛠 Услуги", "menu:services")],
        ])

    def _cmd_orders(self, user_id: str) -> None:
        orders = self.service.list_orders()
        if not orders:
            self.send_message(user_id, "Заказов нет",
                              buttons=[[("← Меню", "menu:main")]])
            return
        buttons = [
            [(f"#{o['id']} {o['client_name']} — {o['status_name']}", f"order:{o['id']}")]
            for o in orders[:10]
        ]
        buttons.append([("← Меню", "menu:main")])
        self.send_message(user_id, "Заказы:", buttons=buttons)

    def _cmd_new_order(self, user_id: str) -> None:
        clients = self.db.list_clients()
        if not clients:
            self.send_message(user_id, "Нет клиентов. Сначала создайте клиента.",
                              buttons=[[("Создать клиента", "client:create")],
                                       [("← Меню", "menu:main")]])
            return
        self.user_states[user_id] = {"state": State.CHOOSING_CLIENT, "data": {}}
        buttons = [[(c["full_name"], f"client:{c['id']}")] for c in clients]
        buttons.append([("← Меню", "menu:main")])
        self.send_message(user_id, "Выберите клиента:", buttons=buttons)

    def _cmd_clients(self, user_id: str) -> None:
        clients = self.db.list_clients()
        self.user_states[user_id] = {"state": State.CHOOSING_CLIENT_ACTION, "data": {}}
        if not clients:
            self.send_message(user_id, "Клиентов нет",
                              buttons=[[("➕ Создать клиента", "client:create")],
                                       [("← Меню", "menu:main")]])
            return
        buttons = [[(c["full_name"], f"client:view:{c['id']}")] for c in clients]
        buttons.append([("➕ Создать клиента", "client:create")])
        buttons.append([("← Меню", "menu:main")])
        self.send_message(user_id, "Клиенты:", buttons=buttons)

    def _cmd_services(self, user_id: str) -> None:
        services = self.db.list_services()
        if not services:
            self.send_message(user_id, "Услуг нет",
                              buttons=[[("➕ Создать услугу", "service:create")],
                                       [("← Меню", "menu:main")]])
            return
        buttons = [
            [(f"#{s['id']} {s['name']} — {s['price'] or 0} ₽", f"service:view:{s['id']}")]
            for s in services
        ]
        buttons.append([("➕ Создать услугу", "service:create")])
        buttons.append([("← Меню", "menu:main")])
        self.send_message(user_id, "Услуги:", buttons=buttons)

    # ------------------------------------------------------------------
    # Колбэки: menu
    # ------------------------------------------------------------------
    def _cb_menu(self, user_id: str, args: list) -> None:
        sub = args[0] if args else "main"
        if sub == "main":
            self._cmd_main_menu(user_id)
        elif sub == "orders":
            self._cmd_orders(user_id)
        elif sub == "new_order":
            self._cmd_new_order(user_id)
        elif sub == "clients":
            self._cmd_clients(user_id)
        elif sub == "services":
            self._cmd_services(user_id)

    # ------------------------------------------------------------------
    # Колбэки: client
    # ------------------------------------------------------------------
    def _cb_client(self, user_id: str, args: list) -> None:
        if not args:
            return
        sub = args[0]
        if sub == "create":
            self._cb_client_create(user_id)
        elif sub == "view" and len(args) > 1:
            self._cb_client_view(user_id, args[1])
        elif sub == "edit" and len(args) > 1:
            self._cb_client_edit(user_id, args[1])
        elif sub == "delete" and len(args) > 1:
            self._cb_client_delete(user_id, args[1])
        else:
            self._cb_client_select(user_id, args[0])

    def _cb_client_select(self, user_id: str, client_id) -> None:
        if self.user_states.get(user_id, {}).get("state") != State.CHOOSING_CLIENT:
            return
        client = self.db.get_client(client_id)
        if client is None:
            self.send_message(user_id, "Клиент не найден")
            return
        self.user_states[user_id]["data"]["client_id"] = client_id
        self.user_states[user_id]["state"] = State.CHOOSING_SERVICE
        services = self.db.list_services()
        if not services:
            self.send_message(user_id, "Нет услуг. Сначала создайте услугу.",
                              buttons=[[("← Меню", "menu:main")]])
            return
        buttons = [[(f"{s['name']} — {s['price'] or 0} ₽", f"service:{s['id']}")]
                   for s in services]
        buttons.append([("← Меню", "menu:main")])
        self.send_message(user_id, "Выберите услугу:", buttons=buttons)

    def _cb_client_view(self, user_id: str, client_id) -> None:
        client = self.db.get_client(client_id)
        if client is None:
            self.send_message(user_id, "Клиент не найден")
            return
        channels = self.db.get_client_channels(client_id)
        ch_text = ", ".join(f"{ch}: {'вкл' if on else 'выкл'}"
                            for ch, on in channels.items()) or "нет"
        text = (
            f"Клиент #{client['id']}\n"
            f"ФИО: {client['full_name']}\n"
            f"Телефон: {client['phone'] or '—'}\n"
            f"TG: {client['telegram_id'] or '—'}\n"
            f"VK: {client['vk_id'] or '—'}\n"
            f"MAX: {client['max_id'] or '—'}\n"
            f"Заметки: {client['notes'] or '—'}\n"
            f"Каналы: {ch_text}"
        )
        self.send_message(user_id, text, buttons=[
            [("✏️ Редактировать", f"client:edit:{client_id}"),
             ("🗑 Удалить", f"client:delete:{client_id}")],
            [("← Назад", "menu:clients")],
        ])

    def _cb_client_create(self, user_id: str) -> None:
        self.user_states[user_id] = {"state": State.ENTERING_CLIENT_NAME, "data": {}}
        self.send_message(user_id, "Введите ФИО клиента:",
                          buttons=[[("← Отмена", "menu:clients")]])

    def _cb_client_edit(self, user_id: str, client_id) -> None:
        client = self.db.get_client(client_id)
        if client is None:
            self.send_message(user_id, "Клиент не найден")
            return
        self.user_states[user_id] = {
            "state": State.CHOOSING_CLIENT_ACTION,
            "data": {"edit_client_id": client_id},
        }
        self.send_message(user_id, "Выберите поле для редактирования:", buttons=[
            [("ФИО", "edit_field:full_name"), ("Телефон", "edit_field:phone")],
            [("TG ID", "edit_field:telegram_id"), ("VK ID", "edit_field:vk_id")],
            [("MAX ID", "edit_field:max_id"), ("Заметки", "edit_field:notes")],
            [("Каналы", "edit_field:channels")],
            [("← Назад", f"client:view:{client_id}")],
        ])

    def _cb_client_delete(self, user_id: str, client_id) -> None:
        if self.db.has_orders_for_client(client_id):
            self.send_message(user_id, "Нельзя удалить: у клиента есть заказы",
                              buttons=[[("← Назад", f"client:view:{client_id}")]])
            return
        self.user_states[user_id] = {
            "state": State.CONFIRMING_CLIENT_DELETE,
            "data": {"delete_client_id": client_id},
        }
        self.send_message(user_id, f"Удалить клиента #{client_id}?", buttons=[
            [("✅ Удалить", "confirm:delete_client"),
             ("❌ Отмена", f"client:view:{client_id}")],
        ])

    # ------------------------------------------------------------------
    # Колбэки: service
    # ------------------------------------------------------------------
    def _cb_service(self, user_id: str, args: list) -> None:
        if not args:
            return
        sub = args[0]
        if sub == "create":
            self._cb_service_create(user_id)
        elif sub == "view" and len(args) > 1:
            self._cb_service_view(user_id, args[1])
        elif sub == "edit" and len(args) > 1:
            self._cb_service_edit(user_id, args[1])
        elif sub == "delete" and len(args) > 1:
            self._cb_service_delete(user_id, args[1])
        else:
            self._cb_service_select(user_id, args[0])

    def _cb_service_select(self, user_id: str, service_id) -> None:
        if self.user_states.get(user_id, {}).get("state") != State.CHOOSING_SERVICE:
            return
        service = self.db.get_service(service_id)
        if service is None:
            self.send_message(user_id, "Услуга не найдена")
            return
        self.user_states[user_id]["data"]["service_id"] = service_id
        self.user_states[user_id]["state"] = State.ENTERING_DESCRIPTION
        self.send_message(user_id, "Введите описание заказа или нажмите «Пропустить»:",
                          buttons=[[("Пропустить", "skip_desc")],
                                   [("← Меню", "menu:main")]])

    def _cb_service_view(self, user_id: str, service_id) -> None:
        s = self.db.get_service(service_id)
        if s is None:
            self.send_message(user_id, "Услуга не найдена")
            return
        text = (f"Услуга #{s['id']}\nНазвание: {s['name']}\n"
                f"Ед.: {s['unit'] or '—'}\nЦена: {s['price'] or 0} ₽")
        self.send_message(user_id, text, buttons=[
            [("✏️ Редактировать", f"service:edit:{service_id}"),
             ("🗑 Удалить", f"service:delete:{service_id}")],
            [("← Назад", "menu:services")],
        ])

    def _cb_service_create(self, user_id: str) -> None:
        self.user_states[user_id] = {"state": State.ENTERING_SERVICE_NAME, "data": {}}
        self.send_message(user_id, "Введите название услуги:",
                          buttons=[[("← Отмена", "menu:services")]])

    def _cb_service_edit(self, user_id: str, service_id) -> None:
        s = self.db.get_service(service_id)
        if s is None:
            self.send_message(user_id, "Услуга не найдена")
            return
        self.user_states[user_id] = {
            "state": State.ENTERING_SERVICE_NAME,
            "data": {"edit_service_id": service_id},
        }
        self.send_message(user_id, f"Введите новое название (текущее: {s['name']}):")

    def _cb_service_delete(self, user_id: str, service_id) -> None:
        if self.db.has_orders_for_service(service_id):
            self.send_message(user_id, "Нельзя удалить: услуга используется в заказах",
                              buttons=[[("← Назад", "menu:services")]])
            return
        self.user_states[user_id] = {
            "state": State.CONFIRMING_SERVICE_DELETE,
            "data": {"delete_service_id": service_id},
        }
        self.send_message(user_id, f"Удалить услугу #{service_id}?", buttons=[
            [("✅ Удалить", "confirm:delete_service"),
             ("❌ Отмена", "menu:services")],
        ])

    # ------------------------------------------------------------------
    # Колбэки: order
    # ------------------------------------------------------------------
    def _cb_order(self, user_id: str, args: list) -> None:
        if not args:
            return
        sub = args[0]
        if sub == "edit" and len(args) > 1:
            self._cb_order_edit(user_id, args[1])
        elif sub == "delete" and len(args) > 1:
            self._cb_order_delete(user_id, args[1])
        else:
            self._cb_order_view(user_id, args[0])

    def _cb_order_view(self, user_id: str, order_id) -> None:
        order = self.service.get_order_detail(order_id)
        if order is None:
            self.send_message(user_id, "Заказ не найден")
            return
        self.user_states[user_id] = {
            "state": State.VIEWING_ORDER,
            "data": {"order_id": order_id},
        }
        text = (
            f"Заказ #{order['id']}\n"
            f"Клиент: {order['client_name']}\n"
            f"Услуга: {order['service_name']}\n"
            f"Статус: {order['status_name']}\n"
            f"Описание: {order['description'] or '—'}\n"
            f"Цена: {order['price'] or 0} ₽\n"
            f"Срок: {order['deadline'] or '—'}"
        )
        self.send_message(user_id, text, buttons=[
            [("🔄 Сменить статус", f"status:change:{order_id}")],
            [("📷 Фото", f"status:photos:{order_id}"),
             ("➕ Доп. услуги", f"status:extra:{order_id}")],
            [("✏️ Редактировать", f"order:edit:{order_id}"),
             ("🗑 Удалить", f"order:delete:{order_id}")],
            [("← Меню", "menu:main")],
        ])

    def _cb_order_edit(self, user_id: str, order_id) -> None:
        order = self.db.get_order(order_id)
        if order is None:
            self.send_message(user_id, "Заказ не найден")
            return
        self.user_states[user_id] = {
            "state": State.CHOOSING_ORDER_EDIT_FIELD,
            "data": {"edit_order_id": order_id},
        }
        self.send_message(user_id, "Выберите поле для редактирования:", buttons=[
            [("Описание", "edit_field:description"), ("Цена", "edit_field:price")],
            [("Срок", "edit_field:deadline")],
            [("← Назад", f"order:{order_id}")],
        ])

    def _cb_order_delete(self, user_id: str, order_id) -> None:
        self.user_states[user_id] = {
            "state": State.CONFIRMING_ORDER_DELETE,
            "data": {"delete_order_id": order_id},
        }
        self.send_message(user_id, f"Удалить заказ #{order_id}?", buttons=[
            [("✅ Удалить", "confirm:delete_order"),
             ("❌ Отмена", f"order:{order_id}")],
        ])

    # ------------------------------------------------------------------
    # Колбэки: status / neworder / extra
    # ------------------------------------------------------------------
    def _cb_status(self, user_id: str, args: list) -> None:
        if len(args) < 2:
            return
        sub, order_id = args[0], args[1]
        if sub == "change":
            self._cb_status_change(user_id, order_id)
        elif sub == "photos":
            self._cb_status_photos(user_id, order_id)
        elif sub == "extra":
            self._cb_status_extra(user_id, order_id)

    def _cb_status_change(self, user_id: str, order_id) -> None:
        order = self.db.get_order(order_id)
        if order is None:
            self.send_message(user_id, "Заказ не найден")
            return
        statuses = self.db.list_statuses()
        buttons = [
            [(s["name"], f"neworder:status:{order_id}:{s['id']}")]
            for s in statuses if s["id"] != order["status_id"]
        ]
        buttons.append([("← Назад", f"order:{order_id}")])
        self.user_states[user_id] = {
            "state": State.CHOOSING_STATUS,
            "data": {"order_id": order_id},
        }
        self.send_message(user_id, "Выберите новый статус:", buttons=buttons)

    def _cb_neworder(self, user_id: str, args: list) -> None:
        if len(args) < 3:
            return
        order_id, status_id = args[1], args[2]
        status = self.db.get_status(status_id)
        if status is None:
            self.send_message(user_id, "Статус не найден")
            return
        self.user_states[user_id] = {
            "state": State.AWAITING_STATUS_PHOTO,
            "data": {"order_id": order_id, "status_id": status_id,
                     "status_name": status["name"]},
        }
        self.send_message(
            user_id,
            f"Новый статус: {status['name']}. Отправьте фото или «Пропустить»:",
            buttons=[[("Пропустить", "skip_photo")]],
        )

    def _cb_status_photos(self, user_id: str, order_id) -> None:
        photos = self.db.get_order_photos(order_id)
        if not photos:
            self.send_message(user_id, "Фото нет",
                              buttons=[[("← Назад", f"order:{order_id}")]])
            return
        for p in photos:
            self.send_photo(user_id, p["photo_data"], caption=p["caption"] or "")
        self.send_message(user_id, f"Фото заказа #{order_id}: {len(photos)} шт.",
                          buttons=[[("← Назад", f"order:{order_id}")]])

    def _cb_status_extra(self, user_id: str, order_id) -> None:
        extras = self.db.get_order_services(order_id)
        services = self.db.list_services()
        if extras:
            text = "Доп. услуги:\n" + "\n".join(
                f"#{es['service_id']} {es['service_name']} x{es['quantity']} — "
                f"{es['effective_price'] or 0} ₽" for es in extras
            )
        else:
            text = "Доп. услуг нет"
        buttons = [[(f"➕ {s['name']}", f"extra:add:{order_id}:{s['id']}")]
                   for s in services]
        buttons.append([("← Назад", f"order:{order_id}")])
        self.send_message(user_id, text, buttons=buttons)

    def _cb_extra(self, user_id: str, args: list) -> None:
        if len(args) < 3:
            return
        order_id, service_id = args[1], args[2]
        ok = self.service.add_extra_service(order_id, service_id)
        if ok:
            self.send_message(user_id, "Доп. услуга добавлена")
        else:
            self.send_message(user_id, "Не удалось добавить услугу")
        self._cb_status_extra(user_id, order_id)

    # ------------------------------------------------------------------
    # Колбэки: confirm
    # ------------------------------------------------------------------
    def _cb_confirm(self, user_id: str, args: list) -> None:
        if not args:
            return
        handlers = {
            "create_order": self._cb_confirm_create_order,
            "change_status": self._cb_confirm_change_status,
            "create_client": self._cb_confirm_create_client,
            "delete_client": self._cb_confirm_delete_client,
            "delete_order": self._cb_confirm_delete_order,
            "create_service": self._cb_confirm_create_service,
            "delete_service": self._cb_confirm_delete_service,
        }
        handler = handlers.get(args[0])
        if handler:
            handler(user_id)

    def _cb_confirm_create_order(self, user_id: str) -> None:
        data = self.user_states.get(user_id, {}).get("data", {})
        if not data.get("client_id") or not data.get("service_id"):
            self.send_message(user_id, "Данные заказа неполные")
            return
        order_id = self.service.create_order(
            client_id=data["client_id"],
            service_id=data["service_id"],
            description=data.get("description", ""),
            price=data.get("price"),
            deadline=data.get("deadline", ""),
            status_id=1,
            photo_data=data.get("photo_data"),
            photo_caption=data.get("photo_caption", ""),
        )
        self.user_states[user_id] = {"state": State.MAIN_MENU, "data": {}}
        self.send_message(user_id, f"Заказ #{order_id} создан", buttons=[
            [("Открыть заказ", f"order:{order_id}")],
            [("← Меню", "menu:main")],
        ])

    def _cb_confirm_change_status(self, user_id: str) -> None:
        data = self.user_states.get(user_id, {}).get("data", {})
        order_id, status_id = data.get("order_id"), data.get("status_id")
        if not order_id or not status_id:
            self.send_message(user_id, "Данные неполные")
            return
        ok = self.service.change_status(
            order_id, status_id,
            photo_data=data.get("photo_data"),
            photo_caption=data.get("photo_caption", ""),
        )
        self.user_states[user_id] = {"state": State.MAIN_MENU, "data": {}}
        if ok:
            self.send_message(user_id, f"Статус заказа #{order_id} изменён", buttons=[
                [("Открыть заказ", f"order:{order_id}")],
                [("← Меню", "menu:main")],
            ])
        else:
            self.send_message(user_id, "Не удалось изменить статус",
                              buttons=[[("← Меню", "menu:main")]])

    def _cb_confirm_create_client(self, user_id: str) -> None:
        data = self.user_states.get(user_id, {}).get("data", {})
        if not data.get("full_name"):
            self.send_message(user_id, "ФИО обязательно")
            return
        client_id = self.db.add_client(
            full_name=data["full_name"],
            phone=data.get("phone") or None,
            telegram_id=data.get("telegram_id") or None,
            vk_id=data.get("vk_id") or None,
            max_id=data.get("max_id") or None,
            notes=data.get("notes") or None,
        )
        for channel in ("telegram", "vk", "max"):
            self.db.set_channel(client_id, channel,
                                data.get(f"channel_{channel}", False))
        self.user_states[user_id] = {"state": State.MAIN_MENU, "data": {}}
        self.send_message(user_id, f"Клиент #{client_id} создан",
                          buttons=[[("← Меню", "menu:main")]])

    def _cb_confirm_delete_client(self, user_id: str) -> None:
        data = self.user_states.get(user_id, {}).get("data", {})
        client_id = data.get("delete_client_id")
        if not client_id:
            return
        if self.db.has_orders_for_client(client_id):
            self.send_message(user_id, "Нельзя удалить: у клиента есть заказы")
            return
        self.db.delete_client(client_id)
        self.user_states[user_id] = {"state": State.MAIN_MENU, "data": {}}
        self.send_message(user_id, "Клиент удалён",
                          buttons=[[("← Меню", "menu:main")]])

    def _cb_confirm_delete_order(self, user_id: str) -> None:
        data = self.user_states.get(user_id, {}).get("data", {})
        order_id = data.get("delete_order_id")
        if not order_id:
            return
        self.db.delete_order(order_id)
        self.user_states[user_id] = {"state": State.MAIN_MENU, "data": {}}
        self.send_message(user_id, "Заказ удалён",
                          buttons=[[("← Меню", "menu:main")]])

    def _cb_confirm_create_service(self, user_id: str) -> None:
        data = self.user_states.get(user_id, {}).get("data", {})
        if not data.get("name"):
            return
        self.db.add_service(name=data["name"], price=data.get("price"))
        self.user_states[user_id] = {"state": State.MAIN_MENU, "data": {}}
        self.send_message(user_id, "Услуга создана",
                          buttons=[[("← Меню", "menu:main")]])

    def _cb_confirm_delete_service(self, user_id: str) -> None:
        data = self.user_states.get(user_id, {}).get("data", {})
        service_id = data.get("delete_service_id")
        if not service_id:
            return
        if self.db.has_orders_for_service(service_id):
            self.send_message(user_id, "Нельзя удалить: услуга используется в заказах")
            return
        self.db.delete_service(service_id)
        self.user_states[user_id] = {"state": State.MAIN_MENU, "data": {}}
        self.send_message(user_id, "Услуга удалена",
                          buttons=[[("← Меню", "menu:main")]])

    # ------------------------------------------------------------------
    # Колбэки: edit_field / каналы / skip
    # ------------------------------------------------------------------
    def _cb_edit_field(self, user_id: str, args: list) -> None:
        if not args:
            return
        field = args[0]
        state = self.user_states.get(user_id, {}).get("state")
        data = self.user_states.get(user_id, {}).get("data", {})
        if state == State.CHOOSING_ORDER_EDIT_FIELD:
            self._cb_order_edit_field(user_id, field)
        elif data.get("edit_client_id"):
            self._cb_client_edit_field(user_id, field)

    def _cb_order_edit_field(self, user_id: str, field: str) -> None:
        field_map = {
            "description": (State.EDITING_ORDER_DESCRIPTION, "Введите новое описание:"),
            "price": (State.EDITING_ORDER_PRICE, "Введите новую цену:"),
            "deadline": (State.EDITING_ORDER_DEADLINE, "Введите новый срок (ГГГГ-ММ-ДД):"),
        }
        if field not in field_map:
            return
        state, prompt = field_map[field]
        self.user_states[user_id]["data"]["edit_field"] = field
        self.user_states[user_id]["state"] = state
        self.send_message(user_id, prompt)

    def _cb_client_edit_field(self, user_id: str, field: str) -> None:
        field_map = {
            "full_name": (State.ENTERING_CLIENT_NAME, "Введите новое ФИО:"),
            "phone": (State.ENTERING_CLIENT_PHONE, "Введите новый телефон:"),
            "telegram_id": (State.ENTERING_CLIENT_TG_ID, "Введите новый Telegram ID:"),
            "vk_id": (State.ENTERING_CLIENT_VK_ID, "Введите новый VK ID:"),
            "max_id": (State.ENTERING_CLIENT_MAX_ID, "Введите новый MAX ID:"),
            "notes": (State.ENTERING_CLIENT_NOTES, "Введите новые заметки:"),
        }
        if field == "channels":
            self._show_client_channels_edit(user_id)
            return
        if field not in field_map:
            return
        state, prompt = field_map[field]
        self.user_states[user_id]["data"]["edit_field"] = field
        self.user_states[user_id]["state"] = state
        self.send_message(user_id, prompt)

    def _cb_client_ch(self, user_id: str, args: list) -> None:
        if not args:
            return
        self._toggle_channel(user_id, args[0], edit=False)

    def _cb_client_ch_edit(self, user_id: str, args: list) -> None:
        if not args:
            return
        self._toggle_channel(user_id, args[0], edit=True)

    def _toggle_channel(self, user_id: str, channel: str, edit: bool) -> None:
        if channel not in ("telegram", "vk", "max"):
            return
        data = self.user_states.get(user_id, {}).get("data", {})
        if edit:
            client_id = data.get("edit_client_id")
            if not client_id:
                return
            current = self.db.get_client_channels(client_id).get(channel, False)
            self.db.set_channel(client_id, channel, not current)
            self._show_client_channels_edit(user_id)
        else:
            data[f"channel_{channel}"] = not data.get(f"channel_{channel}", False)
            self._show_client_summary(user_id)

    def _cb_skip_photo(self, user_id: str) -> None:
        state = self.user_states.get(user_id, {}).get("state")
        data = self.user_states.get(user_id, {}).get("data", {})
        if state == State.AWAITING_PHOTO:
            data["photo_data"] = None
            self.user_states[user_id]["state"] = State.CONFIRMING_ORDER
            self._show_order_summary(user_id)
        elif state == State.AWAITING_STATUS_PHOTO:
            data["photo_data"] = None
            self.user_states[user_id]["state"] = State.CONFIRMING_STATUS_CHANGE
            self._show_status_summary(user_id)

    def _cb_skip_desc(self, user_id: str) -> None:
        if self.user_states.get(user_id, {}).get("state") != State.ENTERING_DESCRIPTION:
            return
        self.user_states[user_id]["data"]["description"] = ""
        self.user_states[user_id]["state"] = State.AWAITING_PHOTO
        self.send_message(user_id, "Отправьте фото или нажмите «Пропустить»:",
                          buttons=[[("Пропустить", "skip_photo")]])

    def _cb_skip_contact(self, user_id: str) -> None:
        state = self.user_states.get(user_id, {}).get("state")
        if state in (State.ENTERING_CLIENT_PHONE, State.ENTERING_CLIENT_TG_ID,
                     State.ENTERING_CLIENT_VK_ID, State.ENTERING_CLIENT_MAX_ID,
                     State.ENTERING_CLIENT_NOTES):
            self._advance_client_creation(user_id)
        elif state == State.ENTERING_SERVICE_PRICE:
            self.user_states[user_id]["data"]["price"] = None
            self.user_states[user_id]["state"] = State.CONFIRMING_SERVICE_CREATE
            self._show_service_summary(user_id)

    # ------------------------------------------------------------------
    # Обработка текстовых сообщений по состояниям
    # ------------------------------------------------------------------
    def _msg_description(self, user_id: str, text: str) -> None:
        self.user_states[user_id]["data"]["description"] = text
        self.user_states[user_id]["state"] = State.AWAITING_PHOTO
        self.send_message(user_id, "Отправьте фото или нажмите «Пропустить»:",
                          buttons=[[("Пропустить", "skip_photo")]])

    def _msg_photo(self, user_id: str, photo_data: bytes | None, caption: str = "") -> None:
        self.user_states[user_id]["data"]["photo_data"] = photo_data
        self.user_states[user_id]["data"]["photo_caption"] = caption
        self.user_states[user_id]["state"] = State.CONFIRMING_ORDER
        self._show_order_summary(user_id)

    def _msg_status_photo(self, user_id: str, photo_data: bytes | None,
                          caption: str = "") -> None:
        data = self.user_states.get(user_id, {}).get("data", {})
        data["photo_data"] = photo_data
        data["photo_caption"] = caption
        self.user_states[user_id]["state"] = State.CONFIRMING_STATUS_CHANGE
        self._show_status_summary(user_id)

    def _msg_client_field(self, user_id: str, text: str) -> None:
        data = self.user_states.get(user_id, {}).get("data", {})
        state = self.user_states.get(user_id, {}).get("state")
        if data.get("edit_client_id"):
            field = data.get("edit_field")
            if field == "full_name" and not text.strip():
                self.send_message(user_id, "ФИО не может быть пустым")
                return
            self.db.update_client(data["edit_client_id"], **{field: text.strip()})
            client_id = data["edit_client_id"]
            self.user_states[user_id] = {"state": State.MAIN_MENU, "data": {}}
            self.send_message(user_id, "Клиент обновлён",
                              buttons=[[("← Назад", f"client:view:{client_id}")]])
            return
        if state == State.ENTERING_CLIENT_NAME:
            if not text.strip():
                self.send_message(user_id, "ФИО не может быть пустым. Введите ФИО:")
                return
            data["full_name"] = text.strip()
        else:
            field_map = {
                State.ENTERING_CLIENT_PHONE: "phone",
                State.ENTERING_CLIENT_TG_ID: "telegram_id",
                State.ENTERING_CLIENT_VK_ID: "vk_id",
                State.ENTERING_CLIENT_MAX_ID: "max_id",
                State.ENTERING_CLIENT_NOTES: "notes",
            }
            field = field_map.get(state)
            if field:
                data[field] = text.strip()
        self._advance_client_creation(user_id)

    def _advance_client_creation(self, user_id: str) -> None:
        next_map = {
            State.ENTERING_CLIENT_NAME: (State.ENTERING_CLIENT_PHONE,
                                         "Введите телефон (или «Пропустить»):"),
            State.ENTERING_CLIENT_PHONE: (State.ENTERING_CLIENT_TG_ID,
                                          "Введите Telegram ID (или «Пропустить»):"),
            State.ENTERING_CLIENT_TG_ID: (State.ENTERING_CLIENT_VK_ID,
                                          "Введите VK ID (или «Пропустить»):"),
            State.ENTERING_CLIENT_VK_ID: (State.ENTERING_CLIENT_MAX_ID,
                                          "Введите MAX ID (или «Пропустить»):"),
            State.ENTERING_CLIENT_MAX_ID: (State.ENTERING_CLIENT_NOTES,
                                           "Введите заметки (или «Пропустить»):"),
            State.ENTERING_CLIENT_NOTES: (State.CONFIRMING_CLIENT_CREATE, None),
        }
        state = self.user_states.get(user_id, {}).get("state")
        if state not in next_map:
            return
        next_state, prompt = next_map[state]
        self.user_states[user_id]["state"] = next_state
        if next_state == State.CONFIRMING_CLIENT_CREATE:
            self._show_client_summary(user_id)
        else:
            self.send_message(user_id, prompt,
                              buttons=[[("Пропустить", "skip_contact")]])

    def _msg_order_edit(self, user_id: str, text: str) -> None:
        data = self.user_states.get(user_id, {}).get("data", {})
        order_id = data.get("edit_order_id")
        field = data.get("edit_field")
        if not order_id or not field:
            return
        if field == "price":
            value = _float(text)
            if value is None:
                self.send_message(user_id, "Введите число")
                return
            self.db.update_order(order_id, price=value)
        elif field == "deadline":
            self.db.update_order(order_id, deadline=text.strip())
        else:
            self.db.update_order(order_id, description=text)
        self.user_states[user_id] = {"state": State.MAIN_MENU, "data": {}}
        self.send_message(user_id, "Заказ обновлён", buttons=[
            [("Открыть заказ", f"order:{order_id}")],
            [("← Меню", "menu:main")],
        ])

    def _msg_service_field(self, user_id: str, text: str) -> None:
        data = self.user_states.get(user_id, {}).get("data", {})
        state = self.user_states.get(user_id, {}).get("state")
        if data.get("edit_service_id"):
            if state == State.ENTERING_SERVICE_NAME:
                name = text.strip()
                if not name:
                    self.send_message(user_id, "Название не может быть пустым")
                    return
                existing = self.db.get_service_by_name(name)
                if existing and existing["id"] != data["edit_service_id"]:
                    self.send_message(user_id, "Услуга уже существует")
                    return
                data["name"] = name
                self.user_states[user_id]["state"] = State.ENTERING_SERVICE_PRICE
                self.send_message(user_id, "Введите новую цену:")
            elif state == State.ENTERING_SERVICE_PRICE:
                price = _float(text)
                if price is None:
                    self.send_message(user_id, "Введите число")
                    return
                self.db.update_service(data["edit_service_id"],
                                       name=data["name"], price=price)
                self.user_states[user_id] = {"state": State.MAIN_MENU, "data": {}}
                self.send_message(user_id, "Услуга обновлена",
                                  buttons=[[("← Меню", "menu:services")]])
            return
        if state == State.ENTERING_SERVICE_NAME:
            name = text.strip()
            if not name:
                self.send_message(user_id, "Название не может быть пустым")
                return
            if self.db.get_service_by_name(name):
                self.send_message(user_id, "Услуга уже существует")
                return
            data["name"] = name
            self.user_states[user_id]["state"] = State.ENTERING_SERVICE_PRICE
            self.send_message(user_id, "Введите цену (или «Пропустить»):",
                              buttons=[[("Пропустить", "skip_contact")]])
        elif state == State.ENTERING_SERVICE_PRICE:
            price = _float(text)
            if price is None:
                self.send_message(user_id, "Введите число")
                return
            data["price"] = price
            self.user_states[user_id]["state"] = State.CONFIRMING_SERVICE_CREATE
            self._show_service_summary(user_id)

    # ------------------------------------------------------------------
    # Сводки для подтверждения
    # ------------------------------------------------------------------
    def _show_order_summary(self, user_id: str) -> None:
        data = self.user_states.get(user_id, {}).get("data", {})
        client = self.db.get_client(data.get("client_id"))
        service = self.db.get_service(data.get("service_id"))
        client_name = client["full_name"] if client else "?"
        service_name = service["name"] if service else "?"
        text = (
            f"Подтвердите заказ:\n"
            f"Клиент: {client_name}\n"
            f"Услуга: {service_name}\n"
            f"Описание: {data.get('description') or '—'}\n"
            f"Фото: {'есть' if data.get('photo_data') else 'нет'}"
        )
        self.send_message(user_id, text, buttons=[
            [("✅ Подтвердить", "confirm:create_order"),
             ("❌ Отмена", "menu:main")],
        ])

    def _show_status_summary(self, user_id: str) -> None:
        data = self.user_states.get(user_id, {}).get("data", {})
        text = (
            f"Подтвердите смену статуса заказа #{data.get('order_id')}:\n"
            f"Новый статус: {data.get('status_name')}\n"
            f"Фото: {'есть' if data.get('photo_data') else 'нет'}"
        )
        self.send_message(user_id, text, buttons=[
            [("✅ Подтвердить", "confirm:change_status"),
             ("❌ Отмена", f"order:{data.get('order_id')}")],
        ])

    def _show_client_summary(self, user_id: str) -> None:
        data = self.user_states.get(user_id, {}).get("data", {})
        channels = {ch: data.get(f"channel_{ch}", False)
                    for ch in ("telegram", "vk", "max")}
        ch_text = ", ".join(f"{ch}: {'вкл' if on else 'выкл'}"
                            for ch, on in channels.items())
        text = (
            f"Подтвердите создание клиента:\n"
            f"ФИО: {data.get('full_name')}\n"
            f"Телефон: {data.get('phone') or '—'}\n"
            f"TG: {data.get('telegram_id') or '—'}\n"
            f"VK: {data.get('vk_id') or '—'}\n"
            f"MAX: {data.get('max_id') or '—'}\n"
            f"Заметки: {data.get('notes') or '—'}\n"
            f"Каналы: {ch_text}"
        )
        buttons = [
            [(f"{'✅' if on else '⬜'} {ch}", f"client_ch:{ch}")
             for ch, on in channels.items()],
            [("✅ Подтвердить", "confirm:create_client"),
             ("❌ Отмена", "menu:clients")],
        ]
        self.send_message(user_id, text, buttons=buttons)

    def _show_client_channels_edit(self, user_id: str) -> None:
        data = self.user_states.get(user_id, {}).get("data", {})
        client_id = data.get("edit_client_id")
        if not client_id:
            return
        channels = self.db.get_client_channels(client_id)
        buttons = [
            [(f"{'✅' if on else '⬜'} {ch}", f"client_ch_edit:{ch}")
             for ch, on in channels.items()],
            [("← Назад", f"client:edit:{client_id}")],
        ]
        self.send_message(user_id, "Переключите каналы:", buttons=buttons)

    def _show_service_summary(self, user_id: str) -> None:
        data = self.user_states.get(user_id, {}).get("data", {})
        text = (f"Подтвердите создание услуги:\n"
                f"Название: {data.get('name')}\n"
                f"Цена: {data.get('price') or '—'} ₽")
        self.send_message(user_id, text, buttons=[
            [("✅ Подтвердить", "confirm:create_service"),
             ("❌ Отмена", "menu:services")],
        ])

    # ------------------------------------------------------------------
    # Основной цикл
    # ------------------------------------------------------------------
    def run(self, poll_interval: int = 30) -> None:
        """Бесконечный Long Polling цикл."""
        print(f"MAX bot started, polling {self.endpoint} every {poll_interval}s")
        while True:
            try:
                updates = self.get_updates(timeout=poll_interval)
                for update in updates:
                    self.handle_update(update)
            except KeyboardInterrupt:
                print("Stopped")
                break
            except Exception as exc:  # noqa: BLE001 — цикл не должен умирать
                print(f"Polling error: {exc}")
                time.sleep(5)


def main() -> None:
    """Точка входа: инициализация БД и запуск Long Polling."""
    token = os.environ.get("MAX_TOKEN")
    endpoint = os.environ.get("MAX_ENDPOINT", "https://platform-api2.max.ru")
    admin_ids = [x.strip() for x in os.environ.get("MAX_ADMIN_ID", "").split(",")
                 if x.strip()]
    if not token or not admin_ids:
        print("MAX_TOKEN и MAX_ADMIN_ID обязательны (см. .env)")
        return
    db_path = os.environ.get("ORDERS_DB", "instance/orders.db")
    db = Database(db_path)
    service = OrderService(db)
    bot = MaxBot(token=token, endpoint=endpoint, admin_ids=admin_ids,
                 db=db, service=service)
    bot.run()


if __name__ == "__main__":
    main()