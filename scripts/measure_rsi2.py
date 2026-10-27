"""Замер: RSI(2) Коннорса на крипте (docs/measurements/2026-10-27-rsi2.md).

  .venv/bin/python -m scripts.measure_rsi2
"""
from __future__ import annotations

import glob
from multiprocessing import Pool

import numpy as np
import pandas as pd

from scripts.measure_trend import (
    FEE,
    FUND_DAY,
    MIN_TURNOVER,
    NINE,
    NINE_DIR,
    SLIP,
    SPLIT,
    UNIV,
    _load,
)

COST_SIDE = FEE + SLIP


def rsi(close: pd.Series, n: int = 2) -> pd.Series:
    """RSI Уайлдера: сглаживание alpha=1/n."""
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def trades(d: pd.DataFrame) -> list[tuple]:
    """[(дата входа, чистая доходность)] — сигнал по закрытию t, сделки по открытию t+1."""
    if len(d) < 210:
        return []
    c, o = d["close"], d["open"].to_numpy(float)
    sma200, sma5, r2 = c.rolling(200).mean(), c.rolling(5).mean(), rsi(c)
    turn = d["turn"].rolling(30).median().shift(1)
    cv, n, out, t = c.to_numpy(float), len(d), [], 200
    while t < n - 1:
        if not (cv[t] > sma200.iloc[t] and r2.iloc[t] < 10 and turn.iloc[t] >= MIN_TURNOVER):
            t += 1
            continue
        entry, k = o[t + 1], t + 1
        while k < n - 1 and not cv[k] > sma5.iloc[k]:
            k += 1
        exit_px = o[k + 1] if k < n - 1 else cv[-1]
        held = (k + 1) - (t + 1)
        ret = exit_px / entry - 1 - COST_SIDE * (1 + exit_px / entry) - FUND_DAY * held
        out.append((d.index[t + 1], ret))
        t = k + 1
    return out


def _coin(path: str) -> list[tuple]:
    _name, d = _load(path)
    return trades(d)


def main() -> None:
    sets = {"U": sorted(glob.glob(str(UNIV / "*_1h.csv"))),
            "N": [str(NINE_DIR / f"{b}-USDT-USDT_1h.csv") for b in NINE]}
    ok = True
    with Pool(8) as pool:
        for name, files in sets.items():
            tr = [x for ch in pool.map(_coin, files) for x in ch]
            print(f"== {name}: сделок {len(tr)}")
            for lab, lo in (("H1", True), ("H2", False)):
                rs = np.array([r for dt, r in tr if (dt < SPLIT) == lo])
                if not len(rs):
                    print(f"  {lab}: сделок нет")
                    ok = False
                    continue
                need = 0.005 if name == "U" else 0.0
                ok &= bool(rs.mean() >= need) if name == "U" else bool(rs.mean() > need)
                print(f"  {lab}: n={len(rs)} средняя {rs.mean() * 100:+.2f}% медиана "
                      f"{np.median(rs) * 100:+.2f}% побед {np.mean(rs > 0) * 100:.0f}% "
                      f"худшая {rs.min() * 100:+.1f}% сумма {rs.sum() * 100:+.0f}%")
    print("RSI(2) →", "кандидат" if ok else "не принят")


if __name__ == "__main__":
    main()
