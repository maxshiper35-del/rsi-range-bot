"""Замер: трендовая система «пробой максимума закрытий» (Zarattini–Pagani–Barbon 2025) как
вторая стратегия (docs/measurements/2026-10-27-trend-following.md).

  .venv/bin/python -m scripts.measure_trend

Самопроверка на лукахед — первым шагом: изменение цен ПОСЛЕ дня t не меняет ни одного
решения до t.
"""
from __future__ import annotations

import glob
from dataclasses import dataclass, field
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

LOOKBACKS = (5, 10, 20, 30, 60, 90, 150, 250, 360)
WEIGHT = 1 / len(LOOKBACKS)          # ансамбль монеты ≤ 1R
FEE, SLIP, FUND_DAY = 0.00055, 0.0015, 0.0003
MIN_STOP = 0.013
MIN_TURNOVER = 2_000_000.0
CAP = 6
SPLIT = pd.Timestamp("2025-07-28", tz="UTC")
UNIV, NINE_DIR = Path("data/cache_univ"), Path("data/cache5y")
NINE = "BTC,ETH,SOL,XRP,1000PEPE,NEAR,ONDO,HYPE,ZEC".split(",")


@dataclass
class Pos:
    coin: str
    lb: int
    side: int                        # +1 лонг, −1 шорт
    entry_i: int                     # индекс дня входа (по открытию)
    exit_i: int                      # индекс дня выхода (по открытию); n — открыта на конце
    entry: float
    risk: float
    turnover: float
    dates: pd.DatetimeIndex = field(repr=False)
    path: np.ndarray = field(repr=False)     # чистый R на закрытие каждого дня удержания
    r: float = 0.0                           # итог, чистый R (MTM для открытых)

    @property
    def entry_dt(self) -> pd.Timestamp:
        return self.dates[self.entry_i]


def daily_from_1h(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["dt"] = pd.to_datetime(df["dt"], utc=True)
    df["day"] = df["dt"].dt.floor("D")
    df["turn"] = df["volume"] * df["close"]
    g = df.groupby("day")
    d = pd.DataFrame({"open": g["open"].first(), "close": g["close"].last(),
                      "turn": g["turn"].sum(), "bars": g.size()})
    if len(d) and d["bars"].iloc[-1] < 24:          # незакрытый последний день — не решаем
        d = d.iloc[:-1]
    return d


def positions(coin: str, d: pd.DataFrame, side: int) -> list[Pos]:
    """Все подпозиции монеты по всем окнам (без портфельного капа)."""
    if len(d) < 3:
        return []
    c, o = d["close"].to_numpy(float), d["open"].to_numpy(float)
    dates, n = d.index, len(d)
    turn = d["turn"].rolling(30).median().shift(1).to_numpy(float)   # 30 дней ДО сигнала
    out = []
    for lb in LOOKBACKS:
        if n <= lb + 1:
            continue
        cs = pd.Series(c)
        prev_ext = (cs.rolling(lb).max() if side > 0 else cs.rolling(lb).min()).shift(1)
        mid = ((cs.rolling(lb).max() + cs.rolling(lb).min()) / 2).to_numpy(float)
        prev_ext = prev_ext.to_numpy(float)
        t = lb
        while t < n - 1:
            brk = c[t] > prev_ext[t] if side > 0 else c[t] < prev_ext[t]
            if not brk or not (turn[t] >= MIN_TURNOVER):
                t += 1
                continue
            entry, stop = o[t + 1], mid[t]
            risk = (entry - stop) * side
            if risk <= 0 or risk / entry < MIN_STOP:
                t += 1
                continue
            k, exit_i = t + 1, n
            while k < n:
                stop = max(stop, mid[k]) if side > 0 else min(stop, mid[k])
                if (c[k] < stop) if side > 0 else (c[k] > stop):
                    exit_i = k + 1
                    break
                k += 1
            last = min(exit_i, n) - 1                    # последний день удержания
            days = np.arange(t + 1, last + 1)
            notional = entry / risk                      # номинал на 1R
            entry_cost = (FEE + SLIP) * notional
            fund = FUND_DAY * notional * (days - t)      # накопленный фандинг к закрытию дня
            path = (c[days] - entry) * side / risk - entry_cost - fund
            if exit_i < n:
                px = o[exit_i]
                held = exit_i - (t + 1)
                r = ((px - entry) * side / risk - entry_cost - (FEE + SLIP) * px / risk
                     - FUND_DAY * notional * held)
            else:
                r = float(path[-1])                      # открыта на конце — MTM
            out.append(Pos(coin, lb, side, t + 1, exit_i, entry, risk, float(turn[t]),
                           dates, path, r))
            t = exit_i if exit_i < n else n             # новый вход — со дня выхода
    return out


def apply_cap(pos: list[Pos], cap: int = CAP) -> list[Pos]:
    """≤ cap монет с открытым ансамблем на сторону; сверх — пропуск. Выходы раньше входов."""
    events = sorted(pos, key=lambda p: (p.entry_dt, -p.turnover))
    open_until: dict[tuple, pd.Timestamp] = {}          # (сторона, монета) -> дата выхода
    kept = []
    for p in events:
        now = p.entry_dt
        for key in [k for k, until in open_until.items() if until <= now]:
            del open_until[key]
        key = (p.side, p.coin)
        busy = sum(1 for s, _ in open_until if s == p.side)
        if key not in open_until and busy >= cap:
            continue
        end = p.dates[p.exit_i] if p.exit_i < len(p.dates) else pd.Timestamp.max.tz_localize("UTC")
        open_until[key] = max(open_until.get(key, end), end)
        kept.append(p)
    return kept


def equity(pos: list[Pos], start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    """Дневная MTM-кривая Σ весов (R) по календарю [start, end]."""
    idx = pd.date_range(start, end, freq="D", tz="UTC")
    eq = pd.Series(0.0, index=idx)
    for p in pos:
        days = p.dates[p.entry_i:p.entry_i + len(p.path)]
        s = pd.Series(p.path * WEIGHT, index=days).reindex(idx).ffill()
        if p.exit_i < len(p.dates):
            s[s.index >= p.dates[p.exit_i]] = p.r * WEIGHT
        eq = eq.add(s.fillna(0.0), fill_value=0.0)
    return eq


def half_stats(pos: list[Pos], eq: pd.Series, lo: bool) -> dict:
    ps = [p for p in pos if (p.entry_dt < SPLIT) == lo]
    e = eq[eq.index < SPLIT] if lo else eq[eq.index >= SPLIT]
    if not len(e):
        return {"n": 0, "exp": 0.0, "sum": 0.0, "dd": 0.0}
    base = e.iloc[0] if lo else eq[eq.index < SPLIT].iloc[-1] if (eq.index < SPLIT).any() else 0.0
    curve = e - base
    dd = float((curve.cummax().clip(lower=0) - curve).max())
    return {"n": len(ps), "exp": float(np.mean([p.r for p in ps])) if ps else 0.0,
            "sum": float(curve.iloc[-1]), "dd": round(dd, 1)}


def _load(path: str) -> tuple[str, pd.DataFrame]:
    return Path(path).name.split("-USDT")[0], daily_from_1h(pd.read_csv(path))


def _coin_positions(job: tuple) -> list[Pos]:
    path, sides = job
    coin, d = _load(path)
    return [p for s in sides for p in positions(coin, d, s)]


def run(files: list[str], sides: tuple) -> list[Pos]:
    with Pool(4) as pool:
        chunks = pool.map(_coin_positions, [(f, sides) for f in files])
    return [p for ch in chunks for p in ch]


def _selftest() -> None:
    """Лукахед: порча цен после дня t не меняет решений, принятых до t."""
    rng = np.random.default_rng(0)
    n = 800
    c = 100 * np.exp(np.cumsum(rng.normal(0.001, 0.03, n)))
    idx = pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC")
    d = pd.DataFrame({"open": np.r_[c[0], c[:-1]], "close": c, "turn": 5e6, "bars": 24},
                     index=idx)
    base = {(p.lb, p.entry_i, p.exit_i) for p in positions("X", d, 1) if p.exit_i < 500}
    d2 = d.copy()
    d2.iloc[500:, d2.columns.get_loc("close")] *= 3.0
    d2.iloc[500:, d2.columns.get_loc("open")] *= 0.3
    got = {(p.lb, p.entry_i, p.exit_i) for p in positions("X", d2, 1) if p.exit_i < 500}
    assert base == got and base, "лукахед: будущее изменило прошлые решения"


def smc_rot_1h() -> list[tuple]:
    from scripts.measure_1h_trail10 import _run
    return _run(("rot", "V1 только 1h"))[2]


def main() -> None:
    _selftest()
    print("самопроверка лукахеда: OK")
    u_files = sorted(glob.glob(str(UNIV / "*_1h.csv")))
    n_files = [str(NINE_DIR / f"{b}-USDT-USDT_1h.csv") for b in NINE]
    smc = smc_rot_1h()
    for name, sides in (("T-L", (1,)), ("T-LS", (1, -1))):
        for data, files in (("U", u_files), ("N", n_files)):
            raw = run(files, sides)
            pos = apply_cap(raw)
            start = min(p.entry_dt for p in pos)
            end = max(p.dates[-1] for p in pos)
            eq = equity(pos, start, end)
            h = [half_stats(pos, eq, lo) for lo in (True, False)]
            print(f"{name} {data}: подпозиций {len(raw)} → с капом {len(pos)}; "
                  f"exp без капа {np.mean([p.r for p in raw]):+.3f}")
            for lab, s in zip(("H1", "H2"), h):
                print(f"   {lab}: n={s['n']} exp={s['exp']:+.3f} Σ={s['sum']:+.1f}R DD={s['dd']}")
            if data == "U":
                s0 = pd.Timestamp("2024-10-24", tz="UTC")
                idx = pd.date_range(s0, end, freq="D", tz="UTC")
                sm = pd.Series(0.0, index=idx)
                for _dec, edt, r in smc:
                    day = pd.Timestamp(edt).floor("D")
                    if day >= s0:
                        sm[sm.index >= day] += r
                tr = eq.reindex(idx).ffill().fillna(0.0)
                tr = tr - tr.iloc[0]
                for lab, lo in (("H1", True), ("H2", False)):
                    m = (idx < SPLIT) if lo else (idx >= SPLIT)
                    for nm, curve in (("SMC-1h", sm[m]), ("SMC+тренд", (sm + tr)[m])):
                        cv = curve - curve.iloc[0]
                        dd = float((cv.cummax().clip(lower=0) - cv).max())
                        print(f"   комб {lab} {nm:10s}: Σ={cv.iloc[-1]:+.1f}R DD={dd:.1f} "
                              f"Σ/DD={cv.iloc[-1] / dd if dd else float('inf'):.2f}")


if __name__ == "__main__":
    main()
