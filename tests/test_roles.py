"""Кто какие страницы видит: гость, оператор, админ."""
import pytest

from conftest import add_order

PAGES = ["/", "/orders/fresh", "/orders/{order}", "/stats", "/staff"]
ADMIN_ONLY = {"/stats", "/staff"}


@pytest.fixture
def order():
    return add_order("2026-10-21T12:00:00+03:00")


@pytest.mark.parametrize("page", PAGES)
def test_guest_is_sent_to_login(browser, order, page):
    response = browser.get(page.format(order=order))
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


@pytest.mark.parametrize("page", PAGES)
def test_operator_access(operator, order, page):
    expected = 403 if page in ADMIN_ONLY else 200
    response = operator.get(page.format(order=order))
    assert response.status_code == expected
    if expected == 403:
        assert "Недостаточно прав" in response.get_data(as_text=True)


@pytest.mark.parametrize("page", PAGES)
def test_admin_access(admin, order, page):
    assert admin.get(page.format(order=order)).status_code == 200


def test_menu_by_role(admin, operator):
    admin_page = admin.get("/").get_data(as_text=True)
    operator_page = operator.get("/").get_data(as_text=True)
    for item in ("Статистика", "Сотрудники"):
        assert item in admin_page
        assert item not in operator_page


def test_guest_cannot_change_status(browser, order):
    response = browser.post(f"/orders/{order}/status", {"status": "accepted"})
    assert response.status_code == 302 and response.headers["Location"].endswith("/login")


def test_static_and_login_open_to_guest(browser):
    assert browser.get("/login").status_code == 200
    assert browser.get("/static/style.css").status_code == 200


@pytest.mark.parametrize("path", ["/.env", "/app.py", "/static/../app.py", "/static/%2e%2e/.env",
                                  "/.git/config", "/static/"])
def test_no_access_to_project_files(browser, path):
    assert browser.get(path).status_code == 404
