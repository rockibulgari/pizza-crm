"""Настройки панели из файла .env рядом с app.py."""
import os
import sys
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

load_dotenv(Path(__file__).parent / ".env")


def required(name: str) -> str:
    """Значение настройки или понятная ошибка, если её нет."""
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"Ошибка: в .env не задано {name} (пример — в .env.example)")
    return value


SECRET_KEY = required("SECRET_KEY")

# Демо-версия для портфолио: своя база с выдуманными заказами, Telegram отключён
DEMO = os.getenv("DEMO", "").strip() == "1"

ORDERS_DB = Path(required("ORDERS_DB"))

if DEMO:
    # Демо-базы может ещё не быть — панель создаст её сама.
    # Бота у демо нет, токена тоже: сообщения клиентам не отправляются вовсе
    BOT_TOKEN = None
else:
    if not ORDERS_DB.is_file():
        sys.exit(f"Ошибка: база заказов не найдена: {ORDERS_DB} (проверьте ORDERS_DB в .env)")

    # Токен бота хранится в одном месте — в .env бота. Берём оттуда только BOT_TOKEN,
    # остальные настройки бота панели не нужны. Сам токен никуда не печатаем.
    BOT_ENV_FILE = Path(required("BOT_ENV_FILE"))
    if not BOT_ENV_FILE.is_file():
        sys.exit(f"Ошибка: .env бота не найден: {BOT_ENV_FILE} (проверьте BOT_ENV_FILE в .env)")
    BOT_TOKEN = (dotenv_values(BOT_ENV_FILE).get("BOT_TOKEN") or "").strip()
    if not BOT_TOKEN:
        sys.exit(f"Ошибка: в {BOT_ENV_FILE} не задан BOT_TOKEN")
