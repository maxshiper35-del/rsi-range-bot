"""Запуск бэктеста SMC-движка из CLI: make backtest (без аргументов — как раньше).

Аргументы (бриф F/G: мульти-ТФ и TP-лесенка — только через бэктест-гейт):
  --symbol BTC/USDT:USDT   --tf 1h   --limit 1000
  --setups "1:1"           только перечисленные сетапы (CSV; дефолт — все)
  --ladder "1:0.5,2:0.5"   лесенка частичных выходов R:доля (дефолт — TP сетапа целиком)
  --be 1                   перевод стопа в безубыток после N закрытых траншей
  --time-filter            включить окно времени NY (дефолт: выключено, как раньше)

Пробует живые данные (ccxt через fetch_ohlcv), при отсутствии сети — синтетика ядра,
чтобы команда всегда что-то печатала. Для продакшена — подать сюда исторический CSV.
"""
from __future__ import annotations

import argparse

from app.brain.smc_setups import SMCConfig
from app.logging_conf import get_logger
from app.sim.backtest import run_backtest


def parse_ladder(raw: str) -> list:
    """'1:0.5,2:0.5' -> [(1.0, 0.5), (2.0, 0.5)]. Доли должны суммироваться в 1.0."""
    ladder = []
    for part in raw.split(","):
        rm, fr = part.split(":")
        ladder.append((float(rm), float(fr)))
    total = sum(fr for _, fr in ladder)
    if abs(total - 1.0) > 1e-9:
        raise ValueError(f"доли лесенки должны суммироваться в 1.0, получено {total:g}")
    return ladder


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Walk-forward бэктест SMC-движка")
    p.add_argument("--symbol", default="BTC/USDT:USDT")
    p.add_argument("--tf", default="1h")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--months", type=float, default=None,
                   help="история в месяцах через пагинацию (перекрывает --limit; "
                        "одиночный fetch у Bybit ограничен ~1000 барами)")
    p.add_argument("--setups", default=None,
                   help="CSV сетапов, напр. '1:1' или '6226,1:1' (дефолт — все)")
    p.add_argument("--ladder", default=None,
                   help="лесенка 'R:доля,R:доля', напр. '1:0.5,2:0.5'")
    p.add_argument("--be", type=int, default=None,
                   help="стоп в безубыток после N закрытых траншей лесенки")
    p.add_argument("--time-filter", action="store_true",
                   help="включить окно времени NY (дефолт: выключено)")
    return p.parse_args(argv)


def main() -> None:
    log = get_logger("backtest")
    args = parse_args()
    symbol, tf = args.symbol, args.tf
    try:
        if args.months:
            from app.data.market_data import _TF_SECONDS, fetch_history
            bars = int(args.months * 30.44 * 86400 / _TF_SECONDS[tf])
            df = fetch_history(symbol, tf, target_bars=bars)
            log.info("live: %d баров %s %s (~%.1f мес, пагинация)",
                     len(df), symbol, tf, args.months)
        else:
            from app.core.signal_engine import fetch_ohlcv
            df = fetch_ohlcv(symbol, tf, limit=args.limit)
            log.info("live: %d баров %s %s", len(df), symbol, tf)
    except Exception as e:
        from app.core.signal_engine import _synthetic_uptrend
        df, symbol = _synthetic_uptrend(), "TEST/USDT"
        log.info("offline (%s) — синтетика, %d баров", type(e).__name__, len(df))

    cfg_kwargs: dict = {"use_time_filter": args.time_filter}
    if args.setups:
        cfg_kwargs["enabled_setups"] = tuple(s.strip() for s in args.setups.split(","))
    rep = run_backtest(
        df, symbol, tf, cfg=SMCConfig(**cfg_kwargs),
        tp_ladder=parse_ladder(args.ladder) if args.ladder else None,
        move_be_after=args.be,
    )
    print(rep.summary())
    print("по сторонам:", rep.by_side)


if __name__ == "__main__":
    main()
