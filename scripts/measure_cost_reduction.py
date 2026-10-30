"""Замер: снижение издержек — лимитный вход и шире минимальный стоп при трейле на 1m
(docs/measurements/2026-10-31-cost-reduction.md).

  DATA_ROOT=/путь/к/data nice -n 15 .venv/bin/python -m scripts.measure_cost_reduction <шаг>

Шаги по порядку (каждый пишет свой pickle в --work, следующий его читает):
  base   — паритет 0: база замера 30.10, E1m net/сд по половинам; сделки и исходы 1m-моделей;
  limit  — вариант L: лимит по entry (проход на тик), выход E1m, издержки мейкера;
  dump   — снимки 1h-сигналов с min_stop 1.6/2.0/2.5 и паритет генератора;
  stop   — вариант S: портфель на снимках, E1m, выбор на W1, проверка;
  combo  — L на снимке выбранного S (только если оба — кандидаты).

Минутки — общий кэш `scripts/fetch_m1.py` (замки на символ, темп щадящий: тот же IP у бота).
"""
from __future__ import annotations

import argparse
import json
import math
import pickle
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

import scripts.fetch_m1 as m1
import scripts.measure_one_position as mop
import scripts.measure_trail_realism as trt
import scripts.research_fine_judge as fj
import scripts.research_judge as rj
from app.brain.journal import MAX_WAIT_HOURS
from scripts.dump_history import cache_name

ROOT = trt.ROOT
HOUR, MIN = trt.HOUR, trt.MIN
HALVES = trt.HALVES
CHECK = ("W2", "rot H1", "rot H2")
ORDERS = ("color", "LH", "HL")
# паритет 0 — замер 30.10, E1m по цвету свечи
PARITY0 = {"W1": (1026, -0.065), "W2": (877, -0.057), "rot H1": (1292, -0.180),
           "rot H2": (1440, -0.004)}
# издержки, % цены: база M2′ — тейкер 0.055% × 2 + слип 0.15%; L — мейкер на входе
MAKER, TAKER = 0.02, 0.055
SLIP_EXIT = (0.05, 0.02, 0.10)          # основное значение и чувствительность
STOPS = (1.6, 2.0, 2.5)
TRUNC = pd.Timestamp("2024-06-01", tz="UTC")
THREADS_M1 = 2                          # докачка минуток: вдвое тише дефолта
EXT_STEP_H = 240


# ---------------------------------------------------------------- общие кирпичи

def _run_jobs(fn, jobs: list, procs: int) -> list:
    with Pool(procs) as pool:
        return pool.map(fn, jobs)


def stats(rows: list) -> dict:
    """rows: [(exit_ms, r_gross, r_net, event)] — как trt.stats."""
    return trt.stats(rows)


def table(title: str, by_half: dict) -> None:
    print(f"\n{title}")
    print(f"  {'половина':8s} {'n':>5s} {'gross/сд':>9s} {'net/сд':>8s} {'Σnet':>8s} {'DD':>6s}")
    for h in HALVES:
        s = by_half.get(h)
        if not s or not s.get("n"):
            print(f"  {h:8s}     0")
            continue
        print(f"  {h:8s} {s['n']:5d} {s['gross']:+9.3f} {s['net']:+8.3f} {s['sum']:+8.1f} "
              f"{s['dd']:6.1f}")


def candidate(var: dict, base: dict) -> tuple[bool, list]:
    """Критерий раздела 5: на каждой проверочной половине net > 0, n ≥ 0.5·n базы,
    DD ≤ 1.5·DD базы."""
    ok, marks = True, []
    for h in CHECK:
        v, b = var[h], base[h]
        c1 = v.get("n", 0) > 0 and v["net"] > 0
        c2 = v.get("n", 0) >= 0.5 * b["n"]
        c3 = v.get("n", 0) > 0 and v["dd"] <= 1.5 * b["dd"]
        ok &= c1 and c2 and c3
        marks.append(f"{h}: net>0 {'+' if c1 else '-'} n {'+' if c2 else '-'} "
                     f"DD {'+' if c3 else '-'}")
    return ok, marks


def save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(obj, f)


def load_pkl(path: Path):
    with open(path, "rb") as f:
        return pickle.load(f)


# ---------------------------------------------------------------- паритет 0 (база)

def step_base(a) -> None:
    print("== База замера 30.10 оригинальным путём (паритет 1–2 замера 30.10)")
    tabs, origs = [], {}
    for data in ("5y", "rot"):
        df, same, orig = trt.base(data)
        origs[data] = orig
        print(f"  {data}: реплика = оригинал: {same}; сделок в окне: {len(df)}")
        if not same:
            raise SystemExit("СТОП: реплика не совпала")
        tabs.append(df)
    if not rj.check_parity(origs):
        raise SystemExit("СТОП: паритет базы")
    trades = pd.concat(tabs, ignore_index=True)
    m1.ensure(trt.windows(trades), threads=THREADS_M1)
    jobs = [(data, "1m", var, order, trades[trades["data"] == data])
            for data in ("5y", "rot") for var in ("J", "E") for order in ORDERS]
    res: dict = {}
    for (_d, tf, var, order), out in _run_jobs(trt.run_model, jobs, a.procs):
        res.setdefault((tf, var, order), {}).update(out)
    trades["cov1m"] = trades["key"].map(trt.coverage_ok(trades))
    keys = trt.paired(trades, res, "1m")
    print("\n== Паритет 0: E1m по цвету свечи, net/сд на парном множестве 1m")
    ok = True
    by_half = {}
    for h in HALVES:
        part = trades[(trades["half"] == h) & trades["key"].isin(keys)]
        out = res[("1m", "E", "color")]
        by_half[h] = stats([(out[k][3], out[k][1], out[k][2], out[k][0]) for k in part["key"]])
        n_exp, net_exp = PARITY0[h]
        good = by_half[h]["n"] == n_exp and round(by_half[h]["net"], 3) == net_exp
        ok &= good
        print(f"  {h:7s} n={by_half[h]['n']} net/сд={by_half[h]['net']:+.4f} "
              f"(эталон {n_exp} / {net_exp:+.3f}) {'OK' if good else 'РАСХОЖДЕНИЕ'}")
    table("База E1m (color)", by_half)
    save(Path(a.work) / "base.pkl", {"trades": trades, "res": res, "keys": keys,
                                     "by_half": by_half})
    if not ok:
        raise SystemExit("СТОП: паритет 0 не совпал — варианты не запускаются")
    print("\nПаритет 0 пройден.")


def base_stats(base: dict, order: str = "color") -> dict:
    trades, res, keys = base["trades"], base["res"], base["keys"]
    out = res[("1m", "E", order)]
    by = {}
    for h in HALVES:
        part = trades[(trades["half"] == h) & trades["key"].isin(keys)]
        by[h] = stats([(out[k][3], out[k][1], out[k][2], out[k][0]) for k in part["key"]])
    return by


# ---------------------------------------------------------------- вариант L

_INST: dict | None = None


def tick_size(symbol: str, t_ms: int) -> float:
    """tickSize Bybit linear для id, торговавшего в момент t (релистинги — несколько id)."""
    global _INST
    if _INST is None:
        _INST = {}
        for x in json.loads((ROOT / "cache_univ" / "instruments.json").read_text()):
            if x.get("settleCoin") != "USDT" or x.get("contractType") != "LinearPerpetual":
                continue
            _INST.setdefault(x["baseCoin"], []).append(
                (int(x.get("launchTime") or 0), int(x.get("deliveryTime") or 0) or 2**62,
                 float(x["priceFilter"]["tickSize"])))
    ids = _INST.get(symbol.split("/")[0])
    if not ids:
        raise KeyError(f"нет tickSize для {symbol}")
    for lo, hi, tick in ids:
        if lo <= t_ms < hi:
            return tick
    return max(t for _lo, _hi, t in ids)


def limit_price(entry: float, side: str, tick: float) -> float:
    """LONG — вниз к тику, SHORT — вверх (заявка не хуже entry)."""
    q = entry / tick
    k = math.floor(q + 1e-9) if side == "LONG" else math.ceil(q - 1e-9)
    return round(k * tick, 12)


def judge_limit(entry: float, stop: float, side: str, start_ms: int, deadline_ms: int,
                bars: np.ndarray, *, tick: float | None, order: str = "color",
                act_r: float = 1.5, dist_r: float = 0.2, max_hold_h: float = 168.0) -> tuple:
    """Лимит по entry: исполнение — проход пути цены за цену заявки на тик (tick=None —
    режим паритета: касание entry, как fj.judge path). Выход — модель E с момента филла.

    -> (event, r_gross, exit_ms, fill_ms); ('nofill', ...) — прохода не было до deadline;
       ('unresolved', ...) — данные кончились раньше развязки."""
    risk = abs(entry - stop)
    sgn = 1.0 if side == "LONG" else -1.0
    if tick is None:
        x_fill = x_need = sgn * entry
    else:
        x_fill = sgn * limit_price(entry, side, tick)
        x_need = x_fill - tick + tick * 1e-6         # допуск на округление float у цены бара
    e_x = sgn * entry
    a_x = e_x + act_r * risk
    d = dist_r * risk
    level = sgn * stop
    ts = bars[:, 0]
    n = len(bars)
    i0 = int(np.searchsorted(ts, start_ms, "left"))
    filled = active = False
    peak = e_x
    prev = last_close = None
    end_ms = fill_ms = None
    for i in range(i0, n):
        t = int(ts[i])
        if not filled and t >= deadline_ms:
            return "nofill", None, None, None
        if filled and t >= end_ms:
            return "timeout", round((last_close - x_fill) / risk, 4), int(ts[i - 1]), fill_ms
        pts = [sgn * p for p in fj._points(bars[i, 1], bars[i, 2], bars[i, 3], bars[i, 4],
                                            order)]
        seq = ([prev] if prev is not None else []) + pts
        for k in range(len(seq) - 1):
            a, b = seq[k], seq[k + 1]
            if not filled:
                if tick is None:
                    if not (min(a, b) <= x_need <= max(a, b)):
                        continue
                    a = x_need
                elif a <= x_need:
                    pass                                   # уже за заявкой (гэп) — филл здесь
                elif b <= x_need:
                    a = x_need
                else:
                    continue
                filled = True
                fill_ms = t
                end_ms = t // HOUR * HOUR + int((max_hold_h + 1) * HOUR)
            if not active and a >= a_x:
                active, peak = True, a
                level = max(level, peak - d)
            if b < a:
                if b <= level:
                    ev = "trail" if active else "stopped"
                    return ev, round((level - x_fill) / risk, 4), t, fill_ms
            elif b > a:
                if active:
                    if b > peak:
                        peak = b
                        level = max(level, peak - d)
                elif b >= a_x:
                    active, peak = True, b
                    level = max(level, peak - d)
        prev = last_close = pts[-1]
        if filled and t + MIN >= end_ms:
            return "timeout", round((last_close - x_fill) / risk, 4), t, fill_ms
    return "unresolved", None, None, None


def _segment(bars: np.ndarray, cov: list, start_ms: int) -> np.ndarray:
    """Минутки непрерывного скачанного отрезка, содержащего start_ms (дыры кэша между
    окнами разных сделок иначе склеились бы в «тишину» без сделок)."""
    for s, e in cov:
        if s <= start_ms < e:
            lo = int(np.searchsorted(bars[:, 0], s, "left"))
            hi = int(np.searchsorted(bars[:, 0], e, "left"))
            return bars[lo:hi]
    return bars[:0]


def run_limit(job: tuple) -> dict:
    """(rows, order, parity) -> {key: (ev, rg, exit_ms, fill_ms)} по сделкам одного процесса."""
    rows, order, parity = job
    out, cache = {}, {}
    for r in rows:
        if r["symbol"] not in cache:
            cache[r["symbol"]] = (m1.load(r["symbol"]), m1.coverage(r["symbol"]))
        bars, cov = cache[r["symbol"]]
        seg = _segment(bars, cov, r["fill_ms"])
        if parity:
            out[r["key"]] = judge_limit(r["entry"], r["stop"], r["side"], r["fill_ms"],
                                        r["fill_ms"] + HOUR, seg, tick=None, order=order)
        else:
            deadline = int(r["decision"].value // 1_000_000) + int(MAX_WAIT_HOURS * HOUR)
            out[r["key"]] = judge_limit(r["entry"], r["stop"], r["side"], r["fill_ms"],
                                        deadline, seg, tick=tick_size(r["symbol"], r["fill_ms"]),
                                        order=order)
    return out


def _chunks(rows: list, k: int) -> list:
    rows = sorted(rows, key=lambda r: r["symbol"])
    return [rows[i::k] for i in range(k)]


def limit_all(trades: pd.DataFrame, order: str, procs: int, parity: bool = False) -> dict:
    """Исходы L на всех сделках; неразрешённые — докачка окна вперёд и повтор."""
    rows = trades[["key", "symbol", "side", "entry", "stop", "fill_ms",
                   "decision"]].to_dict("records")
    res: dict = {}
    for part in _run_jobs(run_limit, [(c, order, parity) for c in _chunks(rows, procs)],
                          procs):
        res.update(part)
    for _round in range(6):
        todo = [r for r in rows if res[r["key"]][0] == "unresolved"]
        if not todo or parity:
            break
        want: dict = {}
        for r in todo:
            deadline = int(r["decision"].value // 1_000_000) + int(MAX_WAIT_HOURS * HOUR)
            ce = next((e for s, e in m1.coverage(r["symbol"]) if s <= r["fill_ms"] < e),
                      r["fill_ms"])
            cap = deadline + int(170 * HOUR)
            if ce >= cap:
                continue
            want.setdefault(r["symbol"], []).append((ce, min(ce + EXT_STEP_H * HOUR, cap)))
        if not want:
            break
        print(f"  L: неразрешённых {len(todo)} — докачка вперёд ({len(want)} символов)")
        m1.ensure(want, threads=THREADS_M1)
        for part in _run_jobs(run_limit, [(c, order, parity) for c in _chunks(todo, procs)],
                              procs):
            res.update(part)
    return res


def cost_limit(row, fill_ms: int, exit_ms: int, slip_exit: float) -> float:
    risk = abs(row.entry - row.stop)
    held = int((exit_ms - fill_ms // HOUR * HOUR) // HOUR)
    return (row.entry * (MAKER + TAKER + slip_exit) / 100.0 / risk
            + trt.FUND / 10000.0 * row.entry / risk * held)


def limit_stats(trades: pd.DataFrame, res: dict, keys: set, slip_exit: float) -> dict:
    by = {}
    for h in HALVES:
        part = trades[(trades["half"] == h) & trades["key"].isin(keys)]
        rows = []
        for r in part.itertuples(index=False):
            ev, rg, ex, fl = res[r.key]
            if ev in ("nofill", "unresolved"):
                continue
            rows.append((ex, rg, rg - cost_limit(r, fl, ex, slip_exit), ev))
        by[h] = stats(rows)
    return by


def step_limit(a) -> None:
    base = load_pkl(Path(a.work) / "base.pkl")
    trades, res_b, keys = base["trades"], base["res"], base["keys"]
    part = trades[trades["key"].isin(keys)]
    print("== Паритет L-судьи: режим касания (без тика, окно — час филла журнала) = E1m")
    par = limit_all(part, "color", a.procs, parity=True)
    e1 = res_b[("1m", "E", "color")]
    # e1[k] = (ev, rg, rn, exit_ms); par[k] = (ev, rg, exit_ms, fill_ms)
    bad = [k for k in part["key"] if (par[k][0], par[k][1], par[k][2]) != (e1[k][0], e1[k][1],
                                                                            e1[k][3])]
    print(f"  совпали {len(part) - len(bad)}/{len(part)}; примеры: "
          f"{[(k, par[k], e1[k]) for k in bad[:3]]}")
    if bad:
        raise SystemExit("СТОП: L-судья в режиме касания не повторил E1m")
    bstats = base_stats(base)
    out: dict = {}
    for order in ORDERS:
        print(f"== L, порядок внутри минуты: {order}")
        out[order] = limit_all(part, order, a.procs)
        cnt = pd.Series([v[0] for v in out[order].values()]).value_counts().to_dict()
        print(f"  исходы: {cnt}")
    save(Path(a.work) / "limit.pkl", {"res": out})
    report_limit(trades, keys, out, res_b, bstats)


def report_limit(trades, keys, out, res_b, bstats) -> None:
    table("База E1m (color), M2′", bstats)
    main = limit_stats(trades, out["color"], keys, SLIP_EXIT[0])
    table(f"L (color), мейкер {MAKER}% + тейкер {TAKER}% + слип выхода {SLIP_EXIT[0]}%", main)
    print("\n  доля исполненных и отрицательный отбор (E1m базы, net/сд):")
    e1 = res_b[("1m", "E", "color")]
    for h in HALVES:
        part = trades[(trades["half"] == h) & trades["key"].isin(keys)]
        fills = [k for k in part["key"] if out["color"][k][0] not in ("nofill", "unresolved")]
        miss = [k for k in part["key"] if out["color"][k][0] == "nofill"]
        unr = [k for k in part["key"] if out["color"][k][0] == "unresolved"]
        fb = np.mean([e1[k][2] for k in fills]) if fills else float("nan")
        mb = np.mean([e1[k][2] for k in miss]) if miss else float("nan")
        print(f"  {h:7s} исполнено {len(fills)}/{len(part)} ({len(fills) / max(len(part), 1):.1%})"
              f"; не исполнено {len(miss)}, неразрешено {len(unr)}; E1m базы у исполненных "
              f"{fb:+.3f}, у неисполненных {mb:+.3f}")
    for se in SLIP_EXIT[1:]:
        table(f"L (color), чувствительность: слип выхода {se}%",
              limit_stats(trades, out["color"], keys, se))
    for order in ("LH", "HL"):
        table(f"L ({order}), слип выхода {SLIP_EXIT[0]}%",
              limit_stats(trades, out[order], keys, SLIP_EXIT[0]))
        table(f"База E1m ({order})", base_stats({"trades": trades, "res": res_b, "keys": keys},
                                                 order))
    ok, marks = candidate(main, bstats)
    print("\n  Критерий L: " + "; ".join(marks))
    print(f"  ВЕРДИКТ L: {'КАНДИДАТ' if ok else 'НЕ КАНДИДАТ'}")


# ---------------------------------------------------------------- снимки S

SIG5Y_BASE = ROOT / "cache5y" / "sig_prod_late_entry"
SIGROT_BASE = ROOT / "cache_univ" / "sig"


def snap_dir(data: str, stop: float) -> Path:
    if data == "5y" and stop == 2.0:
        return ROOT / "cache5y" / "sig_prod_late_entry_stop20"      # существующий снимок
    tag = f"sig_cr_stop{int(round(stop * 10))}_1h"
    return ROOT / ("cache5y" if data == "5y" else "cache_univ") / tag


def prod_cfg(min_stop: float):
    from app.brain.smc_setups import SMCConfig
    from scripts.dump_signals import _PROD_KEYS
    c = json.loads((SIG5Y_BASE / "config.json").read_text())["cfg"]
    cfg = SMCConfig(min_confidence=c["min_confidence"], min_impulse_atr=c["min_impulse_atr"],
                    min_rr_open_model=c["min_rr_open_model"],
                    enabled_setups=tuple(c["enabled_setups"]),
                    htf_block_countertrend=c["htf_block_countertrend"], min_stop_pct=min_stop,
                    late_entry_gate=c["late_entry_gate"], use_time_filter=c["use_time_filter"])
    for k in _PROD_KEYS:
        want = min_stop if k == "min_stop_pct" else c[k]
        got = getattr(cfg, k)
        got = list(got) if isinstance(got, tuple) else got
        if got != want:
            raise SystemExit(f"конфиг снимка: {k} = {got!r}, в базовом снимке {want!r}")
    return cfg


def rot_days() -> dict:
    mem = json.loads((ROOT / "cache_univ" / "membership_a.json").read_text())
    days: dict = {}
    for d, syms in mem.items():
        for s in syms:
            days.setdefault(s, []).append(pd.Timestamp(d, tz="UTC"))
    return days


def mask_for(data: str, sym: str, dt: pd.Series, days: list | None) -> np.ndarray:
    dtu = pd.to_datetime(dt, utc=True)
    if data == "5y":
        return (dtu >= TRUNC).to_numpy()
    mask = np.zeros(len(dtu), dtype=bool)
    for d in days:
        a = int(dtu.searchsorted(d - pd.Timedelta(days=1)))
        b = int(dtu.searchsorted(d + pd.Timedelta(days=1)))
        mask[a:b] = True
    return mask


def dump_one(job: tuple) -> tuple:
    """(data, sym, stop, out_dir, days) -> (sym, stop, n сигналов, n баров маски)."""
    from scripts.dump_signals import scan_signals
    data, sym, stop, out_dir, days = job
    src = ROOT / ("cache5y" if data == "5y" else "cache_univ")
    df = pd.read_csv(src / cache_name(sym, "1h"), parse_dates=["dt"])
    htf = pd.read_csv(src / cache_name(sym, "4h"), parse_dates=["dt"])
    mask = mask_for(data, sym, df["dt"], days)
    sigs = scan_signals(df, sym, "1h", prod_cfg(stop), 199, "4h", htf_bars=htf,
                        only=mask) if len(df) else []
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{cache_name(sym, '1h')[:-4]}.json"
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(sigs, ensure_ascii=False))
    tmp.rename(dest)
    return sym, stop, len(sigs), int(mask.sum())


def _in_mask(sigs: list, data: str, sym: str, days) -> list:
    src = ROOT / ("cache5y" if data == "5y" else "cache_univ")
    dt = pd.read_csv(src / cache_name(sym, "1h"), usecols=["dt"])["dt"]
    m = mask_for(data, sym, dt, days)
    return [s for s in sigs if m[s["bar_index"]]]


def step_dump(a) -> None:
    work = Path(a.work)
    days = rot_days()
    rot_syms = sorted(days)
    print("== Паритет генератора снимков (до вариантов)")
    checks = [("5y", "ZEC/USDT:USDT", 1.3, SIG5Y_BASE), ("5y", "SOL/USDT:USDT", 1.3, SIG5Y_BASE),
              ("5y", "NEAR/USDT:USDT", 2.0, snap_dir("5y", 2.0))]
    rng = np.random.default_rng(0)
    checks += [("rot", s, 1.3, SIGROT_BASE) for s in rng.choice(rot_syms, 5, replace=False)]
    jobs = [(d, s, st, str(work / "par" / f"{d}_{int(st * 10)}"), days.get(s)) for d, s, st, _r
            in checks]
    _run_jobs(dump_one, jobs, a.procs)
    ok = True
    for (d, s, st, ref), job in zip(checks, jobs):
        name = f"{cache_name(s, '1h')[:-4]}.json"
        mine = json.loads((Path(job[3]) / name).read_text())
        theirs = _in_mask(json.loads((ref / name).read_text()), d, s, days.get(s))
        good = mine == theirs
        ok &= good
        print(f"  {d} {s} min_stop {st}: моих {len(mine)}, в эталоне {len(theirs)} — "
              f"{'бит-в-бит' if good else 'РАСХОЖДЕНИЕ'}")
    if not ok:
        raise SystemExit("СТОП: генератор не повторил эталонные снимки")
    print("== Снимки вариантов (1h): 5y с 2024-06-01, ротация — дни членства ±1 сутки")
    jobs = []
    for st in STOPS:
        if st != 2.0:
            jobs += [("5y", f"{b}/USDT:USDT", st, str(snap_dir("5y", st)), None)
                     for b in mop.NINE]
        jobs += [("rot", s, st, str(snap_dir("rot", st)), days[s]) for s in rot_syms]
    done = 0
    with Pool(a.procs) as pool:
        for sym, st, n, nb in pool.imap_unordered(dump_one, jobs):
            done += 1
            if done % 50 == 0 or done == len(jobs):
                print(f"  {done}/{len(jobs)} (последний {sym} {st}: {n} сигн., {nb} баров)",
                      flush=True)
    print("Снимки готовы.")


# ---------------------------------------------------------------- вариант S

def _sigs_5y(sig_dir: Path) -> tuple[dict, list]:
    bars, sigs = {}, []
    for b in mop.NINE:
        sym = f"{b}/USDT:USDT"
        for tf in ("1h", "4h"):
            bars[(sym, tf)] = pd.read_csv(ROOT / "cache5y" / cache_name(sym, tf),
                                          parse_dates=["dt"])
        sigs += json.loads((sig_dir / f"{cache_name(sym, '1h')[:-4]}.json").read_text())
    sigs = [s for s in sigs if s["tf"] == "1h" and rj.utc(s["dt"]) >= TRUNC]
    return bars, sigs


def _sigs_rot(sig_dir: Path) -> tuple[dict, list]:
    import scripts.measure_rotating_universe as mru
    mem = {pd.Timestamp(d, tz="UTC"): v
           for d, v in json.loads((ROOT / "cache_univ" / "membership_a.json").read_text()).items()}
    old = mru._signals.__defaults__
    mru._signals.__defaults__ = (sig_dir,)
    try:
        sigs = [s for s in mru.members_signals(mem) if s["tf"] == "1h"]
    finally:
        mru._signals.__defaults__ = old
    bars = {}
    for sym in sorted({s["symbol"] for s in sigs}):
        for tf in ("1h", "4h"):
            bars[(sym, tf)] = pd.read_csv(ROOT / "cache_univ" / cache_name(sym, tf),
                                          parse_dates=["dt"])
    return bars, sigs


def trades_of(data: str, bars: dict, sigs: list) -> pd.DataFrame:
    """Сделки портфеля (часовой судья — состав, как у базы) в формате trt.base."""
    rj.DIAG = {}
    rj.install(hook=rj.NoopHook())
    rep = rj.simulate(bars, sigs)
    rj.uninstall()
    diag, rj.DIAG = rj.DIAG, None
    by_key = {rj.key(s["symbol"], s["dt"], s["side"], s["setup"]): s for s in sigs}
    cut = mop.SPLIT5Y if data == "5y" else mop.SPLIT2Y
    rows = []
    for t in rep.detail:
        k = rj.key(t.symbol, t.signal_dt, t.side, t.setup)
        s, d = by_key[k], diag[k]
        decision = rj.utc(t.signal_dt) + pd.Timedelta(hours=1)
        half = (("W1" if decision < cut else "W2") if data == "5y"
                else ("rot H1" if decision < cut else "rot H2"))
        rows.append(dict(data=data, key=(data, *k), half=half, symbol=t.symbol, side=t.side,
                         entry=float(s["entry"]), stop=float(s["stop"]), decision=decision,
                         fill_ms=trt._ms(t.fill_dt), exit_ms=trt._ms(t.exit_dt),
                         ev_1h=d["event"], rg_1h=d["r_gross"], cost_1h=d["cost"],
                         rn_1h=float(t.result_r)))
    df = pd.DataFrame(rows)
    return df[df["fill_ms"] >= trt._ms(trt.W_START)].reset_index(drop=True)


def judge_e1m(trades: pd.DataFrame, procs: int) -> dict:
    """E1m по трём порядкам; неразрешённые в окне — докачка как в замере 30.10."""
    m1.ensure(trt.windows(trades), threads=THREADS_M1)
    jobs = [(data, "1m", "E", order, trades[trades["data"] == data])
            for data in ("5y", "rot") for order in ORDERS if (trades["data"] == data).any()]
    res: dict = {}
    for (_d, tf, var, order), out in _run_jobs(trt.run_model, jobs, procs):
        res.setdefault((tf, var, order), {}).update(out)
    probe = {k for out in res.values() for k, v in out.items() if v[0] == "unresolved"}
    if probe:
        print(f"  не разрешились в окне: {len(probe)} — докачка +{trt.EXTEND_H}ч")
        sub = trades[trades["key"].isin(probe)]
        m1.ensure(trt.windows(sub, probe), threads=THREADS_M1)
        jobs = [(data, "1m", "E", order, sub[sub["data"] == data])
                for data in ("5y", "rot") for order in ORDERS if (sub["data"] == data).any()]
        for (_d, tf, var, order), out in _run_jobs(trt.run_model, jobs, procs):
            res[(tf, var, order)].update(out)
    return res


def e1m_keys(trades: pd.DataFrame, res: dict) -> set:
    """Разрешены всеми тремя порядками E1m и покрытие минутками ≥ 95%."""
    good = set(trades["key"])
    for out in res.values():
        good &= {k for k, v in out.items() if v[1] is not None}
    cov = trades["key"].map(trt.coverage_ok(trades))
    return good & set(trades.loc[cov >= 0.95, "key"])


def e1m_stats(trades, res, keys, order="color") -> dict:
    out = res[("1m", "E", order)]
    by = {}
    for h in HALVES:
        part = trades[(trades["half"] == h) & trades["key"].isin(keys)]
        by[h] = stats([(out[k][3], out[k][1], out[k][2], out[k][0]) for k in part["key"]])
    return by


def step_stop(a) -> None:
    work = Path(a.work)
    base = load_pkl(work / "base.pkl")
    bstats = base_stats(base)
    print("== Паритет усечения: база 5y на сигналах с 2024-06-01 = полная база в окне")
    bars, sigs = _sigs_5y(SIG5Y_BASE)
    tr = trades_of("5y", bars, sigs)
    full = base["trades"][base["trades"]["data"] == "5y"]
    cols = ["key", "fill_ms", "exit_ms", "rn_1h"]
    a1 = tr[cols].sort_values("key").reset_index(drop=True)
    b1 = full[cols].sort_values("key").reset_index(drop=True)
    same = a1.equals(b1)
    print(f"  сделок в окне: усечённая {len(a1)}, полная {len(b1)} — "
          f"{'совпали' if same else 'РАСХОЖДЕНИЕ'}")
    if not same:
        raise SystemExit("СТОП: паритет усечения")
    var: dict = {}
    for st in STOPS:
        print(f"\n== S: min_stop {st}%")
        parts = []
        for data, loader in (("5y", _sigs_5y), ("rot", _sigs_rot)):
            b, s = loader(snap_dir(data, st))
            parts.append(trades_of(data, b, s))
            print(f"  {data}: сигналов {len(s)}, сделок в окне {len(parts[-1])}")
        trades = pd.concat(parts, ignore_index=True)
        res = judge_e1m(trades, a.procs)
        keys = e1m_keys(trades, res)
        var[st] = {"trades": trades, "res": res, "keys": keys}
        table(f"S {st}% — E1m (color)", e1m_stats(trades, res, keys))
    save(work / "stop.pkl", var)
    report_stop(var, bstats)


def report_stop(var: dict, bstats: dict) -> None:
    table("База E1m (color), min_stop 1.3%", bstats)
    w1 = {st: e1m_stats(v["trades"], v["res"], v["keys"])["W1"] for st, v in var.items()}
    for st, s in w1.items():
        print(f"  W1 min_stop {st}%: n={s['n']} net/сд {s['net']:+.4f}")
    pick = max(w1, key=lambda st: w1[st]["net"])
    print(f"  Выбор на W1: min_stop {pick}%")
    v = var[pick]
    main = e1m_stats(v["trades"], v["res"], v["keys"])
    for order in ("LH", "HL"):
        table(f"S {pick}% — E1m ({order})", e1m_stats(v["trades"], v["res"], v["keys"], order))
    ok, marks = candidate(main, bstats)
    print("\n  Критерий S: " + "; ".join(marks))
    print(f"  ВЕРДИКТ S ({pick}%): {'КАНДИДАТ' if ok else 'НЕ КАНДИДАТ'}")


# ---------------------------------------------------------------- L+S

def step_combo(a) -> None:
    work = Path(a.work)
    var = load_pkl(work / "stop.pkl")
    pick = max(var, key=lambda st: e1m_stats(var[st]["trades"], var[st]["res"],
                                             var[st]["keys"])["W1"]["net"])
    v = var[pick]
    part = v["trades"][v["trades"]["key"].isin(v["keys"])]
    out = limit_all(part, "color", a.procs)
    base = load_pkl(work / "base.pkl")
    bstats = base_stats(base)
    main = limit_stats(v["trades"], out, v["keys"], SLIP_EXIT[0])
    table(f"L+S ({pick}%), color", main)
    ok, marks = candidate(main, bstats)
    print("\n  Критерий L+S: " + "; ".join(marks))
    print(f"  ВЕРДИКТ L+S: {'КАНДИДАТ' if ok else 'НЕ КАНДИДАТ'}")
    save(work / "combo.pkl", {"pick": pick, "res": out})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=("base", "limit", "dump", "stop", "combo"))
    ap.add_argument("--work", default="out_cost_reduction")
    ap.add_argument("--procs", type=int, default=4)
    a = ap.parse_args()
    {"base": step_base, "limit": step_limit, "dump": step_dump, "stop": step_stop,
     "combo": step_combo}[a.step](a)


if __name__ == "__main__":
    main()
