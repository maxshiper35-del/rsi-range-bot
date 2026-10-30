"""Замер «разворот после экстремальной свечи» — события, исполнение, сетка, вердикт.

Правила и критерий — docs/measurements/2026-10-31-extreme-reversal.md (зарегистрированы до
прогона, поправка 2: исполнение на 1h, часы со стопом и целью разом — по 1m). Данные —
scripts/research_reversal_data.py (1h, вселенная, фандинг) и общий кэш 1m (scripts/fetch_m1.py).
Прод это не импортирует.

  python -m scripts.research_reversal events     # события на 1h -> events.npy
  python -m scripts.research_reversal sim        # 1h + 1m неоднозначных часов -> sims_h.npz
  python -m scripts.research_reversal grid       # 144 варианта, выбор на учёбе, проверка
  python -m scripts.research_reversal frozen     # одна оценка заморозки
  python -m scripts.research_reversal confirm    # выбранный на 1m: проверка + заморозка
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from scripts import fetch_m1
from scripts import research_reversal_data as rd

MIN = 60_000
HOUR = 3_600_000
DAY = 86_400_000
TAKER, MAKER, SLIP = 0.00055, 0.0002, 0.0002
STOP_ATR = 0.1               # стоп за экстремумом: 0.1×ATR
MIN_R_ATR = 0.5              # R < 0.5×ATR — сделки нет
LIMIT_FRAC = 0.2             # лимит глубже на 0.2×range
LIMIT_LIFE = 3 * HOUR
LIMIT_MIN_GAP_ATR = 0.25     # лимит ближе 0.25×ATR к стопу (или за ним) — сделки нет
CAP = 24 * HOUR              # потолок удержания у target-вариантов
WINDOW = LIMIT_LIFE + 25 * HOUR

KS = (2.5, 3.0, 4.0)
MEASURES = ("range", "body")
VOLS = (False, True)
ENTRIES = ("market", "limit")
EXITS = (("target", 0.5), ("target", 1.0), ("target", 1.5), ("time", 4), ("time", 12), ("time", 24))

TRAIN = ("2022-01-01", "2024-01-01")
VALID = ("2024-01-01", "2026-08-01")
FROZEN = ("2026-08-01", "2026-11-01")
HALVES = (("24H1", "2024-01-01", "2024-07-01"), ("24H2", "2024-07-01", "2025-01-01"),
          ("25H1", "2025-01-01", "2025-07-01"), ("25H2", "2025-07-01", "2026-01-01"),
          ("26-01..07", "2026-01-01", "2026-08-01"))


def ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)


def out_dir() -> Path:
    return rd.root()


def _meta() -> dict[str, dict]:
    return {x["symbol"]: x for x in rd.instruments()}


def m1_name(meta: dict) -> str:
    return f"{meta['baseCoin']}/USDT:USDT"


# ---------- события ----------

def find_events() -> np.ndarray:
    """Все 1h-свечи вселенной с max(range, body)/ATR ≥ 2.5 (надмножество сетки).

    Поля: sym_idx, close_ts, o, h, l, c, atr, k_range, k_body, vol_ratio,
    side (+1 LONG / −1 SHORT)."""
    uni = json.loads((out_dir() / "universe.json").read_text())
    members: dict[str, set[int]] = defaultdict(set)
    for day, syms in uni.items():
        for s in syms:
            members[s].add(ms(day))
    import pandas as pd

    syms = sorted(members)
    parts = []
    for si, sym in enumerate(syms):
        a = rd.load("hourly", sym)
        if len(a) < 40:
            continue
        df = pd.DataFrame(a[:, :6], columns=["ts", "o", "h", "l", "c", "v"])
        pc = df["c"].shift(1)
        tr = pd.concat([df["h"] - df["l"], (df["h"] - pc).abs(), (df["l"] - pc).abs()],
                       axis=1).max(axis=1)
        atr = tr.shift(1).rolling(14).mean()                  # свечи t−14…t−1
        med = df["v"].shift(1).rolling(24).median()           # t−24…t−1
        contig = ((df["ts"] - df["ts"].shift(14)) == 14 * HOUR) & \
                 ((df["ts"] - df["ts"].shift(24)) == 24 * HOUR)
        day = (df["ts"] // DAY * DAY).astype("int64")
        in_uni = day.isin(members[sym])
        kr = (df["h"] - df["l"]) / atr
        kb = (df["c"] - df["o"]).abs() / atr
        m = contig & in_uni & (atr > 0) & (np.maximum(kr, kb) >= KS[0])
        if not m.any():
            continue
        d = df[m]
        vr = np.where(med[m] > 0, d["v"] / med[m], 0.0)
        side = np.where((d["o"] - d["l"]) >= (d["h"] - d["o"]), 1, -1)
        parts.append(np.column_stack([np.full(len(d), si), d["ts"] + HOUR, d["o"], d["h"],
                                      d["l"], d["c"], atr[m], kr[m], kb[m], vr, side]))
    ev = np.concatenate(parts).astype(float) if parts else np.empty((0, 11))
    ev = ev[np.argsort(ev[:, 1], kind="stable")]
    np.save(out_dir() / "events.npy", ev)
    (out_dir() / "event_syms.json").write_text(json.dumps(syms))
    return ev


def load_events() -> tuple[np.ndarray, list[str]]:
    return (np.load(out_dir() / "events.npy"),
            json.loads((out_dir() / "event_syms.json").read_text()))


def ensure_m1(ev: np.ndarray, syms: list[str]) -> None:
    """1m-окна событий через общий кэш fetch_m1 (с его .lock)."""
    meta = _meta()
    win: dict[str, list] = defaultdict(list)
    for e in ev:
        t0 = int(e[1])
        win[m1_name(meta[syms[int(e[0])]])].append([t0, t0 + WINDOW])
    print(f"1m: {len(win)} символов, окон {sum(len(v) for v in win.values())}", flush=True)
    # 3 потока перекрывают задержку сети; пауза 1.05 с на поток держит сумму ≤ ~2.9 запроса/с
    fetch_m1.MIN_GAP_S = 1.05
    fetch_m1.ensure(win, threads=3)


def cmd_m5() -> None:
    ev, syms = load_events()
    win: dict[str, list] = defaultdict(list)
    for e in ev:
        t0 = int(e[1])
        win[syms[int(e[0])]].append([t0, t0 + WINDOW])
    print(f"5m: {len(win)} символов, окон {len(ev)}", flush=True)
    rd.ensure_m5(win)


# ---------- исполнение на 1m ----------

def _levels(e: np.ndarray, entry_kind: str):
    """(вход-лимит или None, стоп) для события; None, None — сделки нет."""
    _si, _t, _o, h, lo, c, atr, _kr, _kb, _vr, side = e
    rng = h - lo
    stop = lo - STOP_ATR * atr if side > 0 else h + STOP_ATR * atr
    if entry_kind == "market":
        return None, stop
    lim = c - LIMIT_FRAC * rng if side > 0 else c + LIMIT_FRAC * rng
    if side * (lim - stop) < LIMIT_MIN_GAP_ATR * atr:
        return "skip", stop
    return lim, stop


def simulate(e: np.ndarray, bars: np.ndarray, tick: float, fund: np.ndarray,
             entry_kind: str, exit_kind: str, exit_par: float, step: int = MIN):
    """-> (статус, t_вход, t_выход, gross R, net R, тип выхода) или None, если свечей нет.

    bars — 1m (step=MIN) или 5m (поправка 1 регистрации). статус: 1 сделка, 0 нет сделки
    (лимит не исполнился / правило входа)."""
    side = e[10]
    # 1m: минута :01 после закрытия события; 5m: первый 5m-бар (:00) — поправка 1
    t0 = int(e[1]) + (MIN if step == MIN else 0)
    lim, stop = _levels(e, entry_kind)
    if lim == "skip":
        return (0, 0, 0, 0.0, 0.0, "no_room")
    ts = bars[:, 0]
    k0 = int(np.searchsorted(ts, t0))
    if k0 >= len(ts) or ts[k0] != t0:
        return None
    # вход
    if entry_kind == "market":
        k_in = k0
        px_in = bars[k_in, 1] * (1 + SLIP * side)
        fee_in = TAKER
        same_min_target = True
    else:
        k_end = int(np.searchsorted(ts, t0 + LIMIT_LIFE))
        seg = bars[k0:k_end]
        hit = (seg[:, 3] < lim - tick) if side > 0 else (seg[:, 2] > lim + tick)
        if not hit.any():
            return (0, 0, 0, 0.0, 0.0, "unfilled")
        k_in = k0 + int(np.argmax(hit))
        px_in = lim
        fee_in = MAKER
        same_min_target = False
    t_in = int(ts[k_in])
    R = abs(px_in - stop)
    if R < MIN_R_ATR * e[6] or side * (px_in - stop) <= 0:
        return (0, 0, 0, 0.0, 0.0, "tight")
    hold = CAP if exit_kind == "target" else int(exit_par * HOUR)
    k_last = int(np.searchsorted(ts, t_in + hold))     # первый бар не раньше t_in + hold
    seg = bars[k_in:k_last]
    if len(seg) == 0 or (k_last >= len(ts) and ts[-1] < t_in + hold - step):
        return None                                    # окно свечей не покрывает удержание
    lo_ = seg[:, 3]
    hi_ = seg[:, 2]
    stop_hit = (lo_ <= stop) if side > 0 else (hi_ >= stop)
    ks = int(np.argmax(stop_hit)) if stop_hit.any() else None
    kt = None
    if exit_kind == "target":
        tgt = px_in + side * exit_par * (e[3] - e[4])
        t_hit = (hi_ > tgt + tick) if side > 0 else (lo_ < tgt - tick)
        if not same_min_target:
            t_hit[0] = False
        kt = int(np.argmax(t_hit)) if t_hit.any() else None
    if ks is not None and (kt is None or ks <= kt):           # худший порядок: стоп раньше
        op = seg[ks, 1]
        fill = min(op, stop) if side > 0 else max(op, stop)
        if ks == 0 and entry_kind == "market":
            fill = stop                                        # открытие = наш вход
        px_out = fill * (1 - SLIP * side)
        fee_out, kind, k_out = TAKER, "stop", k_in + ks
    elif kt is not None:
        px_out, fee_out, kind, k_out = tgt, MAKER, "target", k_in + kt
    else:
        if k_last >= len(ts):
            return None
        px_out = bars[k_last, 1] * (1 - SLIP * side)
        fee_out, kind, k_out = TAKER, "time", k_last
    t_out = int(ts[k_out])
    gross = side * (px_out - px_in) / R
    cost = (fee_in * px_in + fee_out * px_out) / R
    if len(fund):
        a = int(np.searchsorted(fund[:, 0], t_in, side="right"))
        b = int(np.searchsorted(fund[:, 0], t_out, side="right"))
        cost += side * float(fund[a:b, 1].sum()) * px_in / R
    return (1, t_in, t_out, gross, gross - cost, kind)


def _cost(px_in: float, px_out: float, fee_in: float, fee_out: float, R: float, side: float,
          fund: np.ndarray, t_in: int, t_out: int) -> float:
    cost = (fee_in * px_in + fee_out * px_out) / R
    if len(fund):
        a = int(np.searchsorted(fund[:, 0], t_in, side="right"))
        b = int(np.searchsorted(fund[:, 0], t_out, side="right"))
        cost += side * float(fund[a:b, 1].sum()) * px_in / R
    return cost


def simulate_h(e: np.ndarray, h1: np.ndarray, tick: float, fund: np.ndarray, entry_kind: str,
               exit_kind: str, exit_par: float, resolve):
    """Поправка 2: исполнение на 1h, час со стопом и целью разом — по 1m (resolve(час) -> 1m
    свечи часа или None -> худший порядок). -> как simulate()."""
    side = e[10]
    close_ts = int(e[1])
    lim, stop = _levels(e, entry_kind)
    if lim == "skip":
        return (0, 0, 0, 0.0, 0.0, "no_room")
    ts = h1[:, 0]
    k0 = int(np.searchsorted(ts, close_ts))
    if k0 >= len(ts) or ts[k0] != close_ts:
        return None
    if entry_kind == "market":
        k_in, px_in, fee_in, tgt_first = k0, h1[k0, 1] * (1 + SLIP * side), TAKER, True
    else:
        k_end = int(np.searchsorted(ts, close_ts + LIMIT_LIFE))
        seg = h1[k0:k_end]
        hit = (seg[:, 3] < lim - tick) if side > 0 else (seg[:, 2] > lim + tick)
        if not hit.any():
            return (0, 0, 0, 0.0, 0.0, "unfilled")
        k_in, px_in, fee_in, tgt_first = k0 + int(np.argmax(hit)), lim, MAKER, False
    t_in = int(ts[k_in])
    R = abs(px_in - stop)
    if R < MIN_R_ATR * e[6] or side * (px_in - stop) <= 0:
        return (0, 0, 0, 0.0, 0.0, "tight")
    hold = 24 if exit_kind == "target" else int(exit_par)
    t_exit = t_in + (hold if entry_kind == "market" else hold + 1) * HOUR
    k_last = int(np.searchsorted(ts, t_exit))
    if k_last >= len(ts) or ts[k_last] != t_exit or (k_last - k_in) * HOUR != t_exit - t_in:
        return None                                   # дыра в часах или конец данных
    tgt = px_in + side * exit_par * (e[3] - e[4]) if exit_kind == "target" else None
    px_out = None
    for k in range(k_in, k_last):
        lo_, hi_, op = h1[k, 3], h1[k, 2], h1[k, 1]
        s_hit = lo_ <= stop if side > 0 else hi_ >= stop
        t_hit = tgt is not None and (k > k_in or tgt_first) and (
            hi_ > tgt + tick if side > 0 else lo_ < tgt - tick)
        if not (s_hit or t_hit):
            continue
        if s_hit and t_hit:
            m = resolve(int(ts[k]))
            if m is not None and len(m):
                sm = (m[:, 3] <= stop) if side > 0 else (m[:, 2] >= stop)
                tm = (m[:, 2] > tgt + tick) if side > 0 else (m[:, 3] < tgt - tick)
                ks = int(np.argmax(sm)) if sm.any() else None
                kt = int(np.argmax(tm)) if tm.any() else None
                if kt is not None and (ks is None or kt < ks):        # цель строго раньше
                    px_out, fee_out, kind, t_out = tgt, MAKER, "target", int(m[kt, 0])
                    break
                if ks is not None:
                    mo = m[ks, 1]
                    fill = min(mo, stop) if side > 0 else max(mo, stop)
                    px_out, fee_out, kind, t_out = (fill * (1 - SLIP * side), TAKER, "stop",
                                                    int(m[ks, 0]))
                    break
            t_hit = False                              # нет 1m (или нет касаний) — худший порядок
        if s_hit:
            fill = min(op, stop) if side > 0 else max(op, stop)
            px_out, fee_out, kind, t_out = fill * (1 - SLIP * side), TAKER, "stop", int(ts[k])
            break
        px_out, fee_out, kind, t_out = tgt, MAKER, "target", int(ts[k])
        break
    if px_out is None:
        px_out = h1[k_last, 1] * (1 - SLIP * side)
        fee_out, kind, t_out = TAKER, "time", int(ts[k_last])
    gross = side * (px_out - px_in) / R
    return (1, t_in, t_out, gross,
            gross - _cost(px_in, px_out, fee_in, fee_out, R, side, fund, t_in, t_out), kind)


def run_hybrid(ev: np.ndarray, syms: list[str], combos: list, fetch: bool = True) -> dict:
    """Поправка 2: первый проход собирает часы со стопом и целью разом, докачка их 1m (общий
    кэш fetch_m1), второй проход — исполнение."""
    meta = _meta()
    need: dict[str, set[int]] = defaultdict(set)

    def recorder(name):
        def r(hour_ts):
            need[name].add(hour_ts)
            return None
        return r

    def loader(name):
        bars = fetch_m1.load(name)
        bts = bars[:, 0] if len(bars) else np.empty(0)

        def r(hour_ts):
            a = int(np.searchsorted(bts, hour_ts))
            b = int(np.searchsorted(bts, hour_ts + HOUR))
            return bars[a:b] if b - a >= 50 else None     # почти полный час, иначе худший
        return r

    by_sym: dict[int, list[int]] = defaultdict(list)
    for i, e in enumerate(ev):
        by_sym[int(e[0])].append(i)
    passes = ("record", "run") if fetch else ("run",)
    out: dict = {}
    for mode in passes:
        shape = (len(ev), len(combos))
        out = {"status": np.full(shape, -1, dtype=np.int8),
               "t_in": np.zeros(shape, dtype=np.int64), "t_out": np.zeros(shape, dtype=np.int64),
               "gross": np.zeros(shape), "net": np.zeros(shape),
               "kinds": np.zeros(shape, dtype=np.int8),
               "combos": np.array([f"{a}|{b}|{c}" for a, b, c in combos])}
        kmap = {"stop": 1, "target": 2, "time": 3}
        for n, (si, idx) in enumerate(sorted(by_sym.items()), 1):
            sym = syms[si]
            m = meta[sym]
            h1 = rd.load("hourly", sym)
            tick = float(m["priceFilter"]["tickSize"])
            name = m1_name(m)
            res = recorder(name) if mode == "record" else loader(name)
            fund = (np.empty((0, 2)) if mode == "record" else
                    rd.fetch_funding(sym, int(ev[idx, 1].min()) - DAY,
                                     int(ev[idx, 1].max()) + WINDOW + DAY))
            for i in idx:
                for j, (en, ex, p) in enumerate(combos):
                    r = simulate_h(ev[i], h1, tick, fund, en, ex, p, res)
                    if r is None:
                        continue
                    out["status"][i, j] = r[0]
                    if r[0] == 1:
                        out["t_in"][i, j], out["t_out"][i, j] = r[1], r[2]
                        out["gross"][i, j], out["net"][i, j] = r[3], r[4]
                        out["kinds"][i, j] = kmap[r[5]]
            if n % 40 == 0:
                print(f"sim {mode} {n}/{len(by_sym)}", flush=True)
        if mode == "record":
            win = {name: [[h, h + HOUR] for h in sorted(hs)] for name, hs in need.items()}
            print(f"неоднозначных часов {sum(len(v) for v in win.values())} "
                  f"в {len(win)} символах — докачка 1m", flush=True)
            fetch_m1.MIN_GAP_S = 1.05                  # 3 потока, сумма ≤ ~2.9 запроса/с
            fetch_m1.ensure(win, threads=3)
    return out


def run_sims(ev: np.ndarray, syms: list[str], combos: list, resolution: str) -> dict:
    """Исполнение всех событий ev по комбинациям (вход, выход) на 5m или 1m свечах."""
    meta = _meta()
    step = MIN if resolution == "1m" else rd.M5
    shape = (len(ev), len(combos))
    out = {"status": np.full(shape, -1, dtype=np.int8),           # −1 нет свечей
           "t_in": np.zeros(shape, dtype=np.int64), "t_out": np.zeros(shape, dtype=np.int64),
           "gross": np.zeros(shape), "net": np.zeros(shape),
           "kinds": np.zeros(shape, dtype=np.int8),               # 1 stop 2 target 3 time
           "combos": np.array([f"{a}|{b}|{c}" for a, b, c in combos])}
    kmap = {"stop": 1, "target": 2, "time": 3}
    by_sym: dict[int, list[int]] = defaultdict(list)
    for i, e in enumerate(ev):
        by_sym[int(e[0])].append(i)
    for n, (si, idx) in enumerate(sorted(by_sym.items()), 1):
        sym = syms[si]
        m = meta[sym]
        bars = fetch_m1.load(m1_name(m)) if resolution == "1m" else rd.load_m5(sym)
        tick = float(m["priceFilter"]["tickSize"])
        fund = rd.fetch_funding(sym, int(ev[idx, 1].min()) - DAY,
                                int(ev[idx, 1].max()) + WINDOW + DAY)
        for i in idx:
            for j, (en, ex, p) in enumerate(combos):
                r = simulate(ev[i], bars, tick, fund, en, ex, p, step) if len(bars) else None
                if r is None:
                    continue
                out["status"][i, j] = r[0]
                if r[0] == 1:
                    out["t_in"][i, j], out["t_out"][i, j] = r[1], r[2]
                    out["gross"][i, j], out["net"][i, j] = r[3], r[4]
                    out["kinds"][i, j] = kmap[r[5]]
        if n % 20 == 0:
            print(f"sim {resolution} {n}/{len(by_sym)}", flush=True)
    return out


def cmd_sim() -> None:
    ev, syms = load_events()
    combos = [(en, ex, p) for en in ENTRIES for ex, p in EXITS]
    np.savez(out_dir() / "sims_h.npz", **run_hybrid(ev, syms, combos))


# ---------- сетка и вердикт ----------

def variants():
    for k in KS:
        for mea in MEASURES:
            for vol in VOLS:
                for en in ENTRIES:
                    for ex in EXITS:
                        yield (k, mea, vol, en, ex)


def vname(v) -> str:
    k, mea, vol, en, (ex, p) = v
    return f"k{k:g}-{mea}-{'vol' if vol else 'novol'}-{en}-{ex}{p:g}"


def trades(v, ev, sims, lo: int, hi: int) -> list[tuple]:
    """Сделки варианта с закрытием события в [lo, hi): (t_вход, t_выход, gross, net, side, sym).
    Одна позиция на символ — по фактическому выходу предыдущей сделки варианта."""
    k, mea, vol, en, (ex, p) = v
    j = list(sims["combos"]).index(f"{en}|{ex}|{p}")
    col = 7 if mea == "range" else 8
    sel = (ev[:, col] > k) & (ev[:, 1] >= lo) & (ev[:, 1] < hi)
    if vol:
        sel &= ev[:, 9] >= 3.0
    out = []
    busy: dict[int, int] = {}
    for i in np.nonzero(sel)[0][np.argsort(ev[sel, 1], kind="stable")]:
        si = int(ev[i, 0])
        if busy.get(si, 0) > ev[i, 1]:
            continue
        st = sims["status"][i, j]
        if st != 1:
            if st == 0 and en == "limit":
                busy[si] = max(busy.get(si, 0), int(ev[i, 1]) + LIMIT_LIFE)
            continue
        busy[si] = int(sims["t_out"][i, j])
        out.append((int(sims["t_in"][i, j]), int(sims["t_out"][i, j]),
                    float(sims["gross"][i, j]), float(sims["net"][i, j]), int(ev[i, 10]), si))
    return out


def stats(tr: list[tuple]) -> dict:
    if not tr:
        return dict(n=0, gross=0.0, net=0.0, sum=0.0, win=0.0, dd=0.0, longs=0, l_net=0.0,
                    s_net=0.0)
    g = np.array([t[2] for t in tr])
    nt = np.array([t[3] for t in tr])
    order = np.argsort([t[1] for t in tr])
    eq = np.cumsum(nt[order])
    dd = float((np.maximum.accumulate(np.r_[0.0, eq]) - np.r_[0.0, eq]).max())
    side = np.array([t[4] for t in tr])
    return dict(n=len(tr), gross=float(g.mean()), net=float(nt.mean()), sum=float(nt.sum()),
                win=float((nt > 0).mean()), dd=dd, longs=int((side > 0).sum()),
                l_net=float(nt[side > 0].sum()), s_net=float(nt[side < 0].sum()))


def neighbors(v) -> list:
    k, mea, vol, en, (ex, p) = v
    out = []
    ki = KS.index(k)
    for d in (-1, 1):
        if 0 <= ki + d < len(KS):
            out.append((KS[ki + d], mea, vol, en, (ex, p)))
    fam = [x for x in EXITS if x[0] == ex]
    fi = fam.index((ex, p))
    for d in (-1, 1):
        if 0 <= fi + d < len(fam):
            out.append((k, mea, vol, en, fam[fi + d]))
    return out


def max_concurrent(tr: list[tuple]) -> int:
    pts = sorted([(t[0], 1) for t in tr] + [(t[1], -1) for t in tr])
    cur = best = 0
    for _t, d in pts:
        cur += d
        best = max(best, cur)
    return best


def capped(tr: list[tuple], cap: int = 5) -> list[tuple]:
    out, open_ = [], []
    for t in sorted(tr):
        open_ = [x for x in open_ if x > t[0]]
        if len(open_) >= cap:
            continue
        out.append(t)
        open_.append(t[1])
    return out


def robust(vt: list[tuple]) -> dict:
    """Критерий 4: ΣR без 5 лучших сделок и без 2 лучших календарных дней (по дню выхода)."""
    nets = sorted((t[3] for t in vt), reverse=True)
    by_day: dict[int, float] = defaultdict(float)
    for t in vt:
        by_day[t[1] // DAY] += t[3]
    top = sorted(by_day.items(), key=lambda x: x[1], reverse=True)[:2]
    return {"no_top5_trades": float(sum(nets[5:])),
            "no_top2_days": float(sum(by_day.values()) - sum(x[1] for x in top)),
            "top2_days": [[datetime.fromtimestamp(d * DAY / 1000, timezone.utc)
                           .strftime("%Y-%m-%d"), r] for d, r in top]}


def cmd_grid() -> None:
    ev, _syms = load_events()
    sims = dict(np.load(out_dir() / "sims_h.npz"))         # в память: npz читает ключ с диска
    tr_lo, tr_hi = ms(TRAIN[0]), ms(TRAIN[1])
    va_lo, va_hi = ms(VALID[0]), ms(VALID[1])
    rows = []
    for v in variants():
        st = stats(trades(v, ev, sims, tr_lo, tr_hi))
        rows.append((v, st))
    elig = [(v, s) for v, s in rows if s["n"] >= 200]
    elig.sort(key=lambda x: (x[1]["net"], x[1]["sum"]), reverse=True)
    res: dict = {"train_top5": [], "all_train": {}}
    for v, s in rows:
        res["all_train"][vname(v)] = s
    print("Учёба, топ-5 (n ≥ 200):")
    for v, s in elig[:5]:
        print(f"  {vname(v):40s} n={s['n']:5d} gross={s['gross']:+.3f} net={s['net']:+.3f} "
              f"ΣR={s['sum']:+.1f} DD={s['dd']:.1f}")
        res["train_top5"].append([vname(v), s])
    pos = sum(1 for _v, s in rows if s["n"] >= 200 and s["net"] > 0)
    print(f"вариантов с n≥200: {len(elig)}, из них net>0 на учёбе: {pos}")
    if not elig:
        print("нет вариантов с ≥200 сделок на учёбе")
        return
    best = elig[0][0]
    res["selected"] = vname(best)
    vt = trades(best, ev, sims, va_lo, va_hi)
    vs = stats(vt)
    res["valid"] = vs
    halves = {}
    for name, a, b in HALVES:
        halves[name] = stats(trades(best, ev, sims, ms(a), ms(b)))
    res["halves"] = halves
    nb = {}
    for w in neighbors(best):
        nb[vname(w)] = {"train": stats(trades(w, ev, sims, tr_lo, tr_hi)),
                        "valid": stats(trades(w, ev, sims, va_lo, va_hi))}
    res["neighbors"] = nb
    res["robust"] = rb = robust(vt)
    no_top5, no_top2d = rb["no_top5_trades"], rb["no_top2_days"]
    res["max_concurrent_valid"] = max_concurrent(vt)
    res["valid_cap5"] = stats(capped(vt))
    c1 = vs["net"] > 0 and all(h["sum"] > 0 for h in halves.values())
    c2 = all(x["train"]["net"] > 0 and x["valid"]["net"] > 0 for x in nb.values())
    c3 = vs["n"] >= 150
    c4 = no_top5 > 0 and no_top2d > 0
    res["criterion"] = {"1_valid_and_halves": c1, "2_neighbors": c2, "3_n150": c3,
                        "4_robust": c4, "candidate": bool(c1 and c2 and c3 and c4)}
    (out_dir() / "grid_result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print(f"\nВыбран: {vname(best)}")
    print(f"Проверка: n={vs['n']} gross={vs['gross']:+.3f} net={vs['net']:+.3f} "
          f"ΣR={vs['sum']:+.1f} DD={vs['dd']:.1f} win={vs['win']:.2f} "
          f"long ΣR={vs['l_net']:+.1f} short ΣR={vs['s_net']:+.1f}")
    for name, h in halves.items():
        print(f"  {name}: n={h['n']} net={h['net']:+.3f} ΣR={h['sum']:+.1f}")
    for name, x in nb.items():
        print(f"  сосед {name}: учёба net={x['train']['net']:+.3f} (n={x['train']['n']}), "
              f"проверка net={x['valid']['net']:+.3f} (n={x['valid']['n']})")
    print(f"  без топ-5 сделок ΣR={no_top5:+.1f}; без 2 лучших дней ΣR={no_top2d:+.1f}")
    print(f"  макс. одновременно={res['max_concurrent_valid']}; потолок 5: "
          f"net={res['valid_cap5']['net']:+.3f} ΣR={res['valid_cap5']['sum']:+.1f}")
    print(f"Критерий: {res['criterion']}")


def cmd_frozen() -> None:
    """Одна оценка заморозки выбранного варианта (1h + 1m для неоднозначных часов)."""
    ev, _syms = load_events()
    sims = dict(np.load(out_dir() / "sims_h.npz"))         # в память: npz читает ключ с диска
    res = json.loads((out_dir() / "grid_result.json").read_text())
    sel = next(v for v in variants() if vname(v) == res["selected"])
    fs = stats(trades(sel, ev, sims, ms(FROZEN[0]), ms(FROZEN[1])))
    res["frozen_h"] = fs
    (out_dir() / "grid_result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print(f"Заморозка {res['selected']}: n={fs['n']} gross={fs['gross']:+.3f} "
          f"net={fs['net']:+.3f} ΣR={fs['sum']:+.1f} DD={fs['dd']:.1f}")


def cmd_confirm() -> None:
    """Поправка 1: выбранный вариант на настоящих 1m — проверка (критерии 1, 3, 4) и заморозка."""
    ev, syms = load_events()
    res = json.loads((out_dir() / "grid_result.json").read_text())
    sel = next(v for v in variants() if vname(v) == res["selected"])
    k, mea, vol, en, (ex, p) = sel
    col = 7 if mea == "range" else 8
    m = (ev[:, col] > k) & (ev[:, 1] >= ms(VALID[0])) & (ev[:, 1] < ms(FROZEN[1]))
    if vol:
        m &= ev[:, 9] >= 3.0
    sub = ev[m]
    ensure_m1(sub, syms)
    sims = run_sims(sub, syms, [(en, ex, p)], "1m")
    vt = trades(sel, sub, sims, ms(VALID[0]), ms(VALID[1]))
    vs = stats(vt)
    halves = {name: stats(trades(sel, sub, sims, ms(a), ms(b))) for name, a, b in HALVES}
    rb = robust(vt)
    fs = stats(trades(sel, sub, sims, ms(FROZEN[0]), ms(FROZEN[1])))
    c1 = vs["net"] > 0 and all(h["sum"] > 0 for h in halves.values())
    c3 = vs["n"] >= 150
    c4 = rb["no_top5_trades"] > 0 and rb["no_top2_days"] > 0
    res["confirm_1m"] = {"valid": vs, "halves": halves, "robust": rb, "frozen": fs,
                         "missing_1m": int((sims["status"][:, 0] == -1).sum()),
                         "criterion": {"1": c1, "3": c3, "4": c4,
                                       "candidate": bool(c1 and c3 and c4)}}
    (out_dir() / "grid_result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print(json.dumps(res["confirm_1m"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    {"events": lambda: print(f"событий {len(find_events())}"), "m5": cmd_m5, "sim": cmd_sim,
     "grid": cmd_grid, "frozen": cmd_frozen, "confirm": cmd_confirm}[sys.argv[1]]()
