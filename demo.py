"""Демо-версия панели для портфолио: выдуманные заказы и демо-сотрудники.

Включается настройкой DEMO=1 в .env. Демо работает со своей базой (demo.db),
к настоящей orders.db и к Telegram отношения не имеет.

Пересоздать демо-базу (это же каждые 2 часа делает таймер на сервере):
    flask --app app reset-demo
"""
import json
import random
from contextlib import closing
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

import db

# Пароль публичный — его показывают на странице входа и на сайте-портфолио
DEMO_PASSWORD = "demo2026"
DEMO_STAFF = (
    ("demo-admin", "Демо Админ", db.ROLE_ADMIN),
    ("demo-operator", "Демо Оператор", db.ROLE_OPERATOR),
)

# Все телефоны демо — +7 900 000-00-XX. По этому признаку демо отличает
# свою базу от настоящей и на настоящей работать отказывается
PHONE_PREFIX = "+7 900 000-00-"

DAYS = 14
MIN_ORDERS, MAX_ORDERS = 40, 60

# Таблица заказов — как её создаёт бот (pizza-bot/db.py). В демо бота нет,
# поэтому таблицу создаёт сама панель
ORDERS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS orders (
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

# Меню и правила скидки — как в боте
PIZZAS = {
    "Маргарита": {30: 450, 40: 690},
    "Грибная": {30: 490, 40: 740},
    "Пепперони": {30: 550, 40: 820},
    "Четыре сыра": {30: 590, 40: 860},
    "Мясная": {30: 650, 40: 920},
    "Фирменная «Назрань»": {30: 690, 40: 950},
}
DISCOUNT_SIZE = 40
DISCOUNT_MIN_COUNT = 3
DISCOUNT_PERCENT = 15
PAYMENTS = ("Наличными курьеру", "Картой курьеру")

NAMES = (
    "Ахмед", "Мадина", "Ислам", "Лейла", "Магомед", "Хава", "Руслан", "Зарина",
    "Адам", "Аминат", "Тимур", "Седа", "Ибрагим", "Фатима", "Беслан", "Луиза",
    "Муса", "Элина", "Хасан", "Марьям", "Алихан", "Петимат",
)
STREETS = (
    "ул. Московская", "ул. Муталиева", "ул. Картоева", "ул. Осканова", "ул. Чеченская",
    "ул. Фабричная", "ул. Мира", "ул. Садовая", "ул. Школьная", "пр. Базоркина",
)
COMMENTS = (
    "Позвоните за 10 минут", "Домофон не работает, позвоните", "Подъезд 2, этаж 5",
    "Сдача с 5000", "Побыстрее, пожалуйста", "Оставить у двери",
)

# Сколько заказов в какой час. Доставка круглосуточная, но ночью заказов
# почти нет; днём — обед, вечером — пик
HOUR_WEIGHTS = {
    0: 2, 1: 1, 2: 0.3, 3: 0.2, 4: 0.2, 5: 0.2, 6: 0.3, 7: 0.5, 8: 1, 9: 1,
    10: 2, 11: 2, 12: 4, 13: 4, 14: 3, 15: 2, 16: 2,
    17: 3, 18: 6, 19: 8, 20: 8, 21: 7, 22: 4, 23: 3,
}
WEEKEND = (4, 5, 6)  # пт, сб, вс — заказов почти вдвое больше
WEEKEND_WEIGHT = 1.8

# Путь заказа по статусам: «новый» → … → «доставлен»
PATH = db.STATUSES[:5]
CANCELLED = db.STATUSES[5]
# Таймер пересоздаёт базу каждые RESET_EVERY. Самые свежие заказы — в последние
# FRESH перед пересозданием, поэтому в любой момент последний заказ сделан
# не раньше чем FRESH + RESET_EVERY (3 часа) назад
RESET_EVERY = timedelta(hours=2)
FRESH = timedelta(hours=1)
# Статусы свежих заказов (они ещё в работе), начиная с последнего.
# Сколько их — зависит от времени суток: вечером больше, ночью два
ACTIVE_NEWEST_FIRST = {
    2: ("новый", "в пути"),
    3: ("новый", "готовится", "в пути"),
    4: ("новый", "принят", "готовится", "в пути"),
    5: ("новый", "новый", "принят", "готовится", "в пути"),
    6: ("новый", "новый", "принят", "готовится", "в пути", "в пути"),
}


def make_clients(rng: random.Random) -> list[dict]:
    """Постоянные клиенты: у каждого своё имя, телефон и адрес."""
    clients = []
    for number, name in enumerate(NAMES, start=1):
        address = f"{rng.choice(STREETS)}, {rng.randint(1, 120)}"
        if rng.random() < 0.4:
            address += f", кв. {rng.randint(1, 80)}"
        clients.append({"name": name, "phone": f"{PHONE_PREFIX}{number:02d}", "address": address})
    return clients


def make_items(rng: random.Random) -> list[dict]:
    """Состав заказа. Иногда — большой заказ на компанию (3+ пиццы 40 см, со скидкой)."""
    if rng.random() < 0.15:
        names = rng.sample(list(PIZZAS), rng.randint(2, 3))
        return [{"name": n, "size": 40, "qty": rng.randint(1, 3) + (i == 0), "price": PIZZAS[n][40]}
                for i, n in enumerate(names)]
    items = []
    for name in rng.sample(list(PIZZAS), rng.choice((1, 1, 1, 2, 2, 3))):
        size = rng.choice((30, 30, 40))
        items.append({"name": name, "size": size, "qty": rng.choice((1, 1, 1, 2)),
                      "price": PIZZAS[name][size]})
    return items


def totals(items: list[dict]) -> tuple[int, int, int]:
    """(сумма без скидки, скидка, итого) — тем же расчётом, что в боте."""
    subtotal = sum(i["qty"] * i["price"] for i in items)
    big = [i for i in items if i["size"] == DISCOUNT_SIZE]
    if sum(i["qty"] for i in big) >= DISCOUNT_MIN_COUNT:
        big_sum = sum(i["qty"] * i["price"] for i in big)
        total = (subtotal * 100 - big_sum * DISCOUNT_PERCENT) // 100
    else:
        total = subtotal
    return subtotal, subtotal - total, total


def fresh_moments(rng: random.Random, now: datetime) -> list[datetime]:
    """Свежие заказы — за последний час (FRESH), примерно через равные промежутки.
    Сколько — по времени суток: в пик 5–6, днём 3–4, ночью 2."""
    weight = HOUR_WEIGHTS[(now - FRESH / 2).hour]
    count = rng.randint(5, 6) if weight >= 4 else rng.randint(3, 4) if weight >= 2 else 2
    start, end = now - FRESH, now - timedelta(minutes=1)
    step = (end - start) / count
    return [start + step * n + timedelta(seconds=rng.uniform(0, step.total_seconds()))
            for n in range(count)]


def moment_in(rng: random.Random, day, hours: list[int], before: datetime) -> datetime:
    """Случайное время дня day в один из часов hours (чаще в «тяжёлые»), раньше before."""
    while True:
        hour = rng.choices(hours, [HOUR_WEIGHTS[h] for h in hours])[0]
        moment = datetime(day.year, day.month, day.day, hour, rng.randint(0, 59),
                          rng.randint(0, 59), tzinfo=before.tzinfo)
        if moment < before:
            return moment


def past_moments(rng: random.Random, now: datetime, count: int, before: datetime) -> list[datetime]:
    """count заказов за последние DAYS дней раньше before. По дням — пропорционально
    весу дня (выходные тяжелее, неполный сегодняшний день легче), чтобы не было
    пустых будней; внутри дня — вечером чаще."""
    first_day = now.date() - timedelta(days=DAYS - 1)
    days, hours, weights = [], [], []
    for n in range(DAYS):
        day = first_day + timedelta(days=n)
        open_hours = [h for h in HOUR_WEIGHTS
                      if datetime(day.year, day.month, day.day, h, tzinfo=now.tzinfo) < before]
        if open_hours:
            days.append(day)
            hours.append(open_hours)
            weights.append((WEEKEND_WEIGHT if day.weekday() in WEEKEND else 1)
                           * sum(HOUR_WEIGHTS[h] for h in open_hours))
    # Целая часть ожидаемого числа — каждому дню, остаток — случайным дням
    counts = [int(count * w / sum(weights)) for w in weights]
    for index in rng.choices(range(len(days)), weights, k=count - sum(counts)):
        counts[index] += 1
    return [moment_in(rng, day, day_hours, before)
            for day, day_hours, day_count in zip(days, hours, counts) for _ in range(day_count)]


def history(rng: random.Random, created_at: datetime, status: str, now: datetime) -> list[dict]:
    """Смены статуса от демо-сотрудников, которые привели заказ к status."""
    if status == CANCELLED:
        steps = [CANCELLED] if rng.random() < 0.5 else ["принят", CANCELLED]
    else:
        steps = list(PATH[1:PATH.index(status) + 1])
    entries, old, moment = [], db.STATUS_NEW, created_at
    for new in steps:
        # Доставка дольше остальных шагов
        minutes = rng.randint(25, 50) if new == "доставлен" else rng.randint(2, 12)
        moment = min(moment + timedelta(minutes=minutes, seconds=rng.randint(0, 59)), now)
        entries.append({"old": old, "new": new, "at": moment, "staff": rng.randrange(len(DEMO_STAFF))})
        old = new
    return entries


def generate(now: datetime, rng: random.Random) -> list[dict]:
    """Выдуманные заказы за последние DAYS дней, старые сверху."""
    clients = make_clients(rng)
    count = rng.randint(MIN_ORDERS, MAX_ORDERS)
    # Свежие заказы ещё в работе. Из остальных несколько отменены, прочие доставлены
    fresh = fresh_moments(rng, now)
    active = len(fresh)
    moments = sorted(past_moments(rng, now, count - active, fresh[0])) + fresh

    statuses = ["доставлен"] * (len(moments) - active) + list(ACTIVE_NEWEST_FIRST[active])[::-1]
    for index in rng.sample(range(len(moments) - active), rng.randint(3, 5)):
        statuses[index] = CANCELLED

    orders = []
    for moment, status in zip(moments, statuses):
        client = rng.choice(clients)
        items = make_items(rng)
        subtotal, discount, total = totals(items)
        orders.append({
            "client_name": client["name"],
            "phone": client["phone"],
            "address": client["address"],
            "comment": rng.choice(COMMENTS) if rng.random() < 0.25 else "",
            "payment": rng.choice(PAYMENTS),
            "items": items,
            "subtotal": subtotal,
            "discount": discount,
            "total": total,
            "created_at": moment,
            "status": status,
            "history": history(rng, moment, status, now),
        })
    return orders


class NotDemoDatabase(RuntimeError):
    """В базе есть заказы с настоящими телефонами — это не демо-база."""


def check_is_demo(conn):
    real = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE substr(phone, 1, ?) != ?",
        (len(PHONE_PREFIX), PHONE_PREFIX),
    ).fetchone()[0]
    if real:
        raise NotDemoDatabase(
            f"в базе {real} заказов с телефонами не вида {PHONE_PREFIX}XX — "
            "похоже, это настоящая база, демо с ней работать не будет"
        )


def reset(only_if_empty: bool = False, now: datetime | None = None) -> dict | None:
    """Стирает всё в демо-базе и заполняет её заново — одной транзакцией:
    посетитель никогда не увидит полупустую базу.
    only_if_empty — только если сотрудников ещё нет (первый запуск).
    Возвращает {"orders", "cancelled"} или None, если ничего не делали."""
    now = now or db.now_moscow()
    # В один и тот же час — одни и те же данные
    rng = random.Random(now.strftime("%Y%m%d%H"))
    orders = generate(now, rng)
    staff_hash = generate_password_hash(DEMO_PASSWORD)

    with closing(db.connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        check_is_demo(conn)
        if only_if_empty and conn.execute("SELECT COUNT(*) FROM staff").fetchone()[0]:
            return None
        for table in ("status_log", "login_attempts", "orders", "staff"):
            conn.execute(f"DELETE FROM {table}")
        # Номера заказов и сотрудников — снова с 1. Сотрудники получают те же id,
        # поэтому открытые сессии посетителей переживают ночное пересоздание
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('orders', 'staff', 'status_log')")
        for login, name, role in DEMO_STAFF:
            conn.execute(
                "INSERT INTO staff (login, name, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                (login, name, staff_hash, role, now.isoformat()),
            )
        staff_ids = [row[0] for row in conn.execute("SELECT id FROM staff ORDER BY id")]
        for order in orders:
            cursor = conn.execute(
                """
                INSERT INTO orders (user_id, client_name, phone, address, comment, payment,
                                    items, subtotal, discount, total, created_at, status)
                VALUES (0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (order["client_name"], order["phone"], order["address"], order["comment"],
                 order["payment"], json.dumps(order["items"], ensure_ascii=False),
                 order["subtotal"], order["discount"], order["total"],
                 order["created_at"].isoformat(), order["status"]),
            )
            for entry in order["history"]:
                # notified = 0: в демо клиентам ничего не отправляется
                conn.execute(
                    """
                    INSERT INTO status_log (order_id, staff_id, old_status, new_status, changed_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (cursor.lastrowid, staff_ids[entry["staff"]], entry["old"], entry["new"],
                     entry["at"].isoformat()),
                )
    return {"orders": len(orders), "cancelled": sum(o["status"] == CANCELLED for o in orders)}


def prepare():
    """При запуске панели в демо: создать demo.db, если её нет, и заполнить
    при первом запуске. На базе с настоящими заказами — отказ (NotDemoDatabase)."""
    with closing(db.connect()) as conn, conn:
        conn.execute(ORDERS_SCHEMA)
        check_is_demo(conn)
    db.init_db()
    reset(only_if_empty=True)
