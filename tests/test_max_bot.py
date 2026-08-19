import pytest

from max_bot import MaxBot
from db_schema import init_db, seed_defaults


class FakeResponse:
    def __init__(self, json_data=None, status_code=200, text=""):
        self._json = json_data or {}
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class FakeSession:
    def __init__(self):
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        # Return response with text attribute for logging
        return FakeResponse({"success": True, "message": {}}, text="OK")

    def get(self, url, **kwargs):
        self.requests.append(("GET", url, kwargs))
        return FakeResponse({"updates": [], "marker": 1}, text="OK")


def make_bot(db_path, admin_ids="1"):
    return MaxBot(token="tok", admin_ids=admin_ids, db_path=db_path,
                  session=FakeSession())


def make_ctx(db_path):
    init_db(db_path)
    seed_defaults(db_path)
    from database import Database
    d = Database(db_path)
    cid = d.add_client("Иван Петров", phone="+7999", max_id="1")
    d.set_channel(cid, "max", True)
    oid = d.add_order(cid, 2, "Ключница 150мм", price=600)
    return d, cid, oid


def message_update(user_id, text):
    return {
        "update_type": "message_created",
        "user": {"id": user_id},
        "message": {"sender": {"id": user_id}, "body": {"text": text}},
    }


def callback_update(user_id, payload, callback_id="cb1"):
    return {
        "update_type": "message_callback",
        "user": {"id": user_id},
        "callback": {"callback_id": callback_id, "user": {"id": user_id}, "payload": payload},
    }


def test_non_admin_is_rejected(tmp_path):
    bot = make_bot(str(tmp_path / "o.db"))
    bot.handle_update(message_update("999", "/orders"))
    sent = bot.session.requests[0]
    assert "Доступ запрещён" in sent[2]["json"]["text"]


def test_help_command(tmp_path):
    bot = make_bot(str(tmp_path / "o.db"))
    bot.handle_update(message_update("1", "/help"))
    sent = bot.session.requests[0]
    assert "/orders" in sent[2]["json"]["text"]


def test_orders_list_shows_orders(tmp_path):
    db_path = str(tmp_path / "o.db")
    make_ctx(db_path)
    bot = make_bot(db_path)
    bot.handle_update(message_update("1", "/orders"))
    sent = bot.session.requests[0]
    assert "Заказы:" in sent[2]["json"]["text"]
    assert "3D-печать" in sent[2]["json"]["text"]


def test_order_detail_with_status_buttons(tmp_path):
    db_path = str(tmp_path / "o.db")
    _, _, oid = make_ctx(db_path)
    bot = make_bot(db_path)
    bot.handle_update(message_update("1", f"/order {oid}"))
    sent = bot.session.requests[0]
    body = sent[2]["json"]
    assert f"Заказ #{oid}" in body["text"]
    buttons = body["attachments"][0]["payload"]["buttons"]
    flat = [b["text"] for row in buttons for b in row]
    assert "готов" in flat
    assert all(b["type"] == "callback" for row in buttons for b in row)


def test_status_change_via_callback(tmp_path):
    db_path = str(tmp_path / "o.db")
    d, _, oid = make_ctx(db_path)
    bot = make_bot(db_path)
    bot.handle_update(callback_update("1", f"status:{oid}:3"))
    sent = bot.session.requests[0]
    assert "изменён на «готов»" in sent[2]["json"]["message"]["text"]
    assert d.get_order(oid)["status_id"] == 3


def test_callback_status_same_no_change(tmp_path):
    db_path = str(tmp_path / "o.db")
    d, _, oid = make_ctx(db_path)
    bot = make_bot(db_path)
    bot.handle_update(callback_update("1", f"status:{oid}:1"))
    sent = bot.session.requests[0]
    assert "уже" in sent[2]["json"]["message"]["text"]
    assert d.get_order(oid)["status_id"] == 1


def test_callback_unknown_order(tmp_path):
    db_path = str(tmp_path / "o.db")
    make_ctx(db_path)
    bot = make_bot(db_path)
    bot.handle_update(callback_update("1", "status:999:3"))
    sent = bot.session.requests[0]
    assert "не найден" in sent[2]["json"]["message"]["text"]


def test_unknown_status_rejected(tmp_path):
    db_path = str(tmp_path / "o.db")
    _, _, oid = make_ctx(db_path)
    bot = make_bot(db_path)
    bot.handle_update(callback_update("1", f"status:{oid}:99"))
    sent = bot.session.requests[0]
    assert "не найден" in sent[2]["json"]["message"]["text"]


def test_status_by_name_text_command(tmp_path):
    db_path = str(tmp_path / "o.db")
    d, _, oid = make_ctx(db_path)
    bot = make_bot(db_path)
    bot.handle_update(message_update("1", f"/status {oid} готов"))
    sent = bot.session.requests[0]
    assert "изменён на «готов»" in sent[2]["json"]["text"]
    assert d.get_order(oid)["status_id"] == 3


def test_get_updates_uses_marker(tmp_path):
    db_path = str(tmp_path / "o.db")
    bot = make_bot(db_path)
    bot.get_updates()
    req = bot.session.requests[0]
    assert req[0] == "GET"
    assert "/updates" in req[1]
    assert "marker" not in req[2]["params"]
    assert bot.marker == 1


def test_commands_require_admin(tmp_path):
    db_path = str(tmp_path / "o.db")
    bot = make_bot(db_path, admin_ids="")
    bot.handle_update(message_update("1", "/orders"))
    sent = bot.session.requests[0]
    assert "Доступ запрещён" in sent[2]["json"]["text"]


def test_bot_started_sends_help(tmp_path):
    bot = make_bot(str(tmp_path / "o.db"))
    bot.handle_update({"update_type": "bot_started", "user": {"id": "1"}})
    sent = bot.session.requests[0]
    assert "/orders" in sent[2]["json"]["text"]