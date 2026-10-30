"""Общие фикстуры тестов: dummy-секреты + изолированная временная БД на каждый тест."""
from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile

import pytest

# Секреты не нужны для тестов, но config.get_settings() требует токен (fail-fast).
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ENV", "test")

# Тесты НИКОГДА не трогают боевые data/signals.db и .env (аудит надёжности 30.10). Тесты
# без fresh_db (форматтеры, читающие runtime) ходили в БД по дефолтному относительному
# пути: в основной checkout — это ЖИВАЯ прод-база (pre-commit гоняет полный pytest на
# каждом коммите), в свежем worktree — пустой файл и «no such table». А load_dotenv() из
# get_settings находит .env, поднимаясь от app/ вверх, — из любого worktree это боевой
# .env основного checkout (адреса пульса, ключи бирж — в окружение тестового процесса).
# Принудительно: внешний DB_PATH тестам не указ; per-test fresh_db перекрывает его своим.
_SESSION_DIR = pathlib.Path(tempfile.mkdtemp(prefix="rsirange-tests-"))
os.environ["DB_PATH"] = str(_SESSION_DIR / "session.db")
try:
    import dotenv

    dotenv.load_dotenv = lambda *a, **k: False      # type: ignore[assignment]
except ImportError:                                 # pragma: no cover — dotenv в зависимостях
    pass


# Жёсткий страж поверх DB_PATH: открыть data/signals.db (и её -wal/-shm) в тестовом процессе
# не может никто — ни app.db при пустом DB_PATH (_env считает пустую дефолтом), ни скрипт с
# дефолтным --db data/signals.db, ни сырой sqlite3/open/shutil. Audit-хук срабатывает ДО
# открытия файла (в свежем worktree не остаётся и пустого signals.db) и не обходится ранним
# `from sqlite3 import connect`. Матч по форме пути, а не по корню репо: из worktree боевая
# база основного checkout достижима как ../../../data/signals.db. pytest.fail бросает
# BaseException — `except Exception` в коде под тестом его не проглотит.
def _forbid_real_db(event: str, args: tuple) -> None:
    if event not in ("sqlite3.connect", "open"):
        return
    raw = args[0]
    if not isinstance(raw, (str, bytes, os.PathLike)):
        return                                      # open(fd) — не путь
    target = os.fsdecode(raw)
    if "signals.db" not in target:                  # быстрый выход: хук видит каждый open
        return
    path = pathlib.Path(target.removeprefix("file:").split("?")[0]).resolve()   # и URI sqlite
    if path.parent.name == "data" and path.name.startswith("signals.db"):
        pytest.fail(f"тест открывает боевую БД {path} — нужна фикстура fresh_db или tmp_path")


sys.addaudithook(_forbid_real_db)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = REPO_ROOT / "migrations"


def _reset_conn(db) -> None:
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        conn.close()
        del db._local.conn


@pytest.fixture(scope="session", autouse=True)
def _session_db():
    """Общая мигрированная БД сессии для тестов без fresh_db (вместо боевой)."""
    from app.config import get_settings

    get_settings.cache_clear()
    import app.db as db

    _reset_conn(db)
    db.init_db(MIGRATIONS)
    yield
    _reset_conn(db)
    get_settings.cache_clear()
    shutil.rmtree(_SESSION_DIR, ignore_errors=True)


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
    # серверный кеш /api/stats переживает границы тестов — каждому тесту чистый
    import app.webapp as _webapp

    _webapp._STATS_CACHE["body"], _webapp._STATS_CACHE["at"] = None, 0.0
    _webapp._TICKERS.update({"prices": {}, "pcts": {}, "tried": {}, "ok": {}})
    yield db
    _reset_conn(db)
    get_settings.cache_clear()
