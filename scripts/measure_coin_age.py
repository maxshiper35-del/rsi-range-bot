"""Замер: только монеты старше года (docs/measurements/2026-10-27-coin-age.md).

  .venv/bin/python -m scripts.measure_coin_age
"""
from __future__ import annotations

from multiprocessing import Pool

import pandas as pd

from app.sim.portfolio import simulate_portfolio
from scripts.measure_one_position import SPLIT2Y, _load_rot, _p95, _stats

START = pd.Timestamp("2025-04-01", tz="UTC")
AGE_DAYS = 365


def _utc(v) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _run(variant: str) -> list[tuple]:
    bars, sigs = _load_rot()
    first = {sym: _utc(df["dt"].iloc[0]) for (sym, tf), df in bars.items() if tf == "1h"}
    sigs = [s for s in sigs if s["tf"] == "1h" and _utc(s["dt"]) >= START]
    if variant == "A12":
        sigs = [s for s in sigs if (_utc(s["dt"]) - first[s["symbol"]]).days >= AGE_DAYS]
    h1 = {sym: df for (sym, tf), df in bars.items() if tf == "1h"}
    rep = simulate_portfolio(
        bars, sigs, max_open_per_side=6, cooldown_stops=3, cooldown_hours=6.0, dedup=True,
        fee_pct=0.055, slippage_pct=0.15, funding_bps_per_bar=0.125, trail_activate_r=1.5,
        trail_distance_r=0.2, entry_fill="touch", live_judge_bars=h1, tp_exit=False,
        max_hold_hours=168.0)
    return sorted((_utc(t.signal_dt) + pd.Timedelta(hours=1), _utc(t.exit_dt), t.result_r)
                  for t in rep.detail)


def main() -> None:
    with Pool(2) as pool:
        base, var = pool.map(_run, ("base", "A12"))
    ok = True
    for lab, lo in (("H1", True), ("H2", False)):
        b = [x for x in base if (x[0] < SPLIT2Y) == lo]
        v = [x for x in var if (x[0] < SPLIT2Y) == lo]
        sb, sv = _stats(b), _stats(v)
        p95 = _p95(b, min(sv["n"], sb["n"]))
        checks = [sv["exp"] >= sb["exp"] + 0.03, sv["exp"] > p95, sv["n"] >= 0.3 * sb["n"],
                  sv["dd"] <= sb["dd"], sv["sum"] >= sb["sum"]]
        ok &= all(checks)
        print(f"{lab}: база n={sb['n']} Σ={sb['sum']:+.1f} exp={sb['exp']:+.3f} DD={sb['dd']} | "
              f"A12 n={sv['n']} Σ={sv['sum']:+.1f} exp={sv['exp']:+.3f} DD={sv['dd']} "
              f"p95={p95:+.3f} | 1-5: {''.join('+' if c else '-' for c in checks)}")
    print("A12 →", "ПРИНЯТ" if ok else "не принят")


if __name__ == "__main__":
    main()
