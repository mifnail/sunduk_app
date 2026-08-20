"""Тесты MaxBot: FSM-потоки, авторизация, колбэки (моки HTTP)."""

import pytest

from database import Database
from max_bot import MaxBot, State
from services.order_service import OrderService


class FakeAPI:
    """Заменяет HTTP-методы бота, записывает вызовы."""

    def __init__(self):
        self.messages = []
        self.photos = []

    def send_message(self, user_id, text, buttons=None):
        self.messages.append((user_id, text, buttons))

    def send_photo(self, user_id, photo_data, caption="", buttons=None):
        self.photos.append((user_id, photo_data, caption, buttons))


@pytest.fixture
def bot(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    service = OrderService(db, notifiers={})
    b = MaxBot(token="t", endpoint="https://max.example",
               admin_ids=["1"], db=db, service=service)
    api = FakeAPI()
    b.send_message = api.send_message
    b.send_photo = api.send_photo
    b._api = api
    return b


def _last_text(bot):
    return bot._api.messages[-1][1]


# --- Авторизация ---
def test_non_admin_denied(bot):
    bot.handle_update({"user_id": "999", "text": "hi"})
    assert bot.user_states.get("999") is None
    assert "Доступ запрещён" in _last_text(bot)


def test_admin_allowed(bot):
    bot.handle_update({"user_id": "1", "text": "hi"})
    assert bot.user_states["1"]["state"] == State.MAIN_MENU


# --- Меню ---
def test_main_menu(bot):
    bot.handle_callback("1", "menu:main")
    assert bot.user_states["1"]["state"] == State.MAIN_MENU
    assert "Главное меню" in _last_text(bot)


def test_orders_menu_empty(bot):
    bot.handle_callback("1", "menu:orders")
    assert "Заказов нет" in _last_text(bot)


# --- Создание заказа ---
def test_create_order_flow(bot):
    cid = bot.db.add_client("Иван", telegram_id="111")
    bot.db.set_channel(cid, "telegram", True)
    sid = bot.db.add_service("Печать", price=10)

    bot.handle_callback("1", "menu:new_order")
    assert bot.user_states["1"]["state"] == State.CHOOSING_CLIENT
    bot.handle_callback("1", f"client:{cid}")
    assert bot.user_states["1"]["state"] == State.CHOOSING_SERVICE
    bot.handle_callback("1", f"service:{sid}")
    assert bot.user_states["1"]["state"] == State.ENTERING_DESCRIPTION
    bot.handle_message("1", {"text": "Описание заказа"})
    assert bot.user_states["1"]["state"] == State.AWAITING_PHOTO
    bot.handle_callback("1", "skip_photo")
    assert bot.user_states["1"]["state"] == State.CONFIRMING_ORDER
    bot.handle_callback("1", "confirm:create_order")

    orders = bot.db.list_orders()
    assert len(orders) == 1
    assert orders[0]["description"] == "Описание заказа"
    assert orders[0]["status_name"] == "принят"


def test_create_order_with_photo(bot):
    cid = bot.db.add_client("Иван")
    sid = bot.db.add_service("Печать")
    bot.handle_callback("1", "menu:new_order")
    bot.handle_callback("1", f"client:{cid}")
    bot.handle_callback("1", f"service:{sid}")
    bot.handle_message("1", {"text": "описание"})
    bot.handle_message("1", {"photo": {"data": b"img"}})
    bot.handle_callback("1", "confirm:create_order")
    photos = bot.db.get_order_photos(1)
    assert len(photos) == 1 and photos[0]["photo_data"] == b"img"


def test_create_order_skip_description(bot):
    cid = bot.db.add_client("Иван")
    sid = bot.db.add_service("Печать")
    bot.handle_callback("1", "menu:new_order")
    bot.handle_callback("1", f"client:{cid}")
    bot.handle_callback("1", f"service:{sid}")
    bot.handle_callback("1", "skip_desc")
    bot.handle_callback("1", "skip_photo")
    bot.handle_callback("1", "confirm:create_order")
    assert bot.db.get_order(1)["description"] == ""


# --- Смена статуса ---
def test_change_status_flow(bot):
    cid = bot.db.add_client("Иван")
    sid = bot.db.add_service("Печать")
    oid = bot.db.add_order(cid, sid, status_id=1)

    bot.handle_callback("1", f"status:change:{oid}")
    assert bot.user_states["1"]["state"] == State.CHOOSING_STATUS
    bot.handle_callback("1", f"neworder:status:{oid}:2")
    assert bot.user_states["1"]["state"] == State.AWAITING_STATUS_PHOTO
    bot.handle_callback("1", "skip_photo")
    assert bot.user_states["1"]["state"] == State.CONFIRMING_STATUS_CHANGE
    bot.handle_callback("1", "confirm:change_status")

    assert bot.db.get_order(oid)["status_id"] == 2
    assert [h["status_id"] for h in bot.db.order_history(oid)] == [1, 2]


def test_change_status_with_photo(bot):
    cid = bot.db.add_client("Иван")
    sid = bot.db.add_service("Печать")
    oid = bot.db.add_order(cid, sid, status_id=1)
    bot.handle_callback("1", f"status:change:{oid}")
    bot.handle_callback("1", f"neworder:status:{oid}:3")
    bot.handle_message("1", {"photo": {"data": b"img"}})
    bot.handle_callback("1", "confirm:change_status")
    photos = bot.db.get_order_photos(oid)
    assert len(photos) == 1 and photos[0]["status_id"] == 3


# --- Клиенты ---
def test_create_client_flow(bot):
    bot.handle_callback("1", "client:create")
    assert bot.user_states["1"]["state"] == State.ENTERING_CLIENT_NAME
    bot.handle_message("1", {"text": "Иван"})
    bot.handle_message("1", {"text": "+79001234567"})
    bot.handle_message("1", {"text": "111"})
    bot.handle_callback("1", "skip_contact")  # vk
    bot.handle_callback("1", "skip_contact")  # max
    bot.handle_callback("1", "skip_contact")  # notes
    assert bot.user_states["1"]["state"] == State.CONFIRMING_CLIENT_CREATE
    bot.handle_callback("1", "confirm:create_client")

    clients = bot.db.list_clients()
    assert len(clients) == 1
    assert clients[0]["full_name"] == "Иван"
    assert clients[0]["phone"] == "+79001234567"
    assert clients[0]["telegram_id"] == "111"


def test_create_client_empty_name(bot):
    bot.handle_callback("1", "client:create")
    bot.handle_message("1", {"text": "   "})
    assert bot.user_states["1"]["state"] == State.ENTERING_CLIENT_NAME


def test_client_channel_toggle(bot):
    bot.handle_callback("1", "client:create")
    bot.handle_message("1", {"text": "Иван"})
    bot.handle_callback("1", "skip_contact")
    bot.handle_callback("1", "skip_contact")
    bot.handle_callback("1", "skip_contact")
    bot.handle_callback("1", "skip_contact")
    bot.handle_callback("1", "skip_contact")
    bot.handle_callback("1", "client_ch:telegram")
    bot.handle_callback("1", "confirm:create_client")
    channels = bot.db.get_client_channels(1)
    assert channels.get("telegram") is True


def test_delete_client_with_orders_blocked(bot):
    cid = bot.db.add_client("Иван")
    sid = bot.db.add_service("Печать")
    bot.db.add_order(cid, sid)
    bot.handle_callback("1", f"client:delete:{cid}")
    assert bot.user_states.get("1", {}).get("state") != State.CONFIRMING_CLIENT_DELETE


def test_delete_client_flow(bot):
    cid = bot.db.add_client("Иван")
    bot.handle_callback("1", f"client:delete:{cid}")
    assert bot.user_states["1"]["state"] == State.CONFIRMING_CLIENT_DELETE
    bot.handle_callback("1", "confirm:delete_client")
    assert bot.db.get_client(cid) is None


# --- Услуги ---
def test_create_service_flow(bot):
    bot.handle_callback("1", "service:create")
    bot.handle_message("1", {"text": "Печать"})
    bot.handle_message("1", {"text": "10"})
    bot.handle_callback("1", "confirm:create_service")
    s = bot.db.get_service_by_name("Печать")
    assert s is not None and s["price"] == 10


def test_create_service_duplicate(bot):
    bot.db.add_service("Печать")
    bot.handle_callback("1", "service:create")
    bot.handle_message("1", {"text": "Печать"})
    assert bot.user_states["1"]["state"] == State.ENTERING_SERVICE_NAME


def test_delete_service_with_orders_blocked(bot):
    cid = bot.db.add_client("Иван")
    sid = bot.db.add_service("Печать")
    bot.db.add_order(cid, sid)
    bot.handle_callback("1", f"service:delete:{sid}")
    assert bot.user_states.get("1", {}).get("state") != State.CONFIRMING_SERVICE_DELETE


# --- Доп. услуги ---
def test_extra_service_add(bot):
    cid = bot.db.add_client("Иван")
    sid = bot.db.add_service("Печать")
    sid2 = bot.db.add_service("Постобработка")
    oid = bot.db.add_order(cid, sid)
    bot.handle_callback("1", f"extra:add:{oid}:{sid2}")
    assert len(bot.db.get_order_services(oid)) == 1


# --- Редактирование заказа ---
def test_order_edit_flow(bot):
    cid = bot.db.add_client("Иван")
    sid = bot.db.add_service("Печать")
    oid = bot.db.add_order(cid, sid, description="старое", price=100)
    bot.handle_callback("1", f"order:edit:{oid}")
    assert bot.user_states["1"]["state"] == State.CHOOSING_ORDER_EDIT_FIELD
    bot.handle_callback("1", "edit_field:description")
    bot.handle_message("1", {"text": "новое описание"})
    assert bot.db.get_order(oid)["description"] == "новое описание"


def test_order_delete_flow(bot):
    cid = bot.db.add_client("Иван")
    sid = bot.db.add_service("Печать")
    oid = bot.db.add_order(cid, sid)
    bot.handle_callback("1", f"order:delete:{oid}")
    bot.handle_callback("1", "confirm:delete_order")
    assert bot.db.get_order(oid) is None


# --- Обработка апдейтов ---
def test_handle_update_callback(bot):
    bot.handle_update({"user_id": "1", "update_id": 5, "callback_data": "menu:main"})
    assert bot.last_update_id == 5
    assert bot.user_states["1"]["state"] == State.MAIN_MENU


def test_unknown_callback(bot):
    bot.handle_callback("1", "unknown:action")
    assert "Неизвестная команда" in _last_text(bot)