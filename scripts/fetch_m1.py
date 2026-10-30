"""Минутные свечи Bybit linear на окна сделок — кэш для замеров реализма трейла.

Только публичный kline (`/v5/market/kline`), без ключей и без ccxt. Прод это не импортирует.
Кэш — `<DATA_ROOT>/cache_tf/m1/<имя>.npy` (ts, o, h, l, c) плюс `<имя>.json` со скачанными
интервалами, в git не идёт. Темп — не выше ~8 запросов/с на всех потоках: тот же IP
держит живой бот, ловить 10006 ему нельзя.

  windows = {"BTC/USDT:USDT": [(start_ms, end_ms), ...]}
  ensure(windows)                  # докачать недостающее
  bars = load("BTC/USDT:USDT")     # np.ndarray (n, 5), по возрастанию ts
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

MIN = 60_000
URL = "https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}&interval=1&start={s}&end={e}&limit=1000"
THREADS = 3
MIN_GAP_S = 0.35            # пауза между запросами одного потока (≈3 запр/с на поток)


def root() -> Path:
    return Path(os.environ.get("DATA_ROOT", "data")) / "cache_tf" / "m1"


def _paths(symbol: str) -> tuple[Path, Path]:
    base = root() / cache_name(symbol, "1m")[:-4]
    return base.with_suffix(".npy"), base.with_suffix(".json")


def _merge(iv: list) -> list:
    out: list = []
    for s, e in sorted(iv):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def _missing(want: list, have: list) -> list:
    """Части интервалов want [s, e), не покрытые have (оба слиты)."""
    out = []
    for s, e in want:
        cur = s
        for hs, he in have:
            if he <= cur or hs >= e:
                continue
            if hs > cur:
                out.append([cur, hs])
            cur = max(cur, he)
            if cur >= e:
                break
        if cur < e:
            out.append([cur, e])
    return out


def bybit_ids(symbol: str) -> list[tuple[str, int, int]]:
    """[(bybit id, от ms, до ms)] — для ротации из instruments.json (релистинги дают
    несколько id на одно имя), иначе BASEUSDT без ограничений."""
    inst_path = Path(os.environ.get("DATA_ROOT", "data")) / "cache_univ" / "instruments.json"
    base = symbol.split("/")[0]
    if inst_path.exists():
        ids = [(x["symbol"], int(x.get("launchTime") or 0),
                int(x.get("deliveryTime") or 0) or 2**62)
               for x in json.loads(inst_path.read_text()) if x.get("baseCoin") == base
               and x.get("settleCoin") == "USDT" and x.get("contractType") == "LinearPerpetual"]
        if ids:
            return ids
    return [(f"{base}USDT", 0, 2**62)]


_tl = threading.local()


class Permanent(Exception):
    """Bybit отверг параметры (нет такого символа) — повтор бесполезен."""


def _get(sym: str, s: int, e: int) -> list:
    """Одна страница. Сеть и любые не-нулевые коды — повтор с паузой (страница, молча
    вернувшаяся пустой, обрезала бы окно, а кэш счёл бы его скачанным)."""
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
    """Бары с открытием в [s, e), страницами по 1000 назад от e. Исключение — окно не
    скачано (покрытие не записывается)."""
    rows: dict[int, list] = {}
    end = e - MIN
    while end >= s:
        lst = _get(sym, s, end)
        if not lst:                    # раньше листинга / после делистинга — данных нет
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
    """Докачать недостающее. Под файловым замком: два процесса, качающие один символ,
    иначе перезапишут npy друг друга, а json останется с чужим покрытием."""
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
                log(f"  m1 {symbol}: {ex} — окно помечено пустым")
                part = []
            except RuntimeError as ex:
                log(f"  m1 {symbol}: {ex} — окно НЕ скачано")
                continue
            got += part
            done.append([s, e])
        old = np.load(npy) if npy.exists() else np.empty((0, 5))
        arr = np.concatenate([old, np.array(got, dtype=float).reshape(-1, 5)])
        if len(arr):
            arr = arr[np.argsort(arr[:, 0], kind="stable")]
            keep = np.ones(len(arr), bool)
            keep[:-1] = arr[1:, 0] != arr[:-1, 0]           # дубли ts — последний
            arr = arr[keep]
        tmp = npy.with_suffix(".tmp.npy")
        np.save(tmp, arr)
        tmp.rename(npy)
        js.write_text(json.dumps(_merge(have + done)))
        return symbol, len(done), len(got)


def rebuild_coverage(symbol: str, max_gap_min: int = 2) -> None:
    """Покрытие json заново по фактическим минуткам npy (починка кэша после обрезанных
    окон старой версии): отрезки подряд идущих минут, разрывы ≤ max_gap_min склеиваются."""
    npy, js = _paths(symbol)
    if not npy.exists():
        return
    ts = np.load(npy)[:, 0].astype(np.int64)
    if not len(ts):
        js.write_text("[]")
        return
    cut = np.nonzero(np.diff(ts) > max_gap_min * MIN)[0]
    starts = np.r_[ts[0], ts[cut + 1]]
    ends = np.r_[ts[cut], ts[-1]] + MIN
    js.write_text(json.dumps([[int(a), int(b)] for a, b in zip(starts, ends)]))


def ensure(windows: dict, threads: int = THREADS, log=print) -> None:
    jobs = [(sym, iv) for sym, iv in sorted(windows.items())]
    done = [0, 0]

    def one(job):
        sym, iv = job
        res = _fetch_symbol(sym, iv)
        done[0] += 1
        done[1] += res[2]
        if done[0] % 20 == 0 or done[0] == len(jobs):
            log(f"  m1: {done[0]}/{len(jobs)} символов, скачано баров {done[1]}", flush=True)
        return res

    with ThreadPoolExecutor(threads) as pool:
        list(pool.map(one, jobs))


def load(symbol: str) -> np.ndarray:
    npy, _js = _paths(symbol)
    return np.load(npy) if npy.exists() else np.empty((0, 5))


def coverage(symbol: str) -> list:
    _npy, js = _paths(symbol)
    return _merge(json.loads(js.read_text())) if js.exists() else []
