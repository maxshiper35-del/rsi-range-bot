"""Замер: геометрия выхода на минутках — сетка трейла и фиксированные цели
(docs/measurements/2026-10-31-exit-geometry.md).

  DATA_ROOT=/путь/к/data .venv/bin/python -m scripts.measure_exit_geometry --fetch
  DATA_ROOT=/путь/к/data .venv/bin/python -m scripts.measure_exit_geometry [--procs 3] [--out f.pkl]

1. Паритет: база оригинальным путём и реплика; парный E1m замера «реализм трейла»
   (−0.065/−0.057/−0.180/−0.004); обвязка в 1h-режиме = база бит-в-бит; минутный судья
   обвязки = E1m замера на общих сделках.
2. Полная пересимуляция портфеля для каждого варианта выхода: сделки с филлом ≥ 2024-07-31
   судятся по минуткам Bybit (модель E — трейл с момента филла), раньше — живым судьёй на 1h.
3. Выбор лучшего варианта в каждом семействе — только на W1; проверка — W2, rot H1, rot H2.
"""
from __future__ import annotations

import argparse
import pickle
from multiprocessing import Pool

import numpy as np
import pandas as pd

import scripts.fetch_m1 as m1
import scripts.measure_one_position as mop
import scripts.measure_trail_realism as mtr
import scripts.research_fine_judge as fj
import scripts.research_judge as rj
from app.brain.journal import MAX_WAIT_HOURS
from app.brain.smc_setups import SMCEngine
from app.sim.backtest import _cost_r

ROOT = rj.rebind_data()
MIN, HOUR = 60_000, 3_600_000
W_START_MS = int(mtr.W_START.value // 1_000_000)
HALVES = mtr.HALVES
CHECK = ("W2", "rot H1", "rot H2")
ACTS = (1.0, 1.5, 2.0, 3.0)
DISTS = (0.2, 0.3, 0.5, 0.75, 1.0)
TARGETS = ("1R", "2R", "sig", "3R")        # по возрастанию R: соседи для устойчивости
TRAIL = [("trail", a, d) for a in ACTS for d in DISTS]
FIXED = [("target", t) for t in TARGETS]
BASE = ("trail", 1.5, 0.2)
REF_PAIRED = {"W1": -0.065, "W2": -0.057, "rot H1": -0.180, "rot H2": -0.004}
MIN_N_FRAC = 0.6        # выбор на W1: n варианта ≥ 60% n базы E1m (не крошечная выборка)
DD_MULT = 1.5

_M1: dict = {}
_END: dict = {}         # символ -> конец 1h-кэша (мс): минутки дальше не используются
DIAG: dict = {}         # ключ сигнала -> (событие, gross R, издержки, выход мс)
MISS = {"nofill": 0, "unresolved": 0}


def name(v: tuple) -> str:
    return f"трейл {v[1]:g}/{v[2]:g}" if v[0] == "trail" else f"цель {v[1]}"


def target_path(entry: float, stop: float, side: str, fill_open_ms: int, bars: np.ndarray,
                target_r: float, order: str = "color", max_hold_h: float = 168.0) -> tuple:
    """Выход по фиксированной цели без трейла — путь цены как у research_fine_judge._path:
    филл — первое касание входа в часе филла журнала, стоп −1R и цель +target_r по уровню,
    потолок — close последнего бара часа филл + 168ч. -> (event, r_gross, exit_bar_ms),
    event ∈ stopped | target | timeout | nofill | unresolved."""
    risk = abs(entry - stop)
    sgn = 1.0 if side == "LONG" else -1.0
    ts = bars[:, 0]
    n = len(bars)
    i0 = int(np.searchsorted(ts, fill_open_ms, "left"))
    E = sgn * entry
    T = E + target_r * risk
    level = sgn * stop
    end_ms = fill_open_ms + int((max_hold_h + 1) * HOUR)
    fill_end = fill_open_ms + HOUR
    filled = False
    prev = last_close = None
    for i in range(i0, n):
        t = int(ts[i])
        if t >= end_ms:
            if filled and last_close is not None:
                return "timeout", round((last_close - E) / risk, 4), int(ts[i - 1])
            break
        if not filled and t >= fill_end:
            return "nofill", None, None
        pts = [sgn * p for p in fj._points(bars[i, 1], bars[i, 2], bars[i, 3], bars[i, 4],
                                           order)]
        seq = ([prev] if prev is not None else []) + pts
        for k in range(len(seq) - 1):
            a, b = seq[k], seq[k + 1]
            if not filled:
                if min(a, b) <= E <= max(a, b):
                    filled = True
                    a = E
                else:
                    continue
            if b < a and b <= level:
                return "stopped", round((level - E) / risk, 4), t
            if b > a and b >= T:
                return "target", round((T - E) / risk, 4), t
        prev = last_close = pts[-1]
        if filled and t + MIN >= end_ms:
            return "timeout", round((last_close - E) / risk, 4), t
    return "unresolved", None, None


def _bars(symbol: str) -> np.ndarray:
    if symbol not in _M1:
        a = m1.load(symbol)
        end = _END.get(symbol)
        if end is not None and len(a):
            a = a[a[:, 0] < end]
        _M1[symbol] = a
    return _M1[symbol]


def _fill_bar(sig: dict, dts: np.ndarray, hi: np.ndarray, lo: np.ndarray,
              max_hold_hours: float):
    """Бар филла 1h — как у живого судья (rj.judge). -> индекс | ('expired', dt) | None."""
    created = rj.utc(sig["dt"])
    decision = created + pd.Timedelta(minutes=SMCEngine.TF_MINUTES.get(sig["tf"], 60))
    dec64 = decision.to_datetime64()
    start = int(np.searchsorted(dts, (decision - rj.HOUR).to_datetime64(), "left"))
    span = int(MAX_WAIT_HOURS + (max_hold_hours if max_hold_hours > 0 else 24 * 40)) + 8
    end = min(start + span, len(dts))
    i0 = max(start, int(np.searchsorted(dts, dec64, "left")))
    if i0 >= end:
        return None
    lim = dec64 + np.timedelta64(int(MAX_WAIT_HOURS * 3600), "s")
    k_exp = i0 + int(np.searchsorted(dts[i0:end], lim, "left"))
    touch = np.nonzero((lo[i0:k_exp] <= sig["entry"]) & (sig["entry"] <= hi[i0:k_exp]))[0]
    if not touch.size:
        if k_exp < end:
            return "expired", decision + pd.Timedelta(hours=MAX_WAIT_HOURS)
        return None
    return i0 + int(touch[0])


def judge_exit(sig: dict, df1h: pd.DataFrame, idx1h=None, **kw):
    """Контракт app.sim.portfolio._live_outcome. Филл — бар 1h живого судьи; выход сделок с
    филлом ≥ W_START — по минуткам (вариант из rj.OPTS), раньше — живой судья на 1h."""
    opts = rj.OPTS
    if opts.get("mode") == "1h":
        return rj.judge(sig, df1h, idx1h, **kw)
    entry, stop, side = sig["entry"], sig["stop"], sig["side"]
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    dts, _op, hi, lo, _cl = rj.arrays(df1h)
    f = _fill_bar(sig, dts, hi, lo, kw["max_hold_hours"])
    if f is None or isinstance(f, tuple):
        return None if f is None else ("expired", None, None, f[1])
    fill_ms = int(pd.Timestamp(dts[f]).value // 1_000_000)
    if fill_ms < W_START_MS:
        return rj.judge(sig, df1h, idx1h, **kw)
    bars = _bars(sig["symbol"])
    var, order = opts["variant"], opts.get("order", "color")
    if var[0] == "trail":
        ev, rg, ex = fj.judge(entry, stop, side, fill_ms, bars, bar_ms=MIN, mode="path",
                              immediate=True, order=order, act_r=var[1], dist_r=var[2],
                              max_hold_h=kw["max_hold_hours"])
    else:
        t_r = {"1R": 1.0, "2R": 2.0, "3R": 3.0}.get(var[1])
        if t_r is None:                       # цель сигнала (rr 2.32 или 3.0)
            t_r = abs(float(sig["tp"]) - entry) / risk
        ev, rg, ex = target_path(entry, stop, side, fill_ms, bars, t_r, order,
                                 kw["max_hold_hours"])
    if rg is None:
        MISS[ev] += 1
        return None
    held = int((ex - fill_ms) // HOUR)
    cost = (_cost_r(entry, stop, kw["fee_pct"], kw["slippage_pct"])
            + kw["funding_bps_per_bar"] / 10000.0 * entry / risk * held)
    DIAG[rj.key(sig["symbol"], sig["dt"], side, sig["setup"])] = (ev, rg, cost, ex)
    kind = "target" if ev == "target" else f"{ev}-stop"
    return kind, rg - cost, rj._ts(dts[f]), pd.Timestamp(ex, unit="ms", tz="UTC")


def half_of(data: str, decision: pd.Timestamp) -> str:
    if data == "5y":
        return "W1" if decision < mop.SPLIT5Y else "W2"
    return "rot H1" if decision < mop.SPLIT2Y else "rot H2"


def run_variant(job: tuple) -> tuple:
    """(data, вариант, порядок, режим) -> (job, строки сделок, MISS, сделки rj)."""
    data, variant, order, mode = job
    bars, sigs = rj.load(data)
    _M1.clear()
    _END.clear()
    for (sym, tf), df in bars.items():
        if tf == "1h":
            _END[sym] = int(pd.Timestamp(df["dt"].iloc[-1]).value // 1_000_000) + HOUR
    DIAG.clear()
    MISS.update(nofill=0, unresolved=0)
    rj.install(variant=variant, order=order, mode=mode)
    rj._pf._live_outcome = judge_exit
    try:
        rep = rj.simulate(bars, sigs)
    finally:
        rj.uninstall()
    rows = []
    for t in rep.detail:
        fill_ms = int(rj.utc(t.fill_dt).value // 1_000_000)
        if fill_ms < W_START_MS:
            continue
        k = rj.key(t.symbol, t.signal_dt, t.side, t.setup)
        ev, rg, _c, _ex = DIAG.get(k, (None, None, None, None))
        rows.append((half_of(data, rj.utc(t.signal_dt) + rj.HOUR), (data, *k),
                     int(rj.utc(t.exit_dt).value // 1_000_000), rg, float(t.result_r), ev))
    return job, rows, dict(MISS), (rj.trades(rep) if mode == "1h" else None)


def fetch() -> None:
    """Минутки на [бар филла 1h; +170ч) для КАЖДОГО сигнала, который исполнился бы (касание
    1h-судьи), с филлом ≥ W_START — хватит любому варианту и любому составу портфеля."""
    win: dict = {}
    for data in ("5y", "rot"):
        bars, sigs = rj.load(data)
        h1 = {sym: df for (sym, tf), df in bars.items() if tf == "1h"}
        for s in sigs:
            dts, _op, hi, lo, _cl = rj.arrays(h1[s["symbol"]])
            f = _fill_bar(s, dts, hi, lo, 168.0)
            if f is None or isinstance(f, tuple):
                continue
            f_ms = int(pd.Timestamp(dts[f]).value // 1_000_000)
            if f_ms >= W_START_MS:
                win.setdefault(s["symbol"], []).append((f_ms, f_ms + 170 * HOUR))
    print(f"символов {len(win)}, окон {sum(len(v) for v in win.values())}", flush=True)
    m1.ensure(win, threads=2)


def stats(rows: list) -> dict:
    return mtr.stats([(ex, rg, rn, ev) for _h, _k, ex, rg, rn, ev in rows])


def by_half(rows: list) -> dict:
    return {h: stats([r for r in rows if r[0] == h]) for h in HALVES}


def neighbours(v: tuple) -> list:
    if v[0] == "trail":
        ia, idd = ACTS.index(v[1]), DISTS.index(v[2])
        out = []
        for da, dd in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            a, d = ia + da, idd + dd
            if 0 <= a < len(ACTS) and 0 <= d < len(DISTS):
                out.append(("trail", ACTS[a], DISTS[d]))
        return out
    i = TARGETS.index(v[1])
    return [("target", TARGETS[j]) for j in (i - 1, i + 1) if 0 <= j < len(TARGETS)]


def pick(family: list, tab: dict) -> tuple:
    """Лучший по net/сд на W1 при n ≥ MIN_N_FRAC · n базы на W1."""
    n_min = MIN_N_FRAC * tab[BASE]["W1"]["n"]
    ok = [v for v in family if tab[v]["W1"]["n"] >= n_min]
    return max(ok, key=lambda v: tab[v]["W1"]["net"])


def table(tab: dict, variants: list) -> None:
    print(f"  {'вариант':14s} " + " ".join(f"{h:>30s}" for h in HALVES))
    for v in variants:
        cells = []
        for h in HALVES:
            s = tab[v][h]
            cells.append(f"n={s['n']:4d} g{s['gross']:+.3f} n{s['net']:+.3f} DD{s['dd']:5.1f}")
        print(f"  {name(v):14s} " + " ".join(f"{c:>30s}" for c in cells))


def verdict(v: tuple, tab: dict, tabs_order: dict) -> bool:
    print(f"\n== Кандидат {name(v)} (выбран на W1)")
    c1 = all(tab[v][h]["net"] > 0 for h in CHECK)
    print("  1) net/сд > 0 на проверочных: " + ", ".join(
        f"{h} {tab[v][h]['net']:+.3f}" for h in CHECK) + f" → {'да' if c1 else 'НЕТ'}")
    nb = neighbours(v)
    c2 = all(tab[u][h]["net"] > 0 for u in nb for h in ("W1", "W2"))
    print("  2) соседи в плюсе на W1 и W2: " + "; ".join(
        f"{name(u)}: W1 {tab[u]['W1']['net']:+.3f}, W2 {tab[u]['W2']['net']:+.3f}" for u in nb)
        + f" → {'да' if c2 else 'НЕТ'}")
    c3 = all(tab[v][h]["dd"] <= DD_MULT * tab[BASE][h]["dd"] for h in HALVES)
    print("  3) DD ≤ 1.5 × DD базы E1m: " + ", ".join(
        f"{h} {tab[v][h]['dd']:.1f}/{tab[BASE][h]['dd']:.1f}" for h in HALVES)
        + f" → {'да' if c3 else 'НЕТ'}")
    for order, t in tabs_order.items():
        ok = all(t[h]["net"] > 0 for h in CHECK)
        print(f"  порядок {order}: " + ", ".join(f"{h} {t[h]['net']:+.3f}" for h in CHECK)
              + ("" if ok == c1 else "  [ФЛАГ: пункт 1 чувствителен к порядку внутри минуты]"))
    ok = c1 and c2 and c3
    print(f"  ВЕРДИКТ {name(v)}: {'КАНДИДАТ' if ok else 'не прошёл'}")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--procs", type=int, default=3)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    if args.fetch:
        fetch()
        return

    print("== Паритет 0–1: база оригинальным путём, реплика сделка-к-сделке", flush=True)
    tabs, origs = [], {}
    for data in ("5y", "rot"):
        df, same, orig = mtr.base(data)
        origs[data] = orig
        print(f"  {data}: реплика = оригинал: {same}; сделок с филлом ≥ W_START: {len(df)}")
        if not same:
            print("СТОП: реплика")
            return
        tabs.append(df)
    if not rj.check_parity(origs):
        print("СТОП: паритет базы")
        return
    trades = pd.concat(tabs, ignore_index=True)

    print("== Паритет 2: парный E1m замера «реализм трейла»", flush=True)
    cov = mtr.coverage_ok(trades)
    trades["cov1m"] = trades["key"].map(cov)
    with Pool(2, maxtasksperchild=1) as pool:
        got = pool.map(mtr.run_model, [(d, "1m", "E", "color", trades[trades["data"] == d])
                                       for d in ("5y", "rot")])
    e1m = {}
    for _j, out in got:
        e1m.update(out)
    ok2 = True
    for h in HALVES:
        part = trades[(trades["half"] == h) & (trades["cov1m"] >= 0.95)]
        vals = [e1m[k][2] for k in part["key"] if e1m[k][1] is not None]
        v = float(np.mean(vals))
        good = round(v, 3) == REF_PAIRED[h]
        ok2 &= good
        print(f"  {h}: n={len(vals)} E1m net/сд {v:+.4f} (эталон {REF_PAIRED[h]:+.3f}) "
              f"{'OK' if good else 'РАСХОЖДЕНИЕ'}")
    if not ok2:
        print("СТОП: парный E1m не воспроизведён")
        return

    print("== Паритет 3: обвязка в 1h-режиме = база бит-в-бит", flush=True)
    with Pool(2, maxtasksperchild=1) as pool:
        got = pool.map(run_variant, [(d, BASE, "color", "1h") for d in ("5y", "rot")])
    for (data, *_rest), _rows, _miss, tr in got:
        same = tr == origs[data]
        print(f"  {data}: {'бит-в-бит' if same else 'РАСХОЖДЕНИЕ'}")
        if not same:
            print("СТОП: обвязка")
            return

    print("== Прогон вариантов (полная пересимуляция, E1m, порядок по цвету свечи)", flush=True)
    jobs = [(d, v, "color", "1m") for v in TRAIL + FIXED for d in ("5y", "rot")]
    rows: dict = {}
    miss: dict = {}
    with Pool(args.procs, maxtasksperchild=1) as pool:
        for i, ((data, v, _o, _m), rr, ms, _tr) in enumerate(
                pool.imap_unordered(run_variant, jobs), 1):
            rows.setdefault(v, []).extend(rr)
            miss[(data, v)] = ms
            print(f"  {i}/{len(jobs)} {data} {name(v)}: сделок {len(rr)}, не разрешились {ms}",
                  flush=True)
    tab = {v: by_half(r) for v, r in rows.items()}

    print("== Паритет 4: минутный судья обвязки = E1m замера на общих сделках", flush=True)
    mine = {k: rg for _h, k, _ex, rg, _rn, _ev in rows[BASE]}
    common = [k for k in mine if k in e1m and e1m[k][1] is not None]
    bad = [k for k in common if round(mine[k], 4) != round(e1m[k][1], 4)]
    print(f"  общих сделок {len(common)}, расхождений gross R {len(bad)} {bad[:3]}")
    if bad:
        print("СТОП: минутный судья обвязки")
        return

    print("\n== Таблицы (net/сд после издержек M2′; DD — по кумулятивному net R)")
    print("-- семейство A: трейл (активация/отступ, R)")
    table(tab, TRAIL)
    print("-- семейство B: фиксированные цели")
    table(tab, FIXED)

    winners = [pick(TRAIL, tab), pick(FIXED, tab)]
    order_jobs = [(d, v, o, "1m") for v in winners for o in ("LH", "HL") for d in ("5y", "rot")]
    with Pool(args.procs, maxtasksperchild=1) as pool:
        got = pool.map(run_variant, order_jobs)
    by_order: dict = {}
    for (_d, v, o, _m), rr, _ms, _tr in got:
        by_order.setdefault((v, o), []).extend(rr)
    results = {}
    for v in winners:
        results[v] = verdict(v, tab, {o: by_half(by_order[(v, o)]) for o in ("LH", "HL")})
    if args.out:
        with open(args.out, "wb") as f:
            pickle.dump({"tab": tab, "rows": rows, "miss": miss, "winners": winners,
                         "results": results, "by_order": by_order}, f)


if __name__ == "__main__":
    main()
