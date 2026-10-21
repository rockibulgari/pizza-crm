"""Вход, выход, блокировка после 5 ошибок, срок сессии."""
from datetime import timedelta

import pytest

import db
from conftest import Browser, add_staff, flashes_in, logged_in_as

WRONG = "Неверный логин или пароль"


def error_text(response) -> str:
    return response.get_data(as_text=True)


def make_old(field: str, value: str, minutes: int):
    """Сдвигает неудачные попытки в прошлое — как будто прошло N минут."""
    with db.connect() as conn:
        rows = conn.execute(f"SELECT rowid, attempted_at FROM login_attempts WHERE {field} = ?",
                            (value,)).fetchall()
        for row in rows:
            moved = db.datetime.fromisoformat(row["attempted_at"]) - timedelta(minutes=minutes)
            conn.execute("UPDATE login_attempts SET attempted_at = ? WHERE rowid = ?",
                         (moved.isoformat(), row["rowid"]))
    conn.close()


def test_login_success_redirects_to_orders(browser):
    add_staff("anna", "password1")
    response = browser.login("anna", "password1")
    assert response.status_code == 302 and response.headers["Location"] == "/"
    assert browser.logged_in()


def test_login_is_case_insensitive_for_login(browser):
    add_staff("anna", "password1")
    assert browser.login("  ANNA ", "password1").status_code == 302


@pytest.mark.parametrize("login, password", [
    ("anna", "wrong-password"),   # неверный пароль
    ("nobody", "password1"),      # нет такого логина
])
def test_same_error_for_wrong_password_and_unknown_login(browser, login, password):
    add_staff("anna", "password1")
    response = browser.login(login, password)
    assert response.status_code == 401
    assert WRONG in error_text(response)


def test_disabled_staff_cannot_login(browser):
    staff = add_staff("anna", "password1")
    db.set_active(staff, False)
    response = browser.login("anna", "password1")
    assert response.status_code == 401 and WRONG in error_text(response)


def test_login_without_csrf_is_rejected(browser):
    add_staff("anna", "password1")
    response = browser.post("/login", {"login": "anna", "password": "password1"}, csrf=None)
    assert response.status_code == 400
    assert not browser.logged_in()


def test_huge_password_is_not_hashed_and_counts_as_failure(browser):
    add_staff("anna", "password1")
    response = browser.login("anna", "x" * 5000)
    assert response.status_code == 401
    count, _ = db.recent_failures("login", "anna", db.now_moscow() - timedelta(minutes=5))
    assert count == 1


# ---------- Блокировка ----------

def fail(times: int, login: str = "anna"):
    for _ in range(times):
        Browser().login(login, "wrong-password")


def test_fifth_failure_blocks_for_5_minutes(browser):
    add_staff("anna", "password1")
    fail(4)
    response = browser.login("anna", "wrong-password")
    assert response.status_code == 429
    assert "Слишком много попыток. Попробуйте через 5 мин." in error_text(response)


def test_blocked_even_with_correct_password(browser):
    add_staff("anna", "password1")
    fail(5)
    response = browser.login("anna", "password1")
    assert response.status_code == 429
    assert not browser.logged_in()


def test_block_by_ip_covers_other_logins(browser):
    """5 ошибок с одного IP по разным логинам — закрыт вход с этого IP в любой логин."""
    add_staff("anna", "password1")
    for name in ("a1", "a2", "a3", "a4", "a5"):
        Browser().login(name, "wrong-password")
    assert browser.login("anna", "password1").status_code == 429


def test_block_ends_after_5_minutes(browser):
    add_staff("anna", "password1")
    fail(5)
    make_old("login", "anna", minutes=6)
    assert browser.login("anna", "password1").status_code == 302


def test_old_failures_do_not_count(browser):
    add_staff("anna", "password1")
    fail(4)
    make_old("login", "anna", minutes=10)  # вне окна 5 минут
    fail(1)
    assert browser.login("anna", "password1").status_code == 302


def test_success_clears_failures(browser):
    add_staff("anna", "password1")
    fail(4)
    assert browser.login("anna", "password1").status_code == 302
    fail(4)  # снова 4 — блокировки нет, старые стёрты
    assert Browser().login("anna", "password1").status_code == 302


def test_attempts_older_than_a_day_are_deleted():
    add_staff("anna", "password1")
    fail(1)
    make_old("login", "anna", minutes=25 * 60)
    fail(1)  # каждая неудачная попытка чистит записи старше суток
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0] == 1
    conn.close()


# ---------- Выход и срок сессии ----------

def test_logout(browser):
    add_staff("anna", "password1")
    browser.login("anna", "password1")
    assert browser.post("/logout").status_code == 302
    assert not browser.logged_in()


def test_logout_requires_post_and_csrf(browser):
    add_staff("anna", "password1")
    browser.login("anna", "password1")
    assert browser.get("/logout").status_code == 405
    assert browser.post("/logout", csrf="wrong").status_code == 400
    assert browser.logged_in()


def test_session_expires_after_12_hours(browser):
    add_staff("anna", "password1")
    browser.login("anna", "password1")
    with browser.client.session_transaction(base_url="https://localhost") as session:
        session["logged_in_at"] = (db.now_moscow() - timedelta(hours=12, minutes=1)).isoformat()
    assert not browser.logged_in()


def test_session_still_valid_before_12_hours(browser):
    add_staff("anna", "password1")
    browser.login("anna", "password1")
    with browser.client.session_transaction(base_url="https://localhost") as session:
        session["logged_in_at"] = (db.now_moscow() - timedelta(hours=11, minutes=59)).isoformat()
    assert browser.logged_in()


def test_logout_everywhere_kills_all_devices():
    phone = logged_in_as("anna")
    computer = Browser()
    computer.login("anna", "password1")
    stolen = Browser()
    with phone.client.session_transaction(base_url="https://localhost") as source:
        copied = dict(source)
    with stolen.client.session_transaction(base_url="https://localhost") as target:
        target.update(copied)  # кто-то скопировал cookie телефона
    assert stolen.logged_in()

    assert computer.post("/logout-everywhere").status_code == 302
    assert not phone.logged_in() and not computer.logged_in() and not stolen.logged_in()
    assert Browser().login("anna", "password1").status_code == 302


def test_logged_in_user_is_sent_from_login_page(browser):
    add_staff("anna", "password1")
    browser.login("anna", "password1")
    response = browser.get("/login")
    assert response.status_code == 302 and response.headers["Location"] == "/"
    assert flashes_in(browser.get("/").get_data(as_text=True)) == []
