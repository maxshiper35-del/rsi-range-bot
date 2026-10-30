"""Замер «пробой волатильности» (docs/measurements/2026-10-31-vol-breakout.md).

Сжатие диапазона на 4h/1d → OCO стоп-заявки на границах → выход трейлом / целью / по времени.
Вселенная — топ-30 по обороту без заглядывания в будущее (дневные свечи с делистнутыми),
разбор внутри бара — по часам, вход уточняется по 1m. Прод это не импортирует.

  nice -n 15 .venv/bin/python -m scripts.research_vol_breakout calibrate   # s_in по 1m
  nice -n 15 .venv/bin/python -m scripts.research_vol_breakout grid        # 48 вариантов
  nice -n 15 .venv/bin/python -m scripts.research_vol_breakout refine KEY  # выбранный по 1m
"""
from __future__ import annotations

import json
import math
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(os.environ.get("DATA_ROOT", "/Users/mac/bot ctypto/data"))
RES = DATA / "cache_research"
OUT = RES / "volbreak"
H_MS = 3_600_000
D_MS = 24 * H_MS
TF_MS = {"4h": 4 * H_MS, "1d": D_MS}

TOP = 30                 # вселенная: топ-30 по медиане оборота за 30 дней до дня
L_RANK = 200             # окно ранга ширины
M_VALID = 5              # сетап живёт M баров
ATR_N = 14
TRAIL_K = 3.0
TARGET_R = 2.0
TIME_BARS = 10
MAX_BARS = 120
FEE = 0.00055            # тейкер на сторону
SLIP_EXIT = 0.0002
SLIP_BASE = 0.0002
FUND_MISSING = 0.0001    # за 8 ч, с обеих сторон

TRAIN = ("2021-11-01", "2024-01-01")
VALID = ("2024-01-01", "2026-08-01")
FROZEN = ("2026-08-01", "2026-10-30")
HALVES = [("2024H1", "2024-01-01", "2024-07-01"), ("2024H2", "2024-07-01", "2025-01-01"),
          ("2025H1", "2025-01-01", "2025-07-01"), ("2025H2", "2025-07-01", "2026-01-01"),
          ("2026H1", "2026-01-01", "2026-08-01")]


def ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)


# ---------------------------------------------------------------- данные

def load_daily() -> dict:
    out = {}
    for f in sorted((RES / "xsmom" / "klines").glob("*.csv")):
        df = pd.read_csv(f)
        if len(df) < 2:
            continue
        out[f.stem] = df[["ts", "open", "high", "low", "close", "turnover"]].to_numpy(float)
    return out


def universe(daily: dict, have: set) -> tuple[dict, float]:
    """День (ms 00:00) → множество топ-30 по медиане оборота за 30 предыдущих дней.
    Покрытие — доля мест топ-30 (по всем монетам), занятых монетами с часовыми данными."""
    ser = {s: pd.Series(a[:, 5], index=a[:, 0].astype(np.int64)) for s, a in daily.items()}
    tv = pd.DataFrame(ser).sort_index()
    med = tv.rolling(30, min_periods=20).median().shift(1)
    uni, slots, covered = {}, 0, 0
    for day, row in med.iterrows():
        top = row.dropna().nlargest(TOP)
        if len(top) < TOP:
            continue
        names = list(top.index)
        slots += len(names)
        ok = {s for s in names if s in have}
        covered += len(ok)
        uni[int(day)] = ok
    return uni, (covered / slots if slots else 0.0)


H1_DIR = OUT / "h1"
PAD_BEFORE = 45 * D_MS      # окно сжатия на 4h: L=200 баров ≈ 33 дня + N=40 баров
PAD_AFTER = 125 * D_MS      # сделка 1d живёт до 120 баров


def load_hourly(sym: str) -> np.ndarray:
    """Часовые свечи: свой полный кэш (вся жизнь монеты во вселенной), иначе — reversal.
    У reversal/hourly полная история лишь у 21 монеты из 285: остальное — окна вокруг
    экстремальных событий того замера (найдено 30.10), торговать по ним нельзя."""
    own = H1_DIR / f"{sym}.npy"
    if own.exists():
        return np.load(own)
    return np.load(RES / "reversal" / "hourly" / f"{sym}.npy")


def needed_hours(uni: dict, daily: dict) -> dict:
    """Монета → [начало, конец) часовых данных: дни во вселенной ± запас, внутри жизни монеты."""
    days: dict = {}
    for day, syms in uni.items():
        for s in syms:
            days.setdefault(s, []).append(day)
    out = {}
    lo_all, hi_all = ms("2021-09-15"), ms("2026-10-30")
    for s, ds in days.items():
        a = daily[s]
        life0, life1 = int(a[0, 0]), int(a[-1, 0]) + D_MS
        s0 = max(min(ds) - PAD_BEFORE, life0, lo_all)
        s1 = min(max(ds) + PAD_AFTER, life1, hi_all)
        if s1 > s0:
            out[s] = (s0, s1)
    return out


def h1_fetch(dry: bool = False) -> None:
    """Докачать часовые свечи вселенной вперёд окнами по 1000 ч ВНУТРИ жизни монеты
    (пагинация «назад от сейчас» у делистнутых обрезана — см. research_xsmom_fetch)."""
    daily = load_daily()
    uni, _ = universe(daily, set(daily))
    need = needed_hours(uni, daily)
    H1_DIR.mkdir(parents=True, exist_ok=True)
    todo = {s: iv for s, iv in need.items() if not (H1_DIR / f"{s}.npy").exists()}
    reqs = sum(math.ceil((b - a) / (1000 * H_MS)) for a, b in todo.values())
    print(f"монет во вселенной {len(need)}, к загрузке {len(todo)}, запросов ≈{reqs}", flush=True)
    if dry:
        return
    done = 0
    for s, (a, b) in sorted(todo.items()):
        rows: dict = {}
        w0 = a
        while w0 < b:
            w1 = min(w0 + 1000 * H_MS - 1, b - 1)
            got = _kline(s, "60", w0, w1)
            for k in got:
                rows[int(k[0])] = [float(x) for x in k[:6]]
            w0 = w1 + 1
            done += 1
        arr = np.array([rows[t] for t in sorted(rows)]) if rows else np.zeros((0, 6))
        np.save(H1_DIR / f"{s}.npy", arr)
        print(f"  h1 {s}: {len(arr)} ч, запросов {done}/{reqs}", flush=True)


def _kline(sym: str, interval: str, s: int, e: int) -> list:
    import time
    import urllib.error
    import urllib.request
    url = (f"https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}"
           f"&interval={interval}&start={s}&end={e}&limit=1000")
    for attempt in range(8):
        time.sleep(0.34)                                  # ≤ 3 запр/с
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                body = json.loads(r.read())
        except urllib.error.HTTPError as err:
            if err.code in (429, 403, 502, 503, 504):
                time.sleep(min(60, 5 * 2 ** attempt))
                continue
            return []
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(min(60, 5 * 2 ** attempt))
            continue
        code = body.get("retCode")
        if code == 0:
            return body["result"]["list"]
        if code in (10006, 10018):
            time.sleep(min(60, 5 * 2 ** attempt))
            continue
        return []
    return []


def slot_coverage(uni: dict) -> float:
    """Доля мест топ-30 (день, монета), где у монеты есть ≥20 часовых свечей этого дня."""
    total = hit = 0
    cache: dict = {}
    for day, syms in uni.items():
        if day < ms(TRAIN[0]) or day >= ms(FROZEN[1]):
            continue
        for s in syms:
            total += 1
            if s not in cache:
                try:
                    cache[s] = load_hourly(s)[:, 0].astype(np.int64)
                except FileNotFoundError:
                    cache[s] = np.zeros(0, np.int64)
            t = cache[s]
            n = np.searchsorted(t, day + D_MS) - np.searchsorted(t, day)
            hit += n >= 20
    return hit / total if total else 0.0


def load_funding() -> dict:
    out = {}
    for f in (RES / "xsmom" / "funding").glob("*.csv"):
        df = pd.read_csv(f)
        if len(df):
            out[f.stem] = (df["ts"].to_numpy(np.int64), df["rate"].to_numpy(float))
    return out


@dataclass
class Bars:
    ts: np.ndarray       # открытие бара, ms
    o: np.ndarray
    h: np.ndarray
    lo: np.ndarray
    c: np.ndarray
    atr: np.ndarray


def make_bars(sym: str, tf: str, hourly: np.ndarray, daily: dict) -> Bars:
    if tf == "1d":
        a = daily[sym]
        ts, o, h, lo, c = a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4]
    else:
        g = (hourly[:, 0].astype(np.int64) // TF_MS[tf]) * TF_MS[tf]
        df = pd.DataFrame({"g": g, "o": hourly[:, 1], "h": hourly[:, 2], "lo": hourly[:, 3],
                           "c": hourly[:, 4]})
        agg = df.groupby("g").agg(o=("o", "first"), h=("h", "max"), lo=("lo", "min"),
                                  c=("c", "last"))
        ts = agg.index.to_numpy(np.int64)
        o, h, lo, c = (agg[k].to_numpy(float) for k in ("o", "h", "lo", "c"))
    pc = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(np.vstack([h - lo, np.abs(h - pc), np.abs(lo - pc)]), axis=0)
    atr = pd.Series(tr).rolling(ATR_N).mean().to_numpy()
    return Bars(ts, o, h, lo, c, atr)


def compression(b: Bars, n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """→ (верх N, низ N, ранг ширины среди L последних; nan там, где истории мало)."""
    hi = pd.Series(b.h).rolling(n).max().to_numpy()
    lo = pd.Series(b.lo).rolling(n).min().to_numpy()
    w = (hi - lo) / b.c
    rank = np.full(len(w), np.nan)
    if len(w) >= L_RANK:
        win = np.lib.stride_tricks.sliding_window_view(w, L_RANK)
        last = win[:, -1:]
        r = np.where(np.isnan(win).any(axis=1), np.nan, (win <= last).mean(axis=1))
        rank[L_RANK - 1:] = r
    return hi, lo, rank


# ---------------------------------------------------------------- симуляция

@dataclass
class Variant:
    tf: str
    n: int
    p: int
    stop: str     # S1 граница / S2 2×ATR
    exit: str     # X1 трейл / X2 цель / X3 время

    @property
    def key(self) -> str:
        return f"{self.tf}_N{self.n}_p{self.p}_{self.stop}_{self.exit}"


def grid() -> list[Variant]:
    return [Variant(tf, n, p, s, x) for tf in ("4h", "1d") for n in (20, 40) for p in (10, 20)
            for s in ("S1", "S2") for x in ("X1", "X2", "X3")]


def funding_cost(fund: tuple | None, side: int, t0: int, t1: int) -> float:
    if fund is None:
        return FUND_MISSING * max(0, (t1 - t0) // (8 * H_MS))
    ts, rate = fund
    i0, i1 = np.searchsorted(ts, t0, "right"), np.searchsorted(ts, t1, "right")
    return float(side * rate[i0:i1].sum())


def simulate(sym: str, v: Variant, hourly: np.ndarray, bars: Bars, uni: dict, fund,
             s_in: float, t_from: int, t_to: int, minute=None) -> tuple[list, list]:
    """Сделки варианта v по монете sym, вход в [t_from, t_to).
    minute(sym, hour_ts, H, L, stop_fn) → уточнение часа входа по 1m или None (нет данных).
    → (сделки, часы входа, которым нужна 1m: (hour_ts, H, L))."""
    hts = hourly[:, 0].astype(np.int64)
    ho, hh, hl, hc = hourly[:, 1], hourly[:, 2], hourly[:, 3], hourly[:, 4]
    tf_ms = TF_MS[v.tf]
    hi, lo, rank = compression(bars, v.n)
    close_ts = bars.ts + tf_ms
    cand = np.where(rank <= v.p / 100.0)[0]
    trades, need = [], []
    free_from = -1
    h_end = hts[-1] + H_MS if len(hts) else 0
    for t in cand:
        ct = int(close_ts[t])
        if ct < free_from or ct < t_from or ct >= t_to or ct < hts[0]:
            continue
        if ct + M_VALID * tf_ms > h_end:
            break
        day = ct - ct % D_MS
        if sym not in uni.get(day, ()):
            continue
        H, Lw = float(hi[t]), float(lo[t])
        i0 = int(np.searchsorted(hts, ct))
        i1 = int(np.searchsorted(hts, ct + M_VALID * tf_ms))
        if i1 <= i0:
            continue
        up = np.flatnonzero(hh[i0:i1] >= H)
        dn = np.flatnonzero(hl[i0:i1] <= Lw)
        fu = up[0] if len(up) else None
        fd = dn[0] if len(dn) else None
        if fu is None and fd is None:
            free_from = ct + M_VALID * tf_ms
            continue
        k = min(x for x in (fu, fd) if x is not None)
        hix = i0 + k
        hour_ts = int(hts[hix])
        both = fu == fd
        res = None
        if minute is not None:
            res = minute(sym, hour_ts, H, Lw)
            if res is None:
                need.append((hour_ts, H, Lw))
        if (both and res is None) or (res is not None and res.get("skip")):
            free_from = ct + M_VALID * tf_ms   # обе стороны в одном часе (минуте) — пропуск
            continue
        side = (1 if fu == k else -1) if res is None else res["side"]
        level = H if side > 0 else Lw
        if res is None:
            entry_lvl = max(level, ho[hix]) if side > 0 else min(level, ho[hix])
        else:
            entry_lvl = res["entry_lvl"]
        entry_fill = entry_lvl * (1 + side * s_in)
        if v.stop == "S1":
            stop = Lw if side > 0 else H
        else:
            a = bars.atr[t]
            if not np.isfinite(a) or a <= 0:
                free_from = ct + M_VALID * tf_ms
                continue
            stop = entry_fill - side * 2 * a
        r_lvl = abs(entry_lvl - stop)
        r_fill = abs(entry_fill - stop)
        if r_lvl <= 0 or r_fill <= 0 or side * (entry_lvl - stop) <= 0:
            free_from = ct + M_VALID * tf_ms
            continue
        target = entry_fill + side * TARGET_R * r_fill
        b0 = int(np.searchsorted(bars.ts, hour_ts, "right") - 1)
        exit_lvl, exit_ts, how = None, None, None
        # час входа
        if res is not None:
            if res["stopped"](stop):
                exit_lvl, exit_ts, how = stop, hour_ts + H_MS, "stop0"
        elif (side > 0 and hl[hix] <= stop) or (side < 0 and hh[hix] >= stop):
            exit_lvl, exit_ts, how = stop, hour_ts + H_MS, "stop0"
        cur_stop = stop
        best_c = None
        if exit_lvl is None:
            b = b0
            pos = hix + 1
            while True:
                bar_end = int(bars.ts[b] + tf_ms) if b < len(bars.ts) else None
                if bar_end is None:
                    break
                j1 = int(np.searchsorted(hts, bar_end))
                if pos < j1:
                    seg_l, seg_h, seg_o = hl[pos:j1], hh[pos:j1], ho[pos:j1]
                    if side > 0:
                        hs = seg_l <= cur_stop
                        ht = seg_h >= target if v.exit == "X2" else np.zeros(len(seg_l), bool)
                    else:
                        hs = seg_h >= cur_stop
                        ht = seg_l <= target if v.exit == "X2" else np.zeros(len(seg_l), bool)
                    hit = np.flatnonzero(hs | ht)
                    if len(hit):
                        q = hit[0]
                        hx = pos + q
                        if hs[q]:
                            exit_lvl = (min(cur_stop, seg_o[q]) if side > 0
                                        else max(cur_stop, seg_o[q]))
                            how = "stop"
                        else:
                            exit_lvl = max(target, seg_o[q]) if side > 0 else min(target, seg_o[q])
                            how = "target"
                        exit_ts = int(hts[hx]) + H_MS
                        break
                    pos = j1
                if j1 >= len(hts) or j1 == 0:
                    exit_lvl = float(hc[min(j1, len(hts)) - 1])
                    exit_ts, how = int(hts[-1]) + H_MS, "end"
                    break
                # закрытие бара b
                bc = float(hc[j1 - 1])
                if v.exit == "X3" and b - b0 >= TIME_BARS:
                    exit_lvl, exit_ts, how = bc, bar_end, "time"
                    break
                if b - b0 >= MAX_BARS:
                    exit_lvl, exit_ts, how = bc, bar_end, "max"
                    break
                if v.exit == "X1":
                    if best_c is None:
                        best_c = bc
                    else:
                        best_c = max(best_c, bc) if side > 0 else min(best_c, bc)
                    a = bars.atr[b] if b < len(bars.atr) else np.nan
                    if np.isfinite(a):
                        trail = best_c - side * TRAIL_K * a
                        cur_stop = max(cur_stop, trail) if side > 0 else min(cur_stop, trail)
                b += 1
                if b >= len(bars.ts):
                    exit_lvl, exit_ts, how = bc, bar_end, "end"
                    break
        slip = 0.0 if how == "target" else SLIP_EXIT
        exit_fill = exit_lvl * (1 - side * slip)
        fcost = funding_cost(fund, side, hour_ts, exit_ts)
        net_ret = side * (exit_fill - entry_fill) / entry_fill - 2 * FEE - fcost
        trades.append(dict(sym=sym, side=side, entry_ts=hour_ts, exit_ts=exit_ts, how=how,
                           lvl=level, entry_lvl=entry_lvl, entry=entry_fill, exit=exit_fill,
                           stop=stop,
                           gross_r=side * (exit_lvl - entry_lvl) / r_lvl,
                           net_r=net_ret / (r_fill / entry_fill),
                           r_pct=r_fill / entry_fill, fund=fcost))
        free_from = exit_ts
    return trades, need


# ---------------------------------------------------------------- метрики

def stats(tr: pd.DataFrame) -> dict:
    if not len(tr):
        return dict(n=0, gross=np.nan, net=np.nan, sum=0.0, dd=0.0, win=np.nan)
    t = tr.sort_values("exit_ts")
    cum = t["net_r"].cumsum().to_numpy()
    dd = float(np.max(np.maximum.accumulate(np.r_[0, cum]) - np.r_[0, cum]))
    return dict(n=len(t), gross=float(t["gross_r"].mean()), net=float(t["net_r"].mean()),
                sum=float(t["net_r"].sum()), dd=dd, win=float((t["net_r"] > 0).mean()))


def period(tr: pd.DataFrame, a: str, b: str) -> pd.DataFrame:
    return tr[(tr["entry_ts"] >= ms(a)) & (tr["entry_ts"] < ms(b))]


# ---------------------------------------------------------------- общий контекст

_CTX: dict = {}


def context() -> dict:
    if _CTX:
        return _CTX
    daily = load_daily()
    have = {p.stem for p in (RES / "reversal" / "hourly").glob("*.npy")}
    have |= {p.stem for p in H1_DIR.glob("*.npy")} if H1_DIR.exists() else set()
    uni, cov = universe(daily, have)
    fund = load_funding()
    _CTX.update(daily=daily, have=have, uni=uni, cov=cov, fund=fund)
    return _CTX


def run_variant(args) -> tuple[str, list]:
    v, s_in, t_from, t_to = args
    ctx = context()
    out = []
    syms = sorted(s for s in ctx["have"] if s in ctx["daily"])
    for sym in syms:
        hourly = load_hourly(sym)
        if len(hourly) < 500:
            continue
        bars = make_bars(sym, v.tf, hourly, ctx["daily"])
        tr, _ = simulate(sym, v, hourly, bars, ctx["uni"], ctx["fund"].get(sym), s_in, t_from, t_to)
        out.extend(tr)
    return v.key, out


# ---------------------------------------------------------------- 1m

# Свой маленький кэш часовых окон: общий data/cache_tf/m1 держат под .lock соседние замеры,
# которые качают годы минуток целиком, — короткие окна входов ждали бы их минутами.
M1_URL = ("https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}&interval=1"
          "&start={s}&end={e}&limit=1000")
M1_DIR = OUT / "m1"
M1_GAP_S = 0.35            # ≈3 запр/с, один поток


def _m1_paths(sym: str) -> tuple[Path, Path]:
    return M1_DIR / f"{sym}.npy", M1_DIR / f"{sym}.json"


def m1_load(sym: str) -> tuple[np.ndarray, set]:
    npy, js = _m1_paths(sym)
    if not npy.exists():
        return np.zeros((0, 5)), set()
    return np.load(npy), set(json.loads(js.read_text()))


def _m1_get(sym: str, s: int, e: int) -> list:
    import time
    import urllib.error
    import urllib.request
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
        return []                      # нет такого символа (делистинг) и т. п.
    return []


def m1_ensure(need: dict) -> None:
    """need: {sym: [hour_ts, ...]} — докачать недостающие часы (соседние часы — одним запросом)."""
    M1_DIR.mkdir(parents=True, exist_ok=True)
    total = sum(len(v) for v in need.values())
    done = 0
    for sym, hours in sorted(need.items()):
        arr, have = m1_load(sym)
        todo = sorted(set(int(h) for h in hours) - have)
        rows = [arr] if len(arr) else []
        i = 0
        while i < len(todo):
            j = i
            while j + 1 < len(todo) and todo[j + 1] - todo[i] < 16 * H_MS:
                j += 1
            s, e = todo[i], todo[j] + H_MS - 1
            got = _m1_get(sym, s, e)
            if got:
                a = np.array([[float(x) for x in g[:5]] for g in got])
                rows.append(a)
            have.update(todo[i:j + 1])         # пустой ответ тоже помечаем: повторно не просим
            done += j - i + 1
            i = j + 1
        if rows:
            a = np.concatenate(rows)
            a = a[np.argsort(a[:, 0])]
            _, keep = np.unique(a[:, 0], return_index=True)
            a = a[keep]
        else:
            a = np.zeros((0, 5))
        npy, js = _m1_paths(sym)
        np.save(npy, a)
        js.write_text(json.dumps(sorted(have)))
        print(f"  1m {sym}: {len(todo)} ч, всего {done}/{total}", flush=True)


def m1_name(sym: str) -> str:
    return sym[:-4] + "/USDT:USDT"


def calibrate() -> None:
    """s_in по 1m: 300 случайных входов учёбы на ТФ (N=20, p=20 — самый частый сетап)."""
    OUT.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(31)
    res = {}
    for tf in ("4h", "1d"):
        v = Variant(tf, 20, 20, "S1", "X3")
        _, trades = run_variant((v, SLIP_BASE, ms(TRAIN[0]), ms(TRAIN[1])))
        df = pd.DataFrame(trades)
        pick = df.iloc[rng.choice(len(df), size=min(300, len(df)), replace=False)]
        need: dict = {}
        for _, r in pick.iterrows():
            need.setdefault(r["sym"], []).append(int(r["entry_ts"]))
        m1_ensure(need)
        gaps, exc = [], []
        for _, r in pick.iterrows():
            m, _ = m1_load(r["sym"])
            if not len(m):
                continue
            seg = m[(m[:, 0] >= r["entry_ts"]) & (m[:, 0] < r["entry_ts"] + H_MS)]
            if not len(seg):
                continue
            side, lvl, base = int(r["side"]), float(r["lvl"]), float(r["entry_lvl"])
            if side > 0:
                k = np.flatnonzero(seg[:, 2] >= lvl)
            else:
                k = np.flatnonzero(seg[:, 3] <= lvl)
            if not len(k):
                continue
            mo, mh, ml = seg[k[0], 1], seg[k[0], 2], seg[k[0], 3]
            fill = max(lvl, mo) if side > 0 else min(lvl, mo)
            # разрыв сверх часовой модели (она уже берёт max(уровень, open часа))
            gaps.append(side * (fill - base) / base)
            exc.append((mh - lvl) / lvl if side > 0 else (lvl - ml) / lvl)
        g = float(np.mean(gaps)) if gaps else 0.0
        s_in = SLIP_BASE + math.ceil(max(g, 0.0) * 1e4) / 1e4
        res[tf] = dict(n=len(gaps), mean_gap=g, median_exc=float(np.median(exc)) if exc else None,
                       mean_exc=float(np.mean(exc)) if exc else None, s_in=s_in)
        print(tf, res[tf], flush=True)
    (OUT / "calibration.json").write_text(json.dumps(res, indent=1))


def run_grid() -> None:
    cal = json.loads((OUT / "calibration.json").read_text())
    ctx = context()
    print(f"места топ-30 с часовыми свечами этого дня: {slot_coverage(ctx['uni']):.3f}", flush=True)
    jobs = [(v, cal[v.tf]["s_in"], ms(TRAIN[0]), ms(FROZEN[0])) for v in grid()]
    rows = []
    with ProcessPoolExecutor(max_workers=2) as ex:
        for key, trades in ex.map(run_variant, jobs):
            df = pd.DataFrame(trades)
            if len(df):
                df = df[df["entry_ts"] < ms(FROZEN[0])]     # заморозку сетка не видит
            df.to_csv(OUT / f"trades_{key}.csv", index=False)
            tr = period(df, *TRAIN) if len(df) else df
            va = period(df, *VALID) if len(df) else df
            st, sv = stats(tr), stats(va)
            rows.append(dict(key=key, tr_n=st["n"], tr_gross=st["gross"], tr_net=st["net"],
                             tr_sum=st["sum"], va_n=sv["n"], va_net=sv["net"], va_sum=sv["sum"]))
            print(key, f"учёба n={st['n']} net={st['net']:.3f}", flush=True)
    pd.DataFrame(rows).to_csv(OUT / "grid_summary.csv", index=False)


def parse_key(key: str) -> Variant:
    tf, n, p, s, x = key.split("_")
    return Variant(tf, int(n[1:]), int(p[1:]), s, x)


def select() -> str:
    """Правило выбора — только учёба: лучший net R/сделку при n ≥ 300, затем больший ΣR."""
    g = pd.read_csv(OUT / "grid_summary.csv")
    ok = g[g["tr_n"] >= 300].sort_values(["tr_net", "tr_sum"], ascending=False)
    return str(ok.iloc[0]["key"])


def neighbors(v: Variant) -> list[str]:
    other_n = 40 if v.n == 20 else 20
    other_p = 20 if v.p == 10 else 10
    return [Variant(v.tf, other_n, v.p, v.stop, v.exit).key,
            Variant(v.tf, v.n, other_p, v.stop, v.exit).key]


def describe(df: pd.DataFrame, label: str) -> list[str]:
    rows = [f"{label}"]
    for name, (a, b) in (("учёба", TRAIN), ("проверка", VALID), ("заморозка", FROZEN)):
        s = stats(period(df, a, b))
        rows.append(f"  {name:9s} n={s['n']:5d} gross={s['gross']:+.3f} net={s['net']:+.3f} "
                    f"ΣR={s['sum']:+8.1f} DD={s['dd']:6.1f} win={s['win']:.2f}")
    for name, a, b in HALVES:
        s = stats(period(df, a, b))
        rows.append(f"    {name} n={s['n']:4d} net={s['net']:+.3f} ΣR={s['sum']:+7.1f}")
    return rows


def report() -> None:
    g = pd.read_csv(OUT / "grid_summary.csv").sort_values("tr_net", ascending=False)
    print(g.to_string(index=False, float_format=lambda x: f"{x:+.3f}"))
    key = select()
    v = parse_key(key)
    df = pd.read_csv(OUT / f"trades_{key}.csv")
    print("\nвыбран на учёбе:", key)
    print("\n".join(describe(df, key)))
    va = period(df, *VALID)
    halves_ok = all(stats(period(df, a, b))["net"] > 0 for _, a, b in HALVES)
    top5 = va.sort_values("net_r", ascending=False).iloc[5:]["net_r"].sum()
    print(f"\nкритерий 1: net>0 в целом {stats(va)['net'] > 0}, во всех полугодиях {halves_ok}")
    for nk in neighbors(v):
        nd = pd.read_csv(OUT / f"trades_{nk}.csv")
        print(f"критерий 2: сосед {nk} проверка net={stats(period(nd, *VALID))['net']:+.3f}")
    print(f"критерий 3: n проверки {len(va)} (нужно ≥150)")
    print(f"критерий 4: ΣR проверки без топ-5 = {top5:+.1f}")
    print(f"стресс s_in+0.05%: net проверки ≈ {(va['net_r'] - 0.0005 / va['r_pct']).mean():+.3f}")


def refine(key: str) -> None:
    """Выбранный вариант с часом входа по 1m: какая сторона первой, цена в минуту
    срабатывания, стоп внутри часа после входа. Недостающие минуты докачиваются (1 поток)."""
    cal = json.loads((OUT / "calibration.json").read_text())
    v = parse_key(key)
    ctx = context()
    mcache: dict = {}

    def mload(sym: str) -> tuple[np.ndarray, set]:
        if sym not in mcache:
            mcache[sym] = m1_load(sym)
        return mcache[sym]

    need: dict = {}
    stat = {"m1": 0, "fallback": 0, "skip": 0}

    def resolver(sym, hour_ts, H, L):
        m, have = mload(sym)
        if hour_ts not in have:
            need.setdefault(sym, []).append(hour_ts)
            return None
        i0, i1 = np.searchsorted(m[:, 0], hour_ts), np.searchsorted(m[:, 0], hour_ts + H_MS)
        seg = m[i0:i1]
        if len(seg) < 50:
            stat["fallback"] += 1              # биржа минуток не дала (делистинг) — часовая модель
            return None
        up = np.flatnonzero(seg[:, 2] >= H)
        dn = np.flatnonzero(seg[:, 3] <= L)
        fu = up[0] if len(up) else None
        fd = dn[0] if len(dn) else None
        if fu is None and fd is None:
            stat["fallback"] += 1            # минутки не подтвердили касание — часовая модель
            return None
        if fu is not None and fd is not None and fu == fd:
            stat["skip"] += 1
            return {"skip": True}
        stat["m1"] += 1
        side = 1 if (fd is None or (fu is not None and fu < fd)) else -1
        k = fu if side > 0 else fd
        lvl = H if side > 0 else L
        entry_lvl = max(lvl, seg[k, 1]) if side > 0 else min(lvl, seg[k, 1])
        tail = seg[k:]

        def stopped(stop, tail=tail, side=side):
            if side > 0:
                return bool((tail[:, 3] <= stop).any())
            return bool((tail[:, 2] >= stop).any())
        return {"side": side, "entry_lvl": float(entry_lvl), "stopped": stopped}

    syms = sorted(s for s in ctx["have"] if s in ctx["daily"])
    for it in range(5):
        need.clear()
        stat.update(m1=0, fallback=0, skip=0)
        out = []
        for sym in syms:
            hourly = load_hourly(sym)
            if len(hourly) < 500:
                continue
            bars = make_bars(sym, v.tf, hourly, ctx["daily"])
            tr, _ = simulate(sym, v, hourly, bars, ctx["uni"], ctx["fund"].get(sym),
                             cal[v.tf]["s_in"], ms(TRAIN[0]), ms(FROZEN[1]), minute=resolver)
            out.extend(tr)
        n_need = sum(len(x) for x in need.values())
        print(f"итерация {it}: сделок {len(out)}, часов без 1m {n_need}, вход по 1m {stat['m1']}, "
              f"часовая модель {stat['fallback']}, пропуск (обе стороны в минуту) {stat['skip']}",
              flush=True)
        if not n_need:
            break
        m1_ensure(need)
        mcache.clear()
    df = pd.DataFrame(out)
    df.to_csv(OUT / f"trades_{key}_m1.csv", index=False)
    print("\n".join(describe(df, key + " (вход по 1m)")))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "grid"
    if cmd == "calibrate":
        calibrate()
    elif cmd == "grid":
        run_grid()
    elif cmd == "report":
        report()
    elif cmd == "h1fetch":
        h1_fetch()
    elif cmd == "h1dry":
        h1_fetch(dry=True)
    elif cmd == "refine":
        refine(sys.argv[2] if len(sys.argv) > 2 else select())
    else:
        raise SystemExit(f"неизвестная команда {cmd}")
