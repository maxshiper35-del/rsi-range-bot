"""
smc_detectors.py — детекторы Smart Money / SMC поверх OHLCV.

Чистые функции над pandas, в стиле Indicators из signal_engine (без сети/БД).
Вход везде: df с колонками open/high/low/close/volume, ТОЛЬКО закрытые бары
(как в контракте SignalEngine.analyze). Индексация — позиционная (0..n-1).

Реализован приоритетный набор из плана (заметкам стратегии (SMC), §1):
  1.1 свинги (фракталы)              -> find_swings
  1.2 FVG / GAP / BPR                 -> detect_fvg / detect_gap / detect_bpr
  1.3 ликвидность EQH/EQL/POOL/PD-PW  -> find_equal_levels / find_pools / prev_period_levels
  1.4 свип ликвидности               -> detect_sweep
  1.5 слом структуры BOS/CHOCH       -> market_structure / detect_structure_break
  1.7 mitigation / инверсия          -> detect_mitigation / apply_inversion_rule

Сборка детекторов в Signal (§2 плана) — следующий шаг, здесь не реализуется.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Базовые типы (см. §0 плана кодирования)
# ---------------------------------------------------------------------------
class Kind(str, Enum):
    """Сторона точки/уровня ликвидности."""
    HIGH = "high"
    LOW = "low"


class Bias(str, Enum):
    """Направление зоны / структуры / слома."""
    BULL = "bull"
    BEAR = "bear"


@dataclass
class Swing:
    index: int
    price: float
    kind: Kind          # HIGH | LOW


@dataclass
class LiquidityLevel:
    price: float
    index: int
    kind: str           # "PDH"|"PDL"|"PWH"|"PWL"|"EQH"|"EQL"|"POOL"|"SWING"


@dataclass
class SweepEvent:
    level: LiquidityLevel
    index: int
    direction: Kind     # HIGH = снят хай (прокол вверх), LOW = снят лой
    confirmed: bool


@dataclass
class Zone:
    kind: str           # "FVG"|"OB"|"BB"|"BPR"|"GAP"|"BWG"
    top: float
    bottom: float
    index_start: int
    index_end: int
    bias: Bias
    mitigated: bool = False
    inverted: bool = False


@dataclass
class StructureBreak:
    kind: str           # "BOS" (в тренд) | "CHOCH" (разворот)
    pivot_price: float
    pivot_index: int
    break_index: int
    direction: Bias
    body_confirmed: bool


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------
def atr_scalar(df: pd.DataFrame, period: int = 14) -> float:
    """Скалярный ATR (медиана True Range) — масштаб для tol_atr/min_gap_atr."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift()
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    val = float(tr.tail(max(period, 1)).median())
    if not np.isfinite(val) or val <= 0:
        val = float(tr.median()) if np.isfinite(tr.median()) else 0.0
    return val


# ---------------------------------------------------------------------------
# 1.1 Свинги (фракталы) — фундамент всего
# ---------------------------------------------------------------------------
def find_swings(df: pd.DataFrame, left: int = 2, right: int = 2) -> list[Swing]:
    """Свинг — бар, чей high (low) строго экстремальнее соседних left/right баров.

    left=right=2 соответствует настройке WICK.ED Fractals («Периоды = 2»).
    """
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    n = len(df)
    out: list[Swing] = []
    for i in range(left, n - right):
        hi = highs[i]
        if hi > highs[i - left:i].max() and hi > highs[i + 1:i + right + 1].max():
            out.append(Swing(index=i, price=float(hi), kind=Kind.HIGH))
        lo = lows[i]
        if lo < lows[i - left:i].min() and lo < lows[i + 1:i + right + 1].min():
            out.append(Swing(index=i, price=float(lo), kind=Kind.LOW))
    return out


# ---------------------------------------------------------------------------
# 1.2 FVG / имбаланс, GAP, BPR
# ---------------------------------------------------------------------------
def detect_fvg(df: pd.DataFrame, lookback: int = 100, min_gap_atr: float = 0.0) -> list[Zone]:
    """Имбаланс из 3 свечей: тени 1-й и 3-й не пересекаются.

    Бычий:  low[i]  > high[i-2]  -> зона [high[i-2], low[i]].
    Медвежий: high[i] < low[i-2] -> зона [high[i], low[i-2]].
    Фильтр min_gap_atr: ширина зоны >= min_gap_atr * ATR.
    """
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    n = len(df)
    atr = atr_scalar(df) if min_gap_atr > 0 else 0.0
    start = max(2, n - lookback)
    out: list[Zone] = []
    for i in range(start, n):
        if low[i] > high[i - 2]:                       # бычий FVG
            top, bottom, bias = low[i], high[i - 2], Bias.BULL
        elif high[i] < low[i - 2]:                     # медвежий FVG
            top, bottom, bias = low[i - 2], high[i], Bias.BEAR
        else:
            continue
        if (top - bottom) < min_gap_atr * atr:
            continue
        out.append(Zone(kind="FVG", top=float(top), bottom=float(bottom),
                        index_start=i - 2, index_end=i, bias=bias))
    return out


def detect_gap(df: pd.DataFrame, lookback: int = 100) -> list[Zone]:
    """GAP — разрыв по телам: open[i] vs close[i-1]. Перекрывается в первую очередь."""
    open_ = df["open"].to_numpy()
    close = df["close"].to_numpy()
    n = len(df)
    start = max(1, n - lookback)
    out: list[Zone] = []
    for i in range(start, n):
        if open_[i] > close[i - 1]:
            out.append(Zone(kind="GAP", top=float(open_[i]), bottom=float(close[i - 1]),
                            index_start=i - 1, index_end=i, bias=Bias.BULL))
        elif open_[i] < close[i - 1]:
            out.append(Zone(kind="GAP", top=float(close[i - 1]), bottom=float(open_[i]),
                            index_start=i - 1, index_end=i, bias=Bias.BEAR))
    return out


def detect_bpr(fvgs: list[Zone]) -> list[Zone]:
    """BPR — два ПРОТИВОПОЛОЖНЫХ FVG, перекрывающих общий ценовой диапазон («магнит»)."""
    out: list[Zone] = []
    for a in fvgs:
        for b in fvgs:
            if a.index_end >= b.index_end or a.bias == b.bias:
                continue
            lo = max(a.bottom, b.bottom)
            hi = min(a.top, b.top)
            if hi > lo:                                # есть пересечение диапазонов
                out.append(Zone(kind="BPR", top=hi, bottom=lo,
                                index_start=min(a.index_start, b.index_start),
                                index_end=max(a.index_end, b.index_end),
                                bias=b.bias))
    return out


# ---------------------------------------------------------------------------
# 1.3 Ликвидность: EQH/EQL, POOL, PDH/PDL/PWH/PWL
# ---------------------------------------------------------------------------
def find_equal_levels(swings: list[Swing], atr: float, tol_atr: float = 0.1) -> list[LiquidityLevel]:
    """EQH/EQL — два соседних (по времени) однотипных свинга в пределах tol_atr*ATR."""
    tol = tol_atr * atr
    out: list[LiquidityLevel] = []
    for kind, tag in ((Kind.HIGH, "EQH"), (Kind.LOW, "EQL")):
        pts = [s for s in swings if s.kind == kind]
        for a, b in zip(pts, pts[1:]):
            if abs(b.price - a.price) <= tol:
                out.append(LiquidityLevel(price=(a.price + b.price) / 2.0,
                                          index=b.index, kind=tag))
    return out


def find_pools(swings: list[Swing], atr: float, tol_atr: float = 0.15,
               min_count: int = 2) -> list[LiquidityLevel]:
    """POOL — скопление >= min_count однотипных свингов в одном ценовом коридоре tol_atr*ATR."""
    tol = tol_atr * atr
    out: list[LiquidityLevel] = []
    for kind in (Kind.HIGH, Kind.LOW):
        pts = sorted((s for s in swings if s.kind == kind), key=lambda s: s.price)
        i = 0
        while i < len(pts):
            j = i
            while j + 1 < len(pts) and pts[j + 1].price - pts[i].price <= tol:
                j += 1
            group = pts[i:j + 1]
            if len(group) >= min_count:
                price = sum(g.price for g in group) / len(group)
                out.append(LiquidityLevel(price=price,
                                          index=max(g.index for g in group), kind="POOL"))
            i = j + 1
    return out


def prev_period_levels(df_htf: pd.DataFrame, dt_col: str = "dt") -> dict[str, LiquidityLevel]:
    """PDH/PDL/PWH/PWL с HTF-df (нужна колонка datetime dt_col).

    Берёт ПРЕДЫДУЩИЙ завершённый день/неделю относительно последнего бара.
    """
    if dt_col not in df_htf.columns:
        raise ValueError(f"нужна колонка времени '{dt_col}' для PDH/PDL/PWH/PWL")
    dt = pd.to_datetime(df_htf[dt_col], utc=True)
    d = df_htf.assign(_date=dt.dt.date, _week=dt.dt.isocalendar().week,
                      _year=dt.dt.isocalendar().year)
    out: dict[str, LiquidityLevel] = {}

    days = list(dict.fromkeys(d["_date"]))
    if len(days) >= 2:
        prev = d[d["_date"] == days[-2]]
        hi = prev["high"].idxmax(); lo = prev["low"].idxmin()
        out["PDH"] = LiquidityLevel(float(prev["high"].max()), int(hi), "PDH")
        out["PDL"] = LiquidityLevel(float(prev["low"].min()), int(lo), "PDL")

    weeks = list(dict.fromkeys(zip(d["_year"], d["_week"])))
    if len(weeks) >= 2:
        yr, wk = weeks[-2]
        prev = d[(d["_year"] == yr) & (d["_week"] == wk)]
        hi = prev["high"].idxmax(); lo = prev["low"].idxmin()
        out["PWH"] = LiquidityLevel(float(prev["high"].max()), int(hi), "PWH")
        out["PWL"] = LiquidityLevel(float(prev["low"].min()), int(lo), "PWL")
    return out


# ---------------------------------------------------------------------------
# 1.4 Свип ликвидности (снятие)
# ---------------------------------------------------------------------------
def detect_sweep(df: pd.DataFrame, level: LiquidityLevel, lookback: int = 20) -> SweepEvent | None:
    """Снятие уровня: тень прокалывает уровень, а тело закрывается обратно.

    Съём хая: high > level и close <= level (direction=HIGH).
    Съём лоя: low  < level и close >= level (direction=LOW).
    confirmed=True всегда (работаем на закрытых барах). Возвращает последний свип.
    """
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()
    n = len(df)
    L = level.price
    start = max(0, n - lookback)
    found: SweepEvent | None = None
    for i in range(start, n):
        up = high[i] > L and close[i] <= L
        down = low[i] < L and close[i] >= L
        if level.kind in ("PDH", "PWH", "EQH") and up:
            found = SweepEvent(level, i, Kind.HIGH, True)
        elif level.kind in ("PDL", "PWL", "EQL") and down:
            found = SweepEvent(level, i, Kind.LOW, True)
        elif level.kind in ("SWING", "POOL"):
            if up:
                found = SweepEvent(level, i, Kind.HIGH, True)
            elif down:
                found = SweepEvent(level, i, Kind.LOW, True)
    return found


def _nearest_side(df: pd.DataFrame, level: LiquidityLevel) -> Kind:
    """Эвристика: если уровень выше последней цены — это хай-ликвидность, иначе лой."""
    return Kind.HIGH if level.price >= float(df["close"].iloc[-1]) else Kind.LOW


# ---------------------------------------------------------------------------
# 1.5 Слом структуры — BOS / CHOCH
# ---------------------------------------------------------------------------
def market_structure(df: pd.DataFrame, swings: list[Swing]) -> Bias:
    """Текущее направление по последним двум однотипным свингам (HH/HL vs LH/LL)."""
    highs = [s for s in swings if s.kind == Kind.HIGH]
    lows = [s for s in swings if s.kind == Kind.LOW]
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1].price > highs[-2].price
        hl = lows[-1].price > lows[-2].price
        if hh and hl:
            return Bias.BULL
        if not hh and not hl:
            return Bias.BEAR
    # фолбэк: наклон по краям закрытий
    close = df["close"].to_numpy()
    return Bias.BULL if close[-1] >= close[0] else Bias.BEAR


def detect_structure_break(df: pd.DataFrame, swings: list[Swing], trend: Bias,
                           require_body_close: bool = True) -> StructureBreak | None:
    """Слом опорной точки с закрепом ТЕЛОМ (close за уровнем).

    Опорная точка (по плану): в аптренде — лой, от которого поставлен самый высокий хай;
    в даунтренде — хай, от которого поставлен самый глубокий лой.
      BOS   — пробой в сторону тренда (обновление экстремума).
      CHOCH — первый пробой опорной точки ПРОТИВ тренда (смена характера).
    """
    highs = [s for s in swings if s.kind == Kind.HIGH]
    lows = [s for s in swings if s.kind == Kind.LOW]
    close_last = float(df["close"].iloc[-1])
    brk = len(df) - 1
    eps = 1e-9

    if trend == Bias.BULL:
        if not highs or not lows:
            return None
        hh = max(highs, key=lambda s: s.price)
        lows_before = [s for s in lows if s.index < hh.index]
        key = lows_before[-1] if lows_before else min(lows, key=lambda s: s.price)
        if close_last > hh.price + eps:
            return StructureBreak("BOS", hh.price, hh.index, brk, Bias.BULL, True)
        if close_last < key.price - eps:
            return StructureBreak("CHOCH", key.price, key.index, brk, Bias.BEAR, True)
    else:
        if not highs or not lows:
            return None
        ll = min(lows, key=lambda s: s.price)
        highs_before = [s for s in highs if s.index < ll.index]
        key = highs_before[-1] if highs_before else max(highs, key=lambda s: s.price)
        if close_last < ll.price - eps:
            return StructureBreak("BOS", ll.price, ll.index, brk, Bias.BEAR, True)
        if close_last > key.price + eps:
            return StructureBreak("CHOCH", key.price, key.index, brk, Bias.BULL, True)
    return None


# ---------------------------------------------------------------------------
# 1.7 Mitigation / инверсия (валидность зоны)
# ---------------------------------------------------------------------------
def detect_mitigation(df: pd.DataFrame, zone: Zone, since_index: int) -> bool:
    """Mitigation — возврат цены в зону (тест/ребаланс) после её формирования."""
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    for i in range(max(since_index, zone.index_end + 1), len(df)):
        if low[i] <= zone.top and high[i] >= zone.bottom:   # диапазон бара касается зоны
            return True
    return False


def apply_inversion_rule(df: pd.DataFrame, zone: Zone) -> Zone:
    """Правило Инверсии: полное перекрытие зоны ТЕЛОМ свечи -> zone.inverted=True."""
    open_ = df["open"].to_numpy()
    close = df["close"].to_numpy()
    for i in range(zone.index_end + 1, len(df)):
        body_lo = min(open_[i], close[i])
        body_hi = max(open_[i], close[i])
        if body_lo <= zone.bottom and body_hi >= zone.top:
            zone.inverted = True
            break
    return zone


# ---------------------------------------------------------------------------
# Дополнения из отдельных колод (см. заметкам стратегии (SMC), раздел «ДОПОЛНЕНИЕ»)
# ---------------------------------------------------------------------------
def mean_threshold(zone: Zone, level: float = 0.5) -> float:
    """MT (Mean Threshold) = уровень внутри зоны (по умолчанию 50%).

    Лонг: интерес к реакции ВЫШЕ MT; шорт: НИЖЕ MT (OB/BB, шкала 0/0.5/1).
    """
    return zone.bottom + level * (zone.top - zone.bottom)


# Корреляции для SMT (пара -> (коэффициент, тип)). Дословно из колоды SMT by SMC.
CORRELATIONS: dict[tuple[str, str], tuple[float, str]] = {
    ("XAUUSD", "XAGUSD"): (0.69, "direct"),
    ("US500", "US100"): (0.83, "direct"),
    ("EURUSD", "DXY"): (0.92, "inverse"),
    ("BTCUSDT", "ETHUSDT"): (0.77, "direct"),
}


# ---------------------------------------------------------------------------
# 1.6 OB (ордер-блок) и BB (брейкер-блок)
# ---------------------------------------------------------------------------
def detect_order_block(df: pd.DataFrame, swings: list[Swing],
                       structure_break: "StructureBreak | None", trend: Bias,
                       window: int = 10) -> Zone | None:
    """Единая модель OB/BB: движение против тренда, снявшее ликвидность + импульсный выход.

    OB  — рывок внутри тренда (BOS, продолжение); BB — тот же рывок при развороте (CHOCH).
    Зона = свеча-origin рывка, границы ПО ТЕНЯМ (high..low). Вход — на возврате (mitigation),
    стоп за дальнюю границу блока.
    """
    if structure_break is None:
        return None
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    bi = structure_break.break_index
    kind = "BB" if structure_break.kind == "CHOCH" else "OB"
    start = max(0, bi - window)

    if structure_break.direction == Bias.BEAR:
        origin = start + int(high[start:bi + 1].argmax())   # вершина рывка перед сломом вниз
        bias = Bias.BEAR
    else:
        origin = start + int(low[start:bi + 1].argmin())    # низ рывка перед сломом вверх
        bias = Bias.BULL
    return Zone(kind=kind, top=float(high[origin]), bottom=float(low[origin]),
                index_start=origin, index_end=bi, bias=bias)


# ---------------------------------------------------------------------------
# 1.8 SMT (Smart Money Technique) — дивергенция коррелируемых активов
# ---------------------------------------------------------------------------
@dataclass
class SMTSignal:
    side: str           # подсказка направления по отстающему активу: "LONG" | "SHORT"
    correlation: str    # "direct" | "inverse"
    note: str


def _made_new_low(df: pd.DataFrame, k: int) -> bool:
    lows = df["low"].to_numpy()
    return len(lows) > k and lows[-1] < lows[-k - 1:-1].min()


def _made_new_high(df: pd.DataFrame, k: int) -> bool:
    highs = df["high"].to_numpy()
    return len(highs) > k and highs[-1] > highs[-k - 1:-1].max()


def detect_smt(df_a: pd.DataFrame, df_b: pd.DataFrame, correlation: str = "direct",
               lookback: int = 20) -> SMTSignal | None:
    """Расхождение экстремумов: один актив снял ликвидность, второй — нет.

    Прямая корреляция (BTC/ETH): A снял лой, B нет -> лонг B (отставший, против снятия A).
    Обратная (EURUSD/DXY): A(DXY) снял хай, B(EURUSD) не снял лой -> лонг B.
    Возвращает подсказку по ОТСТАЮЩЕМУ активу B или None.
    """
    k = lookback
    a_low, a_high = _made_new_low(df_a, k), _made_new_high(df_a, k)
    b_low, b_high = _made_new_low(df_b, k), _made_new_high(df_b, k)
    if correlation == "direct":
        if a_low and not b_low:
            return SMTSignal("LONG", correlation, "A снял лой, B нет — лонг B")
        if a_high and not b_high:
            return SMTSignal("SHORT", correlation, "A снял хай, B нет — шорт B")
    else:  # inverse
        if a_high and not b_low:
            return SMTSignal("LONG", correlation, "A снял хай, B не снял лой (обр.) — лонг B")
        if a_low and not b_high:
            return SMTSignal("SHORT", correlation, "A снял лой, B не снял хай (обр.) — шорт B")
    return None
