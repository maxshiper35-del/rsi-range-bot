"""
risk.py — размер позиции от риска 1–2% депозита, лимит плеча.
Сигнатура calculate_position сохраняется из ядра (переезд из signal_engine).

Статус: тонкая обёртка каркаса — переиспользует calculate_position из ядра,
чтобы пакет был рабочим уже сейчас. Расширение — задача для Fable 5.
См. PLAN_FABLE5.md → «1. risk.py».
"""
from __future__ import annotations

from app.core.signal_engine import calculate_position  # noqa: F401 (публичный ре-экспорт)

__all__ = ["calculate_position"]

# TODO(Fable 5):
#   - attach_position(signal, deposit, risk_pct) -> dict: довесить сайзинг к Signal;
#   - округление qty под market['precision']['amount'] и min notional биржи;
#   - валидация входов (deposit>0, 0<risk_pct<=5), понятные ошибки;
#   - тесты из PLAN_FABLE5.md → «1. risk.py».
