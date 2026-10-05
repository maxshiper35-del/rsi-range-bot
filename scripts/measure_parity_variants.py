"""Перепроверка старых решений на паритете с живым судьёй
(docs/measurements/2026-10-05-parity-recheck.md).

  .venv/bin/python -m scripts.measure_parity_variants
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor

from app.sim.portfolio import simulate_portfolio
from scripts.backtest_monthly import SYMS
from scripts.run_portfolio import load, parse_args

OOS_FROM = "2025-07-28"
BASE = {"setups": "6226,LTF", "min_stop": 1.3, "act": 1.5, "dist": 0.2, "tfs": "1h,4h"}
VARIANTS = {
    "V1 1:1 вкл": {"setups": "6226,1:1,LTF"},
    "V2 trail 0.3": {"dist": 0.3},
    "V3 act 1.0": {"act": 1.0},
    "V4 stop 1.0": {"min_stop": 1.0},
    "V5 stop 1.6": {"min_stop": 1.6},
}


def run(cfg: dict, from_dt: str | None, to_dt: str | None) -> dict:
    argv = ["--symbols", SYMS, "--tfs", "1h,4h", "--cache", "data/cache5y",
            "--signals", "data/cache5y/sig_parity", "--setups", cfg["setups"],
            "--min-stop-pct", str(cfg["min_stop"]), "--fee", "0.055", "--slip", "0.02",
            "--funding", "0.125", "--entry-fill", "touch"]
    if from_dt:
        argv += ["--from-dt", from_dt]
    if to_dt:
        argv += ["--to-dt", to_dt]
    bars, sigs = load(parse_args(argv))
    keep = set(cfg["tfs"].split(","))
    sigs = [s for s in sigs if s["tf"] in keep]
    h1 = {sym: df for (sym, tf), df in bars.items() if tf == "1h"}
    r = simulate_portfolio(
        bars, sigs, max_open_per_side=6, cooldown_stops=3, cooldown_hours=6.0, dedup=True,
        fee_pct=0.055, slippage_pct=0.02, funding_bps_per_bar=0.125,
        trail_activate_r=cfg["act"], trail_distance_r=cfg["dist"], entry_fill="touch",
        live_judge_bars=h1, tp_exit=False, max_hold_hours=168.0)
    return {"n": r.trades, "exp": r.expectancy_r, "sum": r.total_r, "dd": r.max_drawdown_r}


def _job(args):
    name, cfg, a, b = args
    return name, a, b, run(cfg, a, b)


def main() -> None:
    halves = [(None, OOS_FROM), (OOS_FROM, None)]
    jobs = [("base", BASE, None, None)]
    jobs += [("base", BASE, a, b) for a, b in halves]
    jobs += [(n, {**BASE, **v}, a, b) for n, v in VARIANTS.items() for a, b in halves]
    with ProcessPoolExecutor(max_workers=12) as pool:
        res = {(n, a, b): r for n, a, b, r in pool.map(_job, jobs)}
    full = res[("base", None, None)]
    assert full["n"] == 5309 and abs(full["sum"] - 73.3) < 0.5, f"бейслайн: {full}"
    print("бейслайн воспроизведён (n=5309, +73.3R)")
    for name in VARIANTS:
        ok = True
        for label, (a, z) in (("TRAIN", halves[0]), ("OOS", halves[1])):
            b, v = res[("base", a, z)], res[(name, a, z)]
            c1, c2, c3 = (v["sum"] >= b["sum"] + 10, v["exp"] >= b["exp"] + 0.02,
                          v["dd"] <= b["dd"])
            ok &= c1 and c2 and c3
            print(f"{name:13s} {label:5s} база n={b['n']:4d} Σ={b['sum']:+7.1f} "
                  f"exp={b['exp']:+.3f} DD={b['dd']:5.1f} | вар n={v['n']:4d} "
                  f"Σ={v['sum']:+7.1f} exp={v['exp']:+.3f} DD={v['dd']:5.1f} | {c1} {c2} {c3}")
        print(f"  ВЕРДИКТ {name}: {'ПРИНЯТЬ' if ok else 'статус-кво'}")


if __name__ == "__main__":
    main()
