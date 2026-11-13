"""Замер №25: арбитраж фандинга Hyperliquid против Binance — шорт там, где фандинг выше,
лонг того же объёма там, где ниже (docs/measurements/2026-11-13-funding-arb.md).

  DATA_ROOT="/Users/mac/bot ctypto/data" .venv/bin/python -m scripts.measure_funding_arb fetch-hl
  DATA_ROOT="/Users/mac/bot ctypto/data" .venv/bin/python -m scripts.measure_funding_arb fetch-bn
  DATA_ROOT="/Users/mac/bot ctypto/data" .venv/bin/python -m scripts.measure_funding_arb run

fetch-hl — почасовой фандинг и премия Hyperliquid (info fundingHistory, 500 записей за запрос,
           пауза 2.3 с — лимит веса 1200 в минуту) для монет, которые есть и на Binance USDT-M и
           листинг которых на HL был до START_MAX; с START, с продолжением с места остановки.
fetch-bn — месячный фандинг Binance тех же монет (data.binance.vision), кэш общий с №20.
run      — 6 вариантов (порог разницы θ × удержание H), решение раз в сутки в 00:00 UTC по разнице
           фандинга за 72 ч, участки по дате выхода, плацебо «та же монета, сторона и месяц,
           любой день».
           Заморозка — `run --frozen`, один раз.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd

from scripts import hl_fetch
from scripts.binance_metrics_fetch import HOST, fetch, run_calls
from scripts.binance_metrics_fetch import root as bm_root

START = pd.Timestamp("2024-01-01", tz="UTC")
START_MAX = pd.Timestamp("2024-07-01", tz="UTC")   # во вселенной — монеты с листингом на HL раньше
END = pd.Timestamp("2026-11-01", tz="UTC")
hl_fetch.PAUSE["fundingHistory"] = 2.3
hl_fetch.PAUSE["meta"] = 2.3


def root() -> Path:
    p = Path(os.environ.get("DATA_ROOT", "data")) / "cache_research" / "funding_arb"
    (p / "hl").mkdir(parents=True, exist_ok=True)
    return p


def to_bn(coin: str) -> str:
    return ("1000" + coin[1:] if re.fullmatch(r"k[A-Z0-9]+", coin) else coin) + "USDT"


def universe_candidates() -> list[str]:
    p = root() / "hl_meta.json"
    if not p.exists():
        p.write_text(json.dumps(hl_fetch.post({"type": "meta"})))
    meta = json.loads(p.read_text())
    bn = {f.stem for f in (bm_root() / "index").glob("*.txt")}
    return [x["name"] for x in meta["universe"] if to_bn(x["name"]) in bn]


def fetch_coin(coin: str, sub: str = "hl", start_max: pd.Timestamp | None = START_MAX) -> str:
    """sub — папка кэша; start_max=None — без отсечки по дате листинга (№26)."""
    (root() / sub).mkdir(exist_ok=True)
    path = root() / sub / f"{coin}.csv"
    skip = root() / sub / f"{coin}.late"
    if skip.exists():
        return "листинг позже"
    t0 = int(START.timestamp() * 1000)
    if path.exists():
        last = pd.read_csv(path)["time"].max()
        t0 = int(last) + 1
    rows, first = [], True
    while t0 < int(END.timestamp() * 1000):
        page = hl_fetch.post({"type": "fundingHistory", "coin": coin, "startTime": t0,
                              "endTime": int(END.timestamp() * 1000)})
        if not page:
            break
        if first and start_max is not None and not path.exists() \
                and page[0]["time"] > start_max.timestamp() * 1000:
            skip.touch()
            return "листинг позже"
        first = False
        rows += [(r["time"], r["fundingRate"], r["premium"]) for r in page]
        t0 = int(page[-1]["time"]) + 1
        if len(page) < 500:
            break
        if len(rows) >= 5000:                           # сброс на диск каждые 10 страниц
            _append(path, rows)
            rows = []
        if hl_fetch.stop_asked():
            break
    _append(path, rows)
    return "скачан"


def _append(path: Path, rows: list) -> None:
    if not rows:
        return
    d = pd.DataFrame(rows, columns=["time", "fundingRate", "premium"])
    d.to_csv(path, mode="a", header=not path.exists(), index=False)


def cmd_fetch_hl(_a) -> None:
    coins = universe_candidates()
    print(f"монет и на HL, и на Binance: {len(coins)}", flush=True)
    stats: dict[str, int] = {}
    for i, c in enumerate(coins, 1):
        res = fetch_coin(c)
        stats[res] = stats.get(res, 0) + 1
        if i % 10 == 0 or i == len(coins):
            print(f"  {i}/{len(coins)} {stats} {pd.Timestamp.now(tz='UTC'):%H:%M}", flush=True)


def cmd_fetch_bn(_a) -> None:
    coins = universe_candidates()                   # все кандидаты: лишние файлы не мешают
    months = pd.period_range(START, END - pd.Timedelta(days=1), freq="M").strftime("%Y-%m")
    calls = []
    for c in coins:
        s = to_bn(c)
        for ym in months:
            calls.append(lambda s=s, ym=ym: fetch(
                f"{HOST}/monthly/fundingRate/{s}/{s}-fundingRate-{ym}.zip",
                bm_root() / "funding" / s / f"{s}-fundingRate-{ym}.csv"))
    run_calls(calls)


THETAS = (0.20, 0.50, 1.00)          # порог |разницы| в годовых
HOLDS = (3, 7)                         # удержание, суток
LOOK_H = 72                            # окно сигнала, часов
COST_RT = 2 * (0.00045 + 0.0002) + 2 * (0.0005 + 0.0002)   # 4 сделки тейкером: HL и Binance
PERIODS = (("учёба", "2024-01-01", "2024-12-31"), ("проверка A", "2025-01-01", "2025-12-31"),
           ("проверка B", "2026-01-01", "2026-07-31"), ("заморозка", "2026-08-01", "2026-10-31"))


def hourly_hl(coin: str, sub: str = "hl") -> pd.DataFrame:
    d = pd.read_csv(root() / sub / f"{coin}.csv").drop_duplicates("time")
    t = pd.to_datetime(d["time"], unit="ms", utc=True).dt.floor("min")   # мс-сдвиг (урок №24)
    return pd.DataFrame({"f": d["fundingRate"].astype(float).to_numpy(),
                         "prem": d["premium"].astype(float).to_numpy()}, index=t).sort_index()


def events_bn(coin: str) -> pd.Series:
    s = to_bn(coin)
    parts = [pd.read_csv(f) for f in sorted((bm_root() / "funding" / s).glob("*.csv"))]
    parts = [x for x in parts if len(x)]
    if not parts:
        return pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC"))
    x = pd.concat(parts)
    t = pd.to_datetime(pd.to_numeric(x["calc_time"]), unit="ms", utc=True).dt.floor("min")
    return pd.Series(x["last_funding_rate"].astype(float).to_numpy(), index=t).sort_index()


def hourly_diff(coin: str, sub: str = "hl") -> pd.DataFrame | None:
    """Почасовая сетка: фандинг HL за час, фандинг Binance, начисленный в этот час (событие —
    в час своего времени), разница. Только часы, где есть обе биржи."""
    hl = hourly_hl(coin, sub)
    bn = events_bn(coin)
    if len(hl) < LOOK_H * 2 or len(bn) < 10:
        return None
    lo, hi = max(hl.index[0], bn.index[0].floor("h")), min(hl.index[-1], bn.index[-1].ceil("h"))
    if lo >= hi:
        return None
    grid = pd.date_range(lo.ceil("h"), hi.floor("h"), freq="h", tz="UTC")
    f_hl = hl["f"].groupby(hl.index.floor("h")).sum().reindex(grid)
    f_bn = bn.groupby(bn.index.floor("h")).sum().reindex(grid).fillna(0.0)
    d = pd.DataFrame({"hl": f_hl, "bn": f_bn,
                      "prem": hl["prem"].groupby(hl.index.floor("h")).mean().reindex(grid)})
    d["ok"] = d["hl"].notna()
    d["hl"] = d["hl"].fillna(0.0)
    d["diff"] = d["hl"] - d["bn"]                 # шорт HL / лонг Binance получает diff
    return d


def trades_for(coin: str, d: pd.DataFrame, theta: float, hold: int) -> list[dict]:
    cs = d["diff"].cumsum()
    okc = (~d["ok"]).astype(int).cumsum()
    out, busy_until = [], None
    days = d.index[(d.index.hour == 0) & (d.index >= d.index[0] + pd.Timedelta(hours=LOOK_H))]
    for t in days:
        if busy_until is not None and t < busy_until:
            continue
        t0 = t - pd.Timedelta(hours=LOOK_H)
        t1 = t + pd.Timedelta(days=hold)
        if t1 > d.index[-1]:
            break
        if okc.at[t] - okc.at[t0] > 0 or okc.at[t1] - okc.at[t] > 0:
            continue                               # дыра в данных HL в окне сигнала или удержания
        sig = (cs.at[t] - cs.at[t0]) * (24 * 365 / LOOK_H)   # годовых
        if abs(sig) < theta:
            continue
        side = 1 if sig > 0 else -1               # +1: шорт HL / лонг Binance
        got = side * (cs.at[t1] - cs.at[t])       # фандинг за (t, t1]
        basis = side * (d["prem"].at[t] - d["prem"].at[t1]) if np.isfinite(d["prem"].at[t]) \
            and np.isfinite(d["prem"].at[t1]) else math.nan
        out.append(dict(coin=coin, t_in=t, t_out=t1, side=side, sig=sig, got=got,
                        net=got - COST_RT, basis=basis, H=hold, theta=theta))
        busy_until = t1
    return out


def placebo_for(d: pd.DataFrame, side: int, t_in: pd.Timestamp, hold: int) -> float:
    """Та же монета и сторона, входы в 00:00 во все дни того же месяца: средний фандинг за H
    минус издержки."""
    cs = d["diff"].cumsum()
    m0 = t_in.replace(day=1)
    m1 = m0 + pd.offsets.MonthBegin(1)
    days = d.index[(d.index.hour == 0) & (d.index >= m0) & (d.index < m1)]
    v = [side * (cs.at[t + pd.Timedelta(days=hold)] - cs.at[t]) for t in days
         if t + pd.Timedelta(days=hold) <= d.index[-1]]
    return float(np.mean(v)) - COST_RT if v else math.nan


def period_of(t: pd.Timestamp) -> str | None:
    s = t.strftime("%Y-%m-%d")
    return next((n for n, a, b in PERIODS if a <= s <= b), None)


def block_ci(df: pd.DataFrame, col: str, reps: int = 2000) -> tuple[float, float]:
    g = [x[col].to_numpy() for _, x in df.groupby(df["t_in"].dt.strftime("%Y-%m"))]
    if len(g) < 3:
        return math.nan, math.nan
    rng = np.random.default_rng(25)
    ms = [np.concatenate([g[i] for i in rng.integers(0, len(g), len(g))]).mean()
          for _ in range(reps)]
    return float(np.percentile(ms, 5)), float(np.percentile(ms, 95))


def summary(g: pd.DataFrame) -> str:
    if not len(g):
        return "n 0"
    y = g["net"].mean() / g["H"].iloc[0] * 365
    return (f"n {len(g):4d} | net {g['net'].mean():+.3%} на сделку ({y:+.1%} годовых на позицию) | "
            f"фандинг {g['got'].mean():+.3%} | в плюсе {np.mean(g['net'] > 0):.0%} | плацебо "
            f"{g['plac'].mean():+.3%} | сверх плацебо {(g['net'] - g['plac']).mean():+.3%} | "
            f"базис (справочно) {g['basis'].mean():+.3%} | монет {g['coin'].nunique()}")


def cmd_run(a) -> None:
    rows = []
    coins = sorted(p.stem for p in (root() / "hl").glob("*.csv"))
    for c in coins:
        d = hourly_diff(c)
        if d is None:
            continue
        for th in THETAS:
            for h in HOLDS:
                for tr in trades_for(c, d, th, h):
                    tr["plac"] = placebo_for(d, tr["side"], tr["t_in"], h)
                    tr["period"] = period_of(tr["t_out"])
                    if tr["period"]:
                        rows.append(tr)
    t = pd.DataFrame(rows)
    t.to_pickle(root() / "trades.pkl")
    print(f"монет с данными обеих бирж: {t['coin'].nunique()} из {len(coins)}")
    print("\n== Учёба, все 6 (выбор — max net на сделку в годовых на позицию при n ≥ 30)")
    best, bv = None, -math.inf
    for (th, h), g in t[t.period == "учёба"].groupby(["theta", "H"]):
        print(f"  θ {th:.0%} H {h}: {summary(g)}")
        y = g["net"].mean() / h * 365
        if len(g) >= 30 and y > bv:
            best, bv = (th, h), y
    th, h = best
    print(f"\nВЫБРАН: θ {th:.0%}, H {h} сут")
    sel = t[(t.theta == th) & (t.H == h)]
    for per in ("учёба", "проверка A", "проверка B"):
        print(f"  {per:10s}: {summary(sel[sel.period == per])}")
    v = sel[sel.period.isin(["проверка A", "проверка B"])].copy()
    lo, hi = block_ci(v, "net")
    wo = v.drop(v["net"].nlargest(5).index)["net"].mean()
    by_coin = v.groupby("coin")["net"].sum()
    top_share = by_coin.max() / by_coin.sum() if by_coin.sum() > 0 else math.nan
    a_, b_ = sel[sel.period == "проверка A"], sel[sel.period == "проверка B"]
    print(f"\n  A+B: net {v['net'].mean():+.3%} [90% ДИ блоками {lo:+.3%}; {hi:+.3%}], "
          f"без 5 лучших {wo:+.3%}, доля лучшей монеты {top_share:.0%} "
          f"({by_coin.idxmax() if len(by_coin) else '-'})")
    crit = {
        "1 net > 0 в A и в B": a_["net"].mean() > 0 and b_["net"].mean() > 0,
        "2 нижняя граница ДИ net A+B > 0": lo > 0,
        "3 без 5 лучших сделок net > 0": wo > 0,
        "4 лучшая монета < 30% суммы": top_share < 0.30,
        "5 сверх плацебо > 0 в A и в B": (a_["net"] - a_["plac"]).mean() > 0
        and (b_["net"] - b_["plac"]).mean() > 0,
    }
    for k, ok in crit.items():
        print(f"  {'✓' if ok else '✗'} {k}")
    print(f"\nИТОГ: {'КАНДИДАТ' if all(crit.values()) else 'не кандидат'}")
    if a.frozen:
        print(f"\n== ЗАМОРОЗКА (один раз): {summary(sel[sel.period == 'заморозка'])}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch-hl")
    sub.add_parser("fetch-bn")
    p = sub.add_parser("run")
    p.add_argument("--frozen", action="store_true", help="показать заморозку (один раз)")
    a = ap.parse_args()
    {"fetch-hl": cmd_fetch_hl, "fetch-bn": cmd_fetch_bn, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    main()
