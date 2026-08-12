.PHONY: help init-db run run-webhook backtest test lint fmt typecheck clean install-hooks install-playwright

# Интерпретатор: venv проекта, если он есть, иначе системный python3.
# /usr/bin/python3 на macOS — шим Xcode: после апдейта Xcode он отказывается
# стартовать до принятия лицензии и уносит с собой прод и весь дев-тулинг
# (инцидент 30.07.2026, 9 суток простоя). venv от этого не зависит.
PY := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)

help:
	@echo "init-db            - применить миграции (создать/обновить схему БД)"
	@echo "run                - запустить Telegram-бота"
	@echo "run-webhook        - запустить Flask-webhook TradingView (v2)"
	@echo "backtest           - прогон стратегии на истории"
	@echo "backup-db          - бэкап data/signals.db в data/backups/ (ротация 14)"
	@echo "test               - pytest"
	@echo "lint               - ruff check (весь репозиторий)"
	@echo "fmt                - ruff format"
	@echo "typecheck          - mypy app"
	@echo "install-hooks      - поставить git pre-commit хук (lint+typecheck изменённого + test)"
	@echo "install-playwright - поставить headless Chromium для рендера карточки недели"
	@echo "clean              - удалить кэши"

init-db:
	$(PY) -m scripts.init_db

run:
	$(PY) -m app.delivery.bot

run-webhook:
	$(PY) -m app.v2.tradingview_webhook

backtest:
	$(PY) -m scripts.run_backtest

backup-db:
	$(PY) -m scripts.backup_db

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check app tests scripts

fmt:
	$(PY) -m ruff format app tests scripts

typecheck:
	$(PY) -m mypy app

install-hooks:
	cp scripts/hooks/pre-commit .git/hooks/pre-commit
	chmod +x .git/hooks/pre-commit
	@echo "pre-commit хук установлен: .git/hooks/pre-commit"

install-playwright:
	$(PY) -m playwright install chromium

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

# деплой закоммиченного HEAD на прод (Hetzner): тесты на сервере, рестарт только при зелёных
deploy:
	zsh scripts/deploy.sh
