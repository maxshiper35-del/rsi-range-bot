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
    # РАСХОЖДЕНИЕ С ПЕРВОИСТОЧНИКОМ, оставлено сознательно (29.07.2026). Курс определяет
    # свинг как ТРИ свечи («посередине — выше двух по бокам»), то есть 1/1. Механически
    # 1/1 даёт свинг каждые ~2 бара (64 против 28 на 126 барах синтетики) и выдаёт сигнал
    # даже на чистом тренде, где 2/2 молчит: курс полагается на то, что человек выберет
    # «самый явный съём ликвидности», а у движка этой дискреции нет. 2/2 — механическая
    # замена этому отбору. Значение проверяется A/B-прогоном на 5 годах; менять только
    # по результату замера, не по тексту колоды.
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

    # время: окно из слайдов задано ПО НЬЮ-ЙОРКУ — храним в NY-минутах и конвертируем
    # закрытие бара через zoneinfo (DST!): фикс UTC-4 зимой сдвигал окно на час, а минута
    # ОТКРЫТИЯ бара пропускала решения на ТФ позже заявленного (аудит-2 стратегии [5])
    use_time_filter: bool = True
    entry_window_ny: tuple = (7 * 60, 14 * 60)      # NY 07:00-14:00, полуинтервал [lo, hi)
    after_break_ny: int = 7 * 60                    # слом не раньше NY 07:00
    # Окно СЪЁМА ЛИКВИДНОСТИ И СЛОМА — концептуально отдельное от окна входа: первоисточник
    # (итоговый слайд 6226) задаёт «съем самой очевидной ликвидности с 2.00 до 11.00» и вход
    # «только с 7.00 до 14.00». Разделение введено 29.07.2026, но ДЕФОЛТ оставлен равным окну
    # входа: замер на 5 годах показал, что расширение до (120, 660) качество не улучшает —
    # 6226 даёт +0.184R против +0.189R у статус-кво при большем числе сигналов.
    break_window_ny: tuple = (7 * 60, 14 * 60)
    # OB/BB одной свечой-экстремумом, НЕ всем движением. Расхождение с первоисточником
    # («Ордер Блок — это ВСЁ ДВИЖЕНИЕ») сохранено по результату замера 29.07.2026 на 5 годах:
    #   одна свеча (статус-кво)  LTF-BB 2883 сд. +0.213R | LTF-OB 271 сд. +0.524R | PF 1.37
    #   всё движение (по курсу)  LTF-BB 4835 сд. +0.135R | LTF-OB 358 сд. +0.349R | PF 1.26
    # Узкая зона даёт более тесный стоп и работает фильтром отбора; широкая пропускает в 1.7
    # раза больше сделок втрое худшего качества и поднимает просадку с 26.1R до 33.1R.
    # Менять только по новому замеру (флаг detect_order_block(full_move=...) сохранён).
    ob_full_move: bool = False

    # веса SMC-confidence (сумма = 1.0)
    w_bias: float = 0.30
    w_trigger: float = 0.30
    w_sweep: float = 0.20
    w_confluence: float = 0.20
    # min_confidence наследуется из StrategyConfig (0.5)

    # какие сетапы включены; "LTF" покрывает LTF-BB и LTF-OB (матч по префиксу)
    enabled_setups: tuple = ("6226", "1:1", "open-SMC", "LTF")

    # минимальная дистанция стопа в % от цены входа — КОСТОВЫЙ гейт, не структурный.
    # Издержки round-trip (комиссия + проскальзывание) фиксированы в % ЦЕНЫ, а риск R
    # равен дистанции стопа: чем теснее стоп, тем большую долю R съедает вход-выход.
    # Живой замер 25.07.2026: медианный стоп 0.63% -> 0.21R издержек на сделку при
    # измеренном матожидании того же порядка; сигналы со стопом 0.13% стоили целый 1R
    # и были убыточны в момент рождения. 0 = гейт выключен.
    min_stop_pct: float = 0.0
    # гейт «вход уже пройден» (замер 06.10.2026): close решающего бара уже за стопом
    # ("stop") или за уровнем входа ("entry") — сетап сыгран до публикации; "off" — нет
    late_entry_gate: str = "off"

    # --- A/B-кандидаты 11.08.2026: ВСЕ ТРИ ОТКЛОНЕНЫ ЗАМЕРОМ, дефолты «выключено» ---
    # Гейт замера: 5-летний портфельный прогон в паритете проду (9 символов, 1h+4h,
    # 6226+LTF, min_stop 1.3, трейлинг 1.5/0.2, мейкер-вход, фандинг), train 4г / OOS 1г.
    # Эталон: train exp +0.187R Σ +773.7R | OOS exp +0.227R Σ +274.6R DD 21.8R.
    # Флаги оставлены рабочими для будущих перемеров — менять дефолт только по НОВОМУ замеру.
    #
    # Оживший require_body_close: старый параметр detect_structure_break объявлен, но
    # МЁРТВ (в теле не читается — аудит 11.08.2026). Вместо правки контракта детектора —
    # гейт на уровне импульса: слом только закрытием (body_confirmed=False) отбрасывается.
    # ЗАМЕР: train +0.185R (−0.002) | OOS +0.211R Σ +248.5R (−26R) — хуже на ОБЕИХ
    # половинах. Мягкий учёт в confidence (trigger 0.5) бьёт букву курса, как и в
    # случаях свинга 1/1 и full-move OB.
    enforce_body_close: bool = False
    # SMT-вето (пак CHS, колода SMT by SMC): при активной дивергенции коррелированной
    # пары (партнёр снял экстремум, свой актив нет -> подсказка стороны отстающему)
    # кандидат ПРОТИВ подсказки отбрасывается. Нет дивергенции или партнёра — пас.
    # ЗАМЕР (пара BTC<->ETH, по курсу): train +0.188R | OOS +0.226R — ноль-эффект, вся
    # разница в пределах ±0.001R; train-DD 24.1R против 32.2R не считается (одна половина).
    # ЗАМЕР (широко, альты против BTC): train +0.183R | OOS +0.216R Σ +253.3R,
    # DD OOS 29.1R против 21.8R — ХУЖЕ везде; вне реально коррелированной пары
    # дивергенция — шум. Единственный кандидат с новой информацией (второй актив) —
    # и тоже не дал эджа.
    smt_mode: str = "off"               # "off" | "veto"
    smt_lookback: int = 20
    # Вето «слом в имбаланс» (правило отмены из пака: «против HPZ не попрёшь»):
    # закрытие решающего бара слома ВНУТРИ противоположной неинверсированной HPZ-зоны
    # (FVG/GAP/BPR, сформированной до слома) — сетап не берётся.
    # ЗАМЕР: train +0.186R (ноль) | OOS +0.219R Σ +241.6R DD 27.3R — на train пусто,
    # на OOS хуже по exp, Σ и DD; режет ~10% потока без компенсации качеством.
    veto_break_into_hpz: bool = False
    hpz_kinds: tuple = ("FVG", "GAP", "BPR")

    # жёсткий блок контртренда от ПОДТВЕРЖДЁННОЙ структуры HTF (две пары свингов
    # HH+HL / LH+LL, фолбэк по наклону не считается). Урок 17.07: штраф −0.25 в
    # confidence пропускал лонги в сползании (8 стопов LONG за 6ч); 12-мес бэктест —
    # весь эдж в SHORT, LONG ≈ 0 или минус на всех трёх символах.
    htf_block_countertrend: bool = False


# ---------------------------------------------------------------------------
# Время бара
# ---------------------------------------------------------------------------
def _bar_close_minute_ny(df: pd.DataFrame, i: int, tf_minutes: int) -> int | None:
    """Минута дня America/New_York на ЗАКРЫТИИ бара i (метка dt = ОТКРЫТИЕ, ccxt).

    Решение существует с закрытия бара — фильтровать надо его момент, не открытие;
    zoneinfo даёт честный DST (зимой NY = UTC-5, фикс «UTC-4» сдвигал окно на час)."""
    if "dt" not in df.columns:
        return None
    from zoneinfo import ZoneInfo
    ts = pd.to_datetime(df["dt"].iloc[i], utc=True) + pd.Timedelta(minutes=tf_minutes)
    loc = ts.tz_convert(ZoneInfo("America/New_York"))
    return int(loc.hour) * 60 + int(loc.minute)


def _in_window(m: int, lo: int, hi: int) -> bool:
    """Минута дня в полуинтервале [lo, hi); lo > hi — окно через полночь."""
    return lo <= m < hi if lo <= hi else (m >= lo or m < hi)


# ---------------------------------------------------------------------------
# Bias: тренд HTF + премиум/дискаунт (§2.4, компонент 0.30)
# ---------------------------------------------------------------------------
def dealing_range(swings: list[det.Swing]) -> tuple[float, float] | None:
    highs = [s.price for s in swings if s.kind == Kind.HIGH]
    lows = [s.price for s in swings if s.kind == Kind.LOW]
    if not highs or not lows:
        return None
    return min(lows), max(highs)


def htf_confirmed_trend(htf_df: pd.DataFrame, cfg: SMCConfig) -> Bias | None:
    """Подтверждённый СТРУКТУРНЫЙ тренд HTF: две пары однотипных свингов (HH+HL -> BULL,
    LH+LL -> BEAR), иначе None. Фолбэк market_structure по наклону закрытий сюда
    сознательно НЕ входит: жёсткий блок контртренда допустим только от подтверждённой
    структуры — наклон во флэте резал бы половину валидных сигналов."""
    swings = det.find_swings(htf_df, cfg.swing_left, cfg.swing_right)
    highs = [s for s in swings if s.kind == Kind.HIGH]
    lows = [s for s in swings if s.kind == Kind.LOW]
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1].price > highs[-2].price
        hl = lows[-1].price > lows[-2].price
        if hh and hl:
            return Bias.BULL
        if not hh and not hl:
            return Bias.BEAR
    return None


def _bias_score(df: pd.DataFrame, side: Side, htf_df: pd.DataFrame | None,
                cfg: SMCConfig, entry: float | None = None) -> float:
    """Контекст-скор [0..1] (раунд-2: компоненты ШТРАФУЮТ противоход, floor 0.5 делал
    гейт вакуумным). Две честности против первой версии раунда-2:
    - тренд-компонент СЧИТАЕТСЯ ТОЛЬКО от настоящего HTF: пре-брейк тренд собственного
      фрейма против CHOCH по определению (это разворотный сетап) — штраф от себя же
      убивал флагманские развороты; нет старшего контекста -> нейтрально;
    - premium/discount оценивается по ЦЕНЕ ВХОДА (лимитка), не по close на момент скана:
      суть ретрейсмента — возврат цены в зону, close после импульса всегда «не в зоне»."""
    want = Bias.BEAR if side == Side.SHORT else Bias.BULL
    score = 0.5
    if htf_df is not None:
        htf_sw = det.find_swings(htf_df, cfg.swing_left, cfg.swing_right)
        score += 0.25 if det.market_structure(htf_df, htf_sw) == want else -0.25
    dr = dealing_range(det.find_swings(df, cfg.swing_left, cfg.swing_right))
    if dr:
        lo, hi = dr
        mid = (lo + hi) / 2.0
        ref_price = float(entry) if entry is not None else float(df["close"].iloc[-1])
        in_premium = ref_price > mid
        right_zone = (side == Side.SHORT and in_premium) or (side == Side.LONG
                                                             and not in_premium)
        score += 0.25 if right_zone else -0.25
    return max(0.0, min(score, 1.0))


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
        # бонус — если есть хоть одна НЕинвертированная зона у входа; «без бонуса» — только
        # когда ВСЕ ближние зоны перекрыты телом. Раньше судьбу решала СТАРЕЙШАЯ зона
        # (near[0]): свежий валидный FVG не получал бонуса из-за древней инвертированной
        # зоны рядом, и наоборот (аудит-2 стратегии [8]).
        if any(not det.apply_inversion_rule(df, z).inverted for z in near):
            score += 0.4
            reasons.append("FVG/имбаланс у входа")
        else:
            reasons.append("зоны входа инвертированы (перекрыты телом) — без бонуса")

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
    # A/B 11.08.2026: жёсткое требование закрепа телом (см. SMCConfig.enforce_body_close)
    if cfg.enforce_body_close and not brk.body_confirmed:
        return None
    # A/B 11.08.2026: вето «слом в имбаланс» (см. SMCConfig.veto_break_into_hpz)
    if cfg.veto_break_into_hpz and _break_into_hpz(df, brk, cfg):
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

    # направленный свип: для SHORT-якоря (свинг-хай) валиден только съём ХАЯ, для LONG —
    # только съём ЛОЯ; иначе реклейм уровня засчитывался как свип (аудит-2 стратегии [4])
    expect = Kind.HIGH if side == Side.SHORT else Kind.LOW
    sweep = det.detect_sweep(df, level, lookback=cfg.sweep_lookback, expect=expect)
    clean = _impulse_clean(df, s_index, e_index, cfg.max_impulse_wick_ratio)
    return _Impulse(side, float(S), float(E), s_index, e_index,
                    swept=sweep is not None, clean=clean, brk=brk, swings=swings)


def _break_into_hpz(df: pd.DataFrame, brk: det.StructureBreak, cfg: SMCConfig) -> bool:
    """Правило отмены «слом в имбаланс»: закрытие решающего бара слома внутри
    ПРОТИВОПОЛОЖНОЙ неинверсированной HPZ-зоны (FVG/GAP/BPR), сформированной до слома.

    Слом вниз, воткнувшийся в бычий имбаланс-магнит, по паку не торгуется: зона с
    высокой вероятностью даст реакцию против входа. Инверсия зоны проверяется только
    барами окна (df и есть окно скана до текущего закрытого бара) — лукахеда нет.
    """
    close = float(df["close"].iloc[brk.break_index])
    want = Bias.BULL if brk.direction == Bias.BEAR else Bias.BEAR
    fvgs = det.detect_fvg(df) if {"FVG", "BPR"} & set(cfg.hpz_kinds) else []
    zones: list[det.Zone] = []
    if "FVG" in cfg.hpz_kinds:
        zones += fvgs
    if "GAP" in cfg.hpz_kinds:
        zones += det.detect_gap(df)
    if "BPR" in cfg.hpz_kinds:
        zones += det.detect_bpr(fvgs)
    for z in zones:
        if z.bias != want or z.index_end > brk.break_index:
            continue
        if z.bottom <= close <= z.top and not det.apply_inversion_rule(df, z).inverted:
            return True
    return False


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
        tf_min = SMCEngine.TF_MINUTES.get(timeframe, 60)
        m_break = _bar_close_minute_ny(df, imp.brk.break_index, tf_min)
        m_now = _bar_close_minute_ny(df, len(df) - 1, tf_min)
        if m_break is not None and m_now is not None:
            lo, hi = cfg.entry_window_ny
            in_entry_window = _in_window(m_now, lo, hi)
            b_lo, b_hi = cfg.break_window_ny
            if not _in_window(m_break, b_lo, b_hi):
                return None                        # слом вне окна съёма ликвидности

    bias = _bias_score(df, imp.side, htf_df, cfg, entry=entry)
    trigger = 1.0 if imp.brk.body_confirmed else 0.5  # закреп телом / только закрытием
    sweep = 1.0 if imp.swept else 0.0    # без свипа — 0, не 0.5 (раунд-2)
    confl, cf_reasons = _confluence_score(
        df, entry, imp.side, cfg, imp.swings, imp.clean, in_entry_window)

    confidence = _confidence(bias, trigger, sweep, confl, cfg)
    if confidence < cfg.min_confidence:
        return None

    reasons = [
        f"свип {'хая' if imp.side == Side.SHORT else 'лоя'} (снятие ликвидности)",
        # честный текст по факту (аудит 11.08.2026): раньше «закреп телом» писался
        # безусловно, даже когда закрепа не было и trigger=0.5
        f"слом структуры {imp.brk.kind} "
        f"({'закреп телом' if imp.brk.body_confirmed else 'только закрытием'})",
        "фибо 1/0.62/0/-0.26 по импульсу",
    ] + cf_reasons
    return _mk_signal(df, symbol, imp.side, timeframe, entry, stop, [take],
                      cfg.rr_6226, confidence, reasons, imp, setup="6226",
                      scores={"bias": round(bias, 3), "trigger": trigger,
                              "sweep": sweep, "confl": round(confl, 3)})


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

    bias = _bias_score(df, imp.side, htf_df, cfg, entry=entry)
    trigger = 1.0 if imp.brk.body_confirmed else 0.5  # закреп телом / только закрытием
    sweep = 1.0 if imp.swept else 0.0    # без свипа — 0, не 0.5 (раунд-2)
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
                      cfg.rr_setup11, confidence, reasons, imp, setup="1:1",
                      scores={"bias": round(bias, 3), "trigger": trigger,
                              "sweep": sweep, "confl": round(confl, 3)})


# ---------------------------------------------------------------------------
# §2.2 LTF-модель по POI (вход от OB/BB после слома)
# ---------------------------------------------------------------------------
def build_ltf_poi(htf_df: pd.DataFrame | None, ltf_df: pd.DataFrame, symbol: str,
                  cfg: SMCConfig | None = None, timeframe: str = "m5") -> Signal | None:
    """Слом LTF -> откат в OB/BB -> вход от MT(50%), стоп за дальнюю границу блока.

    HTF-тренд идёт как усилитель bias (совпадение направления += confidence).
    htf_df=None (старший ТФ набора) — нейтральный bias и честная причина без «HTF».
    """
    cfg = cfg or SMCConfig()
    imp = _find_impulse(ltf_df, cfg)
    if imp is None:
        return None
    br_dir = Bias.BEAR if imp.side == Side.SHORT else Bias.BULL
    block = det.detect_order_block(ltf_df, imp.swings, imp.brk, br_dir,
                                   full_move=cfg.ob_full_move)
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

    if htf_df is None:
        # старший ТФ набора: HTF-контекста НЕТ. Раньше сюда передавали тот же frame — bias
        # 0.75 просто повторял собственный слом, а причина врала про «контекст HTF»
        # (аудит-2 стратегии [6]). Честно: нейтральный bias, честная причина.
        aligned = False
        bias = 0.5
        ctx_reason = "без HTF-контекста (старший ТФ набора)"
    else:
        htf_sw = det.find_swings(htf_df, cfg.swing_left, cfg.swing_right)
        htf_trend = det.market_structure(htf_df, htf_sw)
        aligned = ((htf_trend == Bias.BEAR and imp.side == Side.SHORT)
                   or (htf_trend == Bias.BULL and imp.side == Side.LONG))
        bias = 0.75 if aligned else 0.5
        ctx_reason = "контекст/POI HTF" + (" (совпадает с трендом)" if aligned else "")
    trigger = 1.0 if imp.brk.body_confirmed else 0.5  # закреп телом / только закрытием
    sweep = 1.0 if imp.swept else 0.0    # без свипа — 0, не 0.5 (раунд-2)
    confl, cf_reasons = _confluence_score(ltf_df, entry, imp.side, cfg, imp.swings, imp.clean, None)
    confidence = _confidence(bias, trigger, sweep, confl, cfg)
    if confidence < cfg.min_confidence:
        return None

    reasons = [
        ctx_reason,
        f"слом LTF {imp.brk.kind}",
        f"вход от {block.kind} (MT 50%), стоп за дальнюю границу блока",
    ] + cf_reasons
    return _mk_signal(ltf_df, symbol, imp.side, timeframe, entry, stop, [take],
                      round(rr, 2), confidence, reasons, imp, setup=f"LTF-{block.kind}",
                      scores={"bias": bias, "trigger": trigger,
                              "sweep": sweep, "confl": round(confl, 3)})


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

    bias = _bias_score(df, imp.side, htf_df, cfg, entry=entry)
    trigger = 1.0 if imp.brk.body_confirmed else 0.5  # закреп телом / только закрытием
    sweep = 1.0 if imp.swept else 0.0    # без свипа — 0, не 0.5 (раунд-2)
    confl, cf_reasons = _confluence_score(df, entry, imp.side, cfg, imp.swings, imp.clean, None)
    confidence = _confidence(bias, trigger, sweep, confl, cfg)
    if confidence < cfg.min_confidence:
        return None

    reasons = [f"модель открытия {model}", ez, "стоп за точку разворота/свип"] + cf_reasons
    return _mk_signal(df, symbol, imp.side, timeframe, entry, stop, [take],
                      rr, confidence, reasons, imp, setup=f"open-{model}",
                      scores={"bias": round(bias, 3), "trigger": trigger,
                              "sweep": sweep, "confl": round(confl, 3)})


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


def _entry_passed(sig: Signal, market: float, mode: str) -> bool:
    """Цена закрытия решающего бара уже за стопом (mode="stop") или за уровнем входа
    (mode="entry"): LONG-лимитка ниже рынка ждёт отката, а рынок уже под ней."""
    if mode == "stop":
        return market <= sig.stop if sig.side == Side.LONG else market >= sig.stop
    if mode == "entry":
        return market < sig.entry if sig.side == Side.LONG else market > sig.entry
    raise ValueError(f"late_entry_gate: {mode!r}")


def _mk_signal(df: pd.DataFrame, symbol: str, side: Side, timeframe: str,
               entry: float, stop: float,
               tps: list[float], rr: float, confidence: float, reasons: list[str],
               imp: _Impulse, setup: str, scores: dict | None = None) -> Signal:
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
            # суб-скоры confidence (аудит 11.08.2026): до этого bias/trigger/confl
            # невосстановимо терялись — калибровка весов w_* была невозможна ни по
            # журналу, ни по кэшу сигналов. Чистая инструментация, дефолты не меняет.
            **({"scores": scores} if scores else {}),
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
                  "1h": 60, "H1": 60, "h1": 60, "2h": 120, "h2": 120, "H2": 120,
                  "4h": 240, "H4": 240, "h4": 240, "6h": 360, "12h": 720,
                  "1d": 1440, "D1": 1440, "d1": 1440}

    def __init__(self, config: SMCConfig | None = None):
        self.cfg = config or SMCConfig()
        # сигналы, которые выиграли бы бар БЕЗ гейта «вход уже пройден», — для тени
        # (scan_auto пишет их как shadow='late_entry'); сброс в начале analyze_multi
        self.late_shadow: list[Signal] = []

    def _setup_enabled(self, tag: str) -> bool:
        """Тег включён при префикс-матче в ЛЮБУЮ сторону: enabled=('LTF',) покрывает
        LTF-BB/LTF-OB, а enabled=('LTF-BB',) проходит пре-гейт 'LTF' (точная фильтрация
        делается пост-фильтром по snapshot['setup'])."""
        return any(e and (tag == e or tag.startswith(e) or e.startswith(tag))
                   for e in self.cfg.enabled_setups)

    def analyze_single(self, df: pd.DataFrame, symbol: str, timeframe: str,
                       htf_df: pd.DataFrame | None = None,
                       partner_df: pd.DataFrame | None = None) -> Signal | None:
        candidates: list[Signal | None] = []
        if self._setup_enabled("6226"):
            candidates.append(build_6226(df, symbol, self.cfg, timeframe, htf_df))
        if self._setup_enabled("1:1"):
            candidates.append(detect_setup_1_1(df, symbol, self.cfg, timeframe, htf_df))
        if self._setup_enabled("open-SMC"):
            candidates.append(build_opening_model(df, symbol, self.cfg, timeframe, "SMC", htf_df))
        if self._setup_enabled("LTF"):
            # htf_df=None на старшем ТФ передаётся КАК ЕСТЬ: подстановка df вместо HTF
            # выдавала «контекст HTF» из самого же frame (аудит-2 стратегии [6])
            candidates.append(build_ltf_poi(htf_df, df, symbol, self.cfg, timeframe))
        # страховочный фильтр по фактическому тегу из snapshot
        signals = [c for c in candidates if c is not None]
        signals = [c for c in signals
                  if self._setup_enabled((c.indicators_snapshot or {}).get("setup", ""))]
        # костовый гейт: слишком тесный стоп = издержки съедают риск (см. min_stop_pct)
        if signals and self.cfg.min_stop_pct > 0:
            signals = [c for c in signals
                       if c.entry > 0
                       and abs(c.entry - c.stop) / c.entry * 100.0 >= self.cfg.min_stop_pct]
        gate = self.cfg.late_entry_gate
        if signals and gate != "off":
            # гейт ДО выбора лучшего (как min_stop_pct): бар достаётся валидному кандидату;
            # победитель без гейта, если он «поздний», уходит в тень для проверки вперёд
            market = float(df["close"].iloc[-1])
            winner = self._finalize([c for c in signals
                                     if not _entry_passed(c, market, gate)],
                                    df, htf_df, partner_df)
            ungated = self._finalize(signals, df, htf_df, partner_df)
            if (ungated is not None and ungated is not winner
                    and _entry_passed(ungated, market, gate)):
                self.late_shadow.append(ungated)
            return winner
        return self._finalize(signals, df, htf_df, partner_df)

    def _finalize(self, signals: list[Signal], df: pd.DataFrame,
                  htf_df: pd.DataFrame | None,
                  partner_df: pd.DataFrame | None) -> Signal | None:
        """Хвост analyze_single: блок контртренда HTF, SMT-вето, лучший по confidence."""
        # жёсткий блок контртренда: подтверждённая структура HTF против стороны сигнала ->
        # сигнал не выдаётся (штраф −0.25 в confidence пропускал лонги в сползании 17.07).
        # htf_df=None (старший ТФ набора) и неподтверждённая структура — блока нет.
        if signals and self.cfg.htf_block_countertrend and htf_df is not None:
            trend = htf_confirmed_trend(htf_df, self.cfg)
            if trend is not None:
                want = {Bias.BULL: Side.LONG, Bias.BEAR: Side.SHORT}[trend]
                signals = [c for c in signals if c.side == want]
        # SMT-вето (A/B 11.08.2026): активная дивергенция коррелированного партнёра
        # даёт подсказку стороны ОТСТАЮЩЕМУ активу (нам) — кандидат против подсказки
        # отбрасывается; без дивергенции/партнёра фильтр нейтрален (см. SMCConfig.smt_mode)
        if signals and self.cfg.smt_mode == "veto" and partner_df is not None:
            smt = det.detect_smt(partner_df, df, "direct", self.cfg.smt_lookback)
            if smt is not None:
                signals = [c for c in signals if c.side.value == smt.side]
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
        self.late_shadow = []
        for i, tf in enumerate(tfs):
            htf = next((frames[h] for h in tfs[i + 1:]), None)   # ближайший старший
            sig = self.analyze_single(frames[tf], symbol, tf, htf_df=htf)
            if sig is not None:
                out.append(sig)
        return out
