"""Запуск бэктеста SMC-движка из CLI: make backtest (без аргументов — как раньше).

Аргументы (бриф F/G: мульти-ТФ и TP-лесенка — только через бэктест-гейт):
  --symbol BTC/USDT:USDT   --tf 1h   --limit 1000
  --csv path.csv           читать бары из кэша (scripts/dump_history) вместо сети —
                           одинаковые бары для всех экспериментов + без рейт-лимита
  --setups "1:1"           только перечисленные сетапы (CSV; дефолт — все)
  --ladder "1:0.5,2:0.5"   лесенка частичных выходов R:доля (дефолт — TP сетапа целиком)
  --be 1                   перевод стопа в безубыток после N закрытых траншей
  --time-filter            включить окно времени NY (ПАРИТЕТ С ПРОДОМ: там ВКЛ)
  --htf 4h --htf-block     старший ТФ контекста и жёсткий блок контртренда
  --min-conf/--min-impulse/--window   рантайм-пороги живого движка (паритет)
  --bars-from/--bars-to    срез истории по индексам баров (walk-forward / OOS)
  --json                   машиночитаемый вывод для серии экспериментов

Пробует живые данные (ccxt через fetch_ohlcv), при отсутствии сети — синтетика ядра,
чтобы команда всегда что-то печатала. Для продакшена — подать сюда исторический CSV.
"""
from __future__ import annotations

import argparse
import json

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
    p.add_argument("--csv", default=None,
                   help="CSV-кэш баров (scripts/dump_history) — перекрывает сетевой фетч")
    p.add_argument("--setups", default=None,
                   help="CSV сетапов, напр. '1:1' или '6226,1:1' (дефолт — все)")
    p.add_argument("--ladder", default=None,
                   help="лесенка 'R:доля,R:доля', напр. '1:0.5,2:0.5'")
    p.add_argument("--be", type=int, default=None,
                   help="стоп в безубыток после N закрытых траншей лесенки")
    p.add_argument("--time-filter", action="store_true",
                   help="включить окно времени NY (в проде ВКЛ — для паритета ставить)")
    p.add_argument("--htf", default=None,
                   help="старший ТФ контекста (напр. 4h при --tf 1h): ресемпл окна, "
                        "как вживую поллер отдаёт движку ближайший старший фрейм")
    p.add_argument("--htf-block", action="store_true",
                   help="жёсткий блок контртренда от подтверждённой структуры HTF "
                        "(требует --htf)")
    p.add_argument("--min-conf", type=float, default=None,
                   help="min_confidence движка (прод: runtime, дефолт 0.5)")
    p.add_argument("--min-impulse", type=float, default=None,
                   help="min_impulse_atr движка (прод: runtime, дефолт 1.0)")
    p.add_argument("--window", type=int, default=300,
                   help="окно баров, видимое движку (прод live: 200)")
    p.add_argument("--bars-from", type=int, default=None,
                   help="срез истории: индекс первого бара (OOS/walk-forward)")
    p.add_argument("--bars-to", type=int, default=None,
                   help="срез истории: индекс последнего бара (не включая)")
    p.add_argument("--label", default=None, help="метка эксперимента в JSON-выводе")
    p.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    return p.parse_args(argv)


def load_df(args, log):
    """Бары для прогона: CSV-кэш -> пагинация -> одиночный фетч -> синтетика."""
    import pandas as pd

    if args.csv:
        df = pd.read_csv(args.csv, parse_dates=["dt"])
        log.info("csv: %d баров %s %s", len(df), args.symbol, args.tf)
        return df, args.symbol
    try:
        if args.months:
            from app.data.market_data import _TF_SECONDS, fetch_history
            bars = int(args.months * 30.44 * 86400 / _TF_SECONDS[args.tf])
            df = fetch_history(args.symbol, args.tf, target_bars=bars)
            log.info("live: %d баров %s %s (~%.1f мес, пагинация)",
                     len(df), args.symbol, args.tf, args.months)
        else:
            from app.core.signal_engine import fetch_ohlcv
            df = fetch_ohlcv(args.symbol, args.tf, limit=args.limit)
            log.info("live: %d баров %s %s", len(df), args.symbol, args.tf)
        return df, args.symbol
    except Exception as e:                              # noqa: BLE001 — оффлайн-фолбэк
        from app.core.signal_engine import _synthetic_uptrend
        log.info("offline (%s) — синтетика", type(e).__name__)
        return _synthetic_uptrend(), "TEST/USDT"


def main() -> None:
    log = get_logger("backtest")
    args = parse_args()
    df, symbol = load_df(args, log)
    if args.bars_from is not None or args.bars_to is not None:
        df = df.iloc[args.bars_from or 0:args.bars_to].reset_index(drop=True)
        log.info("срез: %d баров [%s..%s]", len(df),
                 args.bars_from or 0, args.bars_to if args.bars_to is not None else "конец")

    cfg_kwargs: dict = {"use_time_filter": args.time_filter}
    if args.setups:
        cfg_kwargs["enabled_setups"] = tuple(s.strip() for s in args.setups.split(","))
    if args.htf_block:
        cfg_kwargs["htf_block_countertrend"] = True
    if args.min_conf is not None:
        cfg_kwargs["min_confidence"] = args.min_conf
    if args.min_impulse is not None:
        cfg_kwargs["min_impulse_atr"] = args.min_impulse
    rep = run_backtest(
        df, symbol, args.tf, cfg=SMCConfig(**cfg_kwargs),
        window=args.window,
        tp_ladder=parse_ladder(args.ladder) if args.ladder else None,
        move_be_after=args.be,
        htf_tf=args.htf,
    )
    if args.json:
        print(json.dumps({
            "label": args.label, "symbol": symbol, "tf": args.tf,
            "bars": len(df), "signals": rep.signals, "trades": rep.trades,
            "wins": rep.wins, "losses": rep.losses,
            "winrate": round(rep.winrate, 4), "expectancy_r": round(rep.expectancy_r, 4),
            "total_r": round(rep.total_r, 3), "profit_factor": round(rep.profit_factor, 3),
            "max_drawdown_r": round(rep.max_drawdown_r, 3),
            "avg_mfe_r": round(rep.avg_mfe_r, 3), "avg_mae_r": round(rep.avg_mae_r, 3),
            "by_side": rep.by_side,
        }, ensure_ascii=False))
        return
    print(rep.summary())
    print("по сторонам:", rep.by_side)


if __name__ == "__main__":
    main()
