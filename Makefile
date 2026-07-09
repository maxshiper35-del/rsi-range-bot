.PHONY: help init-db run run-webhook backtest test lint fmt typecheck clean install-hooks install-playwright

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
	python3 -m scripts.init_db

run:
	python3 -m app.delivery.bot

run-webhook:
	python3 -m app.v2.tradingview_webhook

backtest:
	python3 -m scripts.run_backtest

backup-db:
	python3 -m scripts.backup_db

test:
	python3 -m pytest

lint:
	python3 -m ruff check app tests scripts

fmt:
	python3 -m ruff format app tests scripts

typecheck:
	python3 -m mypy app

install-hooks:
	cp scripts/hooks/pre-commit .git/hooks/pre-commit
	chmod +x .git/hooks/pre-commit
	@echo "pre-commit хук установлен: .git/hooks/pre-commit"

install-playwright:
	python3 -m playwright install chromium

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
