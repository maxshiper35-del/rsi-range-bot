"""Смоук ядра: движок импортируется и прогоняется на синтетике; risk.calculate_position считает."""
from __future__ import annotations


def test_engine_runs_on_synthetic():
    from app.core.signal_engine import Signal, SignalEngine, _synthetic_uptrend

    result = SignalEngine().analyze(_synthetic_uptrend(), "TEST/USDT", "1h")
    assert result is None or isinstance(result, Signal)


def test_calculate_position_basic():
    from app.brain.risk import calculate_position

    pos = calculate_position(1000, 2.0, 100.0, 98.0)
    assert pos["risk_amount"] == 20.0      # 2% от 1000
    assert pos["position_qty"] == 10.0     # риск 20 / (100-98)
    assert pos["notional_usd"] == 1000.0
    assert pos["warning"] is None          # плечо x1 < лимита
