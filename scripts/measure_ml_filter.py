"""Замер №19: ИИ-фильтр сделок нынешней стратегии (docs/measurements/2026-11-11-ml-filter.md).

  DATA_ROOT="/Users/mac/bot ctypto/data" PYTHONDONTWRITEBYTECODE=1 nice -n 15 \
      "/Users/mac/bot ctypto/.venv-research/bin/python" -m scripts.measure_ml_filter \
      [--base-cache base_rows.pkl] [--out файл.pkl]
  ... -m scripts.measure_ml_filter --selftest       # проверка модели на синтетике, без сделок

Сделки — база E1m замера №15 (паритет с его таблицей). Признаки — только известное к моменту
решения t. Модель — HistGradientBoostingRegressor (раздел 4а регистрации), walk-forward по
месяцам: обучение на сделках того же набора с выходом раньше начала блока минус сутки. Подбор
доли взятых q — только на учебных половинах (rot H1, W1). Критерий — на rot H2′ и W2. Замороженный
участок ротации открывается один раз и только если пройден весь критерий.
"""
from __future__ import annotations

import argparse
import os
import pickle
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd

QS = (0.3, 0.5, 0.7)
LEARN, VALID = ("rot H1", "W1"), ("rot H2'", "W2")
DAY, HOUR = 86_400_000, 3_600_000
MIN_TRAIN, GAP_MS = 300, DAY
Y_CLIP = (-1.5, 4.0)
N_PLACEBO, P_MAX, MIN_SHARE = 2000, 0.01, 0.30
FROZEN_LO, FROZEN_HI = "2026-08-01", "2026-10-19"
MODEL = dict(loss="squared_error", max_iter=150, learning_rate=0.05, max_depth=2,
             min_samples_leaf=40, l2_regularization=5.0, max_bins=32, early_stopping=False,
             random_state=0)
SETUPS = ("LTF-BB", "LTF-OB", "6226")
FEATURES = (
    ["side"] + [f"setup_{s}" for s in SETUPS]
    + ["confidence", "rr", "bias", "trigger", "sweep", "confl", "stop_w", "stop_atr", "dist_R",
       "c_ret1", "c_ret4", "c_ret24", "c_vol24", "atr_rel", "age_d", "meme",
       "b_ret1", "b_ret4", "b_ret24", "b_vol24", "funding", "oi_chg24",
       "hour", "weekday", "open_all", "open_side"])


def model():
    from sklearn.ensemble import HistGradientBoostingRegressor
    return HistGradientBoostingRegressor(**MODEL)


# ---------------------------------------------------------------- признаки
def _arr(df: pd.DataFrame) -> tuple:
    ts = df["ts"].to_numpy(np.int64) if "ts" in df else \
        (pd.to_datetime(df["dt"], utc=True).astype("int64") // 1_000_000).to_numpy(np.int64)
    return ts, df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float)


def bar_feats(arrs: tuple, t_ms: int) -> dict:
    """Доходности, волатильность и ATR по барам с открытием раньше t (закрыты к решению)."""
    ts, hi, lo, cl = arrs
    k = int(np.searchsorted(ts, t_ms, "left"))           # бары [0, k) закрыты
    out = dict(close=np.nan, ret1=np.nan, ret4=np.nan, ret24=np.nan, vol24=np.nan, atr=np.nan,
               first=int(ts[0]) if len(ts) else t_ms)
    if k < 2:
        return out
    c = cl[k - 1]
    out["close"] = c
    for n in (1, 4, 24):
        if k - 1 - n >= 0:
            out[f"ret{n}"] = float(np.log(c / cl[k - 1 - n]))
    if k >= 26:
        r = np.diff(np.log(cl[k - 25:k]))
        out["vol24"] = float(r.std() * np.sqrt(24))
    if k >= 15:
        h, l_, pc = hi[k - 14:k], lo[k - 14:k], cl[k - 15:k - 1]
        tr = np.maximum(h - l_, np.maximum(np.abs(h - pc), np.abs(l_ - pc)))
        out["atr"] = float(tr.mean())
    return out


def deriv_series(root: str) -> tuple[dict, dict]:
    """{база монеты: (ts, ставка)} фандинга и {база: (ts, oi, шаг мс)} OI из кэшей."""
    fund, oi = {}, {}
    rf = os.path.join(root, "cache_research", "funding")
    for f in sorted(os.listdir(rf)) if os.path.isdir(rf) else []:
        base = f.split("USDT_")[0]
        d = pd.read_csv(os.path.join(rf, f))
        if f.endswith("_funding.csv"):
            fund[base] = (d["ts"].to_numpy(np.int64), d["rate"].to_numpy(float))
        elif f.endswith("_oi4h.csv"):
            oi[base] = (d["ts"].to_numpy(np.int64), d["oi"].to_numpy(float), 4 * HOUR)
    rd = os.path.join(root, "cache5y", "derivs")
    for f in sorted(os.listdir(rd)) if os.path.isdir(rd) else []:
        base = f.split("-USDT-USDT_")[0]
        d = pd.read_csv(os.path.join(rd, f))
        if f.endswith("_funding.csv") and base not in fund:
            fund[base] = (d["ts"].to_numpy(np.int64), d["rate"].to_numpy(float))
        elif f.endswith("_oi1h.csv") and base not in oi:
            oi[base] = (d["ts"].to_numpy(np.int64), d["oi"].to_numpy(float), HOUR)
    return fund, oi


def last_le(ts: np.ndarray, t: int) -> int:
    return int(np.searchsorted(ts, t, "right")) - 1


def build_features(rows: pd.DataFrame, sigs: dict, bars: dict, btc: tuple, fund: dict,
                   oi: dict) -> pd.DataFrame:
    """Признаки сделок одного набора. rows — сделки базы (dec_ms, key, symbol, side, entry,
    stop, t_f, exit_ms); sigs — {ключ сигнала: сигнал}; bars — {символ: массивы 1h}."""
    from scripts.research_aggr_clusters import is_meme

    fill = rows["t_f"].to_numpy(np.int64)
    exit_ = rows["exit_ms"].to_numpy(np.int64)
    side_arr = np.where(rows["side"].to_numpy() == "LONG", 1, -1)
    out = []
    for i, r in enumerate(rows.itertuples(index=False)):
        s = sigs[r.key[1:]]
        sd = 1.0 if r.side == "LONG" else -1.0
        t = int(r.dec_ms)
        c = bar_feats(bars[r.symbol], t)
        b = bar_feats(btc, t)
        risk = abs(r.entry - r.stop)
        base = r.symbol.split("/")[0]
        f = dict(side=sd, confidence=float(s["confidence"]), rr=float(s["rr"]),
                 bias=float(s["scores"].get("bias", np.nan)),
                 trigger=float(s["scores"].get("trigger", np.nan)),
                 sweep=float(s["scores"].get("sweep", np.nan)),
                 confl=float(s["scores"].get("confl", np.nan)),
                 stop_w=risk / r.entry,
                 stop_atr=risk / c["atr"] if c["atr"] and c["atr"] > 0 else np.nan,
                 dist_R=sd * (c["close"] - r.entry) / risk if risk else np.nan,
                 c_ret1=sd * c["ret1"], c_ret4=sd * c["ret4"], c_ret24=sd * c["ret24"],
                 c_vol24=c["vol24"],
                 atr_rel=c["atr"] / c["close"] if c["close"] else np.nan,
                 age_d=min((t - c["first"]) / DAY, 365.0),
                 meme=float(is_meme(base + "USDT")),
                 b_ret1=sd * b["ret1"], b_ret4=sd * b["ret4"], b_ret24=sd * b["ret24"],
                 b_vol24=b["vol24"], funding=np.nan, oi_chg24=np.nan,
                 hour=float((t // HOUR) % 24), weekday=float(pd.Timestamp(t, unit="ms").weekday()))
        for st in SETUPS:
            f[f"setup_{st}"] = float(s["setup"] == st)
        if base in fund:
            fts, rate = fund[base]
            j = last_le(fts, t)
            if j >= 0:
                f["funding"] = sd * float(rate[j])
        if base in oi:
            ots, val, step = oi[base]
            j = last_le(ots, t - step)                    # последний закрытый интервал
            j0 = last_le(ots, t - step - DAY)
            if j >= 0 and j0 >= 0 and val[j0] > 0:
                f["oi_chg24"] = float(val[j] / val[j0] - 1.0)
        open_ = (fill <= t) & (exit_ > t)
        f["open_all"] = float(open_.sum())
        f["open_side"] = float((open_ & (side_arr == side_arr[i])).sum())
        out.append(f)
    return pd.DataFrame(out, index=rows.index)[FEATURES]


# ---------------------------------------------------------------- walk-forward
def walk_forward(dec: np.ndarray, exit_: np.ndarray, X: np.ndarray, y: np.ndarray,
                 permute: bool = False) -> tuple[np.ndarray, dict]:
    """Предсказания вне выборки по месяцам. -> (предсказание или nan, {q: взята ли сделка})."""
    pred = np.full(len(y), np.nan)
    keep = {q: np.zeros(len(y), bool) for q in QS}
    months = pd.to_datetime(dec, unit="ms").to_period("M")       # время UTC, без пояса
    rng = np.random.default_rng(0)
    for m in sorted(set(months)):
        b_ms = int(m.start_time.tz_localize("UTC").value // 1_000_000)
        blk = np.nonzero(months == m)[0]
        tr = np.nonzero(exit_ < b_ms - GAP_MS)[0]
        if len(tr) < MIN_TRAIN:
            continue
        yt = np.clip(y[tr], *Y_CLIP)
        if permute:
            yt = rng.permutation(yt)
        mdl = model().fit(X[tr], yt)
        p_tr = mdl.predict(X[tr])
        pred[blk] = mdl.predict(X[blk])
        for q in QS:
            keep[q][blk] = pred[blk] >= np.quantile(p_tr, 1.0 - q)
    return pred, keep


def evaluate(net: np.ndarray, kept: np.ndarray, seed: int = 0) -> dict:
    """Метрики половины на оцениваемых сделках; плацебо — случайные подмножества того же размера."""
    n, k = len(net), int(kept.sum())
    out = dict(n=n, kept=k, share=k / n if n else np.nan, e_all=float(net.mean()) if n else np.nan,
               e_kept=float(net[kept].mean()) if k else np.nan, p=np.nan, e_wo5=np.nan)
    if k:
        srt = np.sort(net[kept])[::-1]
        out["e_wo5"] = float(srt[5:].mean()) if k > 5 else np.nan
        rng = np.random.default_rng(seed)
        draws = np.array([net[rng.choice(n, k, replace=False)].mean() for _ in range(N_PLACEBO)])
        out["p"] = float((draws >= out["e_kept"] - 1e-12).mean())
    return out


def fmt(e: dict) -> str:
    return (f"n={e['n']:4d} взято {e['kept']:4d} ({e['share']:.0%}) | все {e['e_all']:+.4f} | "
            f"взятые {e['e_kept']:+.4f} | без топ-5 {e['e_wo5']:+.4f} | плацебо p={e['p']:.4f}")


# ---------------------------------------------------------------- самопроверка
def selftest() -> None:
    """Раздел 4а: на синтетике модель находит заложенную зависимость, на шуме — нет, и
    воспроизводима. Сделок нет."""
    rng = np.random.default_rng(1)
    n = 2400
    dec = np.sort(rng.integers(0, 540, n)) * DAY + 1_700_000_000_000
    exit_ = dec + rng.integers(1, 9, n) * DAY
    X = rng.normal(size=(n, len(FEATURES)))
    X[rng.random(X.shape) < 0.1] = np.nan
    signal = 0.6 * np.nan_to_num(X[:, 5]) - 0.4 * (np.nan_to_num(X[:, 9]) > 0.5)
    for name, y in (("сигнал", signal + rng.normal(0, 1.0, n) - 0.1),
                    ("шум", rng.normal(0, 1.0, n) - 0.1)):
        pred, keep = walk_forward(dec, exit_, X, y)
        ev = ~np.isnan(pred)
        e = evaluate(y[ev], keep[0.5][ev])
        print(f"  {name}: {fmt(e)}")
    y = signal + rng.normal(0, 1.0, n)
    p1, _k1 = walk_forward(dec, exit_, X, y)
    p2, _k2 = walk_forward(dec, exit_, X, y)
    same = bool(np.array_equal(np.isnan(p1), np.isnan(p2)) and np.allclose(p1[~np.isnan(p1)],
                                                                           p2[~np.isnan(p2)]))
    print(f"  воспроизводимость: два прогона на одних данных совпадают — {same}")
    _pp, kp = walk_forward(dec, exit_, X, y, permute=True)
    ev = ~np.isnan(p1)
    print(f"  контроль утечки на синтетике (метки перемешаны): {fmt(evaluate(y[ev], kp[0.5][ev]))}")


# ---------------------------------------------------------------- данные
def load_sets(base_cache: str | None, procs: int) -> pd.DataFrame:
    """Сделки базы обоих наборов (решения < 2026-08-01) — из кэша №15 или прогоном."""
    import scripts.measure_market_own as mmo

    if base_cache and os.path.exists(base_cache):
        with open(base_cache, "rb") as f:
            got = pickle.load(f)
    else:
        with Pool(min(procs, 2), maxtasksperchild=1) as pool:
            got = pool.map(mmo.run_base, ("5y", "rot"))
        if base_cache:
            with open(base_cache, "wb") as f:
                pickle.dump(got, f)
    df = pd.DataFrame([r for _d, rr, _m in got for r in rr])
    df["half"] = [mmo.half_of(d, ms) for d, ms in zip(df["data"], df["dec_ms"])]
    return df


def parity(df: pd.DataFrame) -> bool:
    import scripts.measure_market_own as mmo
    import scripts.measure_trail_realism as mtr
    ok = True
    for h in mmo.HALVES:
        p = df[df["half"] == h]
        s = mtr.stats(list(zip(p["exit_ms"], p["g"], p["net"], p["ev"])))
        rn, re, rs = mmo.REF[h]
        good = (s["n"] == rn and f"{s['net']:+.4f}" == f"{re:+.4f}"
                and f"{s['sum']:+.1f}" == f"{rs:+.1f}")
        ok &= good
        print(f"  {h:8s} n={s['n']} E={s['net']:+.4f} Σ={s['sum']:+.1f} "
              f"(эталон {rn} / {re:+.4f} / {rs:+.1f}) {'OK' if good else 'РАСХОЖДЕНИЕ'}")
    return ok


def features_for(df: pd.DataFrame, data: str) -> pd.DataFrame:
    """Признаки сделок набора data: сигналы и бары — те же, что у симуляции."""
    import scripts.research_judge as rj
    from scripts.dump_history import cache_name

    root = os.environ.get("DATA_ROOT", "data")
    bars, sig_list = rj.load(data)
    sigs = {rj.key(s["symbol"], s["dt"], s["side"], s["setup"]): s for s in sig_list}
    arrs = {sym: _arr(d) for (sym, tf), d in bars.items() if tf == "1h"}
    btc = _arr(pd.read_csv(os.path.join(root, "cache5y", cache_name("BTC/USDT:USDT", "1h"))))
    fund, oi = deriv_series(root)
    part = df[df["data"] == data].sort_values("dec_ms")
    return build_features(part, sigs, arrs, btc, fund, oi)


def run_set(df: pd.DataFrame, data: str, X: pd.DataFrame) -> dict:
    part = df.loc[X.index]
    dec = part["dec_ms"].to_numpy(np.int64)
    exit_ = part["exit_ms"].to_numpy(np.int64)
    net = part["net"].to_numpy(float)
    pred, keep = walk_forward(dec, exit_, X.to_numpy(float), net)
    _pp, keep_perm = walk_forward(dec, exit_, X.to_numpy(float), net, permute=True)
    return dict(index=X.index, pred=pred, keep=keep, keep_perm=keep_perm, net=net,
                half=part["half"].to_numpy(), side=part["side"].to_numpy(),
                symbol=part["symbol"].to_numpy())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-cache", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--procs", type=int, default=2)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--open-frozen", action="store_true",
                    help="открыть замороженный участок, если пройден критерий (только с "
                         "согласия главной сессии)")
    a = ap.parse_args()
    t0 = time.time()

    def lap(msg: str) -> None:
        print(f"[{time.time() - t0:6.0f}s] {msg}", flush=True)

    if a.selftest:
        lap("== Самопроверка модели на синтетике (сделок нет)")
        selftest()
        return

    import scripts.research_judge as rj
    rj.rebind_data()
    lap("== Сделки базы и паритет с №15")
    df = load_sets(a.base_cache, a.procs)
    if not parity(df):
        print("СТОП: паритет")
        return
    res = {}
    for data in ("rot", "5y"):
        lap(f"== Признаки и walk-forward: {data}")
        X = features_for(df, data)
        print(f"  сделок {len(X)}; пустых значений по признакам: "
              + ", ".join(f"{c} {X[c].isna().mean():.0%}" for c in X.columns if X[c].isna().any()))
        res[data] = run_set(df, data, X)
        res[data]["X"] = X

    lap("== Оценка по половинам (только сделки с моделью; плацебо — 2000 подмножеств)")
    tab = {}
    for r in res.values():
        ev = ~np.isnan(r["pred"])
        for h in sorted(set(r["half"])):
            m = ev & (r["half"] == h)
            for q in QS:
                tab[(h, q)] = evaluate(r["net"][m], r["keep"][q][m])
                tab[(h, q, "perm")] = evaluate(r["net"][m], r["keep_perm"][q][m])
                print(f"  {h:8s} q={q:.1f} {fmt(tab[(h, q)])}")
            print(f"  {h:8s} оценивается {int(m.sum())} из {int((r['half'] == h).sum())} сделок")
    score = {q: float(np.mean([tab[(h, q)]["e_kept"] for h in LEARN])) for q in QS}
    q_sel = max(QS, key=lambda q: score[q])
    print("\n== Подбор q на учебных (rot H1, W1): "
          + ", ".join(f"q={q:.1f} {score[q]:+.4f}" for q in QS) + f" → q={q_sel:.1f}")

    print(f"\n== Критерий на проверочных (q={q_sel:.1f})")
    ok_all, leak = True, False
    for h in VALID:
        e, ep = tab[(h, q_sel)], tab[(h, q_sel, "perm")]
        c = [e["e_kept"] > 0, e["p"] < P_MAX, e["share"] >= MIN_SHARE,
             bool(e["e_wo5"] > 0)]
        ok_all &= all(c)
        leak |= ep["p"] < P_MAX
        print(f"  {h:8s} {fmt(e)} → пункты 1–4: " + "".join("+" if x else "-" for x in c))
        print(f"  {h:8s} перемешанные метки: {fmt(ep)}")
    if leak:
        print("  КОНТРОЛЬ УТЕЧКИ НЕ ПРОЙДЕН — вердикт inconclusive")
    verdict = ok_all and not leak
    print(f"  ВЕРДИКТ: {'КАНДИДАТ — замороженный участок' if verdict else 'НЕ КАНДИДАТ'}")

    print("\n== Справочно")
    for r in res.values():
        ev = ~np.isnan(r["pred"])
        meme = r["X"]["meme"].to_numpy(float) > 0
        for h in sorted(set(r["half"])):
            m = ev & (r["half"] == h)
            k = r["keep"][q_sel]
            nm = m & ~meme
            line = f"  {h:8s} без мемов: взятые {r['net'][nm & k].mean():+.4f} из " \
                   f"{int((nm & k).sum())}, все {r['net'][nm].mean():+.4f}"
            for sd in ("LONG", "SHORT"):
                ms_ = m & (r["side"] == sd)
                line += f" | {sd} взятые {r['net'][ms_ & k].mean():+.4f} " \
                        f"({int((ms_ & k).sum())}), все {r['net'][ms_].mean():+.4f}"
            print(line)
    importance(res)

    if verdict and a.open_frozen:
        frozen(df, q_sel, lap)
    elif verdict:
        print("\n== Критерий пройден: замороженный участок НЕ открыт — ждёт решения главной "
              "сессии (запуск с --open-frozen)")
    else:
        print("\n== Замороженный участок не открывался (критерий не пройден)")
    if a.out:
        with open(a.out, "wb") as f:
            pickle.dump({"tab": tab, "q": q_sel, "verdict": verdict,
                         "res": {d: {k: v for k, v in r.items() if k != "X"}
                                 for d, r in res.items()}}, f)
    lap("готово")


def importance(res: dict) -> None:
    """Справочно, по просьбе главной сессии: какие признаки модель вообще использует. Модель на
    учебной половине набора, перестановочная важность на ней же (не walk-forward, описание)."""
    from sklearn.inspection import permutation_importance

    for data, half in (("rot", "rot H1"), ("5y", "W1")):
        r = res[data]
        X = r["X"].to_numpy(float)
        tr = r["half"] == half
        y = np.clip(r["net"][tr], *Y_CLIP)
        mdl = model().fit(X[tr], y)
        pi = permutation_importance(mdl, X[tr], y, n_repeats=10, random_state=0,
                                    scoring="neg_mean_squared_error")
        order = np.argsort(pi.importances_mean)[::-1][:10]
        print(f"  важность признаков, {half} (рост ошибки при перестановке): "
              + ", ".join(f"{FEATURES[i]} {pi.importances_mean[i]:+.4f}" for i in order))


def frozen(df: pd.DataFrame, q_sel: float, lap) -> None:
    """Один прогон на замороженном участке ротации — только если пройден критерий."""
    import scripts.measure_market_own as mmo

    lap(f"== ЗАМОРОЖЕННЫЙ УЧАСТОК (один прогон): ротация {FROZEN_LO} ≤ t < {FROZEN_HI}")
    lo = int(pd.Timestamp(FROZEN_LO, tz="UTC").value // 1_000_000)
    hi = int(pd.Timestamp(FROZEN_HI, tz="UTC").value // 1_000_000)
    old = mmo.CUT_MS
    mmo.CUT_MS = hi                                     # воркер отдаёт решения < hi
    try:
        _d, rows, _m = mmo.run_base("rot")
    finally:
        mmo.CUT_MS = old
    fr = pd.DataFrame(rows)
    fr = fr[(fr["dec_ms"] >= lo) & (fr["dec_ms"] < hi)]
    fr["half"] = "frozen"
    base = df[df["data"] == "rot"]
    allr = pd.concat([base, fr], ignore_index=True).sort_values("dec_ms")
    X = features_for(allr.assign(data="rot"), "rot")
    r = run_set(allr.assign(data="rot"), "rot", X)
    m = (~np.isnan(r["pred"])) & (r["half"] == "frozen")
    e = evaluate(r["net"][m], r["keep"][q_sel][m])
    good = e["e_kept"] > 0 and e["e_kept"] > e["e_all"]
    print(f"  {fmt(e)} → {'ПРОШЁЛ' if good else 'НЕ ПРОШЁЛ'} (взятые > 0 и выше всех)")


if __name__ == "__main__":
    main()
