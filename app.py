"""Веб-панель (мини-CRM) пиццерии «Пицца Назрань».

Локальный запуск:  flask --app app run
Первый админ:      flask --app app create-admin
"""
import logging
import math
import sqlite3
from datetime import date, datetime, timedelta

import click
from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import config
import db
import security
import stats
import telegram
from security import admin_required, login_required

SESSION_LIFETIME = timedelta(hours=12)  # одна смена, потом вход заново
MIN_PASSWORD_LENGTH = 8

ROLE_NAMES = {db.ROLE_ADMIN: "Администратор", db.ROLE_OPERATOR: "Оператор"}

ORDERS_PER_PAGE = 50

# Статус в базе → ключ для адреса и CSS (ключи как в bot.py) и смайл как в боте
STATUS_KEYS = dict(zip(db.STATUSES, ("new", "accepted", "cooking", "on_way", "delivered", "cancelled")))
STATUS_EMOJI = dict(zip(db.STATUSES, ("🆕", "✅", "🍕", "🚗", "✔️", "❌")))

# Кнопки в карточке заказа — как в боте: «✅ Принят» … «❌ Отменён».
# Статус «новый» вернуть нельзя, в боте такой кнопки тоже нет
STATUS_BUTTONS = {
    STATUS_KEYS[status]: (status, f"{STATUS_EMOJI[status]} {status.capitalize()}")
    for status in db.STATUSES if status != db.STATUS_NEW
}
STATUS_CANCELLED = "отменён"

# Готовые периоды статистики: ключ в адресе (?period=…) → (надпись, сколько дней)
STAT_PERIODS = {"today": ("Сегодня", 1), "7": ("7 дней", 7), "30": ("30 дней", 30)}
STAT_DEFAULT_PERIOD = "7"
STAT_MAX_DAYS = 366

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


WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


@app.template_filter("day")
def format_day(day: date, weekday: bool = False) -> str:
    """Дата для статистики: «21.10» или «21.10, вт»."""
    text = day.strftime("%d.%m")
    return f"{text}, {WEEKDAYS[day.weekday()]}" if weekday else text


MONTHS = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


@app.template_filter("month")
def format_month(day: date) -> str:
    """Короткий месяц для подписи под графиком: «окт»."""
    return MONTHS[day.month - 1]


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
    session["session_version"] = staff["session_version"]
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
    return render_template(
        "order.html", order=found, params=list_params(),
        status_buttons=STATUS_BUTTONS, history=db.status_history(order_id),
        cancelled_status=STATUS_CANCELLED,
    )


@app.post("/orders/<int:order_id>/status")
@login_required
def change_status(order_id):
    """Кнопка статуса в карточке заказа. Сначала база, потом сообщение клиенту."""
    # Фильтр списка (?status=…&q=…&page=…) передаём дальше, чтобы «← Заказы» вёл туда же
    params = list_params()
    back = redirect(url_for(
        "order", order_id=order_id, status=params["status"], q=params["q"] or None,
        page=params["page"] if params["page"] > 1 else None,
    ))
    key = request.form.get("status", "")
    if key not in STATUS_BUTTONS:
        flash("Неизвестный статус", "error")
        return back
    new_status = STATUS_BUTTONS[key][0]

    try:
        result = db.change_status(order_id, new_status, g.staff["id"])
    except sqlite3.Error:
        logging.exception("Не удалось сменить статус заказа №%s", order_id)
        flash("Не удалось сменить статус, попробуйте ещё раз", "error")
        return back
    if result is None:
        return render_template("error.html", message=f"Заказ №{order_id} не найден"), 404
    if result["log_id"] is None:
        flash("Статус уже такой", "info")
        return back

    # Тот же текст, что присылает бот: «Ваш заказ №7 в пути 🚗»
    text = f"Ваш заказ №{order_id} {new_status} {STATUS_EMOJI[new_status]}"
    if telegram.send_message(result["user_id"], text):
        try:
            db.mark_notified(result["log_id"])
        except sqlite3.Error:
            # Сообщение ушло, статус сохранён — не страшно, если пометка не записалась
            logging.exception("Не удалось отметить уведомление по заказу №%s", order_id)
        flash("Статус изменён, клиент уведомлён", "success")
    else:
        flash("Статус изменён, но клиенту сообщение не доставлено", "warning")
    return back


def stats_period() -> tuple[str | None, date, date, str | None]:
    """Период из адреса: ?period=today|7|30 или ?from=ГГГГ-ММ-ДД&to=ГГГГ-ММ-ДД.
    Возвращает (ключ готового периода или None, с, по, ошибка или None).
    При ошибке — период по умолчанию и понятное сообщение."""
    today = db.now_moscow().date()
    error = None
    if request.args.get("from") or request.args.get("to"):
        try:
            date_from = date.fromisoformat(request.args.get("from", ""))
            date_to = date.fromisoformat(request.args.get("to", ""))
        except ValueError:
            error = "Укажите обе даты периода"
        else:
            if date_from > date_to:
                error = "Дата «с» позже даты «по»"
            elif (date_to - date_from).days + 1 > STAT_MAX_DAYS:
                error = "Период не может быть длиннее года"
            else:
                return None, date_from, date_to, None
    key = request.args.get("period", STAT_DEFAULT_PERIOD)
    if key not in STAT_PERIODS:
        key = STAT_DEFAULT_PERIOD
    days = STAT_PERIODS[key][1]
    return key, today - timedelta(days=days - 1), today, error


@app.route("/stats")
@admin_required
def stats_page():
    period, date_from, date_to, error = stats_period()
    if error:
        flash(error + ". Показана статистика за 7 дней.", "warning")
    data = stats.calculate(db.orders_between(date_from, date_to), date_from, date_to, STATUS_CANCELLED)
    return render_template(
        "stats.html", stats=data, period=period, periods=STAT_PERIODS,
        date_from=date_from, date_to=date_to, today=db.now_moscow().date(),
    )


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


def login_argument(ctx, param, value: str) -> str:
    """Логин из командной строки — по тем же правилам, что при создании."""
    return check_login(value)


@app.cli.command("set-password")
@click.argument("login_name", metavar="ЛОГИН", callback=login_argument)
def set_password(login_name):
    """Сменить пароль сотруднику. Все его открытые сессии завершатся."""
    staff = db.get_staff_by_login(login_name)
    if staff is None:
        raise click.ClickException(f"Сотрудника с логином «{login_name}» нет")
    password = click.prompt(
        "Новый пароль", hide_input=True, confirmation_prompt="Новый пароль ещё раз",
        value_proc=check_password,
    )
    if not db.set_password(login_name, generate_password_hash(password)):
        raise click.ClickException(f"Сотрудника с логином «{login_name}» нет")
    click.echo(f"Пароль для {staff['name']} ({login_name}) изменён. "
               "Все открытые сессии этого сотрудника завершены — нужно войти заново.")


@app.cli.command("rename-login")
@click.argument("old_login", metavar="СТАРЫЙ", callback=login_argument)
@click.argument("new_login", metavar="НОВЫЙ", callback=login_argument)
def rename_login(old_login, new_login):
    """Сменить логин сотрудника. Пароль и открытые сессии не меняются."""
    if old_login == new_login:
        raise click.ClickException("Новый логин совпадает со старым")
    try:
        found = db.rename_login(old_login, new_login)
    except sqlite3.IntegrityError:
        raise click.ClickException(f"Логин «{new_login}» уже занят")
    if not found:
        raise click.ClickException(f"Сотрудника с логином «{old_login}» нет")
    click.echo(f"Готово: логин «{old_login}» заменён на «{new_login}»")
