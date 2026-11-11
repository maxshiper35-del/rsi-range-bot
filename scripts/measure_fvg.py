"""Замер №22: реакция BTC от месячного (и недельного) FVG против случайного уровня
(docs/measurements/2026-11-11-monthly-fvg.md).

  DATA_ROOT="/Users/mac/bot ctypto/data" .venv/bin/python -m scripts.measure_fvg fetch
  DATA_ROOT="/Users/mac/bot ctypto/data" .venv/bin/python -m scripts.measure_fvg run

fetch — дневные свечи BTC-USD с публичного API Coinbase Exchange (без ключей) в кэш.
run   — FVG на месяцах и неделях, касание, реакция R% до срыва, плацебо с той же шириной и
        расстоянием от цены на всех других датах, сделка «вход на касании, стоп — срыв, цель R».
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

API = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
START, END = "2015-07-20", "2026-11-11"
EVAL_CUT = pd.Timestamp("2026-08-12", tz="UTC")    # касания позже — без полного горизонта
COST_RT = 0.0015
TFS = {"месяц": ("MS", (0.05, 0.10), 90), "неделя": ("W-MON", (0.03, 0.05), 30)}


def root() -> Path:
    p = Path(os.environ.get("DATA_ROOT", "data")) / "cache_research" / "fvg"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cmd_fetch(_a) -> None:
    rows, t0, end = [], pd.Timestamp(START, tz="UTC"), pd.Timestamp(END, tz="UTC")
    while t0 < end:
        t1 = min(t0 + pd.Timedelta(days=299), end)
        url = (f"{API}?granularity=86400&start={t0.strftime('%Y-%m-%dT%H:%M:%SZ')}"
               f"&end={t1.strftime('%Y-%m-%dT%H:%M:%SZ')}")
        req = urllib.request.Request(url, headers={"User-Agent": "research-fetch/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            rows += json.load(r)
        t0 = t1 + pd.Timedelta(days=1)
        time.sleep(0.4)
    d = pd.DataFrame(rows, columns=["t", "low", "high", "open", "close", "volume"])
    d = d.drop_duplicates("t").sort_values("t")
    d["date"] = pd.to_datetime(d["t"], unit="s", utc=True)
    d = d[d["date"] < pd.Timestamp(END, tz="UTC")]
    d.to_csv(root() / "btc_usd_1d.csv", index=False)
    print(f"дней: {len(d)}, с {d['date'].min().date()} по {d['date'].max().date()}")


def load_daily() -> pd.DataFrame:
    d = pd.read_csv(root() / "btc_usd_1d.csv")
    d["date"] = pd.to_datetime(d["date"], utc=True)
    return d.set_index("date")[["open", "high", "low", "close"]].astype(float)


def periods(d: pd.DataFrame, rule: str) -> pd.DataFrame:
    kw = dict(label="left", closed="left") if rule.startswith("W") else {}
    p = d.resample(rule, **kw).agg({"open": "first", "high": "max", "low": "min",
                                    "close": "last"}).dropna()
    nxt = p.index[1:].append(pd.DatetimeIndex([p.index[-1] + (p.index[-1] - p.index[-2])]))
    p["end"] = nxt                                      # начало следующего периода
    last_day = d.index.max() + pd.Timedelta(days=1)
    return p[p["end"] <= last_day]                      # только закрытые периоды


def find_fvg(p: pd.DataFrame) -> list[dict]:
    out = []
    for i in range(1, len(p) - 1):
        a, c = p.iloc[i - 1], p.iloc[i + 1]
        if c["low"] > a["high"]:
            out.append(dict(side=1, bottom=a["high"], top=c["low"], formed=p["end"].iloc[i + 1],
                            close=c["close"], b=p.index[i]))
        elif c["high"] < a["low"]:
            out.append(dict(side=-1, bottom=c["high"], top=a["low"], formed=p["end"].iloc[i + 1],
                            close=c["close"], b=p.index[i]))
    return out


def outcome(d_low, d_high, d_close, start: int, side: int, bottom: float, top: float,
            r: float, h: int) -> tuple | None:
    """(день касания, исход, доходность сделки) или None, если касания нет."""
    if start >= len(d_low):
        return None
    if side > 0:
        hit = np.flatnonzero(d_low[start:] <= top)
    else:
        hit = np.flatnonzero(d_high[start:] >= bottom)
    if not len(hit):
        return None
    k = start + int(hit[0])
    entry = top if side > 0 else bottom
    target = entry * (1 + r) if side > 0 else entry * (1 - r)
    for j in range(k, min(k + h + 1, len(d_close))):
        broke = d_close[j] < bottom if side > 0 else d_close[j] > top
        if broke:
            return k, "срыв", side * (d_close[j] / entry - 1) - COST_RT
        if j > k:                                     # реакция — только после дня касания
            reached = d_high[j] >= target if side > 0 else d_low[j] <= target
            if reached:
                return k, "реакция", r - COST_RT
    last = min(k + h, len(d_close) - 1)
    return k, "ничего", side * (d_close[last] / entry - 1) - COST_RT


def wilson(k: int, n: int, z: float = 1.645) -> tuple:
    if n == 0:
        return math.nan, math.nan
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    hw = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return c - hw, c + hw


def cmd_run(_a) -> None:
    d = load_daily()
    lo, hi, cl = d["low"].to_numpy(), d["high"].to_numpy(), d["close"].to_numpy()
    dates = d.index
    print(f"дней: {len(d)}, с {dates.min().date()} по {dates.max().date()}")
    for tf, (rule, rs, h) in TFS.items():
        p = periods(d, rule)
        zones = find_fvg(p)
        ends = p["end"].to_numpy()
        print(f"\n== {tf}: свечей {len(p)}, FVG {len(zones)} "
              f"(бычьих {sum(z['side'] > 0 for z in zones)}, медвежьих "
              f"{sum(z['side'] < 0 for z in zones)})")
        for r in rs:
            real, plac, trades, ptrades, recent = [], [], [], [], []
            for z in zones:
                s = int(dates.searchsorted(z["formed"]))
                res = outcome(lo, hi, cl, s, z["side"], z["bottom"], z["top"], r, h)
                if res is None:
                    continue
                k, what, ret = res
                if dates[k] > EVAL_CUT:
                    recent.append((z, dates[k], what))
                    continue
                real.append(what == "реакция")
                trades.append(ret)
                w = (z["top"] - z["bottom"]) / z["close"]
                if z["side"] > 0:
                    dist = (z["close"] - z["top"]) / z["close"]
                else:
                    dist = (z["bottom"] - z["close"]) / z["close"]
                pr, pt = [], []
                for e in ends:
                    if pd.Timestamp(e) == z["formed"]:
                        continue
                    s2 = int(dates.searchsorted(pd.Timestamp(e)))
                    if s2 == 0 or s2 >= len(cl):
                        continue
                    c0 = cl[s2 - 1]
                    if z["side"] > 0:
                        top2 = c0 * (1 - dist)
                        bot2 = top2 - w * c0
                    else:
                        bot2 = c0 * (1 + dist)
                        top2 = bot2 + w * c0
                    res2 = outcome(lo, hi, cl, s2, z["side"], bot2, top2, r, h)
                    if res2 is None or dates[res2[0]] > EVAL_CUT:
                        continue
                    pr.append(res2[1] == "реакция")
                    pt.append(res2[2])
                plac.append(np.mean(pr) if pr else np.nan)
                ptrades.append(np.mean(pt) if pt else np.nan)
            real_a, plac_a = np.array(real, float), np.array(plac, float)
            n, k = len(real_a), int(real_a.sum())
            lo_w, hi_w = wilson(k, n)
            ok = ~np.isnan(plac_a)
            diff = real_a[ok] - plac_a[ok]
            rng = np.random.default_rng(0)
            boots = [diff[rng.integers(0, len(diff), len(diff))].mean() for _ in range(2000)] \
                if len(diff) else [math.nan]
            share = k / n if n else math.nan
            print(f"  R {r:.0%}, горизонт {h} дн: касаний {n}, реакций {k} = {share:.0%} "
                  f"[ДИ {lo_w:.0%}; {hi_w:.0%}] | случайный уровень {np.nanmean(plac_a):.0%} | "
                  f"разница {diff.mean():+.0%} [ДИ {np.percentile(boots, 5):+.0%}; "
                  f"{np.percentile(boots, 95):+.0%}]")
            print(f"    сделка (вход на касании, стоп — срыв, цель R, издержки): FVG "
                  f"{np.mean(trades):+.2%} на сделку, случайный уровень {np.nanmean(ptrades):+.2%}")
            for z, td, what in recent:
                print(f"    свежая зона {'бычья' if z['side'] > 0 else 'медвежья'} "
                      f"{z['bottom']:,.0f}–{z['top']:,.0f} (свеча B {z['b'].date()}), касание "
                      f"{td.date()}: пока «{what}»")
        open_z = []
        for z in zones:
            s = int(dates.searchsorted(z["formed"]))
            if z["side"] > 0 and (s >= len(lo) or lo[s:].min() > z["top"]):
                open_z.append(z)
            if z["side"] < 0 and (s >= len(hi) or hi[s:].max() < z["bottom"]):
                open_z.append(z)
        for z in open_z[-4:]:
            print(f"  ещё не тронута: {'бычья' if z['side'] > 0 else 'медвежья'} "
                  f"{z['bottom']:,.0f}–{z['top']:,.0f} (свеча B {z['b'].date()})")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    sub.add_parser("run")
    a = ap.parse_args()
    {"fetch": cmd_fetch, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    main()
