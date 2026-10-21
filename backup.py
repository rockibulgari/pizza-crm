"""Резервная копия базы заказов (запускается таймером systemd каждую ночь).

    python backup.py --db /root/pizza-bot/orders.db --dir /root/backup
    python backup.py --verify /root/backup/orders-auto-2026-10-22_040000.db

Копия делается встроенным в SQLite механизмом backup: он безопасен, даже если
бот или панель в этот момент пишут в базу (простой cp мог бы скопировать файл
посередине записи). Готовая копия проверяется (PRAGMA integrity_check)
и только потом получает своё имя.

Старые копии удаляются только после удачной новой и только свои —
файлы вида orders-auto-ГГГГ-ММ-ДД_ЧЧММСС.db. Ручные копии не трогаются.
"""
import argparse
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

MOSCOW = timezone(timedelta(hours=3))
PREFIX = "orders-auto-"
NAME = re.compile(r"^orders-auto-(\d{4}-\d{2}-\d{2})_\d{6}\.db$")


def check_copy(path: Path) -> str:
    """Проверяет файл копии. Возвращает сводку или бросает RuntimeError."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"копия повреждена: {result}")
        count, last = conn.execute("SELECT COUNT(*), MAX(created_at) FROM orders").fetchone()
    finally:
        conn.close()
    return f"целостность ok, заказов: {count}, последний заказ: {last or '—'}"


def make_backup(db: Path, folder: Path, now: datetime) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{PREFIX}{now:%Y-%m-%d_%H%M%S}.db"
    partial = target.with_name(target.name + ".part")
    source = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    copy = sqlite3.connect(partial)
    try:
        source.backup(copy)
    finally:
        copy.close()
        source.close()
    try:
        summary = check_copy(partial)
    except Exception:
        partial.unlink(missing_ok=True)  # битую копию не оставляем
        raise
    partial.rename(target)
    print(f"Копия готова: {target} ({summary})")
    return target


def remove_old(folder: Path, keep_days: int, now: datetime) -> list[Path]:
    """Удаляет свои копии, сделанные раньше чем keep_days дней назад (по дате в имени)."""
    oldest_kept = (now - timedelta(days=keep_days - 1)).date()
    removed = []
    for path in sorted(folder.iterdir()):
        match = NAME.match(path.name)
        if match and datetime.strptime(match.group(1), "%Y-%m-%d").date() < oldest_kept:
            path.unlink()
            removed.append(path)
            print(f"Удалена старая копия: {path.name}")
    return removed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Резервная копия orders.db")
    parser.add_argument("--db", type=Path, help="путь к orders.db")
    parser.add_argument("--dir", type=Path, help="папка для копий")
    parser.add_argument("--keep-days", type=int, default=14, help="сколько дней хранить (14)")
    parser.add_argument("--verify", type=Path, metavar="ФАЙЛ", help="только проверить готовую копию")
    args = parser.parse_args(argv)

    try:
        if args.verify:
            print(f"{args.verify}: {check_copy(args.verify)}")
            return 0
        if not args.db or not args.dir:
            parser.error("нужны --db и --dir (или --verify ФАЙЛ)")
        if not args.db.is_file():
            raise RuntimeError(f"база не найдена: {args.db}")
        now = datetime.now(MOSCOW)
        make_backup(args.db, args.dir, now)
        remove_old(args.dir, args.keep_days, now)
    except Exception as error:  # noqa: BLE001 — любая ошибка: код 1, systemd пометит запуск как failed
        print(f"Ошибка резервного копирования: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
