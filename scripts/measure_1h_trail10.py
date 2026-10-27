"""Замер: «только 1h» и активация трейла 1.0R на M2′
(docs/measurements/2026-10-27-1h-only-trail10-m2.md).

  .venv/bin/python -m scripts.measure_1h_trail10
"""
from __future__ import annotations

from multiprocessing import Pool

import pandas as pd

from app.sim.portfolio import simulate_portfolio
from scripts.measure_one_position import SPLIT2Y, SPLIT5Y, _load5y, _load_rot, _stats

TFH = {"1h": 1, "4h": 4}
VARIANTS = {"база": {}, "V1 только 1h": {"only_1h": True}, "V2 трейл 1.0R": {"act": 1.0}}


def _run(job: tuple) -> tuple:
    data, name = job
    v = VARIANTS[name]
    bars, sigs = _load5y() if data == "5y" else _load_rot()
    if v.get("only_1h"):
        sigs = [s for s in sigs if s["tf"] == "1h"]
    h1 = {sym: df for (sym, tf), df in bars.items() if tf == "1h"}
    rep = simulate_portfolio(
        bars, sigs, max_open_per_side=6, cooldown_stops=3, cooldown_hours=6.0, dedup=True,
        fee_pct=0.055, slippage_pct=0.15, funding_bps_per_bar=0.125,
        trail_activate_r=v.get("act", 1.5), trail_distance_r=0.2, entry_fill="touch",
        live_judge_bars=h1, tp_exit=False, max_hold_hours=168.0)
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
    with Pool(3) as pool:
        res = {(d, n): tr for d, n, tr in pool.map(_run, jobs)}
    for data, split in (("5y", SPLIT5Y), ("rot", SPLIT2Y)):
        print(f"== {data}{'' if data == '5y' else ' (подтверждение, не в вердикт)'}")
        halves = {}
        for name in VARIANTS:
            tr = res[(data, name)]
            halves[name] = [_stats([x for x in tr if (x[0] < split) == lo])
                            for lo in (True, False)]
        base = halves["база"]
        for name, hs in halves.items():
            line = []
            ok = True
            for (lab, b, s) in zip(("TRAIN", "OOS"), base, hs):
                checks = [s["exp"] >= b["exp"] + 0.03, s["dd"] <= b["dd"], s["sum"] >= b["sum"]]
                ok &= all(checks)
                marks = "".join("+" if c else "-" for c in checks)
                line.append(f"{lab} n={s['n']} Σ={s['sum']:+.1f} exp={s['exp']:+.3f} "
                            f"DD={s['dd']}" + ("" if name == "база" else f" [{marks}]"))
            verdict = "" if name == "база" else (" → ПРИНЯТ" if ok else " → не принят")
            print(f"  {name:14s} | " + " | ".join(line) + (verdict if data == "5y" else ""))


if __name__ == "__main__":
    main()
