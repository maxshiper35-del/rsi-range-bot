"""Замер «филл до постановки»: сигнал недействителен, если первое касание входа по 1m — в минуте 0
(P0) / минутах 0–1 (P01) после решения, и час филла открывается в решении. Регистрация — PREREG.md.

  cd <эта папка> && DATA_ROOT="/Users/mac/bot ctypto/data" PYTHONPATH=<last50/tree> \
    PYTHONDONTWRITEBYTECODE=1 nice -n 15 "/Users/mac/bot ctypto/.venv/bin/python" \
    measure_fill_after_publication.py [--procs 4]

Обвязка — scripts/measure_exit_geometry.py без правок: run_variant(BASE, color, 1m) = E1m, M2′,
полная пересимуляция. Вариант — тот же run_variant, но список сигналов фильтруется до rj.simulate
(подмена rj.load в процессе воркера). Заморозка (решения ≥ 2026-08-01) не классифицируется и у
вариантов не считается.
"""
from __future__ import annotations

import argparse
import pickle
import sys
import time
from collections import Counter
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fap  # noqa: E402
import scripts.fetch_m1 as m1  # noqa: E402
import scripts.measure_exit_geometry as mge  # noqa: E402
import scripts.measure_trail_realism as mtr  # noqa: E402
import scripts.research_judge as rj  # noqa: E402

OUT = HERE / "out"
FREEZE = pd.Timestamp("2026-08-01", tz="UTC")
FREEZE_MS = int(FREEZE.value // 1_000_000)
HOUR_MS = 3_600_000
HALVES4 = ("W1", "W2", "rot H1", "rot H2′")
VARS = ("P0", "P01", "P012")
# таблица exit-geometry (трейл 1.5/0.2): n, gross/сд, net/сд, ΣR net, DD
PUBLISHED = {"W1": (1026, 0.071, -0.077, -78.9, 113.1), "W2": (872, 0.091, -0.063, -54.9, 68.3),
             "rot H1": (1300, -0.028, -0.165, -214.8, 222.9),
             "rot H2": (1449, 0.108, -0.015, -21.5, 46.7)}


# ----------------------------------------------------------------- классификация сигналов
def classify(data: str) -> tuple[str, dict, dict]:
    """-> (data, {ключ rj.key: (m, hole, n_min)}, счётчики). Класс считается только у сигналов,
    чей час филла живого судьи открывается в t, t ≥ W_START и t < 2026-08-01."""
    bars, sigs = rj.load(data)
    h1 = {sym: df for (sym, tf), df in bars.items() if tf == "1h"}
    end = {sym: int(pd.Timestamp(df["dt"].iloc[-1]).value // 1_000_000) + HOUR_MS
           for sym, df in h1.items()}
    cnt: Counter = Counter()
    out: dict = {}
    m1c: dict = {}
    keys = Counter(rj.key(s["symbol"], s["dt"], s["side"], s["setup"]) for s in sigs)
    cnt["dup_keys"] = sum(1 for v in keys.values() if v > 1)
    for s in sigs:
        cnt["sigs"] += 1
        t = rj.utc(s["dt"]) + pd.Timedelta(minutes=60)
        t_ms = int(t.value // 1_000_000)
        if t_ms >= FREEZE_MS:
            cnt["freeze_skip"] += 1
            continue
        dts, _op, hi, lo, _cl = rj.arrays(h1[s["symbol"]])
        f = mge._fill_bar(s, dts, hi, lo, 168.0)
        if f is None or isinstance(f, tuple):
            cnt["no_fill_1h"] += 1
            continue
        f_ms = int(pd.Timestamp(dts[f]).value // 1_000_000)
        if f_ms < mge.W_START_MS:
            cnt["fill_before_wstart"] += 1
            continue
        cnt["fill_ge_wstart"] += 1
        if f_ms != t_ms:
            cnt["fill_later_hour"] += 1
            continue
        cnt["fill_hour_at_t"] += 1
        sym = s["symbol"]
        if sym not in m1c:
            a = m1.load(sym)
            if len(a):
                a = a[a[:, 0] < end[sym]]
            m1c[sym] = a
        m, hole, n = fap.touch_minute(float(s["entry"]), t_ms, m1c[sym])
        out[rj.key(sym, s["dt"], s["side"], s["setup"])] = (m, hole, n)
        cnt["no_touch_1m" if m is None else ("m%d" % m if m <= 2 else "m3+")] += 1
        cnt["hole"] += int(hole)
        cnt["hour_incomplete"] += int(n < 60)
    return data, out, dict(cnt)


# ----------------------------------------------------------------- прогон варианта
def run_job(job: tuple) -> tuple:
    """(data, имя, frozenset ключей к удалению) -> (data, имя, строки run_variant, MISS, удалено)."""
    data, name, drop = job
    orig = rj.load
    removed = [0]

    def load_filtered(d):
        bars, sigs = orig(d)
        if not drop:
            return bars, sigs
        keep = [s for s in sigs if rj.key(s["symbol"], s["dt"], s["side"], s["setup"]) not in drop]
        removed[0] = len(sigs) - len(keep)
        return bars, keep

    rj.load = load_filtered
    t0 = time.time()
    try:
        _job, rows, miss, _tr = mge.run_variant((data, mge.BASE, "color", "1m"))
    finally:
        rj.load = orig
    return data, name, rows, miss, removed[0], round(time.time() - t0, 1)


# ----------------------------------------------------------------- статистика
def decision_of(row) -> pd.Timestamp:
    return rj.utc(row[1][2]) + pd.Timedelta(hours=1)


def half4(row) -> str | None:
    """W1 / W2 / rot H1 / rot H2′ (решение < 2026-08-01); None — заморозка."""
    h = row[0]
    if h == "rot H2":
        return "rot H2′" if decision_of(row) < FREEZE else None
    if decision_of(row) >= FREEZE:          # 5y кончается 2026-07-28 — не бывает
        return None
    return h


def st(rows: list) -> dict:
    return mge.stats(rows) if rows else {"n": 0, "gross": float("nan"), "net": float("nan"),
                                         "sum": 0.0, "dd": 0.0, "trail": float("nan")}


def split4(rows: list) -> dict:
    out = {h: [] for h in HALVES4}
    for r in rows:
        h = half4(r)
        if h is not None:
            out[h].append(r)
    return out


def drop_top(rows: list, k: int = 5) -> list:
    return sorted(rows, key=lambda r: r[4], reverse=True)[k:]


def fmt(s: dict) -> str:
    if not s["n"]:
        return "n=   0"
    return (f"n={s['n']:4d} E={s['net']:+.4f} Σ={s['sum']:+7.1f} DD={s['dd']:6.1f} "
            f"gross={s['gross']:+.4f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--procs", type=int, default=4)
    args = ap.parse_args()
    procs = min(args.procs, 4)
    OUT.mkdir(exist_ok=True)
    log = open(OUT / "report.txt", "w")

    def p(*a):
        s = " ".join(str(x) for x in a)
        print(s, flush=True)
        log.write(s + "\n")
        log.flush()

    p(f"== старт {pd.Timestamp.now(tz='UTC').isoformat(timespec='seconds')}; процессов {procs}")
    # фаза 1: классификация и база параллельно
    with Pool(procs, maxtasksperchild=1) as pool:
        cls_async = [pool.apply_async(classify, (d,)) for d in ("5y", "rot")]
        base_async = [pool.apply_async(run_job, ((d, "base", frozenset()),)) for d in ("5y", "rot")]
        classes = {}
        for a in cls_async:
            data, out, cnt = a.get()
            classes[data] = out
            p(f"  классификация {data}: {cnt}")
        base_rows, base_miss = [], {}
        for a in base_async:
            data, _n, rows, miss, _rem, sec = a.get()
            base_rows += rows
            base_miss[data] = miss
            p(f"  база {data}: сделок (филл ≥ W_START) {len(rows)}, MISS {miss}, {sec}с")
    with open(OUT / "phase1.pkl", "wb") as f:
        pickle.dump({"classes": classes, "base_rows": base_rows, "base_miss": base_miss}, f)

    # паритет базы с exit-geometry
    p("\n== Паритет базы с таблицей exit-geometry (трейл 1.5/0.2, E1m, полная пересимуляция)")
    ok = all(m == {"nofill": 0, "unresolved": 0} for m in base_miss.values())
    p(f"  MISS нулевые: {ok}")
    for h, (n, g, e, s, dd) in PUBLISHED.items():
        part = [r for r in base_rows if r[0] == h]
        x = st(part)
        good = (x["n"] == n and round(x["gross"], 3) == g and round(x["net"], 3) == e
                and round(x["sum"], 1) == s and round(x["dd"], 1) == dd)
        ok &= good
        if h == "rot H2":
            p(f"  {h} (полный, только проверка инструмента): {'совпало' if good else 'НЕ совпало'}")
        else:
            p(f"  {h}: n={x['n']} gross={x['gross']:+.3f} net={x['net']:+.3f} Σ={x['sum']:+.1f} "
              f"DD={x['dd']:.1f} (эталон {n} / {g:+.3f} / {e:+.3f} / {s:+.1f} / {dd:.1f}) "
              f"{'OK' if good else 'РАСХОЖДЕНИЕ'}")
    if not ok:
        p("СТОП: база не воспроизводит exit-geometry")
        return

    # наборы к удалению
    drops = {}
    for v in VARS:
        mx = fap.CLASSES[v]
        drops[v] = {d: frozenset(k for k, (m, _h, _n) in classes[d].items()
                                 if m is not None and m <= mx) for d in ("5y", "rot")}
        p(f"  к удалению {v}: 5y {len(drops[v]['5y'])}, rot {len(drops[v]['rot'])} сигналов")

    # фаза 2: варианты
    p("\n== Прогон вариантов (фильтр сигналов до rj.simulate)")
    jobs = [(d, v, drops[v][d]) for v in VARS for d in ("rot", "5y")]
    var_rows: dict = {v: [] for v in VARS}
    var_miss: dict = {}
    with Pool(procs, maxtasksperchild=1) as pool:
        for data, name, rows, miss, rem, sec in pool.imap_unordered(run_job, jobs):
            var_rows[name] += rows
            var_miss[(data, name)] = miss
            p(f"  {name} {data}: удалено сигналов {rem}, сделок {len(rows)}, MISS {miss}, {sec}с")
    with open(OUT / "phase2.pkl", "wb") as f:
        pickle.dump({"var_rows": var_rows, "var_miss": var_miss, "drops": drops}, f)

    report(p, classes, base_rows, var_rows)
    log.close()


def report(p, classes: dict, base_rows: list, var_rows: dict) -> None:
    def m_of(row):
        data, *k = row[1]
        c = classes[data].get(tuple(k))
        return None if c is None else c[0]

    B = split4(base_rows)
    V = {v: split4(var_rows[v]) for v in VARS}
    res: dict = {}
    p("\n== Метрики по половинам (решения < 2026-08-01; E — net R/сд E1m после M2′)")
    for h in HALVES4:
        b = B[h]
        sb = st(b)
        p(f"\n-- {h}: база {fmt(sb)}")
        res[h] = {"base": sb}
        for v in VARS:
            mx = fap.CLASSES[v]
            sel = [r for r in b if (m := m_of(r)) is not None and m <= mx]
            share = len(sel) / sb["n"] if sb["n"] else float("nan")
            mean_sel = float(np.mean([r[4] for r in sel])) if sel else float("nan")
            g_sel = float(np.mean([r[3] for r in sel])) if sel else float("nan")
            sv = st(V[v][h])
            d_e = sv["net"] - sb["net"]
            res[h][v] = {"share": share, "k": len(sel), "mean_net": mean_sel, "mean_gross": g_sel,
                         "var": sv, "dE": d_e}
            p(f"   {v:5s}: сделок базы класса {len(sel):3d} ({share:6.2%}), их net R {mean_sel:+.3f} "
              f"(gross {g_sel:+.3f}) | вариант {fmt(sv)} | ΔE {d_e:+.4f} ΔΣ "
              f"{sv['sum'] - sb['sum']:+.1f} ΔDD {sv['dd'] - sb['dd']:+.1f}")
    # критерий
    p("\n== Критерий (регистрация)")
    c1 = [h for h in HALVES4 if res[h]["P0"]["share"] >= 0.01 or abs(res[h]["P0"]["dE"]) >= 0.01]
    p("  (i) существенность: " + "; ".join(
        f"{h} доля P0 {res[h]['P0']['share']:.2%}, ΔE(P0) {res[h]['P0']['dE']:+.4f}" for h in HALVES4)
      + f" → {'ДА' if c1 else 'нет'} (половины: {', '.join(c1) or '—'})")
    c2 = [h for h in HALVES4 if res[h]["P0"]["k"] and
          res[h]["P0"]["mean_net"] <= res[h]["base"]["net"] - 0.30]
    p("  (ii) механизм: " + "; ".join(
        f"{h} net R сделок P0 {res[h]['P0']['mean_net']:+.3f} vs E(база) − 0.30 = "
        f"{res[h]['base']['net'] - 0.30:+.3f}" for h in HALVES4)
      + f" → {'ДА' if len(c2) == 4 else 'нет'} (выполнено на {len(c2)}/4)")
    p("  (iii) вердикт о стратегии не меняется: E базы и вариантов — " + ", ".join(
        f"{h} {res[h]['base']['net']:+.3f}/{res[h]['P0']['var']['net']:+.3f}/"
        f"{res[h]['P01']['var']['net']:+.3f}" for h in HALVES4))

    # устойчивость
    p("\n== Устойчивость (описательно)")
    p("  без топ-5 сделок (по net R на половине, у базы и варианта отдельно):")
    for h in HALVES4:
        sb = st(drop_top(B[h]))
        cells = []
        for v in VARS:
            sv = st(drop_top(V[v][h]))
            cells.append(f"{v} E {sv['net']:+.4f} ΔE {sv['net'] - sb['net']:+.4f}")
        p(f"   {h}: база E {sb['net']:+.4f} | " + " | ".join(cells))
    p("  без мем-монет (сделки мем-монет убраны из статистики, без пересимуляции):")
    for h in HALVES4:
        b = [r for r in B[h] if not fap.is_meme(r[1][1])]
        sb = st(b)
        cells = []
        for v in VARS:
            mx = fap.CLASSES[v]
            sel = [r for r in b if (m := m_of(r)) is not None and m <= mx]
            sv = st([r for r in V[v][h] if not fap.is_meme(r[1][1])])
            mean_sel = float(np.mean([r[4] for r in sel])) if sel else float("nan")
            cells.append(f"{v} доля {len(sel) / max(sb['n'], 1):.2%} net {mean_sel:+.3f} "
                         f"ΔE {sv['net'] - sb['net']:+.4f}")
        p(f"   {h}: база n={sb['n']} E {sb['net']:+.4f} | " + " | ".join(cells))
    p("  мем-сделок в базе по половинам: " + ", ".join(
        f"{h} {sum(fap.is_meme(r[1][1]) for r in B[h])}" for h in HALVES4))

    # раскладка по исходу и стороне у сделок P0
    p("\n== Сделки базы класса P0 / P01: исход E1m и сторона (описательно)")
    for h in HALVES4:
        for v in ("P0", "P01"):
            mx = fap.CLASSES[v]
            sel = [r for r in B[h] if (m := m_of(r)) is not None and m <= mx]
            ev = Counter(r[5] for r in sel)
            side = Counter(r[1][3] for r in sel)
            win = sum(r[4] > 0 for r in sel)
            p(f"   {h} {v}: n={len(sel)} исходы {dict(ev)} стороны {dict(side)} "
              f"в плюсе {win}")
    with open(OUT / "result.pkl", "wb") as f:
        pickle.dump(res, f)


if __name__ == "__main__":
    main()
