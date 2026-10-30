"""Сверка модели трейла с реальными выходами демо-executor (раздел 8 регистрации
docs/measurements/2026-10-30-trail-realism.md). Диагностика, в вердикт замера не входит.

  DATA_ROOT=/путь/к/data .venv/bin/python -m scripts.trail_demo_recon [--versions e4,e5]

По каждой закрытой сделке executor выбранных версий — три R от входа и риска журнала:
журнал (`signals.result_r`), модель E на минутках Bybit (трейлинг-стоп биржи с активацией
сразу после филла, `scripts/research_fine_judge.py`) и демо (филл и закрытие BingX VST, без
комиссий и с ними). БД — только чтение; минутки — публичный kline Bybit (`scripts/fetch_m1.py`).

Повторный прогон: та же команда, когда трейл-выходов e5 станет ≥ 60 (сводка печатает счётчик).
Модель E подтверждена исполнением, если на трейл-выходах |среднее (демо − E1m)| ≤ 0.1R.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

import pandas as pd

import scripts.fetch_m1 as m1
import scripts.research_fine_judge as fj

MIN, HOUR = 60_000, 3_600_000
NEED_TRAIL = 60
TOL_R = 0.1

SQL = """
SELECT s.id, s.symbol, s.timeframe, s.side, s.entry, s.stop, s.result_r, s.filled_at,
       s.closed_at, e.execution_version, e.fill_px, e.close_px, e.fill_qty,
       COALESCE(e.fill_fee, 0) + COALESCE(e.close_fee, 0) AS fees
FROM executor_orders e JOIN signals s ON s.id = e.signal_id
WHERE e.state = 'closed' AND e.fill_px IS NOT NULL AND e.close_px IS NOT NULL
  AND e.fill_qty > 0 AND s.result_r IS NOT NULL AND s.filled_at IS NOT NULL
  AND s.closed_at IS NOT NULL AND e.execution_version IN ({q})
ORDER BY s.id
"""


def _ms(v: str) -> int:
    return int(pd.Timestamp(v).tz_convert("UTC").value // 1_000_000)


def load(db: Path, versions: list[str]) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        df = pd.read_sql_query(SQL.format(q=",".join("?" * len(versions))), con,
                               params=versions)
    finally:
        con.close()
    df["fill_hour"] = df["filled_at"].map(_ms) // HOUR * HOUR
    df["end"] = df["closed_at"].map(_ms) + 2 * HOUR
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--versions", default="e4,e5")
    ap.add_argument("--db", default=str(Path(os.environ.get("DATA_ROOT", "data")) / "signals.db"))
    a = ap.parse_args()
    df = load(Path(a.db), a.versions.split(","))
    if df.empty:
        print("закрытых сделок выбранных версий нет")
        return
    win: dict = {}
    for r in df.itertuples():
        win.setdefault(r.symbol, []).append((int(r.fill_hour), int(r.end)))
    m1.ensure(win)
    rows = []
    for r in df.itertuples():
        risk = abs(r.entry - r.stop)
        sgn = 1.0 if r.side == "LONG" else -1.0
        ev, rg, _ex = fj.judge(r.entry, r.stop, r.side, int(r.fill_hour), m1.load(r.symbol),
                               bar_ms=MIN, mode="path", immediate=True)
        demo = sgn * (r.close_px - r.fill_px) / risk
        rows.append(dict(id=r.id, symbol=r.symbol.split("/")[0], tf=r.timeframe, side=r.side,
                         v=r.execution_version, journal=r.result_r, e1m=rg, e1m_ev=ev,
                         demo=demo, demo_net=demo - r.fees / (r.fill_qty * risk)))
    out = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print(out.round(3).to_string(index=False))
    print("\nСводка (R от риска журнала; демо — от цены филла BingX):")
    for v, g in out.groupby("v"):
        trail = g[g["journal"] > 0]
        ok = trail.dropna(subset=["e1m"])
        gap = float((ok["demo"] - ok["e1m"]).mean()) if len(ok) else float("nan")
        print(f"  {v}: сделок {len(g)}; Σ журнал {g['journal'].sum():+.2f}, "
              f"E1m {g['e1m'].sum():+.2f}, демо {g['demo'].sum():+.2f} "
              f"(с комиссиями {g['demo_net'].sum():+.2f})")
        print(f"      трейл-выходов журнала {len(trail)}: "
              f"журнал {trail['journal'].mean():+.3f}/сд, E1m {ok['e1m'].mean():+.3f}, "
              f"демо {trail['demo'].mean():+.3f}; демо − E1m {gap:+.3f}R/сд")
    e5 = out[(out["v"] == "e5") & (out["journal"] > 0)].dropna(subset=["e1m"])
    if len(e5) < NEED_TRAIL:
        print(f"\nВердикт по исполнению: мало данных — трейл-выходов e5 {len(e5)}, "
              f"нужно ≥ {NEED_TRAIL}")
    else:
        gap = float((e5["demo"] - e5["e1m"]).mean())
        verdict = ("модель E подтверждена исполнением" if abs(gap) <= TOL_R
                   else "исполнение расходится с моделью E")
        print(f"\nВердикт по исполнению (e5, n={len(e5)}): демо − E1m {gap:+.3f}R → {verdict}")
    print(f"E1m не разрешилась: {int(out['e1m'].isna().sum())} (нет минуток или нет касания входа)")


if __name__ == "__main__":
    main()
