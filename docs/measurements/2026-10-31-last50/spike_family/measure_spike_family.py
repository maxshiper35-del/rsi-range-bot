"""Замер: семейство «монета во всплеске внимания» — T10 / C48 / P30L (регистрация PREREG.md).

  cd test_spike_family
  DATA_ROOT="/Users/mac/bot ctypto/data" PYTHONPATH=../tree PYTHONDONTWRITEBYTECODE=1 \
  DB_PATH=signals_copy.db nice -n 15 "/Users/mac/bot ctypto/.venv/bin/python" \
      measure_spike_family.py --procs 4 --out run.pkl

Обвязка — scripts/measure_exit_geometry (E1m, трейл 1.5/0.2, M2′, полная пересимуляция
портфеля); фильтр сигналов до симуляции — по образцу measure_coin_age.
Решения ≥ 2026-08-01 у вариантов не возвращаются из воркера (заморозка), кроме одного
прогона на заморозке — только если пройден весь критерий регистрации.
"""
from __future__ import annotations

import argparse
import bisect
import json
import pickle
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd

import scripts.measure_exit_geometry as eg
import scripts.measure_one_position as mop
import scripts.measure_trail_realism as mtr
import scripts.research_judge as rj
from scripts.research_aggr_clusters import is_meme

HOUR_MS = 3_600_000
CUT = pd.Timestamp("2026-08-01", tz="UTC")             # начало заморозки
FROZEN_END = pd.Timestamp("2026-10-19", tz="UTC")      # окно 19–30.10 исключено
ROT_START = pd.Timestamp("2024-10-24", tz="UTC")
CUT_MS = int(CUT.value // 1_000_000)

MAIN = ("T10", "C48", "P30L")
NEIGH = {"T10": ("T5", "T20"), "C48": ("C24", "C72"), "P30L": ("P20L", "P50L")}
ALL_VARS = ("T5", "T10", "T20", "C24", "C48", "C72", "P20L", "P30L", "P50L")
REF_5Y = ("T10", "P30L")
HALVES_ROT = ("rot H1", "rot H2'")
HALVES_5Y = ("W1", "W2")
EG_REF = {"W1": (1026, -0.077), "W2": (872, -0.063), "rot H1": (1300, -0.165),
          "rot H2": (1449, -0.015)}


def parse(var: str) -> tuple:
    fam = var[0]
    if fam == "T":
        return fam, float(var[1:])
    if fam == "C":
        return fam, float(var[1:])
    return fam, float(var[1:-1]) / 100.0                # P30L -> 0.30


# ---------------------------------------------------------------- признаки (без исходов)
def admissions(first_day_counts: bool = False) -> dict:
    """{символ: [мс 00:00 UTC дней допуска]}: есть в списке D, нет в списке D−1.
    Первый день списка (2024-10-24) — допуск только при first_day_counts (справочно)."""
    raw = json.loads((mop.UNIV / "membership_a.json").read_text())
    mem = {pd.Timestamp(d, tz="UTC"): set(v) for d, v in raw.items()}
    days = sorted(mem)
    adm: dict = {}
    for i, d in enumerate(days):
        if i == 0:
            if not first_day_counts:
                continue
            new = mem[d]
        else:
            if days[i - 1] != d - pd.Timedelta(days=1):
                raise RuntimeError(f"дыра в membership_a перед {d}")
            new = mem[d] - mem[days[i - 1]]
        for s in new:
            adm.setdefault(s, []).append(int(d.value // 1_000_000))
    return adm


def features(data: str, bars: dict, sigs: list, adm: dict | None) -> dict:
    """{ключ сигнала: (rho, rho_cov, h_adm, pump7, side)}; t = dt + 1ч."""
    h1 = {sym: df for (sym, tf), df in bars.items() if tf == "1h"}
    arr: dict = {}
    out = {}
    for s in sigs:
        sym = s["symbol"]
        if sym not in arr:
            df = h1[sym]
            ts = df["ts"].to_numpy(np.int64)
            cl = df["close"].to_numpy(float)
            to = cl * df["volume"].to_numpy(float)
            arr[sym] = (ts, to, cl)
        ts, to, cl = arr[sym]
        t_ms = int((rj.utc(s["dt"]) + rj.HOUR).value // 1_000_000)
        k = int(np.searchsorted(ts, t_ms, "left"))      # бары с открытием < t
        rho = rho_cov = np.nan
        if k >= 24 * 31:
            s0 = float(to[k - 24:k].sum())
            blocks = [float(to[k - 24 * (j + 1):k - 24 * j].sum()) for j in range(1, 31)]
            med = float(np.median(blocks))
            if med > 0:
                rho = s0 / med
            if k >= 24 * 32:                             # как triage/coverage.py (справочно)
                bc = [float(to[k - 24 * (j + 1):k - 24 * j].sum()) for j in range(2, 32)]
                mc = float(np.median(bc))
                if mc > 0:
                    rho_cov = s0 / mc
        k0 = int(np.searchsorted(ts, t_ms - 168 * HOUR_MS, "left"))
        pump = float(cl[k - 1] / cl[k0 - 1] - 1.0) if (k >= 1 and k0 >= 1) else np.nan
        h_adm = np.inf
        if adm is not None:
            a = adm.get(sym, [])
            i = bisect.bisect_right(a, t_ms)
            if i:
                h_adm = (t_ms - a[i - 1]) / HOUR_MS
        out[rj.key(sym, s["dt"], s["side"], s["setup"])] = (rho, rho_cov, h_adm, pump,
                                                              s["side"])
    return out


def flagged(var: str, f: tuple, rho_idx: int = 0) -> bool:
    fam, val = parse(var)
    rho, h_adm, pump, side = f[rho_idx], f[2], f[3], f[4]
    if fam == "T":
        return bool(not np.isnan(rho) and rho >= val)
    if fam == "C":
        return bool(h_adm < val)
    return bool(side == "LONG" and not np.isnan(pump) and pump >= val)


def drop_set(var: str, feats: dict) -> frozenset:
    return frozenset(k for k, f in feats.items() if flagged(var, f))


# ---------------------------------------------------------------- прогон (воркер)
def run_job(job: tuple) -> tuple:
    """(data, имя, порядок, режим, ключи снятых сигналов, вернуть заморозку) ->
    (id, строки, MISS, сделки rj в 1h-режиме). Строка: (решение мс, ключ, выход мс,
    gross R, net R, событие, символ, сторона)."""
    data, var, order, mode, drop, keep_frozen = job
    bars, sigs = rj.load(data)
    if drop:
        sigs = [s for s in sigs
                if rj.key(s["symbol"], s["dt"], s["side"], s["setup"]) not in drop]
    eg._M1.clear()
    eg._END.clear()
    for (sym, tf), df in bars.items():
        if tf == "1h":
            eg._END[sym] = int(pd.Timestamp(df["dt"].iloc[-1]).value // 1_000_000) + eg.HOUR
    eg.DIAG.clear()
    eg.MISS.update(nofill=0, unresolved=0)
    rj.install(variant=eg.BASE, order=order, mode=mode)
    rj._pf._live_outcome = eg.judge_exit
    try:
        rep = rj.simulate(bars, sigs)
    finally:
        rj.uninstall()
    jid = (data, var, order, mode, keep_frozen)
    if mode == "1h":
        return jid, None, dict(eg.MISS), rj.trades(rep)
    rows = []
    for t in rep.detail:
        fill_ms = int(rj.utc(t.fill_dt).value // 1_000_000)
        if fill_ms < eg.W_START_MS:
            continue
        dec_ms = int((rj.utc(t.signal_dt) + rj.HOUR).value // 1_000_000)
        if var != "base" and not keep_frozen and dec_ms >= CUT_MS:
            continue                                   # заморозка не покидает воркер
        k = rj.key(t.symbol, t.signal_dt, t.side, t.setup)
        ev, rg, _c, _ex = eg.DIAG.get(k, (None, None, None, None))
        rows.append((dec_ms, (data, *k), int(rj.utc(t.exit_dt).value // 1_000_000), rg,
                     float(t.result_r), ev, t.symbol, t.side))
    return jid, rows, dict(eg.MISS), None


def eg_job(data: str) -> tuple:
    """Оригинальная обвязка exit-geometry, база 1.5/0.2, цвет, 1m (паритет 5–6)."""
    return data, eg.run_variant((data, eg.BASE, "color", "1m"))


def base_job(data: str) -> tuple:
    return data, mtr.base(data)


# ---------------------------------------------------------------- статистика
def half_new(data: str, dec_ms: int) -> str | None:
    dec = pd.Timestamp(dec_ms, unit="ms", tz="UTC")
    if dec >= CUT:
        return None
    if data == "5y":
        return "W1" if dec < mop.SPLIT5Y else "W2"
    return "rot H1" if dec < mop.SPLIT2Y else "rot H2'"


def in_half(rows: list, half: str) -> list:
    return [r for r in rows if half_new(r[1][0], r[0]) == half]


def st(rows: list) -> dict:
    return mtr.stats([(r[2], r[3], r[4], r[5]) for r in rows])


def mean(xs) -> float:
    xs = list(xs)
    return float(np.mean(xs)) if xs else float("nan")


def p95(base_rows: list, n: int) -> float:
    srt = sorted(base_rows, key=lambda r: (r[2], r[1]))
    return mop._p95([(None, None, r[4]) for r in srt], n)


def e_wo_top5(rows: list) -> float:
    rs = sorted((r[4] for r in rows), reverse=True)
    return mean(rs[5:])


def bybit(sym: str) -> str:
    return sym.split("/")[0] + "USDT"


def e_wo_meme(rows: list) -> float:
    return mean(r[4] for r in rows if not is_meme(bybit(r[6])))


def fmt(s: dict) -> str:
    if not s.get("n"):
        return "n=0"
    return f"n={s['n']:4d} E={s['net']:+.4f} Σ={s['sum']:+7.1f} DD={s['dd']:6.1f}"


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--out", default="run.pkl")
    args = ap.parse_args()
    procs = min(args.procs, 4)
    t0 = time.time()

    def lap(msg: str) -> None:
        print(f"[{time.time() - t0:7.0f}s] {msg}", flush=True)

    # ---- Паритет 1: база оригинальным путём и реплика; 1h-паритет «только 1h»
    lap("== Паритет 1: база оригинальным путём = реплика; 1h-паритет")
    with Pool(2, maxtasksperchild=1) as pool:
        got = dict(pool.map(base_job, ("5y", "rot")))
    tabs, origs = [], {}
    for data in ("5y", "rot"):
        df, same, orig = got[data]
        origs[data] = orig
        print(f"  {data}: реплика = оригинал: {same}; сделок с филлом ≥ W_START: {len(df)}")
        if not same:
            print("СТОП: реплика")
            return
        tabs.append(df)
    if not rj.check_parity(origs):
        print("СТОП: паритет базы (1h)")
        return
    trades = pd.concat(tabs, ignore_index=True)

    # ---- Паритет 2: парный E1m «реализма трейла»
    lap("== Паритет 2: парный E1m «реализма трейла»")
    cov = mtr.coverage_ok(trades)
    trades["cov1m"] = trades["key"].map(cov)
    with Pool(2, maxtasksperchild=1) as pool:
        got2 = pool.map(mtr.run_model, [(d, "1m", "E", "color", trades[trades["data"] == d])
                                        for d in ("5y", "rot")])
    e1m = {}
    for _j, out in got2:
        e1m.update(out)
    ok2 = True
    for h in mtr.HALVES:
        part = trades[(trades["half"] == h) & (trades["cov1m"] >= 0.95)]
        vals = [e1m[k][2] for k in part["key"] if e1m[k][1] is not None]
        v = float(np.mean(vals))
        good = round(v, 3) == eg.REF_PAIRED[h]
        ok2 &= good
        print(f"  {h}: n={len(vals)} E1m net/сд {v:+.4f} (эталон {eg.REF_PAIRED[h]:+.3f}) "
              f"{'OK' if good else 'РАСХОЖДЕНИЕ'}")
    if not ok2:
        print("СТОП: парный E1m не воспроизведён")
        return

    # ---- признаки и наборы снятых сигналов (без исходов)
    lap("== Признаки сигналов")
    adm = admissions()
    adm_lit = admissions(first_day_counts=True)
    feats, feats_lit, drops = {}, {}, {}
    for data in ("5y", "rot"):
        bars, sigs = rj.load(data)
        feats[data] = features(data, bars, sigs, adm if data == "rot" else None)
        if data == "rot":
            feats_lit[data] = features(data, bars, sigs, adm_lit)
        for v in ALL_VARS:
            if data == "5y" and v.startswith("C"):
                continue
            drops[(data, v)] = drop_set(v, feats[data])
        und_t = sum(np.isnan(f[0]) for f in feats[data].values())
        und_p = sum(np.isnan(f[3]) for f in feats[data].values())
        print(f"  {data}: сигналов 1h {len(sigs)}; T не определён {und_t}, P не определён "
              f"{und_p}; снимается: " + ", ".join(
                  f"{v} {len(drops[(data, v)])}" for v in ALL_VARS if (data, v) in drops))
        del bars, sigs

    # ---- Паритет 3–6 и прогон вариантов
    lap("== Паритет 3 (1h-режим) + база exit-geometry + все варианты (E1m, цвет)")
    jobs = [("eg", d) for d in ("5y", "rot")]
    jobs += [("run", (d, "base", "color", "1h", frozenset(), False)) for d in ("5y", "rot")]
    jobs += [("run", (d, "base", "color", "1m", frozenset(), False)) for d in ("5y", "rot")]
    jobs += [("run", ("rot", v, "color", "1m", drops[("rot", v)], False)) for v in ALL_VARS]
    jobs += [("run", ("5y", v, "color", "1m", drops[("5y", v)], False)) for v in REF_5Y]
    res_eg, res = {}, {}
    with Pool(procs, maxtasksperchild=1) as pool:
        futs = [(kind, pool.apply_async(eg_job if kind == "eg" else run_job, (arg,)))
                for kind, arg in jobs]
        for kind, f in futs:
            out = f.get()
            if kind == "eg":
                res_eg[out[0]] = out[1]
                lap(f"  eg база {out[0]}: сделок {len(out[1][1])}, MISS {out[1][2]}")
            else:
                jid, rows, miss, tr = out
                res[jid] = (rows, miss, tr)
                lap(f"  {jid[:4]}: сделок {len(rows) if rows is not None else len(tr)}, "
                    f"MISS {miss}")

    print("== Паритет 3: обвязка в 1h-режиме = база бит-в-бит")
    for data in ("5y", "rot"):
        same = res[(data, "base", "color", "1h", False)][2] == origs[data]
        print(f"  {data}: {'бит-в-бит' if same else 'РАСХОЖДЕНИЕ'}")
        if not same:
            print("СТОП: обвязка 1h")
            return

    print("== Паритет 4: минутный судья обвязки = E1m замера на общих сделках")
    eg_rows = res_eg["5y"][1] + res_eg["rot"][1]
    mine = {k: rg for _h, k, _ex, rg, _rn, _ev in eg_rows}
    common = [k for k in mine if k in e1m and e1m[k][1] is not None]
    bad = [k for k in common if round(mine[k], 4) != round(e1m[k][1], 4)]
    print(f"  общих сделок {len(common)}, расхождений gross R {len(bad)} {bad[:3]}")
    if bad:
        print("СТОП: минутный судья обвязки")
        return

    print("== Паритет 5: база E1m (полная пересимуляция) = exit-geometry")
    tab_eg = eg.by_half(eg_rows)
    ok5 = True
    for h in mtr.HALVES:
        s = tab_eg[h]
        ref_n, ref_e = EG_REF[h]
        good = s["n"] == ref_n and round(s["net"], 3) == ref_e
        ok5 &= good
        print(f"  {h}: n={s['n']} E={s['net']:+.4f} (эталон {ref_n}/{ref_e:+.3f}) "
              f"{'OK' if good else 'РАСХОЖДЕНИЕ'}")
    if not ok5:
        print("СТОП: база не воспроизводит exit-geometry")
        return

    print("== Паритет 6: моя обвязка с пустым фильтром = база exit-geometry")
    ok6 = True
    for data in ("5y", "rot"):
        a = sorted((k, ex, rg, rn, ev) for _h, k, ex, rg, rn, ev in res_eg[data][1])
        b = sorted((k, ex, rg, rn, ev) for _d, k, ex, rg, rn, ev, _s, _sd
                   in res[(data, "base", "color", "1m", False)][0])
        same = a == b
        ok6 &= same
        print(f"  {data}: {len(a)} / {len(b)} сделок, {'совпадают' if same else 'РАСХОЖДЕНИЕ'}")
    if not ok6:
        print("СТОП: обвязка фильтра")
        return

    base = {d: res[(d, "base", "color", "1m", False)][0] for d in ("5y", "rot")}
    rows = {("rot", v): res[("rot", v, "color", "1m", False)][0] for v in ALL_VARS}
    rows.update({("5y", v): res[("5y", v, "color", "1m", False)][0] for v in REF_5Y})
    for (d, v), rr in rows.items():
        assert all(r[0] < CUT_MS for r in rr), "заморозка просочилась"

    # ---- таблицы
    def table(data: str, variants, halves) -> dict:
        tab = {}
        print(f"  {'вариант':8s} {'половина':8s} {'n':>5s} {'E net':>8s} {'ΣR':>8s} "
              f"{'DD':>6s} {'ΔE':>8s} {'снято%':>7s} {'E снятых':>9s}")
        for v in ("base",) + tuple(variants):
            for h in halves:
                b = in_half(base[data], h)
                rr = b if v == "base" else in_half(rows[(data, v)], h)
                s = st(rr)
                sb = st(b)
                if v == "base":
                    rem, share, rem_e = [], 0.0, float("nan")
                else:
                    rem = [r for r in b if r[1][1:] in drops[(data, v)]]
                    share = len(rem) / len(b)
                    rem_e = mean(r[4] for r in rem)
                tab[(v, h)] = dict(s, share=share, rem_e=rem_e, rem_n=len(rem))
                print(f"  {v:8s} {h:8s} {s['n']:5d} {s['net']:+8.4f} {s['sum']:+8.1f} "
                      f"{s['dd']:6.1f} {s['net'] - sb['net']:+8.4f} {share:7.1%} "
                      f"{rem_e:+9.4f}")
        return tab

    print("\n== Ротация: E1m, цвет, M2′ (rot H2′ без решений ≥ 2026-08-01)")
    tab = table("rot", ALL_VARS, HALVES_ROT)
    print("\n== Справочно 9 монет: W1/W2")
    tab5 = table("5y", REF_5Y, HALVES_5Y)

    # ---- выбор на rot H1
    nb_h1 = tab[("base", "rot H1")]["n"]
    ok_n = [v for v in MAIN if tab[(v, "rot H1")]["n"] >= 0.5 * nb_h1]
    print(f"\n== Выбор на rot H1: n ≥ 50% базы ({0.5 * nb_h1:.0f}): {ok_n}")
    if not ok_n:
        print("  нет варианта с n ≥ 50% — семейство закрыто")
        sel = None
    else:
        sel = max(ok_n, key=lambda v: tab[(v, "rot H1")]["net"])
        print("  E на rot H1: " + ", ".join(f"{v} {tab[(v, 'rot H1')]['net']:+.4f}"
                                             for v in MAIN) + f" → выбран {sel}")

    # ---- границы порядка (база и выбранный вариант), ротация
    order_rows = {}
    if sel:
        lap("== Границы порядка внутри минуты: база и выбранный вариант (ротация)")
        ojobs = [("rot", v, o, "1m", drops[("rot", v)] if v != "base" else frozenset(), False)
                 for v in ("base", sel) for o in ("LH", "HL")]
        with Pool(procs, maxtasksperchild=1) as pool:
            for jid, rr, miss, _tr in pool.imap_unordered(run_job, ojobs):
                order_rows[(jid[1], jid[2])] = rr
                lap(f"  {jid[:4]}: сделок {len(rr)}, MISS {miss}")

    # ---- критерий и устойчивость
    verdict_ok = False
    crit, robust = {}, {}
    if sel:
        print(f"\n== Критерий для {sel} (все пункты на rot H1 И rot H2′)")
        for h in HALVES_ROT:
            b = in_half(base["rot"], h)
            v = in_half(rows[("rot", sel)], h)
            sb, sv = tab[("base", h)], tab[(sel, h)]
            pp = p95(b, min(sv["n"], sb["n"]))
            c = [sv["net"] - sb["net"] >= 0.03, sv["net"] > pp, sv["dd"] <= sb["dd"],
                 sv["n"] >= 0.5 * sb["n"], sv["rem_e"] <= sb["net"] - 0.10]
            crit[h] = c
            print(f"  {h}: ΔE {sv['net'] - sb['net']:+.4f} (≥ +0.03) | E {sv['net']:+.4f} "
                  f"vs p95 {pp:+.3f} | DD {sv['dd']:.1f} vs {sb['dd']:.1f} | n {sv['n']} vs "
                  f"{sb['n']} ({sv['n'] / sb['n']:.0%}) | E снятых {sv['rem_e']:+.4f} vs "
                  f"{sb['net'] - 0.10:+.4f} → пункты 1–5: "
                  + "".join("+" if x else "-" for x in c))
        print(f"\n== Устойчивость {sel} (обе половины ротации)")
        for h in HALVES_ROT:
            b = in_half(base["rot"], h)
            v = in_half(rows[("rot", sel)], h)
            eb = tab[("base", h)]["net"]
            nbs = [tab[(u, h)]["net"] - eb for u in NEIGH[sel]]
            ra = all(x >= 0.015 for x in nbs)
            d_top = e_wo_top5(v) - e_wo_top5(b)
            d_meme = e_wo_meme(v) - e_wo_meme(b)
            d_ord = {}
            for o in ("LH", "HL"):
                bo = in_half(order_rows[("base", o)], h)
                vo = in_half(order_rows[(sel, o)], h)
                d_ord[o] = st(vo)["net"] - st(bo)["net"]
            r = [ra, d_top >= 0.02, d_meme >= 0.02, all(x > 0 for x in d_ord.values())]
            robust[h] = r
            print(f"  {h}: a) соседи " + ", ".join(f"{u} {x:+.4f}" for u, x in
                                                 zip(NEIGH[sel], nbs))
                  + f" (≥ +0.015) | b) без топ-5 {d_top:+.4f} (≥ +0.02) | c) без мемов "
                  f"{d_meme:+.4f} (≥ +0.02) | d) LH {d_ord['LH']:+.4f}, HL {d_ord['HL']:+.4f}"
                  f" (> 0) → a–d: " + "".join("+" if x else "-" for x in r))
        verdict_ok = all(all(c) for c in crit.values()) and all(all(r) for r in robust.values())
        print(f"\n  ВЕРДИКТ {sel}: {'ВСЕ ПУНКТЫ ПРОЙДЕНЫ' if verdict_ok else 'НЕ ПРОШЁЛ'}")

    # ---- справочно: стороны, мемы/топ-5 для всех трёх, различия определений
    print("\n== Справочно: ΔE по сторонам (ротация)")
    for v in MAIN:
        for h in HALVES_ROT:
            b = in_half(base["rot"], h)
            vv = in_half(rows[("rot", v)], h)
            parts = []
            for side in ("LONG", "SHORT"):
                bs = [r for r in b if r[7] == side]
                vs = [r for r in vv if r[7] == side]
                parts.append(f"{side} n {len(vs)}/{len(bs)} ΔE "
                             f"{mean(r[4] for r in vs) - mean(r[4] for r in bs):+.4f}")
            print(f"  {v:5s} {h:8s} " + " | ".join(parts))
    print("\n== Справочно: без топ-5 и без мемов для всех трёх (ротация)")
    for v in MAIN:
        for h in HALVES_ROT:
            b = in_half(base["rot"], h)
            vv = in_half(rows[("rot", v)], h)
            nm_b = sum(1 for r in b if is_meme(bybit(r[6])))
            print(f"  {v:5s} {h:8s} без топ-5 ΔE {e_wo_top5(vv) - e_wo_top5(b):+.4f} | "
                  f"без мемов ΔE {e_wo_meme(vv) - e_wo_meme(b):+.4f} (мем-сделок базы {nm_b})")
    print("\n== Справочно: определения (доля сделок базы, которые сняло бы правило)")
    for h in HALVES_ROT:
        b = in_half(base["rot"], h)
        fl = [feats["rot"][r[1][1:]] for r in b]
        fll = [feats_lit["rot"][r[1][1:]] for r in b]
        t10c = mean(flagged("T10", f, rho_idx=1) for f in fl)
        c48l = mean(flagged("C48", f) for f in fll)
        print(f"  {h}: T10 по блокам coverage.py {t10c:.1%} (здесь "
              f"{mean(flagged('T10', f) for f in fl):.1%}); C48 с первым днём как допуском "
              f"{c48l:.1%} (здесь {mean(flagged('C48', f) for f in fl):.1%}); сделок базы "
              f"с допуском только в первый день: "
              f"{sum(flagged('C48', g) and not flagged('C48', f) for f, g in zip(fl, fll))}")
    print("  P0 из fill_after_publication: не посчитан (результата нет в last50 на момент "
          "прогона)")

    # ---- заморозка: только если пройден весь критерий
    frozen = None
    if verdict_ok:
        lap("== ЗАМОРОЗКА (один прогон): решения 2026-08-01 ≤ t < 2026-10-19, ротация")
        fjobs = [("rot", v, "color", "1m", drops[("rot", v)] if v != "base" else frozenset(),
                  True) for v in ("base", sel)]
        with Pool(2, maxtasksperchild=1) as pool:
            fr = {jid[1]: rr for jid, rr, _m, _t in pool.map(run_job, fjobs)}
        lo, hi = CUT_MS, int(FROZEN_END.value // 1_000_000)
        fb = [r for r in fr["base"] if lo <= r[0] < hi]
        fv = [r for r in fr[sel] if lo <= r[0] < hi]
        sb, sv = st(fb), st(fv)
        frozen = (sb, sv)
        print(f"  база {fmt(sb)} | {sel} {fmt(sv)} | ΔE {sv['net'] - sb['net']:+.4f} → "
              f"{'ΔE ≥ 0: ПРОШЁЛ' if sv['net'] - sb['net'] >= 0 else 'ΔE < 0: НЕ ПРОШЁЛ'}")
    else:
        print("\n== Заморозка не открывалась (критерий не пройден)")

    with open(args.out, "wb") as f:
        pickle.dump({"base": base, "rows": rows, "order_rows": order_rows, "tab": tab,
                     "tab5": tab5, "sel": sel, "crit": crit, "robust": robust,
                     "verdict_ok": verdict_ok, "frozen": frozen,
                     "drops": {k: sorted(v) for k, v in drops.items()},
                     "miss": {k: v[1] for k, v in res.items()}}, f)
    lap(f"готово → {args.out}")


if __name__ == "__main__":
    main()
