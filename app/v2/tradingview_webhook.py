"""
tradingview_webhook.py (V2) — Flask endpoint: алерт TradingView -> Signal -> конвейер.
Проверка общего секрета, rate-limit, нормализация символов/времени.

Статус: заглушка каркаса. Реализация — задача для Fable 5.
См. PLAN_FABLE5.md → «9. tradingview_webhook.py».
"""
from __future__ import annotations


def create_app():
    """Собрать Flask-приложение с эндпоинтом POST /webhook.

    TODO(Fable 5): валидация shared secret, парсинг payload -> Signal/директива,
    идемпотентность, маршрутизация в journal/рассылку/executor.
    """
    raise NotImplementedError("см. PLAN_FABLE5.md → 9. tradingview_webhook.py")


if __name__ == "__main__":
    create_app()
