"""Минутный судья выхода — модель E замера реализма трейла
(docs/measurements/2026-10-30-trail-realism.md): биржевой трейлинг-стоп, следящий за ценой
непрерывно, на минутных свечах.

Зачем: часовой судья (journal._outcome_from_bars) подтягивает стоп по закрытию бара и не
видит откат внутри часа — на 4 635 исторических сделках он завышал итог примерно на 0.13R
за сделку. Новые сигналы со `trail.judge = "1m"` в снапшоте судятся здесь; старые — по-
прежнему часовым судьёй (журнал неизменяем).

Правила (одна ветка кода для LONG и SHORT: цены на оси x = ±price, «выше» = в пользу):
* путь внутри минуты — бычья свеча (C ≥ O) O→L→H→C, медвежья O→H→L→C; между минутами
  C→O следующей;
* вход — первое касание уровня входа путём после решающего бара; окно ожидания — как у
  часового судьи (MAX_WAIT_HOURS), дальше «нет входа»;
* стоп −1R по уровню; после хода +act_r·R от входа (с момента входа, в том числе в минуте
  входа) уровень = экстремум − dist_r·R и ведётся по каждой точке пути;
* срабатывание — худший порядок внутри минуты (путь по цвету свечи);
* gap_open: уровень пробит ПЕРВОЙ точкой минуты (открытием) — исполнение по открытию,
  как рыночный ордер после срабатывания; иначе по уровню. Прод — True; режим паритета с
  замером — False (модель E исполняла по уровню и в гэпе);
* потолок удержания: от начала ЧАСА входа, выход по close минуты, закрывающей час
  anchor + max_hold_h, — та же точка, что у часового судьи;
* тейк (tp_exit) и безубыток (be_*) — расширение модели E теми же правилами, что у
  часового судьи; у прод-сигналов обоих нет.

Детерминирован и без побочных эффектов: ни БД, ни сети. Паритет с эталоном
(`scripts/research_fine_judge.judge(mode="path", immediate=True)`) проверяет
`scripts/judge_parity.py`.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MIN_MS = 60_000
HOUR_MS = 3_600_000


@dataclass(frozen=True)
class Verdict:
    """event: waiting (входа ещё нет, данные кончились) | nofill (окно ожидания вышло) |
    open (вход есть, развязки нет) | stopped | trail | tp | timeout.
    r — gross R (4 знака), exit_price/exit_ms — для терминальных исходов, fill_ms — минута
    входа, level_price — действующий уровень стопа/трейла (для open — его ставит executor)."""
    event: str
    r: float | None = None
    exit_price: float | None = None
    fill_ms: int | None = None
    exit_ms: int | None = None
    level_price: float | None = None
    active: bool = False


def _points(o: float, h: float, lo: float, c: float, order: str) -> tuple:
    if order == "LH" or (order == "color" and c >= o):
        return (o, lo, h, c)
    return (o, h, lo, c)


def judge_path(entry: float, stop: float, side: str, bars: np.ndarray, *,
               decision_ms: int, fill_until_ms: int, act_r: float, dist_r: float,
               max_hold_h: float, fill_from_ms: int | None = None,
               tp: float | None = None, tp_exit: bool = False,
               be_trigger_r: float = 0.0, be_lock_r: float = 0.0,
               gap_open: bool = True, order: str = "color") -> Verdict:
    """bars — (n, 5): ts мс открытия минуты, o, h, l, c по возрастанию ts (минуты до
    decision_ms допустимы — пропускаются). fill_from_ms — с какой минуты искать вход
    (по умолчанию с decision_ms; режим паритета передаёт час входа журнала)."""
    risk = abs(entry - stop)
    if risk <= 0 or len(bars) == 0:
        return Verdict("waiting")
    sgn = 1.0 if side == "LONG" else -1.0
    E = sgn * entry
    S0 = sgn * stop
    A = E + act_r * risk
    D = dist_r * risk
    T = sgn * tp if (tp is not None and tp_exit) else None
    BE_T = E + be_trigger_r * risk if be_trigger_r > 0 else None
    BE_L = E + be_lock_r * risk
    ts = bars[:, 0]
    n = len(bars)
    start_ms = decision_ms if fill_from_ms is None else fill_from_ms
    i0 = int(np.searchsorted(ts, start_ms, "left"))
    level = S0
    active = False
    peak = E
    filled = False
    fill_ms: int | None = None
    end_ms: int | None = None
    prev: float | None = None
    last_close: float | None = None
    last_ts: int | None = None

    def _r(x: float) -> float:
        return round((x - E) / risk, 4)

    def _exit(ev: str, x: float, t: int) -> Verdict:
        return Verdict(ev, _r(x), sgn * x, fill_ms, t, sgn * level, active)

    for i in range(i0, n):
        t = int(ts[i])
        if filled and end_ms is not None and t >= end_ms:
            # дыра в данных перед потолком — закрываем по последнему известному close
            assert last_close is not None and last_ts is not None
            return _exit("timeout", last_close, last_ts)
        if not filled and t >= fill_until_ms:
            return Verdict("nofill")
        o, h, lo, c = float(bars[i, 1]), float(bars[i, 2]), float(bars[i, 3]), float(bars[i, 4])
        pts = [sgn * p for p in _points(o, h, lo, c, order)]
        seq = ([prev] if prev is not None else []) + pts
        for k in range(len(seq) - 1):
            a, b = seq[k], seq[k + 1]
            gap_seg = k == 0 and prev is not None            # переход C_prev -> O минуты
            if not filled:
                if min(a, b) <= E <= max(a, b):
                    filled = True
                    fill_ms = t
                    anchor = t // HOUR_MS * HOUR_MS
                    end_ms = anchor + int((max_hold_h + 1) * HOUR_MS) if max_hold_h > 0 else None
                    a = E
                else:
                    continue
            if not active and a >= A:                      # к моменту входа уже за +act_r
                active, peak = True, a
                level = max(level, peak - D)
            if BE_T is not None and a >= BE_T:
                level = max(level, BE_L)
            if b < a:
                if b <= level:
                    x = b if (gap_open and gap_seg) else level
                    return _exit("trail" if level != S0 else "stopped", x, t)
            elif b > a:
                if T is not None and b >= T:
                    return _exit("tp", T, t)
                if active:
                    if b > peak:
                        peak = b
                        level = max(level, peak - D)
                elif b >= A:
                    active, peak = True, b
                    level = max(level, peak - D)
                if BE_T is not None and b >= BE_T:
                    level = max(level, BE_L)
        prev = pts[-1]
        last_close = pts[-1]
        last_ts = t
        if filled and end_ms is not None and t + MIN_MS >= end_ms:
            return _exit("timeout", last_close, t)
    if filled:
        return Verdict("open", None, None, fill_ms, None, sgn * level, active)
    return Verdict("waiting")
