import os
import io
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, abort, flash, send_file

from db_schema import init_db, seed_defaults, migrate_db
from database import Database
from notifier import build_notifiers, order_status_message, send_notifications


def create_app() -> Flask:
    app = Flask(__name__)
    instance = os.path.join(app.root_path, "instance")
    os.makedirs(instance, exist_ok=True)
    db_path = os.environ.get("ORDERS_DB", os.path.join(instance, "orders.db"))
    app.config.from_mapping(
        DATABASE=db_path,
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev"),
    )

    init_db(db_path)
    seed_defaults(db_path)
    migrate_db(db_path)

    def db() -> Database:
        return Database(db_path)

    # ---------- Главная ----------

    @app.get("/")
    def index():
        d = db()
        return render_template(
            "index.html",
            orders=d.list_orders(),
            statuses=d.list_statuses(),
        )

    # ---------- Клиенты ----------

    @app.get("/clients")
    def clients():
        return render_template("clients.html", clients=db().list_clients())

    @app.get("/clients/new")
    def client_new():
        return render_template("client_form.html", client=None)

    @app.post("/clients/new")
    def client_create():
        d = db()
        full_name = request.form.get("full_name", "").strip()
        if not full_name:
            flash("Укажите ФИО клиента")
            return redirect(url_for("client_new"))
        cid = d.add_client(
            full_name=full_name,
            phone=request.form.get("phone", ""),
            telegram_id=request.form.get("telegram_id", ""),
            vk_id=request.form.get("vk_id", ""),
            max_id=request.form.get("max_id", ""),
            notes=request.form.get("notes", ""),
        )
        for ch in ("telegram", "vk", "max"):
            d.set_channel(cid, ch, bool(request.form.get(f"ch_{ch}")))
        flash("Клиент добавлен")
        return redirect(url_for("clients"))

    @app.get("/clients/<int:cid>/edit")
    def client_edit(cid: int):
        d = db()
        client = d.get_client(cid)
        if client is None:
            abort(404)
        channels = {c["channel"]: c["enabled"] for c in d.list_channels(cid)}
        return render_template("client_form.html", client=client, channels=channels)

    @app.post("/clients/<int:cid>/edit")
    def client_update(cid: int):
        d = db()
        full_name = request.form.get("full_name", "").strip()
        if not full_name:
            flash("Укажите ФИО клиента")
            return redirect(url_for("client_edit", cid=cid))
        d.update_client(
            cid,
            full_name=full_name,
            phone=request.form.get("phone", ""),
            telegram_id=request.form.get("telegram_id", ""),
            vk_id=request.form.get("vk_id", ""),
            max_id=request.form.get("max_id", ""),
            notes=request.form.get("notes", ""),
        )
        for ch in ("telegram", "vk", "max"):
            d.set_channel(cid, ch, bool(request.form.get(f"ch_{ch}")))
        flash("Клиент обновлён")
        return redirect(url_for("clients"))

    @app.post("/clients/<int:cid>/delete")
    def client_delete(cid: int):
        d = db()
        if d.has_orders_for_client(cid):
            flash("Нельзя удалить клиента: у него есть заказы")
            return redirect(url_for("clients"))
        d.delete_client(cid)
        flash("Клиент удалён")
        return redirect(url_for("clients"))

    # ---------- Услуги ----------

    @app.get("/services")
    def services():
        return render_template("services.html", services=db().list_services())

    @app.post("/services/new")
    def service_create():
        d = db()
        name = request.form.get("name", "").strip()
        if not name:
            flash("Укажите название услуги")
            return redirect(url_for("services"))
        if d.get_service_by_name(name) is not None:
            flash("Услуга с таким названием уже существует")
            return redirect(url_for("services"))
        d.add_service(
            name=name,
            unit=request.form.get("unit", ""),
            price=_price(request.form.get("price")),
        )
        flash("Услуга добавлена")
        return redirect(url_for("services"))

    @app.post("/services/<int:sid>/delete")
    def service_delete(sid: int):
        d = db()
        if d.has_orders_for_service(sid):
            flash("Нельзя удалить услугу: она используется в заказах")
            return redirect(url_for("services"))
        d.delete_service(sid)
        flash("Услуга удалена")
        return redirect(url_for("services"))

    @app.post("/services/<int:sid>/update")
    def service_update(sid: int):
        d = db()
        service = d.get_service(sid)
        if service is None:
            abort(404)
        name = request.form.get("name", "").strip()
        if not name:
            flash("Название не может быть пустым")
            return redirect(url_for("services"))
        # Check for duplicate name (excluding current)
        existing = d.get_service_by_name(name)
        if existing and existing["id"] != sid:
            flash("Услуга с таким названием уже существует")
            return redirect(url_for("services"))
        d.update_service(
            sid,
            name=name,
            unit=request.form.get("unit", ""),
            price=_price(request.form.get("price")),
        )
        flash("Услуга обновлена")
        return redirect(url_for("services"))

    # ---------- Фото заказов ----------

    @app.get("/orders/photo/<int:photo_id>")
    def order_photo(photo_id: int):
        """Отдаёт фото заказа по ID."""
        d = db()
        photo = d.get_order_photo(photo_id)
        if photo is None:
            abort(404)
        return send_file(
            io.BytesIO(photo["photo_data"]),
            mimetype=photo["mime_type"],
            as_attachment=False,
            download_name=f"order_photo_{photo_id}.jpg"
        )

    # ---------- Заказы ----------

    @app.get("/orders")
    def orders():
        d = db()
        status_id = _int(request.args.get("status"))
        client_id = _int(request.args.get("client"))
        orders_list = d.list_orders_with_photos(status_id=status_id, client_id=client_id)
        return render_template(
            "orders.html",
            orders=orders_list,
            statuses=d.list_statuses(),
            clients=d.list_clients(),
            status_id=status_id,
            client_id=client_id,
        )

    @app.get("/orders/new")
    def order_new():
        d = db()
        return render_template(
            "order_detail.html",
            order=None,
            clients=d.list_clients(),
            services=d.list_services(),
            statuses=d.list_statuses(),
        )

    @app.post("/orders/new")
    def order_create():
        d = db()
        client_id = _int(request.form.get("client_id"))
        service_id = _int(request.form.get("service_id"))
        if client_id is None or d.get_client(client_id) is None:
            flash("Выберите существующего клиента")
            return redirect(url_for("order_new"))
        if service_id is None or d.get_service(service_id) is None:
            flash("Выберите существующую услугу")
            return redirect(url_for("order_new"))

        order_id = d.add_order(
            client_id=client_id,
            service_id=service_id,
            description=request.form.get("description", ""),
            model_file=request.form.get("model_file", ""),
            price=_price(request.form.get("price")),
            deadline=request.form.get("deadline", ""),
            status_id=_int(request.form.get("status_id", 1)) or 1,
        )

        # Handle photo upload
        photo_file = request.files.get("photo")
        if photo_file and photo_file.filename:
            photo_data = photo_file.read()
            if photo_data:
                d.add_order_photo(
                    order_id=order_id,
                    status_id=1,  # "принят"
                    photo_data=photo_data,
                    mime_type=photo_file.mimetype or "image/jpeg",
                    caption=request.form.get("photo_caption", "")
                )

        flash("Заказ создан")
        return redirect(url_for("orders"))

    @app.get("/orders/<int:oid>")
    def order_detail(oid: int):
        d = db()
        order = d.get_order(oid)
        if order is None:
            abort(404)
        return render_template(
            "order_detail.html",
            order=order,
            history=d.order_history(oid),
            statuses=d.list_statuses(),
            photos=d.get_order_photos(oid),
            extra_services=d.get_order_services(oid),
            extra_total=d.calculate_extra_total(oid),
            services=d.list_services(),
        )

    @app.post("/orders/<int:oid>/status")
    def order_set_status(oid: int):
        d = db()
        order = d.get_order(oid)
        if order is None:
            abort(404)
        status = d.get_status(_int(request.form.get("status_id")) or 0)
        if status is None:
            flash("Указанный статус не существует")
            return redirect(url_for("order_detail", oid=oid))
        status_id = status["id"]
        if status_id == order["status_id"]:
            flash("Статус не изменился")
            return redirect(url_for("order_detail", oid=oid))

        # Handle status photo upload
        photo_file = request.files.get("status_photo")
        photo_data = None
        photo_caption = ""
        photo_mime = "image/jpeg"
        if photo_file and photo_file.filename:
            photo_data = photo_file.read()
            photo_caption = request.form.get("photo_caption", "")
            photo_mime = photo_file.mimetype or "image/jpeg"

        d.set_order_status(oid, status_id)

        # Save photo if provided
        if photo_data:
            d.add_order_photo(
                order_id=oid,
                status_id=status_id,
                photo_data=photo_data,
                mime_type=photo_mime,
                caption=photo_caption
            )

        # Notify client (with photo if provided)
        client = d.get_client(order["client_id"])
        channel_map = {"telegram": "telegram_id", "vk": "vk_id", "max": "max_id"}
        enabled = {c["channel"] for c in d.list_channels(order["client_id"]) if c["enabled"]}
        channels = {
            ch: client[channel_map[ch]]
            for ch in enabled
            if client[channel_map[ch]]
        }
        notifiers = build_notifiers()
        # Use the latest photo for notification (either just uploaded or existing)
        notify_photo = photo_data
        notify_caption = f"Статус: {status['name']}\n{photo_caption}" if photo_caption else f"Статус: {status['name']}"
        results = send_notifications(
            channels,
            order_status_message(order, status["name"]),
            notifiers,
            photo_data=notify_photo,
            photo_caption=notify_caption,
            photo_mime=photo_mime
        )
        sent = [k for k, v in results.items() if v]
        flash("Статус изменён" + (f", уведомлено: {', '.join(sent)}" if sent else ""))
        return redirect(url_for("order_detail", oid=oid))

    @app.post("/orders/<int:oid>/delete")
    def order_delete(oid: int):
        db().delete_order(oid)
        flash("Заказ удалён")
        return redirect(url_for("orders"))

    # ---------- Дополнительные услуги к заказу ----------

    @app.post("/orders/<int:oid>/extra/add")
    def add_extra_service(oid: int):
        d = db()
        order = d.get_order(oid)
        if order is None:
            abort(404)
        service_id = _int(request.form.get("service_id"))
        if service_id is None or d.get_service(service_id) is None:
            flash("Выберите существующую услугу")
            return redirect(url_for("order_detail", oid=oid))
        quantity = _price(request.form.get("quantity")) or 1
        price = _price(request.form.get("price"))
        d.add_service_to_order(oid, service_id, quantity=quantity, price=price)
        flash("Доп. услуга добавлена")
        return redirect(url_for("order_detail", oid=oid))

    @app.post("/orders/<int:oid>/extra/remove/<int:sid>")
    def remove_extra_service(oid: int, sid: int):
        d = db()
        order = d.get_order(oid)
        if order is None:
            abort(404)
        d.remove_service_from_order(oid, sid)
        flash("Доп. услуга удалена")
        return redirect(url_for("order_detail", oid=oid))

    return app


def _int(value: object) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _price(value: object) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)