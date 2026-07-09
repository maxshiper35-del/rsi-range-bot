"""
signal_engine.py — движок торговых сигналов для фьючерсного бота.

Принцип: чистые функции индикаторов + конфигурируемая стратегия.
ANALYZE отделён от FETCH — движок тестируется на любом DataFrame
(бэктест, paper-trading, live) без привязки к бирже. Это позволяет
прогнать стратегию на истории ПЕРЕД запуском и честно замерить винрейт.

Зависимости: pandas, numpy (обязательно); ccxt (только для live-фетча,
импортируется лениво — модуль работает и без него).

Стратегия v1: тренд по EMA + импульс по RSI + подтверждение объёмом.
Стоп по ATR, тейки по R-множителям (честный risk/reward, не выдуманный).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


# ---------------------------------------------------------------------------
# Индикаторы — чистые функции, каждую можно юнит-тестить отдельно
# ---------------------------------------------------------------------------
class Indicators:
    @staticmethod
    def ema(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    @staticmethod
    def rsi(close: pd.Series, period: int = 14) -> pd.Series:
        # RSI по Уайлдеру (ewm с alpha = 1/period)
        delta = close.diff()
        gain = delta.clip(lower=0.0)
        loss = -delta.clip(upper=0.0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0.0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return rsi.fillna(50.0)

    @staticmethod
    def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        # Average True Range — база для стопа
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift()
        true_range = pd.concat(
            [
                high - low,
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        return true_range.ewm(alpha=1 / period, adjust=False).mean()

    @staticmethod
    def volume_ratio(volume: pd.Series, period: int = 20) -> pd.Series:
        avg = volume.rolling(period).mean()
        return volume / avg


# ---------------------------------------------------------------------------
# Конфиг стратегии — крути параметры здесь, логику не трогай
# ---------------------------------------------------------------------------
@dataclass
class StrategyConfig:
    ema_fast: int = 20
    ema_slow: int = 50
    rsi_period: int = 14
    rsi_long_min: float = 45.0     # для лонга RSI должен восстановиться выше
    rsi_long_max: float = 70.0     # но не в перегреве (иначе поздно)
    rsi_short_max: float = 55.0    # для шорта RSI должен опуститься ниже
    rsi_short_min: float = 30.0    # но не в перепроданности
    atr_period: int = 14
    atr_stop_mult: float = 1.5     # стоп = вход ± 1.5 * ATR
    vol_period: int = 20
    vol_min_ratio: float = 1.2     # объём выше среднего минимум в 1.2 раза
    tp_r_multiples: tuple = (1.0, 2.0, 3.0)  # тейки в R (риск-множителях)
    min_confidence: float = 0.5    # ниже порога сигнал не отдаём


# ---------------------------------------------------------------------------
# Сигнал — то, что уходит в базу и подписчику
# ---------------------------------------------------------------------------
@dataclass
class Signal:
    symbol: str
    side: Side
    timeframe: str
    entry: float
    stop: float
    take_profits: list
    risk_reward: float
    confidence: float
    reasons: list
    created_at: str
    indicators_snapshot: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["side"] = self.side.value
        return d

    def format_message(self) -> str:
        """Готовый текст для отправки в Telegram."""
        arrow = "🟢 LONG" if self.side == Side.LONG else "🔴 SHORT"
        conf_bar = "▰" * round(self.confidence * 10) + "▱" * (10 - round(self.confidence * 10))
        tps = "\n".join(
            f"   • TP{i+1}: {_fmt(tp)}" for i, tp in enumerate(self.take_profits)
        )
        reasons = "\n".join(f"   — {r}" for r in self.reasons)
        market = self.indicators_snapshot.get("market") if isinstance(self.indicators_snapshot, dict) else None
        market_line = f"💹 Цена сейчас:  {_fmt(market)}\n" if market else ""
        return (
            f"{arrow}  |  {self.symbol}  ({self.timeframe})\n"
            f"────────────────────\n"
            f"{market_line}"
            f"📍 Вход:  {_fmt(self.entry)}  (лимит)\n"
            f"🛑 Стоп:  {_fmt(self.stop)}\n"
            f"🎯 Тейки:\n{tps}\n"
            f"⚖️ R/R до TP{len(self.take_profits)}: 1:{self.risk_reward:.1f}\n"
            f"🔎 Уверенность: {conf_bar} {int(self.confidence * 100)}%\n"
            f"────────────────────\n"
            f"Почему:\n{reasons}\n\n"
            f"⚠️ Не финансовый совет. Риск на сделку — 1–2% депозита."
        )


def _fmt(x: float) -> float:
    """Округление под масштаб цены (BTC vs альт-мелочь)."""
    if x >= 1000:
        return round(x, 1)
    if x >= 1:
        return round(x, 3)
    return round(x, 6)


# ---------------------------------------------------------------------------
# Движок
# ---------------------------------------------------------------------------
class SignalEngine:
    def __init__(self, config: Optional[StrategyConfig] = None):
        self.cfg = config or StrategyConfig()

    def _enrich(self, df: pd.DataFrame) -> pd.DataFrame:
        c = self.cfg
        out = df.copy()
        out["ema_fast"] = Indicators.ema(out["close"], c.ema_fast)
        out["ema_slow"] = Indicators.ema(out["close"], c.ema_slow)
        out["rsi"] = Indicators.rsi(out["close"], c.rsi_period)
        out["atr"] = Indicators.atr(out, c.atr_period)
        out["vol_ratio"] = Indicators.volume_ratio(out["volume"], c.vol_period)
        return out

    def analyze(self, df: pd.DataFrame, symbol: str, timeframe: str) -> Optional[Signal]:
        """
        df: OHLCV с колонками open/high/low/close/volume, ТОЛЬКО закрытые бары.
        Возвращает Signal или None (нет сетапа).
        """
        c = self.cfg
        min_bars = max(c.ema_slow, c.rsi_period, c.atr_period, c.vol_period) + 5
        if len(df) < min_bars:
            return None

        data = self._enrich(df)
        last, prev = data.iloc[-1], data.iloc[-2]

        if pd.isna(last["atr"]) or last["atr"] <= 0:
            return None

        long_res = self._eval_long(last, prev)
        short_res = self._eval_short(last, prev)

        # берём сторону с большей уверенностью, если она проходит порог
        best = max([long_res, short_res], key=lambda r: r[1])
        side, confidence, reasons = best
        if side is None or confidence < c.min_confidence:
            return None

        entry = float(last["close"])
        atr = float(last["atr"])
        snapshot = {
            "ema_fast": round(float(last["ema_fast"]), 4),
            "ema_slow": round(float(last["ema_slow"]), 4),
            "rsi": round(float(last["rsi"]), 1),
            "atr": round(atr, 4),
            "vol_ratio": round(float(last["vol_ratio"]), 2),
        }

        if side == Side.LONG:
            stop = entry - c.atr_stop_mult * atr
            r = entry - stop
            tps = [round(entry + m * r, 8) for m in c.tp_r_multiples]
        else:
            stop = entry + c.atr_stop_mult * atr
            r = stop - entry
            tps = [round(entry - m * r, 8) for m in c.tp_r_multiples]

        return Signal(
            symbol=symbol,
            side=side,
            timeframe=timeframe,
            entry=round(entry, 8),
            stop=round(stop, 8),
            take_profits=tps,
            risk_reward=float(max(c.tp_r_multiples)),
            confidence=round(confidence, 2),
            reasons=reasons,
            created_at=datetime.now(timezone.utc).isoformat(),
            indicators_snapshot=snapshot,
        )

    # --- логика сторон -----------------------------------------------------
    def _eval_long(self, last, prev):
        c = self.cfg
        bias_up = last["ema_fast"] > last["ema_slow"]
        fresh_cross = prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]
        rsi_reclaim = prev["rsi"] < c.rsi_long_min <= last["rsi"]
        trigger = fresh_cross or rsi_reclaim

        if not (bias_up and trigger):
            return (None, 0.0, [])

        confidence, reasons = 0.30, []          # bias
        reasons.append("Тренд вверх: EMA20 > EMA50")
        confidence += 0.30                       # trigger
        reasons.append("Триггер: свежий кросс EMA" if fresh_cross
                       else "Триггер: RSI вернулся выше зоны отскока")

        if c.rsi_long_min <= last["rsi"] <= c.rsi_long_max:
            confidence += 0.20
            reasons.append(f"RSI в рабочей зоне ({last['rsi']:.0f})")
        if last["vol_ratio"] >= c.vol_min_ratio:
            confidence += 0.20
            reasons.append(f"Объём подтверждает (×{last['vol_ratio']:.1f})")

        return (Side.LONG, min(confidence, 1.0), reasons)

    def _eval_short(self, last, prev):
        c = self.cfg
        bias_down = last["ema_fast"] < last["ema_slow"]
        fresh_cross = prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]
        rsi_reject = prev["rsi"] > c.rsi_short_max >= last["rsi"]
        trigger = fresh_cross or rsi_reject

        if not (bias_down and trigger):
            return (None, 0.0, [])

        confidence, reasons = 0.30, []
        reasons.append("Тренд вниз: EMA20 < EMA50")
        confidence += 0.30
        reasons.append("Триггер: свежий кросс EMA вниз" if fresh_cross
                       else "Триггер: RSI отбился вниз из зоны")

        if c.rsi_short_min <= last["rsi"] <= c.rsi_short_max:
            confidence += 0.20
            reasons.append(f"RSI в рабочей зоне ({last['rsi']:.0f})")
        if last["vol_ratio"] >= c.vol_min_ratio:
            confidence += 0.20
            reasons.append(f"Объём подтверждает (×{last['vol_ratio']:.1f})")

        return (Side.SHORT, min(confidence, 1.0), reasons)


# ---------------------------------------------------------------------------
# Калькулятор позиции (в проде живёт в risk.py, здесь — чтобы сразу пощупать)
# ---------------------------------------------------------------------------
def calculate_position(deposit: float, risk_pct: float, entry: float,
                       stop: float, max_leverage: int = 20) -> dict:
    """
    Размер позиции от РИСКА, а не от «на всё плечо».
    Если стоп сработает — теряешь ровно risk_pct% депозита.
    """
    risk_amount = deposit * risk_pct / 100.0
    per_unit_risk = abs(entry - stop)
    if per_unit_risk == 0:
        return {"error": "стоп равен входу"}
    qty = risk_amount / per_unit_risk
    notional = qty * entry
    implied_leverage = notional / deposit
    warning = None
    if implied_leverage > max_leverage:
        warning = (f"Требуемое плечо ×{implied_leverage:.1f} выше лимита "
                   f"×{max_leverage}. Уменьши риск% или расширь стоп.")
    return {
        "risk_amount": round(risk_amount, 2),
        "position_qty": round(qty, 6),
        "notional_usd": round(notional, 2),
        "implied_leverage": round(implied_leverage, 2),
        "loss_if_stop": round(risk_amount, 2),
        "warning": warning,
    }


# ---------------------------------------------------------------------------
# Live-фетч (ленивый импорт ccxt). Формат перпетуала Bybit: "BTC/USDT:USDT"
# ---------------------------------------------------------------------------
def fetch_ohlcv(symbol: str = "BTC/USDT:USDT", timeframe: str = "1h",
                limit: int = 200, exchange_id: str = "bybit") -> pd.DataFrame:
    import ccxt  # ставится: pip install ccxt
    ex = getattr(ccxt, exchange_id)({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
    raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    # последний бар может быть незакрыт — отбрасываем для честного анализа
    return df.iloc[:-1].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Демо: пробуем live, при отсутствии сети — синтетика (чтобы всегда работало)
# ---------------------------------------------------------------------------
def _synthetic_uptrend(seed: int = 3) -> pd.DataFrame:
    # Сценарий-демо: флэт -> аптренд -> пуллбэк (RSI < 45) -> отскок-бар вверх.
    # Это классический вход в лонг по тренду (rsi reclaim + объём).
    rng = np.random.default_rng(seed)
    core = np.concatenate([
        np.full(70, 100.0) + rng.normal(0, 0.25, 70),   # флэт: EMA сходятся
        np.linspace(100, 122, 45) + rng.normal(0, 0.30, 45),  # тренд вверх
    ])
    dip = np.linspace(121.5, 112.0, 10)                 # пуллбэк (гасит RSI)
    close = np.concatenate([core, dip, [115.5]])        # отскок на последнем баре
    n = len(close)
    r2 = np.random.default_rng(seed + 1)
    high = close + np.abs(r2.normal(0, 0.30, n))
    low = close - np.abs(r2.normal(0, 0.30, n))
    open_ = close - r2.normal(0, 0.20, n)
    vol = r2.uniform(800, 1200, n)
    vol[-1] *= 1.9                                       # всплеск объёма
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": vol})


if __name__ == "__main__":
    symbol, tf = "BTC/USDT:USDT", "1h"
    try:
        df = fetch_ohlcv(symbol, tf, limit=200)
        print(f"[live] загружено {len(df)} баров {symbol} {tf}")
    except Exception as e:
        print(f"[offline] ccxt/сеть недоступны ({type(e).__name__}) — синтетика")
        df, symbol = _synthetic_uptrend(), "TEST/USDT"

    engine = SignalEngine()
    sig = engine.analyze(df, symbol, tf)

    if sig is None:
        print("Сетапа нет — сигнал не сгенерирован.")
    else:
        print("\n" + sig.format_message())
        print("\n--- расчёт позиции (депозит $1000, риск 2%) ---")
        pos = calculate_position(1000, 2.0, sig.entry, sig.stop)
        for k, v in pos.items():
            print(f"{k:>18}: {v}")
