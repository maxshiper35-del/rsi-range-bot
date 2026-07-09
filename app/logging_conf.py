"""Единый конфиг логирования на процесс. Время UTC, без print, без секретов в логах.

LOG_LEVEL читается напрямую из окружения, чтобы настройка логов не тянула
за собой полную валидацию Settings (и не требовала токена бота).
"""
from __future__ import annotations

import logging
import os
import time

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    logging.Formatter.converter = time.gmtime  # метки времени в логах — UTC
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    _configured = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
