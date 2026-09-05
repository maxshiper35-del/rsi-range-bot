"""Помесячный бэктест ТЕКУЩЕЙ боевой конфигурации на паритетном 5-летнем кэше ->
статический JSON для секции дашборда «Бэктест · по месяцам».

  .venv/bin/python -m scripts.backtest_monthly --out app/backtest_monthly.json

Живые правила (runtime прода): сетапы 6226+LTF, min_stop_pct 1.3, трейлинг 1.5/0.2
без тейка, cap 6 на сторону, тормоз 3 стопа / 6ч, дедуп, 1h+4h, 9 эталонных символов.
Издержки — ТЕКУЩАЯ модель входа (маркет по касанию, замер 31.08): тейкер 0.055%/сторона,
слип 0.02%, фандинг 0.125 bps/бар. Паритет-чек перед публикацией: by_setup должен
содержать 6226, LTF-BB, LTF-OB; n и ΣR сверяются с записанным бейслайном
(02.09.2026: n=5353, base +720.9R / taker +622.4R).
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.sim.portfolio import simulate_portfolio
from scripts.run_portfolio import load, parse_args

SYMS = ("BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT,1000PEPE/USDT:USDT,"
        "NEAR/USDT:USDT,ONDO/USDT:USDT,HYPE/USDT:USDT,ZEC/USDT:USDT")


def run(fee: float) -> dict:
    args = parse_args(["--symbols", SYMS, "--tfs", "1h,4h",
                       "--cache", "data/cache5y", "--signals", "data/cache5y/sig_parity",
                       "--setups", "6226,LTF", "--min-stop-pct", "1.3",
                       "--trail-activate", "1.5", "--trail-dist", "0.2",
                       "--fee", str(fee), "--slip", "0.02", "--funding", "0.125",
                       "--entry-fill", "touch"])
    bars, signals = load(args)
    rep = simulate_portfolio(
        bars, signals, max_open_per_side=6, cooldown_stops=3, cooldown_hours=6.0,
        dedup=True, fee_pct=fee, slippage_pct=0.02, funding_bps_per_bar=0.125,
        trail_activate_r=1.5, trail_distance_r=0.2, entry_fill="touch")
    per: dict = defaultdict(list)
    for t in rep.detail:
        per[(t.exit_dt.year, t.exit_dt.month)].append(t.result_r)
    # окно макс. просадки по ПОСДЕЛОЧНОЙ эквити (пик -> дно -> восстановление):
    # месячные ячейки прячут ход внутри месяцев, посетители спрашивают «откуда −50R»
    tr = sorted(rep.detail, key=lambda t: t.exit_dt)
    eq = peak = 0.0
    peak_dt = tr[0].exit_dt if tr else None
    worst = (0.0, None, None, 0.0)
    for t in tr:
        eq += t.result_r
        if eq > peak:
            peak, peak_dt = eq, t.exit_dt
        if peak - eq > worst[0]:
            worst = (peak - eq, peak_dt, t.exit_dt, peak)
    dd, pdt, tdt, pk = worst
    rec_dt = None
    eq = 0.0
    for t in tr:
        eq += t.result_r
        if tdt is not None and t.exit_dt > tdt and eq >= pk:
            rec_dt = t.exit_dt
            break
    dd_window = ({"r": round(dd, 2), "peak": pdt.date().isoformat(),
                  "trough": tdt.date().isoformat(),
                  "recovered": rec_dt.date().isoformat() if rec_dt else None,
                  "days": (tdt - pdt).days,
                  "trades": sum(1 for t in tr if pdt < t.exit_dt <= tdt)}
                 if pdt is not None and tdt is not None else None)
    months = {f"{y}-{m:02d}": {"n": len(rs), "r": round(sum(rs), 2),
                               "w": sum(1 for x in rs if x > 0)}
              for (y, m), rs in sorted(per.items())}
    setups = {k: v["trades"] for k, v in rep.by_setup.items()}
    if not {"6226", "LTF-BB", "LTF-OB"} <= set(setups):      # паритет-чек (чек-лист 09.08)
        raise SystemExit(f"паритет сломан: by_setup={setups}")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fee_pct": fee, "symbols": SYMS.split(","), "tfs": ["1h", "4h"],
        "period": ["2021-07", "2026-07"],
        "totals": {"trades": rep.trades, "winrate": round(rep.winrate, 4),
                   "expectancy_r": round(rep.expectancy_r, 4),
                   "total_r": round(rep.total_r, 2),
                   "profit_factor": round(rep.profit_factor, 3),
                   "max_dd_r": round(rep.max_drawdown_r, 2)},
        "by_setup": setups,
        "max_dd_window": dd_window,
        "months": months,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fee", type=float, default=0.055,
                    help="комиссия за сторону, %% (0.055 тейкер = текущая модель входа)")
    ap.add_argument("--out", default="app/backtest_monthly.json")
    a = ap.parse_args()
    data = run(a.fee)
    Path(a.out).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                           encoding="utf-8")
    t = data["totals"]
    print(f"{a.out}: n={t['trades']} ΣR={t['total_r']:+.1f} exp={t['expectancy_r']:+.3f} "
          f"месяцев={len(data['months'])} by_setup={data['by_setup']}")


if __name__ == "__main__":
    main()
