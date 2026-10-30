"""Замер: трейл срабатывает по mark-цене вместо last
(docs/measurements/2026-10-31-mark-trigger.md).

  DATA_ROOT=/путь/к/data nice -n 15 .venv/bin/python -m scripts.measure_mark_trigger \
      [--procs 4] [--out файл.pkl] [--skip-fetch]

1. База — `measure_trail_realism.base()`: те же 4 635 сделок и паритет 5y/ротации (паритет 1).
2. Модель E на кэше m1 (только чтение) — net за сделку по половинам как в e767637 (паритет 2).
3. Mark 1m Bybit на интервалы, покрытые кэшем m1 (`scripts/fetch_mark1m.py`); сверка mark
   против last и покрытие окон (паритеты 3–4).
4. Модели MT (основная), MT-opt и MB — таблицы по половинам и вердикт по критерию.
"""
from __future__ import annotations

import argparse
import pickle
from multiprocessing import Pool

import numpy as np
import pandas as pd

import scripts.fetch_m1 as m1
import scripts.fetch_mark1m as mk
import scripts.measure_trail_realism as mtr
import scripts.research_fine_judge as fj
import scripts.research_judge as rj

MIN, HOUR = 60_000, 3_600_000
HALVES = mtr.HALVES
BASE_E = {"W1": -0.065, "W2": -0.057, "rot H1": -0.180, "rot H2": -0.004}
MODELS = ("E", "MT", "MT-opt", "MB")


def judge_mark(entry: float, stop: float, side: str, fill_open_ms: int, last: np.ndarray,
               mark: np.ndarray, *, stop_by: str = "last", exec_rule: str = "worst",
               act_r: float = 1.5, dist_r: float = 0.2, max_hold_h: float = 168.0) -> tuple:
    """Трейл по mark (раздел 4 регистрации). Филл и активация — по last, как в модели E.

    stop_by — по какой цене аварийный стоп −1R: 'last' (MT) или 'mark' (MB).
    exec_rule — исполнение срабатывания по mark: 'worst' (худшая из уровня и last open при
    срабатывании на переходе / last low|high внутри минуты) или 'level' (ровно по уровню).
    -> (event, r_gross, exit_bar_open_ms, минут без mark после активации)."""
    risk = abs(entry - stop)
    sgn = 1.0 if side == "LONG" else -1.0
    E, S = sgn * entry, sgn * stop
    A, D = E + act_r * risk, dist_r * risk
    ts, mts = last[:, 0], mark[:, 0]
    n, nm = len(last), len(mark)
    i0 = int(np.searchsorted(ts, fill_open_ms, "left"))
    j = int(np.searchsorted(mts, fill_open_ms, "left"))
    end_ms = fill_open_ms + int((max_hold_h + 1) * HOUR)
    fill_end = fill_open_ms + HOUR
    filled = active = False
    peak = level = 0.0
    prev_last = prev_mark = last_close = None
    gaps = 0

    def out(x: float) -> float:
        return round((x - E) / risk, 4)

    for i in range(i0, n):
        t = int(ts[i])
        if t >= end_ms:
            if filled and last_close is not None:
                return "timeout", out(last_close), int(ts[i - 1]), gaps
            break
        if not filled and t >= fill_end:
            return "nofill", None, None, gaps
        lp = [sgn * p for p in fj._points(last[i, 1], last[i, 2], last[i, 3], last[i, 4],
                                           "color")]
        l_open, l_min, l_close = lp[0], min(lp), lp[-1]
        while j < nm and mts[j] < t:
            j += 1
        mb = mark[j] if (j < nm and mts[j] == t) else None
        mp = (None if mb is None else
              [sgn * p for p in fj._points(mb[1], mb[2], mb[3], mb[4], "color")])

        was_active = active
        act_now = fill_now = False
        seq = ([prev_last] if prev_last is not None else []) + lp
        if not active:
            for k in range(len(seq) - 1):
                a, b = seq[k], seq[k + 1]
                if not filled:
                    if min(a, b) <= E <= max(a, b):
                        filled = fill_now = True
                        a = E
                    else:
                        continue
                if a >= A:
                    active, act_now, peak = True, True, a
                    break
                if b < a:
                    if stop_by == "last" and b <= S:
                        return "stopped", out(S), t, gaps
                elif b >= A:
                    active, act_now, peak = True, True, b
                    break

        if filled and not was_active and not act_now and stop_by == "mark" and mp is not None:
            # MB: аварийный стоп по mark до активации; в минуту филла — только по mark close
            mseq = (mp[-1:] if fill_now else
                    ([prev_mark] if prev_mark is not None else []) + mp)
            for k in range(len(mseq) - 1 if not fill_now else 1):
                if fill_now:
                    hit, gap = mseq[0] <= S, False
                else:
                    a, b = mseq[k], mseq[k + 1]
                    hit, gap = (b < a and b <= S), (k == 0 and prev_mark is not None)
                if hit:
                    if exec_rule == "level":
                        x = S
                    elif fill_now:
                        x = min(S, l_close)
                    else:
                        x = min(S, l_open if gap else l_min)
                    return "stopped", out(x), t, gaps

        if act_now:
            # минута активации: трейл стоит с момента прохода +act_r, порядок mark внутри
            # минуты относительно активации неизвестен — только mark close
            if mp is not None:
                peak = max(peak, mp[-1])
                level = peak - D
                if mp[-1] <= level:
                    x = level if exec_rule == "level" else min(level, l_close)
                    return "trail", out(x), t, gaps
            else:
                level = peak - D
        elif was_active:
            if mp is None:
                gaps += 1
            else:
                mseq = ([prev_mark] if prev_mark is not None else []) + mp
                for k in range(len(mseq) - 1):
                    a, b = mseq[k], mseq[k + 1]
                    if b < a:
                        if b <= level:
                            if exec_rule == "level":
                                x = level
                            else:
                                gap = k == 0 and prev_mark is not None
                                x = min(level, l_open if gap else l_min)
                            return "trail", out(x), t, gaps
                    elif b > peak:
                        peak = b
                        level = peak - D
            if stop_by == "last" and l_min <= S:
                return "stopped", out(S), t, gaps
            if stop_by == "mark" and mp is not None and min(mp) <= S:
                x = S if exec_rule == "level" else min(S, l_min)
                return "stopped", out(x), t, gaps

        prev_last = lp[-1]
        last_close = lp[-1]
        if mp is not None:
            prev_mark = mp[-1]
        if filled and t + MIN >= end_ms:
            return "timeout", out(last_close), t, gaps
    return "unresolved", None, None, gaps


def run_chunk(trades: pd.DataFrame) -> dict:
    """{key: {model: (ev, rg, rn, exit_ms, gaps)}} для E, MT, MT-opt, MB."""
    res: dict = {}
    lc: dict = {}
    mc: dict = {}
    for row in trades.itertuples(index=False):
        if row.symbol not in lc:
            lc[row.symbol] = m1.load(row.symbol)
            mc[row.symbol] = mk.load(row.symbol)
        last, mark = lc[row.symbol], mc[row.symbol]
        r: dict = {}
        ev, rg, ex = fj.judge(row.entry, row.stop, row.side, row.fill_ms, last, bar_ms=MIN,
                              mode="path", immediate=True, order="color")
        r["E"] = (ev, rg, None if rg is None else rg - mtr.cost(row, ex), ex, 0)
        for name, stop_by, rule in (("MT", "last", "worst"), ("MT-opt", "last", "level"),
                                    ("MB", "mark", "worst")):
            ev, rg, ex, gaps = judge_mark(row.entry, row.stop, row.side, row.fill_ms, last,
                                          mark, stop_by=stop_by, exec_rule=rule)
            r[name] = (ev, rg, None if rg is None else rg - mtr.cost(row, ex), ex, gaps)
        res[row.key] = r
    return res


def mark_windows(symbols) -> dict:
    """Интервалы, покрытые кэшем m1, — те же окна для mark."""
    return {s: [tuple(iv) for iv in m1.coverage(s)] for s in symbols if m1.coverage(s)}


def mark_checks(trades: pd.DataFrame) -> pd.DataFrame:
    """Покрытие mark в окне [филл; выход M1h + 1ч) и доля минут |mark − last| > 1%."""
    cov, far, both = {}, {}, {}
    cache: dict = {}
    for row in trades.itertuples(index=False):
        if row.symbol not in cache:
            cache[row.symbol] = (m1.load(row.symbol), mk.load(row.symbol))
        last, mark = cache[row.symbol]
        lo, hi = row.fill_ms, row.exit_ms + HOUR
        ml = mark[(mark[:, 0] >= lo) & (mark[:, 0] < hi)] if len(mark) else mark
        ll = last[(last[:, 0] >= lo) & (last[:, 0] < hi)] if len(last) else last
        cov[row.key] = len(ml) / ((hi - lo) / MIN)
        common, il, im = np.intersect1d(ll[:, 0], ml[:, 0], return_indices=True)
        both[row.key] = len(common)
        far[row.key] = (int(np.sum(np.abs(ml[im, 4] - ll[il, 4]) > 0.01 * ll[il, 4]))
                        if len(common) else 0)
    out = trades.copy()
    out["covmark"] = out["key"].map(cov)
    out["n_both"] = out["key"].map(both)
    out["n_far"] = out["key"].map(far)
    return out


def chunks(trades: pd.DataFrame, k: int) -> list:
    t = trades.sort_values(["symbol", "fill_ms"]).reset_index(drop=True)
    edges = np.linspace(0, len(t), k + 1).astype(int)
    return [t.iloc[a:b] for a, b in zip(edges[:-1], edges[1:]) if b > a]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--out", default=None)
    ap.add_argument("--skip-fetch", action="store_true")
    args = ap.parse_args()

    print("== Паритет 1: база", flush=True)
    tabs, origs = [], {}
    for data in ("5y", "rot"):
        df, same, orig = mtr.base(data)
        origs[data] = orig
        print(f"  {data}: реплика = оригинал: {same}; сделок: {len(df)}", flush=True)
        if not same:
            print("СТОП: реплика не совпала")
            return
        tabs.append(df)
    if not rj.check_parity(origs):
        print("СТОП: паритет базы")
        return
    trades = pd.concat(tabs, ignore_index=True)
    print("  " + str(trades.groupby("half").size().to_dict()), flush=True)
    trades["cov1m"] = trades["key"].map(mtr.coverage_ok(trades))

    if not args.skip_fetch:
        print("== Mark 1m Bybit на окна кэша m1", flush=True)
        mk.ensure(mark_windows(sorted(trades["symbol"].unique())))

    print("== Паритеты 3–4: mark против last, покрытие", flush=True)
    trades = mark_checks(trades)
    for h in HALVES:
        p = trades[trades["half"] == h]
        nb, nf = int(p["n_both"].sum()), int(p["n_far"].sum())
        okc = float((p["covmark"] >= 0.95).mean())
        print(f"  {h}: минут mark∩last {nb}, |mark − last| > 1%: {nf} ({nf / max(nb, 1):.3%})"
              + ("  ФЛАГ > 5%" if nf / max(nb, 1) > 0.05 else "")
              + f"; сделок с покрытием mark ≥ 95%: {okc:.1%}"
              + ("  ФЛАГ < 90%" if okc < 0.90 else ""), flush=True)

    print("== Прогон моделей E, MT, MT-opt, MB", flush=True)
    with Pool(args.procs) as pool:
        parts = pool.map(run_chunk, chunks(trades, args.procs * 3))
    res: dict = {}
    for p in parts:
        res.update(p)

    report(trades, res)
    if args.out:
        with open(args.out, "wb") as f:
            pickle.dump({"trades": trades, "res": res}, f)


def stats(rows: list) -> dict:
    """rows: [(exit_ms, gross, net, event)]."""
    s = mtr.stats(rows)
    tr = [r[1] for r in rows if r[3] == "trail"]
    s["trail_r"] = float(np.mean(tr)) if tr else float("nan")
    return s


def report(trades: pd.DataFrame, res: dict) -> None:
    ok = trades[(trades["cov1m"] >= 0.95) & (trades["covmark"] >= 0.95)]
    good = {k for k in ok["key"] if all(res[k][m][1] is not None for m in MODELS)}
    for m in MODELS:
        cnt = pd.Series([res[k][m][0] for k in trades["key"]]).value_counts().to_dict()
        print(f"  исходы {m}: {cnt}")
    gaps = sum(res[k]["MT"][4] for k in good)
    print(f"  минут без mark после активации (MT, парное множество): {gaps}")

    print("\n== Паритет 2: E на всех сделках с покрытием m1 ≥ 95% (как в e767637)")
    p2 = True
    allk = trades[trades["cov1m"] >= 0.95]
    for h in HALVES:
        ks = allk.loc[allk["half"] == h, "key"]
        v = float(np.mean([res[k]["E"][2] for k in ks if res[k]["E"][2] is not None]))
        okh = abs(v - BASE_E[h]) <= 0.001 + 1e-9
        p2 &= okh
        print(f"  {h}: E net/сд {v:+.4f} (эталон {BASE_E[h]:+.3f}) {'OK' if okh else 'НЕТ'}")
    if not p2:
        print("СТОП: паритет 2 не сошёлся — вердикт не выносится")
        return

    print(f"\n== Парное множество: {len(good)} из {len(trades)}")
    nets: dict = {}
    for h in HALVES:
        part = trades[(trades["half"] == h) & trades["key"].isin(good)]
        tot = int((trades["half"] == h).sum())
        print(f"\n{h}: n={len(part)} из {tot} (покрытие {len(part) / max(tot, 1):.1%})")
        print(f"  {'модель':8s} {'n':>5s} {'gross/сд':>9s} {'net/сд':>8s} {'Σnet':>8s} "
              f"{'DD':>6s} {'трейл%':>7s} {'трейл R':>8s} {'Δnet/сд':>8s}")
        base = None
        for m in MODELS:
            rows = [(res[k][m][3], res[k][m][1], res[k][m][2], res[k][m][0])
                    for k in part["key"]]
            s = stats(rows)
            if m == "E":
                base = s["net"]
            nets[(h, m)] = (s["net"], s["net"] - base)
            print(f"  {m:8s} {s['n']:5d} {s['gross']:+9.3f} {s['net']:+8.3f} {s['sum']:+8.1f} "
                  f"{s['dd']:6.1f} {s['trail']:7.1%} {s['trail_r']:+8.3f} "
                  f"{s['net'] - base:+8.3f}")
    verdict(nets)


def verdict(nets: dict) -> None:
    print("\n== ВЕРДИКТ (основная модель MT; критерий раздела 7)")
    pos = all(nets[(h, "MT")][0] > 0 for h in HALVES)
    up = all(nets[(h, "MT")][1] >= 0.05 for h in HALVES)
    for h in HALVES:
        n, d = nets[(h, "MT")]
        print(f"  {h}: MT net/сд {n:+.3f}, Δ против E {d:+.3f}")
    print(f"  net > 0 на всех половинах: {pos}; Δ ≥ +0.05 на всех половинах: {up}")
    print("  MT → " + ("КАНДИДАТ" if pos and up else "НЕ КАНДИДАТ"))
    for m in ("MT-opt", "MB"):
        halves = ", ".join(f"{h} {nets[(h, m)][0]:+.3f} (Δ {nets[(h, m)][1]:+.3f})"
                           for h in HALVES)
        print(f"  (информация) {m}: {halves}")


if __name__ == "__main__":
    main()
