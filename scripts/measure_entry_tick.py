"""Замер: вход сигнала не кратен шагу цены (docs/measurements/2026-11-10-entry-tick.md).

  DATA_ROOT="/Users/mac/bot ctypto/data" PYTHONDONTWRITEBYTECODE=1 nice -n 15 \
      .venv/bin/python -m scripts.measure_entry_tick [--db копия_signals.db] [--bingx]

Описательный счёт: только факт касания входа, исходы сделок не считаются.
  * история — 1h-сигналы обеих выборок (9 монет и ротация), решения < 2026-08-01;
  * --db — опубликованные и не исполненные сигналы журнала прода: часовые свечи Bybit по
    публичному API, не чаще 2 запросов в секунду;
  * --bingx — сравнить шаг цены Bybit и BingX по монетам журнала (один публичный запрос).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.request
from collections import Counter
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

import scripts.research_judge as rj
from app.brain.journal import MAX_WAIT_HOURS

ROOT = rj.rebind_data()
HOUR_NS = 3_600_000_000_000
CUT = pd.Timestamp("2026-08-01", tz="UTC")
TICK_BACK, TICK_FWD = 300, 50          # окно шага цены: баров до и после решения
EPS = 1e-6                             # допуск кратности, в шагах
CLOSE_SHARE = 0.005                    # порог регистрации: G < 0.5% от исполненных


def decimals(values: np.ndarray) -> int:
    """Число знаков после запятой, при котором все цены — целые (шаг биржи ≤ 10^-k)."""
    sample = np.unique(values)[:: max(1, len(np.unique(values)) // 4000)]
    k = max((-Decimal(repr(float(v))).as_tuple().exponent for v in sample), default=0)
    k = max(int(k), 0)
    while k < 12:
        x = values * 10.0 ** k
        if np.abs(x - np.rint(x)).max() < 1e-4:
            break
        k += 1
    return k


def classify(x: float, c0: float, lo: np.ndarray, hi: np.ndarray) -> dict:
    """Вход x и цены окна касания — в шагах цены. c0 — close решающего бара.
    -> кратность, касание по точному входу, по шагу со стороны рынка и по ближайшему шагу."""
    frac = x - np.floor(x)
    nonmult = bool(min(frac, 1.0 - frac) > EPS)

    def first(level: float) -> int | None:
        hit = np.nonzero((lo <= level + EPS) & (level - EPS <= hi))[0]
        return int(hit[0]) if hit.size else None

    exact = first(x)
    if not nonmult or abs(c0 - x) <= EPS:
        market = round(x)                  # кратный вход: соседний шаг — это он сам
    else:
        market = np.ceil(x - EPS) if c0 > x else np.floor(x + EPS)
    nearest = np.floor(x + 0.5 + EPS)      # ccxt ROUND: половина — вверх
    return dict(nonmult=nonmult, exact=exact, market=first(float(market)),
                nearest=first(float(nearest)))


def history(data: str) -> tuple[Counter, Counter, list]:
    """Счётчики по 1h-сигналам выборки (решения < CUT)."""
    bars, sigs = rj.load(data)
    cnt: Counter = Counter()
    by_sym: Counter = Counter()
    rel = []                               # шаг/цена у сигналов-«зазоров», б.п.
    prep: dict = {}
    for s in sigs:
        decision = rj.utc(s["dt"]) + rj.HOUR
        if decision >= CUT:
            continue
        sym = s["symbol"]
        if sym not in prep:
            df = bars[(sym, "1h")]
            ohlc = df[["open", "high", "low", "close"]].to_numpy(float)
            k = decimals(ohlc.ravel())
            dts = pd.to_datetime(df["dt"], utc=True).to_numpy(dtype="datetime64[ns]")
            prep[sym] = (dts.astype(np.int64), np.rint(ohlc * 10.0 ** k).astype(np.int64), k)
        dts, px, k = prep[sym]
        dec_ns = int(decision.value)
        i0 = int(np.searchsorted(dts, dec_ns, "left"))
        k_exp = int(np.searchsorted(dts, dec_ns + int(MAX_WAIT_HOURS * HOUR_NS), "left"))
        if i0 < 1 or i0 >= len(dts) or k_exp <= i0:
            continue
        g = int(np.gcd.reduce(px[max(0, i0 - TICK_BACK):min(len(px), i0 + TICK_FWD)].ravel()))
        if g <= 0:
            continue
        x = float(s["entry"]) * 10.0 ** k / g
        r = classify(x, px[i0 - 1, 3] / g, px[i0:k_exp, 2] / g, px[i0:k_exp, 1] / g)
        cnt["сигналов"] += 1
        cnt["вход не кратен шагу"] += r["nonmult"]
        if r["exact"] is not None:
            cnt["исполнено по точному входу"] += 1
            if r["nearest"] is not None and r["nearest"] < r["exact"]:
                cnt["E: ближайший шаг задет раньше"] += 1
        else:
            if r["market"] is not None:
                cnt["G: зазор (шаг со стороны рынка задет)"] += 1
                by_sym[sym.split("/")[0]] += 1
                rel.append(g / 10.0 ** k / float(s["entry"]) * 1e4)
            if r["nearest"] is not None:
                cnt["G_exec: зазор по ближайшему шагу"] += 1
    return cnt, by_sym, rel


def bybit_1h(symbol: str, start_ms: int, end_ms: int) -> np.ndarray:
    """Часовые свечи Bybit linear [start, end] одним публичным запросом (≤ 200 баров)."""
    url = ("https://api.bybit.com/v5/market/kline?category=linear"
           f"&symbol={symbol.split('/')[0]}USDT&interval=60&start={start_ms}&end={end_ms}"
           "&limit=200")
    with urllib.request.urlopen(url, timeout=20) as r:
        rows = json.load(r)["result"]["list"]
    return np.array(sorted([[int(k[0])] + [float(v) for v in k[1:5]] for k in rows]))


def journal(db: Path) -> None:
    """Опубликованные сигналы прода без входа (expired и ещё open без filled_at)."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, symbol, side, timeframe, entry, status, created_at FROM signals "
        "WHERE status != 'rejected' AND filled_at IS NULL AND status IN ('expired', 'open') "
        "ORDER BY id").fetchall()
    total = con.execute("SELECT COUNT(*) FROM signals WHERE status != 'rejected'").fetchone()[0]
    filled = con.execute("SELECT COUNT(*) FROM signals WHERE status != 'rejected' "
                         "AND filled_at IS NOT NULL").fetchone()[0]
    con.close()
    tf_min = {"15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
    cnt: Counter = Counter()
    gaps: list = []
    odd: list = []
    for r in rows:
        decision = pd.Timestamp(r["created_at"]).tz_convert("UTC") + pd.Timedelta(
            minutes=tf_min.get(r["timeframe"], 60))
        dec_ms = int(decision.value // 1_000_000)
        try:
            b = bybit_1h(r["symbol"], dec_ms - 120 * 3_600_000,
                         dec_ms + int(MAX_WAIT_HOURS) * 3_600_000)
        except Exception as e:                          # noqa: BLE001 — сеть: сигнал пропущен
            cnt["свечи не получены"] += 1
            print(f"  #{r['id']} {r['symbol']}: {type(e).__name__}")
            time.sleep(0.6)
            continue
        time.sleep(0.6)
        if len(b) < 10:
            cnt["свечей нет (делистинг?)"] += 1
            continue
        k = decimals(b[:, 1:5].ravel())
        px = np.rint(b[:, 1:5] * 10.0 ** k).astype(np.int64)
        g = int(np.gcd.reduce(px.ravel()))
        i0 = int(np.searchsorted(b[:, 0], dec_ms, "left"))
        i1 = int(np.searchsorted(b[:, 0], dec_ms + int(MAX_WAIT_HOURS) * 3_600_000, "left"))
        if i0 < 1 or i1 <= i0:
            cnt["нет баров окна"] += 1
            continue
        x = float(r["entry"]) * 10.0 ** k / g
        c = classify(x, px[i0 - 1, 3] / g, px[i0:i1, 2] / g, px[i0:i1, 1] / g)
        cnt["без входа, проверено"] += 1
        cnt["вход не кратен шагу"] += c["nonmult"]
        if c["exact"] is not None:
            cnt["по часовым свечам касание есть (журнал вход не засчитал)"] += 1
            odd.append(f"#{r['id']} {r['symbol'].split('/')[0]} {r['side']} {r['timeframe']} "
                       f"{r['status']}, бар +{c['exact']}ч от решения")
        elif c["market"] is not None:
            cnt["G: зазор"] += 1
            gaps.append(f"#{r['id']} {r['symbol'].split('/')[0]} {r['side']} {r['timeframe']} "
                        f"вход {r['entry']:g}, шаг {g / 10.0 ** k:g}")
            cnt["G_exec"] += c["nearest"] is not None
    print(f"\n== Журнал прода: опубликовано {total}, с входом {filled}, без входа {len(rows)}")
    for name, v in cnt.items():
        print(f"  {name}: {v}")
    print("  сигналы-«зазоры»: " + ("; ".join(gaps) if gaps else "нет"))
    if odd:
        print("  касание по часовым свечам без входа в журнале: " + "; ".join(odd))
    if filled:
        print(f"  G к числу сигналов с входом: {cnt['G: зазор'] / filled:.2%}")


def bingx_ticks(db: Path) -> None:
    """Шаг цены BingX против Bybit по монетам журнала (публичные данные, один запрос)."""
    import ccxt

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    syms = [r[0] for r in con.execute(
        "SELECT DISTINCT symbol FROM signals WHERE created_at >= '2026-10-27'")]
    con.close()
    inst = {x["symbol"]: float(x["priceFilter"]["tickSize"]) for x in json.loads(
        (ROOT / "cache_univ" / "instruments.json").read_text()) if x.get("priceFilter")}
    ex = ccxt.bingx({"options": {"defaultType": "swap"}})
    ex.load_markets()
    print("\n== Шаг цены: Bybit против BingX (монеты сигналов с 27.10)")
    same = finer = coarser = 0
    for s in sorted(syms):
        by = inst.get(s.split("/")[0] + "USDT")
        bx = (ex.markets.get(s) or {}).get("precision", {}).get("price")
        if by is None or bx is None:
            print(f"  {s.split('/')[0]:10s} Bybit {by} BingX {bx}")
            continue
        same += abs(by - bx) < 1e-15
        finer += bx < by - 1e-15
        coarser += bx > by + 1e-15
        if abs(by - bx) >= 1e-15:
            print(f"  {s.split('/')[0]:10s} Bybit {by:g} BingX {bx:g}")
    print(f"  одинаковый шаг: {same}; у BingX мельче: {finer}; у BingX крупнее: {coarser}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--bingx", action="store_true")
    a = ap.parse_args()
    verdict = True
    for data, name in (("5y", "9 монет"), ("rot", "ротация")):
        cnt, by_sym, rel = history(data)
        fills = cnt["исполнено по точному входу"]
        print(f"== {name}: 1h-сигналы, решения < 2026-08-01")
        for key, v in cnt.items():
            base = cnt["сигналов"] if key in ("вход не кратен шагу",
                                              "исполнено по точному входу") else fills
            print(f"  {key}: {v}" + (f" ({v / base:.2%})" if key != "сигналов" and base else ""))
        share = cnt["G: зазор (шаг со стороны рынка задет)"] / fills if fills else 0.0
        verdict &= share < CLOSE_SHARE
        print(f"  G к исполненным: {share:.3%} (порог {CLOSE_SHARE:.1%})")
        if rel:
            print(f"  шаг/цена у «зазоров», б.п.: медиана {np.median(rel):.2f}, "
                  f"p10 {np.percentile(rel, 10):.2f}, p90 {np.percentile(rel, 90):.2f}")
            print("  по монетам: " + ", ".join(f"{s} {n}" for s, n in by_sym.most_common(8)))
    print("\nВЕРДИКТ: " + ("G < 0.5% на обеих выборках — закрыть строкой в реестре" if verdict
                           else "G ≥ 0.5% хотя бы на одной выборке — описать и предложить "
                                "регистрацию"))
    if a.db:
        journal(Path(a.db))
        if a.bingx:
            bingx_ticks(Path(a.db))


if __name__ == "__main__":
    main()
