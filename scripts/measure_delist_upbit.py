"""Замер №29: правило №28 (шорт после делистинга, стоп +30%, исполнение по 1m) на объявлениях Upbit
(docs/measurements/2026-11-14-delist-upbit.md).

  export DATA_ROOT="/Users/mac/bot ctypto/data"
  .venv/bin/python -m scripts.measure_delist_upbit fetch
  .venv/bin/python -m scripts.measure_delist_upbit run

fetch — 1m-свечи перпа Binance за окно сделки и 1h-свечи с фандингом за полгода до события.
run   — вход и стоп как в №28; выход — прекращение торгов на Upbit, но не позже 14 суток; шесть
        пунктов критерия; сравнения — функции №28.
"""
from __future__ import annotations

import argparse
import json
import math
import re

import numpy as np
import pandas as pd

from scripts import measure_delist_short as d
from scripts import measure_delist_short_1m as m
from scripts.binance_metrics_fetch import HOST, fetch, run_calls
from scripts.binance_metrics_fetch import root as bm_root
from scripts.measure_listing_short import jobs_for, load_1h, load_funding

SKIP = ("변경", "정정", "연기", "출금", "유지")
CAP = pd.Timedelta(days=14)
NEAR_BN = pd.Timedelta(days=60)
DEDUP = pd.Timedelta(days=90)
DATA_END = pd.Timestamp("2026-10-31 23:00", tz="UTC")     # край месячного архива


def binance_delist_times() -> dict[str, list[pd.Timestamp]]:
    out: dict[str, list] = {}
    for a in d.announcements():
        mt = re.match(r"^Binance Will Delist (.+?) on (\d{4}-\d{2}-\d{2})", a["title"])
        if mt:
            for tk in re.split(r",\s*|\s+and\s+|\s*&\s*", re.sub(r"\(.*?\)", "", mt.group(1))):
                out.setdefault(tk.strip(), []).append(pd.Timestamp(int(a["t"]), unit="ms", tz="UTC"))  # noqa: E501
    return out


def events() -> list[dict]:
    notices = json.loads((d.droot() / "upbit_trade_notices.json").read_text())
    syms = {p.stem for p in (bm_root() / "index").glob("*.txt")}
    bn = binance_delist_times()
    raw = []
    for n in notices:
        title = n["title"]
        if "거래지원 종료 안내" not in title or any(w in title for w in SKIP):
            continue
        ticks = re.findall(r"\(([A-Z0-9]{2,10})\)", title)
        md = re.search(r"\((\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\)", title)
        if len(ticks) != 1 or not md:
            continue
        t0 = pd.Timestamp(n["first_listed_at"]).tz_convert("UTC")
        kst = pd.Timestamp(n["first_listed_at"])
        mo, day, hh, mi = (int(x) for x in md.groups())
        end = pd.Timestamp(year=kst.year, month=mo, day=day, hour=hh, minute=mi, tz=kst.tz)
        if end < kst:
            end = end.replace(year=kst.year + 1)
        raw.append(dict(ann=str(n["id"]), t0=t0, token=ticks[0], t_end=end.tz_convert("UTC")))
    out, last = [], {}
    for e in sorted(raw, key=lambda x: x["t0"]):
        tk = e["token"]
        if tk in last and e["t0"] - last[tk] <= DEDUP:
            continue
        last[tk] = e["t0"]
        if any(abs(e["t0"] - b) <= NEAR_BN for b in bn.get(tk, [])):
            continue
        s = next((x for x in (f"{tk}USDT", f"1000{tk}USDT") if x in syms), None)
        if not s:
            continue
        t_in = (e["t0"] + d.DELAY).ceil("h")
        t_out = min(e["t_end"].floor("min"), t_in + CAP)
        if t_out > DATA_END:
            continue
        out.append(dict(e, sym=s, t_in=t_in, t_out=t_out))
    return out


def months_1h(e: dict) -> list[str]:
    return d.months(e["t0"] - pd.Timedelta(days=d.PLAC_FROM + 1), e["t_out"] + pd.Timedelta(days=1))


def cmd_fetch(_a) -> None:
    ev = events()
    print(f"событий Upbit с символом перпа, вне окна Binance: {len(ev)}", flush=True)
    calls = []
    for e in ev:
        s = e["sym"]
        for ym in d.months(e["t0"], e["t_out"]):
            calls.append(lambda s=s, ym=ym: fetch(
                f"{HOST}/monthly/klines/{s}/1m/{s}-1m-{ym}.zip", m.mfile(s, ym)))
        calls += jobs_for(s, months_1h(e))
    run_calls(calls)


def build() -> pd.DataFrame:
    ev = events()
    if not ev:
        return pd.DataFrame()
    a = min(e["t0"] for e in ev) - pd.Timedelta(days=1)
    bms = d.months(a, max(e["t_out"] for e in ev) + pd.Timedelta(days=1))
    bk = {}
    for s in d.BASKET:
        px = load_1h(s, bms)
        if px is not None:
            bk[s] = (px, load_funding(s, bms))
    rows, dead = [], []
    for e in ev:
        m1 = m.load_1m(e["sym"], d.months(e["t0"], e["t_out"]))
        if m1 is None:
            continue
        if not m.alive(m1, e["t_in"]):
            dead.append(e["token"])
            continue
        fu = load_funding(e["sym"], months_1h(e))
        r = m.short_1m(m1, fu, e["t_in"], e["t_out"], "high")
        if r is None:
            continue
        row = dict(e, **r)
        px = load_1h(e["sym"], months_1h(e))
        row["plac"] = (m.placebo_stop(px, fu, e["t_in"], e["t_out"] - e["t_in"])
                       if px is not None else math.nan)
        row["mkt"] = m.market_stop(bk, e["t_in"], e["t_out"])
        rows.append(row)
    out = pd.DataFrame(rows)
    out.attrs["dead"] = dead
    return out


def cmd_run(_a) -> None:
    n_ev = len(events())
    t = build()
    if len(t):
        t.to_pickle(d.droot() / "trades_upbit.pkl")
    dead = t.attrs.get("dead", [])
    print(f"событий с живым перпом и минутными свечами: {len(t)} из {n_ev}; перп уже не "
          f"торговался: {len(dead)} ({', '.join(dead)})")
    if len(t) < 15:
        print(f"ИТОГ: мало событий ({len(t)} < 15), вывода нет")
        return
    st = t["how"] == "стоп"
    lo, hi = d.cluster_ci(t, "net")
    wo3 = t.drop(t["net"].nlargest(3).index)["net"].mean()
    ok_p, ok_m = t["plac"].notna(), t["mkt"].notna()
    ex_p = (t.loc[ok_p, "net"] - t.loc[ok_p, "plac"]).mean()
    ex_m = (t.loc[ok_m, "net"] - t.loc[ok_m, "mkt"]).mean()
    print(f"  net {t['net'].mean():+.2%} на сделку ({t['net'].mean() / m.STOP:+.2f}R) "
          f"[90% ДИ {lo:+.2%}; {hi:+.2%} — описательно], медиана {t['net'].median():+.2%}, "
          f"в плюсе {np.mean(t['net'] > 0):.0%}")
    print(f"  стоп в {int(st.sum())} из {len(t)}; худшая сделка {t['net'].min():+.2%}; "
          f"фандинг {t['fund'].mean():+.2%}")
    print(f"  плацебо «та же монета» {t['plac'].mean():+.2%} (есть у {int(ok_p.sum())}), сверх "
          f"плацебо {ex_p:+.2%}; рынок {t['mkt'].mean():+.2%}, сверх рынка {ex_m:+.2%}")
    print(f"  без 3 лучших {wo3:+.2%}")
    crit = {
        "1 средний net > 0": t["net"].mean() > 0,
        "2 медиана net > 0": t["net"].median() > 0,
        "3 сверх рынка > 0": ex_m > 0,
        "4 сверх плацебо «та же монета» > 0": ex_p > 0,
        "5 без 3 лучших net > 0": wo3 > 0,
        "6 худшая сделка не хуже −60%": t["net"].min() >= -0.60,
    }
    for k, ok in crit.items():
        print(f"  {'✓' if ok else '✗'} {k}")
    word = "подтверждают" if all(crit.values()) else "не подтверждают"
    print(f"\nИТОГ: независимые случаи правило {word}")
    print("\n== Описательно")
    for y, g in t.groupby(t["t_in"].dt.year):
        print(f"  {y}: n {len(g)}, net {g['net'].mean():+.2%}, стопов {int((g['how'] == 'стоп').sum())}")  # noqa: E501
    print("  сделки: " + "; ".join(f"{r.token} {r.t_in:%Y-%m-%d} {r.net:+.0%}" for r in t.itertuples()))  # noqa: E501


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    sub.add_parser("run")
    a = ap.parse_args()
    {"fetch": cmd_fetch, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    main()
