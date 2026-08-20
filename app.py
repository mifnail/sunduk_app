"""Flask-приложение: веб-интерфейс оператора.

Маршруты соответствуют SPEC.md (раздел 3). Вся бизнес-логика
вынесена в OrderService, все SQL — в Database. Каждый POST-маршрут
следует паттерну: валидация → проверка существования → бизнес-правило
→ операция → flash + redirect (PRG).
"""

import io
import os

from flask import (Flask, abort, flash, redirect, render_template, request,
                   send_file, url_for)

from database import Database, _normalize_phone
from services.order_service import OrderService

#: Каналы уведомлений клиента (совпадает с CHECK-ограничением в БД).
CHANNELS = ("telegram", "vk", "max")


def _int(value, default=None):
    """Безопасное приведение к int. None/пусто/мусор → default."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value, default=None):
    """Безопасное приведение к float. None/пусто/мусор → default."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def create_app(db_path: str | None = None) -> Flask:
    """Фабрика приложения. db_path можно передать для тестов."""
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    db_path = db_path or os.environ.get("ORDERS_DB", "instance/orders.db")

    db = Database(db_path)
    service = OrderService(db)
    app.extensions["db"] = db
    app.extensions["order_service"] = service

    # ------------------------------------------------------------------
    # Главная
    # ------------------------------------------------------------------
    @app.get("/")
    def index():
        return redirect(url_for("orders_list"))

    # ------------------------------------------------------------------
    # Клиенты
    # ------------------------------------------------------------------
    @app.get("/clients")
    def clients_list():
        clients = []
        for c in db.list_clients():
            row = dict(c)
            row["channels"] = db.get_client_channels(c["id"])
            clients.append(row)
        return render_template("clients.html", clients=clients)

    @app.get("/clients/new")
    def clients_new():
        return render_template("client_form.html", client=None, channels={})

    @app.post("/clients/new")
    def clients_create():
        full_name = request.form.get("full_name", "").strip()
        if not full_name:
            flash("Укажите ФИО", "error")
            return redirect(url_for("clients_new"))
        client_id = db.add_client(
            full_name=full_name,
            phone=_normalize_phone(request.form.get("phone")),
            telegram_id=request.form.get("telegram_id") or None,
            vk_id=request.form.get("vk_id") or None,
            max_id=request.form.get("max_id") or None,
            notes=request.form.get("notes") or None,
        )
        for channel in CHANNELS:
            db.set_channel(client_id, channel,
                           request.form.get(f"channel_{channel}") == "on")
        flash("Клиент добавлен", "success")
        return redirect(url_for("clients_list"))

    @app.get("/clients/<int:client_id>/edit")
    def clients_edit(client_id):
        client = db.get_client(client_id)
        if client is None:
            abort(404)
        return render_template(
            "client_form.html",
            client=client,
            channels=db.get_client_channels(client_id),
        )

    @app.post("/clients/<int:client_id>/edit")
    def clients_update(client_id):
        client = db.get_client(client_id)
        if client is None:
            abort(404)
        full_name = request.form.get("full_name", "").strip()
        if not full_name:
            flash("Укажите ФИО", "error")
            return redirect(url_for("clients_edit", client_id=client_id))
        db.update_client(
            client_id,
            full_name=full_name,
            phone=_normalize_phone(request.form.get("phone")),
            telegram_id=request.form.get("telegram_id") or None,
            vk_id=request.form.get("vk_id") or None,
            max_id=request.form.get("max_id") or None,
            notes=request.form.get("notes") or None,
        )
        for channel in CHANNELS:
            db.set_channel(client_id, channel,
                           request.form.get(f"channel_{channel}") == "on")
        flash("Клиент обновлён", "success")
        return redirect(url_for("clients_list"))

    @app.post("/clients/<int:client_id>/delete")
    def clients_delete(client_id):
        if db.has_orders_for_client(client_id):
            flash("Нельзя удалить: у клиента есть заказы", "error")
            return redirect(url_for("clients_list"))
        db.delete_client(client_id)
        flash("Клиент удалён", "success")
        return redirect(url_for("clients_list"))

    # ------------------------------------------------------------------
    # Услуги
    # ------------------------------------------------------------------
    @app.get("/services")
    def services_list():
        return render_template("services.html", services=db.list_services())

    @app.post("/services/new")
    def services_create():
        name = request.form.get("name", "").strip()
        if not name:
            flash("Укажите название услуги", "error")
            return redirect(url_for("services_list"))
        if db.get_service_by_name(name):
            flash("Услуга уже существует", "error")
            return redirect(url_for("services_list"))
        db.add_service(name=name,
                       unit=request.form.get("unit") or None,
                       price=_float(request.form.get("price")))
        flash("Услуга добавлена", "success")
        return redirect(url_for("services_list"))

    @app.post("/services/<int:service_id>/update")
    def services_update(service_id):
        service_row = db.get_service(service_id)
        if service_row is None:
            abort(404)
        name = request.form.get("name", "").strip()
        if not name:
            flash("Укажите название услуги", "error")
            return redirect(url_for("services_list"))
        existing = db.get_service_by_name(name)
        if existing and existing["id"] != service_id:
            flash("Услуга уже существует", "error")
            return redirect(url_for("services_list"))
        db.update_service(service_id,
                          name=name,
                          unit=request.form.get("unit") or None,
                          price=_float(request.form.get("price")))
        flash("Услуга обновлена", "success")
        return redirect(url_for("services_list"))

    @app.post("/services/<int:service_id>/delete")
    def services_delete(service_id):
        if db.has_orders_for_service(service_id):
            flash("Нельзя удалить: услуга используется в заказах", "error")
            return redirect(url_for("services_list"))
        db.delete_service(service_id)
        flash("Услуга удалена", "success")
        return redirect(url_for("services_list"))

    # ------------------------------------------------------------------
    # Заказы
    # ------------------------------------------------------------------
    @app.get("/orders")
    def orders_list():
        status_id = _int(request.args.get("status"))
        client_id = _int(request.args.get("client"))
        return render_template(
            "orders.html",
            orders=service.list_orders(status_id=status_id, client_id=client_id),
            statuses=db.list_statuses(),
            clients=db.list_clients(),
            filter_status=status_id,
            filter_client=client_id,
        )

    @app.get("/orders/new")
    def orders_new():
        return render_template(
            "order_form.html",
            order=None,
            clients=db.list_clients(),
            services=db.list_services(),
            statuses=db.list_statuses(),
        )

    @app.post("/orders/new")
    def orders_create():
        client_id = _int(request.form.get("client_id"))
        service_id = _int(request.form.get("service_id"))
        if client_id is None or db.get_client(client_id) is None:
            flash("Выберите существующего клиента", "error")
            return redirect(url_for("orders_new"))
        if service_id is None or db.get_service(service_id) is None:
            flash("Выберите существующую услугу", "error")
            return redirect(url_for("orders_new"))
        status_id = _int(request.form.get("status_id"), default=1)
        if status_id is None or db.get_status(status_id) is None:
            status_id = 1
        photo = request.files.get("photo")
        photo_data = photo.read() if photo and photo.filename else None
        order_id = service.create_order(
            client_id=client_id,
            service_id=service_id,
            description=request.form.get("description", ""),
            model_file=request.form.get("model_file", ""),
            price=_float(request.form.get("price")),
            deadline=request.form.get("deadline", ""),
            status_id=status_id,
            photo_data=photo_data,
            photo_caption=request.form.get("photo_caption", ""),
        )
        flash("Заказ создан", "success")
        return redirect(url_for("order_detail", order_id=order_id))

    @app.get("/orders/<int:order_id>")
    def order_detail(order_id):
        detail = service.get_order_detail(order_id)
        if detail is None:
            abort(404)
        return render_template(
            "order_detail.html",
            order=detail,
            statuses=db.list_statuses(),
            services=db.list_services(),
        )

    @app.get("/orders/<int:order_id>/edit")
    def orders_edit(order_id):
        order = db.get_order(order_id)
        if order is None:
            abort(404)
        return render_template(
            "order_form.html",
            order=order,
            clients=db.list_clients(),
            services=db.list_services(),
            statuses=db.list_statuses(),
        )

    @app.post("/orders/<int:order_id>/edit")
    def orders_update(order_id):
        order = db.get_order(order_id)
        if order is None:
            abort(404)
        client_id = _int(request.form.get("client_id"))
        service_id = _int(request.form.get("service_id"))
        if client_id is None or db.get_client(client_id) is None:
            flash("Выберите существующего клиента", "error")
            return redirect(url_for("orders_edit", order_id=order_id))
        if service_id is None or db.get_service(service_id) is None:
            flash("Выберите существующую услугу", "error")
            return redirect(url_for("orders_edit", order_id=order_id))
        db.update_order(
            order_id,
            client_id=client_id,
            service_id=service_id,
            description=request.form.get("description", ""),
            model_file=request.form.get("model_file", ""),
            price=_float(request.form.get("price")),
            deadline=request.form.get("deadline", ""),
        )
        flash("Заказ обновлён", "success")
        return redirect(url_for("order_detail", order_id=order_id))

    @app.post("/orders/<int:order_id>/status")
    def orders_status(order_id):
        order = db.get_order(order_id)
        if order is None:
            abort(404)
        status_id = _int(request.form.get("status_id"))
        if status_id is None or status_id <= 0:
            flash("Неверный статус", "error")
            return redirect(url_for("order_detail", order_id=order_id))
        status = db.get_status(status_id)
        if status is None:
            flash("Статус не найден", "error")
            return redirect(url_for("order_detail", order_id=order_id))
        if order["status_id"] == status_id:
            flash("Статус не изменился", "error")
            return redirect(url_for("order_detail", order_id=order_id))
        photo = request.files.get("status_photo")
        photo_data = photo.read() if photo and photo.filename else None
        service.change_status(
            order_id,
            status_id,
            photo_data=photo_data,
            photo_caption=request.form.get("photo_caption", ""),
        )
        flash(f"Статус изменён на «{status['name']}»", "success")
        return redirect(url_for("order_detail", order_id=order_id))

    @app.post("/orders/<int:order_id>/delete")
    def orders_delete(order_id):
        if db.get_order(order_id) is None:
            abort(404)
        db.delete_order(order_id)
        flash("Заказ удалён", "success")
        return redirect(url_for("orders_list"))

    @app.post("/orders/<int:order_id>/extra/add")
    def orders_extra_add(order_id):
        if db.get_order(order_id) is None:
            abort(404)
        service_id = _int(request.form.get("service_id"))
        if service_id is None or db.get_service(service_id) is None:
            flash("Выберите существующую услугу", "error")
            return redirect(url_for("order_detail", order_id=order_id))
        quantity = _float(request.form.get("quantity"), default=1) or 1
        price = _float(request.form.get("price"))
        service.add_extra_service(order_id, service_id, quantity=quantity, price=price)
        flash("Доп. услуга добавлена", "success")
        return redirect(url_for("order_detail", order_id=order_id))

    @app.post("/orders/<int:order_id>/extra/remove/<int:service_id>")
    def orders_extra_remove(order_id, service_id):
        if db.get_order(order_id) is None:
            abort(404)
        service.remove_extra_service(order_id, service_id)
        flash("Доп. услуга удалена", "success")
        return redirect(url_for("order_detail", order_id=order_id))

    @app.get("/orders/photo/<int:photo_id>")
    def order_photo(photo_id):
        photo = db.get_order_photo(photo_id)
        if photo is None:
            abort(404)
        return send_file(
            io.BytesIO(photo["photo_data"]),
            mimetype=photo["mime_type"],
            download_name=f"photo_{photo_id}",
        )

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000,
            debug=os.environ.get("FLASK_DEBUG") == "1")