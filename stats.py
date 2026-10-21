"""Статистика за период: итоги, разбивка по дням и популярные пиццы.

Считается из списка заказов (db.orders_between) без Flask и без базы,
поэтому каждую цифру легко проверить отдельно.

Правила (как в команде /today бота):
- отменённые заказы входят в количество заказов, но не в выручку и не в средний чек;
- средний чек = выручка / число неотменённых заказов;
- выручка по пиццам — по ценам до скидки: скидка в базе хранится на весь заказ.
"""
from datetime import date, timedelta

SIZES = (30, 40)  # размеры пицц в боте


def average(revenue: int, count: int) -> int | None:
    """Средний чек, округлённый до рубля (0,5 — вверх). None, если заказов нет."""
    if not count:
        return None
    return (revenue * 2 + count) // (count * 2)


def calculate(orders: list[dict], date_from: date, date_to: date, cancelled_status: str) -> dict:
    # Каждый день периода, даже без заказов, — чтобы в таблице и на графике не было «дыр»
    days = {}
    day = date_from
    while day <= date_to:
        days[day] = {"date": day, "count": 0, "revenue": 0, "paid": 0, "cancelled": 0}
        day += timedelta(days=1)

    pizzas = {}
    for order in orders:
        row = days.get(order["created_at"].date())
        if row is None:  # на всякий случай: заказ вне периода не считаем
            continue
        row["count"] += 1
        if order["status"] == cancelled_status:
            row["cancelled"] += 1
            continue
        row["paid"] += 1
        row["revenue"] += order["total"]
        for item in order["items"]:
            pizza = pizzas.setdefault(
                item["name"], {"name": item["name"], "qty": 0, "revenue": 0}
                | {size: 0 for size in SIZES}
            )
            pizza["qty"] += item["qty"]
            pizza["revenue"] += item["qty"] * item["price"]
            if item["size"] in SIZES:
                pizza[item["size"]] += item["qty"]

    by_day = list(days.values())
    for row in by_day:
        row["average"] = average(row["revenue"], row["paid"])

    revenue = sum(row["revenue"] for row in by_day)
    paid = sum(row["paid"] for row in by_day)
    return {
        "count": sum(row["count"] for row in by_day),
        "revenue": revenue,
        "average": average(revenue, paid),
        "cancelled": sum(row["cancelled"] for row in by_day),
        "by_day": by_day,
        "max_day_revenue": max(row["revenue"] for row in by_day),
        # Больше штук — выше; при равенстве — по выручке, потом по названию
        "pizzas": sorted(pizzas.values(), key=lambda p: (-p["qty"], -p["revenue"], p["name"])),
    }
