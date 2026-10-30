"""Замер «сезонность»: часы суток UTC, сессии, дни недели на перпах Bybit.

Регистрация до прогона — docs/measurements/2026-10-31-seasonality.md (коммит f0367f0).
Шаг 1 — карта (24 часа, 7 дней) с перестановочной поправкой на шум; шаг 2 — 9 вариантов
«держать в окне», выбор только на учёбе, издержки 0.15% на круг + фактический фандинг.

  nice -n 15 .venv/bin/python -m scripts.research_seasonality [--perms 1000] [--frozen]

Данные — кэш замера фандинга (1h-свечи и события фандинга), только чтение. Итоги — в
stdout (markdown) и data/cache_research/season/results.json (вне git).
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

SRC = Path("/Users/mac/bot ctypto/data/cache_research/funding")
OUT = Path("/Users/mac/bot ctypto/data/cache_research/season")
SYMS = ("BTC ETH SOL XRP DOGE ADA LINK AVAX DOT LTC BCH ATOM FIL ETC UNI NEAR AAVE TRX XLM OP "
        "ARB APT SUI 1000PEPE ZEC HYPE ONDO").split()

TRAIN = ("2021-01-01", "2024-01-01")
VALID = ("2024-01-01", "2026-08-01")
FROZEN = ("2026-08-01", "2026-10-30")
SEGMENTS = (("2024H1", "2024-01-01", "2024-07-01"), ("2024H2", "2024-07-01", "2025-01-01"),
            ("2025H1", "2025-01-01", "2025-07-01"), ("2025H2", "2025-07-01", "2026-01-01"),
            ("2026 янв–июл", "2026-01-01", "2026-08-01"))
COST = 2 * (0.00055 + 0.0002)          # круг: тейкер + проскальзывание на входе и выходе
HOUR_MS = 3_600_000
DOW = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _ts(s: str) -> int:
    return int(pd.Timestamp(s, tz="UTC").value // 1_000_000)


# ---------- данные ----------

def load_bars(sym: str) -> pd.DataFrame:
    df = pd.read_csv(SRC / f"{sym}USDT_kline1h.csv")
    df = df.drop_duplicates("ts").sort_values("ts").set_index("ts")
    return df[["open", "close"]].astype(float)


def load_funding(sym: str) -> tuple[np.ndarray, np.ndarray]:
    f = pd.read_csv(SRC / f"{sym}USDT_funding.csv").drop_duplicates("ts").sort_values("ts")
    return f["ts"].to_numpy(np.int64), f["rate"].to_numpy(float)


# ---------- шаг 1: карта ----------

def day_matrix(bars: pd.DataFrame, lo: str, hi: str) -> np.ndarray:
    """Полные UTC-дни участка → матрица (дни × 24) доходностей close/open − 1 по часу."""
    b = bars[(bars.index >= _ts(lo)) & (bars.index < _ts(hi))]
    r = (b["close"] / b["open"] - 1.0)
    day = r.index // (24 * HOUR_MS)
    hour = (r.index // HOUR_MS) % 24
    m = pd.DataFrame({"d": day, "h": hour, "r": r.to_numpy()}).pivot_table(
        index="d", columns="h", values="r")
    m = m.reindex(columns=range(24)).dropna()
    return m.to_numpy()


def week_matrix(bars: pd.DataFrame, lo: str, hi: str) -> np.ndarray:
    """Полные недели (пн–вс) участка → матрица (недели × 7) дневных доходностей."""
    b = bars[(bars.index >= _ts(lo)) & (bars.index < _ts(hi))]
    day = b.index // (24 * HOUR_MS)
    g = pd.DataFrame({"d": day, "o": b["open"].to_numpy(), "c": b["close"].to_numpy()})
    cnt = g.groupby("d").size()
    first = g.groupby("d")["o"].first()
    last = g.groupby("d")["c"].last()
    dr = (last / first - 1.0)[cnt == 24]
    dates = pd.to_datetime(dr.index * 86_400_000, unit="ms", utc=True)
    w = pd.DataFrame({"r": dr.to_numpy(), "wd": dates.weekday,
                      "wk": (dr.index.to_numpy() - 4) // 7})   # 1970-01-01 — четверг; пн → 4
    m = w.pivot_table(index="wk", columns="wd", values="r").reindex(columns=range(7)).dropna()
    return m.to_numpy()


def cell_t(m: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mu = m.mean(axis=0)
    sd = m.std(axis=0, ddof=1)
    return mu, mu / (sd / math.sqrt(m.shape[0]))


def perm_null(m: np.ndarray, reps: int, rng: np.random.Generator) -> dict:
    """Перестановки внутри строки (дня/недели): привязка к метке разрушена, волатильность
    строки сохранена. -> ожидаемое число |t|>2, 95-й перцентиль max|t|, p-значения."""
    _, t_obs = cell_t(m)
    cnt_obs = int((np.abs(t_obs) > 2).sum())
    max_obs = float(np.abs(t_obs).max())
    cnts, maxs = np.empty(reps), np.empty(reps)
    for i in range(reps):
        idx = np.argsort(rng.random(m.shape), axis=1)
        _, t = cell_t(np.take_along_axis(m, idx, axis=1))
        cnts[i] = (np.abs(t) > 2).sum()
        maxs[i] = np.abs(t).max()
    return {"cells": m.shape[1], "rows": m.shape[0], "cnt_obs": cnt_obs,
            "cnt_null_mean": float(cnts.mean()), "p_cnt": float((cnts >= cnt_obs).mean()),
            "max_obs": max_obs, "max_null_p95": float(np.percentile(maxs, 95)),
            "p_max": float((maxs >= max_obs).mean())}


def ew_bars(all_bars: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Равновесная корзина: доходность часа = среднее по доступным монетам; синтетические
    open=1, close=1+r — чтобы та же матрица дней/недель работала без изменений."""
    rs = pd.concat({s: b["close"] / b["open"] - 1.0 for s, b in all_bars.items()}, axis=1)
    r = rs.mean(axis=1, skipna=True)
    return pd.DataFrame({"open": 1.0, "close": 1.0 + r})


def ew_week_matrix(all_bars: dict[str, pd.DataFrame], lo: str, hi: str) -> np.ndarray:
    """Дни недели для корзины: среднее дневных доходностей монет (не сцепка часов)."""
    mats = []
    for b in all_bars.values():
        bb = b[(b.index >= _ts(lo)) & (b.index < _ts(hi))]
        if len(bb) < 24 * 14:
            continue
        day = bb.index // (24 * HOUR_MS)
        g = pd.DataFrame({"d": day, "o": bb["open"].to_numpy(), "c": bb["close"].to_numpy()})
        cnt = g.groupby("d").size()
        dr = (g.groupby("d")["c"].last() / g.groupby("d")["o"].first() - 1.0)[cnt == 24]
        mats.append(dr)
    dr = pd.concat(mats, axis=1).mean(axis=1, skipna=True)
    dates = pd.to_datetime(dr.index * 86_400_000, unit="ms", utc=True)
    w = pd.DataFrame({"r": dr.to_numpy(), "wd": dates.weekday,
                      "wk": (dr.index.to_numpy() - 4) // 7})
    return w.pivot_table(index="wk", columns="wd", values="r").reindex(
        columns=range(7)).dropna().to_numpy()


# ---------- шаг 2: стратегии ----------

@dataclass(frozen=True)
class Window:
    """Окно удержания: старт — час h (или день недели + час), длина в часах, шаг повторения."""
    name: str
    start_hour: int            # час UTC начала (0..23)
    length: int                # часов
    weekday: int | None = None  # 0=пн; None — каждый день
    side: int = 1              # +1 лонг, −1 шорт


def trades(bars: pd.DataFrame, fund: tuple[np.ndarray, np.ndarray], w: Window,
           side: int) -> pd.DataFrame:
    """Все появления окна: вход по open первого бара, выход по close последнего бара (= open
    следующего). Пропуск, если в окне нет хоть одного бара. -> exit_ts, gross, net."""
    idx = bars.index.to_numpy(np.int64)
    opens = bars["open"].to_numpy()
    closes = bars["close"].to_numpy()
    pos = {t: i for i, t in enumerate(idx)}
    hours = idx // HOUR_MS
    starts = idx[(hours % 24) == w.start_hour]
    if w.weekday is not None:
        wd = pd.to_datetime(starts, unit="ms", utc=True).weekday
        starts = starts[wd == w.weekday]
    ft, fr = fund
    cs = np.concatenate([[0.0], np.cumsum(fr)])
    rows = []
    for s in starts:
        i0 = pos[s]
        last = s + (w.length - 1) * HOUR_MS
        i1 = pos.get(last)
        if i1 is None or i1 - i0 != w.length - 1:
            continue
        entry, exit_ = opens[i0], closes[i1]
        gross = side * (exit_ / entry - 1.0)
        t_in, t_out = s, s + w.length * HOUR_MS
        lo = np.searchsorted(ft, t_in, "right")
        hi = np.searchsorted(ft, t_out, "left")
        inside = cs[hi] - cs[lo]           # лонг платит сумму ставок, шорт получает
        edge_cost = 0.0
        for t in (t_in, t_out):
            j = np.searchsorted(ft, t, "left")
            # событие ровно на границе окна — только если оно против позиции (консервативно)
            if j < len(ft) and ft[j] == t and side * fr[j] > 0:
                edge_cost += abs(fr[j])
        net = gross - COST - side * inside - edge_cost
        rows.append((t_out, gross, net))
    return pd.DataFrame(rows, columns=["exit_ts", "gross", "net"])


def daily_pnl(tr: pd.DataFrame, lo: str, hi: str) -> pd.Series:
    """Net-P&L по дням выхода (день последнего бара) на участке; дни без сделок — 0."""
    t = tr[(tr["exit_ts"] > _ts(lo)) & (tr["exit_ts"] <= _ts(hi))]
    d0, d1 = _ts(lo) // 86_400_000, _ts(hi) // 86_400_000
    s = np.zeros(d1 - d0)
    if len(t):
        d = (t["exit_ts"].to_numpy(np.int64) - 1) // 86_400_000 - d0
        np.add.at(s, d, t["net"].to_numpy())
    return pd.Series(s)


def stats(p: pd.Series, n_trades: int, mean_trade: float) -> dict:
    years = len(p) / 365.25
    eq = p.cumsum()
    dd = float((eq.cummax() - eq).max()) if len(eq) else 0.0
    sd = p.std(ddof=1)
    return {"n": int(n_trades), "mean_trade": mean_trade, "sum": float(p.sum()),
            "ann": float(p.sum() / years) if years else float("nan"),
            "sharpe": float(p.mean() / sd * math.sqrt(365)) if sd > 0 else float("nan"),
            "t": float(p.mean() / sd * math.sqrt(len(p))) if sd > 0 else float("nan"),
            "maxdd": dd}


def eval_variant(w: Window, data: dict, lo: str, hi: str) -> dict:
    out = {}
    pnls = {}
    for sym in ("BTC", "ETH"):
        tr = data[sym]["tr"][w.name]
        t = tr[(tr["exit_ts"] > _ts(lo)) & (tr["exit_ts"] <= _ts(hi))]
        p = daily_pnl(tr, lo, hi)
        pnls[sym] = p
        out[sym] = stats(p, len(t), float(t["net"].mean()) if len(t) else float("nan"))
        out[sym]["gross_trade"] = float(t["gross"].mean()) if len(t) else float("nan")
    port = 0.5 * pnls["BTC"] + 0.5 * pnls["ETH"]
    out["PORT"] = stats(port, out["BTC"]["n"] + out["ETH"]["n"], float("nan"))
    ew = data["EW"][w.name]
    t = ew[(ew["exit_ts"] > _ts(lo)) & (ew["exit_ts"] <= _ts(hi))]
    out["EW27_net_trade"] = float(t["net"].mean()) if len(t) else float("nan")
    return out


def ew_trades(all_tr: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Корзина для стратегии: на каждое появление окна — среднее net по доступным монетам."""
    df = pd.concat([t.assign(sym=s) for s, t in all_tr.items() if len(t)])
    g = df.groupby("exit_ts")[["gross", "net"]].mean().reset_index()
    return g


# ---------- main ----------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--perms", type=int, default=1000)
    ap.add_argument("--frozen", action="store_true", help="одна финальная оценка заморозки")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(20261031)
    bars = {s: load_bars(s) for s in SYMS}
    funds = {s: load_funding(s) for s in SYMS}
    res: dict = {"map": {}, "strategies": {}}

    # ---- шаг 1: карта ----
    ew = ew_bars(bars)
    objs = {"BTC": bars["BTC"], "ETH": bars["ETH"], "EW27": ew}
    print("# Шаг 1. Карта сезонности\n")
    for name, b in objs.items():
        res["map"][name] = {}
        prof = {}
        for per, (lo, hi) in (("учёба", TRAIN), ("проверка", VALID)):
            hm = day_matrix(b, lo, hi)
            wm = ew_week_matrix(bars, lo, hi) if name == "EW27" else week_matrix(b, lo, hi)
            hmu, ht = cell_t(hm)
            wmu, wt = cell_t(wm)
            prof[per] = (hmu, ht, wmu, wt)
            res["map"][name][per] = {
                "hour_mean_bp": (hmu * 1e4).round(2).tolist(), "hour_t": ht.round(2).tolist(),
                "dow_mean_bp": (wmu * 1e4).round(1).tolist(), "dow_t": wt.round(2).tolist(),
                "hour_null": perm_null(hm, a.perms, rng), "dow_null": perm_null(wm, a.perms, rng)}
        (h1, t1, w1, _), (h2, _, w2, _) = prof["учёба"], prof["проверка"]
        sig = np.abs(t1) > 2
        rep = {"hour_corr": float(np.corrcoef(h1, h2)[0, 1]),
               "hour_sign_agree": int((np.sign(h1) == np.sign(h2)).sum()),
               "hour_sig_train": int(sig.sum()),
               "hour_sig_same_sign_valid": int((np.sign(h1[sig]) == np.sign(h2[sig])).sum()),
               "dow_corr": float(np.corrcoef(w1, w2)[0, 1]),
               "dow_sign_agree": int((np.sign(w1) == np.sign(w2)).sum())}
        res["map"][name]["replication"] = rep
        print(f"## {name}")
        for per in ("учёба", "проверка"):
            m = res["map"][name][per]
            hn, dn = m["hour_null"], m["dow_null"]
            print(f"- {per}: часы |t|>2 — {hn['cnt_obs']} из 24 (шум ≈ {hn['cnt_null_mean']:.1f}, "
                  f"p={hn['p_cnt']:.3f}); max|t| {hn['max_obs']:.2f} (95% шума "
                  f"{hn['max_null_p95']:.2f}, p={hn['p_max']:.3f}); "
                  f"дни |t|>2 — {dn['cnt_obs']} из 7 "
                  f"(шум ≈ {dn['cnt_null_mean']:.2f}, p={dn['p_cnt']:.3f}); max|t| "
                  f"{dn['max_obs']:.2f} (95% шума {dn['max_null_p95']:.2f}, p={dn['p_max']:.3f})")
        print(f"- повторяемость: корр. профилей часов {rep['hour_corr']:+.2f}, знаки совпали "
              f"{rep['hour_sign_agree']}/24, значимых на учёбе {rep['hour_sig_train']} → "
              f"тот же знак на проверке {rep['hour_sig_same_sign_valid']}; "
              f"дни: корр. {rep['dow_corr']:+.2f}, знаки {rep['dow_sign_agree']}/7")
        for per in ("учёба", "проверка"):
            m = res["map"][name][per]
            print(f"  - {per}, часы (б.п., t): " + ", ".join(
                f"{h:02d}:{mu:+.1f}({t:+.1f})" for h, (mu, t) in
                enumerate(zip(m["hour_mean_bp"], m["hour_t"]))))
            print(f"  - {per}, дни (б.п., t): " + ", ".join(
                f"{DOW[i]}:{mu:+.0f}({t:+.1f})" for i, (mu, t) in
                enumerate(zip(m["dow_mean_bp"], m["dow_t"]))))
        print()

    # ---- шаг 2: выбор на учёбе ----
    cand: list[Window] = [Window(f"w{h:02d}+{L}", h, L) for L in (1, 2, 3, 4) for h in range(24)]
    sessions = [Window("S3 Азия 00–08", 0, 8), Window("S4 Европа 08–13", 8, 5),
                Window("S5 США 13–21", 13, 8), Window("S6 после США 21–24", 21, 3)]
    weekend = Window("S7 выходные пт21→вс22", 21, 49, weekday=4)
    days = [Window(f"day {DOW[d]}", 0, 24, weekday=d) for d in range(7)]
    allw = cand + sessions + [weekend] + days
    data: dict = {s: {"tr": {}} for s in SYMS}
    for w in allw:                       # выбор — только по BTC и ETH на учёбе
        for s in ("BTC", "ETH"):
            data[s]["tr"][w.name] = trades(bars[s], funds[s], w, +1)

    def train_gross(w: Window) -> float:
        vals = []
        for s in ("BTC", "ETH"):
            t = data[s]["tr"][w.name]
            t = t[(t["exit_ts"] > _ts(TRAIN[0])) & (t["exit_ts"] <= _ts(TRAIN[1]))]
            vals.append(t["gross"].mean())
        return float(np.mean(vals))

    tg = {w.name: train_gross(w) for w in allw}
    best_long = max(cand, key=lambda w: tg[w.name])
    best_short = min(cand, key=lambda w: tg[w.name])
    best_day = max(days, key=lambda w: tg[w.name])
    worst_day = min(days, key=lambda w: tg[w.name])
    sgn = lambda x: 1 if x > 0 else -1   # noqa: E731
    variants = [Window("S1 лучшее окно лонг " + best_long.name, best_long.start_hour,
                       best_long.length, side=+1),
                Window("S2 лучшее окно шорт " + best_short.name, best_short.start_hour,
                       best_short.length, side=-1)]
    variants += [Window(w.name, w.start_hour, w.length, w.weekday, sgn(tg[w.name]))
                 for w in sessions + [weekend]]
    variants += [Window("S8 лучший день " + best_day.name, 0, 24, best_day.weekday, +1),
                 Window("S9 худший день " + worst_day.name, 0, 24, worst_day.weekday, -1)]
    res["selection"] = {"train_gross_bp": {k: round(v * 1e4, 2) for k, v in tg.items()},
                        "variants": [(v.name, v.side) for v in variants]}

    # пересчёт сделок с выбранной стороной (фандинг зависит от стороны)
    for v in variants:
        for s in SYMS:
            data[s]["tr"][v.name] = trades(bars[s], funds[s], v, v.side)
    data["EW"] = {v.name: ew_trades({s: data[s]["tr"][v.name] for s in SYMS}) for v in variants}

    print("# Шаг 2. Стратегии (выбор на учёбе)\n")
    print("| вариант | сторона | учёба gross BTC+ETH, б.п./сд |")
    print("|---|---|---|")
    for v in variants:
        base = v.name
        key = next((w.name for w in allw if w.start_hour == v.start_hour and w.length == v.length
                    and w.weekday == v.weekday), None)
        print(f"| {base} | {'лонг' if v.side > 0 else 'шорт'} | "
              f"{tg.get(key, float('nan')) * 1e4:+.1f} |")
    print()

    rows = []
    for v in variants:
        r = {"train": eval_variant(v, data, *TRAIN), "valid": eval_variant(v, data, *VALID),
             "seg": {n: eval_variant(v, data, lo, hi) for n, lo, hi in SEGMENTS}}
        segs_ok = all(r["seg"][n][s]["sum"] > 0 for n, _, _ in SEGMENTS for s in ("BTC", "ETH"))
        p = r["valid"]["PORT"]
        r["criterion"] = {"segments_both_positive": segs_ok,
                          "ann_gt_10": p["ann"] > 0.10, "dd_lt_25": p["maxdd"] < 0.25,
                          "t_gt_2_54": p["t"] > 2.54}
        r["candidate"] = all(r["criterion"].values())
        res["strategies"][v.name] = r
        rows.append((v, r))

    print("| вариант | участок | BTC net б.п./сд (n) | ETH net б.п./сд (n) | портфель годовых | "
          "Шарп | просадка | t | EW27 net б.п./сд |")
    print("|---|---|---|---|---|---|---|---|---|")
    for v, r in rows:
        for per in ("train", "valid"):
            x = r[per]
            print(f"| {v.name} | {'учёба' if per == 'train' else 'проверка'} | "
                  f"{x['BTC']['mean_trade'] * 1e4:+.1f} ({x['BTC']['n']}) | "
                  f"{x['ETH']['mean_trade'] * 1e4:+.1f} ({x['ETH']['n']}) | "
                  f"{x['PORT']['ann'] * 100:+.1f}% | {x['PORT']['sharpe']:+.2f} | "
                  f"{x['PORT']['maxdd'] * 100:.1f}% | {x['PORT']['t']:+.2f} | "
                  f"{x['EW27_net_trade'] * 1e4:+.1f} |")
    print()
    print("| вариант | " + " | ".join(n for n, _, _ in SEGMENTS) + " | кандидат |")
    print("|---|" + "---|" * len(SEGMENTS) + "---|")
    for v, r in rows:
        cells = []
        for n, _, _ in SEGMENTS:
            b, e = r["seg"][n]["BTC"]["sum"], r["seg"][n]["ETH"]["sum"]
            cells.append(f"BTC {b * 100:+.1f}% / ETH {e * 100:+.1f}%")
        print(f"| {v.name} | " + " | ".join(cells) + f" | {'ДА' if r['candidate'] else 'нет'} |")
    print()

    # ---- заморозка: один раз ----
    chosen = max(rows, key=lambda vr: vr[1]["train"]["PORT"]["sharpe"])[0]
    finals = [chosen] + [v for v, r in rows if r["candidate"] and v is not chosen]
    res["frozen_chosen"] = [v.name for v in finals]
    if a.frozen:
        print("# Заморозка (одна оценка)\n")
        res["frozen"] = {}
        for v in finals:
            x = eval_variant(v, data, *FROZEN)
            res["frozen"][v.name] = x
            print(f"- {v.name}: BTC {x['BTC']['sum'] * 100:+.2f}% ({x['BTC']['n']} сд), "
                  f"ETH {x['ETH']['sum'] * 100:+.2f}% ({x['ETH']['n']} сд), портфель "
                  f"{x['PORT']['sum'] * 100:+.2f}%, EW27 {x['EW27_net_trade'] * 1e4:+.1f} б.п./сд")
    else:
        print(f"Для заморозки выбран(ы): {', '.join(res['frozen_chosen'])} — запуск с --frozen.")
    (OUT / ("results_frozen.json" if a.frozen else "results.json")).write_text(
        json.dumps(res, ensure_ascii=False, indent=1, default=float))


if __name__ == "__main__":
    main()
