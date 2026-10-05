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


def test_main_records_backup_timestamp(fresh_db, monkeypatch, tmp_path):
    """main() пишет timestamp успешного бэкапа в settings (runtime) — читает /health и
    алерт о пропуске; backup_once замокан, чтобы не трогать реальный data/backups."""
    from app import runtime
    from scripts import backup_db

    fake_path = tmp_path / "signals-fake.db"
    fake_path.write_bytes(b"x")
    monkeypatch.setattr(backup_db, "backup_once", lambda: fake_path)
    # ИЗОЛЯЦИЯ: без подмены main() писал в НАСТОЯЩИЙ iCloud (signals-fake.db вытеснил
    # реальную копию, аудит 05.10) и copy-truncate'ил боевые logs/
    monkeypatch.setattr(backup_db, "_ICLOUD_DIR", tmp_path / "icloud" / "RSIRange-Backups")
    monkeypatch.setattr(backup_db, "rotate_logs", lambda *a, **k: 0)

    assert runtime.last_backup_at() is None
    backup_db.main()
    assert runtime.last_backup_at() is not None


def test_backup_integrity_rejects_corrupt(fresh_db, tmp_path):
    import pytest

    from scripts.backup_db import _verify_integrity
    # валидная копия проходит
    good = backup_once_helper(fresh_db, tmp_path)
    _verify_integrity(good)                              # не бросает
    # битый файл (не SQLite) отклоняется и удаляется
    bad = tmp_path / "signals-bad.db"
    bad.write_bytes(b"this is not a sqlite database at all")
    with pytest.raises(RuntimeError):
        _verify_integrity(bad)
    assert not bad.exists()                             # битую копию удалили


def backup_once_helper(fresh_db, tmp_path):
    from scripts.backup_db import backup_once
    return backup_once(dst_dir=tmp_path / "bk")


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


def test_rotation_ignores_foreign_names(tmp_path, monkeypatch):
    """Посторонний signals-*.db не занимает слот ротации и не удаляется ею."""
    from scripts import backup_db
    d = tmp_path / "RSIRange-Backups"
    d.mkdir(parents=True)
    for day in range(1, 10):
        (d / f"signals-202610{day:02d}T020000Z.db").write_bytes(b"x")
    (d / "signals-fake.db").write_bytes(b"x")
    backup_db._rotate(d, keep_last=7)
    left = sorted(f.name for f in d.iterdir())
    assert "signals-fake.db" in left                   # не трогаем чужое
    assert len([n for n in left if n != "signals-fake.db"]) == 7
    assert "signals-20261009T020000Z.db" in left and "signals-20261002T020000Z.db" not in left
    # оффсайт: та же логика
    monkeypatch.setattr(backup_db, "_ICLOUD_DIR", d)
    src = tmp_path / "signals-20261010T020000Z.db"
    src.write_bytes(b"y")
    backup_db.offsite_copy(src, keep_last=7)
    names = sorted(f.name for f in d.iterdir() if f.name != "signals-fake.db")
    assert len(names) == 7 and names[-1] == "signals-20261010T020000Z.db"
