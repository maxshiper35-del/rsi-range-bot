"""
smc_setups.py — сборка детекторов SMC в готовый Signal (заметкам стратегии (SMC), §2).

Реализовано:
  §2.1  build_6226        — флагман: свип -> слом(закреп телом) -> фибо 1/0.62/0/-0.26 ->
                           вход 0.62, тейк -0.26, стоп 1, RR 2.26, окно входа 7-14 (UTC-4).
  §2.x  detect_setup_1_1  — самодостаточный: свип свинга -> импульс -> фибо 0/0.5/1 ->
                           вход 0.5, стоп 1, тейк 0, RR 1:1.
  §2.3  SMCConfig         — расширение StrategyConfig под SMC.
  §2.4  confidence-модель — 0.30 bias + 0.30 триггер(слом) + 0.20 свип + 0.20 конфлюэнс,
                           с жёсткими гейтами (инверсия зоны входа, окно времени, закреп телом).
  §3    SMCEngine         — analyze(frames)/analyze_single(df) в контракте SignalEngine.

Выход — Signal из signal_engine (совместим с journal/bot/format_message). Детекторы —
из smc_detectors; окна времени — из smc_time. Дискреционка (§4) не гейтит жёстко, идёт
мягкими суб-скорами в confidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from app.brain import smc_detectors as det
from app.brain.smc_detectors import Bias, Kind, LiquidityLevel
from app.core.signal_engine import Side, Signal, StrategyConfig


# ---------------------------------------------------------------------------
# §2.3 Конфиг SMC
# ---------------------------------------------------------------------------
@dataclass
class SMCConfig(StrategyConfig):
    # структура / ликвидность
    swing_left: int = 2
    swing_right: int = 2
    eq_tol_atr: float = 0.1
    pool_min_count: int = 2
    sweep_lookback: int = 20
    require_body_close: bool = True
    fvg_min_gap_atr: float = 0.0
    max_impulse_wick_ratio: float = 0.33
    entry_band_atr: float = 0.5              # «у входа» = в пределах band*ATR
    min_impulse_atr: float = 1.0             # мин. размер импульса в ATR (фильтр мелкой структуры)
    min_rr_open_model: float = 0.5           # мин. reward/risk для open-SMC (build_opening_model)

    # фибо и RR (дословно со слайдов)
    fib_entry_6226: float = 0.62
    fib_take_6226: float = -0.26
    rr_6226: float = 2.26
    rr_setup11: float = 1.0

    # время (окна нормализованы в UTC; исходно New-York, UTC-4)
    use_time_filter: bool = True
    entry_window_utc: tuple = (11 * 60, 18 * 60)   # NY-4 07:00-14:00 -> UTC 11:00-18:00
    after_break_utc: int = 11 * 60                  # NY-4 07:00 -> UTC 11:00

    # веса SMC-confidence (сумма = 1.0)
    w_bias: float = 0.30
    w_trigger: float = 0.30
    w_sweep: float = 0.20
    w_confluence: float = 0.20
    # min_confidence наследуется из StrategyConfig (0.5)

    # какие сетапы включены; "LTF" покрывает LTF-BB и LTF-OB (матч по префиксу)
    enabled_setups: tuple = ("6226", "1:1", "open-SMC", "LTF")


# ---------------------------------------------------------------------------
# Время бара
# ---------------------------------------------------------------------------
def _bar_minute_utc(df: pd.DataFrame, i: int) -> int | None:
    """Минута дня в UTC для бара i, если в df есть колонка dt; иначе None."""
    if "dt" not in df.columns:
        return None
    ts = pd.to_datetime(df["dt"].iloc[i], utc=True)
    return int(ts.hour) * 60 + int(ts.minute)


# ---------------------------------------------------------------------------
# Bias: тренд HTF + премиум/дискаунт (§2.4, компонент 0.30)
# ---------------------------------------------------------------------------
def dealing_range(swings: list[det.Swing]) -> tuple[float, float] | None:
    highs = [s.price for s in swings if s.kind == Kind.HIGH]
    lows = [s.price for s in swings if s.kind == Kind.LOW]
    if not highs or not lows:
        return None
    return min(lows), max(highs)


def _bias_score(df: pd.DataFrame, side: Side, htf_df: pd.DataFrame | None,
                cfg: SMCConfig) -> float:
    ref = htf_df if htf_df is not None else df
    ref_sw = det.find_swings(ref, cfg.swing_left, cfg.swing_right)
    trend = det.market_structure(ref, ref_sw)
    want = Bias.BEAR if side == Side.SHORT else Bias.BULL
    score = 0.5
    if trend == want:
        score += 0.25
    dr = dealing_range(det.find_swings(df, cfg.swing_left, cfg.swing_right))
    if dr:
        lo, hi = dr
        mid = (lo + hi) / 2.0
        price = float(df["close"].iloc[-1])
        in_premium = price > mid
        if (side == Side.SHORT and in_premium) or (side == Side.LONG and not in_premium):
            score += 0.25
    return min(score, 1.0)


# ---------------------------------------------------------------------------
# Конфлюэнс (§2.4, компонент 0.20): FVG/OB у входа, EQH/EQL, окно времени, чистота импульса
# ---------------------------------------------------------------------------
def _confluence_score(df: pd.DataFrame, entry: float, side: Side, cfg: SMCConfig,
                      swings: list[det.Swing], clean_impulse: bool,
                      in_entry_window: bool | None) -> tuple[float, list[str]]:
    atr = det.atr_scalar(df)
    band = cfg.entry_band_atr * atr
    reasons: list[str] = []
    score = 0.0

    fvgs = det.detect_fvg(df, min_gap_atr=cfg.fvg_min_gap_atr)
    near = [z for z in fvgs if z.bottom - band <= entry <= z.top + band]
    if near:
        # правило инверсии: инвертированная зона входа не даёт бонуса (мягкий штраф),
        # а не жёсткий отказ — выбор «ближайшего FVG» слишком грубый для hard-гейта.
        z = det.apply_inversion_rule(df, near[0])
        if z.inverted:
            reasons.append("зона входа инвертирована (перекрыта телом) — без бонуса")
        else:
            score += 0.4
            reasons.append("FVG/имбаланс у входа")

    eqs = det.find_equal_levels(swings, atr, cfg.eq_tol_atr)
    if any(e.kind == "EQH" for e in eqs) or any(e.kind == "EQL" for e in eqs):
        score += 0.3
        reasons.append("равные экстремумы (EQH/EQL) — доп. ликвидность")

    if in_entry_window:
        score += 0.2
        reasons.append("вход в окне 7:00-14:00 (UTC-4)")

    if clean_impulse:
        score += 0.1
        reasons.append("импульс без длинных теней")

    return min(score, 1.0), reasons


def _confidence(bias: float, trigger: float, sweep: float, confl: float,
                cfg: SMCConfig) -> float:
    return (cfg.w_bias * bias + cfg.w_trigger * trigger
            + cfg.w_sweep * sweep + cfg.w_confluence * confl)


# ---------------------------------------------------------------------------
# Ядро: найти «свип -> слом -> импульс» и его концы S(fib1) / E(fib0)
# ---------------------------------------------------------------------------
@dataclass
class _Impulse:
    side: Side
    S: float            # начало импульса (фибо 1) = снятый экстремум
    E: float            # конец импульса (фибо 0)
    s_index: int
    e_index: int
    swept: bool
    clean: bool
    brk: det.StructureBreak
    swings: list[det.Swing]


def _find_impulse(df: pd.DataFrame, cfg: SMCConfig) -> _Impulse | None:
    swings = det.find_swings(df, cfg.swing_left, cfg.swing_right)
    if len(swings) < 4:
        return None
    trend = det.market_structure(df, swings)
    brk = det.detect_structure_break(df, swings, trend, cfg.require_body_close)
    if brk is None:
        return None
    side = Side.SHORT if brk.direction == Bias.BEAR else Side.LONG

    low = df["low"].to_numpy()
    high = df["high"].to_numpy()
    if side == Side.SHORT:
        cand = [s for s in swings if s.kind == Kind.HIGH and s.index < brk.break_index]
        if not cand:
            return None
        # ближайший к слому хай, а не макс. цена за окно — иначе якорится на давно
        # неактуальный экстремум (LAB 2026-07-11: 200-барное окно ещё захватывало
        # до-обвальный хай в 15x текущей цены -> недостижимый вход)
        top = cand[-1]
        s_index, S = top.index, top.price
        e_index = int(s_index + low[s_index:].argmin())
        E = float(low[e_index])
        level = LiquidityLevel(S, s_index, "SWING")
    else:
        cand = [s for s in swings if s.kind == Kind.LOW and s.index < brk.break_index]
        if not cand:
            return None
        bot = cand[-1]                                   # ближайший к слому лой (см. выше)
        s_index, S = bot.index, bot.price
        e_index = int(s_index + high[s_index:].argmax())
        E = float(high[e_index])
        level = LiquidityLevel(S, s_index, "SWING")

    if e_index <= s_index or abs(S - E) < 1e-9:
        return None

    # фильтр мелкой структуры: импульс должен быть >= min_impulse_atr * ATR,
    # иначе стопы получаются скальп-тесными (шум) — низкое качество сигнала
    if cfg.min_impulse_atr > 0:
        atr = det.atr_scalar(df)
        if atr > 0 and abs(S - E) < cfg.min_impulse_atr * atr:
            return None

    sweep = det.detect_sweep(df, level, lookback=cfg.sweep_lookback)
    clean = _impulse_clean(df, s_index, e_index, cfg.max_impulse_wick_ratio)
    return _Impulse(side, float(S), float(E), s_index, e_index,
                    swept=sweep is not None, clean=clean, brk=brk, swings=swings)


def _impulse_clean(df: pd.DataFrame, i0: int, i1: int, max_ratio: float) -> bool:
    """Импульс «некорректируемый» — средняя доля тени в баре <= max_ratio."""
    seg = df.iloc[i0:i1 + 1]
    rng = (seg["high"] - seg["low"]).replace(0.0, float("nan"))
    body = (seg["close"] - seg["open"]).abs()
    wick_ratio = (1.0 - (body / rng)).dropna()
    if wick_ratio.empty:
        return False
    return float(wick_ratio.mean()) <= max_ratio


# ---------------------------------------------------------------------------
# §2.1 Флагман 6226
# ---------------------------------------------------------------------------
def build_6226(df: pd.DataFrame, symbol: str, cfg: SMCConfig | None = None,
               timeframe: str = "m15", htf_df: pd.DataFrame | None = None) -> Signal | None:
    """свип -> слом(закреп телом) -> фибо 1/0.62/0/-0.26 -> вход 0.62, тейк -0.26,
    стоп 1, RR 2.26."""
    cfg = cfg or SMCConfig()
    imp = _find_impulse(df, cfg)
    if imp is None:
        return None

    span = imp.S - imp.E
    entry = imp.E + cfg.fib_entry_6226 * span      # 0.62
    take = imp.E + cfg.fib_take_6226 * span        # -0.26
    stop = imp.S                                   # уровень 1

    # --- гейт времени (жёсткий, если есть метки времени) ---
    in_entry_window: bool | None = None
    if cfg.use_time_filter:
        m_break = _bar_minute_utc(df, imp.brk.break_index)
        m_now = _bar_minute_utc(df, len(df) - 1)
        if m_break is not None and m_now is not None:
            lo, hi = cfg.entry_window_utc
            in_entry_window = lo <= m_now < hi
            if m_break < cfg.after_break_utc or not in_entry_window:
                return None                        # вне окна — отбраковка

    bias = _bias_score(df, imp.side, htf_df, cfg)
    trigger = 1.0 if imp.brk.body_confirmed else 0.0
    sweep = 1.0 if imp.swept else 0.5
    confl, cf_reasons = _confluence_score(
        df, entry, imp.side, cfg, imp.swings, imp.clean, in_entry_window)

    confidence = _confidence(bias, trigger, sweep, confl, cfg)
    if confidence < cfg.min_confidence:
        return None

    reasons = [
        f"свип {'хая' if imp.side == Side.SHORT else 'лоя'} (снятие ликвидности)",
        f"слом структуры {imp.brk.kind} (закреп телом)",
        "фибо 1/0.62/0/-0.26 по импульсу",
    ] + cf_reasons
    return _mk_signal(df, symbol, imp.side, timeframe, entry, stop, [take],
                      cfg.rr_6226, confidence, reasons, imp, setup="6226")


# ---------------------------------------------------------------------------
# §2.x Setup 1:1 (самодостаточный)
# ---------------------------------------------------------------------------
def detect_setup_1_1(df: pd.DataFrame, symbol: str, cfg: SMCConfig | None = None,
                     timeframe: str = "m5", htf_df: pd.DataFrame | None = None) -> Signal | None:
    """свип свинга -> импульс -> фибо 0/0.5/1 -> вход 0.5, стоп 1, тейк 0, RR 1:1."""
    cfg = cfg or SMCConfig()
    imp = _find_impulse(df, cfg)
    if imp is None:
        return None

    span = imp.S - imp.E
    entry = imp.E + 0.5 * span      # 0.5 (середина импульса)
    stop = imp.S                    # уровень 1
    take = imp.E                    # уровень 0

    bias = _bias_score(df, imp.side, htf_df, cfg)
    trigger = 1.0 if imp.brk.body_confirmed else 0.0
    sweep = 1.0 if imp.swept else 0.5
    confl, cf_reasons = _confluence_score(
        df, entry, imp.side, cfg, imp.swings, imp.clean, None)
    confidence = _confidence(bias, trigger, sweep, confl, cfg)
    if confidence < cfg.min_confidence:
        return None

    reasons = [
        f"свип {'хая' if imp.side == Side.SHORT else 'лоя'} свинга",
        "импульс, фибо 0/0.5/1",
        "вход 0.5, стоп 1, тейк 0 (RR 1:1)",
    ] + cf_reasons
    return _mk_signal(df, symbol, imp.side, timeframe, entry, stop, [take],
                      cfg.rr_setup11, confidence, reasons, imp, setup="1:1")


# ---------------------------------------------------------------------------
# §2.2 LTF-модель по POI (вход от OB/BB после слома)
# ---------------------------------------------------------------------------
def build_ltf_poi(htf_df: pd.DataFrame, ltf_df: pd.DataFrame, symbol: str,
                  cfg: SMCConfig | None = None, timeframe: str = "m5") -> Signal | None:
    """Слом LTF -> откат в OB/BB -> вход от MT(50%), стоп за дальнюю границу блока.

    HTF-тренд идёт как усилитель bias (совпадение направления += confidence).
    """
    cfg = cfg or SMCConfig()
    imp = _find_impulse(ltf_df, cfg)
    if imp is None:
        return None
    br_dir = Bias.BEAR if imp.side == Side.SHORT else Bias.BULL
    block = det.detect_order_block(ltf_df, imp.swings, imp.brk, br_dir)
    if block is None:
        return None

    entry = det.mean_threshold(block)                 # реакция от 50% блока
    rr = float(max(cfg.tp_r_multiples))
    if imp.side == Side.SHORT:
        stop = block.top                              # дальняя граница блока
        risk = stop - entry
        if risk <= 0:
            return None
        take = entry - rr * risk
    else:
        stop = block.bottom
        risk = entry - stop
        if risk <= 0:
            return None
        take = entry + rr * risk

    htf_sw = det.find_swings(htf_df, cfg.swing_left, cfg.swing_right)
    htf_trend = det.market_structure(htf_df, htf_sw)
    aligned = ((htf_trend == Bias.BEAR and imp.side == Side.SHORT)
               or (htf_trend == Bias.BULL and imp.side == Side.LONG))
    bias = 0.75 if aligned else 0.5
    trigger = 1.0 if imp.brk.body_confirmed else 0.0
    sweep = 1.0 if imp.swept else 0.5
    confl, cf_reasons = _confluence_score(ltf_df, entry, imp.side, cfg, imp.swings, imp.clean, None)
    confidence = _confidence(bias, trigger, sweep, confl, cfg)
    if confidence < cfg.min_confidence:
        return None

    reasons = [
        "контекст/POI HTF" + (" (совпадает с трендом)" if aligned else ""),
        f"слом LTF {imp.brk.kind}",
        f"вход от {block.kind} (MT 50%), стоп за дальнюю границу блока",
    ] + cf_reasons
    return _mk_signal(ltf_df, symbol, imp.side, timeframe, entry, stop, [take],
                      round(rr, 2), confidence, reasons, imp, setup=f"LTF-{block.kind}")


# ---------------------------------------------------------------------------
# Модели открытия SMR / SMC / OF (вход в имбаланс/0.5 после слома)
# ---------------------------------------------------------------------------
def build_opening_model(df: pd.DataFrame, symbol: str, cfg: SMCConfig | None = None,
                        timeframe: str = "m5", model: str = "SMC",
                        htf_df: pd.DataFrame | None = None) -> Signal | None:
    """Откат в имбаланс (или 0.5) после слома; стоп за точку разворота/свип; тейк — уровень 0."""
    cfg = cfg or SMCConfig()
    imp = _find_impulse(df, cfg)
    if imp is None:
        return None
    span = imp.S - imp.E
    mid = imp.E + 0.5 * span
    lo, hi = min(imp.E, imp.S), max(imp.E, imp.S)
    cands = [z for z in det.detect_fvg(df)
             if lo <= (z.top + z.bottom) / 2.0 <= hi
             and not det.apply_inversion_rule(df, z).inverted]
    if cands:
        z = min(cands, key=lambda zz: abs((zz.top + zz.bottom) / 2.0 - mid))
        entry, ez = det.mean_threshold(z), "вход в имбаланс"
    else:
        entry, ez = mid, "вход 0.5"

    stop, take = imp.S, imp.E
    risk, reward = abs(entry - stop), abs(entry - take)
    if risk <= 0 or reward <= 0:
        return None
    rr = round(reward / risk, 2)
    # в отличие от 6226/1:1/LTF (тейк = фикс. доля риска, RR задан конструкцией), здесь entry
    # и take выбираются НЕЗАВИСИМО (entry — ближайший к mid FVG, take — фикс. уровень E) —
    # если единственный найденный FVG оказался у самого E, reward стремится к нулю при полном
    # risk (найдено на живой сделке #145: RR=0.02, стоп 2.58% вместо тейка 0.06%)
    if rr < cfg.min_rr_open_model:
        return None

    bias = _bias_score(df, imp.side, htf_df, cfg)
    trigger = 1.0 if imp.brk.body_confirmed else 0.0
    sweep = 1.0 if imp.swept else 0.5
    confl, cf_reasons = _confluence_score(df, entry, imp.side, cfg, imp.swings, imp.clean, None)
    confidence = _confidence(bias, trigger, sweep, confl, cfg)
    if confidence < cfg.min_confidence:
        return None

    reasons = [f"модель открытия {model}", ez, "стоп за точку разворота/свип"] + cf_reasons
    return _mk_signal(df, symbol, imp.side, timeframe, entry, stop, [take],
                      rr, confidence, reasons, imp, setup=f"open-{model}")


# ---------------------------------------------------------------------------
# Маппинг в Signal
# ---------------------------------------------------------------------------
def _signal_created_at(df: pd.DataFrame) -> str:
    """created_at = время закрытия ПОСЛЕДНЕГО бара (контракт схемы: «один сигнал на бар»).

    Именно на этом построен UNIQUE(exchange,symbol,timeframe,side,created_at) —
    повторный скан того же бара даёт тот же created_at и дедуплицируется БД.
    Fallback на wall-clock только если у df нет колонки dt (синтетика в тестах).
    """
    if "dt" in df.columns:
        ts = pd.to_datetime(df["dt"].iloc[-1], utc=True)
        return ts.isoformat()
    return datetime.now(timezone.utc).isoformat()


def _mk_signal(df: pd.DataFrame, symbol: str, side: Side, timeframe: str,
               entry: float, stop: float,
               tps: list[float], rr: float, confidence: float, reasons: list[str],
               imp: _Impulse, setup: str) -> Signal:
    # R/R ВСЕГДА из фактических уровней (до последнего тейка) — чтобы показанное
    # соотношение точно совпадало с показанными вход/стоп/тейк
    risk = abs(entry - stop)
    reward = abs(float(tps[-1]) - entry) if tps else 0.0
    actual_rr = round(reward / risk, 2) if risk > 0 else float(rr)
    market = round(float(df["close"].iloc[-1]), 8)  # цена рынка на момент сигнала
    return Signal(
        symbol=symbol,
        side=side,
        timeframe=timeframe,
        entry=round(float(entry), 8),
        stop=round(float(stop), 8),
        take_profits=[round(float(t), 8) for t in tps],
        risk_reward=actual_rr,
        confidence=round(float(confidence), 2),
        reasons=reasons,
        created_at=_signal_created_at(df),
        indicators_snapshot={
            "setup": setup,
            "market": market,
            "fib_anchors": [round(imp.S, 8), round(imp.E, 8)],
            "break": imp.brk.kind,
            "break_index": imp.brk.break_index,
            "swept": imp.swept,
            "clean_impulse": imp.clean,
        },
    )


# ---------------------------------------------------------------------------
# §3 Движок SMC (контракт SignalEngine)
# ---------------------------------------------------------------------------
class SMCEngine:
    """Оркестратор сетапов SMC. Возвращает Signal с наибольшей confidence либо None."""

    # какие ТФ считаем «входными» и «контекстными» в frames
    ENTRY_TFS = ("m5", "M5", "5m", "m15", "M15", "15m")
    HTF_TFS = ("H1", "1h", "h1", "H4", "4h", "h4", "D1", "1d", "d1")
    # длительность ТФ в минутах — для fallback-выбора самого младшего как входного
    TF_MINUTES = {"m1": 1, "1m": 1, "m3": 3, "3m": 3, "m5": 5, "5m": 5, "M5": 5,
                  "m15": 15, "15m": 15, "M15": 15, "m30": 30, "30m": 30,
                  "1h": 60, "H1": 60, "h1": 60, "4h": 240, "H4": 240, "h4": 240,
                  "1d": 1440, "D1": 1440, "d1": 1440}

    def __init__(self, config: SMCConfig | None = None):
        self.cfg = config or SMCConfig()

    def _setup_enabled(self, tag: str) -> bool:
        """Тег включён при префикс-матче в ЛЮБУЮ сторону: enabled=('LTF',) покрывает
        LTF-BB/LTF-OB, а enabled=('LTF-BB',) проходит пре-гейт 'LTF' (точная фильтрация
        делается пост-фильтром по snapshot['setup'])."""
        return any(e and (tag == e or tag.startswith(e) or e.startswith(tag))
                   for e in self.cfg.enabled_setups)

    def analyze_single(self, df: pd.DataFrame, symbol: str, timeframe: str,
                       htf_df: pd.DataFrame | None = None) -> Signal | None:
        candidates: list[Signal | None] = []
        if self._setup_enabled("6226"):
            candidates.append(build_6226(df, symbol, self.cfg, timeframe, htf_df))
        if self._setup_enabled("1:1"):
            candidates.append(detect_setup_1_1(df, symbol, self.cfg, timeframe, htf_df))
        if self._setup_enabled("open-SMC"):
            candidates.append(build_opening_model(df, symbol, self.cfg, timeframe, "SMC", htf_df))
        if self._setup_enabled("LTF"):
            candidates.append(build_ltf_poi(htf_df if htf_df is not None else df, df,
                                            symbol, self.cfg, timeframe))
        # страховочный фильтр по фактическому тегу из snapshot
        signals = [c for c in candidates if c is not None]
        signals = [c for c in signals
                  if self._setup_enabled((c.indicators_snapshot or {}).get("setup", ""))]
        if not signals:
            return None
        return max(signals, key=lambda s: s.confidence)

    def analyze(self, frames: dict[str, pd.DataFrame], symbol: str) -> Signal | None:
        entry_tf = next((tf for tf in frames if tf in self.ENTRY_TFS), None)
        if entry_tf is None:                       # fallback: САМЫЙ МЛАДШИЙ по длительности
            entry_tf = min(frames, key=lambda t: self.TF_MINUTES.get(t, 10 ** 9))
        htf = next((frames[tf] for tf in frames
                    if tf in self.HTF_TFS and tf != entry_tf), None)
        return self.analyze_single(frames[entry_tf], symbol, entry_tf, htf_df=htf)

    def analyze_multi(self, frames: dict[str, pd.DataFrame], symbol: str) -> list[Signal]:
        """Сигнал на КАЖДОМ входном ТФ, а не один «лучший» — для торговли сразу на
        15m/1h/4h. Для каждого ТФ контекстом берётся ближайший СТАРШИЙ ТФ из набора
        (для самого старшего htf=None — bias считается по самому входному фрейму).
        -> список сигналов (0..len(frames)); дедуп по (symbol,tf,side,entry,stop) —
        выше по стеку (scan_auto/scan_semiauto)."""
        tfs = sorted(frames, key=lambda t: self.TF_MINUTES.get(t, 10 ** 9))
        out: list[Signal] = []
        for i, tf in enumerate(tfs):
            htf = next((frames[h] for h in tfs[i + 1:]), None)   # ближайший старший
            sig = self.analyze_single(frames[tf], symbol, tf, htf_df=htf)
            if sig is not None:
                out.append(sig)
        return out
