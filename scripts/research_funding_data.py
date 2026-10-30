"""Данные для замера «фандинг и OI как самостоятельная стратегия» (2026-10-31).

  nice -n 15 .venv/bin/python -m scripts.research_funding_data --probe
  nice -n 15 .venv/bin/python -m scripts.research_funding_data            # всё по UNIVERSE
  nice -n 15 .venv/bin/python -m scripts.research_funding_data --symbols BTCUSDT,ETHUSDT

Пишет в /Users/mac/bot ctypto/data/cache_research/funding/ (вне git):
  <SYM>_kline1h.csv     ts, open, high, low, close, volume   (linear perp, Bybit)
  <SYM>_funding.csv     ts, rate                             (момент расчёта, ставка за интервал)
  <SYM>_oi4h.csv        ts, oi                               (метка 4h, в монетах)
  <SYM>_premium1h.csv   ts, open, high, low, close           (premium index perp к индексу спота)
  <SYM>_spot1h.csv      ts, open, high, low, close, volume   (spot, если есть на Bybit)

Только публичные эндпоинты Bybit v5, ключи не нужны. Не быстрее ~3 запросов в секунду,
при 429/сетевой ошибке — экспоненциальный отступ. Файл пишется атомарно (tmp → rename),
докачка идёт с последней метки уже лежащего файла.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import time
from pathlib import Path

import ccxt
import pandas as pd

CACHE = Path("/Users/mac/bot ctypto/data/cache_research/funding")
START = dt.datetime(2021, 1, 1, tzinfo=dt.timezone.utc)
END = dt.datetime(2026, 10, 30, tzinfo=dt.timezone.utc)
MIN_GAP_S = 0.34                     # ≤ 3 запросов в секунду

# Ликвидные перпы Bybit с длинной историей + базовый вотчлист бота. Список зафиксирован
# в регистрации ДО прогона (docs/measurements/2026-10-31-funding-strategies.md).
UNIVERSE = (
    "BTCUSDT ETHUSDT SOLUSDT XRPUSDT DOGEUSDT ADAUSDT LINKUSDT AVAXUSDT DOTUSDT LTCUSDT "
    "BCHUSDT ATOMUSDT FILUSDT ETCUSDT UNIUSDT NEARUSDT AAVEUSDT TRXUSDT XLMUSDT OPUSDT "
    "ARBUSDT APTUSDT SUIUSDT 1000PEPEUSDT ZECUSDT HYPEUSDT ONDOUSDT"
).split()

_last_call = [0.0]


def _ms(d: dt.datetime) -> int:
    return int(d.timestamp() * 1000)


def _iso(ms: int | float) -> str:
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def _call(fn, params: dict, tries: int = 6) -> list:
    for i in range(tries):
        wait = MIN_GAP_S - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
        try:
            return fn(params)["result"]["list"]
        except (ccxt.NetworkError, ccxt.ExchangeError, ccxt.RateLimitExceeded) as e:
            if i == tries - 1:
                raise
            time.sleep(min(60, 2 ** (i + 1)))
            print(f"  повтор {i + 1}: {type(e).__name__}", flush=True)
    return []


def _save(df: pd.DataFrame, path: Path) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    df.to_csv(tmp, index=False)
    tmp.replace(path)


def _merge(path: Path, new: pd.DataFrame) -> pd.DataFrame:
    if path.exists():
        old = pd.read_csv(path)
        new = pd.concat([old, new], ignore_index=True)
    return new.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def _from(path: Path, default: dt.datetime) -> int:
    if path.exists():
        old = pd.read_csv(path, usecols=["ts"])
        if len(old):
            return int(old.ts.max()) + 1
    return _ms(default)


def klines(ex, sym: str, category: str, path: Path, fn_name: str = "publicGetV5MarketKline",
           cols: tuple[str, ...] = ("open", "high", "low", "close", "volume")) -> None:
    """1h-свечи вперёд от последней метки: start=…, limit=1000 (Bybit отдаёт новые→старые)."""
    fn = getattr(ex, fn_name)
    start, stop = _from(path, START), _ms(END)
    rows: list = []
    while start < stop:
        end = min(stop, start + 1000 * 3_600_000 - 1)
        params = {"category": category, "symbol": sym, "interval": "60", "start": start,
                  "end": end, "limit": 1000}
        lst = _call(fn, params)
        if lst:
            rows += [[int(r[0]), *map(float, r[1:1 + len(cols)])] for r in lst]
        start = end + 1
    if rows:
        df = pd.DataFrame(rows, columns=["ts", *cols])
        _save(_merge(path, df), path)


def funding(ex, sym: str, path: Path) -> None:
    """История ставок: идём назад от END до START или до последней метки файла."""
    floor = _from(path, START)
    rows, end = [], _ms(END)
    while end > floor:
        lst = _call(ex.publicGetV5MarketFundingHistory,
                    {"category": "linear", "symbol": sym, "endTime": end, "limit": 200})
        if not lst:
            break
        rows += [(int(r["fundingRateTimestamp"]), float(r["fundingRate"])) for r in lst]
        end = min(int(r["fundingRateTimestamp"]) for r in lst) - 1
    if rows:
        df = pd.DataFrame(rows, columns=["ts", "rate"])
        df = df[(df.ts >= _ms(START)) & (df.ts < _ms(END))]
        _save(_merge(path, df), path)


def open_interest(ex, sym: str, path: Path) -> None:
    """OI по 4h-меткам: идём назад курсором Bybit."""
    floor = _from(path, START)
    rows, cursor = [], None
    while True:
        params = {"category": "linear", "symbol": sym, "intervalTime": "4h", "limit": 200,
                  "startTime": floor, "endTime": _ms(END)}
        if cursor:
            params["cursor"] = cursor
        res = None
        for i in range(6):
            wait = MIN_GAP_S - (time.monotonic() - _last_call[0])
            if wait > 0:
                time.sleep(wait)
            _last_call[0] = time.monotonic()
            try:
                res = ex.publicGetV5MarketOpenInterest(params)["result"]
                break
            except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                if i == 5:
                    raise
                time.sleep(min(60, 2 ** (i + 1)))
                print(f"  повтор {i + 1}: {type(e).__name__}", flush=True)
        lst = (res or {}).get("list") or []
        rows += [(int(r["timestamp"]), float(r["openInterest"])) for r in lst]
        cursor = (res or {}).get("nextPageCursor")
        if not lst or not cursor:
            break
    if rows:
        df = pd.DataFrame(rows, columns=["ts", "oi"])
        _save(_merge(path, df), path)


def probe(ex) -> None:
    """Глубина данных — до регистрации, без взгляда на связь с ценой."""
    s, e = _ms(dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)), _ms(START)
    for iv in ("1h", "4h", "1d"):
        r = _call(ex.publicGetV5MarketOpenInterest, {"category": "linear", "symbol": "BTCUSDT",
                  "intervalTime": iv, "startTime": s, "endTime": e, "limit": 200})
        print(f"OI {iv} за 2020: {len(r)} точек, самая ранняя "
              f"{_iso(min(int(x['timestamp']) for x in r)) if r else '—'}")
    r = _call(ex.publicGetV5MarketFundingHistory, {"category": "linear", "symbol": "BTCUSDT",
              "startTime": s, "endTime": e, "limit": 200})
    print(f"фандинг BTC за 2020: {len(r)} ставок, самая ранняя "
          f"{_iso(min(int(x['fundingRateTimestamp']) for x in r)) if r else '—'}")
    r = _call(ex.publicGetV5MarketPremiumIndexPriceKline,
              {"category": "linear", "symbol": "BTCUSDT", "interval": "60", "start": s, "end": e,
               "limit": 1000})
    print(f"premium index 1h BTC за 2020: {len(r)} свечей, самая ранняя "
          f"{_iso(min(int(x[0]) for x in r)) if r else '—'}")
    r = _call(ex.publicGetV5MarketKline, {"category": "spot", "symbol": "BTCUSDT",
              "interval": "60", "start": s, "end": e, "limit": 1000})
    print(f"spot 1h BTC за 2020: {len(r)} свечей, самая ранняя "
          f"{_iso(min(int(x[0]) for x in r)) if r else '—'}")
    info = _call(ex.publicGetV5MarketInstrumentsInfo, {"category": "linear", "limit": 1000})
    by = {x["symbol"]: x for x in info}
    for sym in UNIVERSE:
        x = by.get(sym)
        if x is None:
            print(f"{sym}: нет в списке linear")
            continue
        print(f"{sym}: запуск {_iso(x['launchTime'])}, интервал фандинга сейчас "
              f"{int(x.get('fundingInterval') or 0) // 60} ч")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--symbols", default=",".join(UNIVERSE))
    ap.add_argument("--what", default="kline,funding,oi,premium,spot")
    a = ap.parse_args()
    ex = ccxt.bybit({"enableRateLimit": False})
    if a.probe:
        probe(ex)
        return
    CACHE.mkdir(parents=True, exist_ok=True)
    what = set(a.what.split(","))
    for sym in a.symbols.split(","):
        t0 = time.monotonic()
        try:
            if "kline" in what:
                klines(ex, sym, "linear", CACHE / f"{sym}_kline1h.csv")
            if "funding" in what:
                funding(ex, sym, CACHE / f"{sym}_funding.csv")
            if "oi" in what:
                open_interest(ex, sym, CACHE / f"{sym}_oi4h.csv")
            if "premium" in what:
                klines(ex, sym, "linear", CACHE / f"{sym}_premium1h.csv",
                       fn_name="publicGetV5MarketPremiumIndexPriceKline",
                       cols=("open", "high", "low", "close"))
            if "spot" in what:
                klines(ex, sym, "spot", CACHE / f"{sym}_spot1h.csv")
        except Exception as e:                       # noqa: BLE001 — монету пропускаем, дальше
            print(f"{sym}: СБОЙ {type(e).__name__}: {e}", flush=True)
            continue
        print(f"{sym}: готово за {time.monotonic() - t0:.0f} с", flush=True)


if __name__ == "__main__":
    main()
