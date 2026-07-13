"""Тесты конфигурации логирования: ротация при LOG_FILE, дефолт без него."""
from __future__ import annotations

import importlib
import logging
from logging.handlers import RotatingFileHandler


def _reload_clean(monkeypatch, log_file, tmp_path):
    # сбрасываем корневой логгер и _configured, чтобы setup_logging отработал заново
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    if log_file is None:
        monkeypatch.delenv("LOG_FILE", raising=False)
    else:
        monkeypatch.setenv("LOG_FILE", str(log_file))
    import app.logging_conf as lc
    importlib.reload(lc)
    return lc


def test_log_file_uses_rotating_handler(monkeypatch, tmp_path):
    log_file = tmp_path / "logs" / "bot.log"
    lc = _reload_clean(monkeypatch, log_file, tmp_path)
    log = lc.get_logger("t")
    log.info("написали строку")
    handlers = logging.getLogger().handlers
    rotating = [h for h in handlers if isinstance(h, RotatingFileHandler)]
    assert rotating, "при LOG_FILE ожидается RotatingFileHandler"
    assert rotating[0].maxBytes > 0 and rotating[0].backupCount > 0   # ротация настроена
    assert log_file.exists() and "написали строку" in log_file.read_text()
    # каталог создаётся автоматически, если его не было
    assert log_file.parent.is_dir()


def test_no_log_file_keeps_basicconfig(monkeypatch, tmp_path):
    lc = _reload_clean(monkeypatch, None, tmp_path)
    lc.setup_logging()
    handlers = logging.getLogger().handlers
    # без LOG_FILE не заводим файловый ротатор (dev-режим — basicConfig в stderr)
    assert not any(isinstance(h, RotatingFileHandler) for h in handlers)
