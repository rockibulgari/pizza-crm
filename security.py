"""Вход, роли, защита форм от CSRF и защита от подбора пароля."""
import hmac
import math
import secrets
from datetime import timedelta
from functools import wraps

from flask import abort, g, redirect, request, session, url_for

import db

# Защита от подбора пароля: после MAX_FAILURES неудач за FAILURE_WINDOW
# с одного IP или в один логин вход закрыт на BLOCK_TIME с последней неудачи
MAX_FAILURES = 5
FAILURE_WINDOW = timedelta(minutes=5)
BLOCK_TIME = timedelta(minutes=5)


# ---------- Кто вошёл ----------

def load_current_staff():
    """Перед каждым запросом: кладёт в g.staff вошедшего сотрудника или None.
    Сотрудник каждый раз читается из базы, поэтому отключённого выкидывает сразу,
    а после смены пароля (session_version выросла) — выкидывает все старые сессии.
    Сессии, открытые до появления session_version, считаются версией 0."""
    g.staff = None
    staff_id = session.get("staff_id")
    if staff_id is None:
        return
    staff = db.get_staff(staff_id)
    if (
        staff is None
        or not staff["active"]
        or session.get("session_version", 0) != staff["session_version"]
    ):
        session.clear()
        return
    g.staff = staff


def login_required(view):
    """Страница только для вошедших; остальных — на страницу входа."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.staff is None:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    """Страница только для админа; оператору — ошибка 403."""
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if g.staff["role"] != db.ROLE_ADMIN:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


# ---------- CSRF ----------

def csrf_token() -> str:
    """Токен для скрытого поля форм. Создаётся один раз на сессию."""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def check_csrf():
    """Перед каждым POST: токен из формы (или заголовка X-CSRF-Token
    для запросов из JavaScript) должен совпасть с токеном в сессии."""
    if request.method != "POST":
        return
    expected = session.get("csrf_token")
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not expected or not sent or not hmac.compare_digest(expected, sent):
        abort(400)


# ---------- Подбор пароля ----------

def seconds_blocked(ip: str, login: str) -> int:
    """Сколько секунд ещё закрыт вход для этого IP или логина (0 — не закрыт)."""
    now = db.now_moscow()
    since = now - FAILURE_WINDOW
    left = timedelta(0)
    for field, value in (("ip", ip), ("login", login)):
        count, last = db.recent_failures(field, value, since)
        if count >= MAX_FAILURES:
            left = max(left, last + BLOCK_TIME - now)
    return math.ceil(left.total_seconds())
