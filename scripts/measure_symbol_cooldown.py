"""Замер паузы монеты+стороны после стопа (docs/measurements/2026-10-07-symbol-cooldown.md).

  .venv/bin/python -m scripts.measure_symbol_cooldown
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor

import numpy as np

from app.sim.portfolio import simulate_portfolio
from scripts.backtest_monthly import SYMS
from scripts.run_portfolio import load, parse_args

OOS_FROM = "2025-07-28"
FEE, SLIP, FUND_BPS, EXTRA = 0.055, 0.02, 0.125, 0.080
HALVES = {"TRAIN": (None, OOS_FROM), "OOS": (OOS_FROM, None)}
SIG_DIR = "data/cache5y/sig_prod_late_entry"
SIGS = {"база": 0.0, "C24": 24.0, "C48": 48.0}             # вариант -> пауза, часов
BOOT = 1000


def _run(cool_h: float, half: str | None, extra: float) -> dict:
    argv = ["--symbols", SYMS, "--tfs", "1h,4h", "--cache", "data/cache5y",
            "--signals", SIG_DIR, "--setups", "6226,LTF", "--min-stop-pct", "1.3",
            "--fee", str(FEE), "--slip", str(SLIP), "--funding", str(FUND_BPS)]
    if half:
        a, b = HALVES[half]
        argv += (["--from-dt", a] if a else []) + (["--to-dt", b] if b else [])
    bars, sigs = load(parse_args(argv))
    h1 = {sym: df for (sym, tf), df in bars.items() if tf == "1h"}
    r = simulate_portfolio(
        bars, sigs, max_open_per_side=6, cooldown_stops=3, cooldown_hours=6.0, dedup=True,
        fee_pct=FEE, slippage_pct=SLIP, funding_bps_per_bar=FUND_BPS,
        trail_activate_r=1.5, trail_distance_r=0.2, entry_fill="touch",
        live_judge_bars=h1, tp_exit=False, max_hold_hours=168.0, extra_cost_r=extra,
        symbol_cooldown_hours=cool_h)
    return {"n": r.trades, "sum": r.total_r, "exp": r.expectancy_r, "dd": r.max_drawdown_r,
            "rs": [t.result_r for t in r.detail], "sigs": len(sigs)}


def _job(job):
    name, half, extra = job
    return job, _run(SIGS[name], half, extra)


def main() -> None:
    jobs = [("база", None, 0.0)]
    jobs += [(n, h, e) for n in SIGS for h in HALVES for e in (0.0, EXTRA)]
    with ProcessPoolExecutor(max_workers=9) as pool:
        res = dict(pool.map(_job, jobs))
    chk = res[("база", None, 0.0)]
    assert chk["n"] == 5819 and abs(chk["sum"] - 358.1) < 0.5, f"бейслайн: {chk}"
    print("база воспроизводит эталон прода sig_prod_late_entry (n=5819, +358.1R)")
    rng = np.random.default_rng(20261006)
    verdict = {n: True for n in SIGS if n != "база"}
    for half in HALVES:
        b = res[("база", half, EXTRA)]
        b0 = res[("база", half, 0.0)]
        print(f"[{half}] база: сигналов={b['sigs']} n={b['n']} | M1 Σ={b['sum']:+.1f} "
              f"exp={b['exp']:+.3f} DD={b['dd']:.1f} | M0 Σ={b0['sum']:+.1f} "
              f"exp={b0['exp']:+.3f}")
        base_rs = np.asarray(b["rs"])
        for name in verdict:
            r, r0 = res[(name, half, EXTRA)], res[(name, half, 0.0)]
            k = min(r["n"], len(base_rs))
            p95 = float(np.percentile(
                [rng.choice(base_rs, size=k, replace=False).mean() for _ in range(BOOT)], 95))
            c = [r["exp"] >= b["exp"] + 0.03, r["exp"] > p95, r["n"] >= 0.3 * b["n"],
                 r["dd"] <= b["dd"]]
            verdict[name] &= all(c)
            print(f"  {name:14s} сигналов={r['sigs']} n={r['n']:4d} | M1 Σ={r['sum']:+7.1f} "
                  f"exp={r['exp']:+.3f} DD={r['dd']:6.1f} p95 случ.={p95:+.3f} | "
                  f"M0 Σ={r0['sum']:+7.1f} exp={r0['exp']:+.3f} | "
                  f"{' '.join('да' if x else 'нет' for x in c)}")
    print()
    for name, ok in verdict.items():
        print(f"ВЕРДИКТ {name}: {'ПРИНЯТЬ (предложить владельцу)' if ok else 'не принято'}")


if __name__ == "__main__":
    main()
