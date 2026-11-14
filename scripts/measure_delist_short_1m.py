"""Замер №28: шорт после делистинга со стопом +30% — исполнение по минутным свечам
(docs/measurements/2026-11-14-delist-short-1m.md). События и сравнения — из №27.

  export DATA_ROOT="/Users/mac/bot ctypto/data"
  .venv/bin/python -m scripts.measure_delist_short_1m fetch
  .venv/bin/python -m scripts.measure_delist_short_1m run

fetch — 1m-свечи перпа за месяцы от входа до даты делистинга (data.binance.vision).
run   — вход как в №27, стоп +30% на первой минуте с максимумом ≥ уровня; строгое исполнение —
        по максимуму этой минуты (критерий), обычное — уровень или открытие минуты +0.5%
        (описательно); плацебо и рынок — по 1h со стопом, как в №27; семь пунктов критерия.
"""
from __future__ import annotations

import argparse
import math

import numpy as np
import pandas as pd

from scripts import measure_delist_short as d
from scripts.binance_metrics_fetch import HOST, fetch, run_calls
from scripts.binance_metrics_fetch import root as bm_root
from scripts.measure_listing_short import TAKER, load_1h, load_funding

STOP = 0.30
SLIP = d.SLIP_THIN
SOFT_EXTRA = 0.005
TOL = pd.Timedelta(minutes=5)


def mfile(s: str, ym: str):
    return bm_root() / "klines_1m" / s / f"{s}-1m-{ym}.csv"


def ev_months_1m(e: dict) -> list[str]:
    return d.months(e["t0"], e["t_delist"] + pd.Timedelta(days=1))


def cmd_fetch(_a) -> None:
    calls = []
    for e in d.events():
        for ym in ev_months_1m(e):
            s = e["sym"]
            calls.append(lambda s=s, ym=ym: fetch(
                f"{HOST}/monthly/klines/{s}/1m/{s}-1m-{ym}.zip", mfile(s, ym)))
    run_calls(calls)


def load_1m(s: str, months: list[str]) -> pd.DataFrame | None:
    parts = []
    for ym in months:
        p = mfile(s, ym)
        if p.exists():
            x = pd.read_csv(p, header=None, usecols=range(5),
                            names=["t", "open", "high", "low", "close"])
            x = x[pd.to_numeric(x["t"], errors="coerce").notna()].astype(float)
            t = x["t"].astype("int64")
            t = t.where(t < 10 ** 14, t // 1000)        # с 2025 года архив в микросекундах
            x["t"] = pd.to_datetime(t, unit="ms", utc=True)
            parts.append(x)
    if not parts:
        return None
    return pd.concat(parts).drop_duplicates("t").sort_values("t").set_index("t")


def short_1m(m1: pd.DataFrame, fund: pd.Series, t_in: pd.Timestamp, t_out: pd.Timestamp,
             fill: str) -> dict | None:
    """Шорт по открытию минуты t_in (или первой в пределах TOL) до открытия минуты t_out; стоп
    +STOP на первой минуте с high ≥ уровня. fill: 'high' — по максимуму минуты срабатывания,
    'soft' — уровень или открытие минуты, если оно выше, плюс SOFT_EXTRA."""
    w = m1.loc[t_in:t_out]
    if not len(w) or w.index[0] - t_in > TOL:
        return None
    t_ent, entry = w.index[0], w["open"].iloc[0]
    lvl = entry * (1 + STOP)
    body = w.iloc[:-1] if w.index[-1] == t_out else w
    hit = body.index[body["high"].to_numpy() >= lvl]
    if len(hit):
        end_t, how = hit[0], "стоп"
        bar = body.loc[end_t]
        exit_px = bar["high"] if fill == "high" else max(lvl, bar["open"]) * (1 + SOFT_EXTRA)
    elif w.index[-1] == t_out:
        end_t, exit_px, how = t_out, w.at[t_out, "open"], "время"
    else:
        last = m1.index[-1]
        if last > w.index[-1] + pd.Timedelta(hours=1) or last >= t_out:
            return None                                  # дыра в данных, а не конец истории
        end_t, exit_px, how = w.index[-1], w["close"].iloc[-1], "перп закрыт"
    gross = (entry - exit_px) / entry
    f = fund.loc[(fund.index > t_ent) & (fund.index <= end_t)]
    fr = 0.0
    if len(f):
        pf = m1["close"].reindex(f.index.floor("min"), method="ffill").to_numpy()
        fr = float(np.nansum(f.to_numpy() * pf / entry))
    return dict(net=gross + fr - 2 * (TAKER + SLIP), gross=gross, fund=fr, how=how, t_exit=end_t,
                worst=(entry - body["high"].max()) / entry if len(body) else 0.0)


def placebo_stop(px: pd.DataFrame, fund: pd.Series, t_in: pd.Timestamp, hold: pd.Timedelta) -> float:  # noqa: E501
    vals = []
    for k in range(d.PLAC_TO, d.PLAC_FROM + 1):
        ti = t_in - pd.Timedelta(days=k)
        r = d.short_until(px, fund, ti, ti + hold, SLIP, stop=STOP)
        if r is not None:
            vals.append(r["net"])
    return float(np.mean(vals)) if len(vals) >= d.PLAC_MIN else math.nan


def market_stop(bk: dict, t_in: pd.Timestamp, t_out: pd.Timestamp) -> float:
    vals = []
    for px, fu in bk.values():
        r = d.short_until(px, fu, t_in, t_out.floor("h"), d.SLIP_BIG, stop=STOP)
        if r is not None:
            vals.append(r["net"])
    return float(np.mean(vals)) if vals else math.nan


def build() -> pd.DataFrame:
    ev = d.events()
    bms = d.basket_months(ev)
    bk = {}
    for s in d.BASKET:
        px = load_1h(s, bms)
        if px is not None:
            bk[s] = (px, load_funding(s, bms))
    rows = []
    for e in ev:
        m1 = load_1m(e["sym"], ev_months_1m(e))
        if m1 is None:
            continue
        ms = d.ev_months(e)
        fu = load_funding(e["sym"], ms)
        t_in = (e["t0"] + d.DELAY).ceil("h")
        t_out = e["t_delist"]
        r = short_1m(m1, fu, t_in, t_out, "high")
        if r is None:
            continue
        soft = short_1m(m1, fu, t_in, t_out, "soft")
        row = dict(e, t_in=t_in, **r, net_soft=soft["net"])
        px = load_1h(e["sym"], ms)
        row["plac"] = placebo_stop(px, fu, t_in, t_out - t_in) if px is not None else math.nan
        row["mkt"] = market_stop(bk, t_in, t_out)
        rows.append(row)
    return pd.DataFrame(rows)


def cmd_run(_a) -> None:
    t = build()
    t.to_pickle(d.droot() / "trades_1m.pkl")
    n_ev = len(d.events())
    print(f"событий с минутными свечами: {len(t)} из {n_ev}, объявлений {t['ann'].nunique()}")
    if len(t) < 30:
        print("ИТОГ: мало событий, вывода нет")
        return
    st = t["how"] == "стоп"
    lo, hi = d.cluster_ci(t, "net")
    wo5 = t.drop(t["net"].nlargest(5).index)["net"].mean()
    by_ann = t.groupby("ann")["net"].sum()
    share = by_ann.max() / by_ann.sum() if by_ann.sum() > 0 else math.nan
    order = t.groupby("ann")["t0"].first().sort_values().index.tolist()
    h1, h2 = t[t["ann"].isin(order[:14])], t[t["ann"].isin(order[-14:])]
    ok_p, ok_m = t["plac"].notna(), t["mkt"].notna()
    ex_p = (t.loc[ok_p, "net"] - t.loc[ok_p, "plac"]).mean()
    ex_m = (t.loc[ok_m, "net"] - t.loc[ok_m, "mkt"]).mean()
    print(f"  строгое исполнение: net {t['net'].mean():+.2%} на сделку ({t['net'].mean() / STOP:+.2f}R) "  # noqa: E501
          f"[90% ДИ кластерами {lo:+.2%}; {hi:+.2%}], медиана {t['net'].median():+.2%}, "
          f"в плюсе {np.mean(t['net'] > 0):.0%}")
    print(f"  стоп в {int(st.sum())} из {len(t)}; средний net стопа {t.loc[st, 'net'].mean():+.2%}, "  # noqa: E501
          f"худшая сделка {t['net'].min():+.2%}; фандинг {t['fund'].mean():+.2%}")
    print(f"  плацебо «та же монета» {t['plac'].mean():+.2%}, сверх плацебо {ex_p:+.2%}; "
          f"рынок {t['mkt'].mean():+.2%}, сверх рынка {ex_m:+.2%}")
    print(f"  без 5 лучших {wo5:+.2%}; лучшее объявление {share:.0%} суммы; первые 14 объявлений "
          f"n {len(h1)} {h1['net'].mean():+.2%}, последние 14 n {len(h2)} {h2['net'].mean():+.2%}")
    crit = {
        "1 net > 0 и нижняя граница ДИ > 0": t["net"].mean() > 0 and lo > 0,
        "2 сверх плацебо «та же монета» > 0": ex_p > 0,
        "3 сверх рынка > 0": ex_m > 0,
        "4 без 5 лучших net > 0": wo5 > 0,
        "5 лучшее объявление < 30% суммы": share < 0.30,
        "6 net > 0 в обеих половинах по порядку объявлений": h1["net"].mean() > 0
        and h2["net"].mean() > 0,
        "7 худшая сделка не хуже −60%": t["net"].min() >= -0.60,
    }
    for k, ok in crit.items():
        print(f"  {'✓' if ok else '✗'} {k}")
    verdict = "исполнение эффект не убивает" if all(crit.values()) else "направление закрыто"
    print(f"\nИТОГ: {verdict}")
    print("\n== Описательно, не критерий")
    print(f"  обычное исполнение (уровень или открытие минуты +0.5%): net {t['net_soft'].mean():+.2%}, "  # noqa: E501
          f"худшая {t['net_soft'].min():+.2%}")
    for y, g in t.groupby(t["t_in"].dt.year):
        print(f"  {y}: n {len(g)}, net {g['net'].mean():+.2%}, стопов {int((g['how'] == 'стоп').sum())}")  # noqa: E501
    w = t[st].nsmallest(5, "net")
    print("  худшие стопы: " + "; ".join(f"{r.token} {r.t_in:%Y-%m-%d} {r.net:+.1%}"
                                         for r in w.itertuples()))
    hrs = (t.loc[st, "t_exit"] - t.loc[st, "t_in"]).dt.total_seconds() / 3600
    print(f"  до стопа: медиана {hrs.median():.0f} ч, в первые сутки {int((hrs <= 24).sum())} "
          f"из {int(st.sum())}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    sub.add_parser("run")
    a = ap.parse_args()
    {"fetch": cmd_fetch, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    main()
