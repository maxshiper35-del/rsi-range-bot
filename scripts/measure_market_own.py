"""Замер: ход рынка против собственного хода монеты (docs/measurements/2026-10-31-market-vs-own.md).

  DATA_ROOT="/Users/mac/bot ctypto/data" PYTHONDONTWRITEBYTECODE=1 nice -n 15 \
      .venv/bin/python -m scripts.measure_market_own [--procs 2] [--out файл.pkl]

1. База E1m — прод с 27.10 (трейл 1.5/0.2, M2′), полная пересимуляция для 9 монет и ротации;
   паритет с таблицей базы spike_family. Решения ≥ 2026-08-01 не покидают воркер (заморозка).
2. Каждая сделка: gross R = рыночная часть + собственный ход монеты. Рынок — минутки BTC
   (основная модель) и равновзвешенный индекс топ-30 по обороту без самой монеты.
3. Решение — по rot H1 и rot H2′ (раздел 6 регистрации); остальное справочно.
"""
from __future__ import annotations

import argparse
import os
import pickle
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd

import scripts.fetch_m1 as m1
import scripts.measure_exit_geometry as eg
import scripts.measure_one_position as mop
import scripts.measure_trail_realism as mtr
import scripts.research_fine_judge as fj
import scripts.research_judge as rj
from scripts.dump_history import cache_name
from scripts.research_aggr_clusters import is_meme

ROOT = rj.rebind_data()
MIN, HOUR, DAY = 60_000, 3_600_000, 86_400_000
CUT_MS = int(pd.Timestamp("2026-08-01", tz="UTC").value // 1_000_000)   # начало заморозки
G0 = int(pd.Timestamp("2024-04-01", tz="UTC").value // 1_000_000)      # сетка часов: β, индекс
G1 = int(pd.Timestamp("2026-08-20", tz="UTC").value // 1_000_000)
NG = (G1 - G0) // HOUR
HALVES = ("rot H1", "rot H2'", "W1", "W2")
MAIN = ("rot H1", "rot H2'")
REF = {"rot H1": (1300, -0.1653, -214.8), "rot H2'": (1110, -0.0209, -23.2),
       "W1": (1026, -0.0769, -78.9), "W2": (872, -0.0630, -54.9)}     # база spike_family
WIN = {"30": 720, "14": 336, "60": 1440}                                # окно β, часов
MIN_PAIRS = 168
TOP_N, CLIP = 30, 0.10
HEDGE_COST = 2 * 0.00055 + 0.0002          # хедж BTC: тейкер × 2 + слип 0.01% на сторону
BTC = "BTC/USDT:USDT"
TASK_THR = 0.15


# ---------------------------------------------------------------- воркер: база E1m
def fill_minute(entry: float, side: str, fill_open_ms: int, bars: np.ndarray) -> int | None:
    """Минута филла: первое касание входа в часе филла, как в research_fine_judge._path."""
    sgn = 1.0 if side == "LONG" else -1.0
    e = sgn * entry
    ts = bars[:, 0]
    prev = None
    for i in range(int(np.searchsorted(ts, fill_open_ms, "left")), len(bars)):
        t = int(ts[i])
        if t >= fill_open_ms + HOUR:
            return None
        pts = [sgn * p for p in fj._points(bars[i, 1], bars[i, 2], bars[i, 3], bars[i, 4],
                                           "color")]
        seq = ([prev] if prev is not None else []) + pts
        if any(min(a, b) <= e <= max(a, b) for a, b in zip(seq, seq[1:])):
            return t
        prev = pts[-1]
    return None


def exit_check(x: float, ev: str, ex_ms: int, risk: float, bars: np.ndarray) -> tuple:
    """Цена выхода внутри минуты выхода (с предыдущим close; timeout — её close).
    -> (допуск 1e-6 от цены, допуск ещё и на округление g судьёй до 4 знаков)."""
    ts = bars[:, 0]
    i = int(np.searchsorted(ts, ex_ms, "left"))
    if i >= len(bars) or int(ts[i]) != ex_ms:
        return False, False
    _t, _o, hi, lo, cl = bars[i]
    if ev == "timeout":
        lo_ = hi_ = cl
    else:
        lo_, hi_ = lo, hi
        if i > 0:
            lo_, hi_ = min(lo_, bars[i - 1, 4]), max(hi_, bars[i - 1, 4])
    return tuple(lo_ - tol <= x <= hi_ + tol
                 for tol in (1e-6 * abs(x), max(1e-6 * abs(x), 1e-4 * risk)))


def run_base(data: str) -> tuple:
    """База E1m (как run_job spike_family с пустым фильтром) + поля для разложения."""
    bars, sigs = rj.load(data)
    by_key = {rj.key(s["symbol"], s["dt"], s["side"], s["setup"]): s for s in sigs}
    eg._M1.clear()
    eg._END.clear()
    for (sym, tf), df in bars.items():
        if tf == "1h":
            eg._END[sym] = int(pd.Timestamp(df["dt"].iloc[-1]).value // 1_000_000) + HOUR
    eg.DIAG.clear()
    eg.MISS.update(nofill=0, unresolved=0)
    rj.install(variant=eg.BASE, order="color", mode="1m")
    rj._pf._live_outcome = eg.judge_exit
    try:
        rep = rj.simulate(bars, sigs)
    finally:
        rj.uninstall()
    rows = []
    for t in rep.detail:
        fill_ms = int(rj.utc(t.fill_dt).value // 1_000_000)
        dec_ms = int((rj.utc(t.signal_dt) + rj.HOUR).value // 1_000_000)
        if fill_ms < eg.W_START_MS or dec_ms >= CUT_MS:
            continue                                   # до минуток / заморозка не покидает воркер
        k = rj.key(t.symbol, t.signal_dt, t.side, t.setup)
        s = by_key[k]
        ev, rg, cost, ex = eg.DIAG[k]
        entry, stop = float(s["entry"]), float(s["stop"])
        risk = abs(entry - stop)
        x = entry + (1.0 if t.side == "LONG" else -1.0) * rg * risk
        b = eg._bars(t.symbol)
        strict, loose = exit_check(x, ev, int(ex), risk, b)
        rows.append(dict(data=data, key=(data, *k), symbol=t.symbol, side=t.side,
                         dec_ms=dec_ms, fill_hour=fill_ms,
                         t_f=fill_minute(entry, t.side, fill_ms, b),
                         t_x=int(ex), exit_ms=int(rj.utc(t.exit_dt).value // 1_000_000),
                         entry=entry, stop=stop, g=float(rg), c=float(cost),
                         net=float(t.result_r), ev=ev, x=x, x_strict=strict, x_loose=loose))
    return data, rows, dict(eg.MISS)


def half_of(data: str, dec_ms: int) -> str:
    dec = pd.Timestamp(dec_ms, unit="ms", tz="UTC")
    if data == "5y":
        return "W1" if dec < mop.SPLIT5Y else "W2"
    return "rot H1" if dec < mop.SPLIT2Y else "rot H2'"


# ---------------------------------------------------------------- рынок
def grid_lr(path) -> np.ndarray:
    """Часовые лог-доходности close-to-close соседних баров на сетке [G0, G1)."""
    d = pd.read_csv(path, usecols=["ts", "close"])
    ts = d["ts"].to_numpy(np.int64)
    cl = d["close"].to_numpy(float)
    lr = np.full(len(ts), np.nan)
    cons = np.diff(ts) == HOUR
    lr[1:][cons] = np.log(cl[1:][cons] / cl[:-1][cons])
    out = np.full(NG, np.nan)
    j = (ts - G0) // HOUR
    m = (j >= 0) & (j < NG)
    out[j[m]] = lr[m]
    return out


def ols(y: np.ndarray, x: np.ndarray, j1: int, w: int) -> float:
    """Наклон y на x по барам сетки [j1 − w, j1); меньше MIN_PAIRS пар — nan."""
    yy, xx = y[max(j1 - w, 0):j1], x[max(j1 - w, 0):j1]
    m = ~(np.isnan(yy) | np.isnan(xx))
    if m.sum() < MIN_PAIRS:
        return np.nan
    xv = xx[m] - xx[m].mean()
    v = float((xv * xv).sum())
    return float((xv * (yy[m] - yy[m].mean())).sum() / v) if v > 0 else np.nan


class Index:
    """Равновзвешенный индекс топ-30 по обороту прошлых суток (доходности часа обрезаны ±10%),
    без монеты сделки; монеты — с часовыми свечами в cache_univ."""

    def __init__(self) -> None:
        tv = pd.read_csv(ROOT / "cache_univ" / "turnover.csv",
                         usecols=["symbol", "ts", "turnover"])
        syms = tv["symbol"].unique()
        have = {s for s in syms if (ROOT / "cache_univ" / cache_name(s, "1h")).exists()}
        tv = tv[tv["symbol"].isin(have)]
        top = {int(d) + DAY: g.nlargest(TOP_N, "turnover")["symbol"].tolist()
               for d, g in tv.groupby("ts")}
        top = {d: ss for d, ss in top.items() if G0 - DAY < d < G1}
        self.coins = sorted({s for ss in top.values() for s in ss})
        self.ci = {s: i for i, s in enumerate(self.coins)}
        r = np.full((len(self.coins), NG), np.nan)
        for s in self.coins:
            r[self.ci[s]] = np.clip(np.expm1(grid_lr(ROOT / "cache_univ" / cache_name(s, "1h"))),
                                    -CLIP, CLIP)
        mem = np.zeros(r.shape, bool)
        for d, ss in top.items():
            j0 = (d - G0) // HOUR
            for s in ss:
                mem[self.ci[s], max(j0, 0):min(j0 + 24, NG)] = True
        self.v = mem & ~np.isnan(r)
        self.r = np.where(self.v, r, 0.0)
        self.s, self.n = self.r.sum(0), self.v.sum(0)
        self.days = len(top)
        self._loo: dict = {}

    def loo(self, sym: str) -> np.ndarray:
        """Лог-доходности индекса по часам без монеты sym."""
        if sym not in self._loo:
            s, n = self.s.copy(), self.n.copy()
            i = self.ci.get(sym)
            if i is not None:
                s -= self.r[i]
                n -= self.v[i]
            self._loo[sym] = np.log1p(np.where(n > 0, s / np.maximum(n, 1), np.nan))
        return self._loo[sym]


# ---------------------------------------------------------------- статистика
def boot_ci(vals: np.ndarray, days: np.ndarray, reps: int = 2000, seed: int = 0) -> tuple:
    """95% ДИ среднего: бутстрэп по дням решения (дни с возвращением)."""
    _u, inv = np.unique(days, return_inverse=True)
    sums, cnts = np.bincount(inv, weights=vals), np.bincount(inv)
    idx = np.random.default_rng(seed).integers(0, len(sums), size=(reps, len(sums)))
    m = sums[idx].sum(1) / cnts[idx].sum(1)
    return float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def spearman(a: np.ndarray, b: np.ndarray, perms: int = 10000, seed: int = 0) -> tuple:
    """Ранговая корреляция и двусторонний перестановочный p."""
    ra, rb = pd.Series(a).rank().to_numpy(), pd.Series(b).rank().to_numpy()
    rho = float(np.corrcoef(ra, rb)[0, 1])
    rng = np.random.default_rng(seed)
    null = np.array([np.corrcoef(ra, rng.permutation(rb))[0, 1] for _ in range(perms)])
    return rho, float((np.abs(null) >= abs(rho) - 1e-12).mean())


def tau(p: pd.DataFrame, col: str, min_n: int = 10) -> tuple:
    """Разброс истинных средних по монетам: τ² = max(0, var(m_i) − mean(s_i²/n_i))."""
    g = p.groupby("symbol")[col].agg(["mean", "var", "count"])
    g = g[g["count"] >= min_n]
    if len(g) < 3:
        return len(g), np.nan
    t2 = float(g["mean"].var(ddof=1) - (g["var"] / g["count"]).mean())
    return len(g), float(np.sqrt(max(t2, 0.0)))


def bybit(sym: str) -> str:
    return sym.split("/")[0] + "USDT"


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--procs", type=int, default=2)
    ap.add_argument("--out", default=None)
    ap.add_argument("--base-cache", default=None,
                    help="pickle сделок базы: есть — читается, нет — пишется после прогона")
    args = ap.parse_args()
    t0 = time.time()

    def lap(msg: str) -> None:
        print(f"[{time.time() - t0:6.0f}s] {msg}", flush=True)

    lap("== База E1m: полная пересимуляция (9 монет, ротация), решения < 2026-08-01")
    if args.base_cache and os.path.exists(args.base_cache):
        with open(args.base_cache, "rb") as f:
            got = pickle.load(f)
        print(f"  сделки базы из {args.base_cache}")
    else:
        with Pool(min(args.procs, 2), maxtasksperchild=1) as pool:
            got = pool.map(run_base, ("5y", "rot"))
        if args.base_cache:
            with open(args.base_cache, "wb") as f:
                pickle.dump(got, f)
    for data, rr, miss in got:
        print(f"  {data}: сделок учебного участка {len(rr)}, MISS {miss}")
    df = pd.DataFrame([r for _d, rr, _m in got for r in rr])
    df["half"] = [half_of(d, ms) for d, ms in zip(df["data"], df["dec_ms"])]
    assert (df["dec_ms"] < CUT_MS).all(), "заморозка просочилась"

    print("== Паритет 1: база = таблица базы spike_family")
    ok = True
    for h in HALVES:
        p = df[df["half"] == h]
        s = mtr.stats(list(zip(p["exit_ms"], p["g"], p["net"], p["ev"])))
        rn, re, rs = REF[h]
        good = (s["n"] == rn and f"{s['net']:+.4f}" == f"{re:+.4f}"
                and f"{s['sum']:+.1f}" == f"{rs:+.1f}")
        ok &= good
        print(f"  {h:8s} n={s['n']} E={s['net']:+.4f} Σ={s['sum']:+.1f} DD={s['dd']:.1f} "
              f"(эталон {rn} / {re:+.4f} / {rs:+.1f}) {'OK' if good else 'РАСХОЖДЕНИЕ'}")
    dn = float((df["net"] - (df["g"] - df["c"])).abs().max())
    print(f"  net = g − c: макс. расхождение {dn:.2e} (портфель округляет result_r до 4 знаков)")
    if not ok or dn > 5e-5 + 1e-9:
        print("СТОП: паритет базы — вердикт inconclusive")
        return
    print("== Паритет 2: минута филла найдена у каждой сделки")
    nf = int(df["t_f"].isna().sum())
    print(f"  отказов {nf}")
    if nf:
        print("СТОП: минута филла — вердикт inconclusive")
        return
    df["t_f"] = df["t_f"].astype(np.int64)
    print("== Паритет 3: цена выхода внутри минуты выхода")
    ns, nl = int((~df["x_strict"]).sum()), int((~df["x_loose"]).sum())
    print(f"  нарушений при допуске 1e-6 от цены: {ns}; с допуском на округление g до 4 знаков "
          f"(1e-4 риска): {nl}")
    if nl:
        print("СТОП: цена выхода — вердикт inconclusive")
        return

    if (df["t_x"] != df["exit_ms"]).any():
        print("СТОП: минута выхода судьи ≠ выход портфеля — вердикт inconclusive")
        return

    lap("== Рынок: минутки BTC, часовые ряды, индекс")
    bm = m1.load(BTC)
    bts = bm[:, 0].astype(np.int64)
    m_open, m_close = bm[:, 1], bm[:, 4]

    def bpos(t: np.ndarray) -> tuple:
        i = np.searchsorted(bts, t)
        ic = np.minimum(i, len(bts) - 1)
        return ic, bts[ic] == t

    def log_px(t: int, close: bool) -> float:
        i, ok_ = bpos(np.array([t], np.int64))
        assert ok_[0], f"нет минуты BTC {t}"
        return float(np.log((m_close if close else m_open)[i[0]]))

    i_f, ok_f = bpos(df["t_f"].to_numpy(np.int64))
    i_x, ok_x = bpos(df["t_x"].to_numpy(np.int64))
    print(f"== Паритет 4: минуты BTC — нет t_f у {int((~ok_f).sum())}, нет t_x у "
          f"{int((~ok_x).sum())} сделок")
    if (~ok_f).any() or (~ok_x).any():
        print("СТОП: минутки BTC — вердикт inconclusive")
        return
    btc_lr = grid_lr(ROOT / "cache5y" / cache_name(BTC, "1h"))
    idx = Index()
    print(f"  индекс: монет {len(idx.coins)}, дней состава {idx.days}, часов без индекса в "
          f"[2024-04-02, 2026-08-20): {int((idx.n[24:] == 0).sum())}")
    coin_lr: dict = {}
    for data, sym in sorted({(d, s) for d, s in zip(df["data"], df["symbol"])}):
        src = "cache5y" if data == "5y" else "cache_univ"
        coin_lr[(data, sym)] = grid_lr(ROOT / src / cache_name(sym, "1h"))

    lap("== Разложение сделок")
    out = []
    for k, r in enumerate(df.itertuples(index=False)):
        sgn = 1.0 if r.side == "LONG" else -1.0
        d = abs(r.entry - r.stop) / r.entry
        po, pc = m_open[i_f[k]], m_close[i_x[k]]
        r_m = pc / po - 1.0
        j1 = (r.dec_ms - G0) // HOUR
        y = coin_lr[(r.data, r.symbol)]
        row = dict(d=d, r_c=r.x / r.entry - 1.0, r_m=r_m)
        for w, hw in WIN.items():
            b = ols(y, btc_lr, j1, hw)
            row[f"fb{w}"] = bool(np.isnan(b))
            row[f"beta{w}"] = 1.0 if np.isnan(b) else b
        if r.symbol == BTC:                      # рынок и есть монета: собственный ход 0
            for w in WIN:
                row[f"beta{w}"], row[f"fb{w}"] = 1.0, False
                row[f"M{w}"] = r.g
            row["M1"] = r.g
        else:
            for w in WIN:
                row[f"M{w}"] = sgn * row[f"beta{w}"] * r_m / d
            row["M1"] = sgn * r_m / d
        # индекс: часы H_f…H_x, внутри часа — форма пути BTC
        lc = idx.loo(r.symbol)
        hf, hx = r.t_f - r.t_f % HOUR, r.t_x - r.t_x % HOUR
        jf, jx = (hf - G0) // HOUR, (hx - G0) // HOUR
        ff, fx = (r.t_f - hf) / HOUR, (r.t_x + MIN - hx) / HOUR
        seg = lc[jf:jx + 1]
        bb = ols(lc, btc_lr, j1, WIN["30"])
        bb = 1.0 if np.isnan(bb) else bb
        bi = ols(y, lc, j1, WIN["30"])
        row["fbI"] = bool(np.isnan(bi))
        row["betaI"] = 1.0 if np.isnan(bi) else bi
        r_i = np.nan
        if len(seg) == jx - jf + 1 and not np.isnan(seg).any():
            lam_f = log_px(hf + 59 * MIN, True) - log_px(hf, False)
            lam_x = log_px(hx + 59 * MIN, True) - log_px(hx, False)
            dev_f = np.log(po) - log_px(hf, False) - ff * lam_f
            dev_x = np.log(pc) - log_px(hx, False) - fx * lam_x
            lev = float(seg[:-1].sum() + fx * seg[-1] - ff * seg[0])
            r_i = float(np.expm1(lev + bb * (dev_x - dev_f)))
        row["r_i"], row["bI_btc"] = r_i, bb
        row["MI"] = sgn * row["betaI"] * r_i / d if not np.isnan(r_i) else np.nan
        out.append(row)
    df = pd.concat([df.reset_index(drop=True), pd.DataFrame(out)], axis=1)
    for w in WIN:
        df[f"O{w}"] = df["g"] - df[f"M{w}"]
    df["O1"] = df["g"] - df["M1"]
    df["OI"] = df["g"] - df["MI"]
    df["day"] = df["dec_ms"] // DAY
    df["meme"] = [is_meme(bybit(s)) for s in df["symbol"]]
    df["hedged"] = df["O30"] - df["c"] - df["beta30"] * HEDGE_COST / df["d"]
    df.loc[df["symbol"] == BTC, "hedged"] = np.nan
    print(f"  тождество g = M + O: макс. расхождение "
          f"{float((df['g'] - df['M30'] - df['O30']).abs().max()):.2e}; "
          f"сделок без индекса: {int(df['MI'].isna().sum())}")

    # ---- таблица по половинам
    print("\n== Разложение по половинам (средние на сделку, R)")
    tab = {}
    for h in HALVES:
        p = df[df["half"] == h]
        pi = p[p["MI"].notna()]
        ci_o = boot_ci(p["O30"].to_numpy(), p["day"].to_numpy())
        ci_i = boot_ci(pi["OI"].to_numpy(), pi["day"].to_numpy())
        ci_m = boot_ci(p["M30"].to_numpy(), p["day"].to_numpy())
        nm = p[~p["meme"]]
        top5 = np.sort(p["O30"].to_numpy())[::-1][5:].mean()
        t = dict(n=len(p), g=p["g"].mean(), c=p["c"].mean(), net=p["net"].mean(),
                 M=p["M30"].mean(), O=p["O30"].mean(), ci_o=ci_o, ci_m=ci_m,
                 MI=pi["MI"].mean(), OI=pi["OI"].mean(), ci_i=ci_i, nI=len(pi),
                 O1=p["O1"].mean(), O14=p["O14"].mean(), O60=p["O60"].mean(),
                 med=p["O30"].median(), fb=p["fb30"].mean(), fbI=p["fbI"].mean(),
                 O_nm=nm["O30"].mean(), n_nm=len(nm), OI_nm=nm["OI"].mean(), O_top5=top5,
                 beta=p["beta30"].quantile([0.1, 0.5, 0.9]).tolist(),
                 betaI=p["betaI"].quantile([0.1, 0.5, 0.9]).tolist(),
                 hedged=p["hedged"].mean())
        tab[h] = t
        print(f"-- {h}: n={t['n']} (с индексом {t['nI']})")
        print(f"   gross g {t['g']:+.4f} | издержки c {t['c']:.4f} | net {t['net']:+.4f}")
        print(f"   BTC β30: рынок M {t['M']:+.4f} [ДИ {ci_m[0]:+.3f}; {ci_m[1]:+.3f}] | "
              f"собственный O {t['O']:+.4f} [ДИ {ci_o[0]:+.3f}; {ci_o[1]:+.3f}] | медиана O "
              f"{t['med']:+.4f}")
        print(f"   соседи: O β=1 {t['O1']:+.4f} | O β14 {t['O14']:+.4f} | O β60 {t['O60']:+.4f}")
        print(f"   индекс: рынок M_I {t['MI']:+.4f} | собственный O_I {t['OI']:+.4f} "
              f"[ДИ {ci_i[0]:+.3f}; {ci_i[1]:+.3f}]")
        print(f"   без мем-монет (n={t['n_nm']}): O {t['O_nm']:+.4f}, O_I {t['OI_nm']:+.4f} | "
              f"без топ-5 по O: O {t['O_top5']:+.4f}")
        print(f"   β к BTC p10/50/90 {t['beta'][0]:.2f}/{t['beta'][1]:.2f}/{t['beta'][2]:.2f}, "
              f"фолбэк {t['fb']:.1%} | β к индексу p10/50/90 {t['betaI'][0]:.2f}/"
              f"{t['betaI'][1]:.2f}/{t['betaI'][2]:.2f}, фолбэк {t['fbI']:.1%}")
        print(f"   справочно: с хеджем BTC после издержек обеих ног {t['hedged']:+.4f}")
        for side in ("LONG", "SHORT"):
            q = p[p["side"] == side]
            qi = q[q["MI"].notna()]
            print(f"   {side:5s} n={len(q):4d} g {q['g'].mean():+.4f} | M {q['M30'].mean():+.4f} "
                  f"O {q['O30'].mean():+.4f} | M_I {qi['MI'].mean():+.4f} "
                  f"O_I {qi['OI'].mean():+.4f}")

    print("\n== Справочно: обе половины вместе")
    for name, hh in (("ротация", MAIN), ("9 монет", ("W1", "W2"))):
        p = df[df["half"].isin(hh)]
        ci_o = boot_ci(p["O30"].to_numpy(), p["day"].to_numpy())
        print(f"  {name}: n={len(p)} g {p['g'].mean():+.4f} c {p['c'].mean():.4f} | M "
              f"{p['M30'].mean():+.4f} O {p['O30'].mean():+.4f} [ДИ {ci_o[0]:+.3f}; "
              f"{ci_o[1]:+.3f}] | M_I {p['MI'].mean():+.4f} O_I {p['OI'].mean():+.4f}")

    # ---- решение (раздел 6)
    print("\n== Решение (rot H1 и rot H2′; порог — средние издержки C_h, рядом 0.15)")
    main_ok = all(tab[h]["O"] < tab[h]["c"] for h in MAIN)
    for h in MAIN:
        t = tab[h]
        print(f"  {h}: O {t['O']:+.4f} vs C {t['c']:.4f} → "
              f"{'O < C' if t['O'] < t['c'] else 'O ≥ C'}"
              f" | vs 0.15 → {'<' if t['O'] < TASK_THR else '≥'}")
    if main_ok:
        checks = {
            "верх ДИ O < C": all(tab[h]["ci_o"][1] < tab[h]["c"] for h in MAIN),
            "индекс O_I < C": all(tab[h]["OI"] < tab[h]["c"] for h in MAIN),
            "β14 и β60 O < C": all(tab[h][k] < tab[h]["c"] for h in MAIN for k in ("O14", "O60")),
            "без мемов O < C": all(tab[h]["O_nm"] < tab[h]["c"] for h in MAIN),
        }
        for name, v in checks.items():
            print(f"  {name}: {'да' if v else 'НЕТ'}")
        bad = [n for n, v in checks.items() if not v]
        print("  ВЕРДИКТ: закрыть фильтры и выбор монет — "
              + ("ТВЁРДО" if not bad else "по точечной оценке; не прошло: " + ", ".join(bad)))
    else:
        print("  ВЕРДИКТ: НЕ закрывать — собственный ход окупает издержки хотя бы на одной "
              "половине")
        for h in MAIN:
            if tab[h]["O"] >= tab[h]["c"]:
                print(f"  {h}: без топ-5 по O {tab[h]['O_top5']:+.4f} "
                      f"({'ниже' if tab[h]['O_top5'] < tab[h]['c'] else 'не ниже'} C)")
    w_hi = all(tab[h]["O"] >= tab[h]["c"] for h in ("W1", "W2"))
    print(f"  9 монет: O ≥ C на обеих половинах — {'ДА (отметить в реестре)' if w_hi else 'нет'}")

    # ---- справочно: монеты
    print("\n== Справочно: устойчивость собственного хода по монетам (ротация, ≥ 10 сделок)")
    a = df[df["half"] == "rot H1"].groupby("symbol")["O30"].agg(["mean", "count"])
    b = df[df["half"] == "rot H2'"].groupby("symbol")["O30"].agg(["mean", "count"])
    both = a[a["count"] >= 10].join(b[b["count"] >= 10], how="inner", lsuffix="1", rsuffix="2")
    if len(both) >= 4:
        rho, pv = spearman(both["mean1"].to_numpy(), both["mean2"].to_numpy())
        med = both["mean1"].median()
        hi_set, lo_set = both.index[both["mean1"] > med], both.index[both["mean1"] <= med]
        p2 = df[df["half"] == "rot H2'"]
        print(f"  монет в обеих половинах: {len(both)}; Спирмен H1→H2′ ρ={rho:+.3f}, p={pv:.3f}")
        print(f"  O в rot H2′: верхняя половина по H1 "
              f"{p2[p2['symbol'].isin(hi_set)]['O30'].mean():+.4f}"
              f" (n={int(p2['symbol'].isin(hi_set).sum())}), нижняя "
              f"{p2[p2['symbol'].isin(lo_set)]['O30'].mean():+.4f} "
              f"(n={int(p2['symbol'].isin(lo_set).sum())})")
    else:
        print(f"  монет в обеих половинах {len(both)} — мало для корреляции")
    for h in HALVES:
        p = df[df["half"] == h]
        nc, tv = tau(p, "O30")
        nci, tvi = tau(p[p["MI"].notna()], "OI")
        print(f"  {h:8s} разброс истинных средних O по монетам: τ={tv:.3f} (монет {nc}) → "
              f"отбор лучшей половины задним числом ≈ +{0.8 * tv:.3f}R | индекс τ={tvi:.3f}")
    print("\n== Справочно: 9 монет по монетам (BTC β30)")
    for h in ("W1", "W2"):
        p = df[df["half"] == h]
        g = p.groupby("symbol").agg(n=("g", "size"), g=("g", "mean"), M=("M30", "mean"),
                                    O=("O30", "mean"), OI=("OI", "mean"))
        for sym, r in g.iterrows():
            print(f"  {h} {sym.split('/')[0]:9s} n={int(r['n']):4d} g {r['g']:+.3f} "
                  f"M {r['M']:+.3f} O {r['O']:+.3f} O_I {r['OI']:+.3f}")

    if args.out:
        with open(args.out, "wb") as f:
            pickle.dump({"df": df, "tab": tab}, f)
    lap(f"готово{' → ' + args.out if args.out else ''}")


if __name__ == "__main__":
    main()
