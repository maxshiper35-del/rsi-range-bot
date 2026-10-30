"""Замер: реализм трейла (docs/measurements/2026-10-30-trail-realism.md).

  DATA_ROOT=/путь/к/data .venv/bin/python -m scripts.measure_trail_realism [--out файл.pkl]

1. База — прод с 27.10 оригинальным путём (паритет 1), реплика судья с пустым правилом
   (паритет 2) даёт gross R и издержки каждой сделки.
2. Мелкий судья в дискретном режиме на 1h-кэше повторяет журнал (паритет 3).
3. Минутки Bybit на окна сделок (`scripts/fetch_m1.py`), сверка данных (паритет 4).
4. Модели J/E на 15m и 1m, порядок внутри бара: цвет свечи и две границы. Сделки, где
   мелкий судья не разрешился в окне (стоп на баре филла раньше касания входа), докачиваются
   и пересуживаются.
5. Таблицы по половинам и вердикт по критерию регистрации.
"""
from __future__ import annotations

import argparse
import pickle
from multiprocessing import Pool

import numpy as np
import pandas as pd

import scripts.fetch_m1 as m1
import scripts.measure_one_position as mop
import scripts.research_fine_judge as fj
import scripts.research_judge as rj
from app.sim.backtest import _cost_r
from scripts.dump_history import cache_name

ROOT = rj.rebind_data()
FEE, SLIP, FUND = 0.055, 0.15, 0.125
W_START = pd.Timestamp("2024-07-31", tz="UTC")
MIN, M15, HOUR = 60_000, 900_000, 3_600_000
HALVES = ("W1", "W2", "rot H1", "rot H2")
MODELS = [(tf, var, order) for tf in ("15m", "1m") for var in ("J", "E")
          for order in ("color", "LH", "HL")]
EXTEND_H = 170                       # докачка для сделок, не разрешившихся в окне


def _ms(t) -> int:
    return int(rj.utc(t).value // 1_000_000)


def base(data: str) -> tuple[pd.DataFrame, bool, list]:
    """Сделки базы с gross R и издержками; (таблица, реплика == оригинал, сделки оригинала)."""
    bars, sigs = rj.load(data)
    orig = rj.trades(rj.simulate(bars, sigs))
    rj.DIAG = {}
    rj.install(hook=rj.NoopHook())
    rep = rj.simulate(bars, sigs)
    rj.uninstall()
    diag, rj.DIAG = rj.DIAG, None
    same = rj.trades(rep) == orig
    by_key = {rj.key(s["symbol"], s["dt"], s["side"], s["setup"]): s for s in sigs}
    cut = mop.SPLIT5Y if data == "5y" else mop.SPLIT2Y
    rows = []
    for t in rep.detail:
        k = rj.key(t.symbol, t.signal_dt, t.side, t.setup)
        s, d = by_key[k], diag[k]
        decision = rj.utc(t.signal_dt) + pd.Timedelta(hours=1)
        if data == "5y":
            half = "W1" if decision < cut else "W2"
        else:
            half = "rot H1" if decision < cut else "rot H2"
        rows.append(dict(data=data, key=(data, *k), half=half, symbol=t.symbol, side=t.side,
                         entry=float(s["entry"]), stop=float(s["stop"]),
                         decision=decision, fill_ms=_ms(t.fill_dt), exit_ms=_ms(t.exit_dt),
                         ev_1h=d["event"], rg_1h=d["r_gross"], cost_1h=d["cost"],
                         rn_1h=float(t.result_r)))
    df = pd.DataFrame(rows)
    df = df[df["fill_ms"] >= _ms(W_START)].reset_index(drop=True)
    return df, same, orig


def _arr(df: pd.DataFrame) -> np.ndarray:
    """Кэш-CSV (ts в мс, open..close) -> (n, 5)."""
    cols = ("ts", "open", "high", "low", "close")
    return np.column_stack([df[c].to_numpy(float) for c in cols])


def bars_1h(data: str, symbols) -> dict:
    src = ROOT / ("cache5y" if data == "5y" else "cache_univ")
    return {sym: _arr(pd.read_csv(src / cache_name(sym, "1h"))) for sym in symbols}


def agg(a: np.ndarray, step: int) -> np.ndarray:
    """Агрегат минуток в бары step мс по границам UTC (o — первый, c — последний)."""
    if not len(a):
        return a
    g = (a[:, 0] // step).astype(np.int64)
    starts = np.r_[0, np.nonzero(np.diff(g))[0] + 1]
    ends = np.r_[starts[1:], len(a)]
    return np.column_stack([g[starts] * step, a[starts, 1],
                            np.maximum.reduceat(a[:, 2], starts),
                            np.minimum.reduceat(a[:, 3], starts), a[ends - 1, 4]])


def fine_bars(data: str, tf: str, symbol: str) -> np.ndarray:
    if tf == "1m":
        return m1.load(symbol)
    if data == "5y":
        return _arr(pd.read_csv(ROOT / "cache5y" / cache_name(symbol, "15m")))
    return agg(m1.load(symbol), M15)


def cost(row, exit_ms: int) -> float:
    risk = abs(row.entry - row.stop)
    held = int((exit_ms - row.fill_ms) // HOUR)
    return _cost_r(row.entry, row.stop, FEE, SLIP) + FUND / 10000.0 * row.entry / risk * held


def run_model(job: tuple) -> tuple:
    """(data, tf, var, order, trades) -> (job без trades, {key: (ev, rg, rn, exit_ms)})."""
    data, tf, var, order, trades = job
    out = {}
    cache: dict = {}
    for row in trades.itertuples(index=False):
        if row.symbol not in cache:
            cache[row.symbol] = fine_bars(data, tf, row.symbol)
        b = cache[row.symbol]
        ev, rg, ex = fj.judge(row.entry, row.stop, row.side, row.fill_ms, b,
                              bar_ms=M15 if tf == "15m" else MIN, mode="path",
                              immediate=(var == "E"), order=order)
        out[row.key] = (ev, rg, None if rg is None else rg - cost(row, ex), ex)
    return (data, tf, var, order), out


def parity_discrete(data: str, trades: pd.DataFrame) -> tuple[int, int, list]:
    h1 = bars_1h(data, trades["symbol"].unique())
    bad = []
    for row in trades.itertuples(index=False):
        ev, rg, ex = fj.judge(row.entry, row.stop, row.side, row.fill_ms, h1[row.symbol],
                              bar_ms=HOUR, mode="discrete")
        if (ev, rg, ex) != (row.ev_1h, round(row.rg_1h, 4), row.exit_ms):
            bad.append((row.key, (ev, rg, ex), (row.ev_1h, row.rg_1h, row.exit_ms)))
    return len(trades), len(bad), bad[:5]


def windows(trades: pd.DataFrame, extend: set | None = None) -> dict:
    w: dict = {}
    for row in trades.itertuples(index=False):
        end = row.exit_ms + HOUR
        if extend and row.key in extend:
            end = max(end, row.fill_ms + EXTEND_H * HOUR)
        w.setdefault(row.symbol, []).append((row.fill_ms, end))
    return w


def coverage_ok(trades: pd.DataFrame) -> dict:
    """{key: доля минуток в окне [филл, выход+1ч)} — пропуски Bybit/недокачка."""
    out = {}
    cache: dict = {}
    for row in trades.itertuples(index=False):
        if row.symbol not in cache:
            cache[row.symbol] = m1.load(row.symbol)[:, 0]
        ts = cache[row.symbol]
        a = np.searchsorted(ts, row.fill_ms, "left")
        b = np.searchsorted(ts, row.exit_ms + HOUR, "left")
        out[row.key] = (b - a) / ((row.exit_ms + HOUR - row.fill_ms) / MIN)
    return out


def parity_data(data: str, trades: pd.DataFrame) -> dict:
    """Экстремумы 1m по часам окон против 1h-кэша; для 9 монет — агрегат 1m→15m против 15m."""
    h1 = bars_1h(data, trades["symbol"].unique())
    res = {"h_total": 0, "h_bad": 0, "q_total": 0, "q_bad": 0}
    for sym, g in trades.groupby("symbol"):
        a = m1.load(sym)
        if not len(a):
            continue
        ah = agg(a, HOUR)
        aq = agg(a, M15)
        hh = h1[sym]
        idx = {int(t): i for i, t in enumerate(hh[:, 0])}
        q15 = None
        if data == "5y":
            q = fine_bars("5y", "15m", sym)
            q15 = {int(t): i for i, t in enumerate(q[:, 0])}
        for row in g.itertuples(index=False):
            lo_, hi_ = row.fill_ms, row.exit_ms + HOUR
            sel = (ah[:, 0] >= lo_) & (ah[:, 0] < hi_)
            for r in ah[sel]:
                i = idx.get(int(r[0]))
                if i is None:
                    continue
                res["h_total"] += 1
                if not (np.isclose(r[2], hh[i, 2], rtol=1e-9)
                        and np.isclose(r[3], hh[i, 3], rtol=1e-9)):
                    res["h_bad"] += 1
            if q15 is not None:
                selq = (aq[:, 0] >= lo_) & (aq[:, 0] < hi_)
                for r in aq[selq]:
                    i = q15.get(int(r[0]))
                    if i is None:
                        continue
                    res["q_total"] += 1
                    if not np.allclose(r[1:], q[i, 1:], rtol=1e-9):
                        res["q_bad"] += 1
    return res


def stats(rows: list) -> dict:
    """rows: [(exit_ms, r_gross, r_net, event)] -> n, gross/net на сделку, Σnet, DD, трейл."""
    if not rows:
        return {"n": 0}
    rows = sorted(rows, key=lambda x: x[0])
    net = np.array([r[2] for r in rows])
    cum = np.cumsum(net)
    dd = float(np.max(np.maximum.accumulate(np.r_[0.0, cum])[1:] - cum)) if len(cum) else 0.0
    return {"n": len(rows), "gross": float(np.mean([r[1] for r in rows])),
            "net": float(net.mean()), "sum": float(net.sum()), "dd": max(dd, 0.0),
            "trail": float(np.mean([r[3] == "trail" for r in rows]))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--procs", type=int, default=6)
    args = ap.parse_args()

    print("== База и паритет 1–2")
    tabs, origs = [], {}
    for data in ("5y", "rot"):
        df, same, orig = base(data)
        origs[data] = orig
        print(f"  {data}: реплика = оригинал сделка-к-сделке: {same}; "
              f"сделок в окне 15m: {len(df)}")
        if not same:
            print("СТОП: реплика не совпала")
            return
        tabs.append(df)
    if not rj.check_parity(origs):
        print("СТОП: паритет базы")
        return
    trades = pd.concat(tabs, ignore_index=True)
    print(trades.groupby("half").size().to_dict())

    print("== Паритет 3: дискретный мелкий судья на 1h = журнал")
    ok3 = True
    for data in ("5y", "rot"):
        n, nbad, ex = parity_discrete(data, trades[trades["data"] == data])
        print(f"  {data}: {n - nbad}/{n} совпали; примеры расхождений: {ex}")
        ok3 &= nbad == 0
    if not ok3:
        print("СТОП: паритет 3")
        return

    print("== Минутки Bybit на окна сделок")
    m1.ensure(windows(trades))
    # сделки, где мелкий судья упёрся в конец окна, — докачка и повтор
    probe = {}
    for data in ("5y", "rot"):
        for var in ("J", "E"):
            _j, res = run_model((data, "1m", var, "color", trades[trades["data"] == data]))
            probe.update({k: v for k, v in res.items() if v[0] == "unresolved"})
    if probe:
        print(f"  не разрешились в окне: {len(probe)} — докачка +{EXTEND_H}ч")
        m1.ensure(windows(trades[trades["key"].isin(set(probe))], set(probe)))

    print("== Паритет 4: данные")
    for data in ("5y", "rot"):
        pdat = parity_data(data, trades[trades["data"] == data])
        hb = pdat["h_bad"] / max(pdat["h_total"], 1)
        qb = pdat["q_bad"] / max(pdat["q_total"], 1)
        print(f"  {data}: часы 1m vs 1h-кэш: расхождений {pdat['h_bad']}/{pdat['h_total']} "
              f"({hb:.2%}); 1m→15m vs 15m-кэш: {pdat['q_bad']}/{pdat['q_total']} ({qb:.2%})"
              + ("  ФЛАГ > 1%" if max(hb, qb) > 0.01 else ""))
    cov = coverage_ok(trades)
    trades["cov1m"] = trades["key"].map(cov)

    print("== Прогон моделей")
    jobs = [(data, tf, var, order, trades[trades["data"] == data])
            for data in ("5y", "rot") for tf, var, order in MODELS]
    with Pool(args.procs) as pool:
        got = pool.map(run_model, jobs)
    res: dict = {}
    for (_data, tf, var, order), out in got:
        res.setdefault((tf, var, order), {}).update(out)

    report(trades, res)
    if args.out:
        with open(args.out, "wb") as f:
            pickle.dump({"trades": trades, "res": res}, f)


def _valid(res: dict, tf: str, keys) -> set:
    good = set(keys)
    for (t, _var, _order), out in res.items():
        if t != tf:
            continue
        good &= {k for k, v in out.items() if v[1] is not None}
    return good


def paired(trades: pd.DataFrame, res: dict, tf: str) -> set:
    """Сделки, разрешённые всеми моделями этого ТФ; минутное покрытие окна ≥ 95% (для 15m
    ротации тоже — она собрана из минуток)."""
    good = _valid(res, tf, set(trades["key"]))
    cov_ok = set(trades.loc[trades["cov1m"] >= 0.95, "key"])
    if tf == "1m":
        return good & cov_ok
    return good & (cov_ok | set(trades.loc[trades["data"] == "5y", "key"]))


def report(trades: pd.DataFrame, res: dict) -> None:
    keys = set(trades["key"])
    for tf in ("15m", "1m"):
        good = paired(trades, res, tf)
        print(f"\n### {tf}: парное множество {len(good)} из {len(keys)}")
        for (t, var, order), out in sorted(res.items()):
            if t == tf:
                cnt = pd.Series([v[0] for v in out.values()]).value_counts().to_dict()
                print(f"  исходы {var}{tf} {order}: {cnt}")
        for half in HALVES:
            part = trades[(trades["half"] == half) & trades["key"].isin(good)]
            tot = int((trades["half"] == half).sum())
            print(f"\n{half}: n={len(part)} из {tot} (покрытие {len(part) / max(tot, 1):.1%})")
            base_rows = [(r.exit_ms, r.rg_1h, r.rn_1h, r.ev_1h) for r in part.itertuples()]
            sb = stats(base_rows)
            print(f"  {'модель':10s} {'n':>5s} {'gross/сд':>9s} {'net/сд':>8s} {'Σnet':>8s} "
                  f"{'DD':>6s} {'трейл%':>7s} {'Δnet/сд':>8s} {'переоц.трейла':>13s}")
            print(f"  {'M1h':10s} {sb['n']:5d} {sb['gross']:+9.3f} {sb['net']:+8.3f} "
                  f"{sb['sum']:+8.1f} {sb['dd']:6.1f} {sb['trail']:7.1%}")
            for var in ("J", "E"):
                for order in ("color", "LH", "HL"):
                    out = res[(tf, var, order)]
                    rows = [(out[k][3], out[k][1], out[k][2], out[k][0]) for k in part["key"]]
                    s = stats(rows)
                    tr = part[part["ev_1h"] == "trail"]
                    over = (np.mean([r.rg_1h - out[r.key][1] for r in tr.itertuples()])
                            if len(tr) else 0.0)
                    print(f"  {var + tf + ' ' + order:10s} {s['n']:5d} {s['gross']:+9.3f} "
                          f"{s['net']:+8.3f} {s['sum']:+8.1f} {s['dd']:6.1f} {s['trail']:7.1%} "
                          f"{s['net'] - sb['net']:+8.3f} {over:+13.3f}")
            for var in ("J", "E"):
                out = res[(tf, var, "color")]
                parts = []
                for cls in ("trail", "stopped", "timeout"):
                    sub = part[part["ev_1h"] == cls]
                    d = sum(out[k][2] - rn for k, rn in zip(sub["key"], sub["rn_1h"]))
                    parts.append(f"{cls} n={len(sub)} ΣΔ={d:+.1f}R")
                print(f"  разложение Δ {var}{tf} по исходу M1h: " + "; ".join(parts))
    verdict(trades, res)


def _delta(trades, res, keys, half, model) -> tuple[float, float, float]:
    part = trades[(trades["half"] == half) & trades["key"].isin(keys)]
    out = res[model]
    b = float(part["rn_1h"].mean())
    v = float(np.mean([out[k][2] for k in part["key"]]))
    return v - b, b, v


def _class(ds: list) -> str:
    if all(abs(d) <= 0.02 for d in ds):
        return "адекватна"
    if sum(d <= -0.03 for d in ds) >= 3:
        return "завышает"
    if sum(d >= 0.03 for d in ds) >= 3:
        return "занижает"
    return "неоднозначно"


def verdict(trades: pd.DataFrame, res: dict) -> None:
    keys = paired(trades, res, "1m")
    print("\n== ВЕРДИКТ (основная метрика: Δ = net/сд E1m − M1h, парно на покрытии 1m)")
    cls = {}
    for order in ("color", "LH", "HL"):
        ds = [_delta(trades, res, keys, h, ("1m", "E", order))[0] for h in HALVES]
        cls[order] = _class(ds)
        halves = ", ".join(f"{h} {d:+.3f}" for h, d in zip(HALVES, ds))
        print(f"  E1m {order:5s}: Δ по половинам {halves} → {cls[order]}")
    k15 = paired(trades, res, "15m")
    ds15 = [_delta(trades, res, k15, h, ("15m", "E", "color"))[0] for h in HALVES]
    halves15 = ", ".join(f"{h} {d:+.3f}" for h, d in zip(HALVES, ds15))
    print(f"  (для сравнения) E15 color: {halves15} → {_class(ds15)}")
    stable = len(set(cls.values())) == 1
    print(f"  Вопрос 1: модель журнала {cls['color'].upper()}"
          + ("" if stable else "  [ФЛАГ: чувствителен к порядку внутри минуты]"))
    b_net, e_net = [], []
    for h in HALVES:
        _d, b, v = _delta(trades, res, keys, h, ("1m", "E", "color"))
        n = int(((trades["half"] == h) & trades["key"].isin(keys)).sum())
        b_net.append((b, n))
        e_net.append((v, n))
    pos = [i for i, (b, _n) in enumerate(b_net) if b > 0]
    flipped = sum(e_net[i][0] <= 0 for i in pos)
    total = sum(v * n for v, n in e_net)
    if all(e_net[i][0] > 0 for i in pos) and total > 0:
        q2 = "плюс сохраняется"
    elif flipped >= 2 or total <= 0:
        q2 = "плюс исчезает"
    else:
        q2 = "на грани"
    by_half = ", ".join(f"{h} {v:+.3f} (M1h {b:+.3f})"
                        for h, (v, _n), (b, _m) in zip(HALVES, e_net, b_net))
    print(f"  Вопрос 2: E1m net/сд по половинам {by_half}; Σ E1m {total:+.1f}R → {q2.upper()}")


if __name__ == "__main__":
    main()
