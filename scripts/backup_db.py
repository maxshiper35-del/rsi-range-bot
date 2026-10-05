"""Авто-бэкап SQLite: make backup-db (или launchd-агент com.rsirange.backup, раз в сутки).

Использует встроенный SQLite Backup API (sqlite3.Connection.backup) — онлайн-копирование,
безопасно при активной записи под WAL, останавливать бота не нужно.
data/signals.db — единственный источник правды (сделки, подписчики, публичный трек-рекорд).
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app import clock, runtime
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
    # компаньоны -wal/-shm от проверочного соединения: не нужны, а НЕпустой -wal рядом
    # с восстановленной копией «доигрался» бы в неё при открытии (аудит-2 ops [10])
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


# только настоящие бэкапы: посторонний signals-<что-угодно>.db сортировался ПОСЛЕ меток
# времени, навсегда занимал слот и вытеснял реальную копию (05.10 тестовый
# signals-fake.db в iCloud вытеснил копию за 30.09 — аудит 05.10)
_BACKUP_NAME = re.compile(r"^signals-\d{8}T\d{6}Z\.db$")


def _backups(dst_dir: Path) -> list[Path]:
    return sorted(f for f in dst_dir.glob("signals-*.db") if _BACKUP_NAME.match(f.name))


def _rotate(dst_dir: Path, keep_last: int) -> None:
    """Оставить keep_last свежих файлов signals-*.db, старые удалить (с компаньонами)."""
    backups = _backups(dst_dir)
    for old in backups[:-keep_last] if keep_last > 0 else backups:
        old.unlink()
        for suffix in ("-wal", "-shm"):                     # ротация не копит сироты
            Path(str(old) + suffix).unlink(missing_ok=True)


_ICLOUD_DIR = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" \
    / "RSIRange-Backups"


def offsite_copy(src: Path, keep_last: int = 7) -> Path | None:
    """Копия свежего бэкапа в iCloud Drive (единственный «другой диск» локального
    прода на Mac): смерть SSD не уносит подписчиков/платежи/трек-рекорд вместе с
    основными бэкапами (аудит 02.09). Нет iCloud — молча пропускаем (не macOS/CI)."""
    if not _ICLOUD_DIR.parent.exists():
        get_logger("backup_db").warning("оффсайт пропущен: нет iCloud Drive (%s)",
                                        _ICLOUD_DIR.parent)
        return None
    _ICLOUD_DIR.mkdir(parents=True, exist_ok=True)
    import shutil
    dst = _ICLOUD_DIR / src.name
    shutil.copy2(src, dst)
    for old in _backups(_ICLOUD_DIR)[:-keep_last]:
        old.unlink()
    return dst


def rotate_logs(log_dir: Path = Path("logs"), max_bytes: int = 10 * 1024 * 1024) -> int:
    """Разросшиеся логи: copy-truncate в .1 (одно поколение). Без ротации bot.log
    растёт бесконечно (tunnel.log надуло 5.9 МБ ретрай-спама за 2.5 суток — аудит
    02.09; свой лог туннель теперь ротирует сам при старте). Именно copy-truncate:
    launchd и FileHandler держат fd открытым — rename оставил бы запись в .1 навсегда,
    truncate по живому O_APPEND-fd безопасен (теряем разве что строку на гонке)."""
    import shutil
    n = 0
    for f in log_dir.glob("*.log"):
        try:
            if f.stat().st_size > max_bytes:
                shutil.copy2(f, f.with_suffix(f.suffix + ".1"))
                with open(f, "r+b") as fh:
                    fh.truncate(0)
                n += 1
        except OSError:
            continue
    return n


def main() -> None:
    log = get_logger("backup_db")
    # СНАЧАЛА копия, без миграций: init_db здесь применял к боевой БД любые .sql из
    # рабочего дерева ДО снятия копии — «страховочный» бэкап уже содержал миграцию 023
    # (аудит 05.10), а сломанная миграция останавливала бы все бэкапы
    path = backup_once()
    log.info("бэкап создан: %s (%d байт)", path, path.stat().st_size)
    try:
        off = offsite_copy(path)
        if off is not None:
            log.info("оффсайт-копия: %s", off)
            runtime.set_value("last_offsite_at", clock.utcnow_iso())   # сторож в боте
    except Exception as e:                      # noqa: BLE001 — оффсайт не валит основной бэкап
        log.warning("оффсайт-копия не удалась: %s", e)
    try:
        rotated = rotate_logs()
        if rotated:
            log.info("ротация логов: %d файлов", rotated)
    except Exception as e:                      # noqa: BLE001
        log.warning("ротация логов: %s", e)
    # timestamp в settings (WAL, тот же файл, что и боевая БД) — не отдельный лог-файл,
    # который бот не читает; /health и алерт о пропуске (_maintenance) читают отсюда
    runtime.set_last_backup_at(clock.utcnow_iso())


if __name__ == "__main__":
    main()
