"""Паритет минутного судьи прода (app.brain.judge_1m) с эталоном замера реализма трейла.

  DATA_ROOT=/путь/к/data nice -n 15 .venv/bin/python -m scripts.judge_parity

1. База замера 2026-10-30-trail-realism (`measure_trail_realism.base`): 4 635 сделок прода
   «только 1h» на двух наборах, паритет n/ΣR по половинам.
2. На тех же минутках кэша (`data/cache_tf/m1`, без докачки) эталон E1m
   (`research_fine_judge.judge(mode="path", immediate=True, order="color")`) против
   `judge_1m.judge_path` в режиме паритета (окно входа — час входа журнала, gap_open=False):
   исход, gross R и минута выхода должны совпасть у КАЖДОЙ сделки.
3. Net R/сделку по половинам (издержки замера) против зафиксированных в документе:
   W1 −0.065, W2 −0.057, rot H1 −0.180, rot H2 −0.004 при n 1026 / 877 / 1292 / 1440.
4. Справочно — прод-режим (вход ищется с решающего бара, gap_open=True): net по половинам
   и сколько сделок он разрешил иначе.

Обязателен перед вливанием правок судьи. Долгий (минуты), сеть не трогает.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("DATA_ROOT", str(Path(__file__).resolve().parents[1] / "data"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import scripts.fetch_m1 as m1  # noqa: E402
import scripts.measure_trail_realism as mtr  # noqa: E402
import scripts.research_fine_judge as fj  # noqa: E402
import scripts.research_judge as rj  # noqa: E402
from app.brain.judge_1m import HOUR_MS, MIN_MS, judge_path  # noqa: E402

REF_NET = {"W1": -0.065, "W2": -0.057, "rot H1": -0.180, "rot H2": -0.004}
REF_N = {"W1": 1026, "W2": 877, "rot H1": 1292, "rot H2": 1440}
ACT, DIST, HOLD = 1.5, 0.2, 168.0
_EV = {"open": "unresolved", "waiting": "unresolved"}


def _mine(row, bars: np.ndarray, prod: bool) -> tuple:
    if prod:
        decision = mtr._ms(row.decision)
        v = judge_path(row.entry, row.stop, row.side, bars, decision_ms=decision,
                       fill_until_ms=decision + 50 * HOUR_MS, act_r=ACT, dist_r=DIST,
                       max_hold_h=HOLD, gap_open=True)
    else:
        v = judge_path(row.entry, row.stop, row.side, bars, decision_ms=row.fill_ms,
                       fill_from_ms=row.fill_ms, fill_until_ms=row.fill_ms + HOUR_MS,
                       act_r=ACT, dist_r=DIST, max_hold_h=HOLD, gap_open=False)
    return _EV.get(v.event, v.event), v.r, v.exit_ms, v.fill_ms


def main() -> int:
    print("== База замера (паритет 1–2)")
    tabs, origs = [], {}
    for data in ("5y", "rot"):
        df, same, orig = mtr.base(data)
        origs[data] = orig
        print(f"  {data}: реплика = оригинал: {same}; сделок: {len(df)}")
        if not same:
            print("СТОП: реплика не совпала")
            return 1
        tabs.append(df)
    if not rj.check_parity(origs):
        print("СТОП: паритет базы")
        return 1
    trades = pd.concat(tabs, ignore_index=True)

    print("== Эталон E1m против judge_1m (режим паритета)")
    cache: dict = {}
    bad: list = []
    ref_out: dict = {}
    mine_out: dict = {}
    prod_out: dict = {}
    for row in trades.itertuples(index=False):
        if row.symbol not in cache:
            cache[row.symbol] = m1.load(row.symbol)
        b = cache[row.symbol]
        ev, rg, ex = fj.judge(row.entry, row.stop, row.side, row.fill_ms, b, bar_ms=MIN_MS,
                              mode="path", immediate=True, order="color", act_r=ACT,
                              dist_r=DIST, max_hold_h=HOLD)
        ref_out[row.key] = (ev, rg, ex)
        me = _mine(row, b, prod=False)
        mine_out[row.key] = me[:3]
        if me[:3] != (ev, rg, ex):
            bad.append((row.key, me[:3], (ev, rg, ex)))
        prod_out[row.key] = _mine(row, b, prod=True)
    print(f"  сделок {len(trades)}, расхождений {len(bad)}")
    for x in bad[:5]:
        print("   ", x)

    cov = mtr.coverage_ok(trades)
    trades["cov1m"] = trades["key"].map(cov)
    ok = not bad
    print("\n== Net R/сделку по половинам (парно, покрытие 1m ≥ 95%)")
    print(f"  {'половина':8s} {'n':>5s} {'gross':>7s} {'net':>7s} {'эталон':>7s} "
          f"{'| прод net':>10s} {'иначе':>6s}")
    for half in mtr.HALVES:
        part = trades[(trades["half"] == half) & (trades["cov1m"] >= 0.95)]
        rows = [(r, mine_out[r.key]) for r in part.itertuples(index=False)
                if mine_out[r.key][1] is not None and ref_out[r.key][1] is not None]
        gross = float(np.mean([o[1] for _r, o in rows]))
        net = float(np.mean([o[1] - mtr.cost(r, o[2]) for r, o in rows]))
        prow = [(r, prod_out[r.key]) for r in part.itertuples(index=False)
                if prod_out[r.key][1] is not None]
        pnet = float(np.mean([o[1] - mtr.cost(r, o[2]) for r, o in prow]))
        differ = sum(prod_out[r.key][:3] != mine_out[r.key] for r in part.itertuples(index=False))
        good = len(rows) == REF_N[half] and round(net, 3) == REF_NET[half]
        ok &= good
        print(f"  {half:8s} {len(rows):5d} {gross:+7.3f} {net:+7.3f} {REF_NET[half]:+7.3f} "
              f"{pnet:+10.3f} {differ:6d}  {'OK' if good else 'РАСХОЖДЕНИЕ'}")
    print("\nПАРИТЕТ: " + ("ПРОЙДЕН" if ok else "НЕ ПРОЙДЕН"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
