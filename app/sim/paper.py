"""
paper.py — бумажная торговля: форвард-тест + фича для юзера (воронка free -> paid).
Делит логику детекции исхода с journal.py (общий helper).

Статус: заглушка каркаса. Реализация — задача для Fable 5.
См. PLAN_FABLE5.md → «6. paper.py».
"""
from __future__ import annotations

from app.core.signal_engine import Signal


def open_paper_trade(tg_id: int, signal: Signal, deposit: float, risk_pct: float) -> int:
    """Открыть виртуальную сделку по сигналу (qty из risk.calculate_position). -> paper_trade_id."""
    raise NotImplementedError("см. PLAN_FABLE5.md → 6. paper.py")


def close_paper_trades(latest_prices: dict[str, float]) -> None:
    """Закрыть открытые бумажные сделки по TP/SL, записать pnl и result_r."""
    raise NotImplementedError("см. PLAN_FABLE5.md → 6. paper.py")
