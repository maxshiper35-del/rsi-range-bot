"""Предрасчёт сигналов движка по истории -> JSON-кэш для портфельной симуляции.

Зачем отдельным шагом: прогон движка по 8766 барам стоит ~45с на символ/ТФ, а
портфельных сценариев (лимиты, тормоз, наборы символов) нужно перебрать десятки.
Сигналы от портфельных правил НЕ зависят — движок видит только свой символ. Поэтому
считаем их ОДИН раз, а дальше портфельный слой гоняется за секунды.

  python3 -m scripts.dump_signals --symbols "BTC/USDT:USDT,..." --tfs 1h,4h \
      --setups "6226,1:1,LTF" --htf-map "1h=4h,4h=1d" --window 200 --time-filter

Формат записи: {symbol, tf, bar_index, dt, side, setup, entry, stop, tp, rr, confidence, scores}.
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
                 window: int, htf_tf: str | None, min_bars: int = 20,
                 partner_df: pd.DataFrame | None = None) -> list[dict]:
    """Пройти историю бар за баром и записать КАЖДЫЙ сигнал движка (без симуляции сделок).

    Без лукахеда: движку видно только окно закрытых баров до текущего включительно —
    ровно как вживую (poller отдаёт последние `window` закрытых баров).
    partner_df (SMT-вето): партнёру нарезается окно ПО ВРЕМЕНИ — только бары с ts не
    позже текущего бара своего актива, последние `window` штук — синхронно и без
    лукахеда даже при дырах в данных партнёра."""
    engine = SMCEngine(cfg)
    htf_minutes = SMCEngine.TF_MINUTES.get(htf_tf, 0) if htf_tf else 0
    p_ts = partner_df["ts"].to_numpy() if partner_df is not None else None
    out: list[dict] = []
    for i in range(min_bars, len(df)):
        win = df.iloc[max(0, i - window + 1):i + 1].reset_index(drop=True)
        htf_win = _resample_htf(win, htf_minutes) if htf_minutes else None
        p_win = None
        if partner_df is not None:
            j = int(p_ts.searchsorted(df["ts"].iloc[i], side="right"))
            if j >= min_bars:
                p_win = partner_df.iloc[max(0, j - window):j].reset_index(drop=True)
        sig = engine.analyze_single(win, symbol, tf, htf_df=htf_win, partner_df=p_win)
        if sig is None:
            continue
        out.append({
            "symbol": symbol, "tf": tf, "bar_index": i,
            "dt": str(df["dt"].iloc[i]),
            "side": sig.side.value, "setup": (sig.indicators_snapshot or {}).get("setup", ""),
            "entry": float(sig.entry), "stop": float(sig.stop),
            "tp": float(sig.take_profits[0]), "rr": float(sig.risk_reward),
            "confidence": float(sig.confidence),
            # суб-скоры (bias/trigger/sweep/confl) — для замеров гейтов по компонентам;
            # портфельный слой лишние ключи игнорирует
            "scores": (sig.indicators_snapshot or {}).get("scores"),
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
    p.add_argument("--swing", type=int, default=None,
                   help="баров слева/справа для свинга (1 = как в первоисточнике, 2 = дефолт)")
    p.add_argument("--ob-single-candle", action="store_true",
                   help="старое поведение: OB/BB = одна свеча-экстремум, а не всё движение")
    p.add_argument("--break-window", default=None,
                   help="окно съёма/слома в NY-минутах, напр. '120,660' (2:00-11:00)")
    p.add_argument("--enforce-body-close", action="store_true",
                   help="A/B: слом только закрытием (без закрепа телом) отбрасывается")
    p.add_argument("--veto-break-hpz", action="store_true",
                   help="A/B: вето «слом в имбаланс» — закрытие бара слома внутри "
                        "противоположной неинверсированной FVG/GAP/BPR")
    p.add_argument("--smt-veto-pair", default=None,
                   help="A/B: SMT-вето, партнёры CSV 'BTC/USDT:USDT=ETH/USDT:USDT,...' — "
                        "символ без партнёра сканируется без вето")
    p.add_argument("--cache", default="data/cache")
    p.add_argument("--out", default="data/cache/signals")
    return p.parse_args(argv)


def main() -> None:
    log = get_logger("dump_signals")
    args = parse_args()
    cache, out = Path(args.cache), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    htf_map = parse_htf_map(args.htf_map)
    extra: dict = {}
    if args.swing is not None:
        extra["swing_left"] = args.swing
        extra["swing_right"] = args.swing
    if args.break_window:
        lo, hi = (int(x) for x in args.break_window.split(","))
        extra["break_window_ny"] = (lo, hi)
    smt_pairs = parse_htf_map(args.smt_veto_pair or "")   # тот же формат 'A=B,C=D'
    cfg = SMCConfig(
        use_time_filter=args.time_filter,
        htf_block_countertrend=args.htf_block,
        min_confidence=args.min_conf,
        min_impulse_atr=args.min_impulse,
        enabled_setups=tuple(s.strip() for s in args.setups.split(",") if s.strip()),
        ob_full_move=not args.ob_single_candle,
        enforce_body_close=args.enforce_body_close,
        veto_break_into_hpz=args.veto_break_hpz,
        smt_mode="veto" if smt_pairs else "off",
        **extra,
    )
    for symbol in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        for tf in [t.strip() for t in args.tfs.split(",") if t.strip()]:
            path = cache / cache_name(symbol, tf)
            if not path.exists():
                log.warning("нет кэша баров: %s", path)
                continue
            df = pd.read_csv(path, parse_dates=["dt"])
            partner_df = None
            if smt_pairs.get(symbol):
                p_path = cache / cache_name(smt_pairs[symbol], tf)
                if p_path.exists():
                    partner_df = pd.read_csv(p_path, parse_dates=["dt"])
                else:
                    log.warning("SMT: нет кэша партнёра %s — %s без вето", p_path, symbol)
            sigs = scan_signals(df, symbol, tf, cfg, args.window, htf_map.get(tf),
                                partner_df=partner_df)
            dest = out / f"{cache_name(symbol, tf)[:-4]}.json"
            dest.write_text(json.dumps(sigs, ensure_ascii=False))
            log.info("%s %s: %d сигналов -> %s", symbol, tf, len(sigs), dest)


if __name__ == "__main__":
    main()
