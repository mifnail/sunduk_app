"""Telegram-бот для оператора студии 3D-печати.

Зеркалит функционал MAX-бота: управление заказами, статусы, клиенты.
Работает через SOCKS5 прокси (TG_PROXY в .env).
"""

import logging
import os

import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)
from telegram.request import HTTPXRequest

from database import Database
from db_schema import init_db, seed_defaults
from notifier import TelegramNotifier, build_notifiers, order_status_message, send_notifications

log = logging.getLogger("tg_bot")

HELP_TEXT = (
    "Студия 3D — бот оператора.\n\n"
    "Команды:\n"
    "/help — помощь\n"
    "/orders — список заказов\n"
    "/order <номер> — детали заказа\n"
    "/status <номер> [статус] — сменить статус\n"
    "/clients — список клиентов"
)


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
        self.db = Database(db_path)
        self.notifiers = build_notifiers()

    def _check_admin(self, update: Update) -> bool:
        chat_id = str(update.effective_chat.id)
        return chat_id == self.admin_id

    # ---------- Handlers ----------

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            await update.message.reply_text("Доступ запрещён: вы не являетесь оператором студии.")
            return
        await update.message.reply_text(HELP_TEXT)

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return
        await update.message.reply_text(HELP_TEXT)

    async def cmd_orders(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            await update.message.reply_text("Доступ запрещён.")
            return
        orders = self.db.list_orders()
        if not orders:
            await update.message.reply_text("Заказов пока нет.")
            return
        lines = []
        buttons = []
        for order in orders[:30]:
            lines.append(
                f"#{order['id']} {order['client_name']} — {order['service_name']} · {order['status_name']}"
            )
            buttons.append([
                InlineKeyboardButton(f"Заказ #{order['id']}", callback_data=f"order:{order['id']}")
            ])
        text = "Заказы:\n" + "\n".join(lines)
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))

    async def cmd_order(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return
        args = ctx.args or []
        if not args or not args[0].isdigit():
            await update.message.reply_text("Формат: /order <номер заказа>")
            return
        order = self.db.get_order(int(args[0]))
        if order is None:
            await update.message.reply_text(f"Заказ #{args[0]} не найден.")
            return
        text, buttons = self._order_view(order)
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return
        args = ctx.args or []
        if not args or not args[0].isdigit():
            await update.message.reply_text("Формат: /status <номер заказа> [статус]")
            return
        order = self.db.get_order(int(args[0]))
        if order is None:
            await update.message.reply_text(f"Заказ #{args[0]} не найден.")
            return
        if len(args) >= 2:
            await self._set_status(update, ctx, args[0], args[1])
            return
        _, buttons = self._order_view(order)
        await update.message.reply_text("Выберите новый статус:", reply_markup=InlineKeyboardMarkup(buttons))

    async def cmd_clients(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_admin(update):
            return
        clients = self.db.list_clients()
        if not clients:
            await update.message.reply_text("Клиентов пока нет.")
            return
        lines = [f"#{c['id']} {c['full_name']} ({c['phone'] or '—'})" for c in clients[:30]]
        await update.message.reply_text("Клиенты:\n" + "\n".join(lines))

    # ---------- Callbacks ----------

    async def callback_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self._check_admin(update):
            await query.answer("Доступ запрещён.", show_alert=True)
            return
        await query.answer()
        data = query.data
        parts = data.split(":")
        if parts[0] == "order" and len(parts) == 2:
            order = self.db.get_order(int(parts[1]))
            if order:
                text, buttons = self._order_view(order)
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        elif parts[0] == "status" and len(parts) == 3:
            await self._set_status_cb(query, parts[1], parts[2])

    # ---------- Logic ----------

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
            row.append(InlineKeyboardButton(st["name"], callback_data=f"status:{order['id']}:{st['id']}"))
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        return buttons

    async def _set_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                          order_ref: str, status_ref: str) -> None:
        order = self.db.get_order(int(order_ref))
        if order is None:
            await update.message.reply_text(f"Заказ #{order_ref} не найден.")
            return
        status = self._find_status(status_ref)
        if status is None:
            await update.message.reply_text(f"Статус «{status_ref}» не найден.")
            return
        if status["id"] == order["status_id"]:
            await update.message.reply_text(f"Статус заказа #{order_ref} уже «{status['name']}».")
            return
        self.db.set_order_status(order["id"], status["id"])
        self._notify_client(order, status["name"])
        await update.message.reply_text(f"Статус заказа #{order_ref} изменён на «{status['name']}».")

    async def _set_status_cb(self, query, order_ref: str, status_ref: str) -> None:
        order = self.db.get_order(int(order_ref))
        if order is None:
            await query.edit_message_text(f"Заказ #{order_ref} не найден.")
            return
        status = self._find_status(status_ref)
        if status is None:
            await query.edit_message_text(f"Статус «{status_ref}» не найден.")
            return
        if status["id"] == order["status_id"]:
            await query.edit_message_text(f"Статус заказа #{order_ref} уже «{status['name']}».")
            return
        self.db.set_order_status(order["id"], status["id"])
        self._notify_client(order, status["name"])
        await query.edit_message_text(f"Статус заказа #{order_ref} изменён на «{status['name']}».")

    def _find_status(self, ref: str):
        for st in self.db.list_statuses():
            if str(st["id"]) == str(ref) or st["name"].lower() == ref.lower():
                return st
        return None

    def _notify_client(self, order, status_name: str) -> None:
        client = self.db.get_client(order["client_id"])
        channel_map = {"telegram": "telegram_id", "vk": "vk_id", "max": "max_id"}
        enabled = {c["channel"] for c in self.db.list_channels(order["client_id"]) if c["enabled"]}
        channels = {ch: client[channel_map[ch]] for ch in enabled if client[channel_map[ch]]}
        send_notifications(channels, order_status_message(order, status_name), self.notifiers)

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

        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("orders", self.cmd_orders))
        app.add_handler(CommandHandler("order", self.cmd_order))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("clients", self.cmd_clients))
        app.add_handler(CallbackQueryHandler(self.callback_handler))

        log.info("TG-бот запущен (Long Polling)")
        app.run_polling(drop_pending_updates=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = TgBot()
    bot.run()


if __name__ == "__main__":
    main()
