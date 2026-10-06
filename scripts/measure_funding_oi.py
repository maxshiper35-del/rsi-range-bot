"""Замер фандинга/OI как фильтра (docs/measurements/2026-10-06-funding-oi-filter.md).

  .venv/bin/python -m scripts.dump_derivs        # сначала кэш
  .venv/bin/python -m scripts.measure_funding_oi
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from app.brain.smc_setups import SMCEngine
from app.sim.portfolio import simulate_portfolio
from scripts.backtest_monthly import SIG_DIR, SYMS, check_baseline
from scripts.run_portfolio import load, parse_args

OOS_FROM = "2025-07-28"
DERIVS = Path("data/cache5y/derivs")
HOUR = 3_600_000
WINDOW = 90 * 24 * HOUR
FEE, SLIP, FUND_BPS, EXTRA = 0.055, 0.02, 0.125, 0.080
HALVES = {"TRAIN": (None, OOS_FROM), "OOS": (OOS_FROM, None)}
VARIANTS = ("F1 против перегрева", "F2 только против толпы", "F3 набор против входа")
BOOT = 1000


def _rank(window: np.ndarray, v: float) -> float:
    """Средний процентильный ранг v в окне: (доля < v + доля <= v) / 2."""
    return float(((window < v).mean() + (window <= v).mean()) / 2)


def _name(symbol: str) -> str:
    return symbol.replace("/", "-").replace(":", "-")


class Features:
    """Признаки на момент решения T по кэшу derivs и 1h-барам одного символа."""

    def __init__(self, symbol: str, h1: pd.DataFrame):
        f = pd.read_csv(DERIVS / f"{_name(symbol)}_funding.csv")
        hours = (f.ts.diff() / HOUR).fillna(8.0).clip(lower=1.0)
        self.f_ts = f.ts.to_numpy(np.int64)
        self.f_rate = (f.rate / hours).to_numpy(float)
        o = pd.read_csv(DERIVS / f"{_name(symbol)}_oi1h.csv")
        grid = pd.Series(o.oi.to_numpy(float), index=o.ts.to_numpy(np.int64))
        if len(grid):
            full = np.arange(grid.index[0], grid.index[-1] + HOUR, HOUR, dtype=np.int64)
            grid = grid.reindex(full)
        chg = grid / grid.shift(24) - 1.0
        self.o_ts = chg.index.to_numpy(np.int64)
        self.o_chg = chg.to_numpy(float)
        open_ms = (h1.dt - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(milliseconds=1)
        closes = pd.Series(h1.close.to_numpy(float), index=open_ms.to_numpy(np.int64))
        self.px_open = closes.index.to_numpy(np.int64)  # открытие бара, мс
        self.px_close = closes.to_numpy(float)

    def fund_pct(self, t: int) -> float | None:
        i = int(np.searchsorted(self.f_ts, t, side="right")) - 1
        if i < 0:
            return None
        lo = int(np.searchsorted(self.f_ts, t - WINDOW, side="right"))
        win = self.f_rate[lo:i + 1]
        return _rank(win, self.f_rate[i]) if len(win) >= 60 else None

    def oi_pct(self, t: int) -> float | None:
        t1 = t - HOUR
        i = int(np.searchsorted(self.o_ts, t1, side="right")) - 1
        if i < 0 or t1 - self.o_ts[i] > 3 * HOUR or np.isnan(self.o_chg[i]):
            return None
        lo = int(np.searchsorted(self.o_ts, t - WINDOW, side="right"))
        win = self.o_chg[lo:i + 1]
        win = win[~np.isnan(win)]
        return _rank(win, self.o_chg[i]) if len(win) >= 500 else None

    def px_chg(self, t: int) -> float | None:
        # 1h-бар, закрывшийся к t1 = T − 1ч: открытие <= T − 2ч; и бар на 24 раньше.
        i = int(np.searchsorted(self.px_open, t - 2 * HOUR, side="right")) - 1
        if i < 24:
            return None
        return float(self.px_close[i] / self.px_close[i - 24] - 1.0)


def _keep(variant: str, side: str, fp: float | None, op: float | None,
          px: float | None) -> bool:
    long_ = side == "LONG"
    if variant.startswith("F1"):
        return fp is None or not (fp >= 0.8 if long_ else fp <= 0.2)
    if variant.startswith("F2"):
        return fp is None or (fp <= 0.5 if long_ else fp >= 0.5)
    if variant.startswith("F3"):
        if op is None or px is None:
            return True
        against = px < 0 if long_ else px > 0
        return not (op >= 0.8 and against)
    raise ValueError(variant)


def _load():
    argv = ["--symbols", SYMS, "--tfs", "1h,4h", "--cache", "data/cache5y",
            "--signals", SIG_DIR, "--setups", "6226,LTF",
            "--min-stop-pct", "1.3", "--fee", str(FEE), "--slip", str(SLIP),
            "--funding", str(FUND_BPS)]
    return load(parse_args(argv))


def _annotate(bars: dict, sigs: list) -> dict:
    """{id(sig): (fund_pct, oi_pct, px_chg)}; печатает покрытие."""
    feats: dict[str, Features] = {}
    out = {}
    for s in sigs:
        sym = s["symbol"]
        if sym not in feats:
            feats[sym] = Features(sym, bars[(sym, "1h")])
        ft = feats[sym]
        t_dec = pd.Timestamp(s["dt"]) + pd.Timedelta(minutes=SMCEngine.TF_MINUTES[s["tf"]])
        t = int(t_dec.value // 10**6)
        out[id(s)] = (ft.fund_pct(t), ft.oi_pct(t), ft.px_chg(t))
    return out


def _in_half(s: dict, half: str) -> bool:
    a, b = HALVES[half]
    d = str(s["dt"])
    return (a is None or d[:len(a)] >= a) and (b is None or d[:len(b)] < b)


def _sim(bars: dict, sigs: list, extra: float):
    h1 = {sym: df for (sym, tf), df in bars.items() if tf == "1h"}
    return simulate_portfolio(
        bars, sigs, max_open_per_side=6, cooldown_stops=3, cooldown_hours=6.0, dedup=True,
        fee_pct=FEE, slippage_pct=SLIP, funding_bps_per_bar=FUND_BPS,
        trail_activate_r=1.5, trail_distance_r=0.2, entry_fill="touch",
        live_judge_bars=h1, tp_exit=False, max_hold_hours=168.0, extra_cost_r=extra)


def _job(job: tuple[str, str]):
    variant, half = job
    bars, sigs = _load()
    if variant == "check":
        r = _sim(bars, sigs, 0.0)
        return job, {"n": r.trades, "sum": r.total_r}, None
    sigs = [s for s in sigs if _in_half(s, half)]
    if variant != "base":
        ann = _annotate(bars, sigs)
        cover = np.mean([a[0] is not None for a in ann.values()]), \
            np.mean([a[1] is not None for a in ann.values()])
        sigs = [s for s in sigs if _keep(variant, s["side"], *ann[id(s)])]
    else:
        cover = None
    r = _sim(bars, sigs, EXTRA)
    rs = [t.result_r for t in r.detail]
    return job, {"n": r.trades, "sum": r.total_r, "exp": r.expectancy_r,
                 "dd": r.max_drawdown_r, "rs": rs, "sigs": len(sigs)}, cover


def main() -> None:
    jobs = [("check", "ALL")] + [(v, h) for v in ("base", *VARIANTS) for h in HALVES]
    with ProcessPoolExecutor(max_workers=9) as pool:
        res, cover = {}, {}
        for job, r, c in pool.map(_job, jobs):
            res[job], cover[job] = r, c
    chk = res[("check", "ALL")]
    print("база без доп. издержки:", check_baseline(chk["n"], chk["sum"]))
    rng = np.random.default_rng(20261006)
    verdicts = {}
    for half in HALVES:
        b = res[("base", half)]
        print(f"[{half}] база M1: сигналов={b['sigs']} n={b['n']} Σ={b['sum']:+.1f} "
              f"exp={b['exp']:+.3f} DD={b['dd']:.1f}")
        base_rs = np.asarray(b["rs"])
        for v in VARIANTS:
            r = res[(v, half)]
            k = min(r["n"], len(base_rs))
            boot = [rng.choice(base_rs, size=k, replace=False).mean() for _ in range(BOOT)]
            p95 = float(np.percentile(boot, 95))
            c = [r["exp"] >= b["exp"] + 0.03, r["exp"] > p95,
                 r["n"] >= 0.3 * b["n"], r["dd"] <= b["dd"]]
            verdicts.setdefault(v, []).append((all(c), r["exp"] > 0))
            fc, oc = cover[(v, half)]
            print(f"  {v:24s} сигналов={r['sigs']} n={r['n']:4d} Σ={r['sum']:+7.1f} "
                  f"exp={r['exp']:+.3f} DD={r['dd']:6.1f} | p95 случ.={p95:+.3f} | "
                  f"{' '.join('да' if x else 'нет' for x in c)} | покрытие фанд "
                  f"{fc:.0%} OI {oc:.0%}")
    print()
    for v, vs in verdicts.items():
        ok = all(p for p, _ in vs)
        edge = all(e for _, e in vs)
        print(f"ВЕРДИКТ {v}: {'ПРИНЯТЬ в тень' if ok else 'не принято'}"
              f"{' | край с издержками > 0 на обеих половинах' if ok and edge else ''}")


if __name__ == "__main__":
    main()
