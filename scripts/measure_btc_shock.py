"""Замер: снимать входы при шоке BTC (docs/measurements/2026-10-29-btc-shock-filter.md).

  .venv/bin/python -m scripts.measure_btc_shock
"""
from __future__ import annotations

from multiprocessing import Pool

import numpy as np
import pandas as pd

from app.brain.journal import MAX_WAIT_HOURS
from app.sim.portfolio import simulate_portfolio
from scripts.dump_history import cache_name
from scripts.measure_one_position import CACHE5Y, SPLIT2Y, SPLIT5Y, _load5y, _load_rot, _stats
from scripts.measure_rotating_universe import OUT as UNIV

SHOCK = 0.015                       # ход BTC за закрытый 1h-бар против стороны (не перебираем)
BTC = "BTC/USDT:USDT"
HOUR = pd.Timedelta(hours=1)
VARIANTS = ("база (1h)", "S1 до филла", "S2 вкл. бар филла")


def _utc(v) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _key(symbol: str, dt, side: str, setup: str) -> tuple:
    return symbol, _utc(dt).isoformat(), side, setup


def _arrays(df: pd.DataFrame) -> tuple:
    dts = pd.to_datetime(df["dt"], utc=True).to_numpy(dtype="datetime64[ns]")
    return dts, df["low"].to_numpy(float), df["high"].to_numpy(float)


def _fill_open(sig: dict, arr: tuple):
    """Открытие бара филла по контракту судьи: первое касание low<=entry<=high в барах,
    открывшихся в [решение, решение + 50ч); None — входа не было."""
    dts, lo, hi = arr
    decision = (_utc(sig["dt"]) + HOUR).to_datetime64()
    end = (_utc(sig["dt"]) + HOUR + pd.Timedelta(hours=MAX_WAIT_HOURS)).to_datetime64()
    i0, i1 = np.searchsorted(dts, decision, "left"), np.searchsorted(dts, end, "left")
    e = sig["entry"]
    hit = np.nonzero((lo[i0:i1] <= e) & (e <= hi[i0:i1]))[0]
    return dts[i0 + hit[0]] if hit.size else None


def _cancelled(sigs: list, bars: dict, btc: pd.DataFrame, inclusive: bool) -> set:
    bdt = pd.to_datetime(btc["dt"], utc=True).to_numpy(dtype="datetime64[ns]")
    ret = btc["close"].to_numpy(float) / btc["open"].to_numpy(float) - 1.0
    against = {"LONG": ret <= -SHOCK, "SHORT": ret >= SHOCK}
    arrs = {sym: _arrays(df) for (sym, tf), df in bars.items() if tf == "1h"}
    out = set()
    for s in sigs:
        fill = _fill_open(s, arrs[s["symbol"]])
        if fill is None:
            continue
        decision = (_utc(s["dt"]) + HOUR).to_datetime64()
        i0 = np.searchsorted(bdt, decision, "left")
        i1 = np.searchsorted(bdt, fill, "right" if inclusive else "left")
        if against[s["side"]][i0:i1].any():
            out.add(_key(s["symbol"], s["dt"], s["side"], s["setup"]))
    return out


def _run(job: tuple) -> tuple:
    data, name = job
    bars, sigs = _load5y() if data == "5y" else _load_rot()
    sigs = [s for s in sigs if s["tf"] == "1h"]
    cut: set = set()
    if name != VARIANTS[0]:
        btc = pd.read_csv((CACHE5Y if data == "5y" else UNIV) / cache_name(BTC, "1h"),
                          parse_dates=["dt"])
        cut = _cancelled(sigs, bars, btc, inclusive=name.startswith("S2"))
        sigs = [s for s in sigs if _key(s["symbol"], s["dt"], s["side"], s["setup"]) not in cut]
    h1 = {sym: df for (sym, tf), df in bars.items() if tf == "1h"}
    rep = simulate_portfolio(
        bars, sigs, max_open_per_side=6, cooldown_stops=3, cooldown_hours=6.0, dedup=True,
        fee_pct=0.055, slippage_pct=0.15, funding_bps_per_bar=0.125, trail_activate_r=1.5,
        trail_distance_r=0.2, entry_fill="touch", live_judge_bars=h1, tp_exit=False,
        max_hold_hours=168.0)
    tr = sorted((_utc(t.signal_dt) + HOUR, _utc(t.exit_dt), t.result_r,
                 _key(t.symbol, t.signal_dt, t.side, t.setup)) for t in rep.detail)
    return data, name, tr, cut


def main() -> None:
    jobs = [(d, n) for d in ("5y", "rot") for n in VARIANTS]
    with Pool(6) as pool:
        res = {(d, n): (tr, cut) for d, n, tr, cut in pool.map(_run, jobs)}
    verdict = {n: True for n in VARIANTS[1:]}
    for data, split in (("5y", SPLIT5Y), ("rot", SPLIT2Y)):
        print(f"== {data}")
        base_tr = res[(data, VARIANTS[0])][0]
        for name in VARIANTS:
            tr, cut = res[(data, name)]
            parts = []
            for lab, lo in (("H1", True), ("H2", False)):
                b = [x for x in base_tr if (x[0] < split) == lo]
                v = [x for x in tr if (x[0] < split) == lo]
                sb, sv = _stats([x[:3] for x in b]), _stats([x[:3] for x in v])
                if name == VARIANTS[0]:
                    parts.append(f"{lab} n={sb['n']} Σ={sb['sum']:+.1f} exp={sb['exp']:+.3f} "
                                 f"DD={sb['dd']}")
                    continue
                removed = [x[2] for x in b if x[3] in cut]
                mean_rm = sum(removed) / len(removed) if removed else 0.0
                checks = [bool(removed) and mean_rm < 0, sv["sum"] >= sb["sum"],
                          sv["dd"] <= sb["dd"]]
                verdict[name] &= all(checks)
                parts.append(f"{lab} n={sv['n']} Σ={sv['sum']:+.1f} exp={sv['exp']:+.3f} "
                             f"DD={sv['dd']} | снято из базы {len(removed)} ср {mean_rm:+.2f}R "
                             f"[{''.join('+' if c else '-' for c in checks)}]")
            extra = "" if name == VARIANTS[0] else f" (сигналов снято всего: {len(cut)})"
            print(f"  {name:18s}{extra}\n    " + "\n    ".join(parts))
    for name, ok in verdict.items():
        print(f"{name} → {'КАНДИДАТ' if ok else 'не принят'}")


if __name__ == "__main__":
    main()
