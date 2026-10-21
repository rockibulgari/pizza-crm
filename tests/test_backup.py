"""Ночная резервная копия (backup.py)."""
import sqlite3
from datetime import datetime, timedelta

import backup
from conftest import DB_FILE, add_order


def test_backup_creates_checked_copy(tmp_path, capsys):
    add_order("2026-10-20T12:00:00+03:00")
    add_order("2026-10-21T13:00:00+03:00")
    assert backup.main(["--db", str(DB_FILE), "--dir", str(tmp_path)]) == 0

    copies = list(tmp_path.glob("orders-auto-*.db"))
    assert len(copies) == 1 and backup.NAME.match(copies[0].name)
    assert not list(tmp_path.glob("*.part"))
    with sqlite3.connect(copies[0]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2
    assert "целостность ok, заказов: 2" in capsys.readouterr().out


def test_backup_while_database_is_being_written(tmp_path):
    """Копия во время незавершённой записи: в копию попадает только сохранённое."""
    add_order("2026-10-21T13:00:00+03:00")
    writer = sqlite3.connect(DB_FILE)
    writer.execute("BEGIN")
    writer.execute("UPDATE orders SET status = 'принят'")  # ещё не сохранено
    try:
        assert backup.main(["--db", str(DB_FILE), "--dir", str(tmp_path)]) == 0
    finally:
        writer.rollback()
        writer.close()
    copy = next(tmp_path.glob("orders-auto-*.db"))
    assert backup.check_copy(copy).startswith("целостность ok")
    with sqlite3.connect(copy) as conn:
        assert conn.execute("SELECT status FROM orders").fetchone()[0] == "новый"


def test_old_auto_copies_removed_manual_kept(tmp_path):
    now = datetime.now(backup.MOSCOW)
    names = {
        "keep_13": f"orders-auto-{(now - timedelta(days=13)):%Y-%m-%d}_040000.db",
        "drop_14": f"orders-auto-{(now - timedelta(days=14)):%Y-%m-%d}_040000.db",
        "drop_40": f"orders-auto-{(now - timedelta(days=40)):%Y-%m-%d}_040000.db",
        "manual": "orders-2026-01-01_120000.db",       # ручная копия — не трогаем
        "other": "notes.txt",
    }
    for name in names.values():
        (tmp_path / name).write_text("x")

    assert backup.main(["--db", str(DB_FILE), "--dir", str(tmp_path)]) == 0

    left = {p.name for p in tmp_path.iterdir()}
    assert names["keep_13"] in left and names["manual"] in left and names["other"] in left
    assert names["drop_14"] not in left and names["drop_40"] not in left
    assert len([n for n in left if backup.NAME.match(n)]) == 2  # 13-дневная + сегодняшняя


def test_missing_database_fails_and_removes_nothing(tmp_path, capsys):
    old = tmp_path / "orders-auto-2000-01-01_040000.db"
    old.write_text("x")
    assert backup.main(["--db", str(tmp_path / "nope.db"), "--dir", str(tmp_path)]) == 1
    assert old.exists(), "без новой копии старые не удаляются"
    assert "база не найдена" in capsys.readouterr().err


def test_verify_detects_broken_file(tmp_path, capsys):
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database at all" * 100)
    assert backup.main(["--verify", str(broken)]) == 1
    assert "Ошибка" in capsys.readouterr().err
