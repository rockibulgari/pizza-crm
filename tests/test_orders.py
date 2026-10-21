"""Список заказов, поиск и смена статуса с уведомлением клиента."""
import re

import pytest

import db
from conftest import add_order, flashes_in


def ids(page: str) -> list[int]:
    return [int(x) for x in re.findall(r'data-id="(\d+)"', page)]


def status_of(order_id: int) -> str:
    return db.get_order(order_id)["status"]


def history(order_id: int) -> list[tuple]:
    return [(h["staff_name"], h["old_status"], h["new_status"], h["notified"])
            for h in reversed(db.status_history(order_id))]


# ---------- Список и поиск ----------

def test_list_shows_active_by_default_newest_first(operator):
    first = add_order("2026-10-20T10:00:00+03:00", "новый")
    done = add_order("2026-10-20T11:00:00+03:00", "доставлен")
    last = add_order("2026-10-21T12:00:00+03:00", "в пути")
    assert ids(operator.get("/").get_data(as_text=True)) == [last, first]
    assert ids(operator.get("/?status=all").get_data(as_text=True)) == [last, done, first]


@pytest.mark.parametrize("query, found", [
    ("ИВАН", True),               # имя без учёта регистра (кириллица)
    ("8 928 123", True),          # телефон: только цифры, первая 8 не мешает
    ("+7 928 1234567", True),
    ("ленина", True),             # адрес
    ("№{id}", True),              # номер заказа
    ("Пётр", False),
    ("%", False),                 # % — обычный символ, не «всё подряд»
])
def test_search(operator, query, found):
    order = add_order("2026-10-21T12:00:00+03:00", client_name="Иван",
                      phone="+7 (928) 123-45-67", address="ул. Ленина, 5")
    page = operator.get("/?status=all&q=" + query.format(id=order)).get_data(as_text=True)
    assert (order in ids(page)) is found


def test_client_name_is_escaped(operator):
    add_order("2026-10-21T12:00:00+03:00", client_name="<img src=x onerror=alert(1)>")
    page = operator.get("/").get_data(as_text=True)
    assert "<img src=x" not in page and "&lt;img src=x onerror=alert(1)&gt;" in page


# ---------- Смена статуса ----------

def press(browser, order_id, key):
    response = browser.post(f"/orders/{order_id}/status", {"status": key})
    assert response.status_code == 302
    return flashes_in(browser.get(response.headers["Location"]).get_data(as_text=True))


def test_status_change_notifies_client(operator, sent):
    order = add_order("2026-10-21T12:00:00+03:00", user_id=555)
    assert press(operator, order, "on_way") == [("success", "Статус изменён, клиент уведомлён")]
    assert status_of(order) == "в пути"
    assert sent.messages == [(555, f"Ваш заказ №{order} в пути 🚗")]
    assert history(order) == [("Anna", "новый", "в пути", 1)]


def test_same_status_does_nothing(operator, sent):
    order = add_order("2026-10-21T12:00:00+03:00", status="принят")
    assert press(operator, order, "accepted") == [("info", "Статус уже такой")]
    assert sent.messages == [] and history(order) == []


def test_telegram_failure_still_saves_status(operator, sent):
    sent.ok = False
    order = add_order("2026-10-21T12:00:00+03:00")
    assert press(operator, order, "cooking") == [
        ("warning", "Статус изменён, но клиенту сообщение не доставлено")]
    assert status_of(order) == "готовится"
    assert history(order) == [("Anna", "новый", "готовится", 0)]
    assert "Клиенту не доставлено" in operator.get(f"/orders/{order}").get_data(as_text=True)


def test_database_error_sends_nothing(operator, sent, monkeypatch):
    order = add_order("2026-10-21T12:00:00+03:00")

    def broken(*args):
        raise db.sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "change_status", broken)
    assert press(operator, order, "accepted") == [
        ("error", "Не удалось сменить статус, попробуйте ещё раз")]
    assert sent.messages == [] and status_of(order) == "новый"


@pytest.mark.parametrize("key", ["new", "deleted", ""])
def test_cannot_set_new_or_unknown_status(operator, sent, key):
    order = add_order("2026-10-21T12:00:00+03:00", status="принят")
    assert press(operator, order, key) == [("error", "Неизвестный статус")]
    assert status_of(order) == "принят" and sent.messages == []


def test_unknown_order_is_404(operator, sent):
    assert operator.post("/orders/999/status", {"status": "accepted"}).status_code == 404
    assert sent.messages == []


def test_status_change_requires_csrf(operator, sent):
    order = add_order("2026-10-21T12:00:00+03:00")
    assert operator.post(f"/orders/{order}/status", {"status": "accepted"}, csrf=None).status_code == 400
    assert status_of(order) == "новый" and sent.messages == []


def test_all_status_texts_match_bot(operator, sent):
    order = add_order("2026-10-21T12:00:00+03:00", user_id=7)
    for key in ("accepted", "cooking", "on_way", "delivered", "cancelled"):
        press(operator, order, key)
    assert [text for _, text in sent.messages] == [
        f"Ваш заказ №{order} принят ✅",
        f"Ваш заказ №{order} готовится 🍕",
        f"Ваш заказ №{order} в пути 🚗",
        f"Ваш заказ №{order} доставлен ✔️",
        f"Ваш заказ №{order} отменён ❌",
    ]
