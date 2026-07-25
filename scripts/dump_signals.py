"""Предрасчёт сигналов движка по истории -> JSON-кэш для портфельной симуляции.

Зачем отдельным шагом: прогон движка по 8766 барам стоит ~45с на символ/ТФ, а
портфельных сценариев (лимиты, тормоз, наборы символов) нужно перебрать десятки.
Сигналы от портфельных правил НЕ зависят — движок видит только свой символ. Поэтому
считаем их ОДИН раз, а дальше портфельный слой гоняется за секунды.

  python3 -m scripts.dump_signals --symbols "BTC/USDT:USDT,..." --tfs 1h,4h \
      --setups "6226,1:1,LTF" --htf-map "1h=4h,4h=1d" --window 200 --time-filter

Формат записи: {symbol, tf, bar_index, dt, side, setup, entry, stop, tp, rr, confidence}.
Индекс бара — позиция В ЭТОМ CSV (портфельный слой читает тот же кэш баров).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from app.brain.smc_setups import SMCConfig, SMCEngine
from app.logging_conf import get_logger
from app.sim.backtest import _resample_htf
from scripts.dump_history import cache_name


def parse_htf_map(raw: str) -> dict:
    """'1h=4h,4h=1d' -> {'1h': '4h', '4h': '1d'}. Пустая правая часть = без контекста."""
    out = {}
    for part in (raw or "").split(","):
        if not part.strip():
            continue
        tf, _, htf = part.partition("=")
        out[tf.strip()] = htf.strip() or None
    return out


def scan_signals(df: pd.DataFrame, symbol: str, tf: str, cfg: SMCConfig,
                 window: int, htf_tf: str | None, min_bars: int = 20) -> list[dict]:
    """Пройти историю бар за баром и записать КАЖДЫЙ сигнал движка (без симуляции сделок).

    Без лукахеда: движку видно только окно закрытых баров до текущего включительно —
    ровно как вживую (poller отдаёт последние `window` закрытых баров)."""
    engine = SMCEngine(cfg)
    htf_minutes = SMCEngine.TF_MINUTES.get(htf_tf, 0) if htf_tf else 0
    out: list[dict] = []
    for i in range(min_bars, len(df)):
        win = df.iloc[max(0, i - window + 1):i + 1].reset_index(drop=True)
        htf_win = _resample_htf(win, htf_minutes) if htf_minutes else None
        sig = engine.analyze_single(win, symbol, tf, htf_df=htf_win)
        if sig is None:
            continue
        out.append({
            "symbol": symbol, "tf": tf, "bar_index": i,
            "dt": str(df["dt"].iloc[i]),
            "side": sig.side.value, "setup": (sig.indicators_snapshot or {}).get("setup", ""),
            "entry": float(sig.entry), "stop": float(sig.stop),
            "tp": float(sig.take_profits[0]), "rr": float(sig.risk_reward),
            "confidence": float(sig.confidence),
        })
    return out


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Предрасчёт сигналов для портфельной симуляции")
    p.add_argument("--symbols", required=True)
    p.add_argument("--tfs", default="1h,4h")
    p.add_argument("--setups", default="6226,1:1,LTF")
    p.add_argument("--htf-map", default="1h=4h,4h=1d")
    p.add_argument("--window", type=int, default=200)
    p.add_argument("--min-conf", type=float, default=0.5)
    p.add_argument("--min-impulse", type=float, default=1.0)
    p.add_argument("--time-filter", action="store_true")
    p.add_argument("--htf-block", action="store_true")
    p.add_argument("--cache", default="data/cache")
    p.add_argument("--out", default="data/cache/signals")
    return p.parse_args(argv)


def main() -> None:
    log = get_logger("dump_signals")
    args = parse_args()
    cache, out = Path(args.cache), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    htf_map = parse_htf_map(args.htf_map)
    cfg = SMCConfig(
        use_time_filter=args.time_filter,
        htf_block_countertrend=args.htf_block,
        min_confidence=args.min_conf,
        min_impulse_atr=args.min_impulse,
        enabled_setups=tuple(s.strip() for s in args.setups.split(",") if s.strip()),
    )
    for symbol in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        for tf in [t.strip() for t in args.tfs.split(",") if t.strip()]:
            path = cache / cache_name(symbol, tf)
            if not path.exists():
                log.warning("нет кэша баров: %s", path)
                continue
            df = pd.read_csv(path, parse_dates=["dt"])
            sigs = scan_signals(df, symbol, tf, cfg, args.window, htf_map.get(tf))
            dest = out / f"{cache_name(symbol, tf)[:-4]}.json"
            dest.write_text(json.dumps(sigs, ensure_ascii=False))
            log.info("%s %s: %d сигналов -> %s", symbol, tf, len(sigs), dest)


if __name__ == "__main__":
    main()
