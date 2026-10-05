"""Замер «только 1h против 1h+4h» на паритете с живым судьёй
(docs/measurements/2026-10-05-tf-1h-only.md).

  .venv/bin/python -m scripts.measure_tf_parity
"""
from __future__ import annotations

from app.sim.portfolio import simulate_portfolio
from scripts.backtest_monthly import SYMS
from scripts.run_portfolio import load, parse_args

OOS_FROM = "2025-07-28"


def _run(tfs: str, from_dt: str | None, to_dt: str | None):
    argv = ["--symbols", SYMS, "--tfs", "1h,4h", "--cache", "data/cache5y",
            "--signals", "data/cache5y/sig_parity", "--setups", "6226,LTF",
            "--min-stop-pct", "1.3", "--fee", "0.055", "--slip", "0.02",
            "--funding", "0.125", "--entry-fill", "touch"]
    if from_dt:
        argv += ["--from-dt", from_dt]
    if to_dt:
        argv += ["--to-dt", to_dt]
    bars, sigs = load(parse_args(argv))
    keep = set(tfs.split(","))
    sigs = [s for s in sigs if s["tf"] in keep]
    h1 = {sym: df for (sym, tf), df in bars.items() if tf == "1h"}
    return simulate_portfolio(
        bars, sigs, max_open_per_side=6, cooldown_stops=3, cooldown_hours=6.0, dedup=True,
        fee_pct=0.055, slippage_pct=0.02, funding_bps_per_bar=0.125, trail_activate_r=1.5,
        trail_distance_r=0.2, entry_fill="touch", live_judge_bars=h1, tp_exit=False,
        max_hold_hours=168.0)


def _line(name, r):
    return (f"  {name:8s} n={r.trades:5d} exp={r.expectancy_r:+.3f}R ΣR={r.total_r:+8.1f} "
            f"DD={r.max_drawdown_r:6.1f}R PF={r.profit_factor:.2f}")


def main() -> None:
    full = _run("1h,4h", None, None)
    assert full.trades == 5309 and abs(full.total_r - 73.3) < 0.5, \
        f"бейслайн не совпал с backtest_monthly.json: {full.trades}/{full.total_r:.1f}"
    print("бейслайн воспроизведён (n=5309, +73.3R)")
    verdict = True
    for label, a, b in (("TRAIN", None, OOS_FROM), ("OOS", OOS_FROM, None)):
        base, var = _run("1h,4h", a, b), _run("1h", a, b)
        print(f"[{label}]\n{_line('1h+4h', base)}\n{_line('1h', var)}")
        c1 = var.total_r >= base.total_r + 10
        c2 = var.expectancy_r >= base.expectancy_r + 0.02
        c3 = var.max_drawdown_r <= base.max_drawdown_r
        print(f"  (1) ΣR +10: {c1} | (2) exp +0.02: {c2} | (3) DD не хуже: {c3}")
        verdict &= c1 and c2 and c3
    print(f"\nВЕРДИКТ: {'ПРИНЯТЬ (предложить владельцу)' if verdict else 'НЕ ПРИНЯТО'}")


if __name__ == "__main__":
    main()
