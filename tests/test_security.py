"""Заголовки безопасности, размер запроса, кэш страниц."""
import pytest


@pytest.mark.parametrize("path", ["/login", "/static/style.css"])
def test_security_headers_everywhere(browser, path):
    headers = browser.get(path).headers
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "same-origin"
    assert headers["Strict-Transport-Security"].startswith("max-age=")
    csp = headers["Content-Security-Policy"]
    for rule in ("script-src 'self'", "frame-ancestors 'none'", "form-action 'self'",
                 "object-src 'none'", "base-uri 'none'"):
        assert rule in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]


def test_no_hsts_over_plain_http():
    from conftest import app_module
    response = app_module.app.test_client().get("/login", base_url="http://localhost")
    assert "Strict-Transport-Security" not in response.headers


def test_pages_with_data_are_not_cached(operator):
    for path in ("/", "/orders/fresh"):
        assert operator.get(path).headers["Cache-Control"] == "no-store"


def test_too_large_request_is_rejected(browser):
    response = browser.post("/login", {"login": "x", "password": "x" * 100_000})
    assert response.status_code == 413
    assert "Слишком большой запрос" in response.get_data(as_text=True)


def test_templates_have_no_inline_scripts():
    """CSP запрещает встроенные скрипты — в шаблонах их и не должно быть."""
    import re
    from pathlib import Path
    for template in (Path(__file__).parent.parent / "templates").glob("*.html"):
        text = template.read_text(encoding="utf-8")
        for tag in re.findall(r"<script[^>]*>", text):
            assert "src=" in tag, f"{template.name}: встроенный <script>"
        assert not re.search(r"\son[a-z]+=", text), f"{template.name}: обработчик on…="
