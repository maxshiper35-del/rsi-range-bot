"""Общие фикстуры тестов: dummy-секреты + изолированная временная БД на каждый тест."""
from __future__ import annotations

import os
import pathlib

import pytest

# Секреты не нужны для тестов, но config.get_settings() требует токен (fail-fast).
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ENV", "test")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = REPO_ROOT / "migrations"


def _reset_conn(db) -> None:
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        conn.close()
        del db._local.conn


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Изолированная БД на тест: свой файл, чистый кэш настроек и соединение.

    Трейлинг по умолчанию ВЫКЛЮЧЕН для тестов: подавляющее большинство проверок описывает
    базовую механику журнала (филл, стоп, тейк, дедуп, статистика), и она не должна
    зависеть от текущей боевой схемы выхода. Тесты самой схемы включают её явно —
    так каждый тест сам объявляет режим, который проверяет."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from app.config import get_settings

    get_settings.cache_clear()
    import app.db as db

    _reset_conn(db)
    db.init_db(MIGRATIONS)
    from app import runtime

    runtime.set_value("trail_activate_r", "0")
    yield db
    _reset_conn(db)
    get_settings.cache_clear()
