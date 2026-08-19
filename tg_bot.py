"""Telegram-бот для оператора студии 3D-печати.

Полностью кнопочный интерфейс через ConversationHandler.
Поддерживает: создание заказа с фото, смену статуса с фото, просмотр заказов/клиентов.
Работает через SOCKS5 прокси (TG_PROXY в .env).
"""

import logging
import os

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)
from telegram.request import HTTPXRequest

from database import Database
from db_schema import init_db, seed_defaults, migrate_db
from services.order_service import OrderService, NotificationPayload

log = logging.getLogger("tg_bot")

# ---------- Conversation States ----------

(
    MAIN_MENU,
    CHOOSING_CLIENT, CHOOSING_SERVICE, ENTERING_DESCRIPTION, AWAITING_PHOTO, CONFIRMING_ORDER,
    CHOOSING_STATUS, AWAITING_STATUS_PHOTO, CONFIRMING_STATUS,
    # Client management
    CHOOSING_CLIENT_ACTION,
    ENTERING_CLIENT_NAME, ENTERING_CLIENT_PHONE, ENTERING_CLIENT_TG_ID,
    ENTERING_CLIENT_VK_ID, ENTERING_CLIENT_MAX_ID, ENTERING_CLIENT_NOTES,
    CONFIRMING_CLIENT_CREATE, CONFIRMING_CLIENT_UPDATE, CONFIRMING_CLIENT_DELETE,
    # Order edit
    CHOOSING_ORDER_EDIT_FIELD,
    EDITING_ORDER_DESCRIPTION, EDITING_ORDER_PRICE, EDITING_ORDER_DEADLINE,
    CONFIRMING_ORDER_EDIT, CONFIRMING_ORDER_DELETE,
) = range(25)

# ---------- Keyboards ----------

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Заказы", callback_data="menu:orders")],
        [InlineKeyboardButton("➕ Новый заказ", callback_data="menu:new_order")],
        [InlineKeyboardButton("👥 Клиенты", callback_data="menu:clients")],
        [InlineKeyboardButton("⚙️ Услуги", callback_data="menu:services")],
    ])

def back_button(target: str = "menu:main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=target)]])

def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="menu:main")]])

def skip_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Пропустить", callback_data="skip")],
        [InlineKeyboardButton("❌ Отмена", callback_data="menu:main")]
    ])

def skip_photo_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Пропустить фото", callback_data="skip_photo")],
        [InlineKeyboardButton("❌ Отмена", callback_data="menu:main")],
    ])

def confirm_keyboard(confirm_cb: str, cancel_cb: str = "menu:main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить", callback_data=confirm_cb)],
        [InlineKeyboardButton("❌ Отмена", callback_data=cancel_cb)],
    ])

def contact_request_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Отправить мой номер", request_contact=True)],
            [KeyboardButton(text="⏭ Пропустить")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

def clients_keyboard(clients) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton("➕ Создать клиента", callback_data="client:create")]]
    for c in clients[:20]:
        row = [InlineKeyboardButton(f"{c['full_name']} ({c['phone'] or '—'})", callback_data=f"client:view:{c['id']}")]
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="menu:main")])
    return InlineKeyboardMarkup(buttons)

def services_keyboard(services) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(f"{s['name']}" + (f" — {s['price']} ₽/{s['unit'] or 'шт'}" if s['price'] else ""), callback_data=f"service:{s['id']}")] for s in services]
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="menu:main")])
    return InlineKeyboardMarkup(buttons)

def status_keyboard(statuses, current_status_id: int, order_id: int, prefix: str = "neworder:status") -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for st in statuses:
        if st["id"] == current_status_id:
            continue
        row.append(InlineKeyboardButton(st["name"], callback_data=f"{prefix}:{order_id}:{st['id']}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data=f"order:{order_id}")])
    return InlineKeyboardMarkup(buttons)

def order_detail_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Сменить статус", callback_data=f"status:change:{order_id}")],
        [InlineKeyboardButton("📸 История фото", callback_data=f"status:photos:{order_id}")],
        [InlineKeyboardButton("➕ Доп. услуги", callback_data=f"status:extra:{order_id}")],
        [InlineKeyboardButton("✏️ Редактировать", callback_data=f"order:edit:{order_id}")],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"order:delete:{order_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:orders")],
    ])

def orders_list_keyboard(orders) -> InlineKeyboardMarkup:
    buttons = []
    for order in orders[:20]:
        emoji = status_emoji(order["status_name"])
        buttons.append([InlineKeyboardButton(f"{emoji} #{order['id']} {order['client_name']} — {order['service_name']} · {order['status_name']}", callback_data=f"order:{order['id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(buttons)


def status_emoji(status_name: str) -> str:
    emoji_map = {
        "принят": "📥",
        "в работе": "⚙️",
        "готов": "✅",
        "выдан": "📦",
        "отменён": "❌",
    }
    return emoji_map.get(status_name.lower(), "📌")


class TgBot:
    def __init__(self):
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
        self.token = os.environ.get("TG_TOKEN", "")
        self.admin_id = os.environ.get("TG_ADMIN_ID", "1054215343")
        db_path = os.environ.get(
            "ORDERS_DB",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "orders.db"),
        )
        init_db(db_path)
        seed_defaults(db_path)
        migrate_db(db_path)
        self.db = Database(db_path)
        self.order_service = OrderService(self.db)

    def _check_admin(self, update: Update) -> bool:
        chat_id = str(update.effective_chat.id)
        return chat_id == self.admin_id

    # ---------- Helpers ----------

    async def _send_or_edit(self, update: Update, text: str, markup: InlineKeyboardMarkup, parse_mode: str = "HTML") -> None:
        """Универсальная отправка: edit если callback, иначе reply."""
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode=parse_mode)
        elif update.message:
            await update.message.reply_text(text, reply_markup=markup, parse_mode=parse_mode)

    async def _answer_callback(self, update: Update, text: str = "") -> None:
        if update.callback_query:
            await update.callback_query.answer(text)

    def _notify_client(self, order, status_name: str,
                     photo_data: bytes | None = None, photo_caption: str = "") -> None:
        self.order_service.notify_status_change(
            order_id=order["id"],
            status_name=status_name,
            payload=NotificationPayload(
                photo_data=photo_data,
                photo_caption=photo_caption,
                photo_mime="image/jpeg"
            )
        )

    # ---------- Entry Points ----------

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        if not self._check_admin(update):
            await update.message.reply_text("Доступ запрещён: вы не оператор студии.")
            return ConversationHandler.END

        # Deep link обработка
        args = ctx.args
        if args and args[0].startswith("link_"):
            return await self._handle_link_token(update, ctx, args[0][5:])

        # Показываем запрос контакта
        await update.message.reply_text(
            "👋 Добро пожаловать в студию 3D-печати!\n\n"
            "Для получения уведомлений о заказах нажмите кнопку ниже:",
            reply_markup=contact_request_keyboard()
        )
        return MAIN_MENU

    async def _show_main_menu(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        self._set_conversation_state(ctx, MAIN_MENU)
        await self._send_or_edit(update, "🏠 <b>Главное меню</b>\nВыберите действие:", main_menu_keyboard())

    async def _handle_link_token(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, token: str) -> int:
        """Заглушка для deep-link токенов — привязка по номеру через /start."""
        await update.message.reply_text(
            "🔗 Ссылка не активна. Используйте кнопку ниже для привязки по номеру:",
            reply_markup=contact_request_keyboard()
        )
        return MAIN_MENU

    # ---------- Conversation Handlers ----------

    async def callback_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if not self._check_admin(update):
            await query.answer("Доступ запрещён.", show_alert=True)
            return ConversationHandler.END
        await query.answer()

        data = query.data
        log.info("callback: user=%s data=%s", update.effective_chat.id, data)

        parts = data.split(":")
        action = parts[0]

        try:
            if action == "menu":
                return await self._handle_menu_callback(update, ctx, parts[1] if len(parts) > 1 else "main")
            elif action == "client":
                sub_action = parts[1] if len(parts) > 1 else ""
                client_id_str = parts[2] if len(parts) > 2 else ""
                if sub_action == "create":
                    return await self._start_create_client(update, ctx)
                elif sub_action == "view" and client_id_str.isdigit():
                    return await self._show_client_detail(update, ctx, int(client_id_str))
                elif sub_action == "edit" and client_id_str.isdigit():
                    return await self._start_edit_client(update, ctx, int(client_id_str))
                elif sub_action == "delete" and client_id_str.isdigit():
                    return await self._confirm_delete_client(update, ctx, int(client_id_str))
                return MAIN_MENU
            elif action == "client_ch":
                return await self._handle_client_channel_toggle(update, ctx, parts[1] if len(parts) > 1 else "")
            elif action == "edit_field":
                return await self._handle_client_field_edit(update, ctx, parts[1] if len(parts) > 1 else "")
            elif action == "edit_order_field":
                return await self._handle_order_field_edit(update, ctx, parts[1] if len(parts) > 1 else "")
            elif action == "service":
                return await self._handle_service_callback(update, ctx, parts[1] if len(parts) > 1 else "")
            elif action == "status":
                return await self._handle_status_callback(update, ctx, parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else "", parts[3] if len(parts) > 3 else "")
            elif action == "order":
                return await self._handle_order_callback(update, ctx, parts[1] if len(parts) > 1 else "")
            elif action == "neworder":
                return await self._handle_neworder_callback(update, ctx, parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else "", parts[3] if len(parts) > 3 else "")
            elif action == "skip_photo":
                return await self._handle_skip_photo(update, ctx)
            elif action == "skip_desc":
                return await self._handle_skip_description(update, ctx)
            elif action == "confirm":
                return await self._handle_confirm(update, ctx, parts[1] if len(parts) > 1 else "")
            elif action == "extra":
                return await self._handle_extra_callback(update, ctx, parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else "", parts[3] if len(parts) > 3 else "")
            elif action == "bind_client" and parts[1].isdigit():
                client_id = int(parts[1])
                self.db.update_client(client_id, telegram_id=str(update.effective_chat.id))
                self.db.set_channel(client_id, "telegram", True)
                await query.answer("✅ Привязано!")
                await self._show_main_menu(update, ctx)
                return MAIN_MENU
        except Exception as exc:
            log.exception("Ошибка callback: %s", exc)
            await query.answer("❌ Ошибка. Попробуйте снова.", show_alert=True)

        return ConversationHandler.END

    # ---------- Menu ----------

    async def _handle_menu_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, action: str) -> int:
        self._set_conversation_state(ctx, MAIN_MENU)
        if action == "main":
            await self._show_main_menu(update, ctx)
            return MAIN_MENU
        elif action == "orders":
            return await self._show_orders_list(update, ctx)
        elif action == "new_order":
            return await self._start_new_order(update, ctx)
        elif action == "clients":
            return await self._show_clients_list(update, ctx)
        elif action == "services":
            return await self._show_services_list(update, ctx)
        return MAIN_MENU

    # ---------- Order Creation Flow ----------

    async def _start_new_order(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        clients = self.db.list_clients()
        if not clients:
            await self._send_or_edit(update, "Сначала добавьте клиентов через веб-интерфейс.", back_button())
            return MAIN_MENU

        self._set_conversation_state(ctx, CHOOSING_CLIENT)
        await self._send_or_edit(update, "👤 <b>Шаг 1/4: Выберите клиента</b>", clients_keyboard(clients))
        return CHOOSING_CLIENT

    async def _handle_client_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, client_id_str: str) -> int:
        if not client_id_str.isdigit():
            await self._answer_callback(update, "Неверный клиент")
            return CHOOSING_CLIENT

        client_id = int(client_id_str)
        client = self.db.get_client(client_id)
        if not client:
            await self._answer_callback(update, "Клиент не найден")
            return CHOOSING_CLIENT

        ctx.user_data["order_data"] = {"client_id": client_id, "client_name": client["full_name"]}
        return await self._show_services_for_order(update, ctx)

    async def _show_services_for_order(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        services = self.db.list_services()
        if not services:
            await self._send_or_edit(update, "Нет доступных услуг. Добавьте через веб-интерфейс.", back_button())
            return MAIN_MENU

        self._set_conversation_state(ctx, CHOOSING_SERVICE)
        await self._send_or_edit(update, "🔧 <b>Шаг 2/4: Выберите основную услугу</b>", services_keyboard(services))
        return CHOOSING_SERVICE

    async def _handle_service_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, service_id_str: str) -> int:
        if not service_id_str.isdigit():
            await self._answer_callback(update, "Неверная услуга")
            return CHOOSING_SERVICE

        service_id = int(service_id_str)
        service = self.db.get_service(service_id)
        if not service:
            await self._answer_callback(update, "Услуга не найдена")
            return CHOOSING_SERVICE

        ctx.user_data["order_data"].update({"service_id": service_id, "service_name": service["name"]})

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭ Пропустить", callback_data="skip_desc")],
            [InlineKeyboardButton("❌ Отмена", callback_data="menu:main")],
        ])
        self._set_conversation_state(ctx, ENTERING_DESCRIPTION)
        await self._send_or_edit(update,
            f"📝 <b>Шаг 3/4: Введите описание заказа</b>\n"
            f"Услуга: {service['name']}\n"
            f"Напишите текст или нажмите «Пропустить»",
            keyboard)
        return ENTERING_DESCRIPTION

    async def _handle_skip_photo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        state = self._get_conversation_state(ctx)

        if state == AWAITING_STATUS_PHOTO:
            ctx.user_data["order_data"]["status_photo_data"] = None
            return await self._show_status_change_confirmation(update, ctx)
        elif state == AWAITING_PHOTO:
            ctx.user_data["order_data"]["photo_data"] = None
            return await self._show_order_confirmation(update, ctx)

        return MAIN_MENU

    async def _handle_skip_description(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        """Пропуск описания на шаге 3 — переход к шагу фото."""
        if "order_data" not in ctx.user_data:
            ctx.user_data["order_data"] = {}
        ctx.user_data["order_data"]["description"] = ""
        self._set_conversation_state(ctx, AWAITING_PHOTO)
        await self._send_or_edit(
            update,
            "📷 <b>Шаг 4/4: Пришлите фото</b>\n"
            "Это может быть фото поломки, желаемой детали или эскиза.\n"
            "Можно пропустить.",
            skip_photo_keyboard()
        )
        return AWAITING_PHOTO

    async def description_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        if not self._check_admin(update):
            return ConversationHandler.END

        text = update.message.text.strip()
        if text.lower() in ("пропустить", "skip", "-"):
            text = ""

        ctx.user_data["order_data"]["description"] = text
        self._set_conversation_state(ctx, AWAITING_PHOTO)
        await update.message.reply_text(
            "📷 <b>Шаг 4/4: Пришлите фото</b>\n"
            "Это может быть фото поломки, желаемой детали или эскиза.\n"
            "Можно пропустить.",
            reply_markup=skip_photo_keyboard(), parse_mode="HTML"
        )
        return AWAITING_PHOTO

    async def photo_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        if not self._check_admin(update):
            return ConversationHandler.END

        if not update.message.photo:
            await update.message.reply_text("Это не фото. Пришлите фото или нажмите «Пропустить».", reply_markup=skip_photo_keyboard())
            return AWAITING_PHOTO

        # Get largest photo
        photo = update.message.photo[-1]
        file = await ctx.bot.get_file(photo.file_id)
        photo_bytes = await file.download_as_bytearray()

        state = self._get_conversation_state(ctx)
        if state == AWAITING_STATUS_PHOTO:
            ctx.user_data["order_data"]["status_photo_data"] = bytes(photo_bytes)
            return await self._show_status_change_confirmation(update, ctx)
        else:
            ctx.user_data["order_data"]["photo_data"] = bytes(photo_bytes)
            return await self._show_order_confirmation(update, ctx)

    async def _show_order_confirmation(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        self._set_conversation_state(ctx, CONFIRMING_ORDER)
        data = ctx.user_data.get("order_data", {})
        text = (
            f"✅ <b>Подтвердите заказ</b>\n\n"
            f"Клиент: {data.get('client_name', '—')}\n"
            f"Услуга: {data.get('service_name', '—')}\n"
            f"Описание: {data.get('description', '—') or '—'}\n"
            f"Фото: {'✅ есть' if data.get('photo_data') else '❌ нет'}"
        )
        await self._send_or_edit(update, text, confirm_keyboard("confirm:create_order", "menu:main"))
        return CONFIRMING_ORDER

    async def _handle_confirm(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, action: str) -> int:
        if action == "create_order":
            return await self._create_order(update, ctx)
        elif action == "change_status":
            return await self._execute_status_change(update, ctx)
        elif action in ("create_client", "update_client", "delete_client"):
            return await self._handle_client_confirmation(update, ctx, action)
        elif action in ("update_order", "delete_order"):
            return await self._handle_order_confirmation(update, ctx, action)
        return MAIN_MENU

    async def _create_order(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        data = ctx.user_data.get("order_data", {})
        required = ["client_id", "service_id"]
        if not all(k in data for k in required):
            await self._send_or_edit(update, "❌ Не хватает данных. Начните заново.", main_menu_keyboard())
            ctx.user_data.clear()
            return MAIN_MENU

        order_id = self.order_service.create_order(
            client_id=data["client_id"],
            service_id=data["service_id"],
            description=data.get("description", ""),
            photo_data=data.get("photo_data"),
            photo_caption=f"Фото при создании: {data.get('description', '')}"
        )

        ctx.user_data.clear()
        await self._send_or_edit(update,
            f"✅ <b>Заказ #{order_id} создан!</b>\nСтатус: принят\nКлиент уведомлён.",
            InlineKeyboardMarkup([[InlineKeyboardButton("📋 К заказам", callback_data="menu:orders")]]))
        return MAIN_MENU

    # ---------- Status Change Flow ----------

    async def _show_orders_list(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        self._set_conversation_state(ctx, MAIN_MENU)
        orders = self.db.list_orders()
        if not orders:
            await self._send_or_edit(update, "📋 Заказов пока нет.", back_button())
        else:
            await self._send_or_edit(update, "📋 <b>Список заказов</b> (последние 20):", orders_list_keyboard(orders))
        return MAIN_MENU

    async def _handle_order_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, sub_action: str) -> int:
        self._set_conversation_state(ctx, MAIN_MENU)
        parts = sub_action.split(":")
        action = parts[0] if parts else ""
        order_id_str = parts[1] if len(parts) > 1 else ""
        
        if action == "view" or (action == "" and order_id_str.isdigit()):
            order_id = int(order_id_str)
            order = self.db.get_order(order_id)
            if not order:
                await self._answer_callback(update, "Заказ не найден")
                return MAIN_MENU
            await self._show_order_detail(update, ctx, order)
            return MAIN_MENU
        elif action == "edit" and order_id_str.isdigit():
            return await self._start_edit_order(update, ctx, int(order_id_str))
        elif action == "delete" and order_id_str.isdigit():
            return await self._confirm_delete_order(update, ctx, int(order_id_str))
        
        return MAIN_MENU

    async def _show_order_detail(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, order) -> None:
        photos = self.db.get_order_photos(order["id"])
        photo_info = ""
        if photos:
            latest = photos[-1]
            photo_info = f"\n📷 Последнее фото: {latest['status_name']} ({latest['caption'] or 'без описания'})"

        text = (
            f"📦 <b>Заказ #{order['id']}</b>\n"
            f"Клиент: {order['client_name']}\n"
            f"Услуга: {order['service_name']}\n"
            f"Статус: {status_emoji(order['status_name'])} {order['status_name']}\n"
            f"Описание: {order['description'] or '—'}\n"
            f"Цена: {order['price'] or '—'} ₽\n"
            f"Дедлайн: {order['deadline'] or '—'}"
            f"{photo_info}"
        )
        await self._send_or_edit(update, text, order_detail_keyboard(order["id"]))

    async def _handle_status_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, action: str, value: str, extra: str) -> int:
        self._set_conversation_state(ctx, MAIN_MENU)
        if action == "change":
            return await self._start_status_change(update, ctx, int(value))
        elif action == "photos":
            return await self._show_order_photos(update, ctx, int(value))
        elif action == "extra":
            return await self._show_extra_services(update, ctx, int(value))
        return MAIN_MENU

    async def _start_status_change(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, order_id: int) -> int:
        order = self.db.get_order(order_id)
        if not order:
            await self._answer_callback(update, "Заказ не найден")
            return MAIN_MENU

        statuses = self.db.list_statuses()
        ctx.user_data["order_data"] = {"order_id": order_id}
        self._set_conversation_state(ctx, CHOOSING_STATUS)

        await self._send_or_edit(update,
            f"🔄 Выберите новый статус для заказа #{order_id}:",
            status_keyboard(statuses, order["status_id"], order_id))
        return CHOOSING_STATUS

    async def _handle_neworder_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, action: str, value: str, extra: str) -> int:
        """Handles status selection: neworder:status:order_id:status_id"""
        if action != "status" or not value.isdigit() or not extra.isdigit():
            return CHOOSING_STATUS

        order_id = int(value)
        status_id = int(extra)
        status = self.db.get_status(status_id)
        if not status:
            await self._answer_callback(update, "Статус не найден")
            return CHOOSING_STATUS

        ctx.user_data["order_data"].update({
            "new_status_id": status_id,
            "new_status_name": status["name"]
        })
        self._set_conversation_state(ctx, AWAITING_STATUS_PHOTO)

        await self._send_or_edit(update,
            f"📷 <b>Пришлите фото для статуса «{status['name']}»</b>\n"
            f"Например: 3D-скан, чертеж, фото готового изделия.\n"
            f"Можно пропустить.",
            skip_photo_keyboard())
        return AWAITING_STATUS_PHOTO

    async def _show_status_change_confirmation(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        self._set_conversation_state(ctx, CONFIRMING_STATUS)
        data = ctx.user_data.get("order_data", {})
        order_id = data.get("order_id")
        status_name = data.get("new_status_name", "—")
        has_photo = "✅ есть" if data.get("status_photo_data") else "❌ нет"

        text = (
            f"✅ <b>Подтвердите смену статуса</b>\n\n"
            f"Заказ: #{order_id}\n"
            f"Новый статус: {status_name}\n"
            f"Фото: {has_photo}"
        )
        await self._send_or_edit(update, text, confirm_keyboard("confirm:change_status", f"order:{order_id}"))
        return CONFIRMING_STATUS

    async def _execute_status_change(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        data = ctx.user_data.get("order_data", {})
        order_id = data.get("order_id")
        new_status_id = data.get("new_status_id")
        new_status_name = data.get("new_status_name")
        photo_data = data.get("status_photo_data")

        if not order_id or not new_status_id:
            await self._send_or_edit(update, "❌ Ошибка: данные потеряны", main_menu_keyboard())
            ctx.user_data.clear()
            return MAIN_MENU

        order = self.db.get_order(order_id)
        if not order:
            await self._send_or_edit(update, "Заказ не найден", main_menu_keyboard())
            ctx.user_data.clear()
            return MAIN_MENU

        ok = self.order_service.change_status(
            order_id=order_id,
            new_status_id=new_status_id,
            photo_data=photo_data,
            photo_caption=f"Фото при смене на «{new_status_name}»"
        )
        if not ok:
            await self._send_or_edit(update, "❌ Не удалось сменить статус заказа", main_menu_keyboard())
            ctx.user_data.clear()
            return MAIN_MENU

        ctx.user_data.clear()
        await self._send_or_edit(update,
            f"✅ Статус заказа #{order_id} изменён на «{new_status_name}»\nКлиент уведомлён.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 К заказу", callback_data=f"order:{order_id}")],
                [InlineKeyboardButton("📋 К списку", callback_data="menu:orders")],
            ]))
        return MAIN_MENU

    async def _show_order_photos(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, order_id: int) -> int:
        self._set_conversation_state(ctx, MAIN_MENU)
        photos = self.db.get_order_photos(order_id)
        if not photos:
            await self._send_or_edit(update, "📷 Фото для этого заказа пока нет.", back_button(f"order:{order_id}"))
            return MAIN_MENU

        text = f"📸 <b>История фото заказа #{order_id}</b>:\n\n"
        for i, p in enumerate(photos, 1):
            text += f"{i}. <b>{p['status_name']}</b> — {p['caption'] or 'без описания'}\n"

        latest = photos[-1]
        await ctx.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=latest["photo_data"],
            caption=f"Заказ #{order_id} — {latest['status_name']}\n{latest['caption'] or ''}",
            reply_markup=back_button(f"order:{order_id}"),
            parse_mode="HTML"
        )
        await ctx.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=back_button(f"order:{order_id}"),
            parse_mode="HTML"
        )
        return MAIN_MENU

    async def _show_extra_services(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, order_id: int) -> int:
        order = self.db.get_order(order_id)
        if not order:
            await self._answer_callback(update, "Заказ не найден")
            return MAIN_MENU

        extra = self.db.get_order_services(order_id)
        services = self.db.list_services()

        text = f"➕ <b>Доп. услуги заказа #{order_id}</b>\n\n"
        if extra:
            for e in extra:
                price = e["price"] if e["price"] is not None else e["default_price"]
                text += f"• {e['name']} × {e['quantity']} = {price * e['quantity']:.0f} ₽\n"
        else:
            text += "Пока нет доп. услуг.\n"

        buttons = [[InlineKeyboardButton(f"+ {s['name']}", callback_data=f"extra:add:{order_id}:{s['id']}")] for s in services]
        buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"order:{order_id}")])

        await self._send_or_edit(update, text, InlineKeyboardMarkup(buttons))
        return MAIN_MENU

    async def _handle_extra_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, action: str, order_id_str: str, service_id_str: str) -> int:
        self._set_conversation_state(ctx, MAIN_MENU)
        if action != "add" or not order_id_str.isdigit() or not service_id_str.isdigit():
            return MAIN_MENU

        order_id = int(order_id_str)
        service_id = int(service_id_str)

        self.db.add_service_to_order(order_id, service_id, quantity=1)

        await self._answer_callback(update, "✅ Услуга добавлена!")
        return await self._show_extra_services(update, ctx, order_id)

    # ---------- Clients & Services Lists ----------

    async def _show_clients_list(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        self._set_conversation_state(ctx, MAIN_MENU)
        clients = self.db.list_clients()
        if not clients:
            await self._send_or_edit(update, "👥 Клиентов пока нет.", back_button())
        else:
            await self._send_or_edit(update, "👥 <b>Клиенты</b> (первые 20):", clients_keyboard(clients))
        return MAIN_MENU

    async def _show_services_list(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        self._set_conversation_state(ctx, MAIN_MENU)
        services = self.db.list_services()
        if not services:
            await self._send_or_edit(update, "⚙️ Услуг пока нет.", back_button())
        else:
            text = "⚙️ <b>Услуги</b>:\nДля редактирования используйте веб-интерфейс."
            buttons = [[InlineKeyboardButton(f"{s['name']}" + (f" — {s['price']} ₽/{s['unit'] or 'шт'}" if s['price'] else ""), callback_data=f"service:view:{s['id']}")] for s in services]
            buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:main")])
            await self._send_or_edit(update, text, InlineKeyboardMarkup(buttons))
        return MAIN_MENU

    # ---------- Client CRUD ----------

    async def _start_create_client(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        self._set_conversation_state(ctx, ENTERING_CLIENT_NAME)
        ctx.user_data["client_data"] = {}
        await self._send_or_edit(update, "👤 <b>Создание клиента</b>\nВведите ФИО (обязательно):", cancel_keyboard())
        return ENTERING_CLIENT_NAME

    async def client_name_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        name = update.message.text.strip()
        if not name:
            await update.message.reply_text("ФИО не может быть пустым. Введите снова:", reply_markup=cancel_keyboard())
            return ENTERING_CLIENT_NAME
        ctx.user_data["client_data"]["full_name"] = name
        self._set_conversation_state(ctx, ENTERING_CLIENT_PHONE)
        await update.message.reply_text("📞 Телефон (или «пропустить»):", reply_markup=skip_button())
        return ENTERING_CLIENT_PHONE

    async def client_phone_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip()
        ctx.user_data["client_data"]["phone"] = "" if text.lower() in ("пропустить", "skip", "-") else text
        self._set_conversation_state(ctx, ENTERING_CLIENT_TG_ID)
        await update.message.reply_text("🤖 Telegram ID (или «пропустить»):", reply_markup=skip_button())
        return ENTERING_CLIENT_TG_ID

    async def client_tg_id_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip()
        ctx.user_data["client_data"]["telegram_id"] = "" if text.lower() in ("пропустить", "skip", "-") else text
        self._set_conversation_state(ctx, ENTERING_CLIENT_VK_ID)
        await update.message.reply_text("🔵 VK ID (или «пропустить»):", reply_markup=skip_button())
        return ENTERING_CLIENT_VK_ID

    async def client_vk_id_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip()
        ctx.user_data["client_data"]["vk_id"] = "" if text.lower() in ("пропустить", "skip", "-") else text
        self._set_conversation_state(ctx, ENTERING_CLIENT_MAX_ID)
        await update.message.reply_text("🟣 MAX ID (или «пропустить»):", reply_markup=skip_button())
        return ENTERING_CLIENT_MAX_ID

    async def client_max_id_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip()
        ctx.user_data["client_data"]["max_id"] = "" if text.lower() in ("пропустить", "skip", "-") else text
        self._set_conversation_state(ctx, ENTERING_CLIENT_NOTES)
        await update.message.reply_text("📝 Заметки (или «пропустить»):", reply_markup=skip_button())
        return ENTERING_CLIENT_NOTES

    async def client_notes_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip()
        ctx.user_data["client_data"]["notes"] = "" if text.lower() in ("пропустить", "skip", "-") else text
        self._set_conversation_state(ctx, CONFIRMING_CLIENT_CREATE)
        await self._show_client_confirmation(update, ctx)
        return CONFIRMING_CLIENT_CREATE

    async def _show_client_confirmation(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        data = ctx.user_data.get("client_data", {})
        text = (
            f"✅ <b>Подтвердите создание клиента</b>\n\n"
            f"ФИО: {data.get('full_name', '—')}\n"
            f"Телефон: {data.get('phone', '—') or '—'}\n"
            f"Telegram ID: {data.get('telegram_id', '—') or '—'}\n"
            f"VK ID: {data.get('vk_id', '—') or '—'}\n"
            f"MAX ID: {data.get('max_id', '—') or '—'}\n"
            f"Заметки: {data.get('notes', '—') or '—'}"
        )
        buttons = [
            [InlineKeyboardButton("✅ Telegram", callback_data="client_ch:telegram"),
             InlineKeyboardButton("✅ VK", callback_data="client_ch:vk"),
             InlineKeyboardButton("✅ MAX", callback_data="client_ch:max")],
            [InlineKeyboardButton("✅ Подтвердить", callback_data="confirm:create_client"),
             InlineKeyboardButton("❌ Отмена", callback_data="menu:main")],
        ]
        await self._send_or_edit(update, text, InlineKeyboardMarkup(buttons))

    async def _show_client_detail(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, client_id: int) -> int:
        client = self.db.get_client(client_id)
        if not client:
            await self._send_or_edit(update, "Клиент не найден", back_button("menu:clients"))
            return MAIN_MENU
        channels = {c["channel"]: c["enabled"] for c in self.db.list_channels(client_id)}
        tg_status = "✅" if channels.get("telegram") else "❌"
        vk_status = "✅" if channels.get("vk") else "❌"
        max_status = "✅" if channels.get("max") else "❌"
        text = (
            f"👤 <b>Клиент #{client_id}</b>\n"
            f"ФИО: {client['full_name']}\n"
            f"Телефон: {client['phone'] or '—'}\n"
            f"Telegram ID: {client['telegram_id'] or '—'} {tg_status}\n"
            f"VK ID: {client['vk_id'] or '—'} {vk_status}\n"
            f"MAX ID: {client['max_id'] or '—'} {max_status}\n"
            f"Заметки: {client['notes'] or '—'}"
        )
        buttons = [
            [InlineKeyboardButton("✏️ Редактировать", callback_data=f"client:edit:{client_id}")],
            [InlineKeyboardButton("🗑 Удалить", callback_data=f"client:delete:{client_id}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu:clients")],
        ]
        await self._send_or_edit(update, text, InlineKeyboardMarkup(buttons))
        return MAIN_MENU

    async def _start_edit_client(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, client_id: int) -> int:
        client = self.db.get_client(client_id)
        if not client:
            await self._send_or_edit(update, "Клиент не найден", back_button("menu:clients"))
            return MAIN_MENU
        ctx.user_data["client_data"] = {"id": client_id, "full_name": client["full_name"],
                                        "phone": client["phone"] or "",
                                        "telegram_id": client["telegram_id"] or "",
                                        "vk_id": client["vk_id"] or "",
                                        "max_id": client["max_id"] or "",
                                        "notes": client["notes"] or ""}
        self._set_conversation_state(ctx, CONFIRMING_CLIENT_UPDATE)
        buttons = [
            [InlineKeyboardButton("ФИО", callback_data="edit_field:full_name"),
             InlineKeyboardButton("Телефон", callback_data="edit_field:phone")],
            [InlineKeyboardButton("Telegram ID", callback_data="edit_field:telegram_id"),
             InlineKeyboardButton("VK ID", callback_data="edit_field:vk_id")],
            [InlineKeyboardButton("MAX ID", callback_data="edit_field:max_id"),
             InlineKeyboardButton("Заметки", callback_data="edit_field:notes")],
            [InlineKeyboardButton("✅ Сохранить", callback_data="confirm:update_client"),
             InlineKeyboardButton("❌ Отмена", callback_data=f"client:view:{client_id}")],
        ]
        await self._send_or_edit(update, "✏️ <b>Редактирование клиента</b>\nВыберите поле для изменения:", InlineKeyboardMarkup(buttons))
        return CONFIRMING_CLIENT_UPDATE

    async def _confirm_delete_client(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, client_id: int) -> int:
        if self.db.has_orders_for_client(client_id):
            await self._send_or_edit(update, "❌ Нельзя удалить: у клиента есть заказы", back_button(f"client:view:{client_id}"))
            return MAIN_MENU
        self._set_conversation_state(ctx, CONFIRMING_CLIENT_DELETE)
        ctx.user_data["client_data"] = {"id": client_id}
        await self._send_or_edit(update, f"❗ Удалить клиента #{client_id}?", confirm_keyboard("confirm:delete_client", f"client:view:{client_id}"))
        return CONFIRMING_CLIENT_DELETE

    async def _handle_client_channel_toggle(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, channel: str) -> int:
        data = ctx.user_data.get("client_data", {})
        client_id = data.get("id")
        if not client_id:
            await self._answer_callback(update, "Сначала выберите клиента")
            return MAIN_MENU
        channels = {c["channel"]: c["enabled"] for c in self.db.list_channels(client_id)}
        current = channels.get(channel, False)
        self.db.set_channel(client_id, channel, not current)
        await self._answer_callback(update, f"{channel}: {'вкл' if not current else 'выкл'}")
        return await self._show_client_confirmation(update, ctx)

    async def _handle_client_field_edit(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, field: str) -> int:
        field_names = {
            "full_name": "ФИО",
            "phone": "Телефон",
            "telegram_id": "Telegram ID",
            "vk_id": "VK ID",
            "max_id": "MAX ID",
            "notes": "Заметки",
        }
        ctx.user_data["editing_field"] = field
        self._set_conversation_state(ctx, CONFIRMING_CLIENT_UPDATE)
        await self._send_or_edit(update, f"✏️ Введите новое значение для <b>{field_names.get(field, field)}</b> (или «пропустить» для очистки):", skip_button())
        return CONFIRMING_CLIENT_UPDATE

    async def client_field_value_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        field = ctx.user_data.get("editing_field")
        if not field:
            return CONFIRMING_CLIENT_UPDATE
        text = update.message.text.strip()
        if text.lower() in ("пропустить", "skip", "-"):
            text = ""
        ctx.user_data["client_data"][field] = text
        ctx.user_data.pop("editing_field", None)
        return await self._start_edit_client(update, ctx, ctx.user_data["client_data"]["id"])

    async def _handle_order_field_edit(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, field: str) -> int:
        field_names = {
            "description": "Описание",
            "price": "Цена",
            "deadline": "Дедлайн",
        }
        ctx.user_data["editing_order_field"] = field
        if field == "description":
            self._set_conversation_state(ctx, EDITING_ORDER_DESCRIPTION)
        elif field == "price":
            self._set_conversation_state(ctx, EDITING_ORDER_PRICE)
        elif field == "deadline":
            self._set_conversation_state(ctx, EDITING_ORDER_DEADLINE)
        await self._send_or_edit(update, f"✏️ Введите новое значение для <b>{field_names.get(field, field)}</b> (или «пропустить» для очистки):", skip_button())
        return self._get_conversation_state(ctx)

    # ---------- Order Edit/Delete ----------

    async def _start_edit_order(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, order_id: int) -> int:
        order = self.db.get_order(order_id)
        if not order:
            await self._send_or_edit(update, "Заказ не найден", back_button("menu:orders"))
            return MAIN_MENU
        ctx.user_data["order_data"] = {"order_id": order_id,
                                       "description": order["description"] or "",
                                       "price": str(order["price"] or ""),
                                       "deadline": order["deadline"] or ""}
        self._set_conversation_state(ctx, CHOOSING_ORDER_EDIT_FIELD)
        buttons = [
            [InlineKeyboardButton("Описание", callback_data="edit_order_field:description"),
             InlineKeyboardButton("Цена", callback_data="edit_order_field:price")],
            [InlineKeyboardButton("Дедлайн", callback_data="edit_order_field:deadline")],
            [InlineKeyboardButton("✅ Сохранить", callback_data="confirm:update_order"),
             InlineKeyboardButton("❌ Отмена", callback_data=f"order:view:{order_id}")],
        ]
        await self._send_or_edit(update, "✏️ <b>Редактирование заказа</b>\nВыберите поле:", InlineKeyboardMarkup(buttons))
        return CHOOSING_ORDER_EDIT_FIELD

    async def _confirm_delete_order(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, order_id: int) -> int:
        order = self.db.get_order(order_id)
        if not order:
            await self._send_or_edit(update, "Заказ не найден", back_button("menu:orders"))
            return MAIN_MENU
        self._set_conversation_state(ctx, CONFIRMING_ORDER_DELETE)
        ctx.user_data["order_data"] = {"order_id": order_id}
        await self._send_or_edit(update, f"❗ Удалить заказ #{order_id} ({order['client_name']})?", confirm_keyboard("confirm:delete_order", f"order:view:{order_id}"))
        return CONFIRMING_ORDER_DELETE

    # ---------- Text handlers for order edit ----------

    async def edit_order_description_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip()
        ctx.user_data["order_data"]["description"] = text
        self._set_conversation_state(ctx, CHOOSING_ORDER_EDIT_FIELD)
        await update.message.reply_text("✅ Описание обновлено. Выберите следующее поле или сохраните.", reply_markup=back_button("menu:main"))
        return CHOOSING_ORDER_EDIT_FIELD

    async def edit_order_price_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip()
        if text and not text.replace(".", "").isdigit():
            await update.message.reply_text("Введите число или оставьте пустым:", reply_markup=skip_button())
            return EDITING_ORDER_PRICE
        ctx.user_data["order_data"]["price"] = text
        self._set_conversation_state(ctx, CHOOSING_ORDER_EDIT_FIELD)
        await update.message.reply_text("✅ Цена обновлена. Выберите следующее поле или сохраните.", reply_markup=back_button("menu:main"))
        return CHOOSING_ORDER_EDIT_FIELD

    async def edit_order_deadline_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip()
        ctx.user_data["order_data"]["deadline"] = text
        self._set_conversation_state(ctx, CHOOSING_ORDER_EDIT_FIELD)
        await update.message.reply_text("✅ Дедлайн обновлён. Выберите следующее поле или сохраните.", reply_markup=back_button("menu:main"))
        return CHOOSING_ORDER_EDIT_FIELD

    # ---------- Confirmation handlers for client/order actions ----------

    async def _handle_client_confirmation(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, action: str) -> int:
        if action == "create_client":
            data = ctx.user_data.get("client_data", {})
            client_id = self.db.add_client(
                full_name=data.get("full_name", ""),
                phone=data.get("phone", ""),
                telegram_id=data.get("telegram_id", ""),
                vk_id=data.get("vk_id", ""),
                max_id=data.get("max_id", ""),
                notes=data.get("notes", "")
            )
            ctx.user_data.clear()
            await self._send_or_edit(update, f"✅ Клиент #{client_id} создан!", InlineKeyboardMarkup([[InlineKeyboardButton("👥 К клиентам", callback_data="menu:clients")]]))
            return MAIN_MENU
        elif action == "update_client":
            data = ctx.user_data.get("client_data", {})
            client_id = data.get("id")
            if client_id:
                self.db.update_client(client_id, **{k: v for k, v in data.items() if k != "id"})
            ctx.user_data.clear()
            await self._send_or_edit(update, f"✅ Клиент #{client_id} обновлён!", InlineKeyboardMarkup([[InlineKeyboardButton("👥 К клиентам", callback_data="menu:clients")]]))
            return MAIN_MENU
        elif action == "delete_client":
            data = ctx.user_data.get("client_data", {})
            client_id = data.get("id")
            if client_id:
                self.db.delete_client(client_id)
            ctx.user_data.clear()
            await self._send_or_edit(update, f"✅ Клиент #{client_id} удалён.", InlineKeyboardMarkup([[InlineKeyboardButton("👥 К клиентам", callback_data="menu:clients")]]))
            return MAIN_MENU
        return MAIN_MENU

    async def _handle_order_confirmation(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, action: str) -> int:
        if action == "update_order":
            data = ctx.user_data.get("order_data", {})
            order_id = data.get("order_id")
            if order_id:
                self.db.update_order(order_id,
                                     description=data.get("description") or None,
                                     price=float(data["price"]) if data.get("price") else None,
                                     deadline=data.get("deadline") or None)
            ctx.user_data.clear()
            await self._send_or_edit(update, f"✅ Заказ #{order_id} обновлён!", InlineKeyboardMarkup([[InlineKeyboardButton("📋 К заказам", callback_data="menu:orders")]]))
            return MAIN_MENU
        elif action == "delete_order":
            data = ctx.user_data.get("order_data", {})
            order_id = data.get("order_id")
            if order_id:
                self.db.delete_order(order_id)
            ctx.user_data.clear()
            await self._send_or_edit(update, f"✅ Заказ #{order_id} удалён.", InlineKeyboardMarkup([[InlineKeyboardButton("📋 К заказам", callback_data="menu:orders")]]))
            return MAIN_MENU
        return MAIN_MENU

    def _get_conversation_state(self, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        # ConversationHandler stores state in a special key, but we can track it ourselves
        return ctx.user_data.get("_conversation_state", MAIN_MENU)

    def _set_conversation_state(self, ctx: ContextTypes.DEFAULT_TYPE, state: int) -> None:
        ctx.user_data["_conversation_state"] = state

    # ---------- Run ----------

    def run(self) -> None:
        if not self.token:
            log.error("TG_TOKEN не задан — бот не запущен")
            return

        proxy_url = os.environ.get("TG_PROXY", "")
        timeout = 30.0
        req_args = {"read_timeout": timeout, "write_timeout": timeout, "connect_timeout": timeout, "pool_timeout": timeout}
        if proxy_url:
            req_args["proxy"] = proxy_url
        request = HTTPXRequest(**req_args)

        app = Application.builder().token(self.token).request(request).get_updates_request(request).build()

        conv_handler = ConversationHandler(
            entry_points=[CommandHandler("start", self.cmd_start)],
            states={
                MAIN_MENU: [
                    CallbackQueryHandler(self.callback_handler),
                    MessageHandler(filters.CONTACT, self.contact_handler),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_fallback_handler),
                ],
                CHOOSING_CLIENT: [CallbackQueryHandler(self.callback_handler)],
                CHOOSING_SERVICE: [CallbackQueryHandler(self.callback_handler)],
                ENTERING_DESCRIPTION: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.description_handler),
                    CallbackQueryHandler(self.callback_handler),
                ],
                AWAITING_PHOTO: [
                    MessageHandler(filters.PHOTO, self.photo_handler),
                    CallbackQueryHandler(self.callback_handler),
                ],
                CONFIRMING_ORDER: [CallbackQueryHandler(self.callback_handler)],
                CHOOSING_STATUS: [CallbackQueryHandler(self.callback_handler)],
                AWAITING_STATUS_PHOTO: [
                    MessageHandler(filters.PHOTO, self.photo_handler),
                    CallbackQueryHandler(self.callback_handler),
                ],
                CONFIRMING_STATUS: [CallbackQueryHandler(self.callback_handler)],
                # Client CRUD
                CHOOSING_CLIENT_ACTION: [CallbackQueryHandler(self.callback_handler)],
                ENTERING_CLIENT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.client_name_handler)],
                ENTERING_CLIENT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.client_phone_handler)],
                ENTERING_CLIENT_TG_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.client_tg_id_handler)],
                ENTERING_CLIENT_VK_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.client_vk_id_handler)],
                ENTERING_CLIENT_MAX_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.client_max_id_handler)],
                ENTERING_CLIENT_NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.client_notes_handler)],
                CONFIRMING_CLIENT_CREATE: [CallbackQueryHandler(self.callback_handler)],
                CONFIRMING_CLIENT_UPDATE: [
                    CallbackQueryHandler(self.callback_handler),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.client_field_value_handler),
                ],
                CONFIRMING_CLIENT_DELETE: [CallbackQueryHandler(self.callback_handler)],
                # Order edit
                CHOOSING_ORDER_EDIT_FIELD: [CallbackQueryHandler(self.callback_handler)],
                EDITING_ORDER_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_order_description_handler)],
                EDITING_ORDER_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_order_price_handler)],
                EDITING_ORDER_DEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_order_deadline_handler)],
                CONFIRMING_ORDER_EDIT: [CallbackQueryHandler(self.callback_handler)],
                CONFIRMING_ORDER_DELETE: [CallbackQueryHandler(self.callback_handler)],
            },
            fallbacks=[CommandHandler("cancel", self.cmd_cancel)],
            per_message=False,
            name="tg_bot_conversation",
            persistent=False,
        )

        app.add_handler(conv_handler)
        log.info("TG-бот запущен (Long Polling)")
        app.run_polling(drop_pending_updates=True)

    async def contact_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        contact = update.message.contact
        if not contact or not contact.phone_number:
            await update.message.reply_text("❌ Не удалось получить номер. Попробуйте снова.",
                                            reply_markup=contact_request_keyboard())
            return MAIN_MENU

        phone = self.db._normalize_phone(contact.phone_number)
        clients = self.db.get_client_by_phone(phone)

        if not clients:
            await update.message.reply_text(
                "❌ Номер не найден в базе клиентов.\n"
                "Обратитесь к оператору для регистрации.",
                reply_markup=ReplyKeyboardRemove()
            )
            await self._show_main_menu(update, ctx)
            return MAIN_MENU

        if len(clients) == 1:
            client = clients[0]
            self.db.update_client(client["id"], telegram_id=str(update.effective_chat.id))
            self.db.set_channel(client["id"], "telegram", True)
            await update.message.reply_text(
                f"✅ <b>Аккаунт привязан!</b>\n"
                f"Здравствуйте, {client['full_name']}!\n"
                f"Теперь вы будете получать уведомления о заказах.",
                parse_mode="HTML",
                reply_markup=ReplyKeyboardRemove()
            )
            await self._show_main_menu(update, ctx)
            return MAIN_MENU

        # Несколько клиентов — показываем выбор
        buttons = []
        for c in clients[:10]:
            buttons.append([InlineKeyboardButton(f"{c['full_name']} ({c['phone']})",
                                                  callback_data=f"bind_client:{c['id']}")])
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="menu:main")])
        await update.message.reply_text(
            f"🔍 Найдено {len(clients)} клиентов. Выберите себя:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return MAIN_MENU

    async def text_fallback_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip().lower()
        if text in ("пропустить", "skip", "⏭ пропустить"):
            await update.message.reply_text(
                "Хорошо, можно привязать позже.",
                reply_markup=ReplyKeyboardRemove()
            )
            await self._show_main_menu(update, ctx)
            return MAIN_MENU
        return MAIN_MENU

    async def cmd_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        ctx.user_data.clear()
        await update.message.reply_text("❌ Отменено", reply_markup=main_menu_keyboard())
        return MAIN_MENU


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = TgBot()
    bot.run()


if __name__ == "__main__":
    main()