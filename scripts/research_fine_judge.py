"""Мелкий судья для замера реализма трейла (docs/measurements/2026-10-30-trail-realism.md).

Прод его не импортирует. Вход, стоп, активация и отступ — как у живого судьи; меняется только
то, как оценивается трейл. Две семантики:

* `discrete` — правила живого судьи по барам: стоп бара по уровню с прошлого бара, трейл
  подтягивается на закрытии бара (не на баре филла), потолок — close бара, открытого не раньше
  филл + 168ч. На 1h-барах обязан повторить журнал (паритет 3 регистрации).
* `path` — биржевой трейлинг-стоп, следящий за ценой непрерывно. Путь внутри бара: бычий
  (C ≥ O) O→L→H→C, медвежий O→H→L→C (`order='LH'`/`'HL'` — принудительно для всех баров),
  между барами C→O следующего. Филл — первое касание входа внутри часа филла журнала. Стоп
  −1R по уровню. После активации (+act_r от входа) уровень = экстремум − dist_r, выход по
  уровню. `immediate=False` (модель J) — активация только после конца часа филла;
  `immediate=True` (модель E, как e5) — с момента филла.

Время — миллисекунды UTC открытия баров. Цены приводятся к «выгодной» оси x = ±price,
поэтому одна ветка кода обслуживает LONG и SHORT.
"""
from __future__ import annotations

import numpy as np

HOUR_MS = 3_600_000


def judge(entry: float, stop: float, side: str, fill_open_ms: int, bars: np.ndarray, *,
          bar_ms: int, mode: str, immediate: bool = False, order: str = "color",
          act_r: float = 1.5, dist_r: float = 0.2, max_hold_h: float = 168.0) -> tuple:
    """bars — (n, 5): ts, o, h, l, c по возрастанию ts, начиная не позже часа филла.

    -> (event, r_gross, exit_bar_open_ms), event ∈ stopped | trail | timeout;
       ('nofill', None, None) — в часе филла мелкие бары не касаются входа;
       ('unresolved', None, None) — данные кончились раньше выхода."""
    risk = abs(entry - stop)
    sgn = 1.0 if side == "LONG" else -1.0
    i0 = int(np.searchsorted(bars[:, 0], fill_open_ms, "left"))
    if mode == "discrete":
        return _discrete(entry, stop, risk, sgn, fill_open_ms, bars, i0, act_r, dist_r,
                         max_hold_h)
    return _path(entry, stop, risk, sgn, fill_open_ms, bars, i0, bar_ms, immediate, order,
                 act_r, dist_r, max_hold_h)


def _discrete(entry, stop, risk, sgn, fill_open_ms, bars, i0, act_r, dist_r, max_hold_h):
    ts, hi, lo, cl = bars[:, 0], bars[:, 2], bars[:, 3], bars[:, 4]
    n = len(bars)
    # бар филла — первый бар часа филла, чей диапазон содержит вход
    f = i0
    while f < n and ts[f] < fill_open_ms + HOUR_MS and not (lo[f] <= entry <= hi[f]):
        f += 1
    if f >= n or ts[f] >= fill_open_ms + HOUR_MS:
        return "nofill", None, None
    hold_ms = max_hold_h * HOUR_MS
    cur, peak = stop, entry
    for j in range(f, n):
        fav_hi = hi[j] if sgn > 0 else lo[j]          # экстремум в пользу сделки
        adv_lo = lo[j] if sgn > 0 else hi[j]          # экстремум против
        if sgn * adv_lo <= sgn * cur:
            ev = "stopped" if cur == stop else "trail"
            return ev, round(sgn * (cur - entry) / risk, 4), int(ts[j])
        if j != f and sgn * (fav_hi - entry) / risk >= act_r:
            peak = max(peak, fav_hi) if sgn > 0 else min(peak, fav_hi)
            cand = peak - sgn * dist_r * risk
            cur = max(cur, cand) if sgn > 0 else min(cur, cand)
        if ts[j] - ts[f] >= hold_ms:
            return "timeout", round(sgn * (cl[j] - entry) / risk, 4), int(ts[j])
    return "unresolved", None, None


def _points(o, h, lo, c, order):
    if order == "LH" or (order == "color" and c >= o):
        return (o, lo, h, c)
    return (o, h, lo, c)


def _path(entry, stop, risk, sgn, fill_open_ms, bars, i0, bar_ms, immediate, order,
          act_r, dist_r, max_hold_h):
    ts = bars[:, 0]
    n = len(bars)
    E = sgn * entry
    A = E + act_r * risk
    D = dist_r * risk
    level = sgn * stop                     # уровень выхода на оси x (стоп или трейл)
    active = False
    peak = E
    filled = False
    allow_ms = fill_open_ms if immediate else fill_open_ms + HOUR_MS
    end_ms = fill_open_ms + int((max_hold_h + 1) * HOUR_MS)   # close бара филл+168ч
    fill_end = fill_open_ms + HOUR_MS
    prev = None
    last_close = None
    for i in range(i0, n):
        t = int(ts[i])
        if t >= end_ms:                    # дыра в данных перед потолком — по последнему close
            if filled and last_close is not None:
                return "timeout", round((last_close - E) / risk, 4), int(ts[i - 1])
            break
        if not filled and t >= fill_end:
            return "nofill", None, None
        o, h, lo, c = bars[i, 1], bars[i, 2], bars[i, 3], bars[i, 4]
        pts = [sgn * p for p in _points(o, h, lo, c, order)]   # порядок по времени сохраняется
        seq =([prev] if prev is not None else []) + pts
        can = t >= allow_ms
        for k in range(len(seq) - 1):
            a, b = seq[k], seq[k + 1]
            if not filled:
                if min(a, b) <= E <= max(a, b):
                    filled = True
                    a = E
                else:
                    continue
            if not active and can and a >= A:          # к моменту разрешения уже за +act_r
                active, peak = True, a
                level = max(level, peak - D)
            if b < a:
                if b <= level:
                    ev = "trail" if active else "stopped"
                    return ev, round((level - E) / risk, 4), t
            elif b > a:
                if active:
                    if b > peak:
                        peak = b
                        level = max(level, peak - D)
                elif can and b >= A:
                    active, peak = True, b
                    level = max(level, peak - D)
        prev = pts[-1]
        last_close = pts[-1]
        if filled and t + bar_ms >= end_ms:
            return "timeout", round((last_close - E) / risk, 4), t
    return "unresolved", None, None
