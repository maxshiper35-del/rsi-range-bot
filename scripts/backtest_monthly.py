"""Помесячный бэктест ТЕКУЩЕЙ боевой конфигурации на паритетном 5-летнем кэше ->
статический JSON для секции дашборда «Бэктест · по месяцам».

  .venv/bin/python -m scripts.backtest_monthly --out app/backtest_monthly.json

Живые правила (runtime прода): сетапы 6226+LTF, min_stop_pct 1.3, трейлинг 1.5/0.2
без тейка, cap 6 на сторону, тормоз 3 стопа / 6ч, дедуп, 1h+4h, 9 эталонных символов.
Издержки — ТЕКУЩАЯ модель входа (маркет по касанию, замер 31.08): тейкер 0.055%/сторона,
слип 0.02%, фандинг 0.125 bps за 1h. Паритет-чек перед публикацией: by_setup должен
содержать 6226, LTF-BB, LTF-OB.

ИСХОД — САМ ЖИВОЙ СУДЬЯ (journal._outcome_from_bars) на 1h-барах для всех сигналов
(05.10.2026, аудит): прежняя модель _simulate_open пропускала стоп на баре филла,
судила 4h-сигналы 4h-барами и выходила по тейку — +622.4R против +75.8R по правилам
живого бота (n=5309, exp +0.014, DD 110.9R; 4 года до 07.2025 ≈ +1R, посл. 12 мес +72R).

КЭШ СИГНАЛОВ — sig_prod (06.10.2026): снят `dump_signals --prod` с гейтами движка прода
(сетапы и min_stop ДО выбора кандидата, HTF закрытыми свечами, окно 199). Старый
sig_parity терял ~40% сигналов прода. На sig_prod: n=6675, +59.3R, exp +0.009
(TRAIN +2.2R, OOS +57.1R); с реальным слипом демо (−0.08R/сделку) — минус на обеих половинах.
С 07.10.2026 прод — с гейтом «вход уже пройден» (late_entry_gate=entry, замер 06.10):
снимок sig_prod_late_entry, n=5819, +358.1R, exp +0.061 (TRAIN +251.8R, OOS +106.3R).
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.sim.portfolio import simulate_portfolio
from scripts.run_portfolio import load, parse_args

SYMS = ("BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT,1000PEPE/USDT:USDT,"
        "NEAR/USDT:USDT,ONDO/USDT:USDT,HYPE/USDT:USDT,ZEC/USDT:USDT")
# кэш сигналов для бэктеста и замеров; SIG_DIR=data/cache5y/sig_prod — снимок с паритетом
# конфига движка (dump_signals --prod, 06.10.2026)
# прод с 07.10.2026 — гейт «вход уже пройден» (late_entry_gate=entry): снимок
# sig_prod_late_entry; sig_prod — тот же прод-конфиг без гейта (база замера 06.10)
SIG_DIR = os.environ.get("SIG_DIR", "data/cache5y/sig_prod_late_entry")
# эталон базы (M0, 1h+4h, весь период) по кэшу: сверка, что замер мерит то же, что бэктест
_BASELINES = {"sig_prod_late_entry": (5819, 358.1), "sig_prod": (6675, 59.3),
              "sig_parity": (5309, 73.3)}


def check_baseline(n: int, total: float) -> str:
    ref = _BASELINES.get(SIG_DIR.rstrip("/").rsplit("/", 1)[-1])
    if ref is None:
        return f"бейслайн на {SIG_DIR}: n={n} Σ={total:+.1f}R (эталона нет)"
    assert n == ref[0] and abs(total - ref[1]) < 0.5, f"бейслайн {SIG_DIR}: n={n} Σ={total}"
    return f"бейслайн воспроизведён ({SIG_DIR}: n={n}, {total:+.1f}R)"


def run(fee: float) -> dict:
    args = parse_args(["--symbols", SYMS, "--tfs", "1h,4h",
                       "--cache", "data/cache5y", "--signals", SIG_DIR,
                       "--setups", "6226,LTF", "--min-stop-pct", "1.3",
                       "--trail-activate", "1.5", "--trail-dist", "0.2",
                       "--fee", str(fee), "--slip", "0.02", "--funding", "0.125",
                       "--entry-fill", "touch"])
    bars, signals = load(args)
    h1 = {sym: df for (sym, tf), df in bars.items() if tf == "1h"}
    rep = simulate_portfolio(
        bars, signals, max_open_per_side=6, cooldown_stops=3, cooldown_hours=6.0,
        dedup=True, fee_pct=fee, slippage_pct=0.02, funding_bps_per_bar=0.125,
        trail_activate_r=1.5, trail_distance_r=0.2, entry_fill="touch",
        live_judge_bars=h1, tp_exit=False, max_hold_hours=168.0)
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
    print(f"паритет-чек: by_setup={setups}")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fee_pct": fee, "symbols": SYMS.split(","), "tfs": ["1h", "4h"],
        "period": ["2021-07", "2026-07"],
        "totals": {"trades": rep.trades, "winrate": round(rep.winrate, 4),
                   "expectancy_r": round(rep.expectancy_r, 4),
                   "total_r": round(rep.total_r, 2),
                   "profit_factor": round(rep.profit_factor, 3),
                   "max_dd_r": round(rep.max_drawdown_r, 2)},
        # by_setup НЕ пишем: файл отдаётся публично (/api/backtest), а имена сетапов —
        # закрытая таксономия (утечка 05.09–05.10.2026). Паритет-чек — выше, в консоль.
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
          f"месяцев={len(data['months'])}")


if __name__ == "__main__":
    main()
