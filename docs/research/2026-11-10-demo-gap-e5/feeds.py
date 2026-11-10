"""Публичные минутки в окнах трёх сделок: Bybit, BingX (настоящий рынок) и BingX VST (демо).
Проверка, чья лента расходится. Только рыночные данные, без ключей, ≤ 2 запросов в секунду.

  .venv/bin/python docs/research/2026-11-10-demo-gap-e5/feeds.py
"""
import json
import time
import urllib.request
from datetime import datetime, timezone

import ccxt


def ms(s):
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def hm(t):
    return datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%H:%M")


real = ccxt.bingx({"timeout": 20000, "options": {"defaultType": "swap"}})
vst = ccxt.bingx({"timeout": 20000, "options": {"defaultType": "swap"}})
vst.set_sandbox_mode(True)


def bybit(sym, s, e):
    url = ("https://api.bybit.com/v5/market/kline?category=linear"
           f"&symbol={sym}&interval=1&start={s}&end={e}&limit=1000")
    rows = json.load(urllib.request.urlopen(url, timeout=20))["result"]["list"]
    return {int(r[0]): (float(r[2]), float(r[3]), float(r[4])) for r in rows}


def bingx(ex, sym, s, n):
    return {int(r[0]): (float(r[2]), float(r[3]), float(r[4]))
            for r in ex.fetch_ohlcv(sym, "1m", since=s, limit=n)}


# id, символ ccxt, id Bybit, окно UTC, сторона, вход, стоп, минуты для печати, что смотрим
CASES = [
    (776, "ENA/USDT:USDT", "ENAUSDT", "2026-10-31T19:30:00", "2026-10-31T20:02:00", "SHORT",
     0.193315, 0.19607, ("19:44", "19:45", "19:46", "19:50", "19:55", "19:58", "20:00"),
     "трейл включён 19:45:44 UTC"),
    (868, "ICNT/USDT:USDT", "ICNTUSDT", "2026-11-09T10:10:00", "2026-11-09T11:02:00", "LONG",
     0.31405, 0.3065, ("10:21", "10:22", "10:23", "10:24", "10:25", "10:26", "10:30", "10:45"),
     "трейл включён 10:23:32 UTC"),
    (803, "ESPORTS/USDT:USDT", "ESPORTSUSDT", "2026-11-02T13:45:00", "2026-11-02T14:45:00",
     "LONG", 0.0146, 0.01412, ("13:49", "13:50", "13:51", "13:52", "13:53"),
     "журнал засчитал вход 13:51:11 UTC"),
]
for sid, sym, bid, a, b, side, entry, stop, show, what in CASES:
    s, e = ms(a), ms(b)
    n = (e - s) // 60000
    feeds = {"Bybit": bybit(bid, s, e - 60000)}
    time.sleep(0.6)
    feeds["BingX рынок"] = bingx(real, sym, s, n)
    time.sleep(0.6)
    feeds["BingX VST"] = bingx(vst, sym, s, n)
    time.sleep(0.6)
    risk = abs(entry - stop)
    sg = 1 if side == "LONG" else -1
    print(f"#{sid} {sym.split('/')[0]} {side}: вход {entry:g}, 1R = {risk:g}; {what}")
    for name, d in feeds.items():
        keys = [k for k in d if s <= k < e]
        ext = max(d[k][0] for k in keys) if side == "LONG" else min(d[k][1] for k in keys)
        touch = [hm(k) for k in sorted(keys) if d[k][1] <= entry <= d[k][0]]
        print(f"   {name:12s} лучший ход за окно {sg * (ext - entry) / risk:+.2f}R; "
              f"минут с касанием входа {len(touch)}" + (f", первая {touch[0]}" if touch else ""))
    print("   минута | " + " | ".join(f"{n_:^24s}" for n_ in feeds) + "   (high / low, в R от входа)")
    for k in sorted(feeds["Bybit"]):
        if hm(k) not in show:
            continue
        cells = []
        for d in feeds.values():
            if k in d:
                hi, lo = sg * (d[k][0] - entry) / risk, sg * (d[k][1] - entry) / risk
                cells.append(f"{max(hi, lo):+6.2f} / {min(hi, lo):+6.2f}".center(24))
            else:
                cells.append("—".center(24))
        print(f"   {hm(k)}  | " + " | ".join(cells))
