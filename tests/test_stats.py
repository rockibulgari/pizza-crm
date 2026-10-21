"""Статистика: цифры сверяются с независимым подсчётом SQL-запросом."""
from datetime import timedelta

import pytest

import db
import stats

CANCEL = "отменён"
M30 = {"name": "Маргарита", "size": 30, "qty": 2, "price": 450}
M40 = {"name": "Маргарита", "size": 40, "qty": 1, "price": 690}
P40x3 = {"name": "Пепперони", "size": 40, "qty": 3, "price": 820}   # скидка 15%


@pytest.fixture
def orders():
    """Заказы вокруг «сегодня»: полночь, отмена, граница периода, вне периода."""
    from conftest import add_order
    today = db.now_moscow().date()

    def at(days_ago, time):
        return f"{(today - timedelta(days=days_ago)).isoformat()}T{time}+03:00"

    add_order(at(0, "00:00:00"), "новый", [M30, M40])
    add_order(at(0, "23:59:59"), "доставлен", [P40x3])
    add_order(at(1, "12:00:00"), CANCEL, [P40x3, M30])     # отменён — не в выручку и пиццы
    add_order(at(1, "13:00:00"), "в пути", [M40])
    add_order(at(6, "10:00:00"), "принят", [M30])          # последний день периода «7 дней»
    add_order(at(7, "09:00:00"), "доставлен", [M40])       # уже вне «7 дней»
    add_order(at(40, "09:00:00"), "доставлен", [M30])      # вне «30 дней»
    return today


def sql_totals(date_from, date_to):
    """Независимый подсчёт прямо в базе — как это сделал бы человек вручную."""
    where = "substr(created_at, 1, 10) BETWEEN ? AND ?"
    args = (date_from.isoformat(), date_to.isoformat())
    with db.connect() as conn:
        count, cancelled = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(status = ?), 0) FROM orders WHERE {where}",
            (CANCEL, *args)).fetchone()
        revenue, paid = conn.execute(
            f"SELECT COALESCE(SUM(total), 0), COUNT(*) FROM orders WHERE {where} AND status != ?",
            (*args, CANCEL)).fetchone()
        pizzas = conn.execute(f"""
            SELECT json_extract(j.value, '$.name'),
                   SUM(json_extract(j.value, '$.qty')),
                   SUM(CASE WHEN json_extract(j.value, '$.size') = 30 THEN json_extract(j.value, '$.qty') ELSE 0 END),
                   SUM(CASE WHEN json_extract(j.value, '$.size') = 40 THEN json_extract(j.value, '$.qty') ELSE 0 END),
                   SUM(json_extract(j.value, '$.qty') * json_extract(j.value, '$.price'))
            FROM orders, json_each(orders.items) AS j
            WHERE {where} AND status != ?
            GROUP BY 1 ORDER BY 2 DESC, 5 DESC, 1""", (*args, CANCEL)).fetchall()
    conn.close()
    return count, cancelled, revenue, paid, [tuple(p) for p in pizzas]


@pytest.mark.parametrize("days", [1, 7, 30, 366])
def test_numbers_match_sql(orders, days):
    today = orders
    date_from = today - timedelta(days=days - 1)
    result = stats.calculate(db.orders_between(date_from, today), date_from, today, CANCEL)
    count, cancelled, revenue, paid, pizzas = sql_totals(date_from, today)

    assert result["count"] == count
    assert result["cancelled"] == cancelled
    assert result["revenue"] == revenue
    # Средний чек округляется до рубля, половина — вверх (round() в Python
    # округлял бы 1840,5 к чётному — до 1840)
    assert result["average"] == (int(revenue / paid + 0.5) if paid else None)
    assert len(result["by_day"]) == days
    assert [(p["name"], p["qty"], p[30], p[40], p["revenue"]) for p in result["pizzas"]] == pizzas


def test_cancelled_counted_but_not_in_revenue(orders):
    today = orders
    yesterday = today - timedelta(days=1)
    result = stats.calculate(db.orders_between(yesterday, yesterday), yesterday, yesterday, CANCEL)
    day = result["by_day"][0]
    assert (day["count"], day["cancelled"], day["revenue"]) == (2, 1, 690)
    assert day["average"] == 690


def test_midnight_belongs_to_its_day(orders):
    today = orders
    result = stats.calculate(db.orders_between(today, today), today, today, CANCEL)
    assert result["count"] == 2


def test_empty_days_are_zero(orders):
    today = orders
    date_from = today - timedelta(days=5)
    result = stats.calculate(db.orders_between(date_from, today), date_from, today, CANCEL)
    empty = result["by_day"][3]  # 2 дня назад — заказов нет
    assert (empty["count"], empty["revenue"], empty["average"]) == (0, 0, None)


def test_average_rounds_half_up():
    assert stats.average(5, 2) == 3
    assert stats.average(7, 3) == 2
    assert stats.average(0, 0) is None


# ---------- Страница ----------

def test_stats_page_default_7_days(admin, orders):
    page = admin.get("/stats").get_data(as_text=True)
    assert page.count('class="bar-col"') == 7
    assert 'aria-current="true">7 дней' in page
    assert "без учёта скидки 15%" in page


@pytest.mark.parametrize("query, message", [
    ("from=2026-10-21&to=2026-10-01", "позже"),
    ("from=2026-13-01&to=2026-10-01", "Укажите обе даты"),
    ("from=2020-01-01&to=2026-10-21", "длиннее года"),
])
def test_bad_dates_fall_back_to_7_days(admin, orders, query, message):
    page = admin.get("/stats?" + query).get_data(as_text=True)
    assert message in page and page.count('class="bar-col"') == 7


def test_custom_period(admin, orders):
    today = orders
    date_from = (today - timedelta(days=2)).isoformat()
    page = admin.get(f"/stats?from={date_from}&to={today.isoformat()}").get_data(as_text=True)
    assert page.count('class="bar-col"') == 3
