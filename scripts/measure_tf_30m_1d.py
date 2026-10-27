"""Замер: добавка 30m или 1d к «только 1h» (docs/measurements/2026-10-27-tf-30m-1d.md).

  .venv/bin/python -m scripts.measure_tf_30m_1d
"""
from __future__ import annotations

import json
from multiprocessing import Pool
from pathlib import Path

import pandas as pd

from app.sim.portfolio import simulate_portfolio
from scripts.dump_history import cache_name
from scripts.measure_one_position import NINE, SPLIT5Y, _load5y, _stats

TF_CACHE = Path("data/cache_tf")
TFH = {"30m": 0.5, "1h": 1, "1d": 24}
W30 = (pd.Timestamp("2024-08-01", tz="UTC"), pd.Timestamp("2026-07-28", tz="UTC"))


def _utc(v) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _extra(tf: str) -> tuple[dict, list]:
    bars, sigs = {}, []
    for b in NINE:
        sym = f"{b}/USDT:USDT"
        bars[(sym, tf)] = pd.read_csv(TF_CACHE / cache_name(sym, tf), parse_dates=["dt"])
        path = TF_CACHE / f"sig_{tf}" / f"{cache_name(sym, tf)[:-4]}.json"
        sigs += [s for s in json.loads(path.read_text()) if s["tf"] == tf]
    return bars, sigs


def _run(job: tuple) -> tuple:
    name, add = job
    bars, sigs = _load5y()
    sigs = [s for s in sigs if s["tf"] == "1h"]
    if add:
        xb, xs = _extra(add)
        bars.update(xb)
        sigs += xs
    if name.startswith("30"):                          # окно, где есть 30m-данные
        sigs = [s for s in sigs if W30[0] <= _utc(s["dt"]) < W30[1]]
    h1 = {sym: df for (sym, tf), df in bars.items() if tf == "1h"}
    rep = simulate_portfolio(
        bars, sigs, max_open_per_side=6, cooldown_stops=3, cooldown_hours=6.0, dedup=True,
        fee_pct=0.055, slippage_pct=0.15, funding_bps_per_bar=0.125, trail_activate_r=1.5,
        trail_distance_r=0.2, entry_fill="touch", live_judge_bars=h1, tp_exit=False,
        max_hold_hours=168.0)
    return name, sorted((_utc(t.signal_dt) + pd.Timedelta(hours=TFH[t.tf]), _utc(t.exit_dt),
                         t.result_r, t.tf) for t in rep.detail)


def main() -> None:
    jobs = [("30-база", None), ("30-1h+30m", "30m"), ("D-база", None), ("D-1h+1d", "1d")]
    with Pool(4) as pool:
        res = dict(pool.map(_run, jobs))
    for base, var, tf in (("30-база", "30-1h+30m", "30m"), ("D-база", "D-1h+1d", "1d")):
        ok = True
        print(f"== добавка {tf}")
        for lab, lo in (("H1", True), ("H2", False)):
            b = [x[:3] for x in res[base] if (x[0] < SPLIT5Y) == lo]
            v = [x[:3] for x in res[var] if (x[0] < SPLIT5Y) == lo]
            add = [x[:3] for x in res[var] if (x[0] < SPLIT5Y) == lo and x[3] == tf]
            sb, sv, sa = _stats(b), _stats(v), _stats(add)
            checks = [sa["exp"] > 0 if sa["n"] else False, sv["sum"] >= sb["sum"],
                      sv["dd"] <= sb["dd"]]
            ok &= all(checks)
            print(f"  {lab}: 1h n={sb['n']} Σ={sb['sum']:+.1f} DD={sb['dd']} | 1h+{tf} "
                  f"n={sv['n']} Σ={sv['sum']:+.1f} DD={sv['dd']} | сделок {tf}: n={sa['n']} "
                  f"exp={sa['exp']:+.3f} Σ={sa['sum']:+.1f} | 1-3: "
                  f"{''.join('+' if c else '-' for c in checks)}")
        print(f"  добавка {tf} →", "ПРИНЯТА" if ok else "не принята")


if __name__ == "__main__":
    main()
