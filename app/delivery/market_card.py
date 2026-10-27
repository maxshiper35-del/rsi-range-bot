"""Карточка рынка для inline-режима: «@rsirange_bot btc» в любом чате.

Запрос владельца 27.10.2026 — рост без бюджета: каждое использование inline-бота в чужом
чате показывает всем «via @rsirange_bot». Карточка — только общая картина рынка (цена,
24ч, RSI, тренд, волатильность) по закрытым барам Bybit; НИ сетапов, ни уровней входа —
граница публикации стратегии та же, что у роликов и рекламы. Чистые функции — для тестов;
загрузка баров и кеш — в хэндлере бота.
"""
from __future__ import annotations

import re

import pandas as pd

from app.core.signal_engine import Indicators
from app.delivery.templates import _price, esc

# быстрые подсказки на пустой запрос — самые частые монеты
DEFAULT_COINS = ("BTC", "ETH", "SOL")

_QUOTES = ("USDT", "USDC", "USD", "PERP")


def parse_coin(query: str) -> str | None:
    """'btc' / '#BTC' / 'btcusdt' / 'BTC/USDT' / '$sol' -> 'BTC/USDT:USDT'; мусор -> None.

    Тикер — 2–15 латинских букв/цифр (1000PEPE, 1INCH). Котировку в конце срезаем:
    в карточке всегда линейный USDT-своп."""
    q = (query or "").strip().upper().lstrip("#$")
    q = q.replace("/USDT:USDT", "").replace("/", "").replace("-", "").replace(" ", "")
    for quote in _QUOTES:
        if q.endswith(quote) and len(q) > len(quote) + 1:
            q = q[: -len(quote)]
            break
    if not re.fullmatch(r"[A-Z0-9]{2,15}", q) or q.isdigit() or q in _QUOTES:
        return None
    return f"{q}/USDT:USDT"


def _trend(h4: pd.DataFrame) -> str:
    close = h4["close"]
    last = float(close.iloc[-1])
    e50 = float(Indicators.ema(close, 50).iloc[-1])
    e200 = float(Indicators.ema(close, 200).iloc[-1])
    if last > e50 > e200:
        return "восходящий"
    if last < e50 < e200:
        return "нисходящий"
    return "без явного тренда"


def _rsi_note(v: float) -> str:
    if v >= 70:
        return " (перекуплен)"
    if v <= 30:
        return " (перепродан)"
    return ""


def card_data(symbol: str, h1: pd.DataFrame, h4: pd.DataFrame) -> dict:
    """Метрики карточки по ЗАКРЫТЫМ барам: 1h — цена/24ч/RSI/ATR, 4h — RSI и тренд.

    h1 нужен ≥ 25 баров (24ч назад), h4 ≥ 200 (EMA200); иначе ValueError — молодая монета."""
    if len(h1) < 25 or len(h4) < 200:
        raise ValueError("мало истории")
    last = float(h1["close"].iloc[-1])
    day_ago = float(h1["close"].iloc[-25])
    day = h1.tail(24)
    atr = float(Indicators.atr(h1, 14).iloc[-1])
    return {
        "coin": symbol.split("/")[0],
        "price": last,
        "chg24": (last / day_ago - 1) * 100,
        "hi24": float(day["high"].max()),
        "lo24": float(day["low"].min()),
        "rsi1h": float(Indicators.rsi(h1["close"], 14).iloc[-1]),
        "rsi4h": float(Indicators.rsi(h4["close"], 14).iloc[-1]),
        "trend4h": _trend(h4),
        "atr_pct": atr / last * 100,
    }


def card_title(d: dict) -> str:
    """Строка в списке inline-результатов (без HTML)."""
    return f"{d['coin']} · {_price(d['price'])} · {d['chg24']:+.1f}% за 24ч"


def card_description(d: dict) -> str:
    return f"RSI 1h {d['rsi1h']:.0f} · RSI 4h {d['rsi4h']:.0f} · тренд 4h: {d['trend4h']}"


def render_card(d: dict) -> str:
    """Текст сообщения, которое уйдёт в чат (parse_mode=HTML)."""
    return (
        f"🪙 <b>{esc(d['coin'])}/USDT</b> · <code>{_price(d['price'])}</code>\n"
        f"24ч: {d['chg24']:+.2f}% · диапазон <code>{_price(d['lo24'])}</code> – "
        f"<code>{_price(d['hi24'])}</code>\n"
        f"RSI 1h: {d['rsi1h']:.0f}{_rsi_note(d['rsi1h'])} · "
        f"RSI 4h: {d['rsi4h']:.0f}{_rsi_note(d['rsi4h'])}\n"
        f"Тренд 4h: {d['trend4h']}\n"
        f"Волатильность 1h (ATR): {d['atr_pct']:.2f}%\n\n"
        "<i>Это не сигнал. Сигналы со стопом и открытой статистикой всех сделок — "
        "@rsirange_bot</i>"
    )
