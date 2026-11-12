"""Замер №24: трендовая система статьи Zarattini–Pagani–Barbon 2025 («Catching Crypto Trends»)
только на BTC и ETH, против простого держания (docs/measurements/2026-11-12-trend-btc-eth.md).

  DATA_ROOT="/Users/mac/bot ctypto/data" .venv/bin/python -m scripts.measure_trend_btceth fetch
  DATA_ROOT="/Users/mac/bot ctypto/data" .venv/bin/python -m scripts.measure_trend_btceth run

fetch — дневные свечи Coinbase BTC-USD и ETH-USD с начала истории (публичный API, без ключей).
run   — ансамбль 9 окон статьи без подбора: лонг на пробое максимума закрытий за L дней,
        трейл по середине канала. Доли капитала: BTC и ETH по 1/2, внутри — по 1/9 на окно.
        Сравнение с держанием (перп с фандингом и спот) и с держанием той же средней доли.
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

API = "https://api.exchange.coinbase.com/products/{p}/candles"
STARTS = {"BTC": "2015-07-20", "ETH": "2016-05-18"}
END = "2026-11-11"
LOOKBACKS = (5, 10, 20, 30, 60, 90, 150, 250, 360)
FEE, SLIP = 0.00055, 0.0002
FUND_DAY_DEFAULT = 0.0003           # 0.01% за 8 ч — до истории фандинга (до 2021)
PERIODS = (("S1 2017-06…2020", "2017-06-01", "2020-12-31"),
           ("S2 2021–2022", "2021-01-01", "2022-12-31"),
           ("S3 2023…2025-03", "2023-01-01", "2025-03-31"),
           ("S4 после статьи", "2025-04-01", "2026-07-31"))
FULL = ("вся история", "2017-06-01", "2026-07-31")
FROZEN = ("заморозка", "2026-08-01", "2026-10-30")


def root() -> Path:
    p = Path(os.environ.get("DATA_ROOT", "data")) / "cache_research" / "trend24"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cmd_fetch(_a) -> None:
    for coin, start in STARTS.items():
        rows, t0, end = [], pd.Timestamp(start, tz="UTC"), pd.Timestamp(END, tz="UTC")
        while t0 < end:
            t1 = min(t0 + pd.Timedelta(days=299), end)
            url = (API.format(p=f"{coin}-USD") + f"?granularity=86400&start="
                   f"{t0.strftime('%Y-%m-%dT%H:%M:%SZ')}&end={t1.strftime('%Y-%m-%dT%H:%M:%SZ')}")
            req = urllib.request.Request(url, headers={"User-Agent": "research-fetch/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                rows += json.load(r)
            t0 = t1 + pd.Timedelta(days=1)
            time.sleep(0.4)
        d = pd.DataFrame(rows, columns=["t", "low", "high", "open", "close", "volume"])
        d = d.drop_duplicates("t").sort_values("t")
        d["date"] = pd.to_datetime(d["t"], unit="s", utc=True)
        d = d[d["date"] < pd.Timestamp(END, tz="UTC")]
        d.to_csv(root() / f"{coin}_usd_1d.csv", index=False)
        print(f"{coin}: дней {len(d)}, {d['date'].min().date()} … {d['date'].max().date()}")


def load(coin: str) -> pd.DataFrame:
    """Дневные свечи; битые тики (low < 0.5 тела, high > 2 тел) — до тела свечи, как в №22.
    Пропущенные дни — вперёд последним закрытием (решения по закрытию, исполнение по открытию)."""
    d = pd.read_csv(root() / f"{coin}_usd_1d.csv")
    d["date"] = pd.to_datetime(d["date"], utc=True)
    d = d.set_index("date")[["open", "high", "low", "close"]].astype(float)
    lo, hi = d[["open", "close"]].min(axis=1), d[["open", "close"]].max(axis=1)
    bad_lo, bad_hi = d["low"] < 0.5 * lo, d["high"] > 2 * hi
    d.loc[bad_lo, "low"], d.loc[bad_hi, "high"] = lo[bad_lo], hi[bad_hi]
    full = pd.date_range(d.index.min(), d.index.max(), freq="D", tz="UTC")
    gaps = len(full) - len(d)
    d = d.reindex(full)
    d["close"] = d["close"].ffill()
    d["open"] = d["open"].fillna(d["close"].shift(1))
    if gaps:
        print(f"{coin}: пропущенных дней {gaps} — заполнены последним закрытием")
    return d


def funding_day(coin: str, idx: pd.DatetimeIndex) -> pd.Series:
    """Сумма ставок фандинга за сутки: Binance (кэш №20, с 2021-12), до того Bybit (кэш №5, с
    2021-01), раньше — 0.03% в сутки. Пропуски внутри истории — 0.03%."""
    base = Path(os.environ.get("DATA_ROOT", "data")) / "cache_research"
    parts = []
    by = base / "funding" / f"{coin}USDT_funding.csv"
    if by.exists():
        b = pd.read_csv(by)
        parts.append(pd.Series(b["rate"].astype(float).to_numpy(),
                               index=pd.to_datetime(b["ts"], unit="ms", utc=True)))
    bn_dir = base / "binance_metrics" / "funding" / f"{coin}USDT"
    bn = [pd.read_csv(f) for f in sorted(bn_dir.glob("*.csv"))] if bn_dir.exists() else []
    bn = [x for x in bn if len(x)]
    if bn:
        x = pd.concat(bn)
        s = pd.Series(x["last_funding_rate"].astype(float).to_numpy(),
                      index=pd.to_datetime(pd.to_numeric(x["calc_time"]), unit="ms", utc=True))
        cut = s.index.min()
        parts = [p[p.index < cut] for p in parts] + [s]
    if not parts:
        return pd.Series(FUND_DAY_DEFAULT, index=idx)
    f = pd.concat(parts).sort_index()
    daily = f.groupby(f.index.floor("D")).sum().reindex(idx)
    return daily.fillna(FUND_DAY_DEFAULT)


def exposure(c: np.ndarray, side: int = 1) -> tuple[np.ndarray, np.ndarray, int]:
    """Доля позиции на сутки d (от открытия d до открытия d+1), среднее по 9 окнам, и оборот
    (смена доли в открытие d). Решение по закрытию t, исполнение по открытию t+1."""
    n = len(c)
    cs = pd.Series(c)
    pos = np.zeros((len(LOOKBACKS), n))
    entries = 0
    for j, lb in enumerate(LOOKBACKS):
        ext = (cs.rolling(lb).max() if side > 0 else cs.rolling(lb).min()).shift(1).to_numpy()
        mid = ((cs.rolling(lb).max() + cs.rolling(lb).min()) / 2).to_numpy()
        t = lb
        while t < n - 1:
            brk = c[t] > ext[t] if side > 0 else c[t] < ext[t]
            if not brk:
                t += 1
                continue
            stop, k, out = mid[t], t + 1, n
            entries += 1
            while k < n:
                stop = max(stop, mid[k]) if side > 0 else min(stop, mid[k])
                if (c[k] < stop) if side > 0 else (c[k] > stop):
                    out = k + 1                          # выход по открытию k+1
                    break
                k += 1
            pos[j, t + 1:out] = side                     # сутки t+1 … out−1 в позиции
            t = out
    e = pos.mean(axis=0)
    turn = np.abs(np.diff(pos, axis=1, prepend=0)).mean(axis=0)
    return e, turn, entries


def selftest(c: np.ndarray) -> None:
    """Лукахед: изменение цен после дня t не меняет долю до t+1 включительно."""
    e1, _, _ = exposure(c)
    t = len(c) // 2
    c2 = c.copy()
    c2[t + 1:] *= np.linspace(0.3, 3.0, len(c) - t - 1)
    e2, _, _ = exposure(c2)
    assert np.allclose(e1[:t + 2], e2[:t + 2]), "лукахед!"
    print("самопроверка лукахеда — OK")


def stats(r: pd.Series, e: pd.Series | None = None) -> dict:
    if not len(r):
        return {}
    eq = (1 + r).cumprod()
    yrs = len(r) / 365
    sd = r.std()
    return dict(total=float(eq.iloc[-1] - 1), cagr=float(eq.iloc[-1] ** (1 / yrs) - 1),
                sharpe=float(r.mean() / sd * math.sqrt(365)) if sd > 0 else math.nan,
                dd=float((1 - eq / eq.cummax()).max()),
                expo=float(e.mean()) if e is not None else math.nan)


def fmt(s: dict) -> str:
    x = f" | в рынке {s['expo']:.0%}" if not math.isnan(s.get("expo", math.nan)) else ""
    return (f"итог {s['total']:+8.1%} | в год {s['cagr']:+6.1%} | Шарп {s['sharpe']:+.2f} | "
            f"просадка {s['dd']:5.1%}{x}")


def block_ci(diff: pd.Series, reps: int = 2000) -> tuple[float, float]:
    g = [x.to_numpy() for _, x in diff.groupby(diff.index.strftime("%Y-%m"))]
    rng = np.random.default_rng(24)
    ms = [np.concatenate([g[i] for i in rng.integers(0, len(g), len(g))]).mean()
          for _ in range(reps)]
    return float(np.percentile(ms, 5) * 365), float(np.percentile(ms, 95) * 365)


def cmd_run(a) -> None:
    px = {c: load(c) for c in STARTS}
    idx = px["BTC"].index.intersection(px["ETH"].index)
    selftest(px["BTC"]["close"].to_numpy())
    res = {}
    for side, name in ((1, "T-L"), (-1, "T-LS шорт-нога")):
        tot = pd.Series(0.0, index=idx)
        ex = {}
        for c in STARTS:
            d = px[c].loc[idx]
            e, turn, n_in = exposure(d["close"].to_numpy(), side)
            r_oo = (d["open"].shift(-1) / d["open"] - 1).fillna(0.0).to_numpy()
            f = funding_day(c, idx).to_numpy()
            r = e * r_oo - e * f - turn * (FEE + SLIP)
            tot += 0.5 * pd.Series(r, index=idx)
            ex[c] = pd.Series(e, index=idx)
            res[(name, c, "entries")] = n_in
        res[name] = (tot, ex)
    (tl, ex_l), (ts, _) = res["T-L"], res["T-LS шорт-нога"]
    hold_perp = pd.Series(0.0, index=idx)
    hold_spot = pd.Series(0.0, index=idx)
    r_assets = {}
    for c in STARTS:
        d = px[c].loc[idx]
        r_oo = (d["open"].shift(-1) / d["open"] - 1).fillna(0.0)
        f = funding_day(c, idx)
        r_assets[c] = (r_oo, f)
        hold_perp += 0.5 * (r_oo - f)
        hold_spot += 0.5 * r_oo
    expo = 0.5 * (ex_l["BTC"] + ex_l["ETH"])
    print(f"входов (подпозиций) T-L: BTC {res[('T-L', 'BTC', 'entries')]}, "
          f"ETH {res[('T-L', 'ETH', 'entries')]}")
    crit_rows = {}
    for nm, lo, hi in PERIODS + (FULL,) + ((FROZEN,) if a.frozen else ()):
        sl = slice(lo, hi)
        same = sum(0.5 * ex_l[c].loc[sl].mean() * (r_assets[c][0].loc[sl] - r_assets[c][1].loc[sl])
                   for c in STARTS)
        rows = {"тренд T-L": stats(tl.loc[sl], expo.loc[sl]),
                "тренд T-LS": stats(tl.loc[sl] + ts.loc[sl]),
                "держание перп": stats(hold_perp.loc[sl]),
                "держание спот": stats(hold_spot.loc[sl]),
                "та же доля без тайминга": stats(same)}
        print(f"\n== {nm} ({lo} … {hi})")
        for k, s in rows.items():
            print(f"  {k:24s} {fmt(s)}")
        crit_rows[nm] = rows
        if nm == FULL[0]:
            diff = tl.loc[sl] - same
            clo, chi = block_ci(diff)
            print(f"  тайминг (тренд − та же доля): {diff.mean() * 365:+.1%} в год "
                  f"[90% ДИ блоками {clo:+.1%}; {chi:+.1%}]")
            crit_rows["ci"] = clo
    s4, full = crit_rows[PERIODS[3][0]], crit_rows[FULL[0]]
    crit = {
        "1 S4: итог тренда > 0": s4["тренд T-L"]["total"] > 0,
        "2 S4: Шарп тренда > Шарпа держания перпа": s4["тренд T-L"]["sharpe"]
        > s4["держание перп"]["sharpe"],
        "3 S1, S2, S3: итог тренда > 0 в каждом": all(crit_rows[p[0]]["тренд T-L"]["total"] > 0
                                                       for p in PERIODS[:3]),
        "4 вся история: Шарп выше и просадка меньше, чем у держания перпа":
        full["тренд T-L"]["sharpe"] > full["держание перп"]["sharpe"]
        and full["тренд T-L"]["dd"] < full["держание перп"]["dd"],
        "5 вся история: тайминг > 0, нижняя граница ДИ > 0": crit_rows["ci"] > 0,
    }
    print()
    for k, ok in crit.items():
        print(f"  {'✓' if ok else '✗'} {k}")
    print(f"\nИТОГ: {'КАНДИДАТ' if all(crit.values()) else 'не кандидат'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    p = sub.add_parser("run")
    p.add_argument("--frozen", action="store_true", help="показать заморозку (один раз)")
    a = ap.parse_args()
    {"fetch": cmd_fetch, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    main()
