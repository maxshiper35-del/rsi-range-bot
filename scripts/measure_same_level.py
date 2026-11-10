"""Замер: склейка сигналов одного уровня в дедупе
(docs/measurements/2026-11-10-same-level-dedup.md).

  DATA_ROOT="/Users/mac/bot ctypto/data" PYTHONDONTWRITEBYTECODE=1 nice -n 15 \
      .venv/bin/python -m scripts.measure_same_level [--procs 2] [--out файл.pkl]

База — обвязка замера #15 (E1m, трейл 1.5/0.2, M2′, полная пересимуляция портфеля).
L — `simulate_portfolio(dedup_level_r=0.0)`: активная позиция той же монеты и стороны с тем же
входом и стопом блокирует новый сигнал любого сетапа и ТФ. S (справочно) — тот же стоп, вход
ближе 0.1R. Решения ≥ 2026-08-01 не покидают воркер (заморозка).
"""
from __future__ import annotations

import argparse
import pickle
import time
from multiprocessing import Pool

import pandas as pd

import scripts.measure_exit_geometry as eg
import scripts.measure_market_own as mmo
import scripts.measure_trail_realism as mtr
import scripts.research_judge as rj
from app.brain.journal import same_level

VARIANTS = {"base": None, "L": 0.0, "S": 0.1}
MAX_DROP_E = 0.01


def run(job: tuple) -> tuple:
    """(data, вариант) -> (data, вариант, строки сделок учебного участка, MISS, снято дедупом)."""
    data, var = job
    bars, sigs = rj.load(data)
    by_key = {rj.key(s["symbol"], s["dt"], s["side"], s["setup"]): s for s in sigs}
    eg._M1.clear()
    eg._END.clear()
    for (sym, tf), df in bars.items():
        if tf == "1h":
            eg._END[sym] = int(pd.Timestamp(df["dt"].iloc[-1]).value // 1_000_000) + mmo.HOUR
    eg.DIAG.clear()
    eg.MISS.update(nofill=0, unresolved=0)
    rj.install(variant=eg.BASE, order="color", mode="1m")
    rj._pf._live_outcome = eg.judge_exit
    try:
        rep = rj.simulate(bars, sigs, dedup_level_r=VARIANTS[var])
    finally:
        rj.uninstall()
    rows = []
    for t in rep.detail:
        fill_ms = int(rj.utc(t.fill_dt).value // 1_000_000)
        dec_ms = int((rj.utc(t.signal_dt) + rj.HOUR).value // 1_000_000)
        if fill_ms < eg.W_START_MS or dec_ms >= mmo.CUT_MS:
            continue                                   # до минуток / заморозка не покидает воркер
        k = rj.key(t.symbol, t.signal_dt, t.side, t.setup)
        s = by_key[k]
        ev, rg, _cost, _ex = eg.DIAG[k]
        rows.append(dict(data=data, key=(data, *k), symbol=t.symbol, side=t.side, setup=t.setup,
                         dec_ms=dec_ms, sig_ms=int(rj.utc(t.signal_dt).value // 1_000_000),
                         exit_ms=int(rj.utc(t.exit_dt).value // 1_000_000),
                         entry=float(s["entry"]), stop=float(s["stop"]), g=float(rg),
                         net=float(t.result_r), ev=ev))
    return data, var, rows, dict(eg.MISS), rep.skipped_dedup


def twins(p: pd.DataFrame, tol_r: float) -> pd.Series:
    """Сделка открыта, пока активна другая сделка той же монеты и стороны того же уровня
    (окно активности — как у дедупа портфеля: от сигнала до выхода).

    ДЕФЕКТ (найден после прогона 10.11, код не правился — он дал run.log): группировка без
    набора данных, а 8 монет есть и в «9 монет», и в ротации — одна сделка из двух наборов
    считается двойником самой себя; числа двойников на ротации завышены. На вердикт не
    влияет. Исправление — добавить "data" в groupby (раздел 11 документа замера)."""
    out = pd.Series(False, index=p.index)
    for _k, g in p.sort_values("sig_ms", kind="stable").groupby(["symbol", "side"]):
        rows = list(g.itertuples())
        for i, b in enumerate(rows):
            out[b.Index] = any(a.sig_ms <= b.sig_ms < a.exit_ms
                               and same_level(a.entry, a.stop, b.entry, b.stop, tol_r)
                               for a in rows[:i])
    return out


def max_same_level(p: pd.DataFrame) -> int:
    """Максимум одновременно активных сделок одного точного уровня."""
    best = 1 if len(p) else 0
    for _k, g in p.groupby(["symbol", "side", "entry", "stop"]):
        ev = sorted([(r.sig_ms, 1) for r in g.itertuples()]
                    + [(r.exit_ms, -1) for r in g.itertuples()])
        cur = 0
        for _t, d in ev:
            cur += d
            best = max(best, cur)
    return best


def st(p: pd.DataFrame) -> dict:
    return mtr.stats(list(zip(p["exit_ms"], p["g"], p["net"], p["ev"])))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--procs", type=int, default=2)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    t0 = time.time()

    def lap(msg: str) -> None:
        print(f"[{time.time() - t0:6.0f}s] {msg}", flush=True)

    lap("== Прогон: база, L, S (9 монет, ротация), решения < 2026-08-01")
    jobs = [(d, v) for v in VARIANTS for d in ("5y", "rot")]
    res = {}
    with Pool(min(args.procs, 2), maxtasksperchild=1) as pool:
        for data, var, rows, miss, skipped in pool.imap_unordered(run, jobs):
            df = pd.DataFrame(rows)
            df["half"] = [mmo.half_of(d, ms) for d, ms in zip(df["data"], df["dec_ms"])]
            res[(data, var)] = df
            lap(f"  {data} {var}: сделок {len(df)}, MISS {miss}, "
                f"снято дедупом (вся история) {skipped}")
    tab = {v: pd.concat([res[(d, v)] for d in ("5y", "rot")], ignore_index=True) for v in VARIANTS}

    print("== Паритет 1: база = таблица #15")
    ok = True
    for h in mmo.HALVES:
        s = st(tab["base"][tab["base"]["half"] == h])
        rn, re, rs = mmo.REF[h]
        good = (s["n"] == rn and f"{s['net']:+.4f}" == f"{re:+.4f}"
                and f"{s['sum']:+.1f}" == f"{rs:+.1f}")
        ok &= good
        print(f"  {h:8s} n={s['n']} E={s['net']:+.4f} Σ={s['sum']:+.1f} "
              f"{'OK' if good else 'РАСХОЖДЕНИЕ'}")
    if not ok:
        print("СТОП: паритет базы")
        return
    print("== Паритет 2: общие сделки вариантов с базой — тот же исход")
    base_net = dict(zip(tab["base"]["key"], tab["base"]["net"]))
    for v in ("L", "S"):
        common = [(k, n) for k, n in zip(tab[v]["key"], tab[v]["net"]) if k in base_net]
        bad = sum(1 for k, n in common if abs(n - base_net[k]) > 1e-9)
        print(f"  {v}: общих {len(common)}, с другим исходом {bad}")
        if bad:
            print("СТОП: вариант меняет исход общей сделки")
            return

    b = tab["base"]
    b["twin_L"] = twins(b, 0.0)
    b["twin_S"] = twins(b, 0.1)
    print("\n== Двойники в базе и варианты (E net, ΣR, DD — E1m, M2′)")
    out = {}
    for h in mmo.HALVES:
        p = b[b["half"] == h]
        sb = st(p)
        print(f"-- {h}: база n={sb['n']} E {sb['net']:+.4f} Σ {sb['sum']:+.1f} "
              f"DD {sb['dd']:.1f} | макс. одновременно одного уровня {max_same_level(p)}")
        for v, col in (("L", "twin_L"), ("S", "twin_S")):
            tw = p[p[col]]
            sv = st(tab[v][tab[v]["half"] == h])
            out[(v, h)] = dict(sv, base=sb)
            print(f"   {v}: двойников в базе {len(tw)} ({len(tw) / len(p):.1%}), их net "
                  f"{tw['net'].mean() if len(tw) else float('nan'):+.3f}, "
                  f"Σ {tw['net'].sum():+.1f} | "
                  f"вариант n={sv['n']} E {sv['net']:+.4f} Σ {sv['sum']:+.1f} DD {sv['dd']:.1f} | "
                  f"ΔE {sv['net'] - sb['net']:+.4f} ΔΣ {sv['sum'] - sb['sum']:+.1f} "
                  f"ΔDD {sv['dd'] - sb['dd']:+.1f}")

    print("\n== Решение (раздел 5, по L)")
    c1 = all(out[("L", h)]["net"] >= out[("L", h)]["base"]["net"] - MAX_DROP_E for h in mmo.HALVES)
    dd_l = sum(out[("L", h)]["dd"] for h in mmo.HALVES)
    dd_b = sum(out[("L", h)]["base"]["dd"] for h in mmo.HALVES)
    c2 = dd_l <= dd_b
    print(f"  1) E(L) ≥ E(база) − {MAX_DROP_E} на всех 4 половинах: {'да' if c1 else 'НЕТ'}")
    print(f"  2) ΣDD(L) {dd_l:.1f} ≤ ΣDD(база) {dd_b:.1f}: {'да' if c2 else 'НЕТ'}")
    print(f"  ВЕРДИКТ: склейка {'ПРИНЯТА' if c1 and c2 else 'НЕ ПРИНЯТА'}")
    if args.out:
        with open(args.out, "wb") as f:
            pickle.dump({"tab": tab, "out": out}, f)
    lap("готово")


if __name__ == "__main__":
    main()
