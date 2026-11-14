"""Дневные свечи и фандинг всех USDT-M перпов Binance, включая снятые с торгов, — общий кэш для
замеров на корзинах монет (план 2026-11-14: тренд на корзине, корзина по фандингу, секторы).

  DATA_ROOT="/Users/mac/bot ctypto/data" .venv/bin/python -m scripts.perp_daily_fetch

Символы и их месяцы — из индекса metrics (кэш №20). У символов, которые были уже в начале архива
metrics (2021-12), свечи запрашиваются с 2019-09. Отсутствующие месяцы помечаются .none.
"""
from __future__ import annotations

import pandas as pd

from scripts.binance_metrics_fetch import HOST, fetch, run_calls
from scripts.binance_metrics_fetch import root as bm_root
from scripts.measure_listing_short import ffile

ARCHIVE_START, K_START, END = "2021-12", "2019-09", "2026-10"


def dfile(s: str, ym: str):
    return bm_root() / "klines_1d" / s / f"{s}-1d-{ym}.csv"


def main() -> None:
    calls = []
    for p in sorted((bm_root() / "index").glob("*USDT.txt")):
        s, days = p.stem, p.read_text().split()
        if not days:
            continue
        a, b = min(days)[:7], min(max(days)[:7], END)
        if a <= ARCHIVE_START:
            a = K_START
        for ym in pd.period_range(a, b, freq="M").strftime("%Y-%m"):
            calls.append(lambda s=s, ym=ym: fetch(
                f"{HOST}/monthly/klines/{s}/1d/{s}-1d-{ym}.zip", dfile(s, ym)))
            calls.append(lambda s=s, ym=ym: fetch(
                f"{HOST}/monthly/fundingRate/{s}/{s}-fundingRate-{ym}.zip", ffile(s, ym)))
    run_calls(calls)


if __name__ == "__main__":
    main()
