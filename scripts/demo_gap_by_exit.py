"""Разрыв «журнал − демо» у executor по сделкам: журнал, минутная модель и демо
(docs/research/2026-11-10-demo-gap-e5.md). Диагностика исполнения, не замер стратегии.

  DATA_ROOT=<отдельная папка для минуток> .venv/bin/python -m scripts.demo_gap_by_exit \
      --db <копия signals.db> --log logs/bot.log [--versions e5]

По каждой закрытой сделке executor — три R от входа и риска журнала:
  * журнал (`signals.result_r` — как сделку засчитал судья журнала);
  * модель E на минутках Bybit (трейл биржи с момента филла, `scripts/research_fine_judge.py`;
    так же считает минутный судья журнала);
  * демо (филл и закрытие BingX VST, без комиссий).
Чем закрылась позиция на демо, берётся из bot.log: строка выхода executor и объём «закрыто по
рынку N» (0 — позицию раньше закрыла сама биржа: трейл, стоп или ликвидация).

БД и лог — только чтение. Минутки — публичный kline Bybit, один поток, не чаще 2 запросов в
секунду, в DATA_ROOT/cache_tf/m1: указывать отдельную папку, а не кэш прода.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path

import pandas as pd

import scripts.fetch_m1 as m1
import scripts.research_fine_judge as fj

MIN, HOUR = 60_000, 3_600_000
LIQUIDATION_R = -2.0        # демо хуже −2R при стопе −1R — закрытие вне плана, не стоп

SQL = """
SELECT s.id, s.symbol, s.side, s.entry, s.stop, s.result_r, s.filled_at, s.closed_at,
       e.execution_version, e.fill_px, e.close_px, e.note
FROM executor_orders e JOIN signals s ON s.id = e.signal_id
WHERE e.state = 'closed' AND e.fill_px IS NOT NULL AND e.close_px IS NOT NULL
  AND e.fill_qty > 0 AND s.result_r IS NOT NULL AND s.filled_at IS NOT NULL
  AND s.closed_at IS NOT NULL AND e.execution_version IN ({q})
ORDER BY s.id
"""
LINE = re.compile(r"^(\S+) INFO bot executor(?:\([^)]*\))?: (.*)$")
SID = re.compile(r"#(\d+)\b")
MOVE = re.compile(r"ход BingX ([+-]?\d+(?:\.\d+)?)R")
QTY = re.compile(r"закрыто по рынку ([0-9.eE+-]+)")


def _ms(v: str) -> int:
    return int(pd.Timestamp(v).tz_convert("UTC").value // 1_000_000)


def load(db: Path, versions: list[str]) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return pd.read_sql_query(SQL.format(q=",".join("?" * len(versions))), con,
                                 params=versions)
    finally:
        con.close()


def parse_log(path: Path, ids: set[int]) -> dict:
    """{id: {fill_ts, trail_ts, trail_move, exit_text, exit_qty}} из строк executor в bot.log
    (время лога — UTC). Берётся первая строка филла и трейла и последняя строка выхода."""
    out: dict = {i: {} for i in ids}
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            m = LINE.match(raw.rstrip("\n"))
            if not m:
                continue
            ts, text = m.groups()
            sid = SID.search(text)
            if not sid or int(sid.group(1)) not in out:
                continue
            d = out[int(sid.group(1))]
            if "вход исполнен на BingX" in text:
                d.setdefault("fill_ts", ts)
            elif "трейлинг-стоп на бирже" in text:
                d.setdefault("trail_ts", ts)
                mv = MOVE.search(text)
                if mv:
                    d.setdefault("trail_move", float(mv.group(1)))
            elif text.startswith("📤"):
                d["exit_text"] = text
                q = QTY.search(text)
                d["exit_qty"] = float(q.group(1)) if q else None
    return out


def first_touch(entry: float, bars, start_ms: int) -> int | None:
    """Минута первого касания входа на Bybit в часе филла журнала (как у минутной модели)."""
    for t, _o, hi, lo, _c in bars[(bars[:, 0] >= start_ms) & (bars[:, 0] < start_ms + HOUR)]:
        if lo <= entry <= hi:
            return int(t)
    return None


def mechanism(log: dict, demo: float) -> str:
    """Чем закрылась позиция на демо."""
    text, qty = log.get("exit_text") or "", log.get("exit_qty")
    if qty is not None and qty > 0:
        return "по рынку: журнал закрыл" if "за трейлом" not in text else \
            "по рынку: цена за трейлом журнала"
    if demo <= LIQUIDATION_R:
        return "биржа: вне плана (ликвидация)"
    if log.get("trail_ts") and demo > -0.9:
        return "биржа: трейл"
    return "биржа: стоп"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--versions", default="e5")
    a = ap.parse_args()
    df = load(Path(a.db), a.versions.split(","))
    if df.empty:
        print("закрытых сделок выбранных версий нет")
        return
    df["fill_hour"] = df["filled_at"].map(_ms) // HOUR * HOUR
    win: dict = {}
    for r in df.itertuples():
        win.setdefault(r.symbol, []).append((int(r.fill_hour), _ms(r.closed_at) + 2 * HOUR))
    m1.MIN_GAP_S = 0.6                      # один поток × 0.6 с — не чаще 2 запросов в секунду
    m1.ensure(win, threads=1)
    logs = parse_log(Path(a.log), set(df["id"]))
    rows = []
    for r in df.itertuples():
        risk = abs(r.entry - r.stop)
        sgn = 1.0 if r.side == "LONG" else -1.0
        bars = m1.load(r.symbol)
        ev, rg, ex = fj.judge(r.entry, r.stop, r.side, int(r.fill_hour), bars,
                              bar_ms=MIN, mode="path", immediate=True)
        demo = sgn * (r.close_px - r.fill_px) / risk
        lg = logs[r.id]
        # задержка входа: когда executor увидел филл на BingX против первого касания на Bybit
        touch = first_touch(r.entry, bars, int(r.fill_hour))
        lag = None
        if lg.get("fill_ts") and touch is not None:
            lag = (_ms(lg["fill_ts"] + "+00:00") - touch) / MIN
        rows.append(dict(
            id=r.id, coin=r.symbol.split("/")[0], side=r.side, journal=r.result_r, e1m=rg,
            demo=demo, e1m_ev=ev,
            e1m_exit=pd.Timestamp(ex, unit="ms").strftime("%d %H:%M") if ex else None,
            j_closed=r.closed_at[8:16].replace("T", " "), entry_lag_min=lag,
            trail_move=lg.get("trail_move"), exit_qty=lg.get("exit_qty"),
            how=mechanism(lg, demo)))
    out = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    print(out.round(2).to_string(index=False))

    ok = out.dropna(subset=["e1m"])
    print(f"\nСделок {len(out)}; модель E не разрешилась у {len(out) - len(ok)}")
    print(f"Σ журнал {ok['journal'].sum():+.2f}R | Σ минутная модель {ok['e1m'].sum():+.2f}R | "
          f"Σ демо {ok['demo'].sum():+.2f}R")
    print(f"разрыв журнал→демо {(ok['demo'] - ok['journal']).sum():+.2f}R = часовой журнал "
          f"против минутной модели {(ok['e1m'] - ok['journal']).sum():+.2f}R + минутная "
          f"модель против демо {(ok['demo'] - ok['e1m']).sum():+.2f}R")
    for name, part in (("плюс по журналу", ok[ok["journal"] > 0]),
                       ("минус по журналу", ok[ok["journal"] <= 0])):
        print(f"  {name}: n={len(part)} журнал {part['journal'].sum():+.2f} | модель "
              f"{part['e1m'].sum():+.2f} | демо {part['demo'].sum():+.2f}")
    print("\nПо способу закрытия на демо:")
    g = ok.groupby("how").agg(n=("id", "size"), journal=("journal", "sum"),
                              e1m=("e1m", "sum"), demo=("demo", "sum"))
    g["demo_minus_e1m"] = g["demo"] - g["e1m"]
    print(g.round(2).to_string())
    big = ok[(ok["demo"] - ok["e1m"]).abs() > 1.0]
    print("\nСделки, где демо расходится с минутной моделью больше чем на 1R:")
    print(big.assign(diff=big["demo"] - big["e1m"]).round(2)[
        ["id", "coin", "side", "journal", "e1m", "demo", "diff", "entry_lag_min", "trail_move",
         "how"]].to_string(index=False) if len(big) else "  нет")
    rest = ok[(ok["demo"] - ok["e1m"]).abs() <= 1.0]
    print(f"\nОстальные {len(rest)}: демо − минутная модель "
          f"{(rest['demo'] - rest['e1m']).sum():+.2f}R, "
          f"{(rest['demo'] - rest['e1m']).mean():+.3f}R на сделку")


if __name__ == "__main__":
    main()
