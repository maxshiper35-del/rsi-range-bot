"""Замер №20, шаги 0–1: предсказывает ли позиционирование на Binance ход монеты за 24 часа
(docs/measurements/2026-11-11-binance-positioning.md).

  DATA_ROOT="/Users/mac/bot ctypto/data" .venv-research/bin/python \
      -m scripts.measure_binance_positioning universe
                       # вселенная по OI на 1-е число и задания сборщику (без доходностей)
  ... measure_binance_positioning features   # признаки и цели в кэш (доходности не печатаются)
  ... measure_binance_positioning run        # шаги 0 и 1: IC, спреды, издержки, вердикты

Читает кэш scripts/binance_metrics_fetch.py. Признаки — только по снимкам до t − 5 мин.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import numpy as np
import pandas as pd

TOP_N = 40
MIN_HIST_D = 31
LAG = pd.Timedelta(minutes=5)
Z_WIN = "30D"
COST_RT = 0.0015                       # тейкер 0.055% + проскальзывание 0.02% на сторону, за круг
MIN_COINS = 20
PARTS = {"учебный": ("2022-01-01", "2024-06-30"),
         "проверочный": ("2024-07-01", "2026-07-31"),
         "замороженный": ("2026-08-01", "2026-10-30")}
HALVES = (("2024H2", "2024-07-01", "2024-12-31"), ("2025H1", "2025-01-01", "2025-06-30"),
          ("2025H2", "2025-07-01", "2025-12-31"), ("2026H1", "2026-01-01", "2026-07-31"))
HYP = {"crowd": -1, "top_acc": 1, "top_pos": 1, "div": 1}
DESCR = ("oi24", "taker24")
PRICE = ("ret24", "ret7d", "vol7d", "fund_last")
GBM = dict(max_iter=200, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=200,
           l2_regularization=1.0, random_state=0)


def root() -> Path:
    return Path(os.environ.get("DATA_ROOT", "data")) / "cache_research" / "binance_metrics"


def index(sym: str) -> list[str]:
    p = root() / "index" / f"{sym}.txt"
    return sorted(p.read_text().split()) if p.exists() else []


# ---------- вселенная ----------

def universe() -> pd.DataFrame:
    rows = []
    for f in sorted((root() / "first").glob("*/*-metrics-*-01.csv")):
        sym = f.parent.name
        month = pd.Timestamp(f.stem.rsplit("-metrics-", 1)[1], tz="UTC")
        try:
            d = pd.read_csv(f, nrows=3)
        except (ValueError, OSError):
            continue
        if d.empty:
            continue
        have = index(sym)
        if not have or pd.Timestamp(have[0], tz="UTC") > month - pd.Timedelta(days=MIN_HIST_D):
            continue                                   # меньше 31 дня истории до даты отбора
        rows.append(dict(month=month, symbol=sym,
                         oi_value=float(d["sum_open_interest_value"].iloc[0])))
    u = pd.DataFrame(rows)
    u = u[(u["month"] >= "2022-01-01") & (u["month"] <= "2026-10-01")]
    u["rank"] = u.groupby("month")["oi_value"].rank(ascending=False, method="first")
    return u[u["rank"] <= TOP_N].sort_values(["month", "rank"]).reset_index(drop=True)


def cmd_universe(_a) -> None:
    u = universe()
    u.to_csv(root() / "universe.csv", index=False)
    print(f"месяцев: {u['month'].nunique()}, символов за всё время: {u['symbol'].nunique()}, "
          f"в месяц: {u.groupby('month').size().min()}–{u.groupby('month').size().max()}")
    jobs = []
    for sym, g in u.groupby("symbol"):
        for m in g["month"]:
            lo = (m - pd.Timedelta(days=MIN_HIST_D + 1)).date()
            hi = (m + pd.offsets.MonthEnd(0) + pd.Timedelta(days=2)).date()
            jobs.append((sym, lo, hi))
    jobs.sort()
    merged: list[list] = []
    for sym, lo, hi in jobs:                           # слить пересекающиеся окна символа
        if merged and merged[-1][0] == sym and lo <= merged[-1][2] + pd.Timedelta(days=1):
            merged[-1][2] = max(merged[-1][2], hi)
        else:
            merged.append([sym, lo, hi])
    (root() / "days_jobs.txt").write_text(
        "".join(f"{s} {lo.isoformat()} {hi.isoformat()}\n" for s, lo, hi in merged))
    (root() / "universe_symbols.txt").write_text("\n".join(sorted(u["symbol"].unique())) + "\n")
    print(f"окон для сборщика: {len(merged)} → days_jobs.txt; дней: "
          f"{sum((hi - lo).days + 1 for _, lo, hi in merged)}")


# ---------- признаки ----------

def load_metrics(sym: str) -> pd.DataFrame:
    files = sorted((root() / "days" / sym).glob(f"{sym}-metrics-*.csv"))
    parts = []
    for f in files:
        try:
            parts.append(pd.read_csv(f))
        except (ValueError, OSError):
            continue
    if not parts:
        return pd.DataFrame()
    d = pd.concat(parts, ignore_index=True)
    d["ts"] = pd.to_datetime(d["create_time"], utc=True)
    d = d.drop_duplicates("ts").sort_values("ts").set_index("ts")
    out = pd.DataFrame(index=d.index)
    with np.errstate(divide="ignore", invalid="ignore"):
        out["crowd"] = np.log(d["count_long_short_ratio"].astype(float))
        out["top_acc"] = np.log(d["count_toptrader_long_short_ratio"].astype(float))
        out["top_pos"] = np.log(d["sum_toptrader_long_short_ratio"].astype(float))
        oi = np.log(d["sum_open_interest"].astype(float))
        tk = np.log(d["sum_taker_long_short_vol_ratio"].astype(float))
    out = out.replace([np.inf, -np.inf], np.nan)
    oi = oi.replace([np.inf, -np.inf], np.nan)
    tk = tk.replace([np.inf, -np.inf], np.nan)
    # суточное изменение OI и среднее taker за 24 ч — по времени, а не по числу строк
    oi_prev = oi.reindex(oi.index - pd.Timedelta(hours=24), method="ffill",
                         tolerance=pd.Timedelta(minutes=10)).to_numpy()
    out["oi24"] = oi.to_numpy() - oi_prev
    out["taker24"] = tk.rolling("24h", min_periods=200).mean()
    return out


def zscore(s: pd.Series) -> pd.Series:
    m = s.rolling(Z_WIN, min_periods=2000).mean()
    sd = s.rolling(Z_WIN, min_periods=2000).std()
    return (s - m) / sd


def load_klines(sym: str) -> pd.DataFrame:
    parts = []
    for f in sorted((root() / "klines" / sym).glob("*.csv")):
        d = pd.read_csv(f, header=None)
        if isinstance(d.iloc[0, 0], str) and not str(d.iloc[0, 0]).isdigit():
            d = d.iloc[1:]                             # файлы с заголовком
        parts.append(d.iloc[:, :5].astype(float))
    if not parts:
        return pd.DataFrame()
    k = pd.concat(parts, ignore_index=True)
    k.columns = ["open_time", "open", "high", "low", "close"]
    k["ts"] = pd.to_datetime(k["open_time"].astype("int64"), unit="ms", utc=True)
    return k.drop_duplicates("ts").set_index("ts").sort_index()[["open", "close"]]


def load_funding(sym: str) -> pd.Series:
    parts = []
    for f in sorted((root() / "funding" / sym).glob("*.csv")):
        d = pd.read_csv(f)
        if "calc_time" not in d:                       # файл без заголовка
            d = pd.read_csv(f, header=None).iloc[:, [0, -1]]
            d.columns = ["calc_time", "last_funding_rate"]
        parts.append(d[["calc_time", "last_funding_rate"]])
    if not parts:
        return pd.Series(dtype=float)
    f = pd.concat(parts, ignore_index=True)
    f["ts"] = pd.to_datetime(f["calc_time"].astype("int64"), unit="ms", utc=True)
    return f.drop_duplicates("ts").set_index("ts").sort_index()["last_funding_rate"].astype(float)


def features_for(sym: str, times: pd.DatetimeIndex) -> pd.DataFrame:
    m = load_metrics(sym)
    k = load_klines(sym)
    if m.empty or k.empty:
        return pd.DataFrame()
    z = pd.DataFrame({c: zscore(m[c]) for c in ("crowd", "top_acc", "top_pos", "oi24", "taker24")})
    z["div"] = z["top_pos"] - z["crowd"]
    at = times - LAG                                   # последний снимок не позже t − 5 мин
    zf = z.reindex(at, method="ffill", tolerance=pd.Timedelta(minutes=10))
    zf.index = times
    out = zf.copy()
    c = k["close"]
    lc = np.log(c)
    close_t = lc.reindex(times - pd.Timedelta(hours=1))          # свеча, закрывшаяся в t
    out["ret24"] = close_t.to_numpy() - lc.reindex(times - pd.Timedelta(hours=25)).to_numpy()
    out["ret7d"] = close_t.to_numpy() - lc.reindex(times - pd.Timedelta(hours=169)).to_numpy()
    r1 = lc.diff()
    vol = r1.rolling(168, min_periods=150).std() * np.sqrt(24)
    out["vol7d"] = vol.reindex(times - pd.Timedelta(hours=1)).to_numpy()
    fu = load_funding(sym)
    if len(fu):
        out["fund_last"] = fu.reindex(times, method="ffill").to_numpy()
        cs = fu.cumsum()
        a = cs.reindex(times, method="ffill").fillna(0.0).to_numpy()
        b = cs.reindex(times + pd.Timedelta(hours=24), method="ffill").fillna(0.0).to_numpy()
        out["fund24"] = b - a                          # сумма ставок за (t, t + 24 ч]
    else:
        out["fund_last"] = np.nan
        out["fund24"] = 0.0
    o = np.log(k["open"]).reindex(times).to_numpy()
    e = lc.reindex(times + pd.Timedelta(hours=23)).to_numpy()   # свеча, закрывающаяся в t + 24 ч
    out["r24"] = e - o
    e8 = lc.reindex(times + pd.Timedelta(hours=7)).to_numpy()
    out["r8"] = e8 - o
    out["symbol"] = sym
    return out


def cmd_features(_a) -> None:
    u = pd.read_csv(root() / "universe.csv", parse_dates=["month"])
    frames = []
    for i, (sym, g) in enumerate(u.groupby("symbol"), 1):
        times = []
        for mth in g["month"]:
            end = mth + pd.offsets.MonthBegin(1)
            times.append(pd.date_range(mth, end - pd.Timedelta(hours=8), freq="8h", tz="UTC"))
        times = pd.DatetimeIndex(np.concatenate(times)).unique().sort_values()
        f = features_for(sym, times)
        if not f.empty:
            frames.append(f.reset_index(names="t"))
        if i % 20 == 0:
            print(f"  признаки: {i}/{u['symbol'].nunique()}", flush=True)
    df = pd.concat(frames, ignore_index=True)
    df.to_pickle(root() / "features.pkl")
    print(f"строк: {len(df)}; символов: {df['symbol'].nunique()}; без цели r24: "
          f"{int(df['r24'].isna().sum())}; без признаков: {int(df['crowd'].isna().sum())}")


# ---------- расчёт ----------

def spearman_by_t(df: pd.DataFrame, col: str, target: str) -> pd.Series:
    def ic(g: pd.DataFrame) -> float:
        g = g[[col, target]].dropna()
        if len(g) < MIN_COINS:
            return np.nan
        return g[col].rank().corr(g[target].rank())
    return df.groupby("t")[[col, target]].apply(ic).dropna()


def quintile_net(df: pd.DataFrame, col: str, sign: int) -> pd.DataFrame:
    """Спред пятых частей по знаку гипотезы, издержки по замене монет, фандинг."""
    rows, prev_l, prev_s, prev_t = [], set(), set(), None
    for t, g in df.groupby("t"):
        g = g[[col, "r24", "fund24", "symbol"]].dropna(subset=[col, "r24"])
        if len(g) < MIN_COINS:
            prev_t = None
            continue
        g = g.sort_values(col, ascending=sign < 0)    # первые — лонги
        q = max(1, len(g) // 5)
        lg, sh = g.iloc[:q], g.iloc[-q:]
        lset, sset = set(lg["symbol"]), set(sh["symbol"])
        cont = prev_t is not None and t - prev_t == pd.Timedelta(days=1)
        rl = 1 - len(lset & prev_l) / len(lset) if cont else 1.0
        rs = 1 - len(sset & prev_s) / len(sset) if cont else 1.0
        spread = lg["r24"].mean() - sh["r24"].mean()
        fund = -lg["fund24"].fillna(0).mean() + sh["fund24"].fillna(0).mean()
        cost = COST_RT * (rl + rs)
        rows.append(dict(t=t, spread=spread, cost=cost, fund=fund, net=spread - cost + fund))
        prev_l, prev_s, prev_t = lset, sset, t
    return pd.DataFrame(rows).set_index("t")


def block_ci(x: np.ndarray, block: int = 7, reps: int = 2000, seed: int = 0) -> tuple:
    if len(x) < block * 2:
        return np.nan, np.nan
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(len(x) / block))
    starts = rng.integers(0, len(x) - block + 1, size=(reps, nb))
    idx = (starts[..., None] + np.arange(block)).reshape(reps, -1)[:, :len(x)]
    m = x[idx].mean(1)
    return float(np.percentile(m, 5)), float(np.percentile(m, 95))


def tstat(s: pd.Series) -> float:
    return float(s.mean() / s.std() * np.sqrt(len(s))) if len(s) > 2 and s.std() > 0 else np.nan


def part(s: pd.Series | pd.DataFrame, name: str):
    lo, hi = PARTS[name]
    return s[(s.index >= pd.Timestamp(lo, tz="UTC")) & (s.index <= pd.Timestamp(hi, tz="UTC"))]


def report(daily: pd.DataFrame, col: str, sign: int, pred: bool = False) -> bool:
    ic = spearman_by_t(daily, col, "r24n")
    qn = quintile_net(daily, col, sign)
    print(f"\n== {col} (знак гипотезы {'+' if sign > 0 else '−'})")
    for name in ("учебный", "проверочный"):
        i, q = part(ic, name), part(qn, name)
        lo, hi = block_ci(q["net"].to_numpy())
        print(f"  {name}: IC {i.mean():+.4f} t {tstat(i):+.2f} (дней {len(i)}) | спред "
              f"{q['spread'].mean():+.3%} издержки {q['cost'].mean():.3%} фандинг "
              f"{q['fund'].mean():+.3%} net {q['net'].mean():+.3%} [ДИ {lo:+.3%}; {hi:+.3%}]")
    v = part(qn, "проверочный")
    halves = []
    for _h, a, b in HALVES:
        hq = v[(v.index >= pd.Timestamp(a, tz="UTC")) & (v.index <= pd.Timestamp(b, tz="UTC"))]
        halves.append(hq["net"].mean())
    print("  полугодия net: " + ", ".join(f"{h} {x:+.3%}" for (h, _, _), x in zip(HALVES, halves)))
    tr = part(ic, "учебный")
    if pred:
        tr = tr[tr.index >= pd.Timestamp("2023-01-01", tz="UTC")]
    va = part(ic, "проверочный")
    lo, _ = block_ci(v["net"].to_numpy())
    if pred:
        c1 = tr.mean() > 0 and tstat(tr) >= 2
    else:
        c1 = np.sign(tr.mean()) == sign and abs(tstat(tr)) >= 3
    checks = [c1, np.sign(va.mean()) == sign and abs(tstat(va)) >= 2,
              v["net"].mean() > 0 and lo > 0, sum(x > 0 for x in halves) >= 3]
    print("  пункты 1–4: " + " ".join("да" if c else "НЕТ" for c in checks)
          + f" → {'ЕСТЬ ЭФФЕКТ' if all(checks) else 'нет эффекта'}")
    return all(checks)


def walk_forward(daily: pd.DataFrame) -> pd.Series:
    from sklearn.ensemble import HistGradientBoostingRegressor
    cols = list(HYP) + list(DESCR) + list(PRICE)
    pred = pd.Series(np.nan, index=daily.index)
    q0 = pd.Timestamp("2023-01-01", tz="UTC")
    end = daily["t"].max()
    while q0 <= end:
        q1 = q0 + pd.offsets.QuarterBegin(1, startingMonth=1)
        tr = daily[(daily["t"] + pd.Timedelta(hours=24) < q0 - pd.Timedelta(days=1))
                   & daily["r24n"].notna()]
        te = daily[(daily["t"] >= q0) & (daily["t"] < q1)]
        if len(te) and len(tr) > 1000:
            m = HistGradientBoostingRegressor(**GBM).fit(tr[cols], tr["r24n"])
            pred.loc[te.index] = m.predict(te[cols])
        q0 = q1
    return pred


def cmd_run(_a) -> None:
    df = pd.read_pickle(root() / "features.pkl")
    u = pd.read_csv(root() / "universe.csv", parse_dates=["month"])
    print(f"строк признаков: {len(df)}, символов: {df['symbol'].nunique()}, "
          f"месяцев вселенной: {u['month'].nunique()}")
    daily = df[df["t"].dt.hour == 0].copy()
    daily["r24n"] = daily["r24"] - daily.groupby("t")["r24"].transform("mean")
    print(f"дней: {daily['t'].nunique()}; строк: {len(daily)}; без цели: "
          f"{int(daily['r24'].isna().sum())}")
    ok = {}
    for col, sign in HYP.items():
        ok[col] = report(daily, col, sign)
    for col in DESCR:
        ic = spearman_by_t(daily, col, "r24n")
        print(f"\n== {col} (описательно): IC учебный {part(ic, 'учебный').mean():+.4f} "
              f"(t {tstat(part(ic, 'учебный')):+.2f}), проверочный "
              f"{part(ic, 'проверочный').mean():+.4f} (t {tstat(part(ic, 'проверочный')):+.2f})")
    e8 = df.copy()
    e8["r8n"] = e8["r8"] - e8.groupby("t")["r8"].transform("mean")
    print("\n== справочно, горизонт 8 ч, шаг 8 ч (IC проверочный):")
    for col in HYP:
        ic = spearman_by_t(e8, col, "r8n")
        print(f"  {col}: {part(ic, 'проверочный').mean():+.4f} (t "
              f"{tstat(part(ic, 'проверочный')):+.2f})")
    daily["pred"] = walk_forward(daily)
    print("\n== Шаг 1: модель (градиентный бустинг, walk-forward по кварталам)")
    ok["модель"] = report(daily[daily["pred"].notna()], "pred", 1, pred=True)
    passed = [k for k, v in ok.items() if v]
    print(f"\n== ИТОГ: {'ЕСТЬ ЭФФЕКТ — ' + ', '.join(passed) if passed else 'НЕТ ЭФФЕКТА'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("universe", "features", "run"):
        sub.add_parser(c)
    a = ap.parse_args()
    {"universe": cmd_universe, "features": cmd_features, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    main()
