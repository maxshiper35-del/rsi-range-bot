"""Замер №27: шорт после объявления Binance о делистинге монеты
(docs/measurements/2026-11-14-delist-short.md).

  export DATA_ROOT="/Users/mac/bot ctypto/data"
  .venv/bin/python -m scripts.measure_delist_short fetch
  .venv/bin/python -m scripts.measure_delist_short run

fetch — объявления раздела Delisting CMS Binance (если кэша нет); события «Binance Will Delist …
        on DATE»; 1h-свечи и фандинг перпа монеты за [−181; +16] суток вокруг события и корзины
        крупных альтов за весь период (data.binance.vision, кэш общий с №20 и №23).
run   — одно правило: шорт по открытию первого часа не раньше 60 мин после публикации, выход в
        00:00 UTC даты делистинга, без стопа; плацебо «та же монета», рынок — корзина; шесть
        пунктов критерия; описательно — вход в час публикации, 1/3/7/14 суток, стоп +30%.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.binance_metrics_fetch import root as bm_root
from scripts.binance_metrics_fetch import run_calls
from scripts.measure_listing_short import TAKER, jobs_for, load_1h, load_funding

CMS = ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
       "?type=1&catalogId=161&pageNo={page}&pageSize=50")
STABLE = {"USDP", "SUSD", "RENBTC", "BUSD", "TUSD", "USDC", "PAX", "AEUR", "FDUSD", "WBTC"}
BASKET = ("ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT",
          "LTCUSDT", "AVAXUSDT", "DOTUSDT")
SLIP_THIN, SLIP_BIG = 0.0010, 0.0002
DELAY = pd.Timedelta(minutes=60)
PLAC_FROM, PLAC_TO, PLAC_MIN = 180, 30, 30
HALF = pd.Timestamp("2024-07-01", tz="UTC")         # половины по дате выхода: до 2024-06-30 и после
PAD = pd.Timedelta(days=16)


def droot() -> Path:
    p = Path(os.environ.get("DATA_ROOT", "data")) / "cache_research" / "delist"
    p.mkdir(parents=True, exist_ok=True)
    return p


def announcements() -> list[dict]:
    p = droot() / "announcements.json"
    if p.exists():
        return json.loads(p.read_text())
    out = []
    for page in range(1, 50):
        req = urllib.request.Request(CMS.format(page=page), headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        cats = (d.get("data") or {}).get("catalogs") or []
        arts = cats[0]["articles"] if cats else []
        if not arts:
            break
        out += [{"code": a["code"], "title": a["title"], "t": a["releaseDate"]} for a in arts]
        time.sleep(1.0)
    p.write_text(json.dumps(out, ensure_ascii=False))
    return out


def events() -> list[dict]:
    """Монеты из статей «Binance Will Delist … on DATE», у которых есть символ перпа в архиве."""
    syms = {p.stem for p in (bm_root() / "index").glob("*.txt")}
    out = []
    for a in announcements():
        m = re.match(r"^Binance Will Delist (.+?) on (\d{4}-\d{2}-\d{2})", a["title"])
        if not m:
            continue
        names = re.split(r",\s*|\s+and\s+|\s*&\s*", re.sub(r"\(.*?\)", "", m.group(1)))
        t0 = pd.Timestamp(int(a["t"]), unit="ms", tz="UTC")
        for tk in (x.strip() for x in names):
            if not tk or tk in STABLE:
                continue
            s = next((x for x in (f"{tk}USDT", f"1000{tk}USDT") if x in syms), None)
            if s:
                out.append(dict(ann=a["code"], t0=t0, token=tk, sym=s,
                                t_delist=pd.Timestamp(m.group(2), tz="UTC")))
    return out


def months(a: pd.Timestamp, b: pd.Timestamp) -> list[str]:
    pr = pd.period_range(a.strftime("%Y-%m"), b.strftime("%Y-%m"), freq="M")
    return pr.strftime("%Y-%m").tolist()


def ev_months(e: dict) -> list[str]:
    return months(e["t0"] - pd.Timedelta(days=PLAC_FROM + 1), e["t_delist"] + PAD)


def basket_months(ev: list[dict]) -> list[str]:
    a = min(e["t0"] for e in ev) - pd.Timedelta(days=1)
    return months(a, max(e["t_delist"] for e in ev) + PAD)


def cmd_fetch(_a) -> None:
    ev = events()
    print(f"событий (монета × статья) с символом перпа: {len(ev)}", flush=True)
    need: dict[str, set] = {}
    for e in ev:
        need.setdefault(e["sym"], set()).update(ev_months(e))
    for s in BASKET:
        need.setdefault(s, set()).update(basket_months(ev))
    calls = []
    for s, ms in sorted(need.items()):
        calls += jobs_for(s, sorted(ms))
    run_calls(calls)


def short_until(px: pd.DataFrame, fund: pd.Series, t_in: pd.Timestamp, t_out: pd.Timestamp,
                slip: float, stop: float | None = None) -> dict | None:
    """Шорт с открытия часа t_in до открытия часа t_out; стоп +stop по максимуму часа (исполнение
    на 1% хуже уровня). Перп закрыт раньше t_out — выход по последней цене; дыра — None."""
    w = px.loc[t_in:t_out]
    if not len(w) or w.index[0] != t_in:
        return None
    entry = w["open"].iloc[0]
    body = w.iloc[:-1] if w.index[-1] == t_out else w
    end_t, exit_px, how = None, None, "время"
    if stop is not None:
        lvl = entry * (1 + stop)
        hit = body.index[body["high"].to_numpy() >= lvl]
        if len(hit):
            end_t, exit_px, how = hit[0], max(body.at[hit[0], "open"], lvl) * 1.01, "стоп"
    if end_t is None:
        if w.index[-1] == t_out:
            end_t, exit_px = t_out, w.at[t_out, "open"]
        else:
            last = px.index[-1]
            if last > w.index[-1] or last >= t_out:
                return None                             # дыра в данных, а не конец истории
            end_t, exit_px, how = last, w["close"].iloc[-1], "перп закрыт"
    gross = (entry - exit_px) / entry
    f = fund.loc[(fund.index > t_in) & (fund.index <= end_t)]
    fr = 0.0
    if len(f):
        pf = px["close"].reindex(f.index.floor("h"), method="ffill").to_numpy()
        fr = float(np.nansum(f.to_numpy() * pf / entry))
    return dict(net=gross + fr - 2 * (TAKER + slip), gross=gross, fund=fr, how=how, t_exit=end_t)


def placebo(px: pd.DataFrame, fund: pd.Series, t_in: pd.Timestamp, hold: pd.Timedelta) -> float:
    vals = []
    for k in range(PLAC_TO, PLAC_FROM + 1):
        ti = t_in - pd.Timedelta(days=k)
        r = short_until(px, fund, ti, ti + hold, SLIP_THIN)
        if r is not None:
            vals.append(r["net"])
    return float(np.mean(vals)) if len(vals) >= PLAC_MIN else math.nan


def market(bk: dict, t_in: pd.Timestamp, t_out: pd.Timestamp) -> float:
    vals = []
    for px, fu in bk.values():
        r = short_until(px, fu, t_in, t_out.floor("h"), SLIP_BIG)
        if r is not None:
            vals.append(r["net"])
    return float(np.mean(vals)) if vals else math.nan


def cluster_ci(df: pd.DataFrame, col: str, reps: int = 2000) -> tuple[float, float]:
    g = [x[col].to_numpy() for _, x in df.groupby("ann")]
    if len(g) < 3:
        return math.nan, math.nan
    rng = np.random.default_rng(27)
    ms = [np.concatenate([g[i] for i in rng.integers(0, len(g), len(g))]).mean()
          for _ in range(reps)]
    return float(np.percentile(ms, 5)), float(np.percentile(ms, 95))


def build() -> pd.DataFrame:
    ev = events()
    bms = basket_months(ev)
    bk = {}
    for s in BASKET:
        px = load_1h(s, bms)
        if px is not None:
            bk[s] = (px, load_funding(s, bms))
    rows = []
    for e in ev:
        ms = ev_months(e)
        px = load_1h(e["sym"], ms)
        if px is None:
            continue
        fu = load_funding(e["sym"], ms)
        t_in = (e["t0"] + DELAY).ceil("h")
        t_out = e["t_delist"]
        r = short_until(px, fu, t_in, t_out, SLIP_THIN)
        if r is None:
            continue
        row = dict(e, t_in=t_in, **r)
        row["plac"] = placebo(px, fu, t_in, t_out - t_in)
        row["mkt"] = market(bk, t_in, r["t_exit"])
        r0 = short_until(px, fu, e["t0"].floor("h"), t_out, SLIP_THIN)
        row["net_pub"] = r0["net"] if r0 else math.nan
        for h in (1, 3, 7, 14):
            rh = short_until(px, fu, t_in, t_in + pd.Timedelta(days=h), SLIP_THIN)
            row[f"net_{h}d"] = rh["net"] if rh else math.nan
        rs = short_until(px, fu, t_in, t_out, SLIP_THIN, stop=0.30)
        row["net_stop30"] = rs["net"] if rs else math.nan
        rows.append(row)
    return pd.DataFrame(rows)


def cmd_run(_a) -> None:
    t = build()
    t.to_pickle(droot() / "trades.pkl")
    if not len(t):
        print("ИТОГ: мало событий (0)")
        return
    print(f"событий в выборке: {len(t)}, объявлений {t['ann'].nunique()}, "
          f"монет {t['token'].nunique()}; перп закрыт раньше даты делистинга: "
          f"{int((t['how'] == 'перп закрыт').sum())}")
    if len(t) < 30:
        print(f"ИТОГ: мало событий ({len(t)} < 30), вывода нет")
        return
    lo, hi = cluster_ci(t, "net")
    wo5 = t.drop(t["net"].nlargest(5).index)["net"].mean()
    by_ann = t.groupby("ann")["net"].sum()
    share = by_ann.max() / by_ann.sum() if by_ann.sum() > 0 else math.nan
    h1, h2 = t[t["t_exit"] < HALF], t[t["t_exit"] >= HALF]
    ok_p, ok_m = t["plac"].notna(), t["mkt"].notna()
    ex_p = (t.loc[ok_p, "net"] - t.loc[ok_p, "plac"]).mean()
    ex_m = (t.loc[ok_m, "net"] - t.loc[ok_m, "mkt"]).mean()
    print(f"  net {t['net'].mean():+.2%} на сделку [90% ДИ кластерами {lo:+.2%}; {hi:+.2%}], "
          f"медиана {t['net'].median():+.2%}, в плюсе {np.mean(t['net'] > 0):.0%}")
    print(f"  из них: цена {t['gross'].mean():+.2%}, фандинг {t['fund'].mean():+.2%}, "
          f"издержки {-2 * (TAKER + SLIP_THIN):+.2%}")
    print(f"  плацебо «та же монета» {t['plac'].mean():+.2%} (есть у {int(ok_p.sum())}), "
          f"сверх плацебо {ex_p:+.2%}")
    print(f"  рынок (корзина) {t['mkt'].mean():+.2%}, сверх рынка {ex_m:+.2%}")
    print(f"  без 5 лучших {wo5:+.2%}; лучшее объявление {share:.0%} суммы; "
          f"половины: до 2024-06 n {len(h1)} {h1['net'].mean():+.2%}, после n {len(h2)} "
          f"{h2['net'].mean():+.2%}")
    crit = {
        "1 net > 0 и нижняя граница ДИ > 0": t["net"].mean() > 0 and lo > 0,
        "2 сверх плацебо «та же монета» > 0": ex_p > 0,
        "3 сверх рынка > 0": ex_m > 0,
        "4 без 5 лучших net > 0": wo5 > 0,
        "5 лучшее объявление < 30% суммы": share < 0.30,
        "6 net > 0 в обеих половинах": h1["net"].mean() > 0 and h2["net"].mean() > 0,
    }
    for k, ok in crit.items():
        print(f"  {'✓' if ok else '✗'} {k}")
    print(f"\nИТОГ: {'КАНДИДАТ' if all(crit.values()) else 'не кандидат'}")
    print("\n== Описательно, не критерий")
    print(f"  вход в час публикации (недостижимо): {t['net_pub'].mean():+.2%}; "
          + ", ".join(f"{h} сут {t[f'net_{h}d'].mean():+.2%}" for h in (1, 3, 7, 14))
          + f"; стоп +30%: {t['net_stop30'].mean():+.2%}")
    for y, g in t.groupby(t["t_in"].dt.year):
        print(f"  {y}: n {len(g)}, net {g['net'].mean():+.2%}, сверх рынка "
              f"{(g['net'] - g['mkt']).mean():+.2%}")
    worst = t.nsmallest(3, "net")[["token", "t_in", "net", "fund"]]
    best = t.nlargest(3, "net")[["token", "t_in", "net", "fund"]]
    print("  худшие:", "; ".join(f"{r.token} {r.t_in:%Y-%m-%d} {r.net:+.0%} (фандинг {r.fund:+.0%})"
                                 for r in worst.itertuples()))
    print("  лучшие:", "; ".join(f"{r.token} {r.t_in:%Y-%m-%d} {r.net:+.0%} (фандинг {r.fund:+.0%})"
                                 for r in best.itertuples()))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    sub.add_parser("run")
    a = ap.parse_args()
    {"fetch": cmd_fetch, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    main()
