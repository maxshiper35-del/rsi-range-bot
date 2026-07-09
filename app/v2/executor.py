"""
executor.py (V2) — авто-исполнение на ключах юзера. Права ТОЛЬКО на торговлю, без вывода.
Ключи расшифровываются через app.crypto (Fernet) непосредственно перед ордером.

Статус: заглушка каркаса. Реализация — задача для Fable 5.
См. PLAN_FABLE5.md → «8. executor.py».
"""
from __future__ import annotations

from app.core.signal_engine import Signal


def execute_signal(tg_id: int, signal: Signal) -> dict:
    """Выставить вход + стоп + лесенку TP на аккаунте юзера через ccxt.

    TODO(Fable 5): сайзинг из risk.py под депозит/риск юзера, установка плеча,
    обработка частичных исполнений и уже открытых позиций, kill-switch и лимиты.
    НИКОГДА не трогать вывод средств.
    """
    raise NotImplementedError("см. PLAN_FABLE5.md → 8. executor.py")
