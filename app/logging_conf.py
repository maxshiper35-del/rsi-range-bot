"""Единый конфиг логирования на процесс. Время UTC, без print, без секретов в логах.

LOG_LEVEL читается напрямую из окружения, чтобы настройка логов не тянула
за собой полную валидацию Settings (и не требовала токена бота).
"""
from __future__ import annotations

import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

_configured = False

# ротация лога, когда его пишет САМ процесс (LOG_FILE): без неё файл рос бы безгранично
_LOG_MAX_BYTES = 10 * 1024 * 1024      # 10 МБ на файл
_LOG_BACKUPS = 5                       # +5 архивов -> потолок ~60 МБ


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    logging.Formatter.converter = time.gmtime  # метки времени в логах — UTC
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s",
                            datefmt="%Y-%m-%dT%H:%M:%S")
    # LOG_FILE задан (прод под launchd) — процесс САМ пишет лог с ротацией, а launchd
    # редиректит stderr в ОТДЕЛЬНЫЙ crash-файл (иначе два писателя в один файл + ротация
    # с rename ломают открытый fd launchd). LOG_FILE не задан (dev) — прежнее поведение:
    # basicConfig в stderr, без ротации.
    log_file = os.getenv("LOG_FILE")
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            log_file, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8")
        handler.setFormatter(fmt)
        root = logging.getLogger()
        root.setLevel(level)
        root.addHandler(handler)       # только файловый — app-логи не дублируются в stderr
    else:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    _configured = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
