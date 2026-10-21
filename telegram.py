"""Сообщения клиентам в Telegram от имени бота.

Запрос к Telegram Bot API делается встроенным urllib, без библиотек.
Адрес запроса содержит токен, поэтому ни адрес, ни сам запрос в лог не пишутся —
только номер чата и причина, которую назвал Telegram.
"""
import json
import logging
import urllib.error
import urllib.request

import config

TIMEOUT = 10  # секунд; дольше сотрудник ждать не должен

log = logging.getLogger(__name__)


def send_message(chat_id: int, text: str) -> bool:
    """Отправляет текст клиенту. True — Telegram подтвердил доставку боту клиента,
    False — не получилось (клиент заблокировал бота, нет сети, Telegram не ответил)."""
    if config.DEMO:
        # В демо в Telegram не ходим никогда, даже если сюда попали по ошибке
        return False
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            answer = json.load(response)
    except urllib.error.HTTPError as error:
        # Telegram ответил ошибкой, например 403 «bot was blocked by the user»
        log.warning("Telegram не доставил сообщение в чат %s: %s", chat_id, describe(error))
        return False
    except (OSError, ValueError) as error:
        # Нет сети, таймаут или непонятный ответ. Пишем только тип ошибки:
        # в тексте некоторых ошибок urllib бывает адрес запроса с токеном
        log.warning("Telegram не ответил (чат %s): %s", chat_id, type(error).__name__)
        return False
    if not answer.get("ok"):
        log.warning("Telegram отказал (чат %s): %s", chat_id, answer.get("description"))
        return False
    return True


def describe(error: urllib.error.HTTPError) -> str:
    """Причина отказа из ответа Telegram, например «Forbidden: bot was blocked by the user»."""
    try:
        return json.load(error).get("description") or f"HTTP {error.code}"
    except (OSError, ValueError):
        return f"HTTP {error.code}"
