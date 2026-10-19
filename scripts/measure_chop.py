"""Замер фильтра «пилы» по ER Кауфмана (docs/measurements/2026-10-19-chop-filter.md).

  .venv/bin/python -m scripts.measure_chop
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

from app.brain.smc_setups import SMCEngine
from app.sim.portfolio import simulate_portfolio
from scripts.backtest_monthly import SYMS
from scripts.dump_history import cache_name
from scripts.run_portfolio import load, parse_args

OOS_FROM = "2025-07-28"
SIG_DIR = "data/cache5y/sig_prod_late_entry"
FEE, SLIP, FUND_BPS, EXTRA = 0.055, 0.02, 0.125, 0.080
HALVES = {"TRAIN": (None, OOS_FROM), "OOS": (OOS_FROM, None)}
VARIANTS = ("R1 пила рынка", "R2 пила монеты")
N_ER, WINDOW_MS, MIN_PTS, CUT = 42, 180 * 24 * 3_600_000, 500, 1 / 3
H4 = 4 * 3_600_000
BOOT = 1000


class ER:
    """ER Кауфмана по закрытым 4h-свечам и его точечный ранг за 180 суток."""

    def __init__(self, symbol: str):
        df = pd.read_csv(f"data/cache5y/{cache_name(symbol, '4h')}")
        c = df["close"].to_numpy(float)
        d = np.abs(np.diff(c, prepend=c[0]))
        cs = np.cumsum(d)
        er = np.full(len(c), np.nan)
        if len(c) > N_ER:
            path = cs[N_ER:] - cs[:-N_ER]
            net = np.abs(c[N_ER:] - c[:-N_ER])
            with np.errstate(invalid="ignore", divide="ignore"):
                er[N_ER:] = np.where(path > 0, net / path, np.nan)
        self.close_ts = df["ts"].to_numpy(np.int64) + H4
        self.er = er

    def rank(self, t: int) -> float | None:
        k = int(np.searchsorted(self.close_ts, t, side="right")) - 1
        if k < 0 or np.isnan(self.er[k]):
            return None
        lo = int(np.searchsorted(self.close_ts, t - WINDOW_MS, side="right"))
        win = self.er[lo:k + 1]
        win = win[~np.isnan(win)]
        if len(win) < MIN_PTS:
            return None
        v = self.er[k]
        return float(((win < v).mean() + (win <= v).mean()) / 2)


def _decision_ms(s: dict) -> int:
    t = pd.Timestamp(s["dt"]) + pd.Timedelta(minutes=SMCEngine.TF_MINUTES[s["tf"]])
    return int(t.value // 10**6)


def _in_half(s: dict, half: str) -> bool:
    a, b = HALVES[half]
    d = str(s["dt"])
    return (a is None or d[:len(a)] >= a) and (b is None or d[:len(b)] < b)


def _load():
    argv = ["--symbols", SYMS, "--tfs", "1h,4h", "--cache", "data/cache5y",
            "--signals", SIG_DIR, "--setups", "6226,LTF", "--min-stop-pct", "1.3",
            "--fee", str(FEE), "--slip", str(SLIP), "--funding", str(FUND_BPS)]
    return load(parse_args(argv))


def _sim(bars, sigs, extra):
    h1 = {sym: df for (sym, tf), df in bars.items() if tf == "1h"}
    return simulate_portfolio(
        bars, sigs, max_open_per_side=6, cooldown_stops=3, cooldown_hours=6.0, dedup=True,
        fee_pct=FEE, slippage_pct=SLIP, funding_bps_per_bar=FUND_BPS,
        trail_activate_r=1.5, trail_distance_r=0.2, entry_fill="touch",
        live_judge_bars=h1, tp_exit=False, max_hold_hours=168.0, extra_cost_r=extra)


def _job(job):
    variant, half, extra = job
    bars, sigs = _load()
    if variant == "check":
        r = _sim(bars, sigs, 0.0)
        return job, {"n": r.trades, "sum": r.total_r}, None
    sigs = [s for s in sigs if _in_half(s, half)]
    cover = None
    if variant != "база":
        ers: dict[str, ER] = {}
        btc = ER("BTC/USDT:USDT")
        kept, defined = [], 0
        for s in sigs:
            t = _decision_ms(s)
            if variant.startswith("R1"):
                rk = btc.rank(t)
            else:
                if s["symbol"] not in ers:
                    ers[s["symbol"]] = ER(s["symbol"])
                rk = ers[s["symbol"]].rank(t)
            defined += rk is not None
            if rk is None or rk >= CUT:
                kept.append(s)
        cover = defined / max(1, len(sigs))
        sigs = kept
    r = _sim(bars, sigs, extra)
    return job, {"n": r.trades, "sum": r.total_r, "exp": r.expectancy_r,
                 "dd": r.max_drawdown_r, "rs": [t.result_r for t in r.detail],
                 "sigs": len(sigs)}, cover


def main() -> None:
    jobs = [("check", "ALL", 0.0)]
    jobs += [(v, h, e) for v in ("база", *VARIANTS) for h in HALVES for e in (0.0, EXTRA)]
    with ProcessPoolExecutor(max_workers=9) as pool:
        res, cover = {}, {}
        for job, r, c in pool.map(_job, jobs):
            res[job], cover[job] = r, c
    chk = res[("check", "ALL", 0.0)]
    assert chk["n"] == 5819 and abs(chk["sum"] - 358.1) < 0.5, f"бейслайн: {chk}"
    print("база воспроизводит эталон прода (n=5819, +358.1R)")
    rng = np.random.default_rng(20261019)
    verdict = {v: True for v in VARIANTS}
    for half in HALVES:
        b, b0 = res[("база", half, EXTRA)], res[("база", half, 0.0)]
        print(f"[{half}] база: сигналов={b['sigs']} n={b['n']} | M1 Σ={b['sum']:+.1f} "
              f"exp={b['exp']:+.3f} DD={b['dd']:.1f} | M0 Σ={b0['sum']:+.1f} exp={b0['exp']:+.3f}")
        base_rs = np.asarray(b["rs"])
        for v in VARIANTS:
            r, r0 = res[(v, half, EXTRA)], res[(v, half, 0.0)]
            k = min(r["n"], len(base_rs))
            p95 = float(np.percentile(
                [rng.choice(base_rs, size=k, replace=False).mean() for _ in range(BOOT)], 95))
            c = [r["exp"] >= b["exp"] + 0.03, r["exp"] > p95, r["n"] >= 0.3 * b["n"],
                 r["dd"] <= b["dd"]]
            verdict[v] &= all(c)
            print(f"  {v:15s} сигналов={r['sigs']} n={r['n']:4d} | M1 Σ={r['sum']:+7.1f} "
                  f"exp={r['exp']:+.3f} DD={r['dd']:6.1f} p95 случ.={p95:+.3f} | "
                  f"M0 Σ={r0['sum']:+7.1f} exp={r0['exp']:+.3f} | "
                  f"{' '.join('да' if x else 'нет' for x in c)} | покрытие "
                  f"{cover[(v, half, EXTRA)]:.0%}")
    print()
    for v, ok in verdict.items():
        print(f"ВЕРДИКТ {v}: {'ПРИНЯТЬ (предложить владельцу)' if ok else 'не принято'}")


if __name__ == "__main__":
    main()
