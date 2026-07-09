"""Прогон миграций: создаёт/обновляет схему БД. Запуск: make init-db (или python -m scripts.init_db).

Требует заданного окружения (см. .env.example) — config.get_settings() валидирует секреты fail-fast.
"""
from __future__ import annotations

from pathlib import Path

from app import db
from app.logging_conf import get_logger


def main() -> None:
    log = get_logger("init_db")
    db.init_db(Path("migrations"))
    tables = [r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    log.info("Схема БД инициализирована. Таблицы: %s", ", ".join(tables))


if __name__ == "__main__":
    main()
