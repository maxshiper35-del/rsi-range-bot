"""Замер №21, шаг 0: «страх на опционах» (DVOL Deribit) и ход BTC/ETH
(docs/measurements/2026-11-11-dvol-fear.md).

  DATA_ROOT="/Users/mac/bot ctypto/data" .venv/bin/python -m scripts.measure_dvol fetch
  DATA_ROOT="/Users/mac/bot ctypto/data" .venv/bin/python -m scripts.measure_dvol run

fetch — дневной DVOL BTC и ETH (публичный API Deribit) и дневные свечи Coinbase BTC-USD
        и ETH-USD.
run   — признаки fear / vrp / dvol7 на 00:00 UTC, ранговая связь с простой доходностью
        через 1/3/7 дней по неперекрывающимся точкам, сделка «покупать страх» с издержками
        и фандингом, вердикт.
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

DERIBIT = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
COINBASE = "https://api.exchange.coinbase.com/products/{p}/candles"
START, END = "2021-01-01", "2026-11-11"
ASSETS = {"BTC": "BTC-USD", "ETH": "ETH-USD"}
PARTS = {"учебный": ("2021-04-01", "2023-12-31"), "проверочный": ("2024-01-01", "2026-07-31"),
         "замороженный": ("2026-08-01", "2026-10-30")}
HALVES = (("2024H1", "2024-01-01", "2024-06-30"), ("2024H2", "2024-07-01", "2024-12-31"),
          ("2025H1", "2025-01-01", "2025-06-30"), ("2025H2", "2025-07-01", "2025-12-31"),
          ("2026H1", "2026-01-01", "2026-07-31"))
HS = (1, 3, 7)
HYP = {"fear": 1, "vrp": 1}
COST_RT, FEAR_IN, FUND_8H = 0.0015, 1.5, 0.0001
UA = {"User-Agent": "research-fetch/1.0"}


def root() -> Path:
    p = Path(os.environ.get("DATA_ROOT", "data")) / "cache_research" / "dvol"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_json(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return json.load(r)


def cmd_fetch(_a) -> None:
    t_end = int(pd.Timestamp(END, tz="UTC").value // 1_000_000)
    for cur in ASSETS:
        rows, t0 = [], int(pd.Timestamp(START, tz="UTC").value // 1_000_000)
        while t0 < t_end:
            t1 = min(t0 + 800 * 86_400_000, t_end)
            res = get_json(f"{DERIBIT}?currency={cur}&start_timestamp={t0}&end_timestamp={t1}"
                           f"&resolution=1D")["result"]
            rows += res["data"]
            t0 = t1
            time.sleep(0.5)
        d = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close"]).drop_duplicates("t")
        d["date"] = pd.to_datetime(d["t"], unit="ms", utc=True)
        d.sort_values("t").to_csv(root() / f"dvol_{cur}.csv", index=False)
        print(f"DVOL {cur}: {len(d)} дней, {d['date'].min().date()} … {d['date'].max().date()}")
    for cur, prod in ASSETS.items():
        rows, t0, end = [], pd.Timestamp(START, tz="UTC"), pd.Timestamp(END, tz="UTC")
        while t0 < end:
            t1 = min(t0 + pd.Timedelta(days=299), end)
            rows += get_json(f"{COINBASE.format(p=prod)}?granularity=86400"
                             f"&start={t0.strftime('%Y-%m-%dT%H:%M:%SZ')}"
                             f"&end={t1.strftime('%Y-%m-%dT%H:%M:%SZ')}")
            t0 = t1 + pd.Timedelta(days=1)
            time.sleep(0.4)
        d = pd.DataFrame(rows, columns=["t", "low", "high", "open", "close", "volume"])
        d = d.drop_duplicates("t").sort_values("t")
        d["date"] = pd.to_datetime(d["t"], unit="s", utc=True)
        d = d[d["date"] < pd.Timestamp(END, tz="UTC")]
        d.to_csv(root() / f"px_{cur}.csv", index=False)
        print(f"цены {cur}: {len(d)} дней, {d['date'].min().date()} … {d['date'].max().date()}")


def load_px(cur: str) -> pd.Series:
    d = pd.read_csv(root() / f"px_{cur}.csv")
    d["date"] = pd.to_datetime(d["date"], utc=True)
    d = d.set_index("date")[["open", "high", "low", "close"]].astype(float)
    return d["close"]                                   # битые тики трогают только low/high


def load_funding(cur: str) -> pd.Series:
    base = Path(os.environ.get("DATA_ROOT", "data")) / "cache_research" / "binance_metrics"
    parts = []
    for f in sorted((base / "funding" / f"{cur}USDT").glob("*.csv")):
        d = pd.read_csv(f)
        if "calc_time" not in d:
            d = pd.read_csv(f, header=None).iloc[:, [0, -1]]
            d.columns = ["calc_time", "last_funding_rate"]
        parts.append(d[["calc_time", "last_funding_rate"]])
    if not parts:
        return pd.Series(dtype=float)
    f = pd.concat(parts, ignore_index=True)
    f["ts"] = pd.to_datetime(f["calc_time"].astype("int64"), unit="ms", utc=True)
    return f.drop_duplicates("ts").set_index("ts").sort_index()["last_funding_rate"].astype(float)


def frame(cur: str) -> pd.DataFrame:
    """Строка — момент t (00:00 UTC): значения, известные в t, и будущие доходности."""
    px = load_px(cur)
    dv = pd.read_csv(root() / f"dvol_{cur}.csv")
    dv["date"] = pd.to_datetime(dv["date"], utc=True)
    dv = dv.set_index("date")["close"].astype(float)
    day = pd.Timedelta(days=1)
    idx = pd.date_range(max(px.index.min(), dv.index.min()) + day, px.index.max(), freq="1D")
    f = pd.DataFrame(index=idx)
    f["price"] = px.reindex(idx - day).to_numpy()        # закрытие дня t−1 = цена в t
    f["dvol"] = dv.reindex(idx - day).to_numpy()
    prev = f["dvol"].shift(1)
    f["fear"] = (f["dvol"] - prev.rolling(90, min_periods=80).mean()) / \
        prev.rolling(90, min_periods=80).std()
    r1 = np.log(f["price"]).diff()
    f["rv30"] = r1.rolling(30, min_periods=28).std() * math.sqrt(365) * 100
    f["vrp"] = f["dvol"] - f["rv30"]
    f["dvol7"] = np.log(f["dvol"] / f["dvol"].shift(7))
    fu = load_funding(cur)
    cs = fu.cumsum() if len(fu) else None
    for h in HS:
        f[f"R{h}"] = f["price"].shift(-h) / f["price"] - 1
        if cs is not None:
            a = cs.reindex(idx, method="ffill").fillna(0.0).to_numpy()
            b = cs.reindex(idx + pd.Timedelta(days=h), method="ffill").fillna(0.0).to_numpy()
            f[f"F{h}"] = b - a
            f.loc[f.index < fu.index.min(), f"F{h}"] = FUND_8H * 3 * h
        else:
            f[f"F{h}"] = FUND_8H * 3 * h
    f["asset"] = cur
    return f


def part(f: pd.DataFrame, name: str) -> pd.DataFrame:
    lo, hi = PARTS[name]
    return f[(f.index >= pd.Timestamp(lo, tz="UTC")) & (f.index <= pd.Timestamp(hi, tz="UTC"))]


def rho_t(f: pd.DataFrame, col: str, h: int) -> tuple:
    g = f.iloc[::h][[col, f"R{h}"]].dropna()
    n = len(g)
    if n < 10:
        return math.nan, math.nan, n
    r = g[col].rank().corr(g[f"R{h}"].rank())
    return r, r * math.sqrt(n - 2) / math.sqrt(max(1e-12, 1 - r * r)), n


def trades(f: pd.DataFrame, h: int) -> pd.DataFrame:
    out, i, idx = [], 0, f.index
    fear, ret, fund = f["fear"].to_numpy(), f[f"R{h}"].to_numpy(), f[f"F{h}"].to_numpy()
    while i < len(f):
        if not math.isnan(fear[i]) and fear[i] >= FEAR_IN and not math.isnan(ret[i]):
            out.append(dict(t=idx[i], asset=f["asset"].iloc[i], net=ret[i] - COST_RT - fund[i]))
            i += h
        else:
            i += 1
    return pd.DataFrame(out)


def cmd_run(_a) -> None:
    frames = {cur: frame(cur) for cur in ASSETS}
    for cur, f in frames.items():
        print(f"{cur}: дней {len(f)}, с {f.index.min().date()} по {f.index.max().date()}; "
              f"fear есть у {int(f['fear'].notna().sum())}")
    chosen = {}
    for col, sign in list(HYP.items()) + [("dvol7", 0)]:
        print(f"\n== {col} (знак гипотезы {'+' if sign > 0 else 'нет, описательно'})")
        best = None
        for h in HS:
            line = []
            st = []
            for cur, f in frames.items():
                for name in ("учебный", "проверочный"):
                    r, t, n = rho_t(part(f, name), col, h)
                    line.append(f"{cur} {name[:4]} ρ {r:+.3f} t {t:+.2f} n {n}")
                    if name == "учебный":
                        st.append(sign * t if sign else abs(t))
            print(f"  h={h}: " + " | ".join(line))
            score = float(np.mean(st))
            if best is None or score > best[0]:
                best = (score, h)
        if sign:
            chosen[col] = best[1]
            print(f"  выбран на учебном h = {best[1]}")
    verdicts = {}
    for col, sign in HYP.items():
        h = chosen[col]
        tr_rt = [rho_t(part(f, "учебный"), col, h) for f in frames.values()]
        va_rt = [rho_t(part(f, "проверочный"), col, h) for f in frames.values()]
        c1 = all(np.sign(r) == sign and abs(t) >= 2.5 for r, t, _ in tr_rt)
        c2 = all(np.sign(r) == sign and abs(t) >= 2 for r, t, _ in va_rt)
        checks = [c1, c2]
        if col == "fear":
            for name in ("учебный", "проверочный"):
                tr = pd.concat([trades(part(f, name), h) for f in frames.values()],
                               ignore_index=True)
                base = {}
                for cur, f in frames.items():
                    pf = part(f, name)
                    base[cur] = float((pf[f"R{h}"] - COST_RT - pf[f"F{h}"]).mean())
                if len(tr):
                    diff = (tr["net"] - tr["asset"].map(base)).to_numpy()
                    rng = np.random.default_rng(0)
                    bs = [diff[rng.integers(0, len(diff), len(diff))].mean() for _ in range(2000)]
                    lo, hi = np.percentile(bs, 5), np.percentile(bs, 95)
                else:
                    diff, lo, hi = np.array([]), math.nan, math.nan
                mean_tr = tr["net"].mean() if len(tr) else math.nan
                mean_df = diff.mean() if len(diff) else math.nan
                print(f"\n  сделка «покупать страх» (fear ≥ {FEAR_IN}, h={h}), {name}: "
                      f"сделок {len(tr)}, результат {mean_tr:+.2%} на сделку, случайный вход "
                      f"BTC {base['BTC']:+.2%} / ETH {base['ETH']:+.2%}, разница "
                      f"{mean_df:+.2%} [ДИ {lo:+.2%}; {hi:+.2%}]")
                if name == "проверочный":
                    halves = []
                    for _hn, a, b in HALVES:
                        m = ((tr["t"] >= pd.Timestamp(a, tz="UTC"))
                             & (tr["t"] <= pd.Timestamp(b, tz="UTC")))
                        halves.append(tr.loc[m, "net"].mean() if m.any() else math.nan)
                    print("  полугодия: " + ", ".join(
                        f"{hn} {x:+.2%}" for (hn, _, _), x in zip(HALVES, halves)))
                    checks += [len(diff) > 0 and diff.mean() > 0 and lo > 0,
                               sum(x > 0 for x in halves if not math.isnan(x)) >= 3]
        verdicts[col] = all(checks)
        print(f"\n  {col}: пункты " + " ".join("да" if c else "НЕТ" for c in checks)
              + f" → {'ЕСТЬ ЭФФЕКТ' if all(checks) else 'нет эффекта'}")
    passed = [k for k, v in verdicts.items() if v]
    print(f"\n== ИТОГ: {'ЕСТЬ ЭФФЕКТ — ' + ', '.join(passed) if passed else 'НЕТ ЭФФЕКТА'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    sub.add_parser("run")
    a = ap.parse_args()
    {"fetch": cmd_fetch, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    main()
