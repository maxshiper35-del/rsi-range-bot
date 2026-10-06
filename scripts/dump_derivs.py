"""Кэш фандинга и открытого интереса (Bybit linear) для замеров «новой информации».

  .venv/bin/python -m scripts.dump_derivs            # все SYMS бэктеста
  .venv/bin/python -m scripts.dump_derivs --symbols BTC/USDT:USDT

Пишет data/cache5y/derivs/<имя>_funding.csv (ts, rate) и <имя>_oi1h.csv (ts, oi).
ts — мс UTC: для фандинга это момент расчёта ставки, для OI — метка часа Bybit.
Публичные эндпоинты, ключи не нужны. Повторный запуск перекачивает целиком.
"""
from __future__ import annotations

import argparse
import datetime as dt
import time
from pathlib import Path

import ccxt
import pandas as pd

from scripts.backtest_monthly import SYMS

OUT = Path("data/cache5y/derivs")
START = dt.datetime(2021, 6, 1, tzinfo=dt.UTC)
HOUR_MS = 3_600_000


def _bybit_symbol(symbol: str) -> str:
    return symbol.split(":")[0].replace("/", "")


def _ms(d: dt.datetime) -> int:
    return int(d.timestamp() * 1000)


def _call(fn, params: dict, tries: int = 5) -> list:
    for i in range(tries):
        try:
            return fn(params)["result"]["list"]
        except (ccxt.NetworkError, ccxt.ExchangeError):
            if i == tries - 1:
                raise
            time.sleep(2 ** i)
    return []


def funding(ex, sym: str) -> pd.DataFrame:
    rows, end = [], _ms(dt.datetime.now(dt.UTC))
    while end > _ms(START):
        lst = _call(ex.publicGetV5MarketFundingHistory,
                    {"category": "linear", "symbol": sym, "endTime": end, "limit": 200})
        if not lst:
            break
        rows += [(int(r["fundingRateTimestamp"]), float(r["fundingRate"])) for r in lst]
        end = min(int(r["fundingRateTimestamp"]) for r in lst) - 1
    df = pd.DataFrame(rows, columns=["ts", "rate"]).drop_duplicates("ts").sort_values("ts")
    return df[df.ts >= _ms(START)]


def open_interest(ex, sym: str) -> pd.DataFrame:
    rows, end = [], _ms(dt.datetime.now(dt.UTC))
    empty = 0
    while end > _ms(START):
        start = end - 199 * HOUR_MS
        lst = _call(ex.publicGetV5MarketOpenInterest,
                    {"category": "linear", "symbol": sym, "intervalTime": "1h",
                     "startTime": start, "endTime": end, "limit": 200})
        rows += [(int(r["timestamp"]), float(r["openInterest"])) for r in lst]
        empty = 0 if lst else empty + 1
        if empty >= 3:  # ~25 дней пусто подряд — монета ещё не торговалась
            break
        end = start - 1
    df = pd.DataFrame(rows, columns=["ts", "oi"]).drop_duplicates("ts").sort_values("ts")
    return df[df.ts >= _ms(START)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=SYMS)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    ex = ccxt.bybit({"enableRateLimit": True})
    for symbol in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        name = symbol.replace("/", "-").replace(":", "-")
        sym = _bybit_symbol(symbol)
        f = funding(ex, sym)
        f.to_csv(OUT / f"{name}_funding.csv", index=False)
        o = open_interest(ex, sym)
        o.to_csv(OUT / f"{name}_oi1h.csv", index=False)
        first = lambda d: pd.to_datetime(d.ts.iloc[0], unit="ms") if len(d) else None  # noqa: E731
        print(f"{symbol}: фандинг {len(f)} с {first(f)}, OI {len(o)} с {first(o)}", flush=True)


if __name__ == "__main__":
    main()
