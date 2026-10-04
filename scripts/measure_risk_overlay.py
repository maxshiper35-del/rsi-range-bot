"""Замер оверлея риска по зарегистрированному критерию
(docs/measurements/2026-10-04-risk-overlay.md). Запуск:

  .venv/bin/python -m scripts.measure_risk_overlay

Пост-обработка сделок бейслайна: отбор не меняется, меняется только вес сделки.
Решение о весе — по информации на момент филла (без заглядывания).
"""
from __future__ import annotations

import bisect
import math
from collections import defaultdict

from scripts.measure_bias_gate import OOS_FROM, _args, _sim
from scripts.run_portfolio import load

DD_THROTTLE_R = 15.0


def w_corr(trades: list) -> list[float]:
    """V1: 1/sqrt(1+k), k — сделки той же стороны в позиции на момент филла."""
    out = []
    by_side = defaultdict(list)
    for t in trades:
        by_side[t.side].append(t)
    for t in trades:
        k = sum(1 for o in by_side[t.side]
                if o is not t and o.fill_dt <= t.fill_dt < o.exit_dt)
        out.append(1.0 / math.sqrt(1 + k))
    return out


def w_dd(trades: list) -> list[float]:
    """V2: 0.5, если эквити (по сделкам, закрытым ДО филла) ниже пика на >= 15R."""
    closed = sorted(trades, key=lambda t: t.exit_dt)
    exits = [t.exit_dt for t in closed]
    eq, peak, dd_after = 0.0, 0.0, []
    for t in closed:
        eq += t.result_r
        peak = max(peak, eq)
        dd_after.append(peak - eq)
    out = []
    for t in trades:
        i = bisect.bisect_left(exits, t.fill_dt)        # закрытые строго до филла
        dd = dd_after[i - 1] if i else 0.0
        out.append(0.5 if dd >= DD_THROTTLE_R else 1.0)
    return out


def curve(trades: list, w: list[float]) -> dict:
    mean_w = sum(w) / len(w)
    norm = [x / mean_w for x in w]
    pairs = sorted(zip(trades, norm), key=lambda p: p[0].exit_dt)
    eq = peak = dd = 0.0
    months: dict = defaultdict(float)
    for t, x in pairs:
        eq += x * t.result_r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        months[(t.exit_dt.year, t.exit_dt.month)] += x * t.result_r
    return {"sum": eq, "dd": dd, "mar": eq / dd if dd else float("inf"),
            "worst_month": min(months.values()),
            "neg_months": sum(1 for v in months.values() if v < 0),
            "months": len(months), "min_w": min(norm), "max_w": max(norm)}


def half(label: str, from_dt: str | None, to_dt: str | None) -> dict:
    bars, sigs = load(_args(from_dt, to_dt, sig_dir="sig_parity"))
    trades = _sim(bars, sigs).detail
    res = {"base": curve(trades, [1.0] * len(trades)),
           "V1": curve(trades, w_corr(trades)),
           "V2": curve(trades, w_dd(trades))}
    print(f"\n[{label}] сделок {len(trades)}")
    for k, c in res.items():
        print(f"  {k:4s}: ΣR={c['sum']:+8.1f} DD={c['dd']:5.1f}R ΣR/DD={c['mar']:5.2f} "
              f"худший месяц={c['worst_month']:+6.1f}R "
              f"минус-месяцев={c['neg_months']}/{c['months']} "
              f"вес[{c['min_w']:.2f}..{c['max_w']:.2f}]")
    return res


def main() -> None:
    full = half("ВСЯ ИСТОРИЯ", None, None)
    b = full["base"]
    assert abs(b["sum"] - 622.4) < 0.5 and abs(b["dd"] - 50.3) < 0.1, \
        f"бейслайн не воспроизведён: {b['sum']:.1f}/{b['dd']:.1f}"
    print("  бейслайн воспроизведён (+622.4R, DD 50.3R)")
    halves = {"TRAIN": half("TRAIN (до 2025-07-28)", None, OOS_FROM),
              "OOS": half("OOS (с 2025-07-28)", OOS_FROM, None)}
    print("\n== ЗАРЕГИСТРИРОВАННЫЙ КРИТЕРИЙ ==")
    for v in ("V1", "V2"):
        ok_all = True
        for h, r in halves.items():
            c1 = r[v]["dd"] <= 0.85 * r["base"]["dd"]
            c2 = r[v]["sum"] >= 0.95 * r["base"]["sum"]
            ok_all &= c1 and c2
            print(f"  {v} {h}: (1) DD {r[v]['dd']:.1f} <= 0.85×{r['base']['dd']:.1f}="
                  f"{0.85 * r['base']['dd']:.1f} -> {c1} | (2) ΣR {r[v]['sum']:+.1f} >= "
                  f"0.95×{r['base']['sum']:+.1f}={0.95 * r['base']['sum']:+.1f} -> {c2}")
        print(f"  ВЕРДИКТ {v}: {'ПРИНЯТЬ' if ok_all else 'ОТВЕРГНУТО'}")


if __name__ == "__main__":
    main()
