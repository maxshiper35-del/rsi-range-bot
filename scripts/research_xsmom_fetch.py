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
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = "https://api.bybit.com"
CACHE = Path("/Users/mac/bot ctypto/data/cache_research/xsmom")
START_MS = 1_606_780_800_000          # 2020-12-01 00:00 UTC — запас под 4-недельный lookback
MIN_INTERVAL = 0.34                   # ≤ ~3 запроса в секунду на ВСЕ потоки вместе
_next = [0.0]
_lock = threading.Lock()


def _slot() -> None:
    """Общий для потоков темп: следующий запрос не раньше MIN_INTERVAL после предыдущего."""
    with _lock:
        now = time.monotonic()
        at = max(now, _next[0])
        _next[0] = at + MIN_INTERVAL
    if at > now:
        time.sleep(at - now)


def _get(path: str, params: dict) -> dict:
    """GET с темпом и отступом. retCode 10006/10018 и HTTP 429 — ждать и повторить."""
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{API}{path}?{qs}"
    for attempt in range(8):
        _slot()
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
    # Проход ВПЕРЁД окнами по 1000 дней. API отдаёт не больше 1000 свечей ВНУТРИ окна, которое
    # кончается в end. Для символа, снятого раньше начала окна (end − 1000 дней), ответ пуст.
    # Поэтому прежняя пагинация «назад от сейчас» обрезала историю делистнутых по 2024-02-04,
    # а снятые раньше приходили пустыми (найдено 30.10).
    now = int(time.time() * 1000)
    span = 1000 * 86_400_000
    w0 = START_MS
    while w0 < now:
        w1 = min(w0 + span - 1, now)
        d = _get("/v5/market/kline", {"category": "linear", "symbol": sym, "interval": "D",
                                      "start": w0, "end": w1, "limit": 1000})
        if d.get("retCode") != 0:
            break                                   # символа нет в API — пустой файл
        for k in d["result"]["list"]:
            rows[int(k[0])] = k
        w0 = w1 + 1
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume", "turnover"])
        for ts in sorted(rows):
            w.writerow(rows[ts][:7])
    tmp.rename(out)
    return len(rows)


def fetch_funding(sym: str, start_ms: int, end_ms: int | None = None) -> int:
    """История фандинга символа в [start_ms, end_ms] -> CSV ts,rate."""
    out = CACHE / "funding" / f"{sym}.csv"
    if out.exists():
        return -1
    rows: dict[int, str] = {}
    end = end_ms or int(time.time() * 1000)
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


def missing() -> None:
    """Символы архива, чьих свечей API не отдаёт: период торгов и объём архива сделок
    (сумма размеров дневных файлов) — оценка, могли ли они попасть в топ-50 по обороту."""
    out = {}
    empty = [f.stem for f in sorted((CACHE / "klines").glob("*.csv"))
             if sum(1 for _ in f.open()) <= 1]
    for sym in empty:
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(f"https://public.bybit.com/trading/{sym}/",
                                        timeout=30) as r:
                h = r.read().decode()
        except (urllib.error.URLError, TimeoutError):
            continue
        days = sorted(re.findall(rf'href="{sym}(\d{{4}}-\d{{2}}-\d{{2}})\.csv\.gz"', h))
        mid_bytes = None
        if days:
            # активность — размер файла сделок за средний день торгов (HEAD, без скачивания)
            mid = days[len(days) // 2]
            time.sleep(0.5)
            req = urllib.request.Request(
                f"https://public.bybit.com/trading/{sym}/{sym}{mid}.csv.gz", method="HEAD")
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    mid_bytes = int(r.headers.get("Content-Length") or 0)
            except (urllib.error.URLError, TimeoutError):
                pass
        out[sym] = {"first": days[0] if days else None, "last": days[-1] if days else None,
                    "days": len(days), "mid_day": days[len(days) // 2] if days else None,
                    "mid_bytes": mid_bytes}
    (CACHE / "missing_symbols.json").write_text(json.dumps(out, indent=1))
    print(f"без свечей в API: {len(empty)}; описано: {len(out)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["klines", "funding", "missing"])
    ap.add_argument("--symbols-file", help="JSON {символ: старт_мс} для funding")
    a = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    if a.what == "missing":
        missing()
        return
    if a.what == "klines":
        syms = all_symbols()
        print(f"символов: {len(syms)}, 3 потока", flush=True)
        cnt = [0]

        def kjob(s: str) -> None:
            n = fetch_klines(s)
            with _lock:
                cnt[0] += 1
                if cnt[0] % 50 == 0 or n == 0:
                    print(f"{cnt[0]}/{len(syms)} {s}: {n}", flush=True)

        with ThreadPoolExecutor(max_workers=3) as ex:
            list(ex.map(kjob, syms))
    else:
        todo = json.loads(Path(a.symbols_file).read_text())
        print(f"фандинг: {len(todo)} символов, 3 потока", flush=True)
        done = [0]

        def job(item: tuple) -> None:
            s, win = item
            st, en = (win, None) if isinstance(win, int) else (int(win[0]), int(win[1]))
            n = fetch_funding(s, st, en)
            with _lock:
                done[0] += 1
                if done[0] % 25 == 0:
                    print(f"{done[0]}/{len(todo)} {s}: {n}", flush=True)

        with ThreadPoolExecutor(max_workers=3) as ex:
            list(ex.map(job, sorted(todo.items())))
    print("готово", flush=True)


if __name__ == "__main__":
    sys.exit(main())
