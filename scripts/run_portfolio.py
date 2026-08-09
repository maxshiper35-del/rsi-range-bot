"""Портфельный прогон: весь вотчлист сразу, с живыми правилами бота.

  python3 -m scripts.run_portfolio --symbols "BTC/USDT:USDT,ETH/USDT:USDT" \
      --tfs 1h,4h --cap 6 --cooldown-stops 3 --cooldown-hours 6 --json

Читает бары из data/cache (scripts.dump_history) и предрасчитанные сигналы из
data/cache/signals (scripts.dump_signals) — сами сигналы от портфельных правил
не зависят, поэтому перебор лимитов/тормоза стоит секунды.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from app.sim.portfolio import simulate_portfolio
from scripts.dump_history import cache_name
from scripts.run_backtest import parse_ladder


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Портфельная симуляция вотчлиста")
    p.add_argument("--symbols", required=True)
    p.add_argument("--tfs", default="1h,4h")
    p.add_argument("--cache", default="data/cache")
    p.add_argument("--signals", default="data/cache/signals")
    p.add_argument("--cap", type=int, default=6, help="max_open_per_side (0 = без лимита)")
    p.add_argument("--cooldown-stops", type=int, default=3, help="0 = тормоз выключен")
    p.add_argument("--cooldown-hours", type=float, default=6.0)
    p.add_argument("--cooldown-losses-only", action="store_true",
                   help="в тормоз стороны считать только УБЫТОЧНЫЕ выходы по стопу "
                        "(при выходе только по трейлингу прибыльная фиксация тоже 'stop')")
    p.add_argument("--no-dedup", action="store_true")
    p.add_argument("--ladder", default=None)
    p.add_argument("--be", type=int, default=None)
    p.add_argument("--min-conf", type=float, default=None,
                   help="фильтр по confidence поверх кэша сигналов")
    p.add_argument("--min-stop-pct", type=float, default=None,
                   help="мин. дистанция стопа в %% от входа: при 0.6%% издержки "
                        "(комиссия 2×0.055%% + проскальзывание 0.02%%) съедают ~0.2R, "
                        "при 0.13%% — целый 1R. Отсекает 'мёртвые' сигналы")
    p.add_argument("--max-stop-pct", type=float, default=None,
                   help="макс. дистанция стопа в %% от входа (аномально широкие стопы)")
    p.add_argument("--setups", default=None, help="оставить только эти сетапы (CSV)")
    p.add_argument("--trail-activate", type=float, default=None,
                   help="включить трейлинг после хода +N·R (напр. 1.0)")
    p.add_argument("--trail-dist", type=float, default=1.0,
                   help="дистанция трейлинга от пика в R (по умолчанию 1.0)")
    p.add_argument("--max-hold", type=int, default=None,
                   help="принудительный выход по close через N баров в позиции")
    p.add_argument("--from-dt", default=None,
                   help="брать сигналы с этой даты (OOS-срез, ISO: 2026-01-25)")
    p.add_argument("--to-dt", default=None, help="брать сигналы до этой даты (не включая)")
    p.add_argument("--fee", type=float, default=0.055,
                   help="комиссия за сторону, %% (Bybit taker 0.055, maker 0.02)")
    p.add_argument("--slip", type=float, default=0.02, help="проскальзывание, %%")
    p.add_argument("--funding", type=float, default=0.0,
                   help="фандинг в bps на бар (Bybit ~0.01%%/8ч = 0.125 bps на 1h-бар); "
                        "критично для схем без фиксированного тейка — позиция висит долго")
    p.add_argument("--label", default=None)
    p.add_argument("--json", action="store_true")
    return p.parse_args(argv)


def _setup_enabled(tag: str, enabled: tuple) -> bool:
    """Та же префиксная семантика, что у SMCEngine._setup_enabled (smc_setups.py).

    Иначе инструмент замера молча меряет НЕ ТО, что поедет в прод: `--setups "6226,LTF"`
    при точном сравнении выкидывало LTF-BB/LTF-OB целиком, и прогон, подписанный
    «6226+LTF», на деле был одним 6226 (найдено 08.08.2026 по by_setup в отчёте).
    """
    return any(e and (tag == e or tag.startswith(e) or e.startswith(tag)) for e in enabled)


def load(args) -> tuple[dict, list]:
    cache, sig_dir = Path(args.cache), Path(args.signals)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    tfs = [t.strip() for t in args.tfs.split(",") if t.strip()]
    setups = tuple(s.strip() for s in args.setups.split(",") if s.strip()) if args.setups else None
    bars, signals = {}, []
    for symbol in symbols:
        for tf in tfs:
            bar_path = cache / cache_name(symbol, tf)
            sig_path = sig_dir / f"{cache_name(symbol, tf)[:-4]}.json"
            if not bar_path.exists() or not sig_path.exists():
                continue
            bars[(symbol, tf)] = pd.read_csv(bar_path, parse_dates=["dt"])
            for s in json.loads(sig_path.read_text()):
                if setups is not None and not _setup_enabled(s["setup"], setups):
                    continue
                if args.min_conf is not None and s["confidence"] < args.min_conf:
                    continue
                stop_pct = abs(s["entry"] - s["stop"]) / s["entry"] * 100.0
                if args.min_stop_pct is not None and stop_pct < args.min_stop_pct:
                    continue
                if args.max_stop_pct is not None and stop_pct > args.max_stop_pct:
                    continue
                if args.from_dt and str(s["dt"])[:len(args.from_dt)] < args.from_dt:
                    continue
                if args.to_dt and str(s["dt"])[:len(args.to_dt)] >= args.to_dt:
                    continue
                signals.append(s)
    return bars, signals


def main() -> None:
    args = parse_args()
    bars, signals = load(args)
    if not bars:
        raise SystemExit("нет кэша баров/сигналов — сначала dump_history и dump_signals")
    rep = simulate_portfolio(
        bars, signals,
        max_open_per_side=args.cap,
        cooldown_stops=args.cooldown_stops,
        cooldown_hours=args.cooldown_hours,
        cooldown_losses_only=args.cooldown_losses_only,
        dedup=not args.no_dedup,
        fee_pct=args.fee, slippage_pct=args.slip,
        funding_bps_per_bar=args.funding,
        tp_ladder=parse_ladder(args.ladder) if args.ladder else None,
        move_be_after=args.be,
        trail_activate_r=args.trail_activate,
        trail_distance_r=args.trail_dist,
        max_hold_bars=args.max_hold,
    )
    if args.json:
        print(json.dumps({
            "label": args.label, "symbols": rep.symbols, "tfs": rep.timeframes,
            "signals_total": rep.signals_total, "signals_taken": rep.signals_taken,
            "skipped": {"dedup": rep.skipped_dedup, "cap": rep.skipped_cap,
                        "cooldown": rep.skipped_cooldown},
            "trades": rep.trades, "winrate": round(rep.winrate, 4),
            "expectancy_r": rep.expectancy_r, "total_r": rep.total_r,
            "profit_factor": rep.profit_factor, "max_drawdown_r": rep.max_drawdown_r,
            "max_concurrent": rep.max_concurrent,
            "max_concurrent_side": rep.max_concurrent_side,
            "avg_hold_hours": rep.avg_hold_hours, "max_hold_hours": rep.max_hold_hours,
            "by_symbol": rep.by_symbol, "by_setup": rep.by_setup, "by_side": rep.by_side,
        }, ensure_ascii=False))
        return
    print(rep.summary())
    print("по символам:", rep.by_symbol)
    print("по сетапам:", rep.by_setup)
    print("по сторонам:", rep.by_side)


if __name__ == "__main__":
    main()
