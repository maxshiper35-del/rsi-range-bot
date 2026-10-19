"""Замер min_stop на реалистичных издержках (docs/measurements/2026-10-20-min-stop-real-costs.md).

  .venv/bin/python -m scripts.measure_min_stop_costs
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor

from app.sim.portfolio import simulate_portfolio
from scripts.backtest_monthly import SYMS
from scripts.run_portfolio import load, parse_args

OOS_FROM = "2025-07-28"
FEE, FUND_BPS = 0.055, 0.125
HALVES = {"TRAIN": (None, OOS_FROM), "OOS": (OOS_FROM, None)}
VARIANTS = {"база 1.3": ("data/cache5y/sig_prod_late_entry", 1.3),
            "S2 2.0": ("data/cache5y/sig_prod_late_entry_stop20", 2.0),
            "S3 3.0": ("data/cache5y/sig_prod_late_entry_stop30", 3.0)}
MODELS = {"M2": 0.25, "M0": 0.02, "M2'": 0.15}     # суммарный слип на круг, % цены


def _run(name: str, half: str | None, slip: float) -> dict:
    sig_dir, min_stop = VARIANTS[name]
    argv = ["--symbols", SYMS, "--tfs", "1h,4h", "--cache", "data/cache5y",
            "--signals", sig_dir, "--setups", "6226,LTF", "--min-stop-pct", str(min_stop),
            "--fee", str(FEE), "--slip", str(slip), "--funding", str(FUND_BPS)]
    if half:
        a, b = HALVES[half]
        argv += (["--from-dt", a] if a else []) + (["--to-dt", b] if b else [])
    bars, sigs = load(parse_args(argv))
    h1 = {sym: df for (sym, tf), df in bars.items() if tf == "1h"}
    r = simulate_portfolio(
        bars, sigs, max_open_per_side=6, cooldown_stops=3, cooldown_hours=6.0, dedup=True,
        fee_pct=FEE, slippage_pct=slip, funding_bps_per_bar=FUND_BPS,
        trail_activate_r=1.5, trail_distance_r=0.2, entry_fill="touch",
        live_judge_bars=h1, tp_exit=False, max_hold_hours=168.0)
    return {"n": r.trades, "sum": r.total_r, "exp": r.expectancy_r, "dd": r.max_drawdown_r}


def _job(job):
    name, half, model = job
    return job, _run(name, half, MODELS[model])


def main() -> None:
    jobs = [("база 1.3", None, "M0")]
    jobs += [(n, h, m) for n in VARIANTS for h in HALVES for m in MODELS]
    with ProcessPoolExecutor(max_workers=9) as pool:
        res = dict(pool.map(_job, jobs))
    chk = res[("база 1.3", None, "M0")]
    assert chk["n"] == 5819 and abs(chk["sum"] - 358.1) < 0.5, f"бейслайн: {chk}"
    print("база воспроизводит эталон прода (n=5819, +358.1R при M0)")
    verdict = {n: True for n in VARIANTS if n != "база 1.3"}
    for half in HALVES:
        b = res[("база 1.3", half, "M2")]
        print(f"[{half}]")
        for name in VARIANTS:
            line = " | ".join(
                f"{m} Σ={res[(name, half, m)]['sum']:+7.1f} exp={res[(name, half, m)]['exp']:+.3f}"
                f" DD={res[(name, half, m)]['dd']:5.1f}" for m in MODELS)
            r = res[(name, half, "M2")]
            mark = ""
            if name in verdict:
                c = [r["sum"] >= b["sum"] + 10, r["exp"] >= b["exp"] + 0.02, r["dd"] <= b["dd"]]
                verdict[name] &= all(c)
                mark = " | M2-критерий: " + " ".join("да" if x else "нет" for x in c)
            print(f"  {name:9s} n={r['n']:4d} | {line}{mark}")
    print()
    for name, ok in verdict.items():
        print(f"ВЕРДИКТ {name}: {'ПРИНЯТЬ (предложить владельцу)' if ok else 'не принято'}")


if __name__ == "__main__":
    main()
