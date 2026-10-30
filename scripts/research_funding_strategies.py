"""Прогон замера «фандинг и OI как самостоятельная стратегия»
(docs/measurements/2026-10-31-funding-strategies.md — регистрация до прогона).

  nice -n 15 .venv/bin/python -m scripts.research_funding_strategies           # учёба + проверка
  nice -n 15 .venv/bin/python -m scripts.research_funding_strategies --frozen  # одна оценка

Данные — scripts/research_funding_data.py (кэш вне git). Правила исполнения, сетка, правило
выбора и критерий — ровно как в регистрации; здесь ничего не подбирается.
"""
from __future__ import annotations

import argparse
import itertools
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from scripts.research_funding_data import CACHE, UNIVERSE

H_MS = 3_600_000
DAY_MS = 24 * H_MS


def _ms(s: str) -> int:
    return int(pd.Timestamp(s, tz="UTC").value // 1_000_000)


TRAIN = (_ms("2021-01-01"), _ms("2024-01-01"))
TEST = (_ms("2024-01-01"), _ms("2026-08-01"))
FROZEN = (_ms("2026-08-01"), _ms("2026-10-30"))
HALVES = [("2024H1", _ms("2024-01-01"), _ms("2024-07-01")),
          ("2024H2", _ms("2024-07-01"), _ms("2025-01-01")),
          ("2025H1", _ms("2025-01-01"), _ms("2025-07-01")),
          ("2025H2", _ms("2025-07-01"), _ms("2026-01-01")),
          ("2026H1", _ms("2026-01-01"), _ms("2026-08-01"))]
YEARS = [("2024", _ms("2024-01-01"), _ms("2025-01-01"), 1.0),
         ("2025", _ms("2025-01-01"), _ms("2026-01-01"), 1.0),
         ("2026 янв–июл", _ms("2026-01-01"), _ms("2026-08-01"), 7 / 12)]

FEE = 0.00055          # тейкер перпа
SLIP = 0.0002          # на каждую сторону
SPOT_FEE = 0.0010      # тейкер спота (CARRY)
RANK_WIN = 90 * DAY_MS


# ─────────────────────────────── данные ───────────────────────────────

@dataclass
class Sym:
    name: str
    ts: np.ndarray            # начало 1h-бара, мс
    o: np.ndarray
    h: np.ndarray
    lo: np.ndarray
    c: np.ndarray
    atr: np.ndarray           # ATR(24) по барам ДО бара i (для входа по open бара i)
    f_ts: np.ndarray          # моменты расчёта ставок
    f_rate: np.ndarray        # ставка за интервал
    f_hour: np.ndarray        # ставка за час
    f_rank: np.ndarray        # средний ранг за 90 дней (nan — не определён)
    oi_ts: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    oi_rank: np.ndarray = field(default_factory=lambda: np.zeros(0))
    p_ts: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    p_o: np.ndarray = field(default_factory=lambda: np.zeros(0))


def _mid_rank(vals: np.ndarray, ts: np.ndarray, win_ms: int, min_n: int) -> np.ndarray:
    """Средний ранг vals[i] среди vals с ts в (ts[i] − win, ts[i]]; < min_n точек — nan."""
    out = np.full(len(vals), np.nan)
    lo = np.searchsorted(ts, ts - win_ms, side="right")
    for i in range(len(vals)):
        if not np.isfinite(vals[i]):
            continue
        w = vals[lo[i]:i + 1]
        w = w[np.isfinite(w)]
        if len(w) < min_n:
            continue
        v = vals[i]
        out[i] = ((w < v).sum() + (w <= v).sum()) / (2 * len(w))
    return out


def load(name: str) -> Sym | None:
    kp = CACHE / f"{name}_kline1h.csv"
    fp = CACHE / f"{name}_funding.csv"
    if not kp.exists() or not fp.exists():
        return None
    k = pd.read_csv(kp).drop_duplicates("ts").sort_values("ts")
    k = k[k.close > 0]
    ts = k.ts.to_numpy(np.int64)
    o, h, lo, c = (k[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    prev_c = np.concatenate([[np.nan], c[:-1]])
    tr = np.nanmax(np.vstack([h - lo, np.abs(h - prev_c), np.abs(lo - prev_c)]), axis=0)
    atr_incl = pd.Series(tr).rolling(24, min_periods=24).mean().to_numpy()
    atr = np.concatenate([[np.nan], atr_incl[:-1]])          # только бары ДО входа
    f = pd.read_csv(fp).drop_duplicates("ts").sort_values("ts")
    f_ts = f.ts.to_numpy(np.int64)
    f_rate = f.rate.to_numpy(float)
    gap_h = np.diff(f_ts, prepend=f_ts[0] - 8 * H_MS) / H_MS
    f_hour = f_rate / np.clip(gap_h, 1, 8)
    s = Sym(name, ts, o, h, lo, c, atr, f_ts, f_rate, f_hour,
            _mid_rank(f_hour, f_ts, RANK_WIN, 60))
    op = CACHE / f"{name}_oi4h.csv"
    if op.exists():
        oi = pd.read_csv(op).drop_duplicates("ts").sort_values("ts")
        oi = oi[oi.oi > 0]
        ots = oi.ts.to_numpy(np.int64)
        ov = oi.oi.to_numpy(float)
        j = np.searchsorted(ots, ots - DAY_MS)              # метка ровно сутки назад
        ok = (j < len(ots)) & (ots[np.minimum(j, len(ots) - 1)] == ots - DAY_MS)
        chg = np.full(len(ov), np.nan)
        chg[ok] = ov[ok] / ov[j[ok]] - 1
        s.oi_ts, s.oi_rank = ots, _mid_rank(chg, ots, RANK_WIN, 300)
    pp = CACHE / f"{name}_premium1h.csv"
    if pp.exists():
        p = pd.read_csv(pp).drop_duplicates("ts").sort_values("ts")
        s.p_ts, s.p_o = p.ts.to_numpy(np.int64), p.open.to_numpy(float)
    return s


# ─────────────────────────────── FC ───────────────────────────────

@dataclass(frozen=True)
class FCVar:
    P: float
    oi: bool
    k: float
    m: float
    H: int

    def label(self) -> str:
        return f"P{self.P:.2f} OI{'+' if self.oi else '-'} k{self.k:g} m{self.m:g} H{self.H}"


FC_GRID = [FCVar(P, oi, k, m, H) for P, oi, k, m, H in itertools.product(
    (0.90, 0.95, 0.98), (False, True), (1.5, 3.0), (1.0, 2.0, 3.0), (24, 72))]


def _oi_rank_at(s: Sym, T: int) -> float:
    if not len(s.oi_ts):
        return np.nan
    i = np.searchsorted(s.oi_ts, T - 4 * H_MS, side="right") - 1
    return float(s.oi_rank[i]) if i >= 0 else np.nan


def fc_trades(s: Sym, v: FCVar, lo_ms: int, hi_ms: int) -> list[dict]:
    """Сделки FC символа с входом в [lo_ms, hi_ms)."""
    out: list[dict] = []
    busy_until = -1
    n_bars = len(s.ts)
    for kf in range(len(s.f_ts)):
        T = int(s.f_ts[kf])
        p = s.f_rank[kf]
        if not np.isfinite(p) or T < busy_until:
            continue
        side = -1 if p >= v.P else (1 if p <= 1 - v.P else 0)
        if side == 0:
            continue
        if v.oi:
            r = _oi_rank_at(s, T)
            if not (np.isfinite(r) and r >= 0.8):
                continue
        e = int(np.searchsorted(s.ts, T + H_MS))             # первый бар, начавшийся ≥ T+1ч
        if e >= n_bars or not (lo_ms <= s.ts[e] < hi_ms):
            continue
        atr = s.atr[e]
        if not (np.isfinite(atr) and atr > 0):
            continue
        entry = s.o[e] * (1 + side * SLIP)
        dist = v.k * atr
        stop = entry - side * dist
        target = entry + side * v.m * dist
        exit_px = None
        exit_i = None
        why = "time"
        last = min(n_bars - 1, e + v.H - 1)
        for i in range(e, last + 1):
            if side > 0:
                gap = i > e and s.o[i] <= stop
                hit_stop = s.lo[i] <= stop
                hit_tg = s.h[i] >= target
            else:
                gap = i > e and s.o[i] >= stop
                hit_stop = s.h[i] >= stop
                hit_tg = s.lo[i] <= target
            if gap:
                exit_px, why = s.o[i], "stop"
            elif hit_stop:
                exit_px, why = stop, "stop"
            elif hit_tg:
                exit_px, why = target, "target"
            if exit_px is not None:
                exit_i = i
                break
        if exit_px is None:                                  # срок: open следующего бара
            j = e + v.H
            if j < n_bars:
                exit_px, exit_i, exit_t = s.o[j], j, int(s.ts[j])
            else:
                exit_px, exit_i, exit_t = s.c[-1], n_bars - 1, int(s.ts[-1]) + H_MS
            fund_hi = exit_t - 1                             # расчёт ровно в момент выхода — не наш
        else:
            exit_t = int(s.ts[exit_i]) + H_MS               # выход где-то внутри бара
            fund_hi = int(s.ts[exit_i])                      # расчёт в начале бара выхода — наш
        exit_px = exit_px * (1 - side * SLIP)
        a = np.searchsorted(s.f_ts, int(s.ts[e]), side="right")
        b = np.searchsorted(s.f_ts, fund_hi, side="right")
        fund = 0.0
        for q in range(a, b):
            bi = min(n_bars - 1, max(0, int(np.searchsorted(s.ts, s.f_ts[q], side="right")) - 1))
            fund += -side * s.f_rate[q] * s.c[bi]
        gross = side * (exit_px - entry) / dist
        cost = FEE * (entry + exit_px) / dist
        out.append(dict(sym=s.name, side=side, T=T, t_in=int(s.ts[e]), t_out=exit_t, why=why,
                        gross=gross, cost=cost, fund=fund / dist,
                        net=gross - cost + fund / dist))
        busy_until = exit_t
    return out


def fc_run(syms: list[Sym], v: FCVar, lo_ms: int, hi_ms: int) -> pd.DataFrame:
    rows = []
    for s in syms:
        rows += fc_trades(s, v, lo_ms, hi_ms)
    return pd.DataFrame(rows)


def _stats(df: pd.DataFrame) -> dict:
    if df.empty:
        return dict(n=0, exp=float("nan"), sum=0.0, gross=float("nan"), dd=0.0)
    d = df.sort_values("t_out")
    eq = d.net.cumsum().to_numpy()
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:] - eq))
    return dict(n=len(d), exp=float(d.net.mean()), sum=float(d.net.sum()),
                gross=float(d.gross.mean()), dd=dd)


def _max_concurrent(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    ev = sorted([(t, 1) for t in df.t_in] + [(t, -1) for t in df.t_out], key=lambda x: (x[0], x[1]))
    cur = best = 0
    for _t, d in ev:
        cur += d
        best = max(best, cur)
    return best


def event_study(syms: list[Sym], P: float, lo_ms: int, hi_ms: int) -> pd.DataFrame:
    """Диагностика вне критерия: ход цены через 8/24/72 ч после крайнего фандинга (без стопов
    и издержек). Одно событие на символ и сторону за 24 ч."""
    rows = []
    for s in syms:
        last = {1: -10**15, -1: -10**15}
        for kf in range(len(s.f_ts)):
            T, p = int(s.f_ts[kf]), s.f_rank[kf]
            if not np.isfinite(p):
                continue
            side = -1 if p >= P else (1 if p <= 1 - P else 0)
            if side == 0 or T - last[side] < DAY_MS:
                continue
            e = int(np.searchsorted(s.ts, T + H_MS))
            if e + 72 >= len(s.ts) or not (lo_ms <= s.ts[e] < hi_ms):
                continue
            last[side] = T
            r = {f"r{hh}": side * (s.c[e + hh - 1] / s.o[e] - 1) * 1e4 for hh in (8, 24, 72)}
            rows.append(dict(sym=s.name, side=side, **r))
    return pd.DataFrame(rows)


# ─────────────────────────────── CARRY ───────────────────────────────

def _prem_at(s: Sym, t: int) -> float:
    i = np.searchsorted(s.p_ts, t, side="right") - 1
    return float(s.p_o[i]) if i >= 0 else 0.0


ROUND_SIDE = SPOT_FEE + FEE + 2 * SLIP          # одно открытие ИЛИ закрытие, обе ноги


def carry_symbol(s: Sym, lo_ms: int, hi_ms: int, mode: str, E: float = 0.0,
                 X: float = 0.0) -> pd.DataFrame:
    """Почасовая эквити (доля номинала N) позиции лонг спот + шорт перп на [lo, hi).
    mode='always' — весь участок; mode='timed' — вход при APR(3 дня) > E, выход при < X."""
    if not len(s.p_ts):
        return pd.DataFrame(columns=["t", "pnl"])
    start = max(lo_ms, int(s.p_ts[0]), int(s.f_ts[0]) + 3 * DAY_MS)
    hours = np.arange(start - start % H_MS, hi_ms, H_MS, dtype=np.int64)
    if not len(hours):
        return pd.DataFrame(columns=["t", "pnl"])
    pnl = np.zeros(len(hours))                   # приращение за час
    in_pos = False
    prem_prev = 0.0
    # решения — в моменты расчётов; исполнение — по open бара, начавшегося ≥ T+1ч
    decisions: dict[int, bool] = {}
    if mode == "always":
        decisions[int(hours[0])] = True
    else:
        a = np.searchsorted(s.f_ts, start - 3 * DAY_MS)
        for kf in range(a, len(s.f_ts)):
            T = int(s.f_ts[kf])
            if T >= hi_ms:
                break
            w0 = np.searchsorted(s.f_ts, T - 3 * DAY_MS, side="right")
            apr = float(np.mean(s.f_hour[w0:kf + 1])) * 24 * 365
            t_exec = T + H_MS
            t_exec -= t_exec % H_MS
            want = None
            if apr > E:
                want = True
            elif apr < X:
                want = False
            if want is not None:
                decisions[t_exec] = want
    f_idx = 0
    t_entry = -1
    for i, t in enumerate(hours):
        t = int(t)
        want = decisions.get(t)
        if want is True and not in_pos:
            in_pos = True
            t_entry = t
            prem_prev = _prem_at(s, t)
            pnl[i] -= ROUND_SIDE
        elif want is False and in_pos:
            in_pos = False
            pr = _prem_at(s, t)
            pnl[i] += prem_prev - pr - ROUND_SIDE
        elif in_pos:
            pr = _prem_at(s, t)
            pnl[i] += prem_prev - pr                    # шорт перпа выигрывает при сужении премии
            prem_prev = pr
        # фандинг расчётов в (t − 1ч, t] — шорт получает ставку, если держал позицию ДО расчёта
        while f_idx < len(s.f_ts) and s.f_ts[f_idx] <= t:
            if s.f_ts[f_idx] > t - H_MS and in_pos and s.f_ts[f_idx] > t_entry:
                pnl[i] += s.f_rate[f_idx]
            f_idx += 1
    if in_pos:                                           # закрытие в конце участка
        pnl[-1] -= ROUND_SIDE
    return pd.DataFrame({"t": hours, "pnl": pnl})


def carry_portfolio(syms: list[Sym], names: list[str], lo_ms: int, hi_ms: int, mode: str,
                    E: float = 0.0, X: float = 0.0, slots: int | None = None) -> pd.Series:
    """Почасовая доходность на капитал N_total (доля). Каждой монете — слот 1/slots."""
    slots = slots or len(names)
    by = {s.name: s for s in syms}
    tot = None
    for n in names:
        s = by.get(n)
        if s is None:
            continue
        df = carry_symbol(s, lo_ms, hi_ms, mode, E, X)
        if df.empty:
            continue
        ser = pd.Series(df.pnl.to_numpy() / slots, index=df.t.to_numpy())
        tot = ser if tot is None else tot.add(ser, fill_value=0.0)
    return tot if tot is not None else pd.Series(dtype=float)


def carry_report(series: pd.Series, lo_ms: int, hi_ms: int, cap: float) -> dict:
    """Годовая доходность и просадка на капитал cap × N."""
    if series.empty:
        return dict(apr=float("nan"), dd=float("nan"))
    s = series.sort_index() / cap
    eq = s.cumsum().to_numpy()
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:] - eq))
    years = (hi_ms - lo_ms) / (365 * DAY_MS)
    return dict(apr=float(eq[-1] / years), dd=dd, total=float(eq[-1]))


# ─────────────────────────────── отчёт ───────────────────────────────

def _fmt(x: float, d: int = 3) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:+.{d}f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--frozen", action="store_true", help="ОДНА оценка заморозки выбранных")
    a = ap.parse_args()
    syms = [s for s in (load(n) for n in UNIVERSE) if s is not None]
    print(f"символов с данными: {len(syms)} из {len(UNIVERSE)}")
    for s in syms:
        print(f"  {s.name}: бары с {pd.Timestamp(int(s.ts[0]), unit='ms'):%Y-%m-%d}, "
              f"ставок {len(s.f_ts)}, OI {len(s.oi_ts)}, премия {len(s.p_ts)}")

    # ── FC: выбор на учёбе
    res = []
    for v in FC_GRID:
        tr = fc_run(syms, v, *TRAIN)
        res.append((v, _stats(tr)))
    elig = [(v, st) for v, st in res if st["n"] >= 200]
    best_v, best_st = max(elig, key=lambda x: (x[1]["exp"], x[1]["n"]))
    print("\n== FC: учёба, все 72 варианта (n, exp net R, Σ, gross) ==")
    for v, st in sorted(res, key=lambda x: -x[1]["exp"] if x[1]["n"] else 9):
        print(f"  {v.label():28s} n={st['n']:5d} exp={_fmt(st['exp'])} "
              f"Σ={st['sum']:+8.1f} gross={_fmt(st['gross'])}")
    print(f"\nвыбран на учёбе: {best_v.label()}  n={best_st['n']} exp={_fmt(best_st['exp'])}")

    def fc_eval(v: FCVar, lo: int, hi: int, label: str) -> dict:
        df = fc_run(syms, v, lo, hi)
        st = _stats(df)
        lng = _stats(df[df.side > 0]) if not df.empty else _stats(df)
        sht = _stats(df[df.side < 0]) if not df.empty else _stats(df)
        print(f"  {label:10s} n={st['n']:5d} exp={_fmt(st['exp'])} Σ={st['sum']:+8.1f} "
              f"DD={st['dd']:6.1f} gross={_fmt(st['gross'])} | LONG n={lng['n']} "
              f"exp={_fmt(lng['exp'])} | SHORT n={sht['n']} exp={_fmt(sht['exp'])}")
        return dict(df=df, st=st, lng=lng, sht=sht)

    if a.frozen:
        print("\n== FC: ЗАМОРОЗКА (одна оценка) ==")
        fc_eval(best_v, *FROZEN, "заморозка")
    else:
        print("\n== FC: выбранный вариант ==")
        fc_eval(best_v, *TRAIN, "учёба")
        t = fc_eval(best_v, *TEST, "проверка")
        halves_ok = True
        for name, lo, hi in HALVES:
            hst = fc_eval(best_v, lo, hi, name)["st"]
            halves_ok &= hst["n"] > 0 and hst["sum"] > 0
        neigh = []
        Ps = (0.90, 0.95, 0.98)
        i = Ps.index(best_v.P)
        for j in (i - 1, i + 1):
            if 0 <= j < len(Ps):
                nv = FCVar(Ps[j], best_v.oi, best_v.k, best_v.m, best_v.H)
                nst = _stats(fc_run(syms, nv, *TEST))
                neigh.append((nv, nst))
                print(f"  сосед {nv.label()}: n={nst['n']} exp={_fmt(nst['exp'])}")
        c1 = t["st"]["exp"] > 0
        c3 = all(nst["exp"] > 0 for _v, nst in neigh)
        c4 = t["st"]["n"] >= 100
        print(f"\nкритерий FC: exp>0 {c1}; каждое полугодие в плюсе {halves_ok}; "
              f"соседи в плюсе {c3}; n≥100 {c4}; SHORT отдельно >0 {t['sht']['exp'] > 0}")
        print(f"макс. одновременных позиций (проверка): {_max_concurrent(t['df'])}")
        if not t["df"].empty:
            print("причины выхода (проверка):", t["df"].why.value_counts().to_dict())
            print(f"средн. фандинг в R: {t['df'].fund.mean():+.3f}, издержки в R: "
                  f"{t['df'].cost.mean():+.3f}")
        print("\n== событийный разрез (P=0.95, без стопов/издержек), б.п. ==")
        for lab, (lo, hi) in (("учёба", TRAIN), ("проверка", TEST)):
            es = event_study(syms, 0.95, lo, hi)
            for side, nm in ((1, "LONG (толпа в шортах)"), (-1, "SHORT (толпа в лонгах)")):
                d = es[es.side == side] if not es.empty else es
                if d.empty:
                    continue
                cells = []
                for hh in (8, 24, 72):
                    x = d[f"r{hh}"]
                    tstat = x.mean() / (x.std(ddof=1) / math.sqrt(len(x))) if len(x) > 1 else 0
                    cells.append(f"{hh}ч {x.mean():+6.1f} (t={tstat:+.1f})")
                print(f"  {lab:9s} {nm:24s} n={len(d):4d}  " + "  ".join(cells))

    # ── CARRY
    print("\n== CARRY ==")
    names = [s.name for s in syms]
    c1_sets = {"C1 BTC": ["BTCUSDT"], "C1 ETH": ["ETHUSDT"], "C1 BTC+ETH": ["BTCUSDT", "ETHUSDT"]}
    c2_grid = [(E, X) for E in (0.10, 0.20, 0.40) for X in (0.0, 0.05)]
    best_c2 = None
    print("C2 на учёбе (капитал 1.5N; слот 1/27):")
    for E, X in c2_grid:
        ser = carry_portfolio(syms, names, *TRAIN, mode="timed", E=E, X=X, slots=len(UNIVERSE))
        r = carry_report(ser, *TRAIN, cap=1.5)
        print(f"  E={E:.0%} X={X:.0%}: годовых {r['apr']:+.2%}, просадка {r['dd']:.2%}")
        if r["dd"] <= 0.10 and (best_c2 is None or r["apr"] > best_c2[2]):
            best_c2 = (E, X, r["apr"])
    chosen = "нет допустимых" if best_c2 is None else f"E={best_c2[0]:.0%} X={best_c2[1]:.0%}"
    print(f"выбран C2: {chosen}")

    def carry_eval(label: str, ser_fn) -> None:
        rows = []
        segs = [("заморозка", *FROZEN, (FROZEN[1] - FROZEN[0]) / (365 * DAY_MS))] if a.frozen \
            else [("учёба", *TRAIN, None), ("проверка", *TEST, None)] + \
                 [(y, lo, hi, None) for y, lo, hi, _f in YEARS]
        for seg, lo, hi, _f in segs:
            ser = ser_fn(lo, hi)
            for cap in (1.5, 1.0):
                r = carry_report(ser, lo, hi, cap)
                rows.append(f"{seg} cap{cap:g}: {r['apr']:+.2%} год., просадка {r['dd']:.2%}")
        print(f"  {label}:\n    " + "\n    ".join(rows))

    for lab, nm in c1_sets.items():
        carry_eval(lab, lambda lo, hi, nm=nm: carry_portfolio(syms, nm, lo, hi, mode="always"))
    if best_c2 is not None:
        E, X, _ = best_c2
        carry_eval(f"C2 E={E:.0%} X={X:.0%}",
                   lambda lo, hi: carry_portfolio(syms, names, lo, hi, mode="timed", E=E, X=X,
                                                  slots=len(UNIVERSE)))


if __name__ == "__main__":
    main()
