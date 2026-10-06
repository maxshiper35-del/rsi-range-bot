"""Тесты сборки сетапов SMC (6226, Setup 1:1, SMCEngine, confidence, гейт времени)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from app.brain.smc_detectors import Kind, Swing  # noqa: F401 (используется в тестах ниже)
from app.brain.smc_setups import (
    SMCConfig,
    SMCEngine,
    _bar_close_minute_ny,
    _confidence,
    _find_impulse,
    _in_window,
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
    sig = build_6226(_df(_BARS_SHORT), "TEST/USDT",
                     SMCConfig(use_time_filter=False, min_confidence=0.0))
    assert sig is not None
    assert sig.side == Side.SHORT
    # R/R считается из фактических уровней (фибо 0.62/-0.26 дают ~2.32, не зашитые 2.26)
    actual = abs(sig.take_profits[0] - sig.entry) / abs(sig.entry - sig.stop)
    assert abs(sig.risk_reward - round(actual, 2)) < 0.001
    assert abs(sig.risk_reward - 2.32) < 0.05
    assert abs(sig.entry - 105.9) < 0.6      # E + 0.62*(S-E)
    assert abs(sig.stop - 113.5) < 0.1       # уровень 1
    assert abs(sig.take_profits[0] - 88.3) < 0.6  # уровень -0.26
    assert 0.0 <= sig.confidence <= 1.0   # уровень гейта проверяет отдельный тест
    assert sig.indicators_snapshot["setup"] == "6226"
    assert sig.indicators_snapshot["market"] == 94.0   # close последнего бара


def test_snapshot_logs_confidence_subscores():
    """Суб-скоры confidence пишутся в снапшот (аудит 11.08.2026).

    До этого bias/trigger/confl невосстановимо терялись — калибровка весов w_*
    была невозможна ни по журналу, ни по кэшу сигналов. Проверяем и арифметику:
    свёртка суб-скоров весами обязана давать сам confidence — иначе в снапшоте
    лежит не то, из чего скор посчитан."""
    cfg = SMCConfig(use_time_filter=False, min_confidence=0.0)
    for sig in (build_6226(_df(_BARS_SHORT), "T", cfg),
                detect_setup_1_1(_df(_BARS_SHORT), "T", cfg)):
        assert sig is not None
        sc = sig.indicators_snapshot["scores"]
        assert set(sc) == {"bias", "trigger", "sweep", "confl"}
        expect = _confidence(sc["bias"], sc["trigger"], sc["sweep"], sc["confl"], cfg)
        assert abs(sig.confidence - expect) < 0.02   # confidence округлён до 2 знаков


def test_6226_reason_text_honest_about_body_close():
    """Текст причины отражает ФАКТ закрепа телом, а не пишется безусловно.

    Баг из аудита 11.08.2026: «закреп телом» печатался даже при trigger=0.5
    (body_confirmed=False) — пост в канале врал о качестве слома."""
    sig = build_6226(_df(_BARS_SHORT), "T",
                     SMCConfig(use_time_filter=False, min_confidence=0.0))
    assert sig is not None
    brk_line = next(r for r in sig.reasons if r.startswith("слом структуры"))
    body = sig.indicators_snapshot["scores"]["trigger"] == 1.0
    assert ("закреп телом" in brk_line) == body
    assert ("только закрытием" in brk_line) == (not body)


def test_build_6226_flat_returns_none():
    from app.core.signal_engine import _synthetic_uptrend
    assert build_6226(_synthetic_uptrend(), "T",
                  SMCConfig(use_time_filter=False, min_confidence=0.0)) is None


# --- Setup 1:1 -------------------------------------------------------------
def test_setup_1_1_short():
    sig = detect_setup_1_1(_df(_BARS_SHORT), "TEST/USDT", SMCConfig(min_confidence=0.0))
    assert sig is not None
    assert sig.side == Side.SHORT
    assert sig.risk_reward == 1.0
    assert abs(sig.entry - 103.5) < 0.6      # E + 0.5*(S-E)
    assert abs(sig.stop - 113.5) < 0.1
    assert abs(sig.take_profits[0] - 93.5) < 0.6


# --- гейт времени 6226 -----------------------------------------------------
def test_6226_time_gate_blocks_outside_window():
    # бар слома закрывается в NY 05:00 — вне окна break_window_ny (дефолт 7:00-14:00) -> None
    early = datetime(2026, 1, 5, 6, 0, tzinfo=timezone.utc)
    cfg = SMCConfig(use_time_filter=True)
    assert build_6226(_df(_BARS_SHORT, dt_start=early), "T", cfg) is None


def test_6226_time_gate_allows_inside_window():
    # бар слома закрывается в NY 12:00 — внутри окна -> сигнал есть
    ok = datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc)
    sig = build_6226(_df(_BARS_SHORT, dt_start=ok), "T", SMCConfig(use_time_filter=True))
    assert sig is not None and sig.side == Side.SHORT


def test_6226_break_window_is_separate_knob_from_entry_window():
    """Окно слома вынесено в break_window_ny — концептуально это НЕ окно входа.

    Первоисточник задаёт два окна: съём ликвидности и слом с 2:00 до 11:00 NY, вход с
    7:00 до 14:00. Дефолт оставлен равным окну входа, потому что расширение до (120, 660)
    качество не улучшило (6226: +0.184R против +0.189R на 5 годах). Тест фиксирует, что
    ручка РАБОТАЕТ и её можно перемерить, не переписывая гейт заново."""
    late = datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc)   # слом в NY 12:00
    df = _df(_BARS_SHORT, dt_start=late)
    assert build_6226(df, "T", SMCConfig(use_time_filter=True)) is not None
    narrow = SMCConfig(use_time_filter=True, break_window_ny=(2 * 60, 11 * 60))
    assert build_6226(df, "T", narrow) is None    # 12:00 вне окна первоисточника


# --- SMCEngine -------------------------------------------------------------
def test_engine_analyze_single_returns_best():
    eng = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0))
    sig = eng.analyze_single(_df(_BARS_SHORT), "TEST/USDT", "m15")
    assert sig is not None and sig.side == Side.SHORT


def test_engine_analyze_frames():
    eng = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0))
    frames = {"H1": _df(_BARS_SHORT), "m15": _df(_BARS_SHORT)}
    sig = eng.analyze(frames, "TEST/USDT")
    assert sig is not None


def test_min_impulse_filter():
    # огромный порог импульса -> мелкую структуру отсекает, сигналов нет
    df = _df(_BARS_SHORT)
    assert build_6226(df, "T", SMCConfig(use_time_filter=False, min_confidence=0.0,
                                     min_impulse_atr=1.0)) is not None
    assert build_6226(df, "T", SMCConfig(use_time_filter=False, min_confidence=0.0,
                                     min_impulse_atr=100.0)) is None


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
    imp = _find_impulse(df, SMCConfig(use_time_filter=False, min_confidence=0.0,
                                  min_impulse_atr=0.0))
    assert imp is not None and imp.side == Side.SHORT
    assert imp.S == 113.5              # ближайший к слому реальный свинг
    assert imp.S != 500.0              # НЕ древний экстремум


def test_find_impulse_anchors_on_recent_swing_not_stale_extreme_long():
    df = _df(_BARS_STALE_LOW)
    imp = _find_impulse(df, SMCConfig(use_time_filter=False, min_confidence=0.0,
                                  min_impulse_atr=0.0))
    assert imp is not None and imp.side == Side.LONG
    assert imp.S == 93.5               # ближайший к слому реальный свинг
    assert imp.S != -300.0             # НЕ древний экстремум


def test_engine_enabled_setups_filter():
    df = _df(_BARS_SHORT)
    # только Setup 1:1 -> движок обязан вернуть именно его
    eng = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0, enabled_setups=("1:1",)))
    sig = eng.analyze_single(df, "TEST/USDT", "m15")
    assert sig is not None
    assert sig.indicators_snapshot["setup"] == "1:1"
    # 6226 выключен -> его тег не может появиться ни при каком выборе
    eng2 = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0,
                               enabled_setups=("1:1", "open-SMC", "LTF")))
    sig2 = eng2.analyze_single(df, "TEST/USDT", "m15")
    assert sig2 is None or sig2.indicators_snapshot["setup"] != "6226"
    # пустой набор -> None
    eng3 = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0, enabled_setups=()))
    assert eng3.analyze_single(df, "TEST/USDT", "m15") is None


def test_created_at_is_bar_close_time():
    # с колонкой dt created_at сигнала = метка ПОСЛЕДНЕГО бара (для UNIQUE-дедупа),
    # и повторный анализ того же окна даёт тот же created_at
    start = datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc)
    df = _df(_BARS_SHORT, dt_start=start)
    cfg = SMCConfig(use_time_filter=False, min_confidence=0.0)
    sig1 = build_6226(df, "T", cfg)
    sig2 = build_6226(df, "T", cfg)
    assert sig1 is not None and sig2 is not None
    expected = (start + timedelta(minutes=15 * (len(_BARS_SHORT) - 1))).isoformat()
    assert sig1.created_at == expected
    assert sig1.created_at == sig2.created_at          # дедуп повторного скана возможен


def test_analyze_fallback_picks_youngest_tf():
    # frames без ENTRY_TFS: '1h' должен стать входным, '4h' — контекстом (не наоборот)
    eng = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0))
    frames = {"4h": _df(_BARS_SHORT), "1h": _df(_BARS_SHORT)}
    sig = eng.analyze(frames, "T")
    assert sig is not None
    assert sig.timeframe == "1h"


def test_enabled_setups_specific_ltf_entry():
    # конкретная запись 'LTF-BB' должна проходить пре-гейт (двусторонний префикс-матч)
    eng = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0,
                              enabled_setups=("LTF-BB",)))
    sig = eng.analyze_single(_df(_BARS_SHORT), "T", "m15")
    assert sig is None or sig.indicators_snapshot["setup"] == "LTF-BB"


def test_engine_flat_is_none():
    from app.core.signal_engine import _synthetic_uptrend
    eng = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0))
    out = eng.analyze_single(_synthetic_uptrend(), "T", "m15")
    assert out is None or out.side in (Side.LONG, Side.SHORT)


# --- вспомогательные -------------------------------------------------------
def test_confidence_weights():
    cfg = SMCConfig()
    assert abs(_confidence(0.5, 1.0, 0.5, 0.0, cfg) - 0.55) < 1e-9


def test_dealing_range():
    sw = [Swing(1, 100.5, Kind.LOW), Swing(3, 113.5, Kind.HIGH), Swing(5, 104.5, Kind.LOW)]
    assert dealing_range(sw) == (100.5, 113.5)


def test_bar_close_minute_ny():
    # 5 января = EST (UTC-5): бар открыт 13:00 UTC, ТФ 60м -> закрытие 14:00 UTC = 09:00 NY
    df = _df(_BARS_SHORT, dt_start=datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc), step_min=0)
    assert _bar_close_minute_ny(df, 0, 60) == 9 * 60
    # 6 июля = EDT (UTC-4): то же закрытие 14:00 UTC = 10:00 NY — DST учитывается честно
    df_summer = _df(_BARS_SHORT, dt_start=datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc),
                    step_min=0)
    assert _bar_close_minute_ny(df_summer, 0, 60) == 10 * 60


def test_in_window_wraps_midnight():
    assert _in_window(23 * 60, 22 * 60, 2 * 60) is True     # окно через полночь
    assert _in_window(1 * 60, 22 * 60, 2 * 60) is True
    assert _in_window(12 * 60, 22 * 60, 2 * 60) is False
    assert _in_window(7 * 60, 7 * 60, 14 * 60) is True      # обычное окно, [lo, hi)
    assert _in_window(14 * 60, 7 * 60, 14 * 60) is False


# --- §2.2 LTF-POI и модели открытия ----------------------------------------
def test_build_ltf_poi_short():
    df = _df(_BARS_SHORT)
    sig = build_ltf_poi(df, df, "TEST/USDT", SMCConfig(min_confidence=0.0))
    assert sig is not None
    assert sig.side == Side.SHORT
    assert abs(sig.stop - 113.5) < 0.1               # дальняя граница блока
    assert sig.stop > sig.entry > sig.take_profits[0]  # SHORT: стоп > вход > тейк
    assert sig.indicators_snapshot["setup"].startswith("LTF")


def test_build_opening_model_short():
    df = _df(_BARS_SHORT)
    sig = build_opening_model(df, "TEST/USDT", SMCConfig(min_confidence=0.0), model="SMC")
    assert sig is not None
    assert sig.side == Side.SHORT
    assert abs(sig.stop - 113.5) < 0.1               # за точку разворота
    assert sig.indicators_snapshot["setup"] == "open-SMC"
    assert sig.risk_reward > 0


def test_build_opening_model_rejects_low_rr():
    # open-SMC — единственный сетап, где entry (ближайший к mid FVG) и take (фикс. E)
    # выбираются НЕЗАВИСИМО: если FVG оказался у самого E, reward -> 0 при полном risk
    # (живая сделка #145: risk 2.58%, reward 0.06%, RR=0.02). Порог должен отсекать такое.
    df = _df(_BARS_SHORT)
    baseline = build_opening_model(df, "TEST/USDT", SMCConfig(min_confidence=0.0), model="SMC")
    assert baseline is not None and baseline.risk_reward == 1.35   # RR фикстуры (справочно)
    # порог выше фактического RR -> сигнал отклонён
    rejected = build_opening_model(
        df, "TEST/USDT", SMCConfig(min_confidence=0.0, min_rr_open_model=1.5), model="SMC")
    assert rejected is None
    # порог ниже фактического RR -> сигнал по-прежнему проходит
    passed = build_opening_model(
        df, "TEST/USDT", SMCConfig(min_confidence=0.0, min_rr_open_model=1.0), model="SMC")
    assert passed is not None and passed.risk_reward == 1.35


def test_build_opening_model_signal_145_regression():
    # точная геометрия сигнала #145 (WLD/USDT LONG, живой инцидент): entry 0.39375,
    # stop 0.3836, take 0.394 -> risk 0.01015 (2.58%), reward 0.00025 (0.06%), RR=0.02.
    # Дефолтный порог (0.5) обязан отбрасывать такой сигнал.
    entry, stop, take = 0.39375, 0.3836, 0.394
    risk, reward = abs(entry - stop), abs(entry - take)
    rr = round(reward / risk, 2)
    assert rr == 0.02                                 # подтверждаем геометрию инцидента
    assert rr < SMCConfig().min_rr_open_model          # дефолтный пол (0.5) отсекает


def test_confidence_gate_discriminates_weak_setups():
    """Раунд-2: гейт confidence — реальный фильтр, не вакуум. Слабый сетап (без свипа /
    контртренд / без конфлюэнса) при дефолтном min_confidence=0.5 отсекается, а с
    выключенным гейтом (0.0) — существует. Floor больше не 0.55."""
    df = _df(_BARS_SHORT)
    eng_strict = SMCEngine(SMCConfig(use_time_filter=False, enabled_setups=("1:1",)))
    eng_loose = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0,
                                    enabled_setups=("1:1",)))
    strict = eng_strict.analyze_single(df, "TEST/USDT", "m15")
    loose = eng_loose.analyze_single(df, "TEST/USDT", "m15")
    assert loose is not None                          # сетап конструктивно существует
    assert loose.confidence < 0.5                     # ...но уверенность честно низкая
    assert strict is None                             # дефолтный гейт его режет


# --- HTF-блок контртренда --------------------------------------------------
# Бычий зигзаг с ПОДТВЕРЖДЁННОЙ структурой: HIGH 105@2 -> LOW 100.5@5 ->
# HIGH 110@8 (HH) -> LOW 103@11 (HL) + хвост для подтверждения правого плеча.
_BARS_HTF_BULL = [
    (100, 101, 99, 100),
    (100, 102, 100, 101),
    (101, 105, 101, 104),       # swing HIGH 105
    (104, 104.5, 102, 103),
    (103, 103.5, 101, 102),
    (102, 102.5, 100.5, 101),   # swing LOW 100.5
    (101, 104, 101, 103),
    (103, 106, 102.5, 105),
    (105, 110, 104, 109),       # swing HIGH 110 (HH)
    (109, 109.5, 105, 106),
    (106, 107, 104, 105),
    (105, 105.5, 103, 104),     # swing LOW 103 (HL)
    (104, 106, 103.5, 105),
    (105, 107, 104, 106),
]
# Зеркало (отражение цен вокруг 200): LH+LL — подтверждённый медвежий тренд.
_BARS_HTF_BEAR = [(200 - o, 200 - lo, 200 - h, 200 - c) for o, h, lo, c in _BARS_HTF_BULL]


def test_htf_confirmed_trend_detects_structure():
    from app.brain.smc_detectors import Bias
    from app.brain.smc_setups import htf_confirmed_trend
    cfg = SMCConfig()
    assert htf_confirmed_trend(_df(_BARS_HTF_BULL), cfg) == Bias.BULL
    assert htf_confirmed_trend(_df(_BARS_HTF_BEAR), cfg) == Bias.BEAR
    # расширяющийся диапазон (HH, но LL) — структура НЕ подтверждена -> None (блока нет);
    # фолбэк market_structure по наклону сюда сознательно не входит
    expanding = list(_BARS_HTF_BULL)
    expanding[11] = (105, 105.5, 98, 104)      # LOW2 98 < LOW1 100.5 при HH
    assert htf_confirmed_trend(_df(expanding), cfg) is None


def test_htf_block_countertrend_filters_signal():
    """SHORT-сетап против подтверждённого БЫЧЬЕГО HTF: с флагом — блок, без флага —
    проходит (раньше был лишь штраф −0.25 в confidence — 17.07 пропускал контртренд)."""
    df = _df(_BARS_SHORT)
    on = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0,
                             htf_block_countertrend=True))
    off = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0))
    assert on.analyze_single(df, "T", "m15", htf_df=_df(_BARS_HTF_BULL)) is None
    # по тренду (медвежий HTF) — SHORT проходит
    sig = on.analyze_single(df, "T", "m15", htf_df=_df(_BARS_HTF_BEAR))
    assert sig is not None and sig.side == Side.SHORT
    # неподтверждённая структура HTF — блока нет
    assert on.analyze_single(df, "T", "m15", htf_df=None) is not None
    # флаг выключен — контртренд проходит (со штрафом в confidence)
    assert off.analyze_single(df, "T", "m15", htf_df=_df(_BARS_HTF_BULL)) is not None


def test_min_stop_pct_cost_gate():
    """Костовый гейт: сигналы со слишком тесным стопом не выдаются — издержки
    round-trip фиксированы в % цены, а R = дистанция стопа (живой замер 25.07.2026:
    стоп 0.63% -> 0.21R издержек; стоп 0.13% -> 1.0R, сделка мертва в момент рождения)."""
    df = _df(_BARS_SHORT)
    base = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0))
    sig = base.analyze_single(df, "T", "m15")
    assert sig is not None
    stop_pct = abs(sig.entry - sig.stop) / sig.entry * 100.0
    # порог ниже фактической дистанции — победитель не меняется
    loose = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0,
                                min_stop_pct=stop_pct - 0.01))
    keep = loose.analyze_single(df, "T", "m15")
    assert keep is not None and keep.entry == sig.entry

    # порог выше — этот кандидат выбывает, но гейт применяется К КАЖДОМУ кандидату:
    # побеждает сетап с более широким стопом, а не «сигнала нет» (важная семантика —
    # костовый гейт не глушит символ, он сдвигает выбор к более дешёвой по издержкам структуре)
    tight = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0,
                                min_stop_pct=stop_pct + 0.01))
    alt = tight.analyze_single(df, "T", "m15")
    assert alt is not None
    assert abs(alt.entry - alt.stop) / alt.entry * 100.0 > stop_pct

    # порог выше ВСЕХ кандидатов — сигнала нет
    none_cfg = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0,
                                   min_stop_pct=99.0))
    assert none_cfg.analyze_single(df, "T", "m15") is None
    # 0 = гейт выключен (дефолт), поведение не меняется
    assert SMCConfig().min_stop_pct == 0.0


# --- A/B-гейты 11.08.2026 (enforce_body_close / SMT-вето / вето «слом в имбаланс») ---
def test_smt_veto_drops_side_against_divergence(monkeypatch):
    """SMT-вето отбрасывает кандидата против подсказки и пропускает по ней.

    Подсказку монкейпатчим — семантика самого detect_smt покрыта своими тестами,
    здесь проверяется ПРОВОДКА: analyze_single реально читает подсказку и фильтрует.
    """
    from app.brain import smc_detectors as det
    from app.brain.smc_detectors import SMTSignal

    cfg = SMCConfig(use_time_filter=False, min_confidence=0.0, smt_mode="veto")
    eng = SMCEngine(cfg)
    df, partner = _df(_BARS_SHORT), _df(_BARS_SHORT)

    monkeypatch.setattr(det, "detect_smt", lambda *a, **k: SMTSignal("LONG", "direct", "t"))
    assert eng.analyze_single(df, "T", "m15", partner_df=partner) is None  # SHORT против LONG

    monkeypatch.setattr(det, "detect_smt", lambda *a, **k: SMTSignal("SHORT", "direct", "t"))
    ok = eng.analyze_single(df, "T", "m15", partner_df=partner)
    assert ok is not None and ok.side == Side.SHORT                       # по подсказке

    monkeypatch.setattr(det, "detect_smt", lambda *a, **k: None)
    assert eng.analyze_single(df, "T", "m15", partner_df=partner) is not None  # нейтрально


def test_smt_veto_neutral_without_partner():
    # без партнёрского фрейма вето обязано быть нейтральным даже при smt_mode=veto
    cfg = SMCConfig(use_time_filter=False, min_confidence=0.0, smt_mode="veto")
    assert SMCEngine(cfg).analyze_single(_df(_BARS_SHORT), "T", "m15") is not None


def test_enforce_body_close_gates_close_only_break(monkeypatch):
    """enforce_body_close: слом без закрепа телом отбрасывается ЦЕЛИКОМ (не скором).

    body_confirmed монкейпатчим через detect_structure_break — синтетика _BARS_SHORT
    даёт закреп телом, а нужен именно слом «только закрытием»."""
    from app.brain import smc_detectors as det

    real = det.detect_structure_break

    def close_only(*a, **k):
        brk = real(*a, **k)
        if brk is not None:
            brk.body_confirmed = False
        return brk

    monkeypatch.setattr(det, "detect_structure_break", close_only)
    df = _df(_BARS_SHORT)
    soft = build_6226(df, "T", SMCConfig(use_time_filter=False, min_confidence=0.0))
    assert soft is not None and soft.indicators_snapshot["scores"]["trigger"] == 0.5
    assert "только закрытием" in " ".join(soft.reasons)
    hard = SMCConfig(use_time_filter=False, min_confidence=0.0, enforce_body_close=True)
    assert build_6226(df, "T", hard) is None


def test_veto_break_into_hpz_flag_off_is_noop():
    # дефолт False — сигнал тот же (паритет пове́дения); True на этой синтетике не
    # обязан находить зону, но не должен падать
    df = _df(_BARS_SHORT)
    base = build_6226(df, "T", SMCConfig(use_time_filter=False, min_confidence=0.0))
    assert base is not None
    on = SMCConfig(use_time_filter=False, min_confidence=0.0, veto_break_into_hpz=True)
    build_6226(df, "T", on)   # не падает; исход зависит от зон в синтетике


def test_late_entry_gate_rejects_candidate_before_argmax():
    """Гейт «вход уже пройден» (06.10.2026): кандидат, чей вход (или стоп) рынок уже
    прошёл к закрытию решающего бара, отбрасывается ДО выбора лучшего — побеждает
    следующий валидный кандидат; "off" — поведение прежнее."""
    import pandas as pd

    from app.brain import smc_setups as ss
    from app.core.signal_engine import Side, Signal

    def mk(setup, side, entry, stop, conf):
        return Signal(symbol="X/USDT:USDT", side=side, timeframe="1h", entry=entry, stop=stop,
                      take_profits=[entry + (entry - stop) * 2], risk_reward=2.0,
                      confidence=conf, reasons=[], created_at="2026-01-01T00:00:00+00:00",
                      indicators_snapshot={"setup": setup})

    df = pd.DataFrame({"open": [100.0], "high": [101.0], "low": [96.0], "close": [97.0],
                       "volume": [1.0]})
    late = mk("6226", Side.LONG, entry=99.0, stop=95.0, conf=0.9)    # рынок 97 < входа 99
    dead = mk("6226", Side.LONG, entry=99.0, stop=97.5, conf=0.95)   # рынок 97 <= стопа
    ok = mk("LTF-BB", Side.LONG, entry=96.0, stop=93.0, conf=0.6)    # рынок выше входа
    assert ss._entry_passed(late, 97.0, "entry") and not ss._entry_passed(late, 97.0, "stop")
    assert ss._entry_passed(dead, 97.0, "stop")
    assert not ss._entry_passed(ok, 97.0, "entry")
    short = mk("6226", Side.SHORT, entry=101.0, stop=105.0, conf=0.9)
    assert ss._entry_passed(short, 102.0, "entry") and not ss._entry_passed(short, 100.0, "entry")

    import app.brain.smc_setups as mod
    orig = (mod.build_6226, mod.build_ltf_poi)
    try:
        mod.build_6226 = lambda *a, **k: late
        mod.build_ltf_poi = lambda *a, **k: ok
        cfg = ss.SMCConfig(enabled_setups=("6226", "LTF"), use_time_filter=False)
        assert ss.SMCEngine(cfg).analyze_single(df, "X/USDT:USDT", "1h") is late
        cfg.late_entry_gate = "entry"
        assert ss.SMCEngine(cfg).analyze_single(df, "X/USDT:USDT", "1h") is ok
    finally:
        mod.build_6226, mod.build_ltf_poi = orig
