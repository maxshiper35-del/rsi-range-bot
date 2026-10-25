"""Замер: одна позиция на монету+сторону (docs/measurements/2026-10-25-one-position-per-coin.md).

  .venv/bin/python -m scripts.measure_one_position

Главные данные — 5 лет, 9 монет (снимок прода sig_prod_late_entry, LTF-BB@4h в тени);
подтверждение — 2-летний ротационный вотчлист A (measure_rotating_universe).
"""
from __future__ import annotations

import json
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

from app.sim.portfolio import simulate_portfolio
from scripts.dump_history import cache_name
from scripts.measure_rotating_universe import OUT as UNIV
from scripts.measure_rotating_universe import members_signals

CACHE5Y, SIG5Y = Path("data/cache5y"), Path("data/cache5y/sig_prod_late_entry")
NINE = "BTC,ETH,SOL,XRP,1000PEPE,NEAR,ONDO,HYPE,ZEC".split(",")
SPLIT5Y = pd.Timestamp("2025-07-28", tz="UTC")
SPLIT2Y = pd.Timestamp("2025-10-24", tz="UTC")
SHADOW = {("LTF-BB", "4h")}


def _load5y() -> tuple[dict, list]:
    bars, sigs = {}, []
    for b in NINE:
        sym = f"{b}/USDT:USDT"
        for tf in ("1h", "4h"):
            bars[(sym, tf)] = pd.read_csv(CACHE5Y / cache_name(sym, tf), parse_dates=["dt"])
            path = SIG5Y / f"{cache_name(sym, tf)[:-4]}.json"
            sigs += [s for s in json.loads(path.read_text()) if (s["setup"], tf) not in SHADOW]
    return bars, sigs


def _load_rot() -> tuple[dict, list]:
    mem = {pd.Timestamp(d, tz="UTC"): v
           for d, v in json.loads((UNIV / "membership_a.json").read_text()).items()}
    sigs = members_signals(mem)
    bars = {}
    for sym in sorted({s["symbol"] for s in sigs}):
        for tf in ("1h", "4h"):
            bars[(sym, tf)] = pd.read_csv(UNIV / cache_name(sym, tf), parse_dates=["dt"])
    return bars, sigs


def _sim(job: tuple) -> tuple:
    data, scope, slip = job
    bars, sigs = _load5y() if data == "5y" else _load_rot()
    h1 = {sym: df for (sym, tf), df in bars.items() if tf == "1h"}
    rep = simulate_portfolio(
        bars, sigs, max_open_per_side=6, cooldown_stops=3, cooldown_hours=6.0, dedup=True,
        fee_pct=0.055, slippage_pct=slip, funding_bps_per_bar=0.125, trail_activate_r=1.5,
        trail_distance_r=0.2, entry_fill="touch", live_judge_bars=h1, tp_exit=False,
        max_hold_hours=168.0, dedup_scope=scope)
    tr = [(pd.Timestamp(t.signal_dt), pd.Timestamp(t.exit_dt), t.result_r)
          for t in rep.detail]
    return job, tr, rep.skipped_dedup


def _utc(ts: pd.Timestamp) -> pd.Timestamp:
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _stats(tr: list) -> dict:
    tr = sorted(tr, key=lambda x: x[1])
    rs = [r for _s, _e, r in tr]
    peak = cum = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {"n": len(rs), "sum": round(sum(rs), 1),
            "exp": round(sum(rs) / len(rs), 3) if rs else None, "dd": round(dd, 1)}


def _halves(tr: list, split: pd.Timestamp) -> tuple[list, list]:
    return ([x for x in tr if _utc(x[0]) < split], [x for x in tr if _utc(x[0]) >= split])


def _p95(base: list, n: int, seed: int = 0) -> float:
    rs = np.array([r for _s, _e, r in base])
    rng = np.random.default_rng(seed)
    return round(float(np.percentile([rng.choice(rs, n, replace=False).mean()
                                      for _ in range(1000)], 95)), 3)


def main() -> None:
    jobs = [(d, sc, sl) for d in ("5y", "rot") for sc in ("setup", "symbol_side")
            for sl in (0.15, 0.02)]
    with Pool(6) as pool:
        res = {job: (tr, sk) for job, tr, sk in pool.map(_sim, jobs)}
    for data, split in (("5y", SPLIT5Y), ("rot", SPLIT2Y)):
        for slip, model in ((0.15, "M2'"), (0.02, "M0")):
            base, _ = res[(data, "setup", slip)]
            var, skipped = res[(data, "symbol_side", slip)]
            print(f"== {data} {model} (дедуп пропустил в S1: {skipped})")
            ok_all = True
            for half, (b, v) in zip(("TRAIN/Y1", "OOS/Y2"),
                                    zip(_halves(base, split), _halves(var, split))):
                sb, sv = _stats(b), _stats(v)
                p95 = _p95(b, min(sv["n"], sb["n"]))
                checks = [sv["exp"] >= sb["exp"] + 0.03, sv["exp"] > p95,
                          sv["n"] >= 0.3 * sb["n"], sv["dd"] <= sb["dd"], sv["sum"] >= sb["sum"]]
                ok_all &= all(checks)
                marks = "".join("+" if c else "-" for c in checks)
                print(f"  {half:8s} база n={sb['n']} Σ={sb['sum']:+.1f} exp={sb['exp']:+.3f} "
                      f"DD={sb['dd']} | S1 n={sv['n']} Σ={sv['sum']:+.1f} exp={sv['exp']:+.3f} "
                      f"DD={sv['dd']} p95={p95:+.3f} | пункты 1-5: {marks}")
            sb, sv = _stats(base), _stats(var)
            print(f"  весь     база Σ={sb['sum']:+.1f} exp={sb['exp']:+.3f} DD={sb['dd']} | "
                  f"S1 Σ={sv['sum']:+.1f} exp={sv['exp']:+.3f} DD={sv['dd']} | "
                  f"критерий на обеих половинах: {'ПРОХОДИТ' if ok_all else 'нет'}")


if __name__ == "__main__":
    main()
