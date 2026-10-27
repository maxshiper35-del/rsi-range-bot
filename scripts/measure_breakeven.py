"""Замер: безубыток после +1R поверх «только 1h» (docs/measurements/2026-10-27-breakeven.md).

  .venv/bin/python -m scripts.measure_breakeven
"""
from __future__ import annotations

from multiprocessing import Pool

import pandas as pd

from app.sim.portfolio import simulate_portfolio
from scripts.measure_one_position import SPLIT2Y, SPLIT5Y, _load5y, _load_rot, _stats

TFH = {"1h": 1, "4h": 4}
VARIANTS = {"база (1h)": {}, "B1 BE@1R→0": {"be": 1.0, "lock": 0.0},
            "B2 BE@1R→+0.3": {"be": 1.0, "lock": 0.3}}


def _run(job: tuple) -> tuple:
    data, name = job
    v = VARIANTS[name]
    bars, sigs = _load5y() if data == "5y" else _load_rot()
    sigs = [s for s in sigs if s["tf"] == "1h"]
    h1 = {sym: df for (sym, tf), df in bars.items() if tf == "1h"}
    rep = simulate_portfolio(
        bars, sigs, max_open_per_side=6, cooldown_stops=3, cooldown_hours=6.0, dedup=True,
        fee_pct=0.055, slippage_pct=0.15, funding_bps_per_bar=0.125, trail_activate_r=1.5,
        trail_distance_r=0.2, entry_fill="touch", live_judge_bars=h1, tp_exit=False,
        max_hold_hours=168.0, be_trigger_r=v.get("be", 0.0), be_lock_r=v.get("lock", 0.0))
    out = []
    for t in rep.detail:
        sdt = pd.Timestamp(t.signal_dt)
        sdt = sdt.tz_localize("UTC") if sdt.tzinfo is None else sdt.tz_convert("UTC")
        edt = pd.Timestamp(t.exit_dt)
        edt = edt.tz_localize("UTC") if edt.tzinfo is None else edt.tz_convert("UTC")
        out.append((sdt + pd.Timedelta(hours=TFH[t.tf]), edt, t.result_r))
    return data, name, sorted(out)


def main() -> None:
    jobs = [(d, n) for d in ("5y", "rot") for n in VARIANTS]
    with Pool(6) as pool:
        res = {(d, n): tr for d, n, tr in pool.map(_run, jobs)}
    for data, split in (("5y", SPLIT5Y), ("rot", SPLIT2Y)):
        print(f"== {data}{'' if data == '5y' else ' (подтверждение, не в вердикт)'}")
        halves = {n: [_stats([x for x in res[(data, n)] if (x[0] < split) == lo])
                      for lo in (True, False)] for n in VARIANTS}
        base = halves["база (1h)"]
        for name, hs in halves.items():
            line, ok = [], True
            for lab, b, s in zip(("TRAIN", "OOS"), base, hs):
                checks = [s["exp"] >= b["exp"] + 0.03, s["dd"] <= b["dd"], s["sum"] >= b["sum"]]
                ok &= all(checks)
                marks = "".join("+" if c else "-" for c in checks)
                line.append(f"{lab} n={s['n']} Σ={s['sum']:+.1f} exp={s['exp']:+.3f} "
                            f"DD={s['dd']}" + ("" if name.startswith("база") else f" [{marks}]"))
            verdict = "" if name.startswith("база") else (" → ПРИНЯТ" if ok else " → не принят")
            print(f"  {name:14s} | " + " | ".join(line) + (verdict if data == "5y" else ""))


if __name__ == "__main__":
    main()
