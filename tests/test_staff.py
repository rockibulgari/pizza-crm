"""Раздел «Сотрудники»: права, добавление, отключение, пароль, роль."""
import pytest

import db
from conftest import Browser, add_staff, flashes_in, logged_in_as


def staff_id(login):
    return db.get_staff_by_login(login)["id"]


# ---------- Права ----------

def test_operator_gets_403_everywhere(operator):
    target = add_staff("petr")
    assert operator.get("/staff").status_code == 403
    for url, data in (
        ("/staff", {"login": "x", "name": "X", "role": "admin", "password": "12345678"}),
        (f"/staff/{target}/active", {"active": "0"}),
        (f"/staff/{target}/password", {"password": "hacked123"}),
        (f"/staff/{staff_id('anna')}/role", {"role": "admin"}),
    ):
        assert operator.post(url, data).status_code == 403, url
    assert db.get_staff_by_login("x") is None
    assert db.get_staff(staff_id("anna"))["role"] == db.ROLE_OPERATOR
    assert db.get_staff(target)["active"] == 1


def test_operator_has_no_staff_menu(operator):
    assert "Сотрудники" not in operator.get("/").get_data(as_text=True)


def test_anonymous_is_sent_to_login(browser):
    response = browser.get("/staff")
    assert response.status_code == 302 and response.headers["Location"].endswith("/login")


@pytest.mark.parametrize("csrf", [None, "wrong-token"])
def test_post_without_valid_csrf_is_rejected(admin, csrf):
    target = add_staff("petr")
    response = admin.post(f"/staff/{target}/active", {"active": "0"}, csrf=csrf)
    assert response.status_code == 400
    assert db.get_staff(target)["active"] == 1


def test_actions_are_post_only(admin):
    target = add_staff("petr")
    assert admin.get(f"/staff/{target}/active").status_code == 405


def test_unknown_staff_is_404(admin):
    assert admin.post("/staff/9999/active", {"active": "0"}).status_code == 404


# ---------- Добавление ----------

def test_create_staff(admin):
    response = admin.post("/staff", {"login": "  Petr ", "name": " Пётр ", "role": "operator",
                                     "password": "petrpass1"})
    assert response.status_code == 302
    assert admin.flashes("/staff") == [("success", "Сотрудник Пётр (petr) добавлен — Оператор")]
    assert db.get_staff_by_login("petr")["name"] == "Пётр"
    assert Browser().login("petr", "petrpass1").status_code == 302


@pytest.mark.parametrize("data, message", [
    ({"login": "anna2", "name": "П", "role": "operator", "password": "12345678"}, "уже занят"),
    ({"login": "p r", "name": "П", "role": "operator", "password": "12345678"}, "пробел"),
    ({"login": "", "name": "П", "role": "operator", "password": "12345678"}, "пуст"),
    ({"login": "ok", "name": "  ", "role": "operator", "password": "12345678"}, "Имя не может"),
    ({"login": "ok", "name": "П", "role": "operator", "password": "1234567"}, "не короче 8"),
    ({"login": "ok", "name": "П", "role": "root", "password": "12345678"}, "Выберите роль"),
    ({"login": "x" * 65, "name": "П", "role": "operator", "password": "12345678"}, "Слишком длинное"),
])
def test_create_staff_errors(admin, data, message):
    add_staff("anna2")
    response = admin.post("/staff", data)
    assert response.status_code == 422
    flashes = flashes_in(response.get_data(as_text=True))
    assert flashes and flashes[0][0] == "error" and message in flashes[0][1]
    assert len(db.list_staff()) == 2  # boss и anna2 — никого не добавилось


def test_form_keeps_input_but_not_password(admin):
    page = admin.post("/staff", {"login": "ok4", "name": "Ввёл имя", "role": "admin",
                                 "password": "123"}).get_data(as_text=True)
    assert 'class="card add-staff" open' in page
    assert 'value="ok4"' in page and 'value="Ввёл имя"' in page
    assert 'value="123"' not in page


def test_staff_name_is_escaped(admin):
    admin.post("/staff", {"login": "xss", "name": '<script>alert(1)</script>', "role": "operator",
                          "password": "12345678"})
    page = admin.get("/staff").get_data(as_text=True)
    assert "<script>alert(1)" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


# ---------- Отключение ----------

def test_disable_kills_sessions_forever(admin):
    anna = logged_in_as("anna")
    anna_id = staff_id("anna")

    admin.post(f"/staff/{anna_id}/active", {"active": "0"})
    assert not anna.logged_in()
    assert Browser().login("anna", "password1").status_code == 401

    admin.post(f"/staff/{anna_id}/active", {"active": "1"})
    assert not anna.logged_in(), "старая сессия не должна ожить после включения"
    assert Browser().login("anna", "password1").status_code == 302


def test_admin_cannot_disable_self(admin):
    me = staff_id("boss")
    admin.post(f"/staff/{me}/active", {"active": "0"})
    assert admin.flashes("/staff") == [("error", "Нельзя отключить самого себя")]
    assert db.get_staff(me)["active"] == 1 and admin.logged_in()


# ---------- Пароль ----------

def test_change_password_of_other(admin):
    anna = logged_in_as("anna")
    admin.post(f"/staff/{staff_id('anna')}/password", {"password": "annanew22"})
    assert not anna.logged_in()
    assert Browser().login("anna", "password1").status_code == 401
    assert Browser().login("anna", "annanew22").status_code == 302
    assert admin.logged_in()


def test_change_own_password_keeps_this_session_only(admin):
    other_device = Browser()
    other_device.login("boss", "password1")
    admin.post(f"/staff/{staff_id('boss')}/password", {"password": "bossnew22"})
    assert admin.flashes("/staff") == [
        ("success", "Ваш пароль изменён. На других устройствах нужно войти заново.")]
    assert admin.logged_in()
    assert not other_device.logged_in()


def test_short_password_is_rejected(admin):
    target = add_staff("anna")
    admin.post(f"/staff/{target}/password", {"password": "short"})
    flashes = admin.flashes("/staff")
    assert flashes[0][0] == "error" and "не короче 8" in flashes[0][1]
    assert Browser().login("anna", "password1").status_code == 302


# ---------- Роль ----------

def test_role_change_applies_immediately(admin):
    anna = logged_in_as("anna")
    assert anna.get("/stats").status_code == 403
    admin.post(f"/staff/{staff_id('anna')}/role", {"role": "admin"})
    assert anna.get("/stats").status_code == 200
    admin.post(f"/staff/{staff_id('anna')}/role", {"role": "operator"})
    assert anna.get("/stats").status_code == 403


def test_admin_cannot_demote_self(admin):
    admin.post(f"/staff/{staff_id('boss')}/role", {"role": "operator"})
    assert admin.flashes("/staff") == [("error", "Нельзя снять с себя роль администратора")]
    assert db.get_staff_by_login("boss")["role"] == db.ROLE_ADMIN


def test_unknown_role_is_rejected(admin):
    target = add_staff("anna")
    admin.post(f"/staff/{target}/role", {"role": "root"})
    assert admin.flashes("/staff") == [("error", "Неизвестная роль")]
