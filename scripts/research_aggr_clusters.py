"""Замер «кластеры агрессивных покупок» (docs/measurements/2026-10-31-aggressor-clusters.md).

  nice -n 15 .venv/bin/python -m scripts.research_aggr_clusters describe   # часть D и ворота
  nice -n 15 .venv/bin/python -m scripts.research_aggr_clusters trade      # A/B, только если ворота
"""
from __future__ import annotations

import json
import sys
from collections import deque
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from scripts.research_aggr_fetch import OUT, UNIVERSE, _base

MIN = 60_000
HORIZONS = (15, 60, 240)
KS = (3, 6)
COST_ROUND = 2 * (0.0005 + 0.0002)            # тейкер 0.05% + проскальзывание 0.02% на сторону
FUND = 0.0001                                  # 0.01% за пересечённую метку фандинга
TRAIN = ("2022-01-01", "2024-01-01")
VALID = ("2024-01-01", "2026-08-01")
FROZEN = ("2026-08-01", "2026-10-30")
HALVES = (("2024H1", "2024-01-01", "2024-07-01"), ("2024H2", "2024-07-01", "2025-01-01"),
          ("2025H1", "2025-01-01", "2025-07-01"), ("2025H2", "2025-07-01", "2026-01-01"),
          ("2026H1", "2026-01-01", "2026-08-01"))
MEMES = {
    "DOGE", "SHIB", "PEPE", "FLOKI", "BONK", "WIF", "BOME", "MEME", "PEOPLE", "TURBO", "NEIRO",
    "NEIROETH", "MOODENG", "PNUT", "GOAT", "ACT", "POPCAT", "MEW", "BRETT", "TRUMP", "MELANIA",
    "FARTCOIN", "SPX", "PENGU", "MOG", "DOGS", "CAT", "SATS", "RATS", "BABYDOGE", "HIPPO",
    "CHILLGUY", "VINE", "LADYS", "AIDOGE", "COQ", "MYRO", "SLERF", "BANANA", "MBABYDOGE", "BOB",
    "WOJAK", "SUNDOG", "GIGA", "TOSHI", "PONKE", "MICHI", "FWOG", "SC", "PUMPFUN", "PUMP", "BAN",
    "ZEREBRO", "AIZ", "GRIFFAIN", "TST", "BROCCOLI", "MUBARAK", "哈基米", "币安人生", "我踏马来了",
    "牛来", "龙虾",
}
SEED = 20261031


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def period(ms: int) -> str | None:
    d = _iso(ms)
    for name, (a, b) in (("train", TRAIN), ("valid", VALID), ("frozen", FROZEN)):
        if a <= d < b:
            return name
    return None


def half(ms: int) -> str | None:
    d = _iso(ms)
    for name, a, b in HALVES:
        if a <= d < b:
            return name
    return None


def is_meme(sym: str) -> bool:
    b = _base(sym)
    return b in MEMES or b.removeprefix("1M") in MEMES or "AI16Z" in sym


def top_days() -> dict[str, set[str]]:
    uni = json.loads(UNIVERSE.read_text())
    m = json.loads((OUT / "symbol_map.json").read_text())["map"]
    out: dict[str, set[str]] = {}
    for d, ss in uni.items():
        for s in ss:
            if s in m:
                out.setdefault(m[s], set()).add(d)
    return out


def load(sym: str) -> tuple[np.ndarray, np.ndarray] | None:
    files = sorted((OUT / "m1" / sym).glob("*.npz"))
    ts, xs = [], []
    for f in files:
        z = np.load(f)
        if len(z["t"]):
            ts.append(z["t"])
            xs.append(z["x"])
    if not ts:
        return None
    t = np.concatenate(ts)
    x = np.concatenate(xs).astype(np.float64)
    o = np.argsort(t, kind="stable")
    t, x = t[o], x[o]
    keep = np.concatenate([[True], np.diff(t) > 0])
    return t[keep], x[keep]                   # x: open, high, low, close, volume, taker_buy


def events(t: np.ndarray, x: np.ndarray) -> np.ndarray:
    c, v, tb = x[:, 3], x[:, 4], x[:, 5]
    prev_ok = np.concatenate([[False], np.diff(t) == MIN])
    r = np.full(len(c), np.nan)
    r[1:] = c[1:] / c[:-1] - 1
    r[~prev_ok] = np.nan
    vz = pd.Series(np.where(v > 0, v, np.nan))
    med = vz.rolling(1440, min_periods=720).median().shift(1).to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        share = np.where(v > 0, tb / v, np.nan)
        ev = (r >= 0.01) & (v >= 5 * med) & (share >= 0.65)
    return np.flatnonzero(ev)


def signals(t: np.ndarray, ev: np.ndarray, k: int, days: set[str]) -> list[int]:
    out, win, last = [], deque(), -10**15
    for i in ev:
        ti = t[i]
        win.append(ti)
        while win and win[0] < ti - 59 * MIN:
            win.popleft()
        if len(win) == k and ti >= last + 60 * MIN and _iso(int(ti)) in days:
            out.append(int(i))
            last = ti
    return out


def at(t: np.ndarray, ms: int, tol: int = 5) -> int | None:
    """Индекс последней минуты <= ms, если она не дальше tol минут; иначе None."""
    j = int(np.searchsorted(t, ms, side="right")) - 1
    if j < 0 or ms - t[j] > tol * MIN:
        return None
    return j


def fwd(t: np.ndarray, c: np.ndarray, i: int, h: int) -> float:
    j = at(t, int(t[i]) + h * MIN)
    return float(c[j] / c[i] - 1) if j is not None and j > i else np.nan


def month_range(t: np.ndarray, ms: int) -> tuple[int, int]:
    d = datetime.fromtimestamp(ms / 1000, timezone.utc)
    a = int(datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp() * 1000)
    ny, nm = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    b = int(datetime(ny, nm, 1, tzinfo=timezone.utc).timestamp() * 1000)
    return int(np.searchsorted(t, a)), int(np.searchsorted(t, b))


def describe() -> None:
    days_by = top_days()
    rng = np.random.default_rng(SEED)
    rows = []
    slips = []
    for n_sym, sym in enumerate(sorted(days_by)):
        got = load(sym)
        if got is None:
            continue
        t, x = got
        c = x[:, 3]
        ev = events(t, x)
        for k in KS:
            for i in signals(t, ev, k, days_by[sym]):
                p = period(int(t[i]))
                if p is None:
                    continue
                a, b = month_range(t, int(t[i]))
                cand = rng.integers(a, b, size=60) if b - a > 300 else np.array([], int)
                row = {"sym": sym, "meme": is_meme(sym), "k": k, "t": int(t[i]), "period": p,
                       "half": half(int(t[i])), "day": _iso(int(t[i]))}
                for h in HORIZONS:
                    f = fwd(t, c, i, h)
                    base = [fwd(t, c, int(j), h) for j in cand[:40]]
                    base = [z for z in base if np.isfinite(z)][:20]
                    row[f"f{h}"] = f
                    row[f"b{h}"] = float(np.mean(base)) if base else np.nan
                rows.append(row)
                if i + 1 < len(t) and t[i + 1] == t[i] + MIN:
                    o1, h1, l1 = x[i + 1, 0], x[i + 1, 1], x[i + 1, 2]
                    slips.append((abs(o1 / c[i] - 1), (h1 - l1) / o1))
        if n_sym % 25 == 0:
            print(f"{n_sym}/{len(days_by)} {sym} сигналов {len(rows)}", flush=True)
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "signals.csv", index=False)
    sl = np.array(slips) if slips else np.zeros((0, 2))
    res = {"n_signals": len(df), "slip_gap_median": float(np.median(sl[:, 0])) if len(sl) else None,
           "slip_gap_p90": float(np.quantile(sl[:, 0], 0.9)) if len(sl) else None,
           "entry_range_median": float(np.median(sl[:, 1])) if len(sl) else None, "table": []}
    for p in ("train", "valid", "frozen"):
        for subset in ("all", "ex_meme"):
            for k in KS:
                d = df[(df.period == p) & (df.k == k)]
                if subset == "ex_meme":
                    d = d[~d.meme]
                for h in HORIZONS:
                    e = (d[f"f{h}"] - d[f"b{h}"]).dropna()
                    dd = d.loc[e.index].assign(e=e).groupby("day").e.mean()
                    tstat = (float(dd.mean() / dd.std(ddof=1) * np.sqrt(len(dd)))
                             if len(dd) > 2 else np.nan)
                    res["table"].append({
                        "period": p, "subset": subset, "k": k, "h": h, "n": int(len(e)),
                        "days": int(len(dd)), "mean_excess_pct": float(e.mean() * 100),
                        "median_excess_pct": float(e.median() * 100),
                        "mean_fwd_pct": float(d[f"f{h}"].mean() * 100),
                        "t_by_day": tstat, "share_pos": float((e > 0).mean())})
    tr = [r for r in res["table"] if r["period"] == "train" and r["subset"] == "all"]
    gate_a = any(r["k"] in KS and r["h"] in (60, 240) and r["mean_excess_pct"] > 0.14
                 and r["t_by_day"] > 2 for r in tr)
    gate_b = any(r["k"] == 6 and r["h"] == 240 and r["mean_excess_pct"] < -0.14
                 and r["t_by_day"] < -2 for r in tr)
    res["gate_A"], res["gate_B"] = gate_a, gate_b
    (OUT / "describe.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    print(json.dumps({k: v for k, v in res.items() if k != "table"}, ensure_ascii=False))
    for r in res["table"]:
        print(f"{r['period']:6} {r['subset']:7} K{r['k']} {r['h']:3}m n={r['n']:5} "
              f"days={r['days']:4} excess={r['mean_excess_pct']:+.3f}% "
              f"med={r['median_excess_pct']:+.3f}% fwd={r['mean_fwd_pct']:+.3f}% "
              f"t={r['t_by_day']:+.2f} pos={r['share_pos']:.2f}")


def hourly_atr(t: np.ndarray, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """ATR14 по часовым барам из 1m -> (начало часа, ATR этого часа, считая его закрытым)."""
    hr = (t // (60 * MIN)) * (60 * MIN)
    df = pd.DataFrame({"h": hr, "hi": x[:, 1], "lo": x[:, 2], "c": x[:, 3]})
    g = df.groupby("h").agg(hi=("hi", "max"), lo=("lo", "min"), c=("c", "last"))
    pc = g.c.shift(1)
    tr = np.maximum(g.hi - g.lo, np.maximum((g.hi - pc).abs(), (g.lo - pc).abs()))
    atr = tr.rolling(14, min_periods=14).mean()
    return g.index.to_numpy(), atr.to_numpy()


def atr_at(hours: np.ndarray, atr: np.ndarray, ms: int) -> float:
    """ATR последнего ЗАКРЫТОГО часа до часа сигнала."""
    j = int(np.searchsorted(hours, (ms // (60 * MIN)) * (60 * MIN))) - 1
    return float(atr[j]) if j >= 0 else np.nan


def fund_marks(a: int, b: int) -> int:
    step = 8 * 60 * MIN
    return max(0, b // step - a // step)


def walk(t, x, e, stop, minutes, side):
    """Выход по стопу (худшее из уровня и open) или по close минуты e+minutes-1."""
    j_end = at(t, int(t[e]) + (minutes - 1) * MIN, tol=5)
    if j_end is None or j_end < e:
        return None
    for j in range(e, j_end + 1):
        if side > 0 and x[j, 2] <= stop:
            return min(stop, x[j, 0]), int(t[j]) + MIN
        if side < 0 and x[j, 1] >= stop:
            return max(stop, x[j, 0]), int(t[j]) + MIN
    return x[j_end, 3], int(t[j_end]) + MIN


def trade_a(t, x, i, hours, atr, minutes):
    e = i + 1
    if e >= len(t) or t[e] != t[i] + MIN:
        return None
    entry = x[e, 0]
    a = atr_at(hours, atr, int(t[i]))
    if not np.isfinite(a) or a <= 0:
        return None
    stop = entry - 2 * a
    if stop <= 0:
        return None
    w = walk(t, x, e, stop, minutes, +1)
    if w is None:
        return None
    exit_px, exit_t = w
    return entry, exit_px, (entry - stop) / entry, int(t[e]), exit_t, +1


def trade_b(t, x, i, minutes):
    lo = int(np.searchsorted(t, int(t[i]) - 59 * MIN))
    low, high = float(x[lo:i + 1, 2].min()), float(x[lo:i + 1, 1].max())
    q = -(-(int(t[i]) + MIN) // (15 * MIN)) * (15 * MIN)
    limit = int(t[i]) + MIN + 240 * MIN
    while q + 15 * MIN <= limit:
        jc = at(t, q + 14 * MIN, tol=0)
        if jc is not None and t[jc] == q + 14 * MIN and x[jc, 3] < low:
            e = jc + 1
            if e >= len(t) or t[e] != q + 15 * MIN:
                return None
            entry = x[e, 0]
            if high <= entry:
                return None
            w = walk(t, x, e, high, minutes, -1)
            if w is None:
                return None
            exit_px, exit_t = w
            return entry, exit_px, (high - entry) / entry, int(t[e]), exit_t, -1
        q += 15 * MIN
    return None


def _net(entry, exit_px, risk, t_in, t_out, side, slip=0.0002, fee=0.0005):
    gross = (exit_px / entry - 1) * side
    cost = 2 * (fee + slip) + FUND * fund_marks(t_in, t_out)
    return (gross - cost) / risk, gross - cost


def trade() -> None:
    desc = json.loads((OUT / "describe.json").read_text())
    variants = []
    if desc["gate_A"]:
        variants += [("A", k, m) for k in KS for m in (60, 240)]
    if desc["gate_B"]:
        variants += [("B", 6, m) for m in (60, 240)]
    if not variants:
        print("ворота не пройдены — торговой части нет (регистрация, раздел 5)")
        return
    days_by = top_days()
    rows = []
    for sym in sorted(days_by):
        got = load(sym)
        if got is None:
            continue
        t, x = got
        ev = events(t, x)
        hours, atr = hourly_atr(t, x)
        for k in KS:
            for i in signals(t, ev, k, days_by[sym]):
                p = period(int(t[i]))
                if p is None:
                    continue
                for kind, kk, m in variants:
                    if kk != k:
                        continue
                    r = trade_a(t, x, i, hours, atr, m) if kind == "A" else trade_b(t, x, i, m)
                    if r is None:
                        continue
                    row = {"sym": sym, "meme": is_meme(sym), "var": f"{kind}_K{k}_T{m}",
                           "period": p, "half": half(int(t[i])), "t": int(t[i])}
                    for tag, slip, fee in (("", 0.0002, 0.0005), ("_s05", 0.0005, 0.0005),
                                           ("_s10", 0.0010, 0.0005), ("_bybit", 0.0002, 0.00055)):
                        row["R" + tag], row["ret" + tag] = _net(*r, slip=slip, fee=fee)
                    rows.append(row)
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "trades.csv", index=False)
    summary(df)


def summary(df: pd.DataFrame) -> None:
    out = {}
    tr = df[df.period == "train"].groupby("var").R.agg(["count", "mean"])
    ok = tr[tr["count"] >= 300]
    chosen = ok["mean"].idxmax() if len(ok) else None
    out["train"] = tr.round(4).to_dict()
    out["chosen"] = chosen
    for var in sorted(df["var"].unique()):
        v = df[(df["var"] == var) & (df.period == "valid")]
        top5 = v.R.nlargest(5).sum() if len(v) else 0.0
        out[var] = {
            "valid_n": int(len(v)), "valid_R": float(v.R.mean()) if len(v) else None,
            "valid_sum": float(v.R.sum()), "valid_sum_ex_top5": float(v.R.sum() - top5),
            "valid_R_ex_meme": float(v[~v.meme].R.mean()) if (~v.meme).any() else None,
            "halves": {h: float(v[v.half == h].R.mean()) for h, _a, _b in HALVES
                       if (v.half == h).any()},
            "stress_s05": float(v.R_s05.mean()) if len(v) else None,
            "stress_s10": float(v.R_s10.mean()) if len(v) else None,
            "bybit_fee": float(v.R_bybit.mean()) if len(v) else None,
        }
    (OUT / "trade_summary.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    {"describe": describe, "trade": trade}[sys.argv[1]]()
