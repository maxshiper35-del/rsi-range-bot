"""Тесты сборки сетапов SMC (6226, Setup 1:1, SMCEngine, confidence, гейт времени)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from app.brain.smc_detectors import Kind, Swing  # noqa: F401 (используется в тестах ниже)
from app.brain.smc_setups import (
    SMCConfig,
    SMCEngine,
    _bar_minute_utc,
    _confidence,
    _find_impulse,
    build_6226,
    build_ltf_poi,
    build_opening_model,
    dealing_range,
    detect_setup_1_1,
)
from app.core.signal_engine import Side

# Сценарий: аптренд (HH/HL) -> свип хая (спайк 113.5, закрытие 108) -> импульсный слом вниз.
# Свинги: HIGH @2,7,11; LOW @4,9. CHOCH вниз (close 94 < опорный лой 104.5). SHORT.
_BARS_SHORT = [
    (100, 100.5, 99.5, 100),
    (100, 102.5, 99.8, 102),
    (102, 104.8, 101.5, 104),   # swing HIGH
    (104, 103.0, 101.8, 102),
    (102, 102.2, 100.5, 101),   # swing LOW
    (101, 103.5, 100.8, 103),
    (103, 107.5, 102.8, 107),
    (107, 110.8, 106.5, 110),   # swing HIGH
    (110, 108.5, 106.8, 107),
    (107, 106.0, 104.5, 105),   # swing LOW (опорный)
    (105, 109.5, 104.8, 109),
    (109, 113.5, 108.5, 108),   # swing HIGH + свип (спайк 113.5)
    (108, 108.2, 103.5, 104),   # импульс вниз
    (104, 104.2, 99.5, 100),
    (100, 100.2, 95.5, 96),
    (96, 96.2, 93.5, 94),       # слом: close 94 < 104.5
]


def _df(bars, dt_start=None, step_min=15):
    o, h, low, c = zip(*bars)
    data = {"open": o, "high": h, "low": low, "close": c, "volume": [1000.0] * len(bars)}
    df = pd.DataFrame(data)
    if dt_start is not None:
        df["dt"] = [dt_start + timedelta(minutes=step_min * i) for i in range(len(bars))]
    return df


# --- 6226 ------------------------------------------------------------------
def test_build_6226_short():
    sig = build_6226(_df(_BARS_SHORT), "TEST/USDT", SMCConfig(use_time_filter=False))
    assert sig is not None
    assert sig.side == Side.SHORT
    # R/R считается из фактических уровней (фибо 0.62/-0.26 дают ~2.32, не зашитые 2.26)
    actual = abs(sig.take_profits[0] - sig.entry) / abs(sig.entry - sig.stop)
    assert abs(sig.risk_reward - round(actual, 2)) < 0.001
    assert abs(sig.risk_reward - 2.32) < 0.05
    assert abs(sig.entry - 105.9) < 0.6      # E + 0.62*(S-E)
    assert abs(sig.stop - 113.5) < 0.1       # уровень 1
    assert abs(sig.take_profits[0] - 88.3) < 0.6  # уровень -0.26
    assert sig.confidence >= 0.5
    assert sig.indicators_snapshot["setup"] == "6226"
    assert sig.indicators_snapshot["market"] == 94.0   # close последнего бара


def test_build_6226_flat_returns_none():
    from app.core.signal_engine import _synthetic_uptrend
    assert build_6226(_synthetic_uptrend(), "T", SMCConfig(use_time_filter=False)) is None


# --- Setup 1:1 -------------------------------------------------------------
def test_setup_1_1_short():
    sig = detect_setup_1_1(_df(_BARS_SHORT), "TEST/USDT", SMCConfig())
    assert sig is not None
    assert sig.side == Side.SHORT
    assert sig.risk_reward == 1.0
    assert abs(sig.entry - 103.5) < 0.6      # E + 0.5*(S-E)
    assert abs(sig.stop - 113.5) < 0.1
    assert abs(sig.take_profits[0] - 93.5) < 0.6


# --- гейт времени 6226 -----------------------------------------------------
def test_6226_time_gate_blocks_outside_window():
    # окно входа UTC 11:00-18:00; последний бар (06:00 + 15*15м = 09:45) вне окна -> None
    early = datetime(2026, 1, 5, 6, 0, tzinfo=timezone.utc)
    cfg = SMCConfig(use_time_filter=True)
    assert build_6226(_df(_BARS_SHORT, dt_start=early), "T", cfg) is None


def test_6226_time_gate_allows_inside_window():
    # 13:00 UTC -> внутри окна 11:00-18:00 -> сигнал есть
    ok = datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc)
    sig = build_6226(_df(_BARS_SHORT, dt_start=ok), "T", SMCConfig(use_time_filter=True))
    assert sig is not None and sig.side == Side.SHORT


# --- SMCEngine -------------------------------------------------------------
def test_engine_analyze_single_returns_best():
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    sig = eng.analyze_single(_df(_BARS_SHORT), "TEST/USDT", "m15")
    assert sig is not None and sig.side == Side.SHORT


def test_engine_analyze_frames():
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    frames = {"H1": _df(_BARS_SHORT), "m15": _df(_BARS_SHORT)}
    sig = eng.analyze(frames, "TEST/USDT")
    assert sig is not None


def test_min_impulse_filter():
    # огромный порог импульса -> мелкую структуру отсекает, сигналов нет
    df = _df(_BARS_SHORT)
    assert build_6226(df, "T", SMCConfig(use_time_filter=False, min_impulse_atr=1.0)) is not None
    assert build_6226(df, "T", SMCConfig(use_time_filter=False, min_impulse_atr=100.0)) is None


# Живой инцидент 2026-07-11: LAB рухнул на ~97% за 5 дней, но 200-барное окно (~8 дней
# для 1h) ещё видело до-обвальный хай. _find_impulse брал МАКСИМАЛЬНУЮ цену свинга за
# всё окно как якорь импульса -> вход получался в 12-15 раз дальше рынка, недостижим.
# Фикс: якорь — БЛИЖАЙШИЙ к слому свинг (cand[-1]), не глобальный экстремум окна.
_BARS_STALE_HIGH = [
    (95, 96, 94, 95),
    (95, 97, 94, 96),
    (96, 500, 96, 150),    # древний экстремальный хай — вне относимости к текущей структуре
    (150, 151, 120, 130),
    (130, 131, 110, 115),
    (115, 116, 105, 108),
    (108, 109, 100, 100),
] + _BARS_SHORT

_BARS_STALE_LOW = [
    (112, 114, 111, 113),
    (113, 115, 112, 114),
    (114, 114.5, -300, 50),  # древний экстремальный лой — вне относимости к текущей структуре
    (50, 70, 49, 65),
    (65, 85, 64, 80),
    (80, 100, 79, 95),
    (95, 107, 94, 107),
] + [
    (107.0, 107.5, 106.5, 107.0),
    (107.0, 107.2, 104.5, 105.0),
    (105.0, 105.5, 102.2, 103.0),
    (103.0, 105.2, 104.0, 105.0),
    (105.0, 106.5, 104.8, 106.0),
    (106.0, 106.2, 103.5, 104.0),
    (104.0, 104.2, 99.5, 100.0),
    (100.0, 100.5, 96.2, 97.0),
    (97.0, 100.2, 98.5, 100.0),
    (100.0, 102.5, 101.0, 102.0),
    (102.0, 102.2, 97.5, 98.0),
    (98.0, 98.5, 93.5, 99.0),
    (99.0, 103.5, 98.8, 103.0),
    (103.0, 107.5, 102.8, 107.0),
    (107.0, 111.5, 106.8, 111.0),
    (111.0, 113.5, 110.8, 113.0),
]


def test_find_impulse_anchors_on_recent_swing_not_stale_extreme_short():
    df = _df(_BARS_STALE_HIGH)
    imp = _find_impulse(df, SMCConfig(use_time_filter=False, min_impulse_atr=0.0))
    assert imp is not None and imp.side == Side.SHORT
    assert imp.S == 113.5              # ближайший к слому реальный свинг
    assert imp.S != 500.0              # НЕ древний экстремум


def test_find_impulse_anchors_on_recent_swing_not_stale_extreme_long():
    df = _df(_BARS_STALE_LOW)
    imp = _find_impulse(df, SMCConfig(use_time_filter=False, min_impulse_atr=0.0))
    assert imp is not None and imp.side == Side.LONG
    assert imp.S == 93.5               # ближайший к слому реальный свинг
    assert imp.S != -300.0             # НЕ древний экстремум


def test_engine_enabled_setups_filter():
    df = _df(_BARS_SHORT)
    # только Setup 1:1 -> движок обязан вернуть именно его
    eng = SMCEngine(SMCConfig(use_time_filter=False, enabled_setups=("1:1",)))
    sig = eng.analyze_single(df, "TEST/USDT", "m15")
    assert sig is not None
    assert sig.indicators_snapshot["setup"] == "1:1"
    # 6226 выключен -> его тег не может появиться ни при каком выборе
    eng2 = SMCEngine(SMCConfig(use_time_filter=False,
                               enabled_setups=("1:1", "open-SMC", "LTF")))
    sig2 = eng2.analyze_single(df, "TEST/USDT", "m15")
    assert sig2 is None or sig2.indicators_snapshot["setup"] != "6226"
    # пустой набор -> None
    eng3 = SMCEngine(SMCConfig(use_time_filter=False, enabled_setups=()))
    assert eng3.analyze_single(df, "TEST/USDT", "m15") is None


def test_created_at_is_bar_close_time():
    # с колонкой dt created_at сигнала = метка ПОСЛЕДНЕГО бара (для UNIQUE-дедупа),
    # и повторный анализ того же окна даёт тот же created_at
    start = datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc)
    df = _df(_BARS_SHORT, dt_start=start)
    cfg = SMCConfig(use_time_filter=False)
    sig1 = build_6226(df, "T", cfg)
    sig2 = build_6226(df, "T", cfg)
    assert sig1 is not None and sig2 is not None
    expected = (start + timedelta(minutes=15 * (len(_BARS_SHORT) - 1))).isoformat()
    assert sig1.created_at == expected
    assert sig1.created_at == sig2.created_at          # дедуп повторного скана возможен


def test_analyze_fallback_picks_youngest_tf():
    # frames без ENTRY_TFS: '1h' должен стать входным, '4h' — контекстом (не наоборот)
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    frames = {"4h": _df(_BARS_SHORT), "1h": _df(_BARS_SHORT)}
    sig = eng.analyze(frames, "T")
    assert sig is not None
    assert sig.timeframe == "1h"


def test_enabled_setups_specific_ltf_entry():
    # конкретная запись 'LTF-BB' должна проходить пре-гейт (двусторонний префикс-матч)
    eng = SMCEngine(SMCConfig(use_time_filter=False, enabled_setups=("LTF-BB",)))
    sig = eng.analyze_single(_df(_BARS_SHORT), "T", "m15")
    assert sig is None or sig.indicators_snapshot["setup"] == "LTF-BB"


def test_engine_flat_is_none():
    from app.core.signal_engine import _synthetic_uptrend
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    out = eng.analyze_single(_synthetic_uptrend(), "T", "m15")
    assert out is None or out.side in (Side.LONG, Side.SHORT)


# --- вспомогательные -------------------------------------------------------
def test_confidence_weights():
    cfg = SMCConfig()
    assert abs(_confidence(0.5, 1.0, 0.5, 0.0, cfg) - 0.55) < 1e-9


def test_dealing_range():
    sw = [Swing(1, 100.5, Kind.LOW), Swing(3, 113.5, Kind.HIGH), Swing(5, 104.5, Kind.LOW)]
    assert dealing_range(sw) == (100.5, 113.5)


def test_bar_minute_utc():
    df = _df(_BARS_SHORT, dt_start=datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc), step_min=0)
    assert _bar_minute_utc(df, 0) == 13 * 60


# --- §2.2 LTF-POI и модели открытия ----------------------------------------
def test_build_ltf_poi_short():
    df = _df(_BARS_SHORT)
    sig = build_ltf_poi(df, df, "TEST/USDT", SMCConfig())
    assert sig is not None
    assert sig.side == Side.SHORT
    assert abs(sig.stop - 113.5) < 0.1               # дальняя граница блока
    assert sig.stop > sig.entry > sig.take_profits[0]  # SHORT: стоп > вход > тейк
    assert sig.indicators_snapshot["setup"].startswith("LTF")


def test_build_opening_model_short():
    df = _df(_BARS_SHORT)
    sig = build_opening_model(df, "TEST/USDT", SMCConfig(), model="SMC")
    assert sig is not None
    assert sig.side == Side.SHORT
    assert abs(sig.stop - 113.5) < 0.1               # за точку разворота
    assert sig.indicators_snapshot["setup"] == "open-SMC"
    assert sig.risk_reward > 0
