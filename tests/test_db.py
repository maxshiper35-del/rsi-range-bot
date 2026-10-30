"""Смоук слоя БД: миграция создаёт схему, запись/чтение сигнала и идемпотентность работают."""
from __future__ import annotations

import sqlite3

import pytest

_INSERT = ("INSERT INTO signals(symbol, side, timeframe, entry, stop, risk_reward, "
           "confidence, snapshot_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)")


def test_schema_and_signal_roundtrip(fresh_db):
    db = fresh_db
    tables = {r["name"] for r in
              db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"signals", "users", "payments", "paper_trades", "signal_events"} <= tables

    sid = db.execute(_INSERT, ("BTC/USDT:USDT", "LONG", "1h", 100.0, 98.0, 3.0, 0.8, "{}",
                               "2026-01-01T00:00:00+00:00"))
    assert sid == 1
    row = db.query_one("SELECT * FROM signals WHERE id=?", (sid,))
    assert row["symbol"] == "BTC/USDT:USDT"
    assert row["status"] == "open"          # дефолт статуса
    assert row["exchange"] == "bybit"       # дефолт биржи


def test_signals_unique_idempotency(fresh_db):
    db = fresh_db
    args = ("ETH/USDT:USDT", "SHORT", "4h", 50.0, 51.0, 3.0, 0.7, "{}",
            "2026-01-01T04:00:00+00:00")
    db.execute(_INSERT, args)
    # тот же бар/сторона/символ/биржа -> нарушение UNIQUE
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(_INSERT, args)


def test_side_check_constraint(fresh_db):
    db = fresh_db
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(_INSERT, ("BTC/USDT:USDT", "UP", "1h", 1.0, 0.9, 3.0, 0.6, "{}",
                             "2026-02-01T00:00:00+00:00"))  # side не в ('LONG','SHORT')


def test_migration_atomicity_partial_failure_rolls_back(fresh_db, tmp_path):
    """Миграция, упавшая на середине .sql, не должна оставлять частично применённую схему —
    иначе ретрай на следующем рестарте падает на 'table already exists' от первой половины."""
    db = fresh_db
    bad_dir = tmp_path / "migrations_bad"
    bad_dir.mkdir()
    (bad_dir / "999_broken.sql").write_text(
        "CREATE TABLE atomicity_test (id INTEGER PRIMARY KEY);\n"
        "THIS IS NOT VALID SQL;\n"
    )
    with pytest.raises(sqlite3.OperationalError):
        db.init_db(bad_dir)

    # ROLLBACK -> таблица из первого (валидного) statement не осталась висеть в схеме
    tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "atomicity_test" not in tables
    # и миграция не отмечена применённой -> ретрай возможен, а не заблокирован навсегда
    applied = {r["name"] for r in db.query("SELECT name FROM schema_migrations")}
    assert "999_broken.sql" not in applied

    # ретрай с исправленным файлом ТОГО ЖЕ имени проходит чисто (не "table already exists")
    (bad_dir / "999_broken.sql").write_text(
        "CREATE TABLE atomicity_test (id INTEGER PRIMARY KEY);\n")
    db.init_db(bad_dir)
    tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "atomicity_test" in tables
    applied = {r["name"] for r in db.query("SELECT name FROM schema_migrations")}
    assert "999_broken.sql" in applied


def test_tests_never_touch_repo_db_or_dotenv():
    """Страж изоляции (аудит надёжности 30.10): без fresh_db тесты ходят в сессионную
    временную БД, а не в data/signals.db (в основном checkout — живая прод-база), и
    боевой .env в тестовый процесс не подтягивается."""
    import pathlib

    import dotenv

    from app.config import get_settings

    repo = pathlib.Path(__file__).resolve().parents[1]
    db_path = get_settings().db_path.resolve()
    assert repo / "data" not in db_path.parents, db_path
    assert dotenv.load_dotenv() is False                 # заглушка conftest, не чтение .env


def test_real_db_cannot_be_opened(tmp_path, monkeypatch):
    """Жёсткий страж conftest (audit-хук): data/signals.db не открыть ни sqlite3 (путь, URI),
    ни open — и файл не создаётся. Проверка в tmp_path: сломанный страж не дотянется до
    настоящей БД."""
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    attempts = (lambda: sqlite3.connect("data/signals.db"),
                lambda: sqlite3.connect("file:data/signals.db?mode=ro", uri=True),
                lambda: open("data/signals.db-wal", "wb"))
    for attempt in attempts:
        with pytest.raises(pytest.fail.Exception, match="боевую БД"):
            attempt()
    assert not any((tmp_path / "data").iterdir())
