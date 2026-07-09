"""Тесты бэкапа БД: копия читается отдельным соединением, ротация работает."""
from __future__ import annotations

import sqlite3


def test_backup_readable_by_separate_connection(fresh_db, tmp_path):
    from scripts.backup_db import backup_once
    fresh_db.execute(
        "INSERT INTO signals(symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("BTC/USDT:USDT", "LONG", "15m", 100.0, 98.0, 3.0, 0.8, "{}",
         "2026-01-01T00:00:00+00:00"))

    dst = backup_once(dst_dir=tmp_path / "backups")
    assert dst.exists() and dst.name.startswith("signals-")

    # копия консистентна и читается независимым соединением
    conn = sqlite3.connect(dst)
    try:
        rows = conn.execute("SELECT symbol, side FROM signals").fetchall()
    finally:
        conn.close()
    assert rows == [("BTC/USDT:USDT", "LONG")]


def test_backup_rotation(fresh_db, tmp_path):
    from scripts.backup_db import backup_once
    dst_dir = tmp_path / "backups"
    dst_dir.mkdir()
    # 5 старых фейковых бэкапов (имена сортируются лексикографически по времени)
    for i in range(5):
        (dst_dir / f"signals-2020010{i}T000000Z.db").write_bytes(b"old")

    newest = backup_once(dst_dir=dst_dir, keep_last=3)
    remaining = sorted(p.name for p in dst_dir.glob("signals-*.db"))
    assert len(remaining) == 3                      # 5 старых + 1 новый -> оставили 3
    assert newest.name in remaining                 # свежий бэкап не удалён
    assert "signals-20200100T000000Z.db" not in remaining   # старейшие удалены
