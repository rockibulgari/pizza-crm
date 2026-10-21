"""Настройки панели из файла .env рядом с app.py."""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")


def required(name: str) -> str:
    """Значение настройки или понятная ошибка, если её нет."""
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"Ошибка: в .env не задано {name} (пример — в .env.example)")
    return value


SECRET_KEY = required("SECRET_KEY")

ORDERS_DB = Path(required("ORDERS_DB"))
if not ORDERS_DB.is_file():
    sys.exit(f"Ошибка: база заказов не найдена: {ORDERS_DB} (проверьте ORDERS_DB в .env)")
