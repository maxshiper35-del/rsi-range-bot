"""Паритет минутного судьи прода (app.brain.judge_1m) с эталоном замера реализма трейла.

  DATA_ROOT=/путь/к/data nice -n 15 .venv/bin/python -u -m scripts.judge_parity [--procs 4]

1. База замера 2026-10-30-trail-realism (`measure_trail_realism.base`): 4 635 сделок прода
   «только 1h» на двух наборах, паритет n/ΣR по половинам.
2. На тех же минутках кэша (`data/cache_tf/m1`, без докачки) эталон E1m
   (`research_fine_judge.judge(mode="path", immediate=True, order="color")`) против
   `judge_1m.judge_path` в режиме паритета (окно входа — час входа журнала, gap_open=False):
   исход, gross R и минута выхода должны совпасть у КАЖДОЙ сделки.
3. Net R/сделку по половинам на парном множестве замера (разрешены, покрытие 1m ≥ 95%;
   в замере — все 4 635), издержки и статистика — функциями замера, против документа:
   W1 −0.065, W2 −0.057, rot H1 −0.180, rot H2 −0.004 при n 1026 / 877 / 1292 / 1440.
4. Справочно — прод-режим (вход ищется с решающего бара, gap_open=True): net по половинам
   и сколько сделок он разрешил иначе.

Обязателен перед вливанием правок судьи. Долгий (минуты), сеть не трогает.
"""
from __future__ import annotations

import argparse
import os
import sys
from multiprocessing import Pool
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
    return _EV.get(v.event, v.event), v.r, v.exit_ms


def _judge_chunk(rows: pd.DataFrame) -> dict:
    """{key: (эталон E1m, мой — режим паритета, мой — прод)}, каждый (исход, gross R, выход)."""
    out: dict = {}
    cache: dict = {}
    for row in rows.itertuples(index=False):
        if row.symbol not in cache:
            cache[row.symbol] = m1.load(row.symbol)
        b = cache[row.symbol]
        ref = fj.judge(row.entry, row.stop, row.side, row.fill_ms, b, bar_ms=MIN_MS,
                       mode="path", immediate=True, order="color", act_r=ACT, dist_r=DIST,
                       max_hold_h=HOLD)
        out[row.key] = (ref, _mine(row, b, prod=False), _mine(row, b, prod=True))
    return out


def _chunks(trades: pd.DataFrame, k: int) -> list:
    """Монеты по k группам поровну по числу сделок: минутки монеты грузит один процесс."""
    groups: list = [[] for _ in range(k)]
    load = [0] * k
    for sym, n in trades["symbol"].value_counts().items():
        i = load.index(min(load))
        groups[i].append(sym)
        load[i] += int(n)
    return [trades[trades["symbol"].isin(g)] for g in groups if g]


def _stats(part: pd.DataFrame, res: dict, idx: int) -> dict:
    return mtr.stats([(o[2], o[1], o[1] - mtr.cost(r, o[2]), o[0])
                      for r in part.itertuples(index=False)
                      for o in [res[r.key][idx]] if o[1] is not None])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--procs", type=int, default=4)
    args = ap.parse_args()

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

    print(f"== Эталон E1m против judge_1m ({args.procs} проц.)")
    with Pool(args.procs) as pool:
        parts = pool.map(_judge_chunk, _chunks(trades, args.procs))
    res: dict = {}
    for p in parts:
        res.update(p)
    bad = [(k, v[1], v[0]) for k, v in res.items() if v[1] != v[0]]
    unres = sum(v[0][1] is None for v in res.values())
    print(f"  сделок {len(res)}, без исхода у эталона {unres}, расхождений {len(bad)}")
    for x in bad[:5]:
        print("   ", x)

    trades["cov1m"] = trades["key"].map(mtr.coverage_ok(trades))
    ok = not bad
    print("\n== Net R/сделку по половинам (парно: исход есть, покрытие 1m ≥ 95%)")
    print(f"  {'половина':8s} {'n':>5s} {'эталон':>7s} {'док.':>7s} {'judge_1m':>8s} "
          f"{'| прод':>7s} {'n прод':>6s} {'иначе':>6s}")
    for half in mtr.HALVES:
        part = trades[(trades["half"] == half) & (trades["cov1m"] >= 0.95)]
        part = part[part["key"].map(lambda k: res[k][0][1] is not None)]
        s_ref, s_me, s_prod = (_stats(part, res, i) for i in (0, 1, 2))
        differ = sum(res[k][2] != res[k][1] for k in part["key"])
        good = s_me["n"] == REF_N[half] and round(s_me["net"], 3) == REF_NET[half]
        ok &= good
        print(f"  {half:8s} {s_me['n']:5d} {s_ref['net']:+7.3f} {REF_NET[half]:+7.3f} "
              f"{s_me['net']:+8.3f} {s_prod.get('net', float('nan')):+7.3f} "
              f"{s_prod['n']:6d} {differ:6d}  {'OK' if good else 'РАСХОЖДЕНИЕ'}")
    print("\nПАРИТЕТ: " + ("ПРОЙДЕН" if ok else "НЕ ПРОЙДЕН"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
