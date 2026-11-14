"""Замер №30: проверка вперёд правила №28 на новых объявлениях Binance о делистинге, без денег
(docs/measurements/2026-11-14-delist-forward.md).

  export DATA_ROOT="/Users/mac/bot ctypto/data"
  .venv/bin/python -m scripts.delist_forward poll
  .venv/bin/python -m scripts.delist_forward report

poll   — первая страница раздела Delisting CMS Binance; новые статьи пишутся с временем первого
         обнаружения; по событиям «Binance Will Delist … on DATE» — снимки стаканов Binance, Bybit
         и BingX при обнаружении и в час входа. launchd запускает каждые 5 минут.
report — закрытые события: главный вариант (вход через ≥60 мин после releaseDate) и быстрый (через
         ≥5 мин после обнаружения), стоп +30% по максимуму минуты; свечи и фандинг — fapi Binance.

Только публичное чтение, без ключей и ордеров.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

from scripts import measure_delist_short as d
from scripts import measure_delist_short_1m as m

BN, BY, BX = "https://fapi.binance.com", "https://api.bybit.com", "https://open-api.bingx.com"
UA = {"User-Agent": "Mozilla/5.0"}
FAST = pd.Timedelta(minutes=5)


def froot() -> Path:
    p = Path(os.environ.get("DATA_ROOT", "data")) / "forward" / "delist"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get(url: str, tries: int = 3):
    err = ""
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            err = str(e)[:80]
            time.sleep(2 + 3 * k)
    raise RuntimeError(f"{url[:80]}: {err}")


def jl_read(name: str) -> list[dict]:
    p = froot() / name
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def jl_add(name: str, row: dict) -> None:
    with open(froot() / name, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def parse(title: str) -> tuple[list[str], str] | None:
    mt = re.match(r"^Binance Will Delist (.+?) on (\d{4}-\d{2}-\d{2})", title)
    if not mt:
        return None
    names = re.split(r",\s*|\s+and\s+|\s*&\s*", re.sub(r"\(.*?\)", "", mt.group(1)))
    return [x.strip() for x in names if x.strip() and x.strip() not in d.STABLE], mt.group(2)


def book(venue: str, sym: str) -> dict | None:
    """Лучшие цены и глубина в пределах 1% от середины, в долларах; None — перпа нет."""
    try:
        if venue == "binance":
            r = get(f"{BN}/fapi/v1/depth?symbol={sym}&limit=500", tries=1)
            bids, asks = r["bids"], r["asks"]
        elif venue == "bybit":
            r = get(f"{BY}/v5/market/orderbook?category=linear&symbol={sym}&limit=500", tries=1)
            bids, asks = r["result"]["b"], r["result"]["a"]
        else:
            r = get(f"{BX}/openApi/swap/v2/quote/depth?symbol={sym}&limit=500", tries=1)
            bids, asks = r["data"]["bids"], r["data"]["asks"]
        bids = sorted(((float(p), float(q)) for p, q in bids), reverse=True)
        asks = sorted((float(p), float(q)) for p, q in asks)
        if not bids or not asks:
            return None
        mid = (bids[0][0] + asks[0][0]) / 2
        return dict(bid=bids[0][0], ask=asks[0][0],
                    bid_usd_1pct=round(sum(p * q for p, q in bids if p >= mid * 0.99)),
                    ask_usd_1pct=round(sum(p * q for p, q in asks if p <= mid * 1.01)))
    except (RuntimeError, KeyError, TypeError, ValueError):
        return None


def snapshot(ev: dict, kind: str) -> None:
    tk, s = ev["token"], ev["sym"]
    names = {"binance": s, "bybit": s, "bingx": f"{tk}-USDT"}
    row = dict(ann=ev["ann"], token=tk, kind=kind, t=pd.Timestamp.now(tz="UTC").isoformat())
    for v, name in names.items():
        row[v] = book(v, name)
    jl_add("books.jsonl", row)


def live_perps() -> set[str]:
    info = get(f"{BN}/fapi/v1/exchangeInfo")
    return {x["symbol"] for x in info["symbols"] if x.get("contractType") == "PERPETUAL"
            and x.get("status") == "TRADING" and x.get("quoteAsset") == "USDT"}


def cmd_poll(_a) -> None:
    lock = open(froot() / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    now = pd.Timestamp.now(tz="UTC")
    arts = (get(d.CMS.format(page=1)).get("data") or {}).get("catalogs") or []
    arts = arts[0]["articles"] if arts else []
    seen_p = froot() / "seen.json"
    first_run = not seen_p.exists()
    seen = set(json.loads(seen_p.read_text())) if not first_run else set()
    new = [a for a in arts if a["code"] not in seen]
    perps = None
    for a in sorted(new, key=lambda x: x["releaseDate"]):
        seen.add(a["code"])
        if first_run:
            continue
        jl_add("articles.jsonl", dict(code=a["code"], title=a["title"], release=int(a["releaseDate"]),  # noqa: E501
                                      first_seen=now.isoformat()))
        pr = parse(a["title"])
        if not pr:
            continue
        perps = perps if perps is not None else live_perps()
        for tk in pr[0]:
            s = next((x for x in (f"{tk}USDT", f"1000{tk}USDT") if x in perps), None)
            ev = dict(ann=a["code"], token=tk, sym=s, release=int(a["releaseDate"]),
                      first_seen=now.isoformat(), delist=pr[1])
            jl_add("events.jsonl", ev)
            if s:
                snapshot(ev, "обнаружение")
        print(f"{now:%Y-%m-%d %H:%M} новое объявление: {a['title'][:90]}", flush=True)
    seen_p.write_text(json.dumps(sorted(seen)))
    if first_run:
        print(f"{now:%Y-%m-%d %H:%M} первый запуск: в ленте {len(seen)} статей, события — только "
              f"новее", flush=True)
    done = {(b["ann"], b["token"]) for b in jl_read("books.jsonl") if b["kind"] == "вход"}
    for ev in jl_read("events.jsonl"):
        t_in = (pd.Timestamp(ev["release"], unit="ms", tz="UTC") + d.DELAY).ceil("h")
        if ev["sym"] and (ev["ann"], ev["token"]) not in done and t_in <= now < t_in + FAST * 3:
            snapshot(ev, "вход")


def klines_1m(sym: str, a: pd.Timestamp, b: pd.Timestamp) -> pd.DataFrame | None:
    rows, t0 = [], int(a.timestamp() * 1000)
    end = int(b.timestamp() * 1000)
    while t0 <= end:
        page = get(f"{BN}/fapi/v1/klines?symbol={sym}&interval=1m&startTime={t0}&limit=1500")
        if not page:
            break
        rows += [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]))
                 for x in page]
        t0 = int(page[-1][0]) + 60_000
        if len(page) < 1500:
            break
        time.sleep(0.3)
    if not rows:
        return None
    x = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "vol"])
    x["t"] = pd.to_datetime(x["t"], unit="ms", utc=True)
    return x.drop_duplicates("t").set_index("t")


def funding(sym: str, a: pd.Timestamp, b: pd.Timestamp) -> pd.Series:
    page = get(f"{BN}/fapi/v1/fundingRate?symbol={sym}&startTime={int(a.timestamp() * 1000)}"
               f"&endTime={int(b.timestamp() * 1000)}&limit=1000")
    t = pd.to_datetime([int(x["fundingTime"]) for x in page], unit="ms", utc=True)
    return pd.Series([float(x["fundingRate"]) for x in page], index=t, dtype=float).sort_index()


def cmd_report(_a) -> None:
    now = pd.Timestamp.now(tz="UTC")
    arts, evs = jl_read("articles.jsonl"), jl_read("events.jsonl")
    lag = [(pd.Timestamp(x["first_seen"]) - pd.Timestamp(x["release"], unit="ms", tz="UTC"))
           .total_seconds() / 60 for x in arts]
    print(f"новых статей с запуска: {len(arts)}; событий: {len(evs)}, с перпом: "
          f"{sum(1 for e in evs if e['sym'])}")
    if lag:
        print(f"отставание ленты от публикации: медиана {np.median(lag):.0f} мин, макс {max(lag):.0f}")  # noqa: E501
    rows = []
    for e in evs:
        t0 = pd.Timestamp(e["release"], unit="ms", tz="UTC")
        t_out = pd.Timestamp(e["delist"], tz="UTC")
        if not e["sym"] or t_out > now:
            continue
        try:
            m1 = klines_1m(e["sym"], t0 - pd.Timedelta(hours=26), t_out)
            fu = funding(e["sym"], t0, t_out)
        except RuntimeError as err:                     # символ уже снят с API — свечей не взять
            print(f"  {e['token']}: нет данных API ({err})")
            continue
        if m1 is None:
            continue
        t_in = (t0 + d.DELAY).ceil("h")
        t_fast = (pd.Timestamp(e["first_seen"]) + FAST).ceil("min")
        main = m.short_1m(m1, fu, t_in, t_out, "high") if m.alive(m1, t_in) else None
        fast = m.short_1m(m1, fu, t_fast, t_out, "high") if m.alive(m1, t_fast) else None
        rows.append(dict(ann=e["ann"], token=e["token"], t_in=t_in,
                         net=main["net"] if main else math.nan, how=main["how"] if main else "-",
                         net_fast=fast["net"] if fast else math.nan))
    if not rows:
        print("закрытых событий пока нет")
        return
    t = pd.DataFrame(rows)
    t.to_pickle(froot() / "ledger.pkl")
    v = t[t["net"].notna()]
    for r in t.itertuples():
        print(f"  {r.token:8s} вход {r.t_in:%Y-%m-%d %H:%M} | главный {r.net:+.1%} ({r.how}) | "
              f"быстрый {r.net_fast:+.1%}")
    if len(v):
        lo, hi = d.cluster_ci(v, "net")
        print(f"главный вариант: n {len(v)}, net {v['net'].mean():+.2%} [90% ДИ {lo:+.2%}; {hi:+.2%}], "  # noqa: E501
              f"медиана {v['net'].median():+.2%}, худшая {v['net'].min():+.1%}; быстрый: "
              f"{t['net_fast'].mean():+.2%}")
        print("вывод — при 30 закрытых событиях или 2027-08-31" if len(v) < 30 else "30 событий: вывод")  # noqa: E501


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("poll")
    sub.add_parser("report")
    a = ap.parse_args()
    {"poll": cmd_poll, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    main()
