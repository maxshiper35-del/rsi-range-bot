"""Предрасчёт сигналов движка по истории -> JSON-кэш для портфельной симуляции.

Зачем отдельным шагом: прогон движка по 8766 барам стоит ~45с на символ/ТФ, а
портфельных сценариев (лимиты, тормоз, наборы символов) нужно перебрать десятки.
Сигналы от портфельных правил НЕ зависят — движок видит только свой символ. Поэтому
считаем их ОДИН раз, а дальше портфельный слой гоняется за секунды.

  python3 -m scripts.dump_signals --symbols "BTC/USDT:USDT,..." --tfs 1h,4h \
      --setups "6226,1:1,LTF" --htf-map "1h=4h,4h=1d" --window 200 --time-filter

  # паритет с продом (06.10.2026): конфиг движка — как у живого поллера (runtime),
  # старший ТФ — закрытые свечи из кэша старшего ТФ, окно 199 закрытых баров
  python3 -m scripts.dump_signals --prod --symbols "..." --tfs 1h,4h \
      --cache data/cache5y --out data/cache5y/sig_prod

Почему --prod, а не набор флагов: движок выбирает ОДНОГО кандидата на бар (max
confidence) ПОСЛЕ гейтов enabled_setups и min_stop_pct. Снимок со всеми сетапами и без
min_stop, отфильтрованный уже в портфельном слое, теряет сигналы, которые вживую
выиграли бы бар (1:1 или тесный стоп перебивал 6226/LTF): на BTC 1h — 441 сигнал
вместо 891.

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
                 partner_df: pd.DataFrame | None = None,
                 htf_bars: pd.DataFrame | None = None,
                 only=None) -> list[dict]:
    """Пройти историю бар за баром и записать КАЖДЫЙ сигнал движка (без симуляции сделок).

    Без лукахеда: движку видно только окно закрытых баров до текущего включительно —
    ровно как вживую (poller отдаёт последние `window` закрытых баров).
    partner_df (SMT-вето): партнёру нарезается окно ПО ВРЕМЕНИ — только бары с ts не
    позже текущего бара своего актива, последние `window` штук — синхронно и без
    лукахеда даже при дырах в данных партнёра.
    htf_bars (паритет с продом): старший ТФ берётся из его собственного кэша — только
    свечи, ЗАКРЫТЫЕ к закрытию текущего бара, последние `window` штук (вживую
    fetch_closed_ohlcv отбрасывает формирующуюся свечу). Без htf_bars — ресемпл окна
    (частичная последняя свеча, ~window/4 свечей истории).
    only (булева маска длины df): сканировать только эти бары — движок без состояния между
    вызовами, поэтому сигнал на баре тот же, что при полном проходе (замеры по окнам)."""
    engine = SMCEngine(cfg)
    htf_minutes = SMCEngine.TF_MINUTES.get(htf_tf, 0) if htf_tf else 0
    p_ts = partner_df["ts"].to_numpy() if partner_df is not None else None
    tf_ms = SMCEngine.TF_MINUTES.get(tf, 60) * 60_000
    htf_close = (htf_bars["ts"].to_numpy() + htf_minutes * 60_000
                 if htf_bars is not None and htf_minutes else None)
    out: list[dict] = []
    for i in range(min_bars, len(df)):
        if only is not None and not only[i]:
            continue
        win = df.iloc[max(0, i - window + 1):i + 1].reset_index(drop=True)
        if htf_close is not None:
            k = int(htf_close.searchsorted(df["ts"].iloc[i] + tf_ms, side="right"))
            htf_win = (htf_bars.iloc[max(0, k - window):k].reset_index(drop=True)
                       if k >= min_bars else None)
        else:
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
    p.add_argument("--min-stop-pct", type=float, default=0.0,
                   help="гейт движка ДО выбора кандидата (как runtime.min_stop_pct вживую)")
    p.add_argument("--htf-closed", action="store_true",
                   help="старший ТФ из его кэша, только закрытые свечи (как вживую)")
    p.add_argument("--prod", action="store_true",
                   help="паритет с продом: конфиг из runtime, старший ТФ из набора --tfs "
                        "закрытыми свечами, окно 199; остальные флаги конфига игнорируются")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="только с --prod: вариант замера поверх прод-конфига, напр. "
                        "--set min_stop_pct=1.0 --set enabled_setups=6226,1:1,LTF")
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
    if args.prod:
        cfg, htf_map = _prod_config(args)
        (out / "config.json").write_text(json.dumps(
            {"tfs": args.tfs, "window": args.window, "htf_map": htf_map,
             "cfg": {k: getattr(cfg, k) for k in _PROD_KEYS}}, ensure_ascii=False, indent=1))
        log.info("--prod: %s", {k: getattr(cfg, k) for k in _PROD_KEYS})
    else:
        cfg = _flags_config(args, extra, smt_pairs)
    tfs = [t.strip() for t in args.tfs.split(",") if t.strip()]
    for symbol in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        for tf in tfs:
            path = cache / cache_name(symbol, tf)
            if not path.exists():
                log.warning("нет кэша баров: %s", path)
                continue
            df = pd.read_csv(path, parse_dates=["dt"])
            partner_df = None
            if smt_pairs.get(symbol) and not args.prod:
                p_path = cache / cache_name(smt_pairs[symbol], tf)
                if p_path.exists():
                    partner_df = pd.read_csv(p_path, parse_dates=["dt"])
                else:
                    log.warning("SMT: нет кэша партнёра %s — %s без вето", p_path, symbol)
            htf_tf, htf_bars = htf_map.get(tf), None
            if htf_tf and (args.prod or args.htf_closed):
                htf_path = cache / cache_name(symbol, htf_tf)
                if not htf_path.exists():
                    raise SystemExit(f"нет кэша старшего ТФ {htf_path} — паритет невозможен")
                htf_bars = pd.read_csv(htf_path, parse_dates=["dt"])
            sigs = scan_signals(df, symbol, tf, cfg, args.window, htf_tf,
                                partner_df=partner_df, htf_bars=htf_bars)
            dest = out / f"{cache_name(symbol, tf)[:-4]}.json"
            dest.write_text(json.dumps(sigs, ensure_ascii=False))
            log.info("%s %s: %d сигналов -> %s", symbol, tf, len(sigs), dest)


_PROD_KEYS = ("enabled_setups", "min_confidence", "min_impulse_atr", "min_rr_open_model",
              "htf_block_countertrend", "min_stop_pct", "use_time_filter", "ob_full_move",
              "smt_mode", "late_entry_gate")


def _prod_config(args) -> tuple[SMCConfig, dict]:
    """Конфиг — ровно как app/data/market_data.py собирает движок поллера; старший ТФ —
    как SMCEngine.analyze_multi: ближайший старший из набора, у старшего — нет."""
    from dotenv import load_dotenv

    from app import runtime
    load_dotenv(".env")         # runtime читает настройки прода из его БД
    cfg = SMCConfig(
        min_confidence=runtime.min_confidence(),
        min_impulse_atr=runtime.min_impulse_atr(),
        min_rr_open_model=runtime.min_rr_open_model(),
        enabled_setups=runtime.enabled_setups(),
        htf_block_countertrend=runtime.htf_block_countertrend(),
        min_stop_pct=runtime.min_stop_pct(),
        late_entry_gate=runtime.late_entry_gate(),
        use_time_filter=runtime.time_filter_enabled())
    for item in args.set:
        key, _, raw = item.partition("=")
        cur = getattr(cfg, key)                     # AttributeError — опечатка в ключе
        if isinstance(cur, tuple):
            val = tuple(x.strip() for x in raw.split(",") if x.strip())
        elif isinstance(cur, bool):
            val = raw.strip().lower() in ("1", "true", "yes", "on")
        else:
            val = type(cur)(raw)
        setattr(cfg, key, val)
    tfs = sorted((t.strip() for t in args.tfs.split(",") if t.strip()),
                 key=lambda t: SMCEngine.TF_MINUTES.get(t, 10 ** 9))
    htf_map = {tf: (tfs[i + 1] if i + 1 < len(tfs) else None) for i, tf in enumerate(tfs)}
    if args.window == 200:      # дефолт флага -> живое окно: limit=200 минус формирующийся
        args.window = 199
    return cfg, htf_map


def _flags_config(args, extra: dict, smt_pairs: dict) -> SMCConfig:
    return SMCConfig(
        min_stop_pct=args.min_stop_pct,
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


if __name__ == "__main__":
    main()
