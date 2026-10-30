"""Замер «модель DenFx» (docs/measurements/2026-10-31-denfx-model.md).

POI старшего ТФ (реакция на FVG TF0) → BOS на TF1 → лимит в FVG ноги BOS (или по рынку) →
стоп за минимумом ноги → цель на фрактале TF0, RR ≥ порога, без трейла. Вселенная и данные —
из замера «пробой волатильности». Ход — по часам; часы, где порядок событий важен, — по 1m.
Прод это не импортирует.

  nice -n 15 .venv/bin/python -m scripts.research_denfx selftest   # детекторы на синтетике и BTC
  nice -n 15 .venv/bin/python -m scripts.research_denfx grid       # 12 вариантов, по часам
  nice -n 15 .venv/bin/python -m scripts.research_denfx m1         # докачать нужные 1m-часы
  nice -n 15 .venv/bin/python -m scripts.research_denfx grid --m1  # пересчёт с 1m
  nice -n 15 .venv/bin/python -m scripts.research_denfx report     # выбор, проверка, заморозка
"""
from __future__ import annotations

import json
import math
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

import numpy as np
import pandas as pd

from scripts.research_vol_breakout import (
    D_MS,
    FROZEN,
    H_MS,
    HALVES,
    RES,
    TRAIN,
    VALID,
    funding_cost,
    load_daily,
    load_funding,
    load_hourly,
    ms,
    universe,
)

OUT = RES / "denfx"
M1_DIR = OUT / "m1"
TF_MS = {"1h": H_MS, "4h": 4 * H_MS, "1d": D_MS}
PAIRS = {"D4": ("1d", "4h"), "D1": ("1d", "1h"), "H1": ("4h", "1h")}

FVG_MAX_AGE = 60          # баров TF0
CONFIRM_TF0 = 3           # окно BOS, баров TF0
ORDER_TF0 = 2             # жизнь лимитки, баров TF0
HOLD_TF0 = 20             # макс. удержание, баров TF0
STOP_ATR = 0.1
FILL_EPS = 1e-4           # лимит налит, если цена прошла сквозь него на ~тик
MAKER = 0.0002
TAKER = 0.00055
SLIP = 0.0002
ATR_N = 14


@dataclass(frozen=True)
class Variant:
    pair: str     # D4 / D1 / H1
    rr: int       # 3 / 4
    entry: str    # LIM / MKT

    @property
    def key(self) -> str:
        return f"{self.pair}_RR{self.rr}_{self.entry}"


def grid() -> list[Variant]:
    return [Variant(p, r, e) for p in ("D4", "D1", "H1") for r in (3, 4) for e in ("LIM", "MKT")]


def parse_key(key: str) -> Variant:
    p, r, e = key.split("_")
    return Variant(p, int(r[2:]), e)


# ---------------------------------------------------------------- бары и детекторы

@dataclass
class Bars:
    ts: np.ndarray
    o: np.ndarray
    h: np.ndarray
    lo: np.ndarray
    c: np.ndarray
    atr: np.ndarray
    tf_ms: int


def make_bars(tf: str, hourly: np.ndarray, daily_arr: np.ndarray | None) -> Bars:
    if tf == "1d":
        a = daily_arr
        ts, o, h, lo, c = a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4]
    elif tf == "1h":
        ts = hourly[:, 0].astype(np.int64)
        o, h, lo, c = hourly[:, 1], hourly[:, 2], hourly[:, 3], hourly[:, 4]
    else:
        step = TF_MS[tf]
        g = (hourly[:, 0].astype(np.int64) // step) * step
        df = pd.DataFrame({"g": g, "o": hourly[:, 1], "h": hourly[:, 2], "lo": hourly[:, 3],
                           "c": hourly[:, 4]})
        agg = df.groupby("g").agg(o=("o", "first"), h=("h", "max"), lo=("lo", "min"),
                                  c=("c", "last"))
        ts = agg.index.to_numpy(np.int64)
        o, h, lo, c = (agg[k].to_numpy(float) for k in ("o", "h", "lo", "c"))
    pc = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(np.vstack([h - lo, np.abs(h - pc), np.abs(lo - pc)]), axis=0)
    atr = pd.Series(tr).rolling(ATR_N).mean().to_numpy()
    return Bars(ts, np.asarray(o, float), np.asarray(h, float), np.asarray(lo, float),
                np.asarray(c, float), atr, TF_MS[tf])


def swings(b: Bars) -> tuple[np.ndarray, np.ndarray]:
    """Свинги 2/2 → (is_high, is_low) по индексу бара i; известны после закрытия i+2."""
    h, lo, n = b.h, b.lo, len(b.h)
    sh = np.zeros(n, bool)
    sl = np.zeros(n, bool)
    if n >= 5:
        i = np.arange(2, n - 2)
        sh[i] = (h[i] > np.maximum(h[i - 2], h[i - 1])) & (h[i] >= np.maximum(h[i + 1], h[i + 2]))
        sl[i] = (lo[i] < np.minimum(lo[i - 2], lo[i - 1])) & (lo[i] <= np.minimum(lo[i + 1],
                                                                                  lo[i + 2]))
    return sh, sl


def last_known(flag: np.ndarray) -> np.ndarray:
    """m → индекс последнего свинга i с i+2 ≤ m (−1, если нет)."""
    n = len(flag)
    idx = np.where(flag, np.arange(n), -1)
    shifted = np.full(n, -1)
    if n > 2:
        shifted[2:] = idx[:-2]
    return np.maximum.accumulate(shifted)


@dataclass
class FVG:
    side: int        # +1 бычий, −1 медвежий
    j: int           # бар, на закрытии которого известен
    bottom: float
    top: float
    used: bool = False
    dead: bool = False

    @property
    def mid(self) -> float:
        return (self.bottom + self.top) / 2


def fvg_at(b: Bars, j: int) -> list[FVG]:
    out = []
    if j >= 2:
        if b.lo[j] > b.h[j - 2]:
            out.append(FVG(1, j, b.h[j - 2], b.lo[j]))
        if b.h[j] < b.lo[j - 2]:
            out.append(FVG(-1, j, b.h[j], b.lo[j - 2]))
    return out


# ---------------------------------------------------------------- 1m

M1_URL = ("https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}&interval=1"
          "&start={s}&end={e}&limit=1000")
M1_GAP_S = 0.35


class Minutes:
    """Кэш 1m: sym → (массив [ts,o,h,l,c], множество закрытых часов)."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.cache: dict = {}
        self.need: dict = {}

    def _load(self, sym: str):
        if sym not in self.cache:
            npy, js = M1_DIR / f"{sym}.npy", M1_DIR / f"{sym}.json"
            if npy.exists() and js.exists():
                self.cache[sym] = (np.load(npy), set(json.loads(js.read_text())))
            else:
                self.cache[sym] = (np.zeros((0, 5)), set())
        return self.cache[sym]

    def hour(self, sym: str, hour_ts: int) -> np.ndarray | None:
        """Минутки часа или None (нужно докачать / нет данных) — тогда пессимистично."""
        self.need.setdefault(sym, set()).add(int(hour_ts))
        if not self.enabled:
            return None
        arr, have = self._load(sym)
        if int(hour_ts) not in have or not len(arr):
            return None
        i0 = np.searchsorted(arr[:, 0], hour_ts)
        i1 = np.searchsorted(arr[:, 0], hour_ts + H_MS)
        seg = arr[i0:i1]
        return seg if len(seg) else None


def _m1_get(sym: str, s: int, e: int) -> list:
    for attempt in range(6):
        time.sleep(M1_GAP_S)
        try:
            with urllib.request.urlopen(M1_URL.format(sym=sym, s=s, e=e), timeout=20) as r:
                body = json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(2 + 3 * attempt)
            continue
        code = body.get("retCode")
        if code == 0:
            return body["result"]["list"]
        if code in (10006, 10018):
            time.sleep(10 + 5 * attempt)
            continue
        return []
    return []


def m1_fetch(need: dict) -> None:
    M1_DIR.mkdir(parents=True, exist_ok=True)
    total = sum(len(v) for v in need.values())
    done = 0
    for sym, hours in sorted(need.items()):
        npy, js = M1_DIR / f"{sym}.npy", M1_DIR / f"{sym}.json"
        arr = np.load(npy) if npy.exists() else np.zeros((0, 5))
        have = set(json.loads(js.read_text())) if js.exists() else set()
        todo = sorted(set(int(h) for h in hours) - have)
        rows = [arr] if len(arr) else []
        i = 0
        while i < len(todo):
            j = i
            while j + 1 < len(todo) and todo[j + 1] - todo[i] < 16 * H_MS:
                j += 1
            got = _m1_get(sym, todo[i], todo[j] + H_MS - 1)
            if got:
                rows.append(np.array([[float(x) for x in g[:5]] for g in got]))
            have.update(todo[i:j + 1])
            done += j - i + 1
            i = j + 1
        if rows:
            a = np.concatenate(rows)
            a = a[np.argsort(a[:, 0])]
            _, keep = np.unique(a[:, 0], return_index=True)
            a = a[keep]
        else:
            a = np.zeros((0, 5))
        np.save(npy, a)
        js.write_text(json.dumps(sorted(have)))
        if todo:
            print(f"  1m {sym}: +{len(todo)} ч, всего {done}/{total}", flush=True)


# ---------------------------------------------------------------- исполнение

def first_touch(seg: np.ndarray, side: int, stop: float, target: float) -> str | None:
    """Порядок «стоп/цель» по минуткам сегмента. Одна минута с обоими → стоп."""
    for row in seg:
        hit_stop = row[3] <= stop if side > 0 else row[2] >= stop
        hit_tgt = row[2] >= target if side > 0 else row[3] <= target
        if hit_stop:
            return "stop"
        if hit_tgt:
            return "target"
    return None


@dataclass
class Trade:
    sym: str
    key: str
    side: int
    bos_ts: int
    entry_ts: int
    exit_ts: int
    how: str
    entry: float
    stop: float
    target: float
    gross_r: float
    net_r: float
    rr: float
    pess: int


def run_position(sym, side, entry_lvl, entry_fill, fee_in, stop, target, t0, hold_until,
                 hourly, mins: Minutes, fund, start_minutes: np.ndarray | None):
    """Позиция открыта в t0 (мс). start_minutes — минутки после налива в часе налива.
    → (how, exit_lvl, exit_fill, fee_out, exit_ts, pess)."""
    pess = 0
    if start_minutes is not None and len(start_minutes):
        r = first_touch(start_minutes, side, stop, target)
        if r is not None:
            row = next(x for x in start_minutes
                       if (x[3] <= stop if side > 0 else x[2] >= stop)
                       or (x[2] >= target if side > 0 else x[3] <= target))
            t_hit = int(row[0]) + 60_000
            if r == "target":
                return "target", target, target, MAKER, t_hit, pess
            lvl = min(stop, row[1]) if side > 0 else max(stop, row[1])
            return "stop", stop, lvl * (1 - side * SLIP), TAKER, t_hit, pess
    hts = hourly[:, 0].astype(np.int64)
    i = int(np.searchsorted(hts, ((t0 + H_MS - 1) // H_MS) * H_MS))
    while i < len(hourly):
        ht = int(hts[i])
        if ht >= hold_until:
            break
        o, h, lo = hourly[i, 1], hourly[i, 2], hourly[i, 3]
        hit_stop = lo <= stop if side > 0 else h >= stop
        hit_tgt = h >= target if side > 0 else lo <= target
        if hit_stop or hit_tgt:
            how = "stop" if hit_stop else "target"
            lvl_open = o
            t_hit = ht + H_MS
            if hit_stop and hit_tgt:
                seg = mins.hour(sym, ht)
                if seg is None:
                    pess += 1
                    how = "stop"
                else:
                    how = first_touch(seg, side, stop, target) or "stop"
                    row = next((x for x in seg
                                if (x[3] <= stop if side > 0 else x[2] >= stop)
                                or (x[2] >= target if side > 0 else x[3] <= target)), None)
                    if row is not None:
                        lvl_open, t_hit = row[1], int(row[0]) + 60_000
            if how == "target":
                return "target", target, target, MAKER, t_hit, pess
            lvl = min(stop, lvl_open) if side > 0 else max(stop, lvl_open)
            return "stop", stop, lvl * (1 - side * SLIP), TAKER, t_hit, pess
        i += 1
    # выход по времени: закрытие последнего часа до hold_until
    j = min(max(int(np.searchsorted(hts, hold_until)) - 1, 0), len(hourly) - 1)
    c = hourly[j, 4]
    return "time", c, c * (1 - side * SLIP), TAKER, int(hts[j]) + H_MS, pess


def simulate(sym: str, v: Variant, hourly: np.ndarray, daily_arr: np.ndarray, uni: dict, fund,
             mins: Minutes, t_from: int, t_to: int, counters: dict) -> list[Trade]:
    tf0, tf1 = PAIRS[v.pair]
    b0 = make_bars(tf0, hourly, daily_arr)
    b1 = make_bars(tf1, hourly, daily_arr)
    if len(b0.ts) < 10 or len(b1.ts) < 50:
        return []
    sh0, sl0 = swings(b0)
    sh1, sl1 = swings(b1)
    lk_sh1, lk_sl1 = last_known(sh1), last_known(sl1)
    hts = hourly[:, 0].astype(np.int64)
    h_first, h_last = int(hts[0]), int(hts[-1]) + H_MS
    trades: list[Trade] = []
    active: list[FVG] = []
    known_hi: list[float] = []   # значения известных свинг-хаев TF0
    known_lo: list[float] = []
    free_from = 0
    for k in range(len(b0.ts)):
        # свинги TF0, подтверждённые закрытием k (бар k−2)
        if k >= 2 and k - 2 < len(sh0):
            if sh0[k - 2]:
                known_hi.append(b0.h[k - 2])
            if sl0[k - 2]:
                known_lo.append(b0.lo[k - 2])
        t_close = int(b0.ts[k]) + b0.tf_ms
        if k >= 1 and h_first <= t_close <= h_last:
            reacted = {1: None, -1: None}
            for f in active:
                if f.used or f.dead or f.j >= k:
                    continue
                if f.side > 0:
                    ok = b0.c[k - 1] > f.top and b0.lo[k] <= f.top and b0.c[k] > f.mid
                else:
                    ok = b0.c[k - 1] < f.bottom and b0.h[k] >= f.bottom and b0.c[k] < f.mid
                if ok:
                    f.used = True
                    prev = reacted[f.side]
                    if prev is None or f.j > prev.j:
                        reacted[f.side] = f
            for side, f in reacted.items():
                if f is None or t_close < free_from or not (t_from <= t_close < t_to + 3 * D_MS):
                    continue
                tr = setup(sym, v, side, f, k, b0, b1, sh1, sl1, lk_sh1, lk_sl1, known_hi,
                           known_lo, active, hourly, hts, uni, fund, mins, t_from, t_to,
                           counters)
                if tr is not None:
                    trades.append(tr)
                    free_from = tr.exit_ts
                elif counters.get("_busy_until", 0) > free_from:
                    free_from = counters["_busy_until"]
                counters.pop("_busy_until", None)
        # инвалидация и старение
        for f in active:
            if f.dead:
                continue
            if (f.side > 0 and b0.c[k] < f.bottom) or (f.side < 0 and b0.c[k] > f.top):
                f.dead = True
            if k - f.j > FVG_MAX_AGE:
                f.dead = True
        active = [f for f in active if not f.dead]
        active.extend(fvg_at(b0, k))
    return trades


def setup(sym, v, side, f, k, b0, b1, sh1, sl1, lk_sh1, lk_sl1, known_hi, known_lo, active,
          hourly, hts, uni, fund, mins, t_from, t_to, counters):
    tf1_ms = b1.tf_ms
    t_r = int(b0.ts[k]) + b0.tf_ms
    close_k = b0.c[k]
    # цель: точка начала коррекции в POI — экстремум TF0 от бара FVG j до k−1 (поправка после
    # самотеста, см. регистрацию); встречный активный FVG по пути — его ближний край
    if side > 0:
        target = float(np.max(b0.h[f.j:k]))
        if not target > close_k:
            counters["no_target"] = counters.get("no_target", 0) + 1
            return None
        opp = [g.bottom for g in active if g.side < 0 and not g.dead and g.j < k
               and close_k < g.bottom < target]
        if opp:
            target = min(opp)
    else:
        target = float(np.min(b0.lo[f.j:k]))
        if not target < close_k:
            counters["no_target"] = counters.get("no_target", 0) + 1
            return None
        opp = [g.top for g in active if g.side > 0 and not g.dead and g.j < k
               and target < g.top < close_k]
        if opp:
            target = max(opp)
    counters["reactions"] = counters.get("reactions", 0) + 1
    # BOS на TF1 в окне CONFIRM_TF0 баров TF0
    m0 = int(np.searchsorted(b1.ts, t_r))
    m_end = int(np.searchsorted(b1.ts, t_r + CONFIRM_TF0 * b0.tf_ms))
    bos = None
    for m in range(m0, min(m_end, len(b1.ts))):
        c, h, lo = b1.c[m], b1.h[m], b1.lo[m]
        if (side > 0 and c < f.bottom) or (side < 0 and c > f.top):
            break
        if (side > 0 and h >= target) or (side < 0 and lo <= target):
            break
        i_sw = lk_sh1[m] if side > 0 else lk_sl1[m]
        if i_sw < 0:
            continue
        lvl = b1.h[i_sw] if side > 0 else b1.lo[i_sw]
        if (side > 0 and c > lvl) or (side < 0 and c < lvl):
            bos = (m, i_sw)
            break
    if bos is None:
        counters["no_bos"] = counters.get("no_bos", 0) + 1
        return None
    m, i_sw = bos
    t_bos = int(b1.ts[m]) + tf1_ms
    if not (t_from <= t_bos < t_to):
        return None
    day = (t_bos // D_MS) * D_MS
    if sym not in uni.get(day, ()):
        counters["not_in_universe"] = counters.get("not_in_universe", 0) + 1
        return None
    seg_lo = b1.lo[i_sw:m + 1] if side > 0 else b1.h[i_sw:m + 1]
    i_ext = i_sw + (int(np.argmin(seg_lo)) if side > 0 else int(np.argmax(seg_lo)))
    atr = b1.atr[m]
    if not np.isfinite(atr):
        return None
    stop = (b1.lo[i_ext] - STOP_ATR * atr) if side > 0 else (b1.h[i_ext] + STOP_ATR * atr)
    if v.entry == "MKT":
        entry = b1.c[m]
    else:
        best = None
        for j in range(max(i_ext + 2, 2), m + 1):
            for g in fvg_at(b1, j):
                if g.side != side:
                    continue
                if side > 0:
                    untouched = all(b1.lo[q] > g.top for q in range(j + 1, m + 1))
                    if untouched and (best is None or g.top > best):
                        best = g.top
                else:
                    untouched = all(b1.h[q] < g.bottom for q in range(j + 1, m + 1))
                    if untouched and (best is None or g.bottom < best):
                        best = g.bottom
        if best is None:
            counters["no_ltf_fvg"] = counters.get("no_ltf_fvg", 0) + 1
            return None
        entry = best
    risk = (entry - stop) * side
    reward = (target - entry) * side
    if risk <= 0 or reward <= 0:
        counters["bad_geometry"] = counters.get("bad_geometry", 0) + 1
        return None
    rr = reward / risk
    counters.setdefault("rr_all", []).append(rr)
    if rr < v.rr:
        counters["rr_cut"] = counters.get("rr_cut", 0) + 1
        return None
    hold_until = t_bos + HOLD_TF0 * b0.tf_ms
    pess = 0
    if v.entry == "MKT":
        entry_fill = entry * (1 + side * SLIP)
        fee_in = TAKER
        t_entry = t_bos
        how, ex_lvl, ex_fill, fee_out, t_exit, p2 = run_position(
            sym, side, entry, entry_fill, fee_in, stop, target, t_entry, hold_until, hourly,
            mins, fund, None)
        pess += p2
    else:
        # ожидание налива по часам
        i = int(np.searchsorted(hts, t_bos))
        expire = t_bos + ORDER_TF0 * b0.tf_ms
        filled = None
        while i < len(hourly) and int(hts[i]) < expire:
            ht = int(hts[i])
            h, lo = hourly[i, 2], hourly[i, 3]
            fill_hit = lo <= entry * (1 - FILL_EPS) if side > 0 else h >= entry * (1 + FILL_EPS)
            tgt_hit = h >= target if side > 0 else lo <= target
            if fill_hit or tgt_hit:
                seg = mins.hour(sym, ht)
                if seg is None:
                    if fill_hit:
                        filled = (ht, None)      # без минуток: налив, остаток часа — пессимистично
                        pess += 1
                    break
                for x in seg:
                    t_hit = x[2] >= target if side > 0 else x[3] <= target
                    f_hit = x[3] <= entry * (1 - FILL_EPS) if side > 0 \
                        else x[2] >= entry * (1 + FILL_EPS)
                    if t_hit and not f_hit:
                        break                   # цель раньше налива — снимаем
                    if f_hit:
                        rest = seg[seg[:, 0] > x[0]]
                        filled = (int(x[0]), rest)
                        break
                break
            i += 1
        if filled is None:
            counters["unfilled"] = counters.get("unfilled", 0) + 1
            counters["_busy_until"] = expire
            return None
        t_entry, rest = filled
        entry_fill = entry
        fee_in = MAKER
        if rest is None:
            # нет минуток часа налива: если в этом часе задет стоп — стоп
            h, lo = hourly[i, 2], hourly[i, 3]
            if (lo <= stop) if side > 0 else (h >= stop):
                how, ex_lvl = "stop", stop
                ex_fill = stop * (1 - side * SLIP)
                fee_out, t_exit = TAKER, int(hts[i]) + H_MS
            else:
                how, ex_lvl, ex_fill, fee_out, t_exit, p2 = run_position(
                    sym, side, entry, entry_fill, fee_in, stop, target, int(hts[i]) + H_MS,
                    hold_until, hourly, mins, fund, None)
                pess += p2
        else:
            how, ex_lvl, ex_fill, fee_out, t_exit, p2 = run_position(
                sym, side, entry, entry_fill, fee_in, stop, target, t_entry + 60_000,
                hold_until, hourly, mins, fund, rest)
            pess += p2
    risk_px = (entry - stop) * side
    fcost = funding_cost(fund, side, t_entry, t_exit)
    gross_r = side * (ex_lvl - entry) / risk_px
    pnl = side * (ex_fill - entry_fill) - fee_in * entry_fill - fee_out * ex_fill \
        - fcost * entry_fill
    net_r = pnl / risk_px
    return Trade(sym, v.key, side, t_bos, t_entry, t_exit, how, entry, stop, target,
                 float(gross_r), float(net_r), float(rr), pess)


# ---------------------------------------------------------------- прогон

_CTX: dict = {}


def context() -> dict:
    if _CTX:
        return _CTX
    daily = load_daily()
    have = {p.stem for p in (RES / "volbreak" / "h1").glob("*.npy")}
    uni, cov = universe(daily, have)
    fund = load_funding()
    _CTX.update(daily=daily, have=have, uni=uni, cov=cov, fund=fund)
    return _CTX


def run_variant(args):
    v, use_m1 = args
    ctx = context()
    mins = Minutes(use_m1)
    counters: dict = {}
    out = []
    t_from, t_to = ms(TRAIN[0]), ms(FROZEN[1])
    for sym in sorted(ctx["have"]):
        if sym not in ctx["daily"]:
            continue
        hourly = load_hourly(sym)
        if len(hourly) < 500:
            continue
        tr = simulate(sym, v, hourly, ctx["daily"][sym], ctx["uni"], ctx["fund"].get(sym),
                      mins, t_from, t_to, counters)
        out.extend(tr)
    rr_all = counters.pop("rr_all", [])
    counters["rr_ge3"] = int(sum(r >= 3 for r in rr_all))
    counters["rr_ge4"] = int(sum(r >= 4 for r in rr_all))
    counters["setups_after_bos"] = len(rr_all)
    need = {s: sorted(h) for s, h in mins.need.items()}
    return v.key, [t.__dict__ for t in out], counters, need


def stats(tr: pd.DataFrame) -> dict:
    if not len(tr):
        return dict(n=0, gross=np.nan, net=np.nan, sum=0.0, dd=0.0, win=np.nan, t=np.nan)
    t = tr.sort_values("exit_ts")
    cum = t["net_r"].cumsum().to_numpy()
    dd = float(np.max(np.maximum.accumulate(np.r_[0, cum]) - np.r_[0, cum]))
    sd = float(t["net_r"].std(ddof=1)) if len(t) > 1 else np.nan
    tstat = float(t["net_r"].mean() / (sd / math.sqrt(len(t)))) if sd and sd > 0 else np.nan
    return dict(n=len(t), gross=float(t["gross_r"].mean()), net=float(t["net_r"].mean()),
                sum=float(t["net_r"].sum()), dd=dd, win=float((t["net_r"] > 0).mean()), t=tstat)


def period(tr: pd.DataFrame, a: str, b: str) -> pd.DataFrame:
    return tr[(tr["entry_ts"] >= ms(a)) & (tr["entry_ts"] < ms(b))]


def cmd_grid(use_m1: bool) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ctx = context()
    print(f"вселенная: {len(ctx['uni'])} дней, покрытие часовыми {ctx['cov']:.3f}", flush=True)
    res, need_all, cnt = {}, {}, {}
    with ProcessPoolExecutor(max_workers=2) as ex:
        for key, trades, counters, need in ex.map(run_variant, [(v, use_m1) for v in grid()]):
            res[key] = trades
            cnt[key] = counters
            for s, hs in need.items():
                need_all.setdefault(s, set()).update(hs)
            df = pd.DataFrame(trades)
            st = stats(period(df, *TRAIN)) if len(df) else stats(df)
            print(f"  {key}: сделок {len(df)}, учёба n={st['n']} net={st['net']:.3f}", flush=True)
    tag = "m1" if use_m1 else "h"
    (OUT / f"trades_{tag}.json").write_text(json.dumps(res))
    (OUT / f"counters_{tag}.json").write_text(json.dumps(cnt, indent=1))
    (OUT / "m1_need.json").write_text(json.dumps({s: sorted(h) for s, h in need_all.items()}))
    print("часов для 1m:", sum(len(h) for h in need_all.values()), "монет:", len(need_all))


def cmd_m1() -> None:
    need = json.loads((OUT / "m1_need.json").read_text())
    m1_fetch(need)


def neighbors(v: Variant) -> list[Variant]:
    order = ["D4", "D1", "H1"]
    i = order.index(v.pair)
    out = [Variant(order[j], v.rr, v.entry) for j in (i - 1, i + 1) if 0 <= j < 3]
    out.append(Variant(v.pair, 7 - v.rr, v.entry))
    out.append(Variant(v.pair, v.rr, "MKT" if v.entry == "LIM" else "LIM"))
    return out


def fmt(st: dict) -> str:
    return (f"n={st['n']:>4} gross={st['gross']:+.3f} net={st['net']:+.3f} ΣR={st['sum']:+7.1f} "
            f"DD={st['dd']:6.1f} win={st['win']:.0%} t={st['t']:+.2f}")


def cmd_report(tag: str) -> None:
    res = json.loads((OUT / f"trades_{tag}.json").read_text())
    cnt = json.loads((OUT / f"counters_{tag}.json").read_text())
    dfs = {k: pd.DataFrame(v) for k, v in res.items()}
    print(f"== Сетка ({tag}): учёба / проверка")
    rows = []
    for v in grid():
        df = dfs[v.key]
        tr = stats(period(df, *TRAIN)) if len(df) else stats(df)
        va = stats(period(df, *VALID)) if len(df) else stats(df)
        rows.append((v, tr, va))
        print(f"{v.key:14} учёба {fmt(tr)}\n{'':14} пров. {fmt(va)}")
    elig = [(v, tr) for v, tr, _ in rows if tr["n"] >= 100]
    thr = 100
    if not elig:
        elig = [(v, tr) for v, tr, _ in rows if tr["n"] >= 50]
        thr = 50
    if not elig:
        print("нет вариантов с n_учёба ≥ 50 — выбирать нечего")
        return
    best = max(elig, key=lambda x: x[1]["net"])[0]
    print(f"\n== Выбран (только учёба, n ≥ {thr}): {best.key}")
    df = dfs[best.key]
    va = period(df, *VALID)
    st_va = stats(va)
    print("учёба     ", fmt(stats(period(df, *TRAIN))))
    print("проверка  ", fmt(st_va))
    halves_ok = True
    for name, a, b in HALVES:
        st = stats(period(df, a, b))
        ok = st["n"] >= 10 and st["net"] > 0
        halves_ok &= ok
        print(f"  {name:8}", fmt(st), "ок" if ok else "НЕТ")
    nb_ok = True
    for nb in neighbors(best):
        st = stats(period(dfs[nb.key], *VALID))
        ok = st["n"] > 0 and st["net"] > 0
        nb_ok &= ok
        print(f"  сосед {nb.key:14}", fmt(st), "ок" if ok else "НЕТ")
    top5 = va.sort_values("net_r", ascending=False).iloc[5:]["net_r"].sum() if len(va) else 0.0
    c1 = st_va["n"] >= 100 and st_va["net"] > 0 and halves_ok
    c3 = top5 > 0
    c4 = st_va["t"] >= 2.64 if np.isfinite(st_va["t"]) else False
    print(f"без топ-5 сделок проверки ΣR={top5:+.1f}")
    print(f"критерии: 1 (n≥100, net>0, все полугодия) {'да' if c1 else 'нет'}; "
          f"2 (соседи) {'да' if nb_ok else 'нет'}; 3 (без топ-5) {'да' if c3 else 'нет'}; "
          f"4 (t≥2.64) {'да' if c4 else 'нет'}")
    print("ВЕРДИКТ:", "КАНДИДАТ" if (c1 and nb_ok and c3 and c4) else "не кандидат")
    fr = period(df, *FROZEN)
    print("заморозка (один раз):", fmt(stats(fr)))
    pess = int(df["pess"].sum()) if len(df) else 0
    print(f"пессимистичных разборов без 1m у выбранного: {pess} из {len(df)} сделок")
    print("\n== Воронка (все годы):")
    for v in grid():
        c = cnt[v.key]
        print(f"{v.key:14} реакций {c.get('reactions', 0)}, без цели {c.get('no_target', 0)}, "
              f"без BOS {c.get('no_bos', 0)}, нет FVG TF1 {c.get('no_ltf_fvg', 0)}, "
              f"после BOS {c.get('setups_after_bos', 0)} (RR≥3: {c.get('rr_ge3', 0)}, "
              f"RR≥4: {c.get('rr_ge4', 0)}), отсечено RR {c.get('rr_cut', 0)}, "
              f"не налито {c.get('unfilled', 0)}")


# ---------------------------------------------------------------- самотест

def selftest() -> None:
    # синтетика: FVG на j=2, свинг-хай на i=3 подтверждается на 5
    ts = np.arange(8, dtype=np.int64) * H_MS
    o = np.array([10, 11, 13, 14, 13, 12, 12.5, 15.0])
    h = np.array([11, 12, 14, 16, 14, 13, 13.5, 15.5])
    lo = np.array([9.5, 10.5, 12.5, 13.5, 12, 11, 12, 12.5])
    c = np.array([11, 12, 13.5, 14, 12.5, 12, 13, 15.2])
    b = Bars(ts, o, h, lo, c, np.full(8, 1.0), H_MS)
    f = fvg_at(b, 2)
    assert f and f[0].side == 1 and f[0].bottom == 11 and f[0].top == 12.5, f
    assert not fvg_at(b, 1)
    sh, sl = swings(b)
    assert sh[3] and not sh[2] and not sh[4], sh
    lk = last_known(sh)
    assert lk[4] == -1 and lk[5] == 3, lk
    # BOS — закрытие выше свинга, тень не считается: бар 6 high 13.5 < 16; бар 7 close 15.2 < 16
    assert not any(c[m] > h[lk[m]] for m in range(5, 8) if lk[m] >= 0)
    print("синтетика: FVG, свинг, подтверждение на i+2, BOS по закрытию — ок")
    # реальные примеры BTC: первые три сделки варианта D4_RR3_LIM
    ctx = context()
    hourly = load_hourly("BTCUSDT")
    mins = Minutes(False)
    cnt: dict = {}
    tr = simulate("BTCUSDT", Variant("D4", 3, "LIM"), hourly, ctx["daily"]["BTCUSDT"],
                  ctx["uni"], ctx["fund"].get("BTCUSDT"), mins, ms(TRAIN[0]), ms(FROZEN[1]), cnt)
    funnel = {k: v for k, v in cnt.items() if k != "rr_all"}
    print(f"BTC D4_RR3_LIM: сделок {len(tr)}, воронка {funnel}")
    for t in tr[:3]:
        d = pd.to_datetime
        side = "лонг" if t.side > 0 else "шорт"
        print(f"  {side} BOS {d(t.bos_ts, unit='ms')} вход {d(t.entry_ts, unit='ms')} "
              f"@{t.entry:.1f} стоп {t.stop:.1f} цель {t.target:.1f} RR {t.rr:.2f} → {t.how} "
              f"gross {t.gross_r:+.2f} net {t.net_r:+.2f}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "grid"
    if cmd == "selftest":
        selftest()
    elif cmd == "grid":
        cmd_grid("--m1" in sys.argv)
    elif cmd == "m1":
        cmd_m1()
    elif cmd == "report":
        cmd_report("m1" if "--m1" in sys.argv else "h")
    else:
        sys.exit(__doc__)
