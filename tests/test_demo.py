"""Демо-версия: выдуманные данные, демо-вход, запреты, Telegram отключён, плашка."""
import random
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import app as app_module
import config
import db
import demo
import telegram
from conftest import Browser, add_order, flashes_in

PHONE = re.compile(r"^\+7 900 000-00-\d\d$")
ADDRESS = re.compile(r"^(ул\.|пр\.) [А-Яа-я]+, \d+(, кв\. \d+)?$")


@pytest.fixture
def demo_mode(monkeypatch):
    """Панель в режиме демо с заполненной демо-базой."""
    monkeypatch.setattr(config, "DEMO", True)
    demo.reset()


def demo_login(login="demo-admin") -> Browser:
    b = Browser()
    assert b.login(login, demo.DEMO_PASSWORD).status_code == 302
    return b


@pytest.fixture
def demo_admin(demo_mode):
    return demo_login("demo-admin")


# ---------- Генератор ----------

TIMER = Path(__file__).resolve().parent.parent / "deploy" / "pizza-crm-demo-reset.timer"


def reset_hours() -> list[int]:
    """Часы пересоздания по Москве из таймера: «*-*-* 01/2:00:00 Europe/Moscow» → 1, 3, … 23."""
    rule = re.search(r"^OnCalendar=\*-\*-\* (\d+)/(\d+):00:00 Europe/Moscow$",
                     TIMER.read_text(encoding="utf-8"), re.M)
    assert rule, "в таймере нет OnCalendar вида «*-*-* ЧЧ/2:00:00 Europe/Moscow»"
    first, step = int(rule[1]), int(rule[2])
    assert timedelta(hours=step) == demo.RESET_EVERY
    return list(range(first, 24, step))


# Будний день и выходной; каждый час, когда срабатывает таймер, и между ними
DAYS = (datetime(2026, 10, 21, tzinfo=db.MOSCOW), datetime(2026, 10, 24, tzinfo=db.MOSCOW))
MOMENTS = [day + timedelta(hours=h, minutes=m) for day in DAYS for h in range(24) for m in (0, 37)]


@pytest.mark.parametrize("now", MOMENTS, ids=lambda m: m.strftime("%a-%H%M"))
def test_generated_orders_look_real(now):
    for seed in range(5):
        orders = demo.generate(now, random.Random(seed))
        assert demo.MIN_ORDERS <= len(orders) <= demo.MAX_ORDERS
        first_day = now.date() - timedelta(days=demo.DAYS - 1)
        for order in orders:
            assert first_day <= order["created_at"].date() and order["created_at"] < now
            assert PHONE.match(order["phone"]), order["phone"]
            assert ADDRESS.match(order["address"]), order["address"]
            assert order["client_name"] in demo.NAMES
            assert order["status"] in db.STATUSES
            for item in order["items"]:
                assert item["price"] == demo.PIZZAS[item["name"]][item["size"]]
            assert order["total"] == order["subtotal"] - order["discount"]
            for entry in order["history"]:
                assert order["created_at"] <= entry["at"] <= now
            if order["history"]:
                assert order["history"][-1]["new"] == order["status"]
        assert orders == sorted(orders, key=lambda o: o["created_at"])
        statuses = [o["status"] for o in orders]
        assert 3 <= statuses.count("отменён") <= 5
        assert {"новый", "в пути", "доставлен"} <= set(statuses)
        # Заказы в работе — только последние, за последний час, а не недельной давности
        active = [o for o in orders if o["status"] in db.ACTIVE_STATUSES]
        assert active == orders[-len(active):]
        assert [o["status"] for o in active][::-1] == list(demo.ACTIVE_NEWEST_FIRST[len(active)])
        assert all(now - demo.FRESH <= o["created_at"] for o in active)
        # Нет пустых дней (кроме сегодняшнего — он может только начаться)
        days = {o["created_at"].date() for o in orders}
        assert len(days | {now.date()}) == demo.DAYS


@pytest.mark.parametrize("day", DAYS, ids=lambda d: d.strftime("%a"))
def test_between_resets_orders_stay_fresh_and_today_not_empty(day):
    """Как на сервере: таймер пересоздаёт базу, посетитель смотрит в любую минуту
    до следующего пересоздания."""
    for hour in reset_hours():
        reset_at = day + timedelta(hours=hour)
        for seed in range(5):
            orders = demo.generate(reset_at, random.Random(seed))
            newest = orders[-1]["created_at"]
            # Свежие заказы — тем больше, чем оживлённее час
            fresh = [o for o in orders if o["status"] in db.ACTIVE_STATUSES]
            if demo.HOUR_WEIGHTS[hour - 1] >= 4:
                assert len(fresh) >= 5
            for minutes in range(0, int(demo.RESET_EVERY.total_seconds() // 60), 10):
                viewed = reset_at + timedelta(minutes=minutes)
                assert viewed - newest <= timedelta(hours=3), (reset_at, viewed, newest)
                if viewed.hour >= 10:
                    assert any(o["created_at"].date() == viewed.date() for o in orders), viewed


def test_realistic_time_of_day():
    """По всем пересозданиям за сутки: вечером больше всего заказов, ночью — единицы."""
    moments = [o["created_at"] for day in DAYS for hour in reset_hours() for seed in range(3)
               for o in demo.generate(day + timedelta(hours=hour), random.Random(seed))]
    evening = sum(18 <= m.hour <= 22 for m in moments)
    night = sum(2 <= m.hour <= 8 for m in moments)
    assert evening > len(moments) * 0.4
    assert night < len(moments) * 0.1
    assert evening > night * 5


def test_more_orders_on_weekends():
    now = datetime(2026, 10, 21, 13, 0, tzinfo=db.MOSCOW)
    moments = [o["created_at"] for seed in range(30)
               for o in demo.generate(now, random.Random(seed))
               if o["created_at"].date() < now.date()]  # сегодняшний день неполный
    weekend_days = sum(d.weekday() in demo.WEEKEND for d in {m.date() for m in moments})
    weekdays = len({m.date() for m in moments}) - weekend_days
    per_weekend_day = sum(m.weekday() in demo.WEEKEND for m in moments) / weekend_days
    per_weekday = sum(m.weekday() not in demo.WEEKEND for m in moments) / weekdays
    assert per_weekend_day > per_weekday * 1.4


def test_discount_like_in_bot():
    big = [{"name": "Пепперони", "size": 40, "qty": 3, "price": 820}]
    assert demo.totals(big) == (2460, 369, 2091)
    small = [{"name": "Пепперони", "size": 40, "qty": 2, "price": 820},
             {"name": "Маргарита", "size": 30, "qty": 2, "price": 450}]
    assert demo.totals(small) == (2540, 0, 2540)


# ---------- Пересоздание базы ----------

def test_reset_fills_db_and_replaces_everything(monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)
    first = demo.reset()
    assert demo.MIN_ORDERS <= first["orders"] <= demo.MAX_ORDERS
    assert db.last_order_id() == first["orders"]  # номера с 1
    # Посетитель что-то поменял — после пересоздания следов нет
    db.change_status(1, "отменён", db.get_staff_by_login("demo-admin")["id"])
    db.add_failed_attempt("1.2.3.4", "demo-admin")
    second = demo.reset(now=db.now_moscow() + timedelta(days=1))
    assert db.last_order_id() == second["orders"]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM staff").fetchone()[0] == 2
        assert conn.execute("SELECT MIN(id) FROM status_log").fetchone()[0] == 1
    conn.close()
    assert [s["login"] for s in db.list_staff()] == ["demo-admin", "demo-operator"]


def test_reset_keeps_visitor_sessions(demo_admin):
    demo.reset(now=db.now_moscow() + timedelta(days=1))
    assert demo_admin.logged_in()


def test_prepare_fills_only_empty_db(monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)
    demo.prepare()
    count = db.last_order_id()
    assert count >= demo.MIN_ORDERS
    db.create_staff("extra", "Extra", "x", db.ROLE_OPERATOR)
    demo.prepare()  # уже заполнена — не трогает
    assert db.get_staff_by_login("extra") is not None


def test_refuses_real_database(monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)
    order_id = add_order("2026-10-20T19:00:00+03:00")  # настоящий телефон +7 (928) …
    with pytest.raises(demo.NotDemoDatabase):
        demo.reset()
    with pytest.raises(demo.NotDemoDatabase):
        demo.prepare()
    assert db.get_order(order_id) is not None
    assert db.last_order_id() == order_id


def test_cli_reset_only_in_demo(monkeypatch):
    runner = app_module.app.test_cli_runner()
    result = runner.invoke(args=["reset-demo"])
    assert result.exit_code != 0 and "только для демо" in result.output
    assert db.last_order_id() == 0

    monkeypatch.setattr(config, "DEMO", True)
    result = runner.invoke(args=["reset-demo"])
    assert result.exit_code == 0, result.output
    assert "Демо-база пересоздана: заказов" in result.output


# ---------- Вход ----------

def test_demo_logins(demo_mode):
    admin = demo_login("demo-admin")
    assert admin.get("/stats").status_code == 200
    operator = demo_login("demo-operator")
    assert operator.get("/").status_code == 200
    assert operator.get("/stats").status_code == 403


def test_login_page_shows_demo_credentials(demo_mode):
    page = Browser().get("/login").get_data(as_text=True)
    assert "demo-admin" in page and "demo-operator" in page and "demo2026" in page


def test_brute_force_blocks_ip_but_not_login(demo_mode):
    # Пять ошибок с разных адресов не закрывают demo-admin для остальных
    for n in range(6):
        b = Browser()
        b.client.environ_base["REMOTE_ADDR"] = f"10.0.0.{n}"
        assert b.login("demo-admin", "wrong-password").status_code == 401
    good = Browser()
    good.client.environ_base["REMOTE_ADDR"] = "10.0.1.1"
    assert good.login("demo-admin", demo.DEMO_PASSWORD).status_code == 302

    # А один адрес после пяти ошибок по-прежнему блокируется
    bad = Browser()
    bad.client.environ_base["REMOTE_ADDR"] = "10.0.2.2"
    codes = [bad.login("demo-admin", "wrong-password").status_code for _ in range(5)]
    assert codes[-1] == 429
    assert bad.login("demo-admin", demo.DEMO_PASSWORD).status_code == 429


# ---------- Запреты ----------

def test_staff_changes_forbidden(demo_admin):
    operator_id = db.get_staff_by_login("demo-operator")["id"]
    admin_id = db.get_staff_by_login("demo-admin")["id"]
    before = db.list_staff()
    for url, data in (
        ("/staff", {"login": "x", "name": "X", "role": "admin", "password": "12345678"}),
        (f"/staff/{operator_id}/active", {"active": "0"}),
        (f"/staff/{operator_id}/password", {"password": "hacked123"}),
        (f"/staff/{admin_id}/password", {"password": "hacked123"}),
        (f"/staff/{operator_id}/role", {"role": "admin"}),
    ):
        response = demo_admin.post(url, data)
        assert response.status_code == 403, url
        assert "В демо-версии это недоступно" in response.get_data(as_text=True)
    assert db.list_staff() == before
    assert demo_login("demo-operator").logged_in()


def test_logout_everywhere_forbidden(demo_admin):
    other = demo_login("demo-admin")
    assert demo_admin.post("/logout-everywhere").status_code == 403
    assert other.logged_in() and demo_admin.logged_in()


def test_staff_page_buttons_disabled(demo_admin):
    page = demo_admin.get("/staff").get_data(as_text=True)
    assert "В демо-версии нельзя" in page
    assert "Добавить сотрудника" not in page
    assert "Выйти на всех устройствах" not in page
    buttons = re.findall(r"<button type=\"submit\" class=\"button-[a-z]+\"[^>]*>", page)
    assert buttons and all("disabled" in b for b in buttons)


# ---------- Telegram ----------

def test_status_change_without_telegram(demo_admin, sent):
    order_id = next(o["id"] for o in db.list_orders(("новый",), "", 1, 0))
    response = demo_admin.post(f"/orders/{order_id}/status", {"status": "accepted"})
    assert response.status_code == 302
    assert db.get_order(order_id)["status"] == "принят"
    assert sent.messages == []
    page = demo_admin.get(response.headers["Location"]).get_data(as_text=True)
    assert ("info", "Статус изменён. В демо сообщения не отправляются") in flashes_in(page)
    assert "В демо не отправляется" in page
    assert "Клиенту не доставлено" not in page
    assert "tg://user" not in page


def test_telegram_send_is_off_in_demo(monkeypatch):
    # Сеть в тестах запрещена (conftest): если бы был запрос — тест упал бы
    monkeypatch.setattr(config, "DEMO", True)
    assert telegram.send_message(111, "проверка") is False


# ---------- Плашка ----------

def test_banner_on_every_page(demo_admin):
    guest = Browser()
    pages = [guest.get("/login")]
    pages += [demo_admin.get(url) for url in ("/", "/orders/1", "/stats", "/staff", "/nope")]
    for response in pages:
        assert "Демо-версия: данные выдуманные" in response.get_data(as_text=True), response.request.path


def test_no_demo_traces_in_real_panel(admin):
    for url in ("/", "/staff"):
        page = admin.get(url).get_data(as_text=True)
        assert "Демо-версия" not in page and "demo2026" not in page
    assert "demo2026" not in Browser().get("/login").get_data(as_text=True)
