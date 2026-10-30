"""Данные для замера кросс-секционного моментума (docs/measurements/2026-10-31-xs-momentum.md).

Дневные свечи (1D, UTC) всех USDT-перпетуалов Bybit, когда-либо торговавшихся, — включая
делистнутые: список символов = текущие инструменты v5 + индекс публичного архива сделок
public.bybit.com/trading/ (там есть и снятые контракты). Плюс история фандинга — для
символов, которые попадали в торгуемую вселенную (её считает research_xsmom.py).

Публичный API, без ключей. Темп ≤ 3 запроса/с с отступом при 429/10006: в тот же Bybit
ходит прод-бот на этом Mac. Кэш — data/cache_research/xsmom/ (не в git): повторный запуск
докачивает только недостающее.

  nice -n 15 .venv/bin/python -m scripts.research_xsmom_fetch klines
  nice -n 15 .venv/bin/python -m scripts.research_xsmom_fetch funding --symbols-file F
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.bybit.com"
CACHE = Path("/Users/mac/bot ctypto/data/cache_research/xsmom")
START_MS = 1_606_780_800_000          # 2020-12-01 00:00 UTC — запас под 4-недельный lookback
MIN_INTERVAL = 0.34                   # ≤ ~3 запроса в секунду
_last = [0.0]


def _get(path: str, params: dict) -> dict:
    """GET с темпом и отступом. retCode 10006/10018 и HTTP 429 — ждать и повторить."""
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{API}{path}?{qs}"
    for attempt in range(8):
        wait = MIN_INTERVAL - (time.monotonic() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                d = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 403, 502, 503, 504):
                time.sleep(min(60, 5 * 2 ** attempt))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(min(60, 5 * 2 ** attempt))
            continue
        if d.get("retCode") in (10006, 10018):
            time.sleep(min(60, 5 * 2 ** attempt))
            continue
        return d
    raise RuntimeError(f"не удалось: {url}")


def all_symbols() -> list[str]:
    """Текущие USDT-перпетуалы + все USDT-символы публичного архива (там есть делистнутые)."""
    cur = _get("/v5/market/instruments-info", {"category": "linear", "limit": 1000})
    now = {x["symbol"] for x in cur["result"]["list"]
           if x["quoteCoin"] == "USDT" and x["contractType"] == "LinearPerpetual"}
    idx = CACHE / "public_trading_index.html"
    if not idx.exists():
        with urllib.request.urlopen("https://public.bybit.com/trading/", timeout=30) as r:
            idx.write_bytes(r.read())
    arch = set(re.findall(r'href="([A-Z0-9]+USDT)/"', idx.read_text()))
    (CACHE / "symbols_current.json").write_text(json.dumps(sorted(now)))
    return sorted(now | arch)


def fetch_klines(sym: str) -> int:
    """Все дневные свечи символа с 2020-12-01 -> CSV ts,open,high,low,close,volume,turnover."""
    out = CACHE / "klines" / f"{sym}.csv"
    if out.exists():
        return -1
    rows: dict[int, list[str]] = {}
    end = int(time.time() * 1000)
    while True:
        d = _get("/v5/market/kline", {"category": "linear", "symbol": sym, "interval": "D",
                                      "start": START_MS, "end": end, "limit": 1000})
        if d.get("retCode") != 0:
            break                                   # символа нет в API — пустой файл
        lst = d["result"]["list"]
        if not lst:
            break
        for k in lst:
            rows[int(k[0])] = k
        oldest = min(int(k[0]) for k in lst)
        if len(lst) < 1000 or oldest <= START_MS:
            break
        end = oldest - 1
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume", "turnover"])
        for ts in sorted(rows):
            w.writerow(rows[ts][:7])
    tmp.rename(out)
    return len(rows)


def fetch_funding(sym: str, start_ms: int) -> int:
    """История фандинга символа с start_ms -> CSV ts,rate."""
    out = CACHE / "funding" / f"{sym}.csv"
    if out.exists():
        return -1
    rows: dict[int, str] = {}
    end = int(time.time() * 1000)
    while True:
        d = _get("/v5/market/funding/history", {"category": "linear", "symbol": sym,
                                                "startTime": start_ms, "endTime": end,
                                                "limit": 200})
        if d.get("retCode") != 0:
            break
        lst = d["result"]["list"]
        if not lst:
            break
        for x in lst:
            rows[int(x["fundingRateTimestamp"])] = x["fundingRate"]
        oldest = min(int(x["fundingRateTimestamp"]) for x in lst)
        if len(lst) < 200 or oldest <= start_ms:
            break
        end = oldest - 1
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "rate"])
        for ts in sorted(rows):
            w.writerow([ts, rows[ts]])
    tmp.rename(out)
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["klines", "funding"])
    ap.add_argument("--symbols-file", help="JSON {символ: старт_мс} для funding")
    a = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    if a.what == "klines":
        syms = all_symbols()
        print(f"символов: {len(syms)}", flush=True)
        for i, s in enumerate(syms, 1):
            n = fetch_klines(s)
            if i % 50 == 0 or n == 0:
                print(f"{i}/{len(syms)} {s}: {n}", flush=True)
    else:
        todo = json.loads(Path(a.symbols_file).read_text())
        print(f"фандинг: {len(todo)} символов", flush=True)
        for i, (s, st) in enumerate(sorted(todo.items()), 1):
            n = fetch_funding(s, int(st))
            if i % 25 == 0:
                print(f"{i}/{len(todo)} {s}: {n}", flush=True)
    print("готово", flush=True)


if __name__ == "__main__":
    sys.exit(main())
