"""Замер №26: правило №25 (θ 100%, H 7 суток) без изменений на 87 монетах, которых не было в №25
(docs/measurements/2026-11-14-funding-arb-new.md).

  export DATA_ROOT="/Users/mac/bot ctypto/data"
  .venv/bin/python -m scripts.measure_funding_arb_new fetch-hl
  .venv/bin/python -m scripts.measure_funding_arb_new run

fetch-hl — почасовой фандинг HL с первой записи по END для монет с меткой *.late из №25 (листинг на
           HL позже 2024-07-01); кэш funding_arb/hl_new/, с продолжением с места остановки.
run      — правило №25 без изменений, участки 2025 и 2026 по дате выхода (2024-07…12 описательно),
           пять пунктов критерия №25; описательно — базис, №25 и №26 вместе, первые 90 дней.
"""
from __future__ import annotations

import argparse
import math

import pandas as pd

from scripts import measure_funding_arb as m

THETA, HOLD = 1.00, 7
SUB = "hl_new"
PERIODS = (("2024-07…12", "2024-07-01", "2024-12-31"), ("2025", "2025-01-01", "2025-12-31"),
           ("2026", "2026-01-01", "2026-10-31"))
MAIN = ("2025", "2026")


def new_coins() -> list[str]:
    return sorted(p.stem for p in (m.root() / "hl").glob("*.late"))


def cmd_fetch_hl(_a) -> None:
    coins = new_coins()
    print(f"монет без №25 (первая запись на HL позже {m.START_MAX.date()}): {len(coins)}",
          flush=True)
    stats: dict[str, int] = {}
    for i, c in enumerate(coins, 1):
        res = m.fetch_coin(c, sub=SUB, start_max=None)
        stats[res] = stats.get(res, 0) + 1
        if i % 5 == 0 or i == len(coins):
            print(f"  {i}/{len(coins)} {stats} {pd.Timestamp.now(tz='UTC'):%H:%M}", flush=True)


def period_of(t: pd.Timestamp) -> str | None:
    s = t.strftime("%Y-%m-%d")
    return next((n for n, a, b in PERIODS if a <= s <= b), None)


def top_share(v: pd.DataFrame) -> tuple[float, str]:
    by_coin = v.groupby("coin")["net"].sum()
    if not len(by_coin) or by_coin.sum() <= 0:
        return math.nan, "-"
    return by_coin.max() / by_coin.sum(), str(by_coin.idxmax())


def cmd_run(_a) -> None:
    rows, first = [], {}
    coins = sorted(p.stem for p in (m.root() / SUB).glob("*.csv"))
    for c in coins:
        d = m.hourly_diff(c, SUB)
        if d is None:
            continue
        first[c] = m.hourly_hl(c, SUB).index[0]
        for tr in m.trades_for(c, d, THETA, HOLD):
            tr["plac"] = m.placebo_for(d, tr["side"], tr["t_in"], HOLD)
            tr["period"] = period_of(tr["t_out"])
            tr["age_d"] = (tr["t_in"] - first[c]).days
            if tr["period"]:
                rows.append(tr)
    t = pd.DataFrame(rows)
    t.to_pickle(m.root() / "trades_new.pkl")
    print(f"монет со скачанным HL: {len(coins)}, с данными обеих бирж: {len(first)}, "
          f"со сделками: {t['coin'].nunique() if len(t) else 0}")
    print(f"правило: θ {THETA:.0%}, H {HOLD} сут, издержки {m.COST_RT:.2%} за круг")
    for per, _, _ in PERIODS:
        print(f"  {per:10s}: {m.summary(t[t.period == per]) if len(t) else 'n 0'}")
    v = t[t.period.isin(MAIN)].copy() if len(t) else t
    if len(v) < 30:
        print(f"\nИТОГ: мало сделок (n {len(v)} < 30 за 2025+2026), вывода нет")
        return
    lo, hi = m.block_ci(v, "net")
    wo = v.drop(v["net"].nlargest(5).index)["net"].mean()
    share, best = top_share(v)
    a_, b_ = v[v.period == "2025"], v[v.period == "2026"]
    print(f"\n  2025+2026: net {v['net'].mean():+.3%} [90% ДИ блоками {lo:+.3%}; {hi:+.3%}], "
          f"без 5 лучших {wo:+.3%}, доля лучшей монеты {share:.0%} ({best})")
    crit = {
        "1 net > 0 и в 2025, и в 2026": a_["net"].mean() > 0 and b_["net"].mean() > 0,
        "2 нижняя граница ДИ net 2025+2026 > 0": lo > 0,
        "3 без 5 лучших сделок net > 0": wo > 0,
        "4 лучшая монета < 30% суммы": share < 0.30,
        "5 сверх плацебо > 0 и в 2025, и в 2026": (a_["net"] - a_["plac"]).mean() > 0
        and (b_["net"] - b_["plac"]).mean() > 0,
    }
    for k, ok in crit.items():
        print(f"  {'✓' if ok else '✗'} {k}")
    if all(crit.values()):
        verdict = "правило переносится на новые монеты"
    elif v["net"].mean() > 0:
        verdict = "не кандидат (net > 0, но не все пункты)"
    else:
        verdict = "направление закрыто (net за 2025+2026 ≤ 0)"
    print(f"\nИТОГ: {verdict}")

    print("\n== Описательно, не критерий")
    by = v.groupby("coin")["net"].sum().sort_values(ascending=False)
    top5 = ", ".join(f"{c} {x:+.1%}" for c, x in by.head(5).items())
    print(f"  лучшие монеты (сумма net): {top5}")
    wb = v[v.coin != by.index[0]]
    print(f"  без лучшей монеты: n {len(wb)}, net {wb['net'].mean():+.3%}, "
          f"с базисом {(wb['net'] + wb['basis'].fillna(0)).mean():+.3%}")
    young = v[v["age_d"] < 90]
    part = f", {young['net'].sum() / v['net'].sum():.0%} суммы net" if v["net"].sum() > 0 else ""
    print(f"  первые 90 дней после первой записи на HL: {len(young)} из {len(v)} сделок{part}")
    old = pd.read_pickle(m.root() / "trades.pkl")
    old = old[(old.theta == THETA) & (old.H == HOLD)
              & old.period.isin(["проверка A", "проверка B", "заморозка"])]
    both = pd.concat([old.assign(src="№25"), v.assign(src="№26")], ignore_index=True)
    s2, b2 = top_share(both)
    bb = both.groupby("coin")["net"].sum().idxmax()
    rest = both[both.coin != bb]
    print(f"  №25 и №26 вместе, 2025–26: n {len(both)}, net {both['net'].mean():+.3%}, "
          f"доля лучшей монеты {s2:.0%} ({b2}), без неё net {rest['net'].mean():+.3%}, "
          f"монет {both['coin'].nunique()}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch-hl")
    sub.add_parser("run")
    a = ap.parse_args()
    {"fetch-hl": cmd_fetch_hl, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    main()
