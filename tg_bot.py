"""Telegram-бот для оператора студии 3D-печати.

Полностью кнопочный интерфейс через ConversationHandler.
Поддерживает: создание заказа с фото, смену статуса с фото, просмотр заказов/клиентов.
Работает через SOCKS5 прокси (TG_PROXY в .env).
"""

import logging
import os
from typing import Any

import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)
from telegram.request import HTTPXRequest

from database import Database
from db_schema import init_db, seed_defaults, migrate_db
from notifier import build_notifiers, order_status_message, send_notifications

log = logging.getLogger("tg_bot")

# ---------- Conversation States ----------

(
    MAIN_MENU,
    CHOOSING_CLIENT, CHOOSING_SERVICE, ENTERING_DESCRIPTION, AWAITING_PHOTO, CONFIRMING_ORDER,
    CHOOSING_STATUS, AWAITING_STATUS_PHOTO, CONFIRMING_STATUS,
) = range(9)

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

def clients_keyboard(clients) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(f"{c['full_name']} ({c['phone'] or '—'})", callback_data=f"client:{c['id']}")] for c in clients[:20]]
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
        load_dotenv("/opt/sunduk_app/.env")
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
        self.notifiers = build_notifiers()

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
        client = self.db.get_client(order["client_id"])
        channel_map = {"telegram": "telegram_id", "vk": "vk_id", "max": "max_id"}
        enabled = {c["channel"] for c in self.db.list_channels(order["client_id"]) if c["enabled"]}
        channels = {ch: client[channel_map[ch]] for ch in enabled if client[channel_map[ch]]}
        send_notifications(
            channels,
            order_status_message(order, status_name),
            self.notifiers,
            photo_data=photo_data,
            photo_caption=photo_caption,
            photo_mime="image/jpeg"
        )

    # ---------- Entry Points ----------

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        if not self._check_admin(update):
            await update.message.reply_text("Доступ запрещён: вы не оператор студии.")
            return ConversationHandler.END
        await self._show_main_menu(update)
        return MAIN_MENU

    async def _show_main_menu(self, update: Update) -> None:
        ctx = self._get_ctx(update)
        self._set_conversation_state(ctx, MAIN_MENU)
        await self._send_or_edit(update, "🏠 <b>Главное меню</b>\nВыберите действие:", main_menu_keyboard())

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
                return await self._handle_menu_callback(update, parts[1] if len(parts) > 1 else "main")
            elif action == "client":
                return await self._handle_client_callback(update, parts[1] if len(parts) > 1 else "")
            elif action == "service":
                return await self._handle_service_callback(update, parts[1] if len(parts) > 1 else "")
            elif action == "status":
                return await self._handle_status_callback(update, parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else "", parts[3] if len(parts) > 3 else "")
            elif action == "order":
                return await self._handle_order_callback(update, parts[1] if len(parts) > 1 else "")
            elif action == "neworder":
                return await self._handle_neworder_callback(update, parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else "", parts[3] if len(parts) > 3 else "")
            elif action == "skip_photo":
                return await self._handle_skip_photo(update)
            elif action == "confirm":
                return await self._handle_confirm(update, parts[1] if len(parts) > 1 else "")
            elif action == "extra":
                return await self._handle_extra_callback(update, parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else "", parts[3] if len(parts) > 3 else "")
        except Exception as exc:
            log.exception("Ошибка callback: %s", exc)
            await query.answer("❌ Ошибка. Попробуйте снова.", show_alert=True)

        return ConversationHandler.END

    # ---------- Menu ----------

    async def _handle_menu_callback(self, update: Update, action: str) -> int:
        ctx = self._get_ctx(update)
        self._set_conversation_state(ctx, MAIN_MENU)
        if action == "main":
            await self._show_main_menu(update)
            return MAIN_MENU
        elif action == "orders":
            return await self._show_orders_list(update)
        elif action == "new_order":
            return await self._start_new_order(update)
        elif action == "clients":
            return await self._show_clients_list(update)
        elif action == "services":
            return await self._show_services_list(update)
        return MAIN_MENU

    # ---------- Order Creation Flow ----------

    async def _start_new_order(self, update: Update) -> int:
        clients = self.db.list_clients()
        if not clients:
            await self._send_or_edit(update, "Сначала добавьте клиентов через веб-интерфейс.", back_button())
            return MAIN_MENU

        ctx = self._get_ctx(update)
        self._set_conversation_state(ctx, CHOOSING_CLIENT)
        await self._send_or_edit(update, "👤 <b>Шаг 1/4: Выберите клиента</b>", clients_keyboard(clients))
        return CHOOSING_CLIENT

    async def _handle_client_callback(self, update: Update, client_id_str: str) -> int:
        if not client_id_str.isdigit():
            await self._answer_callback(update, "Неверный клиент")
            return CHOOSING_CLIENT

        client_id = int(client_id_str)
        client = self.db.get_client(client_id)
        if not client:
            await self._answer_callback(update, "Клиент не найден")
            return CHOOSING_CLIENT

        ctx = self._get_ctx(update)
        ctx.user_data["order_data"] = {"client_id": client_id, "client_name": client["full_name"]}
        return await self._show_services_for_order(update)

    async def _show_services_for_order(self, update: Update) -> int:
        services = self.db.list_services()
        if not services:
            await self._send_or_edit(update, "Нет доступных услуг. Добавьте через веб-интерфейс.", back_button())
            return MAIN_MENU

        ctx = self._get_ctx(update)
        self._set_conversation_state(ctx, CHOOSING_SERVICE)
        await self._send_or_edit(update, "🔧 <b>Шаг 2/4: Выберите основную услугу</b>", services_keyboard(services))
        return CHOOSING_SERVICE

    async def _handle_service_callback(self, update: Update, service_id_str: str) -> int:
        if not service_id_str.isdigit():
            await self._answer_callback(update, "Неверная услуга")
            return CHOOSING_SERVICE

        service_id = int(service_id_str)
        service = self.db.get_service(service_id)
        if not service:
            await self._answer_callback(update, "Услуга не найдена")
            return CHOOSING_SERVICE

        ctx = self._get_ctx(update)
        ctx.user_data["order_data"].update({"service_id": service_id, "service_name": service["name"]})

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭ Пропустить", callback_data="skip_desc")],
            [InlineKeyboardButton("❌ Отмена", callback_data="menu:main")],
        ])
        ctx = self._get_ctx(update)
        self._set_conversation_state(ctx, ENTERING_DESCRIPTION)
        await self._send_or_edit(update,
            f"📝 <b>Шаг 3/4: Введите описание заказа</b>\n"
            f"Услуга: {service['name']}\n"
            f"Напишите текст или нажмите «Пропустить»",
            keyboard)
        return ENTERING_DESCRIPTION

    async def _handle_skip_photo(self, update: Update) -> int:
        ctx = self._get_ctx(update)
        state = self._get_conversation_state(ctx)

        if state == AWAITING_STATUS_PHOTO:
            ctx.user_data["order_data"]["status_photo_data"] = None
            return await self._show_status_change_confirmation(update)
        elif state == AWAITING_PHOTO:
            ctx.user_data["order_data"]["photo_data"] = None
            return await self._show_order_confirmation(update)

        return MAIN_MENU

    async def description_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        if not self._check_admin(update):
            return ConversationHandler.END

        text = update.message.text.strip()
        if text.lower() in ("пропустить", "skip", "-", "skip_desc"):
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
            return await self._show_status_change_confirmation(update)
        else:
            ctx.user_data["order_data"]["photo_data"] = bytes(photo_bytes)
            return await self._show_order_confirmation(update)

    async def _show_order_confirmation(self, update: Update) -> int:
        ctx = self._get_ctx(update)
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

    async def _handle_confirm(self, update: Update, action: str) -> int:
        ctx = self._get_ctx(update)

        if action == "create_order":
            return await self._create_order(update)
        elif action == "change_status":
            return await self._execute_status_change(update)
        return MAIN_MENU

    async def _create_order(self, update: Update) -> int:
        ctx = self._get_ctx(update)
        data = ctx.user_data.get("order_data", {})
        required = ["client_id", "service_id"]
        if not all(k in data for k in required):
            await self._send_or_edit(update, "❌ Не хватает данных. Начните заново.", main_menu_keyboard())
            ctx.user_data.clear()
            return MAIN_MENU

        order_id = self.db.add_order(
            client_id=data["client_id"],
            service_id=data["service_id"],
            description=data.get("description", ""),
            status_id=1,
        )

        if data.get("photo_data"):
            self.db.add_order_photo(
                order_id=order_id,
                status_id=1,
                photo_data=data["photo_data"],
                caption=f"Фото при создании: {data.get('description', '')}"
            )

        self._notify_client(self.db.get_order(order_id), "принят", data.get("photo_data"), "Новый заказ создан")

        ctx.user_data.clear()
        await self._send_or_edit(update,
            f"✅ <b>Заказ #{order_id} создан!</b>\nСтатус: принят\nКлиент уведомлён.",
            InlineKeyboardMarkup([[InlineKeyboardButton("📋 К заказам", callback_data="menu:orders")]]))
        return MAIN_MENU

    # ---------- Status Change Flow ----------

    async def _show_orders_list(self, update: Update) -> int:
        ctx = self._get_ctx(update)
        self._set_conversation_state(ctx, MAIN_MENU)
        orders = self.db.list_orders()
        if not orders:
            await self._send_or_edit(update, "📋 Заказов пока нет.", back_button())
        else:
            await self._send_or_edit(update, "📋 <b>Список заказов</b> (последние 20):", orders_list_keyboard(orders))
        return MAIN_MENU

    async def _handle_order_callback(self, update: Update, order_id_str: str) -> int:
        ctx = self._get_ctx(update)
        self._set_conversation_state(ctx, MAIN_MENU)
        if not order_id_str.isdigit():
            await self._answer_callback(update, "Неверный заказ")
            return MAIN_MENU

        order = self.db.get_order(int(order_id_str))
        if not order:
            await self._answer_callback(update, "Заказ не найден")
            return MAIN_MENU

        await self._show_order_detail(update, order)
        return MAIN_MENU

    async def _show_order_detail(self, update: Update, order) -> None:
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

    async def _handle_status_callback(self, update: Update, action: str, value: str, extra: str) -> int:
        ctx = self._get_ctx(update)
        self._set_conversation_state(ctx, MAIN_MENU)
        if action == "change":
            return await self._start_status_change(update, int(value))
        elif action == "photos":
            return await self._show_order_photos(update, int(value))
        elif action == "extra":
            return await self._show_extra_services(update, int(value))
        return MAIN_MENU

    async def _start_status_change(self, update: Update, order_id: int) -> int:
        order = self.db.get_order(order_id)
        if not order:
            await self._answer_callback(update, "Заказ не найден")
            return MAIN_MENU

        statuses = self.db.list_statuses()
        ctx = self._get_ctx(update)
        ctx.user_data["order_data"] = {"order_id": order_id}
        self._set_conversation_state(ctx, CHOOSING_STATUS)

        await self._send_or_edit(update,
            f"🔄 Выберите новый статус для заказа #{order_id}:",
            status_keyboard(statuses, order["status_id"], order_id))
        return CHOOSING_STATUS

    async def _handle_neworder_callback(self, update: Update, action: str, value: str, extra: str) -> int:
        """Handles status selection: neworder:status:order_id:status_id"""
        if action != "status" or not value.isdigit() or not extra.isdigit():
            return CHOOSING_STATUS

        order_id = int(value)
        status_id = int(extra)
        status = self.db.get_status(status_id)
        if not status:
            await self._answer_callback(update, "Статус не найден")
            return CHOOSING_STATUS

        ctx = self._get_ctx(update)
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

    async def _show_status_change_confirmation(self, update: Update) -> int:
        ctx = self._get_ctx(update)
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

    async def _execute_status_change(self, update: Update) -> int:
        ctx = self._get_ctx(update)
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

        self.db.set_order_status(order_id, new_status_id)

        if photo_data:
            self.db.add_order_photo(
                order_id=order_id,
                status_id=new_status_id,
                photo_data=photo_data,
                caption=f"Фото при смене на «{new_status_name}»"
            )

        self._notify_client(order, new_status_name, photo_data, f"Статус: {new_status_name}")

        ctx.user_data.clear()
        await self._send_or_edit(update,
            f"✅ Статус заказа #{order_id} изменён на «{new_status_name}»\nКлиент уведомлён.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 К заказу", callback_data=f"order:{order_id}")],
                [InlineKeyboardButton("📋 К списку", callback_data="menu:orders")],
            ]))
        return MAIN_MENU

    async def _show_order_photos(self, update: Update, order_id: int) -> int:
        ctx = self._get_ctx(update)
        self._set_conversation_state(ctx, MAIN_MENU)
        photos = self.db.get_order_photos(order_id)
        if not photos:
            await self._send_or_edit(update, "📷 Фото для этого заказа пока нет.", back_button(f"order:{order_id}"))
            return MAIN_MENU

        text = f"📸 <b>История фото заказа #{order_id}</b>:\n\n"
        for i, p in enumerate(photos, 1):
            text += f"{i}. <b>{p['status_name']}</b> — {p['caption'] or 'без описания'}\n"

        latest = photos[-1]
        await self._get_ctx(update).bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=latest["photo_data"],
            caption=f"Заказ #{order_id} — {latest['status_name']}\n{latest['caption'] or ''}",
            reply_markup=back_button(f"order:{order_id}"),
            parse_mode="HTML"
        )
        await self._get_ctx(update).bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=back_button(f"order:{order_id}"),
            parse_mode="HTML"
        )
        return MAIN_MENU

    async def _show_extra_services(self, update: Update, order_id: int) -> int:
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

    async def _handle_extra_callback(self, update: Update, action: str, order_id_str: str, service_id_str: str) -> int:
        ctx = self._get_ctx(update)
        self._set_conversation_state(ctx, MAIN_MENU)
        if action != "add" or not order_id_str.isdigit() or not service_id_str.isdigit():
            return MAIN_MENU

        order_id = int(order_id_str)
        service_id = int(service_id_str)

        self.db.add_service_to_order(order_id, service_id, quantity=1)

        await self._answer_callback(update, "✅ Услуга добавлена!")
        return await self._show_extra_services(update, order_id)

    # ---------- Clients & Services Lists ----------

    async def _show_clients_list(self, update: Update) -> int:
        ctx = self._get_ctx(update)
        self._set_conversation_state(ctx, MAIN_MENU)
        clients = self.db.list_clients()
        if not clients:
            await self._send_or_edit(update, "👥 Клиентов пока нет.", back_button())
        else:
            await self._send_or_edit(update, "👥 <b>Клиенты</b> (первые 20):", clients_keyboard(clients))
        return MAIN_MENU

    async def _show_services_list(self, update: Update) -> int:
        ctx = self._get_ctx(update)
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

    def _get_ctx(self, update: Update) -> ContextTypes.DEFAULT_TYPE:
        return update._context

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
                MAIN_MENU: [CallbackQueryHandler(self.callback_handler)],
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
            },
            fallbacks=[CommandHandler("cancel", self.cmd_cancel)],
            per_message=False,
            name="tg_bot_conversation",
            persistent=False,
        )

        app.add_handler(conv_handler)
        log.info("TG-бот запущен (Long Polling)")
        app.run_polling(drop_pending_updates=True)

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