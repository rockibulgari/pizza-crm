"""Общая подготовка автотестов.

Тесты работают на своей временной базе (схема таблицы orders — как у бота)
с поддельным токеном. Настоящие orders.db и .env не трогаются, а в Telegram
ничего не отправляется: любая попытка сходить в сеть роняет тест.
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import urllib.request
from pathlib import Path

import pytest

# Настройки задаём ДО импорта панели: config.py читает их при импорте
TMP = Path(tempfile.mkdtemp(prefix="pizza-crm-tests-"))
DB_FILE = TMP / "orders.db"
(TMP / "bot.env").write_text("BOT_TOKEN=123456:TEST-TOKEN\n", encoding="utf-8")
os.environ["ORDERS_DB"] = str(DB_FILE)
os.environ["BOT_ENV_FILE"] = str(TMP / "bot.env")
os.environ["SECRET_KEY"] = "test-secret-key"

# Таблица заказов — как её создаёт бот (pizza-bot/db.py)
ORDERS_SCHEMA = """
    CREATE TABLE orders (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        client_name TEXT    NOT NULL,
        phone       TEXT    NOT NULL,
        address     TEXT    NOT NULL,
        comment     TEXT    NOT NULL,
        payment     TEXT    NOT NULL,
        items       TEXT    NOT NULL,
        subtotal    INTEGER NOT NULL,
        discount    INTEGER NOT NULL,
        total       INTEGER NOT NULL,
        created_at  TEXT    NOT NULL,
        status      TEXT    NOT NULL
    )
"""


def create_orders_table():
    for suffix in ("", "-wal", "-shm"):
        Path(str(DB_FILE) + suffix).unlink(missing_ok=True)
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(ORDERS_SCHEMA)
    conn.close()


create_orders_table()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module  # noqa: E402
import db  # noqa: E402
import telegram  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

HTTPS = "https://localhost"  # cookie сессии — только по HTTPS


# ---------- База ----------

@pytest.fixture(autouse=True)
def fresh_db():
    """Каждый тест — с чистой базой: таблица заказов и пустые таблицы панели."""
    create_orders_table()
    db.init_db()
    yield


def add_order(created_at: str, status: str = "новый", items=None, user_id: int = 111,
              client_name: str = "Клиент", phone: str = "+7 (928) 123-45-67",
              address: str = "ул. Ленина, 1") -> int:
    """Заказ в базу так же, как его записывает бот. Скидка 15% на пиццы 40 см от 3 штук."""
    items = items or [{"name": "Маргарита", "size": 30, "qty": 1, "price": 450}]
    subtotal = sum(i["qty"] * i["price"] for i in items)
    big = [i for i in items if i["size"] == 40]
    big_sum = sum(i["qty"] * i["price"] for i in big)
    total = (subtotal * 100 - big_sum * 15) // 100 if sum(i["qty"] for i in big) >= 3 else subtotal
    with db.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO orders (user_id, client_name, phone, address, comment, payment,
                                items, subtotal, discount, total, created_at, status)
            VALUES (?, ?, ?, ?, '', 'Наличными курьеру', ?, ?, ?, ?, ?, ?)
            """,
            (user_id, client_name, phone, address, json.dumps(items, ensure_ascii=False),
             subtotal, subtotal - total, total, created_at, status),
        )
    conn.close()
    return cursor.lastrowid


def add_staff(login: str, password: str = "password1", role: str = db.ROLE_OPERATOR,
              name: str | None = None) -> int:
    return db.create_staff(login, name or login.capitalize(), generate_password_hash(password), role)


# ---------- Telegram ----------

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Никаких настоящих запросов в интернет из тестов."""
    def forbidden(*args, **kwargs):
        raise AssertionError("тест попытался сходить в сеть")
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)


@pytest.fixture
def sent(monkeypatch):
    """Подменённая отправка в Telegram. sent.messages — что ушло бы клиентам,
    sent.ok = False — Telegram «не доставил»."""
    class Sent:
        ok = True
        messages: list = []

    box = Sent()
    box.messages = []

    def fake_send(chat_id, text):
        box.messages.append((chat_id, text))
        return box.ok

    monkeypatch.setattr(telegram, "send_message", fake_send)
    return box


# ---------- «Браузер» ----------

class Browser:
    """Тестовый клиент, который ведёт себя как браузер сотрудника."""

    def __init__(self):
        self.client = app_module.app.test_client()

    def get(self, url):
        return self.client.get(url, base_url=HTTPS)

    def csrf(self) -> str | None:
        with self.client.session_transaction(base_url=HTTPS) as session:
            return session.get("csrf_token")

    def post(self, url, data=None, csrf: str | None = "auto"):
        data = dict(data or {})
        if csrf == "auto":
            if not self.csrf():
                # Токен появляется в сессии, когда показана страница с формой:
                # у гостя — вход, у вошедшего — любая страница (форма «Выйти»)
                self.get("/login")
                self.get("/")
            csrf = self.csrf()
        if csrf is not None:
            data["csrf_token"] = csrf
        return self.client.post(url, data=data, base_url=HTTPS)

    def login_page_token(self) -> str:
        page = self.get("/login").get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

    def login(self, login: str, password: str):
        token = self.login_page_token()
        return self.post("/login", {"login": login, "password": password}, csrf=token)

    def logged_in(self) -> bool:
        return self.get("/").status_code == 200

    def flashes(self, url: str = "/") -> list[tuple[str, str]]:
        page = self.get(url).get_data(as_text=True)
        return flashes_in(page)


def flashes_in(page: str) -> list[tuple[str, str]]:
    return re.findall(r'class="flash flash-(\w+)"[^>]*>([^<]+)<', page)


@pytest.fixture
def browser():
    return Browser()


def logged_in_as(login: str, password: str = "password1", role: str = db.ROLE_OPERATOR) -> Browser:
    add_staff(login, password, role)
    b = Browser()
    assert b.login(login, password).status_code == 302
    return b


@pytest.fixture
def admin():
    return logged_in_as("boss", role=db.ROLE_ADMIN)


@pytest.fixture
def operator():
    return logged_in_as("anna", role=db.ROLE_OPERATOR)


def pytest_sessionfinish(session, exitstatus):
    """Временную базу тестов убираем за собой."""
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
