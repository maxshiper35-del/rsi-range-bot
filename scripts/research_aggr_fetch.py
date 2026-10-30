"""Минутки Binance USDT-M с taker buy volume из архива data.binance.vision для замера
«кластеры агрессивных покупок» (docs/measurements/2026-10-31-aggressor-clusters.md).

Вселенная — топ-30 Bybit по дням (data/cache_research/reversal/universe.json); качаем пары
(символ, месяц) для месяцев членства и месяца перед каждым (история для медианы 24 ч).
Октябрь 2026 — дневные файлы архива (месячного ещё нет).

  nice -n 15 .venv/bin/python -m scripts.research_aggr_fetch map     # сопоставление символов
  nice -n 15 .venv/bin/python -m scripts.research_aggr_fetch fetch   # загрузка (4 потока I/O)
"""
from __future__ import annotations

import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

DATA = Path("/Users/mac/bot ctypto/data")
OUT = DATA / "cache_research" / "aggr"
UNIVERSE = DATA / "cache_research" / "reversal" / "universe.json"
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?delimiter=/&prefix="
ARCH = "https://data.binance.vision/"
FIRST, LAST = "2021-12", "2026-10"


def _get(url: str, tries: int = 4) -> bytes | None:
    for k in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 * (k + 1))
        except Exception:                             # noqa: BLE001 — сеть: повтор
            time.sleep(2 * (k + 1))
    raise RuntimeError(f"не скачалось: {url}")


def archive_symbols() -> list[str]:
    prefix = "data/futures/um/monthly/klines/"
    syms, marker = [], ""
    while True:
        x = _get(S3 + prefix + (f"&marker={prefix}{marker}/" if marker else "")).decode()
        got = re.findall(rf"<Prefix>{re.escape(prefix)}([^/<]+)/</Prefix>", x)
        syms += got
        if "<IsTruncated>true</IsTruncated>" not in x or not got:
            return syms
        marker = got[-1]


def _base(sym: str) -> str:
    """1000PEPEUSDT, SHIB1000USDT, 10000LADYSUSDT -> PEPE, SHIB, LADYS."""
    s = sym.removesuffix("USDT")
    s = re.sub(r"^\d+", "", s)
    return re.sub(r"1000$", "", s)


def build_map() -> dict[str, str]:
    uni = json.loads(UNIVERSE.read_text())
    bybit = sorted({s for d, ss in uni.items() if d >= "2021-12-01" for s in ss})
    arch = [s for s in archive_symbols() if s.endswith("USDT")]
    exact = set(arch)
    by_base: dict[str, list[str]] = {}
    for s in arch:
        by_base.setdefault(_base(s), []).append(s)
    m, miss = {}, []
    for s in bybit:
        if s in exact:
            m[s] = s
        elif len(by_base.get(_base(s), [])) == 1:
            m[s] = by_base[_base(s)][0]
        else:
            miss.append(s)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "symbol_map.json").write_text(json.dumps({"map": m, "missing": miss}, indent=1))
    print(f"сопоставлено {len(m)} из {len(bybit)}, нет на Binance: {len(miss)}")
    print("нет:", miss)
    return m


def _months_between(a: str, b: str) -> list[str]:
    y, mo = map(int, a.split("-"))
    out = []
    while f"{y:04d}-{mo:02d}" <= b:
        out.append(f"{y:04d}-{mo:02d}")
        mo += 1
        if mo == 13:
            y, mo = y + 1, 1
    return out


def jobs() -> list[tuple[str, str]]:
    uni = json.loads(UNIVERSE.read_text())
    m = json.loads((OUT / "symbol_map.json").read_text())["map"]
    allm = _months_between(FIRST, LAST)
    need: set[tuple[str, str]] = set()
    for d, ss in uni.items():
        mon = d[:7]
        if mon < "2022-01":
            continue
        i = allm.index(mon)
        for s in ss:
            if s in m:
                need.add((m[s], mon))
                need.add((m[s], allm[i - 1]))
    return sorted(need)


def _parse(raw: bytes) -> np.ndarray:
    """CSV архива -> [open_time, o, h, l, c, volume, taker_buy_volume]; заголовок есть не всегда."""
    import pandas as pd

    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        body = z.read(z.namelist()[0])
    if not body:
        return np.empty((0, 7))
    header = 0 if not body[:1].isdigit() else None
    df = pd.read_csv(io.BytesIO(body), header=header, usecols=[0, 1, 2, 3, 4, 5, 9])
    return df.to_numpy(dtype=np.float64)


def _store(sym: str, mon: str) -> Path:
    return OUT / "m1" / sym / f"{mon}.npz"


def fetch_one(sym: str, mon: str) -> str:
    p = _store(sym, mon)
    if p.exists():
        return "есть"
    if mon == "2026-10":
        parts = []
        for day in range(1, 32):
            raw = _get(f"{ARCH}data/futures/um/daily/klines/{sym}/1m/{sym}-1m-{mon}-{day:02d}.zip")
            if raw:
                parts.append(_parse(raw))
        a = np.concatenate(parts) if parts else np.empty((0, 7))
    else:
        raw = _get(f"{ARCH}data/futures/um/monthly/klines/{sym}/1m/{sym}-1m-{mon}.zip")
        a = _parse(raw) if raw else np.empty((0, 7))
    p.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(p, t=a[:, 0].astype(np.int64), x=a[:, 1:].astype(np.float32))
    return f"{len(a)}"


def fetch_all(workers: int = 8) -> None:
    js = jobs()
    print(f"пар (символ, месяц): {len(js)}", flush=True)
    done = 0
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(fetch_one, s, m): (s, m) for s, m in js}
        for f in as_completed(futs):
            done += 1
            try:
                f.result()
            except Exception as e:                    # noqa: BLE001 — отметить и идти дальше
                print("ошибка", futs[f], e, flush=True)
            if done % 100 == 0:
                print(f"{done}/{len(js)}", flush=True)
    print("готово", flush=True)


if __name__ == "__main__":
    {"map": build_map, "fetch": fetch_all}[sys.argv[1]]()
