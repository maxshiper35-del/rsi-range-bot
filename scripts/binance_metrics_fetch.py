"""Сбор публичной истории позиционирования Binance USDⓈ-M для замера №20
(docs/measurements/2026-11-11-binance-positioning.md).

  DATA_ROOT="/Users/mac/bot ctypto/data" .venv/bin/python -m scripts.binance_metrics_fetch symbols
  DATA_ROOT=... .venv/bin/python -m scripts.binance_metrics_fetch firstdays [--start 2022-01]
  DATA_ROOT=... .venv/bin/python -m scripts.binance_metrics_fetch days --symbols-file s.txt \
      --start 2021-12-01 --end 2026-11-10

Источник: data.binance.vision, futures/um/daily/metrics (шаг 5 минут: open interest, long/short
топ-трейдеров по счетам и по позициям, long/short всех счетов, taker buy/sell). Только чтение
статических файлов, без ключей. Кэш — DATA_ROOT/cache_research/binance_metrics/, сбор возобновляется
с места остановки; файл STOP в этой папке останавливает его.

  symbols   — список символов, у которых есть папка metrics (листинг бакета), → symbols.txt
  index     — по каждому бессрочному …USDT: даты, за которые файл metrics есть → index/<SYMBOL>.txt
              (дальше качаются только существующие файлы, без лишних 404)
  firstdays — файл за 1-е число каждого месяца для всех бессрочных …USDT (для отбора вселенной
              по OI на дату, без взгляда вперёд) → first/<SYMBOL>/<SYMBOL>-metrics-YYYY-MM-01.csv
  days      — все дни диапазона для символов из файла
              → days/<SYMBOL>/<SYMBOL>-metrics-YYYY-MM-DD.csv
  monthly   — месячные файлы klines 1h и fundingRate для символов из файла
              → klines/<SYMBOL>/<SYMBOL>-1h-YYYY-MM.csv,
                funding/<SYMBOL>/<SYMBOL>-fundingRate-YYYY-MM.csv
"""
from __future__ import annotations

import argparse
import io
import os
import re
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOST = "https://data.binance.vision/data/futures/um"
BASE = f"{HOST}/daily/metrics"
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
LIST = f"{S3}?prefix=data/futures/um/daily/metrics/&delimiter=/"
UA = {"User-Agent": "research-fetch/1.0"}
THREADS = int(os.environ.get("BINANCE_THREADS", "4"))


def root() -> Path:
    p = Path(os.environ.get("DATA_ROOT", "data")) / "cache_research" / "binance_metrics"
    p.mkdir(parents=True, exist_ok=True)
    return p


def stop_asked() -> bool:
    return (root() / "STOP").exists()


def get(url: str, tries: int = 5) -> bytes | None:
    """Файл или None, если его нет (404 — символ не торговался в этот день)."""
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(3 + 3 * k)
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(3 + 3 * k)
    raise RuntimeError(f"не скачалось: {url}")


def cmd_symbols(_a) -> None:
    out, marker = [], ""
    while True:
        xml = get(LIST + (f"&marker={marker}" if marker else "")).decode()
        pref = re.findall(r"<Prefix>data/futures/um/daily/metrics/([^<]+)/</Prefix>", xml)
        out += pref
        if "<IsTruncated>true</IsTruncated>" not in xml or not pref:
            break
        marker = f"data/futures/um/daily/metrics/{pref[-1]}/"
    syms = sorted(set(out))
    (root() / "symbols.txt").write_text("\n".join(syms) + "\n")
    print(f"символов с metrics: {len(syms)}", flush=True)


def fetch_day(sym: str, day: str, sub: str) -> str:
    return fetch(f"{BASE}/{sym}/{sym}-metrics-{day}.zip",
                 root() / sub / sym / f"{sym}-metrics-{day}.csv")


def fetch(url: str, path: Path) -> str:
    if path.exists() or (path.with_suffix(".none")).exists():
        return "есть"
    data = get(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    if data is None:
        path.with_suffix(".none").touch()           # файла нет — пометка, чтобы не просить снова
        return "нет"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        path.write_bytes(z.read(z.namelist()[0]))
    return "скачан"


def run(jobs: list[tuple[str, str]], sub: str) -> None:
    run_calls([lambda j=j: fetch_day(j[0], j[1], sub) for j in jobs])


def run_calls(calls: list) -> None:
    print(f"заданий: {len(calls)}", flush=True)
    stats: dict[str, int] = {}
    with ThreadPoolExecutor(THREADS) as ex:
        for i, res in enumerate(ex.map(lambda c: c(), calls), 1):
            stats[res] = stats.get(res, 0) + 1
            if i % 2000 == 0 or i == len(calls):
                print(f"  {i}/{len(calls)} {stats} {time.strftime('%H:%M:%S')}", flush=True)
            if stop_asked():
                print("STOP — выхожу", flush=True)
                ex.shutdown(cancel_futures=True)
                break


def months(start: str, end: str) -> list[str]:
    y, m = map(int, start.split("-"))
    ye, me = map(int, end.split("-"))
    out = []
    while (y, m) <= (ye, me):
        out.append(f"{y:04d}-{m:02d}-01")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def usdt_perps() -> list[str]:
    return [s for s in (root() / "symbols.txt").read_text().split()
            if re.fullmatch(r"[A-Z0-9]+USDT", s)]


def list_days(sym: str) -> str:
    """Даты файлов metrics символа по листингу бакета (с пагинацией)."""
    path = root() / "index" / f"{sym}.txt"
    if path.exists():
        return "есть"
    prefix = f"data/futures/um/daily/metrics/{sym}/"
    days, marker = [], ""
    while True:
        xml = get(f"{S3}?prefix={prefix}" + (f"&marker={marker}" if marker else "")).decode()
        keys = re.findall(r"<Key>([^<]+)</Key>", xml)
        days += re.findall(rf"{sym}-metrics-(\d{{4}}-\d{{2}}-\d{{2}})\.zip</Key>", xml)
        if "<IsTruncated>true</IsTruncated>" not in xml or not keys:
            break
        marker = keys[-1]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(sorted(set(days))) + "\n")
    return "список"


def index(sym: str) -> set[str]:
    p = root() / "index" / f"{sym}.txt"
    return set(p.read_text().split()) if p.exists() else set()


def cmd_index(_a) -> None:
    run_calls([lambda s=s: list_days(s) for s in usdt_perps()])


def cmd_firstdays(a) -> None:
    syms = Path(a.symbols_file).read_text().split() if a.symbols_file else usdt_perps()
    jobs = []
    for s in syms:
        have = index(s)
        jobs += [(s, d) for d in months(a.start, a.end) if d in have]
    run(jobs, "first")


def cmd_days(a) -> None:
    """Строки файла: «СИМВОЛ» (весь диапазон) или «СИМВОЛ начало конец» (окно членства)."""
    jobs = []
    for line in Path(a.symbols_file).read_text().splitlines():
        parts = line.split()
        if not parts:
            continue
        s, lo, hi = (parts + [a.start, a.end])[:3] if len(parts) == 1 else parts[:3]
        jobs += [(s, d) for d in sorted(index(s)) if max(lo, a.start) <= d <= min(hi, a.end)]
    run(jobs, "days")


def cmd_monthly(a) -> None:
    calls = []
    for s in Path(a.symbols_file).read_text().split():
        have = index(s)
        if not have:
            continue
        for ym in sorted({d[:7] for d in have if a.start <= d[:7] <= a.end}):
            calls.append(lambda s=s, ym=ym: fetch(
                f"{HOST}/monthly/klines/{s}/1h/{s}-1h-{ym}.zip",
                root() / "klines" / s / f"{s}-1h-{ym}.csv"))
            calls.append(lambda s=s, ym=ym: fetch(
                f"{HOST}/monthly/fundingRate/{s}/{s}-fundingRate-{ym}.zip",
                root() / "funding" / s / f"{s}-fundingRate-{ym}.csv"))
    run_calls(calls)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("symbols")
    sub.add_parser("index")
    p = sub.add_parser("firstdays")
    p.add_argument("--start", default="2022-01")
    p.add_argument("--end", default="2026-11")
    p.add_argument("--symbols-file", default=None, help="по умолчанию — все бессрочные …USDT")
    p = sub.add_parser("days")
    p.add_argument("--symbols-file", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p = sub.add_parser("monthly")
    p.add_argument("--symbols-file", required=True)
    p.add_argument("--start", default="2021-11")
    p.add_argument("--end", default="2026-11")
    a = ap.parse_args()
    {"symbols": cmd_symbols, "index": cmd_index, "firstdays": cmd_firstdays, "days": cmd_days,
     "monthly": cmd_monthly}[a.cmd](a)


if __name__ == "__main__":
    main()
