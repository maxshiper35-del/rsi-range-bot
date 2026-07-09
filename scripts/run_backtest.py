"""Запуск бэктеста SMC-движка из CLI: make backtest.

Пробует живые данные (ccxt через fetch_ohlcv), при отсутствии сети — синтетика ядра,
чтобы команда всегда что-то печатала. Для продакшена — подать сюда исторический CSV.
"""
from __future__ import annotations

from app.brain.smc_setups import SMCConfig
from app.logging_conf import get_logger
from app.sim.backtest import run_backtest


def main() -> None:
    log = get_logger("backtest")
    symbol, tf = "BTC/USDT:USDT", "1h"
    try:
        from app.core.signal_engine import fetch_ohlcv
        df = fetch_ohlcv(symbol, tf, limit=1000)
        log.info("live: %d баров %s %s", len(df), symbol, tf)
    except Exception as e:
        from app.core.signal_engine import _synthetic_uptrend
        df, symbol = _synthetic_uptrend(), "TEST/USDT"
        log.info("offline (%s) — синтетика, %d баров", type(e).__name__, len(df))

    rep = run_backtest(df, symbol, tf, cfg=SMCConfig(use_time_filter=False))
    print(rep.summary())
    print("по сторонам:", rep.by_side)


if __name__ == "__main__":
    main()
