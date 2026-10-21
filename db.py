"""Вся работа с базой панели (SQLite).

Панель использует ту же orders.db, что и бот. Таблицу orders бота панель
не меняет по структуре; свои таблицы (сотрудники, попытки входа) создаёт
рядом в той же базе.
"""
import json
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

# Статусы заказа — те же слова, что пишет в базу бот, в том же порядке
STATUS_NEW = "новый"
STATUSES = (STATUS_NEW, "принят", "готовится", "в пути", "доставлен", "отменён")
ACTIVE_STATUSES = STATUSES[:4]  # всё, кроме доставленных и отменённых


def now_moscow() -> datetime:
    return datetime.now(MOSCOW).replace(microsecond=0)


def py_lower(text):
    """Нижний регистр для поиска. Встроенный LOWER в SQLite не понимает кириллицу."""
    return text.lower() if isinstance(text, str) else text


def digits_only(text):
    """Только цифры: «+7 (928) 123-45-67» → «79281234567». Для поиска по телефону."""
    return "".join(ch for ch in text if ch.isdigit()) if isinstance(text, str) else text


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.ORDERS_DB)
    # Чтобы строки из базы можно было читать по имени поля: row["name"]
    conn.row_factory = sqlite3.Row
    # Если база занята ботом — подождать до 5 секунд, а не сразу падать с ошибкой
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.create_function("py_lower", 1, py_lower, deterministic=True)
    conn.create_function("digits_only", 1, digits_only, deterministic=True)
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
        # История смен статуса из панели. notified — дошло ли сообщение клиенту (1/0)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS status_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id   INTEGER NOT NULL,
                staff_id   INTEGER NOT NULL REFERENCES staff (id),
                old_status TEXT    NOT NULL,
                new_status TEXT    NOT NULL,
                changed_at TEXT    NOT NULL,
                notified   INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS status_log_order ON status_log (order_id)")


# ---------- Заказы ----------

PHONE_CHARS = set("0123456789+()- ")


def like_pattern(text: str) -> str:
    r"""«50%» → «%50\%%»: ищем текст где угодно в поле, а % и _ из запроса —
    обычные символы, а не шаблоны LIKE (их экранирует обратный слеш)."""
    for ch in ("\\", "%", "_"):
        text = text.replace(ch, "\\" + ch)
    return f"%{text}%"


def search_condition(q: str) -> tuple[str, list]:
    """Условие WHERE для поиска по номеру, имени, адресу и телефону.
    Пустой запрос — подходят все заказы."""
    q = q.strip()
    if not q:
        return "1", []
    # Имя и адрес — без учёта регистра (свой py_lower, встроенный не знает кириллицу)
    parts = [
        r"py_lower(client_name) LIKE ? ESCAPE '\'",
        r"py_lower(address) LIKE ? ESCAPE '\'",
    ]
    params = [like_pattern(q.lower())] * 2

    number = q.lstrip("№#").strip()
    if number.isdigit():
        parts.append("id = ?")
        params.append(int(number))

    # Телефон — только если в запросе нет ничего, кроме цифр и знаков телефона.
    # Сравниваем одни цифры; первую 8 или 7 отбрасываем, чтобы «8 928 123»
    # нашёл «+7 (928) 123-45-67»
    digits = digits_only(q)
    if digits and set(q) <= PHONE_CHARS:
        if len(digits) >= 4 and digits[0] in "78":
            digits = digits[1:]
        parts.append("digits_only(phone) LIKE ?")
        params.append(f"%{digits}%")

    return "(" + " OR ".join(parts) + ")", params


def row_to_order(row: sqlite3.Row) -> dict:
    """Строка из базы → словарь заказа: позиции снова список, время — datetime."""
    order = dict(row)
    order["items"] = json.loads(order["items"])
    order["created_at"] = datetime.fromisoformat(order["created_at"])
    return order


def list_orders(statuses: tuple[str, ...] | None, q: str, limit: int, offset: int) -> list[dict]:
    """Заказы для списка, новые сверху. statuses=None — заказы с любым статусом."""
    where, params = search_condition(q)
    if statuses is not None:
        marks = ", ".join("?" * len(statuses))  # «?, ?» — по одному на статус
        where += f" AND status IN ({marks})"
        params += statuses
    with closing(connect()) as conn:
        rows = conn.execute(
            f"SELECT * FROM orders WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return [row_to_order(row) for row in rows]


def count_by_status(q: str) -> dict[str, int]:
    """Сколько заказов в каждом статусе (с учётом поиска): {"новый": 2, ...}."""
    where, params = search_condition(q)
    with closing(connect()) as conn:
        rows = conn.execute(
            f"SELECT status, COUNT(*) AS count FROM orders WHERE {where} GROUP BY status",
            params,
        ).fetchall()
    return {row["status"]: row["count"] for row in rows}


def last_order_id() -> int:
    """Номер самого нового заказа (0, если заказов нет). По нему видно, что пришёл новый."""
    with closing(connect()) as conn:
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM orders").fetchone()[0]


def get_order(order_id: int) -> dict | None:
    """Один заказ по номеру или None, если такого нет."""
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return row_to_order(row) if row else None


# ---------- Смена статуса ----------

def change_status(order_id: int, new_status: str, staff_id: int) -> dict | None:
    """Меняет статус заказа и записывает это в status_log (notified = 0).

    Возвращает {"user_id", "old_status", "log_id"}; log_id — None, если статус
    уже такой (тогда в базу ничего не пишется). None — такого заказа нет."""
    with closing(connect()) as conn, conn:
        # Сразу берём блокировку на запись: если два сотрудника нажмут одновременно,
        # второй подождёт и увидит уже новый статус, а не прочитает старый
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT user_id, status FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if row is None:
            return None
        result = {"user_id": row["user_id"], "old_status": row["status"], "log_id": None}
        if row["status"] == new_status:
            return result
        conn.execute("UPDATE orders SET status = ? WHERE id = ?", (new_status, order_id))
        cursor = conn.execute(
            """
            INSERT INTO status_log (order_id, staff_id, old_status, new_status, changed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (order_id, staff_id, row["status"], new_status, now_moscow().isoformat()),
        )
        result["log_id"] = cursor.lastrowid
        return result


def mark_notified(log_id: int):
    """Telegram подтвердил, что сообщение клиенту ушло."""
    with closing(connect()) as conn, conn:
        conn.execute("UPDATE status_log SET notified = 1 WHERE id = ?", (log_id,))


def status_history(order_id: int) -> list[dict]:
    """Смены статуса заказа из панели, новые сверху, с именем сотрудника."""
    with closing(connect()) as conn:
        rows = conn.execute(
            """
            SELECT status_log.*, staff.name AS staff_name
            FROM status_log JOIN staff ON staff.id = status_log.staff_id
            WHERE order_id = ?
            ORDER BY status_log.id DESC
            """,
            (order_id,),
        ).fetchall()
    history = [dict(row) for row in rows]
    for entry in history:
        entry["changed_at"] = datetime.fromisoformat(entry["changed_at"])
    return history


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
