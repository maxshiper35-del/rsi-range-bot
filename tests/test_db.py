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
