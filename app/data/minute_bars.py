"""Минутные свечи для минутного судьи (app.brain.judge_1m): кэш на диске + докачка с биржи.

Кэш — тот же формат и то же место, что у замеров (`scripts/fetch_m1.py`):
`<каталог БД>/cache_tf/m1/<SYMBOL>_1m.npy` (ts, o, h, l, c) и `.json` со скачанными
интервалами [s, e) в мс; в проде это data/cache_tf/m1 — минутки замеров переиспользуются.
Докачка — общий ccxt-инстанс бота под его замком (встроенный rate-limiter, 200 мс между
запросами): судья зовётся из такта поллера в потоке, 1–2 страницы на открытую сделку в час.

Контракт `load_window`: массив минут [start, end) только если покрытие кэша ПОЛНОЕ
(с учётом докачки), иначе None — судья не должен молча судить по неполным данным.
Незакрытая (текущая) минута никогда не отдаётся и не кэшируется.
"""
from __future__ import annotations

import fcntl
import json
import time
from pathlib import Path

import numpy as np

MIN_MS = 60_000
PAGE = 1000                     # лимит kline Bybit за запрос
MAX_PAGES = 12                  # ≤ 12 000 минут (8.3 суток) за один вызов


def cache_root() -> Path:
    from app.config import get_settings
    return Path(get_settings().db_path).parent / "cache_tf" / "m1"


def _paths(symbol: str) -> tuple[Path, Path]:
    base = cache_root() / f"{symbol.replace('/', '-').replace(':', '-')}_1m"
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


def _now_ms() -> int:
    return int(time.time() * 1000)


def _fetch_pages(symbol: str, s: int, e: int) -> list[list]:
    """Закрытые минуты [s, e) с биржи, since-курсором вперёд. Пустая страница — данных
    нет (до листинга / после делистинга). Исключение сети — наверх (окно не скачано)."""
    from app.config import get_settings
    from app.data.market_data import _EXCHANGE_LOCK, _bump, _exchange

    ex = _exchange(get_settings().exchange_id)
    closed_before = _now_ms() // MIN_MS * MIN_MS            # минуты с ts < этого закрыты
    rows: list[list] = []
    since = s
    for _ in range(MAX_PAGES):
        if since >= min(e, closed_before):
            break
        with _EXCHANGE_LOCK:                                # sync-ccxt не потокобезопасен
            _bump("requests")
            page = ex.fetch_ohlcv(symbol, timeframe="1m", since=since, limit=PAGE)
        if not page:
            break
        for k in page:
            t = int(k[0])
            if s <= t < e and t < closed_before:
                rows.append([t, float(k[1]), float(k[2]), float(k[3]), float(k[4])])
        last = int(page[-1][0])
        if last + MIN_MS <= since:                          # защита от зацикливания
            break
        since = last + MIN_MS
        if len(page) < PAGE:
            break
    return rows


def load_window(symbol: str, start_ms: int, end_ms: int, fetch: bool = True,
                log=None) -> np.ndarray | None:
    """Минуты [start_ms, end_ms) (end обрезается по последней закрытой минуте). None —
    покрытие неполное: сеть не дала окно, биржа не отдаёт минуты (символа нет), или
    fetch=False и кэша не хватает."""
    end_ms = min(end_ms, _now_ms() // MIN_MS * MIN_MS)
    if end_ms <= start_ms:
        return np.empty((0, 5))
    npy, js = _paths(symbol)
    npy.parent.mkdir(parents=True, exist_ok=True)
    with open(npy.with_suffix(".lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        have = _merge(json.loads(js.read_text())) if js.exists() else []
        todo = _missing([[start_ms, end_ms]], have)
        if todo and fetch:
            got: list = []
            done: list = []
            for s, e in todo:
                try:
                    part = _fetch_pages(symbol, s, e)
                except Exception as ex:                     # noqa: BLE001 — окно НЕ скачано
                    if log is not None:
                        log.warning("минутки %s %s–%s: %s", symbol, s, e, ex)
                    continue
                if not part:
                    continue                                # биржа без данных — не кэшируем
                got += part
                # покрытие — только до последней полученной минуты: страница короче окна
                # (лимит страниц / биржа отдала меньше) не должна помечать окно скачанным
                done.append([s, min(e, part[-1][0] + MIN_MS)])
            if got:
                old = np.load(npy) if npy.exists() else np.empty((0, 5))
                arr = np.concatenate([old, np.array(got, dtype=float).reshape(-1, 5)])
                arr = arr[np.argsort(arr[:, 0], kind="stable")]
                keep = np.ones(len(arr), bool)
                keep[:-1] = arr[1:, 0] != arr[:-1, 0]        # дубли ts — последний
                arr = arr[keep]
                tmp = npy.with_suffix(".tmp.npy")
                np.save(tmp, arr)
                tmp.rename(npy)
                have = _merge(have + done)
                js.write_text(json.dumps(have))
        if _missing([[start_ms, end_ms]], have):
            return None
        arr = np.load(npy) if npy.exists() else np.empty((0, 5))
    sel = (arr[:, 0] >= start_ms) & (arr[:, 0] < end_ms)
    return arr[sel]
