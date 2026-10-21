"""Вся работа с базой панели (SQLite).

Панель использует ту же orders.db, что и бот. Таблицу orders бота панель
не меняет по структуре; свои таблицы (сотрудники, попытки входа) создаёт
рядом в той же базе.
"""
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import config

# В Москве нет перехода на летнее время, поэтому достаточно сдвига +3 часа.
# Так время записывается в том же виде, что и у бота: 2026-10-18T15:32:04+03:00
MOSCOW = timezone(timedelta(hours=3))

ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLES = (ROLE_ADMIN, ROLE_OPERATOR)


def now_moscow() -> datetime:
    return datetime.now(MOSCOW).replace(microsecond=0)


def py_lower(text):
    """Нижний регистр для поиска. Встроенный LOWER в SQLite не понимает кириллицу."""
    return text.lower() if isinstance(text, str) else text


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.ORDERS_DB)
    # Чтобы строки из базы можно было читать по имени поля: row["name"]
    conn.row_factory = sqlite3.Row
    # Если база занята ботом — подождать до 5 секунд, а не сразу падать с ошибкой
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.create_function("py_lower", 1, py_lower, deterministic=True)
    return conn


def init_db():
    """Включает режим WAL и создаёт таблицы панели, если их ещё нет."""
    with closing(connect()) as conn, conn:  # второй conn — сохранить всё разом
        # WAL позволяет боту и панели одновременно читать и писать.
        # Режим запоминается в самом файле базы.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS staff (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                login         TEXT    NOT NULL UNIQUE,
                name          TEXT    NOT NULL,
                password_hash TEXT    NOT NULL,
                role          TEXT    NOT NULL CHECK (role IN {ROLES}),
                active        INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT    NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_attempts (
                ip           TEXT NOT NULL,
                login        TEXT NOT NULL,
                attempted_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS login_attempts_time ON login_attempts (attempted_at)"
        )


# ---------- Сотрудники ----------

def create_staff(login: str, name: str, password_hash: str, role: str) -> int:
    """Добавляет сотрудника и возвращает его id.
    Если такой логин уже есть — sqlite3.IntegrityError."""
    with closing(connect()) as conn, conn:
        cursor = conn.execute(
            """
            INSERT INTO staff (login, name, password_hash, role, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (login, name, password_hash, role, now_moscow().isoformat()),
        )
        return cursor.lastrowid


def get_staff(staff_id: int) -> dict | None:
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()
    return dict(row) if row else None


def get_staff_by_login(login: str) -> dict | None:
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM staff WHERE login = ?", (login,)).fetchone()
    return dict(row) if row else None


# ---------- Неудачные попытки входа ----------

def add_failed_attempt(ip: str, login: str):
    """Записывает неудачную попытку и заодно стирает записи старше суток."""
    now = now_moscow()
    with closing(connect()) as conn, conn:
        conn.execute(
            "INSERT INTO login_attempts (ip, login, attempted_at) VALUES (?, ?, ?)",
            (ip, login, now.isoformat()),
        )
        conn.execute(
            "DELETE FROM login_attempts WHERE attempted_at < ?",
            ((now - timedelta(days=1)).isoformat(),),
        )


def recent_failures(field: str, value: str, since: datetime) -> tuple[int, datetime | None]:
    """Сколько неудачных попыток было с этого IP (field="ip") или в этот логин
    (field="login") начиная с since, и время последней из них."""
    if field not in ("ip", "login"):
        raise ValueError(field)
    with closing(connect()) as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS count, MAX(attempted_at) AS last
            FROM login_attempts
            WHERE {field} = ? AND attempted_at >= ?
            """,
            (value, since.isoformat()),
        ).fetchone()
    last = datetime.fromisoformat(row["last"]) if row["last"] else None
    return row["count"], last


def clear_failures(ip: str, login: str):
    """После успешного входа забываем прошлые ошибки этого IP и этого логина."""
    with closing(connect()) as conn, conn:
        conn.execute("DELETE FROM login_attempts WHERE ip = ? OR login = ?", (ip, login))
