"""Замер «SMT-дивергенция BTC/ETH на снятии ликвидности».

Регистрация и результат — docs/measurements/2026-10-31-smt-divergence.md.

Сигнальные 15m/1h/4h собираются из минуток Bybit, исполнение — по тем же минуткам (худший
порядок внутри минуты). Плечо SMT (опорный актив свой минимум не снял) против плеча плацебо
(снял) — отдельными портфелями по одной позиции.

  DATA_ROOT="/Users/mac/bot ctypto/data" nice -n 15 .venv/bin/python -m scripts.research_smt fetch
  DATA_ROOT="/Users/mac/bot ctypto/data" nice -n 15 .venv/bin/python -m scripts.research_smt run
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

MIN = 60_000
H = 60 * MIN
START = {"BTC": "2021-03-15", "ETH": "2021-03-15", "SOL": "2021-10-15"}
END = "2026-10-30"
TRAIN = ("2021-03-15", "2024-01-01")
VALID = ("2024-01-01", "2026-08-01")
FROZEN = ("2026-08-01", "2026-10-30")
HALVES = [("2024H1", "2024-01-01", "2024-07-01"), ("2024H2", "2024-07-01", "2025-01-01"),
          ("2025H1", "2025-01-01", "2025-07-01"), ("2025H2", "2025-07-01", "2026-01-01"),
          ("2026-01…07", "2026-01-01", "2026-08-01")]
FEE = 0.00055
SLIP = 0.0002
TIME_STOP = 24 * H
ENTRY_BARS = 4            # бар снятия и ещё три
ATR_N = 14
TP_R = 3.0
VARIANTS = {"V1": ("1h", "BTC", "ETH"), "V2": ("1h", "ETH", "BTC"),
            "V3": ("15m", "BTC", "ETH"), "V4": ("15m", "ETH", "BTC")}
EXTRA = {"X1": ("1h", "ETH", "SOL"), "X2": ("1h", "SOL", "ETH"),
         "X3": ("15m", "ETH", "SOL"), "X4": ("15m", "SOL", "ETH")}
TF_MS = {"15m": 15 * MIN, "1h": H, "4h": 4 * H}


def ms(d: str) -> int:
    return int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp() * 1000)


def data_root() -> Path:
    return Path(os.environ.get("DATA_ROOT", "data"))


def out_dir() -> Path:
    p = data_root() / "cache_research" / "smt"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------- данные

def cmd_fetch() -> None:
    from scripts import fetch_m1
    win = {f"{c}/USDT:USDT": [(ms(START[c]), ms(END))] for c in START}
    fetch_m1.ensure(win, threads=1)


def load_m1(coin: str) -> np.ndarray:
    from scripts import fetch_m1
    a = fetch_m1.load(f"{coin}/USDT:USDT")
    lo, hi = ms(START[coin]), ms(END)
    return a[(a[:, 0] >= lo) & (a[:, 0] < hi)]


def resample(m1: np.ndarray, tf_ms: int) -> pd.DataFrame:
    """Бары [open, open+ТФ) из минуток: ts, o, h, l, c, n (число минуток в баре)."""
    b = (m1[:, 0] // tf_ms).astype(np.int64)
    cut = np.nonzero(np.diff(b))[0] + 1
    starts = np.r_[0, cut]
    ends = np.r_[cut, len(b)]
    df = pd.DataFrame({
        "ts": b[starts] * tf_ms,
        "o": m1[starts, 1],
        "h": np.maximum.reduceat(m1[:, 2], starts),
        "l": np.minimum.reduceat(m1[:, 3], starts),
        "c": m1[ends - 1, 4],
        "n": ends - starts,
    })
    return df


def swings(b4: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Фракталы 2/2 на 4h. -> (время подтверждения, уровень) для минимумов и максимумов.
    Подтверждение — закрытие бара i+2, то есть открытие бара i+3."""
    lo, hi, ts = b4["l"].to_numpy(), b4["h"].to_numpy(), b4["ts"].to_numpy()
    sl_t, sl_v, sh_t, sh_v = [], [], [], []
    for i in range(2, len(b4) - 2):
        if lo[i] < lo[i - 2] and lo[i] < lo[i - 1] and lo[i] < lo[i + 1] and lo[i] < lo[i + 2]:
            sl_t.append(ts[i + 2] + 4 * H)
            sl_v.append(lo[i])
        if hi[i] > hi[i - 2] and hi[i] > hi[i - 1] and hi[i] > hi[i + 1] and hi[i] > hi[i + 2]:
            sh_t.append(ts[i + 2] + 4 * H)
            sh_v.append(hi[i])
    return np.array(sl_t), np.array(sl_v), np.array(sh_t), np.array(sh_v)


def last_level(t_conf: np.ndarray, v: np.ndarray, at: int) -> float | None:
    """Последний подтверждённый уровень на момент at (подтверждение ≤ at)."""
    k = np.searchsorted(t_conf, at, side="right") - 1
    return float(v[k]) if k >= 0 else None


# ---------------------------------------------------------------- события

@dataclass
class Ev:
    side: int            # +1 лонг, −1 шорт
    t_sweep: int         # открытие бара снятия
    t_entry: int         # закрытие бара входа = время входа
    stop: float
    smt: bool


def events(tf: str, a: str, b: str, bars: dict, sw: dict) -> list[Ev]:
    """События снятия 4h-уровней актива a на сигнальном ТФ, с меткой SMT/плацебо по b."""
    tfm = TF_MS[tf]
    A, B = bars[(a, tf)], bars[(b, tf)].set_index("ts")
    sa, sb = sw[a], sw[b]
    ts, o, h, lw, c = (A[k].to_numpy() for k in ("ts", "o", "h", "l", "c"))
    tr = np.maximum(h - lw, np.maximum(np.abs(h - np.r_[c[0], c[:-1]]),
                                        np.abs(lw - np.r_[c[0], c[:-1]])))
    atr = pd.Series(tr).rolling(ATR_N).mean().to_numpy()
    out: list[Ev] = []
    for side in (+1, -1):
        t_conf, val = (sa[0], sa[1]) if side > 0 else (sa[2], sa[3])
        tb_conf, vb = (sb[0], sb[1]) if side > 0 else (sb[2], sb[3])
        k_lvl, used = -1, True          # активный уровень: последний подтверждённый, не снятый
        i = 0
        while i < len(ts):
            k = np.searchsorted(t_conf, ts[i], side="right") - 1
            if k != k_lvl:                      # подтвердился новый уровень
                k_lvl, used = k, False
            if k_lvl < 0 or used:
                i += 1
                continue
            lvl = val[k_lvl]
            swept = lw[i] < lvl if side > 0 else h[i] > lvl
            if not swept:
                i += 1
                continue
            used = True                         # уровень снимается один раз
            e = None
            for j in range(i, min(i + ENTRY_BARS, len(ts))):
                if (c[j] > lvl) if side > 0 else (c[j] < lvl):
                    e = j
                    break
            if e is None or np.isnan(atr[e]):
                i += 1
                continue
            lb = last_level(tb_conf, vb, int(ts[i]))
            if lb is None:
                i += 1
                continue
            w = B.loc[ts[max(i - 1, 0)]:ts[e]]
            if len(w) < (e - max(i - 1, 0) + 1):    # дыра в опорном активе — событие не судим
                i += 1
                continue
            b_swept = (w["l"].min() < lb) if side > 0 else (w["h"].max() > lb)
            ext = lw[i:e + 1].min() if side > 0 else h[i:e + 1].max()
            stop = ext - 0.1 * atr[e] if side > 0 else ext + 0.1 * atr[e]
            out.append(Ev(side, int(ts[i]), int(ts[e]) + tfm, float(stop), not b_swept))
            i += 1
    out.sort(key=lambda x: x.t_entry)
    return out


# ---------------------------------------------------------------- исполнение

def funding_series(coin: str) -> tuple[np.ndarray, np.ndarray]:
    f = pd.read_csv(data_root() / "cache_research" / "funding" / f"{coin}USDT_funding.csv")
    return f["ts"].to_numpy(np.int64), f["rate"].to_numpy(float)


def simulate(evs: list[Ev], coin: str, m1: np.ndarray, sw: dict, fund) -> pd.DataFrame:
    """Одна позиция за раз; минутки, худший порядок. -> сделки с net R."""
    ts, o, h, lw = m1[:, 0].astype(np.int64), m1[:, 1], m1[:, 2], m1[:, 3]
    f_ts, f_rate = fund
    rows, busy_until = [], -1
    for ev in evs:
        if ev.t_entry <= busy_until:
            continue
        i0 = np.searchsorted(ts, ev.t_entry)
        if i0 >= len(ts) or ts[i0] != ev.t_entry:
            rows.append(dict(t=ev.t_entry, side=ev.side, smt=ev.smt, status="no_m1"))
            continue
        side = ev.side
        entry = o[i0] * (1 + SLIP * side)
        risk = (entry - ev.stop) * side
        if risk <= 0:
            rows.append(dict(t=ev.t_entry, side=side, smt=ev.smt, status="bad_risk"))
            continue
        hi_lvl = (last_level(sw[2], sw[3], ev.t_entry) if side > 0
                  else last_level(sw[0], sw[1], ev.t_entry))
        tgt = entry + side * TP_R * risk
        if hi_lvl is not None and (hi_lvl - entry) * side > 0:
            tgt = hi_lvl if (hi_lvl - tgt) * side < 0 else tgt
        i1 = np.searchsorted(ts, ev.t_entry + TIME_STOP)
        seg = slice(i0, min(i1 + 1, len(ts)))
        sts = ts[seg]
        if ts[-1] < ev.t_entry + TIME_STOP:              # данные кончились раньше тайм-стопа
            rows.append(dict(t=ev.t_entry, side=side, smt=ev.smt, status="open_at_end"))
            continue
        if len(sts) < 2 or (np.diff(sts) > 5 * MIN).any():
            rows.append(dict(t=ev.t_entry, side=side, smt=ev.smt, status="gap"))
            continue
        so, sh, sl = o[seg], h[seg], lw[seg]
        hit_stop = (sl <= ev.stop) if side > 0 else (sh >= ev.stop)
        hit_tgt = (sh >= tgt) if side > 0 else (sl <= tgt)
        n = len(sts)
        k_ts = np.searchsorted(sts, ev.t_entry + TIME_STOP)      # минута тайм-стопа
        k_s = int(np.argmax(hit_stop)) if hit_stop.any() else n
        k_t = int(np.argmax(hit_tgt)) if hit_tgt.any() else n
        k = min(k_s, k_t, k_ts)
        if k >= n:
            rows.append(dict(t=ev.t_entry, side=side, smt=ev.smt, status="gap"))
            continue
        if k == k_ts and k < min(k_s, k_t):
            raw, why = so[k], "time"
        elif k_s <= k_t:                                        # стоп раньше цели в той же минуте
            gap = (so[k] < ev.stop) if side > 0 else (so[k] > ev.stop)
            raw, why = (so[k] if gap else ev.stop), "stop"
        else:
            raw, why = tgt, "target"
        exit_px = raw * (1 - SLIP * side)
        t_exit = int(sts[k])
        gross = (exit_px - entry) * side / risk
        fees = FEE * (entry + exit_px) / risk
        fm = (f_ts > ev.t_entry) & (f_ts <= t_exit)
        fcost = float(f_rate[fm].sum()) * entry * side / risk   # лонг платит при rate > 0
        rows.append(dict(t=ev.t_entry, t_exit=t_exit, side=side, smt=ev.smt, status="ok",
                         why=why, risk_pct=risk / entry * 100, gross=gross,
                         net=gross - fees - fcost, fees=fees, funding=fcost))
        busy_until = t_exit
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- статистика

def dd(r: np.ndarray) -> float:
    if not len(r):
        return 0.0
    eq = np.cumsum(r)
    return float((np.maximum.accumulate(np.r_[0, eq])[1:] - eq).max())


def part(df: pd.DataFrame, lo: str, hi: str) -> pd.DataFrame:
    return df[(df["t"] >= ms(lo)) & (df["t"] < ms(hi))]


def summ(df: pd.DataFrame) -> dict:
    r = df["net"].to_numpy()
    return dict(n=int(len(r)), net=float(r.mean()) if len(r) else 0.0,
                gross=float(df["gross"].mean()) if len(r) else 0.0, sum=float(r.sum()),
                dd=dd(r), win=float((r > 0).mean()) if len(r) else 0.0)


def perm_p(a: np.ndarray, b: np.ndarray, n: int = 10_000, seed: int = 7) -> float:
    """Односторонний перестановочный тест: P(mean(a) − mean(b) ≥ наблюдаемого)
    при перемешивании меток."""
    rng = np.random.default_rng(seed)
    obs = a.mean() - b.mean()
    pool = np.r_[a, b]
    na = len(a)
    hits = 0
    for _ in range(n):
        rng.shuffle(pool)
        if pool[:na].mean() - pool[na:].mean() >= obs:
            hits += 1
    return (hits + 1) / (n + 1)


def cmd_run() -> None:
    coins = ["BTC", "ETH", "SOL"]
    m1 = {c: load_m1(c) for c in coins}
    for c in coins:
        d = np.diff(m1[c][:, 0])
        first = datetime.utcfromtimestamp(m1[c][0, 0] / 1000)
        print(f"{c}: минуток {len(m1[c])}, с {first:%Y-%m-%d}, "
              f"дыр > 5 мин: {(d > 5 * MIN).sum()}", flush=True)
    bars = {(c, tf): resample(m1[c], TF_MS[tf]) for c in coins for tf in ("15m", "1h", "4h")}
    sw = {c: swings(bars[(c, "4h")]) for c in coins}
    fund = {c: funding_series(c) for c in coins}
    res: dict = {}
    for name, (tf, a, b) in {**VARIANTS, **EXTRA}.items():
        evs = events(tf, a, b, bars, sw)
        trades = {}
        for arm in ("smt", "placebo"):
            sub = [e for e in evs if e.smt == (arm == "smt")]
            t = simulate(sub, a, m1[a], sw[a], fund[a])
            t.to_csv(out_dir() / f"trades_{name}_{arm}.csv", index=False)
            trades[arm] = t
        res[name] = trades
        ok = {arm: trades[arm][trades[arm]["status"] == "ok"] for arm in trades}
        bad = {arm: trades[arm]["status"].value_counts().to_dict() for arm in trades}
        n_smt = sum(e.smt for e in evs)
        print(f"{name} {tf} {a}/{b}: событий {len(evs)} (SMT {n_smt}), статусы {bad}",
              flush=True)
        for arm in ("smt", "placebo"):
            s_tr, s_va = summ(part(ok[arm], *TRAIN)), summ(part(ok[arm], *VALID))
            print(f"   {arm:8s} учёба n={s_tr['n']} net={s_tr['net']:+.3f} "
                  f"gross={s_tr['gross']:+.3f} | проверка n={s_va['n']} "
                  f"net={s_va['net']:+.3f} gross={s_va['gross']:+.3f} "
                  f"ΣR={s_va['sum']:+.1f}", flush=True)
    # выбор на учёбе
    cand = {}
    for name in VARIANTS:
        t = res[name]["smt"]
        s = summ(part(t[t["status"] == "ok"], *TRAIN))
        if s["n"] >= 100:
            cand[name] = s["net"]
    pick = max(cand, key=cand.get) if cand else None
    report: dict = {"selection_train_net": cand, "pick": pick, "variants": {}}
    for name in {**VARIANTS, **EXTRA}:
        ok = {arm: res[name][arm][res[name][arm]["status"] == "ok"] for arm in ("smt", "placebo")}
        rep = {}
        for arm in ("smt", "placebo"):
            rep[arm] = {"train": summ(part(ok[arm], *TRAIN)), "valid": summ(part(ok[arm], *VALID)),
                        "halves": {h: summ(part(ok[arm], lo, hi)) for h, lo, hi in HALVES}}
        va_s = part(ok["smt"], *VALID)["net"].to_numpy()
        va_p = part(ok["placebo"], *VALID)["net"].to_numpy()
        rep["diff_valid"] = float(va_s.mean() - va_p.mean()) if len(va_s) and len(va_p) else None
        rep["perm_p"] = perm_p(va_s, va_p) if len(va_s) and len(va_p) else None
        top = np.sort(va_s)[::-1]
        rep["valid_sum_wo_top5"] = float(va_s.sum() - top[:5].sum()) if len(va_s) else None
        report["variants"][name] = rep
    if pick:
        r = report["variants"][pick]
        crit = {
            "1_net_all_halves": r["smt"]["valid"]["net"] > 0 and all(
                r["smt"]["halves"][h]["net"] > 0 for h, _, _ in HALVES),
            "2_smt_minus_placebo": (r["diff_valid"] or 0) > 0 and (r["perm_p"] or 1) < 0.0125,
            "3_n_valid_ge_100": r["smt"]["valid"]["n"] >= 100,
            "4_wo_top5_positive": (r["valid_sum_wo_top5"] or 0) > 0,
        }
        report["criteria"] = crit
        report["verdict"] = "кандидат" if all(crit.values()) else "не кандидат"
        t = res[pick]["smt"]
        report["frozen"] = summ(part(t[t["status"] == "ok"], *FROZEN))
    (out_dir() / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    keys = ("selection_train_net", "pick", "criteria", "verdict", "frozen")
    print(json.dumps({k: report[k] for k in keys if k in report}, ensure_ascii=False, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["fetch", "run"])
    a = ap.parse_args()
    cmd_fetch() if a.cmd == "fetch" else cmd_run()


if __name__ == "__main__":
    main()
