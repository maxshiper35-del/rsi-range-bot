"""Авто-бэкап SQLite: make backup-db (или launchd-агент com.rsirange.backup, раз в сутки).

Использует встроенный SQLite Backup API (sqlite3.Connection.backup) — онлайн-копирование,
безопасно при активной записи под WAL, останавливать бота не нужно.
data/signals.db — единственный источник правды (сделки, подписчики, публичный трек-рекорд).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app import clock, db, runtime
from app.config import get_settings
from app.logging_conf import get_logger


def backup_once(dst_dir: Path = Path("data/backups"), keep_last: int = 14) -> Path:
    """Снять онлайн-копию боевой БД в dst_dir, отротировать старые. -> путь копии.

    Пишем во временный файл и переименовываем только после успешного backup —
    падение посреди копирования не оставляет битый signals-*.db, который занял бы
    слот ротации и вытеснил валидные старые копии.
    """
    src_path = Path(get_settings().db_path)
    if not src_path.exists():
        # sqlite3.connect по несуществующему пути молча создал бы пустую БД
        # и «успешно» забэкапил 0 таблиц
        raise FileNotFoundError(f"боевая БД не найдена: {src_path}")
    dst_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dst_path = dst_dir / f"signals-{stamp}.db"
    tmp_path = dst_dir / f"signals-{stamp}.db.tmp"

    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(tmp_path)
        try:
            src.backup(dst)                     # онлайн-копия, консистентна под WAL
        finally:
            dst.close()
    except BaseException:
        tmp_path.unlink() if tmp_path.exists() else None
        raise
    finally:
        src.close()

    tmp_path.replace(dst_path)                  # атомарно: в ротацию попадают только целые
    _verify_integrity(dst_path)                 # читаема и не побита ДО отметки об успехе
    _rotate(dst_dir, keep_last)
    return dst_path


def _verify_integrity(path: Path) -> None:
    """PRAGMA integrity_check на свежей копии перед тем, как считать бэкап успешным.
    Онлайн-backup под активной записью теоретически может дать нечитаемый файл — если
    отметить такой как успешный, watchdog промолчит, а восстанавливать будет нечего.
    Битую копию удаляем и падаем (main тогда НЕ вызовет set_last_backup_at)."""
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        ok, detail = (bool(row) and row[0] == "ok"), row
    except sqlite3.DatabaseError as e:                      # файл вообще не SQLite / нечитаем
        ok, detail = False, e
    finally:
        conn.close()
    if not ok:
        path.unlink(missing_ok=True)                        # closed до удаления
        raise RuntimeError(f"бэкап не прошёл integrity_check: {path} -> {detail}")


def _rotate(dst_dir: Path, keep_last: int) -> None:
    """Оставить keep_last свежих файлов signals-*.db, старые удалить."""
    backups = sorted(dst_dir.glob("signals-*.db"))
    for old in backups[:-keep_last] if keep_last > 0 else backups:
        old.unlink()


def main() -> None:
    log = get_logger("backup_db")
    db.init_db()                                # settings-таблица гарантированно существует
    path = backup_once()
    log.info("бэкап создан: %s (%d байт)", path, path.stat().st_size)
    # timestamp в settings (WAL, тот же файл, что и боевая БД) — не отдельный лог-файл,
    # который бот не читает; /health и алерт о пропуске (_maintenance) читают отсюда
    runtime.set_last_backup_at(clock.utcnow_iso())


if __name__ == "__main__":
    main()
