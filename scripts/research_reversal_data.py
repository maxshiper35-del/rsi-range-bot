"""Данные для замера «разворот после экстремальной свечи».

Регистрация — docs/measurements/2026-10-31-extreme-reversal.md. Только публичный Bybit
(`/v5/market/kline`, `/v5/market/funding/history`); старты запросов всех потоков не чаще
~3 в секунду — с того же IP работает прод-бот. 1h берётся из готовых кэшей бота, докачиваются
только дыры. Кэш — `<DATA_ROOT>/cache_research/reversal/`, в git не идёт. Прод это не
импортирует.

  python -m scripts.research_reversal_data daily      # дневные свечи всех USDT-перпетуалов
  python -m scripts.research_reversal_data universe   # топ-30 по обороту на каждый день
  python -m scripts.research_reversal_data hourly     # 1h для попавших в топ-30
"""
from __future__ import annotations

import csv
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HOUR = 3_600_000
DAY = 86_400_000
GAP_S = 0.36                     # между СТАРТАМИ запросов всех потоков: ≤2.8 запроса/с
THREADS = 3                      # потоки только перекрывают задержку сети, темп общий
START = int(datetime(2021, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)
END = int(datetime(2026, 10, 30, tzinfo=timezone.utc).timestamp() * 1000)
TOP_N = 30
LOOKBACK_D = 30
MIN_HISTORY_D = 60
_last = [0.0]
_rate = threading.Lock()


def _slot() -> None:
    """Общий для всех потоков темп: старты запросов не чаще GAP_S."""
    with _rate:
        wait = GAP_S - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()


def data_root() -> Path:
    return Path(os.environ.get("DATA_ROOT", "data"))


def root() -> Path:
    p = data_root() / "cache_research" / "reversal"
    p.mkdir(parents=True, exist_ok=True)
    return p


def instruments() -> list[dict]:
    ins = json.loads((data_root() / "cache_univ" / "instruments.json").read_text())
    return [x for x in ins if x.get("settleCoin") == "USDT"
            and x.get("contractType") == "LinearPerpetual"
            and x.get("status") in ("Trading", "Closed")]


def _get(url: str) -> dict:
    for attempt in range(8):
        _slot()
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                js = json.load(r)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            time.sleep(3 + 4 * attempt)
            continue
        code = js.get("retCode")
        if code == 0:
            return js["result"]
        if code == 10001:                         # нет такого символа/параметров
            return {"list": []}
        time.sleep((15 if code == 10006 else 3) + 5 * attempt)
    raise RuntimeError(f"не скачалось: {url}")


def fetch_kline(sym: str, interval: str, s: int, e: int, step: int) -> np.ndarray:
    """Бары с открытием в [s, e): ts, o, h, l, c, volume, turnover. Страницы по 1000 назад от e."""
    rows: dict[int, list] = {}
    end = e - step
    prev_oldest = None
    while end >= s:
        lst = _get("https://api.bybit.com/v5/market/kline?category=linear"
                   f"&symbol={sym}&interval={interval}&start={s}&end={end}&limit=1000")["list"]
        if not lst:
            break
        for k in lst:
            rows[int(k[0])] = k
        oldest = min(int(k[0]) for k in lst)
        # неполная страница — НЕ начало истории: Bybit отдаёт окно из 1000 баров до end, и у
        # снятой с торгов монеты (end после делистинга) первая страница короткая. Конец — пустой
        # ответ или старт периода (замечание координатора 30.10, ветка xsmom)
        if oldest <= s or (prev_oldest is not None and oldest >= prev_oldest):
            break
        prev_oldest = oldest
        end = oldest - step
    out = [[int(t), *map(float, k[1:7])] for t, k in sorted(rows.items()) if s <= t < e]
    return np.array(out, dtype=float).reshape(-1, 7)


def _store(kind: str, sym: str) -> Path:
    p = root() / kind
    p.mkdir(exist_ok=True)
    return p / f"{sym}.npy"


def load(kind: str, sym: str) -> np.ndarray:
    p = _store(kind, sym)
    if not p.exists():
        return np.empty((0, 7))
    try:
        return np.load(p)
    except (ValueError, OSError):                 # файл оборван при остановке — перекачать
        p.unlink()
        return np.empty((0, 7))


def _save(p: Path, arr: np.ndarray) -> None:
    tmp = p.with_suffix(".tmp.npy")
    np.save(tmp, arr)
    tmp.rename(p)


def _parallel(jobs: list, fn, label: str) -> None:
    done = [0]
    lock = threading.Lock()

    def one(job):
        fn(job)
        with lock:
            done[0] += 1
            if done[0] % 50 == 0 or done[0] == len(jobs):
                print(f"{label} {done[0]}/{len(jobs)}", flush=True)

    with ThreadPoolExecutor(THREADS) as pool:
        list(pool.map(one, jobs))


def cmd_daily() -> None:
    def one(x: dict) -> None:
        sym = x["symbol"]
        p = _store("daily", sym)
        if p.exists():
            load("daily", sym)                    # оборванный файл удаляется и перекачивается
        if p.exists():
            return
        lo = max(START, int(x.get("launchTime") or 0) // DAY * DAY)
        hi = min(END, int(x.get("deliveryTime") or 0) or END)
        _save(p, fetch_kline(sym, "D", lo, hi, DAY) if hi > lo else np.empty((0, 7)))

    _parallel(instruments(), one, "daily")


def build_universe() -> dict[str, list[str]]:
    """День (UTC, ISO) -> топ-30 id по обороту за 30 предыдущих дней (данные до этого дня)."""
    turn: dict[str, dict[int, float]] = {}
    first: dict[str, int] = {}
    gone: dict[str, int] = {}
    for x in instruments():
        a = load("daily", x["symbol"])
        if len(a):
            turn[x["symbol"]] = {int(t): float(v) for t, v in zip(a[:, 0], a[:, 6])}
            first[x["symbol"]] = int(a[0, 0])
            gone[x["symbol"]] = int(x.get("deliveryTime") or 0) or 2**62
    days = range(START + (LOOKBACK_D + 1) * DAY, END, DAY)
    out: dict[str, list[str]] = {}
    for d in days:
        score = []
        for sym, tv in turn.items():
            if d - first[sym] < MIN_HISTORY_D * DAY:
                continue
            if d + DAY > gone[sym]:                  # день делистинга и после — не торгуется
                continue
            s = sum(tv.get(d - j * DAY, 0.0) for j in range(1, LOOKBACK_D + 1))
            if s > 0:
                score.append((s, sym))
        score.sort(reverse=True)
        key = datetime.fromtimestamp(d / 1000, timezone.utc).strftime("%Y-%m-%d")
        out[key] = [sym for _s, sym in score[:TOP_N]]
    (root() / "universe.json").write_text(json.dumps(out))
    return out


def _cached_1h(base: str) -> np.ndarray:
    """1h из готовых кэшей бота (cache5y, cache_univ, cache_tf): ts, o, h, l, c, volume."""
    parts = []
    for d in ("cache5y", "cache_univ", "cache_tf"):
        f = data_root() / d / f"{base}-USDT-USDT_1h.csv"
        if not f.exists():
            continue
        with open(f) as fh:
            rows = [[float(r["ts"]), float(r["open"]), float(r["high"]), float(r["low"]),
                     float(r["close"]), float(r["volume"])] for r in csv.DictReader(fh)]
        if rows:
            parts.append(np.array(rows))
    if not parts:
        return np.empty((0, 6))
    a = np.concatenate(parts)
    a = a[np.argsort(a[:, 0], kind="stable")]
    keep = np.ones(len(a), bool)
    keep[1:] = a[1:, 0] != a[:-1, 0]
    return a[keep]


def _gaps(ts: np.ndarray, lo: int, hi: int) -> list[tuple[int, int]]:
    """Интервалы [lo, hi), не покрытые часами ts (дыры длиннее 3 ч)."""
    t = ts[(ts >= lo) & (ts < hi)]
    if not len(t):
        return [(lo, hi)]
    out = []
    if t[0] - lo > 3 * HOUR:
        out.append((lo, int(t[0])))
    jumps = np.nonzero(np.diff(t) > 3 * HOUR)[0]
    for j in jumps:
        out.append((int(t[j]) + HOUR, int(t[j + 1])))
    if hi - t[-1] > 3 * HOUR:
        out.append((int(t[-1]) + HOUR, hi))
    return out


def cmd_hourly() -> None:
    uni = json.loads((root() / "universe.json").read_text())
    meta = {x["symbol"]: x for x in instruments()}
    spans: dict[str, list[int]] = {}
    for day, syms in uni.items():
        d = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)
        for s in syms:
            lo, hi = spans.get(s, [d, d])
            spans[s] = [min(lo, d), max(hi, d)]
    print(f"1h: {len(spans)} символов", flush=True)

    def one(item) -> None:
        sym, (lo, hi) = item
        p = _store("hourly", sym)
        if p.exists():
            return
        # запас: ATR и медиана объёма до первого дня, удержание и лимит после последнего дня;
        # end не позже делистинга (иначе Bybit режет историю)
        gone = int(meta[sym].get("deliveryTime") or 0) or END
        lo, hi = lo - 3 * DAY, min(END, hi + 4 * DAY, gone)
        have = _cached_1h(meta[sym]["baseCoin"])
        have = have[(have[:, 0] >= lo) & (have[:, 0] < hi)] if len(have) else have
        got = [fetch_kline(sym, "60", a, b, HOUR)[:, :6]
               for a, b in _gaps(have[:, 0] if len(have) else np.empty(0), lo, hi)]
        a = np.concatenate([have.reshape(-1, 6), *[g.reshape(-1, 6) for g in got]])
        a = a[np.argsort(a[:, 0], kind="stable")]
        keep = np.ones(len(a), bool)
        keep[1:] = a[1:, 0] != a[:-1, 0]
        _save(p, a[keep])

    _parallel(sorted(spans.items()), one, "hourly")


M5 = 300_000


def _m5_paths(sym: str) -> tuple[Path, Path]:
    p = root() / "m5"
    p.mkdir(exist_ok=True)
    return p / f"{sym}.npy", p / f"{sym}.json"


def load_m5(sym: str) -> np.ndarray:
    npy, _js = _m5_paths(sym)
    return np.load(npy) if npy.exists() else np.empty((0, 5))


def ensure_m5(windows: dict[str, list]) -> None:
    """Докачать 5m-свечи (ts, o, h, l, c) на окна {bybit id: [[s, e), ...]} — личный кэш замера."""
    from scripts.fetch_m1 import _merge, _missing

    now = int(time.time() * 1000) // M5 * M5

    def one(item) -> None:
        sym, iv = item
        npy, js = _m5_paths(sym)
        have = _merge(json.loads(js.read_text())) if js.exists() else []
        todo = _missing(_merge(iv), have)
        got, done = [], []
        for s, e in todo:
            s, e = s // M5 * M5, min(e, now)
            if e <= s:
                continue
            got.append(fetch_kline(sym, "5", s, e, M5)[:, :5])
            done.append([s, e])
        if not done:
            return
        old = load_m5(sym)
        a = np.concatenate([old.reshape(-1, 5), *[g.reshape(-1, 5) for g in got]])
        a = a[np.argsort(a[:, 0], kind="stable")]
        keep = np.ones(len(a), bool)
        keep[1:] = a[1:, 0] != a[:-1, 0]
        _save(npy, a[keep])
        js.write_text(json.dumps(_merge(have + done)))

    _parallel(sorted(windows.items()), one, "m5")


def fetch_funding(sym: str, lo: int = START, hi: int = END) -> np.ndarray:
    """История фандинга [ts, rate] по символу на [lo, hi] (кэш; зовётся один раз на символ
    с полным интервалом его сделок)."""
    p = root() / "funding" / f"{sym}.npy"
    p.parent.mkdir(exist_ok=True)
    if p.exists():
        return np.load(p)
    rows: dict[int, float] = {}
    end = hi
    while end > lo:
        lst = _get("https://api.bybit.com/v5/market/funding/history?category=linear"
                   f"&symbol={sym}&startTime={lo}&endTime={end}&limit=200")["list"]
        if not lst:
            break
        for r in lst:
            rows[int(r["fundingRateTimestamp"])] = float(r["fundingRate"])
        oldest = min(int(r["fundingRateTimestamp"]) for r in lst)
        if len(lst) < 200 or oldest <= lo:
            break
        end = oldest - 1
    arr = np.array(sorted(rows.items()), dtype=float).reshape(-1, 2)
    np.save(p, arr)
    return arr


if __name__ == "__main__":
    what = sys.argv[1]
    if what == "daily":
        cmd_daily()
    elif what == "universe":
        u = build_universe()
        print(f"дней {len(u)}, символов за всё время {len({s for v in u.values() for s in v})}")
    elif what == "hourly":
        cmd_hourly()
    else:
        raise SystemExit(f"неизвестная команда {what}")
