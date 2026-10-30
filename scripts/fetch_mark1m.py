"""Минутная mark-цена Bybit linear на окна сделок — кэш для замера «трейл по mark-цене»
(docs/measurements/2026-10-31-mark-trigger.md).

Только публичный `/v5/market/mark-price-kline`, без ключей. Прод это не импортирует.
Кэш — `<DATA_ROOT>/cache_tf/mark1m/<имя>.npy` (ts, o, h, l, c) + `<имя>.json` со скачанными
интервалами, в git не идёт. Устроен как `scripts/fetch_m1.py` (замок на символ, покрытие в
json, страница пустой — окно не скачано), но тише: 2 потока по ~2.5 запр/с — тот же IP
держит живой бот и другие замеры.

  ensure({"BTC/USDT:USDT": [(start_ms, end_ms), ...]})
  bars = load("BTC/USDT:USDT")
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from scripts.dump_history import cache_name
from scripts.fetch_m1 import MIN, Permanent, _merge, _missing, bybit_ids

URL = ("https://api.bybit.com/v5/market/mark-price-kline?category=linear&symbol={sym}"
       "&interval=1&start={s}&end={e}&limit=1000")
THREADS = 2
MIN_GAP_S = 0.4


def root() -> Path:
    return Path(os.environ.get("DATA_ROOT", "data")) / "cache_tf" / "mark1m"


def _paths(symbol: str) -> tuple[Path, Path]:
    base = root() / cache_name(symbol, "1m")[:-4]
    return base.with_suffix(".npy"), base.with_suffix(".json")


_tl = threading.local()


def _get(sym: str, s: int, e: int) -> list:
    last = getattr(_tl, "t", 0.0)
    wait = MIN_GAP_S - (time.time() - last)
    if wait > 0:
        time.sleep(wait)
    err = ""
    for attempt in range(6):
        _tl.t = time.time()
        try:
            with urllib.request.urlopen(URL.format(sym=sym, s=s, e=e), timeout=20) as r:
                js = json.load(r)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as ex:
            err = str(ex)
            time.sleep(2 + 3 * attempt)
            continue
        code = js.get("retCode")
        if code == 0:
            return js["result"]["list"]
        if code == 10001:
            raise Permanent(f"{sym}: {js.get('retMsg')}")
        err = f"retCode {code} {js.get('retMsg')}"
        time.sleep((10 if code == 10006 else 2) + 5 * attempt)
    raise RuntimeError(f"{sym} {s}-{e}: {err}")


def _fetch_range(sym: str, s: int, e: int) -> list:
    rows: dict[int, list] = {}
    end = e - MIN
    while end >= s:
        lst = _get(sym, s, end)
        if not lst:
            break
        for k in lst:
            rows[int(k[0])] = k
        oldest = min(int(k[0]) for k in lst)
        if oldest <= s or len(lst) < 1000:
            break
        end = oldest - MIN
    return [[int(t), float(k[1]), float(k[2]), float(k[3]), float(k[4])]
            for t, k in sorted(rows.items()) if s <= t < e]


def _fetch_symbol(symbol: str, want: list, log=print) -> tuple[str, int, int]:
    npy, js = _paths(symbol)
    npy.parent.mkdir(parents=True, exist_ok=True)
    with open(npy.with_suffix(".lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        have = _merge(json.loads(js.read_text())) if js.exists() else []
        todo = _missing(_merge(want), have)
        if not todo:
            return symbol, 0, 0
        now = int(time.time() * 1000) // MIN * MIN
        got: list = []
        done: list = []
        for s, e in todo:
            e = min(e, now)
            if e <= s:
                continue
            try:
                part: list = []
                for bid, lo, hi in bybit_ids(symbol):
                    if hi <= s or lo >= e:
                        continue
                    part += _fetch_range(bid, max(s, lo // MIN * MIN), min(e, hi))
            except Permanent as ex:
                log(f"  mark1m {symbol}: {ex} — окно помечено пустым")
                part = []
            except RuntimeError as ex:
                log(f"  mark1m {symbol}: {ex} — окно НЕ скачано")
                continue
            got += part
            done.append([s, e])
        old = np.load(npy) if npy.exists() else np.empty((0, 5))
        arr = np.concatenate([old, np.array(got, dtype=float).reshape(-1, 5)])
        if len(arr):
            arr = arr[np.argsort(arr[:, 0], kind="stable")]
            keep = np.ones(len(arr), bool)
            keep[:-1] = arr[1:, 0] != arr[:-1, 0]
            arr = arr[keep]
        tmp = npy.with_suffix(".tmp.npy")
        np.save(tmp, arr)
        tmp.rename(npy)
        js.write_text(json.dumps(_merge(have + done)))
        return symbol, len(done), len(got)


def ensure(windows: dict, threads: int = THREADS, log=print) -> None:
    jobs = sorted(windows.items())
    done = [0, 0]

    def one(job):
        sym, iv = job
        res = _fetch_symbol(sym, iv, log)
        done[0] += 1
        done[1] += res[2]
        if done[0] % 20 == 0 or done[0] == len(jobs):
            log(f"  mark1m: {done[0]}/{len(jobs)} символов, скачано баров {done[1]}",
                flush=True)
        return res

    with ThreadPoolExecutor(threads) as pool:
        list(pool.map(one, jobs))


def load(symbol: str) -> np.ndarray:
    npy, _js = _paths(symbol)
    return np.load(npy) if npy.exists() else np.empty((0, 5))
