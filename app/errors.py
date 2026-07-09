"""Доменные исключения проекта. Ловим точечно, не глушим всё подряд."""
from __future__ import annotations


class BotError(Exception):
    """Базовое доменное исключение."""


class ExchangeUnavailable(BotError):
    """Биржа недоступна / сетевые ошибки ccxt после ретраев."""


class InsufficientData(BotError):
    """Недостаточно баров для анализа (меньше минимума движка)."""


class PaymentError(BotError):
    """Ошибка обработки платежа (инвойс, вебхук, идемпотентность)."""


class ConfigError(BotError):
    """Некорректная конфигурация или отсутствующие секреты."""
