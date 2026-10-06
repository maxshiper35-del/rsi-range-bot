"""Замер модели входа на паритете (docs/measurements/2026-10-05-entry-model.md).

  .venv/bin/python -m scripts.measure_entry_model
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor

from app.sim.portfolio import simulate_portfolio
from scripts.backtest_monthly import SIG_DIR, SYMS, check_baseline
from scripts.run_portfolio import load, parse_args

OOS_FROM = "2025-07-28"
MODELS = {
    "M0 маркет-модель": dict(fee=0.055, slip=0.02, fill="touch", frac=0.0, extra=0.0),
    "M1 маркет-реальность": dict(fee=0.055, slip=0.02, fill="touch", frac=0.0, extra=0.080),
    "L5 лимитка 5%": dict(fee=0.0375, slip=0.01, fill="through", frac=0.05, extra=0.0),
    "L10 лимитка 10%": dict(fee=0.0375, slip=0.01, fill="through", frac=0.10, extra=0.0),
}


def run(m: dict, a: str | None, b: str | None) -> dict:
    argv = ["--symbols", SYMS, "--tfs", "1h,4h", "--cache", "data/cache5y",
            "--signals", SIG_DIR, "--setups", "6226,LTF",
            "--min-stop-pct", "1.3", "--fee", str(m["fee"]), "--slip", str(m["slip"]),
            "--funding", "0.125"]
    if a:
        argv += ["--from-dt", a]
    if b:
        argv += ["--to-dt", b]
    bars, sigs = load(parse_args(argv))
    h1 = {sym: df for (sym, tf), df in bars.items() if tf == "1h"}
    r = simulate_portfolio(
        bars, sigs, max_open_per_side=6, cooldown_stops=3, cooldown_hours=6.0, dedup=True,
        fee_pct=m["fee"], slippage_pct=m["slip"], funding_bps_per_bar=0.125,
        trail_activate_r=1.5, trail_distance_r=0.2, entry_fill=m["fill"],
        through_frac=m["frac"] or 0.05, live_judge_bars=h1, tp_exit=False,
        max_hold_hours=168.0, extra_cost_r=m["extra"])
    return {"n": r.trades, "exp": r.expectancy_r, "sum": r.total_r, "dd": r.max_drawdown_r}


def _job(args):
    name, a, b = args
    return name, a, b, run(MODELS[name], a, b)


def main() -> None:
    halves = [(None, OOS_FROM), (OOS_FROM, None)]
    jobs = [("M0 маркет-модель", None, None)]
    jobs += [(n, a, b) for n in MODELS for a, b in halves]
    with ProcessPoolExecutor(max_workers=9) as pool:
        res = {(n, a, b): r for n, a, b, r in pool.map(_job, jobs)}
    full = res[("M0 маркет-модель", None, None)]
    print("M0:", check_baseline(full["n"], full["sum"]))
    for (n, a, b), r in sorted(res.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        if a is None and b is None:
            continue
        label = "TRAIN" if a is None else "OOS"
        print(f"{n:22s} {label:5s} n={r['n']:4d} Σ={r['sum']:+7.1f} exp={r['exp']:+.3f} "
              f"DD={r['dd']:5.1f}")
    for n in ("L5 лимитка 5%", "L10 лимитка 10%"):
        ok = True
        for (a, b) in halves:
            base, v = res[("M1 маркет-реальность", a, b)], res[(n, a, b)]
            ok &= (v["sum"] >= base["sum"] + 10 and v["exp"] >= base["exp"] + 0.02
                   and v["dd"] <= base["dd"])
        print(f"ВЕРДИКТ {n}: {'ПРИНЯТЬ (лимит-вход)' if ok else 'не принято'}")


if __name__ == "__main__":
    main()
