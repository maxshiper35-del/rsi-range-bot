"""Выгрузка истории OHLCV в CSV-кэш — фундамент серии бэктест-экспериментов.

Зачем: каждый прогон run_backtest тянет ~8800 баров через пагинацию (~50с и удар по
рейт-лимиту Bybit). Десятки параллельных экспериментов по одному и тому же периоду
должны читать ОДНИ И ТЕ ЖЕ бары с диска: это и быстрее, и делает сравнение конфигов
честным (разные прогоны не попадают на разные срезы рынка из-за дрейфа «сейчас»).

  python3 -m scripts.dump_history --symbols "BTC/USDT:USDT,ETH/USDT:USDT" \
      --tfs 1h,4h --months 12 --out data/cache

Имя файла: <BASE>_<TF>.csv (BTC-USDT-USDT_1h.csv) — читается run_backtest --csv.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from app.logging_conf import get_logger


def cache_name(symbol: str, tf: str) -> str:
    """BTC/USDT:USDT + 1h -> BTC-USDT-USDT_1h.csv (безопасно для файловой системы)."""
    return f"{symbol.replace('/', '-').replace(':', '-')}_{tf}.csv"


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Выгрузка OHLCV в CSV-кэш для бэктестов")
    p.add_argument("--symbols", required=True, help="CSV символов")
    p.add_argument("--tfs", default="1h", help="CSV таймфреймов (напр. 1h,4h)")
    p.add_argument("--months", type=float, default=12.0)
    p.add_argument("--out", default="data/cache")
    return p.parse_args(argv)


def main() -> None:
    log = get_logger("dump_history")
    args = parse_args()
    from app.data.market_data import _TF_SECONDS, fetch_history

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    tfs = [t.strip() for t in args.tfs.split(",") if t.strip()]
    for symbol in symbols:
        for tf in tfs:
            bars = int(args.months * 30.44 * 86400 / _TF_SECONDS[tf])
            try:
                df = fetch_history(symbol, tf, target_bars=bars)
            except Exception as e:                     # noqa: BLE001 — один символ не рушит выгрузку
                log.warning("%s %s: %s", symbol, tf, e)
                continue
            path = out / cache_name(symbol, tf)
            df.to_csv(path, index=False)
            log.info("%s %s -> %s (%d баров, %s..%s)", symbol, tf, path, len(df),
                     df["dt"].iloc[0], df["dt"].iloc[-1])


if __name__ == "__main__":
    main()
