.PHONY: help init-db run run-webhook backtest test lint fmt typecheck clean

help:
	@echo "init-db     - применить миграции (создать/обновить схему БД)"
	@echo "run         - запустить Telegram-бота"
	@echo "run-webhook - запустить Flask-webhook TradingView (v2)"
	@echo "backtest    - прогон стратегии на истории"
	@echo "backup-db   - бэкап data/signals.db в data/backups/ (ротация 14)"
	@echo "test        - pytest"
	@echo "lint        - ruff check"
	@echo "fmt         - ruff format"
	@echo "typecheck   - mypy app"
	@echo "clean       - удалить кэши"

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
	ruff check app tests scripts

fmt:
	ruff format app tests scripts

typecheck:
	mypy app

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
