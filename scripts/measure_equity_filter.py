"""Замер: фильтр режима по кривой стратегии (docs/measurements/2026-10-27-equity-curve-filter.md).

  .venv/bin/python -m scripts.measure_equity_filter
"""
from __future__ import annotations

from multiprocessing import Pool

import numpy as np
import pandas as pd

from app.sim.portfolio import simulate_portfolio
from scripts.measure_one_position import (
    SPLIT2Y,
    SPLIT5Y,
    _load5y,
    _load_rot,
    _p95,
    _stats,
)

TFH = {"1h": 1, "4h": 4}


def _base(data: str) -> list[tuple]:
    """Сделки базы (прод-конфиг, M2′): (решение, выход, r)."""
    bars, sigs = _load5y() if data == "5y" else _load_rot()
    h1 = {sym: df for (sym, tf), df in bars.items() if tf == "1h"}
    rep = simulate_portfolio(
        bars, sigs, max_open_per_side=6, cooldown_stops=3, cooldown_hours=6.0, dedup=True,
        fee_pct=0.055, slippage_pct=0.15, funding_bps_per_bar=0.125, trail_activate_r=1.5,
        trail_distance_r=0.2, entry_fill="touch", live_judge_bars=h1, tp_exit=False,
        max_hold_hours=168.0)
    out = []
    for t in rep.detail:
        sdt = pd.Timestamp(t.signal_dt)
        sdt = sdt.tz_localize("UTC") if sdt.tzinfo is None else sdt.tz_convert("UTC")
        edt = pd.Timestamp(t.exit_dt)
        edt = edt.tz_localize("UTC") if edt.tzinfo is None else edt.tz_convert("UTC")
        out.append((sdt + pd.Timedelta(hours=TFH[t.tf]), edt, t.result_r))
    return sorted(out)


def ec_filter(trades: list[tuple], n: int) -> list[tuple]:
    """Сделки, которые EC(n) торгует: exp последних n сделок базы, закрытых к решению − 1ч,
    ≥ 0 (окно не набрано — торгуем). Окно — по ВСЕМ сделкам базы, включая отсеянные."""
    by_exit = sorted(trades, key=lambda x: x[1])
    exits = np.array([e.value for _d, e, _r in by_exit])
    rs = np.array([r for _d, _e, r in by_exit])
    kept = []
    for dec, edt, r in trades:
        k = int(np.searchsorted(exits, (dec - pd.Timedelta(hours=1)).value, side="right"))
        if k < n or rs[k - n:k].mean() >= 0:
            kept.append((dec, edt, r))
    return kept


def main() -> None:
    with Pool(2) as pool:
        base = dict(zip(("5y", "rot"), pool.map(_base, ("5y", "rot"))))
    for data, split in (("5y", SPLIT5Y), ("rot", SPLIT2Y)):
        b_all = base[data]
        print(f"== {data}, M2′: база n={len(b_all)} {_stats(b_all)}")
        for n in (50, 100):
            v_all = ec_filter(b_all, n)
            ok = True
            for half, lo in (("TRAIN/Y1", True), ("OOS/Y2", False)):
                b = [x for x in b_all if (x[0] < split) == lo]
                v = [x for x in v_all if (x[0] < split) == lo]
                sb, sv = _stats(b), _stats(v)
                p95 = _p95(b, min(sv["n"], sb["n"]))
                checks = [sv["exp"] >= sb["exp"] + 0.03, sv["exp"] > p95,
                          sv["n"] >= 0.3 * sb["n"], sv["dd"] <= sb["dd"],
                          sv["sum"] >= sb["sum"]]
                ok &= all(checks)
                marks = "".join("+" if c else "-" for c in checks)
                print(f"  EC{n} {half:8s} база n={sb['n']} Σ={sb['sum']:+.1f} "
                      f"exp={sb['exp']:+.3f} DD={sb['dd']} | EC n={sv['n']} "
                      f"Σ={sv['sum']:+.1f} exp={sv['exp']:+.3f} DD={sv['dd']} "
                      f"p95={p95:+.3f} | 1-5: {marks}")
            print(f"  EC{n} весь: {_stats(v_all)} | критерий на обеих половинах: "
                  f"{'ПРОХОДИТ' if ok else 'нет'}")


if __name__ == "__main__":
    main()
