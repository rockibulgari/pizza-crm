"""Веб-панель (мини-CRM) пиццерии «Пицца Назрань».

Локальный запуск:  flask --app app run
Первый админ:      flask --app app create-admin
"""
import math
import sqlite3
from datetime import datetime, timedelta

import click
from flask import Flask, g, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import config
import db
import security
from security import login_required

SESSION_LIFETIME = timedelta(hours=12)  # одна смена, потом вход заново
MIN_PASSWORD_LENGTH = 8

ROLE_NAMES = {db.ROLE_ADMIN: "Администратор", db.ROLE_OPERATOR: "Оператор"}

ORDERS_PER_PAGE = 50

# Статус в базе → ключ для адреса и CSS (ключи как в bot.py) и смайл как в боте
STATUS_KEYS = dict(zip(db.STATUSES, ("new", "accepted", "cooking", "on_way", "delivered", "cancelled")))
STATUS_EMOJI = dict(zip(db.STATUSES, ("🆕", "✅", "🍕", "🚗", "✔️", "❌")))

# Вкладки над списком: ключ в адресе (?status=…) → (надпись, статусы; None — все)
STATUS_TABS = {
    "active": ("Активные", db.ACTIVE_STATUSES),
    "all": ("Все", None),
} | {STATUS_KEYS[status]: (status.capitalize(), (status,)) for status in db.STATUSES}

# С этим хэшем сверяется пароль, если логина нет, — чтобы ответ «нет такого
# логина» не приходил заметно быстрее, чем «неверный пароль»
DUMMY_HASH = generate_password_hash("dummy-password")

app = Flask(__name__)
app.config.update(
    SECRET_KEY=config.SECRET_KEY,
    SESSION_COOKIE_SECURE=True,      # cookie сессии — только по HTTPS (и на localhost)
    SESSION_COOKIE_HTTPONLY=True,    # недоступна из JavaScript
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=SESSION_LIFETIME,
)
# На сервере перед панелью стоит Caddy. ProxyFix берёт из его заголовков
# настоящий IP сотрудника и то, что запрос пришёл по HTTPS
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

db.init_db()


@app.before_request
def before_request():
    security.load_current_staff()
    # Смена кончилась — входим заново
    logged_in_at = session.get("logged_in_at")
    if g.staff and (
        logged_in_at is None
        or db.now_moscow() - datetime.fromisoformat(logged_in_at) > SESSION_LIFETIME
    ):
        session.clear()
        g.staff = None
    security.check_csrf()


@app.context_processor
def template_helpers():
    return {
        "csrf_token": security.csrf_token,
        "role_names": ROLE_NAMES,
        "status_keys": STATUS_KEYS,
    }


@app.template_filter("when")
def format_when(moment: datetime) -> str:
    """Время заказа для списка: сегодня — «21:05», другой день — «17.10 21:05»."""
    if moment.date() == db.now_moscow().date():
        return moment.strftime("%H:%M")
    return moment.strftime("%d.%m %H:%M")


@app.template_filter("money")
def format_money(amount: int) -> str:
    """3450 → «3 450 ₽» (пробелы неразрывные, чтобы сумма не переносилась)."""
    return f"{amount:,}".replace(",", "\u00a0") + "\u00a0₽"


@app.template_filter("status")
def format_status(status: str) -> str:
    """«в пути» → «в пути 🚗», как в боте."""
    return f"{status} {STATUS_EMOJI.get(status, '')}".rstrip()


@app.template_filter("tel")
def format_tel(phone: str) -> str:
    """Телефон для ссылки tel: — только «+» и цифры."""
    return "".join(ch for ch in phone if ch.isdigit() or ch == "+")


@app.errorhandler(400)
def bad_request(error):
    return render_template("error.html", message="Форма устарела, обновите страницу"), 400


@app.errorhandler(403)
def forbidden(error):
    return render_template("error.html", message="Недостаточно прав"), 403


@app.errorhandler(404)
def not_found(error):
    return render_template("error.html", message="Страница не найдена"), 404


# ---------- Страницы ----------

@app.route("/login", methods=["GET", "POST"])
def login():
    if g.staff:
        return redirect(url_for("orders"))
    if request.method == "GET":
        return render_template("login.html")

    login_name = request.form.get("login", "").strip().lower()
    password = request.form.get("password", "")
    ip = request.remote_addr or ""

    # Во время блокировки пароль не проверяем, даже правильный
    if security.seconds_blocked(ip, login_name):
        return render_blocked(ip, login_name)

    staff = db.get_staff_by_login(login_name)
    password_ok = check_password_hash(staff["password_hash"] if staff else DUMMY_HASH, password)
    if not (staff and staff["active"] and password_ok):
        db.add_failed_attempt(ip, login_name)
        # Если это была пятая ошибка — сразу говорим про паузу
        if security.seconds_blocked(ip, login_name):
            return render_blocked(ip, login_name)
        return render_template(
            "login.html", error="Неверный логин или пароль", login=login_name
        ), 401

    db.clear_failures(ip, login_name)
    # Новая сессия с нуля: старый CSRF-токен и всё прочее выбрасываем
    session.clear()
    session.permanent = True
    session["staff_id"] = staff["id"]
    session["logged_in_at"] = db.now_moscow().isoformat()
    return redirect(url_for("orders"))


def render_blocked(ip: str, login_name: str):
    minutes = -(-security.seconds_blocked(ip, login_name) // 60)  # вверх до целой минуты
    return render_template(
        "login.html",
        error=f"Слишком много попыток. Попробуйте через {minutes} мин.",
        login=login_name,
    ), 429


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def list_params() -> dict:
    """Фильтр, поиск и страница из адреса (?status=…&q=…&page=…).
    Неправильные значения молча заменяются на обычные."""
    status = request.args.get("status", "")
    page = request.args.get("page", "")
    return {
        "status": status if status in STATUS_TABS else "active",
        "q": request.args.get("q", "").strip()[:100],
        "page": int(page) if page.isdigit() and int(page) > 0 else 1,
    }


def list_context() -> dict:
    """Всё, что нужно шаблону списка заказов."""
    params = list_params()
    counts = db.count_by_status(params["q"])
    tab_counts = {
        key: sum(counts.get(s, 0) for s in statuses) if statuses else sum(counts.values())
        for key, (title, statuses) in STATUS_TABS.items()
    }
    pages = max(1, math.ceil(tab_counts[params["status"]] / ORDERS_PER_PAGE))
    params["page"] = min(params["page"], pages)
    orders = db.list_orders(
        STATUS_TABS[params["status"]][1], params["q"],
        ORDERS_PER_PAGE, (params["page"] - 1) * ORDERS_PER_PAGE,
    )
    return {
        "params": params,
        "tabs": STATUS_TABS,
        "tab_counts": tab_counts,
        "pages": pages,
        "orders": orders,
        "last_order_id": db.last_order_id(),
    }


@app.route("/")
@login_required
def orders():
    return render_template("orders.html", **list_context())


@app.route("/orders/fresh")
@login_required
def orders_fresh():
    """Свежий список без шапки и поиска — его раз в 15 секунд берёт orders.js."""
    response = app.make_response(render_template("_orders_list.html", **list_context()))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/orders/<int:order_id>")
@login_required
def order(order_id):
    found = db.get_order(order_id)
    if found is None:
        return render_template("error.html", message=f"Заказ №{order_id} не найден"), 404
    return render_template("order.html", order=found, params=list_params())


# ---------- Команда для сервера ----------

def check_login(value: str) -> str:
    value = value.strip().lower()
    if not value or " " in value:
        raise click.BadParameter("логин не может быть пустым или содержать пробелы")
    return value


def check_name(value: str) -> str:
    value = value.strip()
    if not value:
        raise click.BadParameter("имя не может быть пустым")
    return value


def check_password(value: str) -> str:
    if len(value) < MIN_PASSWORD_LENGTH:
        raise click.BadParameter(f"пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов")
    return value


@app.cli.command("create-admin")
@click.option(
    "--role", type=click.Choice(db.ROLES), default=db.ROLE_ADMIN, show_default=True,
    help="Роль нового сотрудника.",
)
def create_admin(role):
    """Создать сотрудника (по умолчанию — админа)."""
    login_name = click.prompt("Логин", value_proc=check_login)
    name = click.prompt("Имя (как показывать в панели)", value_proc=check_name)
    password = click.prompt(
        "Пароль", hide_input=True, confirmation_prompt="Пароль ещё раз",
        value_proc=check_password,
    )
    try:
        db.create_staff(login_name, name, generate_password_hash(password), role)
    except sqlite3.IntegrityError:
        raise click.ClickException(f"Логин «{login_name}» уже занят")
    click.echo(f"Готово: {name} ({login_name}) — {ROLE_NAMES[role]}")
