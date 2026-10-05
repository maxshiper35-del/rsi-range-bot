"""Тонкий слой доступа к SQLite. WAL + соединение-на-поток, безопасно под async-бот."""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.config import get_settings

_local = threading.local()      # своё соединение на каждый поток-воркер
_write_lock = threading.Lock()  # SQLite допускает одного писателя


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        s = get_settings()
        s.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(s.db_path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        _local.conn = conn
    return conn


def query(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return get_conn().execute(sql, params).fetchall()


def query_one(sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    return get_conn().execute(sql, params).fetchone()


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    """INSERT -> lastrowid, иначе rowcount.

    Ветвление по типу statement ОБЯЗАТЕЛЬНО: sqlite3.lastrowid отражает
    last_insert_rowid() уровня СОЕДИНЕНИЯ — после любого INSERT последующий UPDATE
    на новом курсоре всё равно вернул бы rowid того INSERT (найдено аудитом-2:
    UPDATE после register_user возвращал tg_id вместо rowcount)."""
    with _write_lock:
        conn = get_conn()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
        except Exception:
            # упавший DML (IntegrityError/CHECK) оставлял неявную транзакцию открытой:
            # соединение потока держало write-lock до следующего commit (аудит 05.10)
            conn.rollback()
            raise
        if sql.lstrip()[:6].upper() == "INSERT":
            return cur.lastrowid or cur.rowcount
        return cur.rowcount


def executemany(sql: str, rows: Iterable[Sequence[Any]]) -> int:
    with _write_lock:
        conn = get_conn()
        try:
            cur = conn.executemany(sql, rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return cur.rowcount


@contextmanager
def transaction():
    """Атомарно для нескольких записей (journal: сигнал + событие исхода)."""
    with _write_lock:
        conn = get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def upsert(table: str, keys: dict[str, Any], values: dict[str, Any]) -> None:
    cols = list(keys) + list(values)
    ph = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in values)
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph}) "
           f"ON CONFLICT({', '.join(keys)}) DO UPDATE SET {updates}")
    execute(sql, [*keys.values(), *values.values()])


def init_db(migrations_dir: Path = Path("migrations")) -> None:
    """Прогон .sql-миграций по порядку, идемпотентно через schema_migrations.

    Каждая миграция — ОДНА атомарная транзакция (DDL-скрипт + запись в schema_migrations):
    BEGIN/COMMIT встроены В ТЕКСТ скрипта (executescript иначе не транзакционен — коммитит
    только ПРЕДШЕСТВУЮЩУЮ висящую транзакцию, но не оборачивает свои же statements).
    Падение посреди .sql -> полный ROLLBACK, схема не остаётся частично применённой,
    и на следующем рестарте миграция просто повторится с чистого листа, а не упадёт
    на 'table already exists' от уже применённой половины прошлого прогона.
    """
    conn = get_conn()
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                 "(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))")
    conn.commit()
    applied = {r["name"] for r in query("SELECT name FROM schema_migrations")}
    for path in sorted(migrations_dir.glob("*.sql")):
        if path.name in applied:
            continue
        script = path.read_text(encoding="utf-8")
        name_escaped = path.name.replace("'", "''")     # свои файлы миграций, не внешний ввод
        full = (f"BEGIN IMMEDIATE;\n{script}\n"
               f"INSERT INTO schema_migrations(name) VALUES ('{name_escaped}');\nCOMMIT;")
        with _write_lock:
            try:
                conn.executescript(full)
            except Exception:
                conn.rollback()       # BEGIN мог открыться, COMMIT — нет; закрыть транзакцию явно
                raise
