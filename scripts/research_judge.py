"""Исследовательская реплика живого судьи — ТОЛЬКО для замеров управления сделкой.

Прод её не импортирует и не меняется: единственный судья журнала — по-прежнему
`journal._outcome_from_bars`. Здесь та же логика на numpy (без iterrows, ×10 быстрее) плюс
выключенные по умолчанию опции замеров. С выключенными опциями реплика обязана давать
сделки бит-в-бит с `app.sim.portfolio._live_outcome` — каждый замер проверяет это сам
(`parity`: прогон оригинальным путём против реплики, сделка-к-сделке).

`install(**opts)` подменяет `app.sim.portfolio._live_outcome` В ТЕКУЩЕМ ПРОЦЕССЕ замера;
`simulate_portfolio` (cap, тормоз, дедуп, окно филла) работает без изменений.

Пути к кэшам: скрипты замеров читают `data/...` относительно cwd. Из git-worktree кэши
главного чекаута подключаются переменной окружения DATA_ROOT (только чтение).
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd

import app.sim.portfolio as _pf
from app.brain.journal import MAX_WAIT_HOURS
from app.brain.smc_setups import SMCEngine
from app.sim.backtest import _cost_r

HOUR = pd.Timedelta(hours=1)
_ORIG_LIVE_OUTCOME = _pf._live_outcome
OPTS: dict = {}                    # опции варианта (пусто = поведение живого судьи)
DIAG: dict | None = None           # {ключ сигнала: диагностика} — если включено


def rebind_data() -> Path:
    """DATA_ROOT=/abs/path/data — читать кэши оттуда (worktree без своих кэшей)."""
    import scripts.measure_one_position as mop
    import scripts.measure_rotating_universe as mru
    root = Path(os.environ.get("DATA_ROOT", "data"))
    mop.CACHE5Y, mop.SIG5Y = root / "cache5y", root / "cache5y/sig_prod_late_entry"
    mop.UNIV = mru.OUT = root / "cache_univ"
    mru.SIG = mru.OUT / "sig"
    mru._signals.__defaults__ = (mru.SIG,)
    mru._bars.__defaults__ = (mru.OUT,)
    return root


def utc(v) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def key(symbol: str, dt, side: str, setup: str) -> tuple:
    return symbol, utc(dt).isoformat(), side, setup


_ARR: dict = {}


def arrays(df: pd.DataFrame) -> tuple:
    """(dt64 UTC, open, high, low, close) по id кадра; ссылка на кадр держится в кэше,
    чтобы id не переиспользовался после сборки мусора."""
    hit = _ARR.get(id(df))
    if hit is not None and hit[0] is df:
        return hit[1]
    dts = pd.to_datetime(df["dt"], utc=True).to_numpy(dtype="datetime64[ns]")
    arr = (dts,) + tuple(df[c].to_numpy(float) for c in ("open", "high", "low", "close"))
    _ARR[id(df)] = (df, arr)
    return arr


def _ts(v: np.datetime64) -> pd.Timestamp:
    return pd.Timestamp(v).tz_localize("UTC")


def judge(sig: dict, df1h: pd.DataFrame, idx1h=None, *, fee_pct: float, slippage_pct: float,
          funding_bps_per_bar: float, trail_activate_r: float | None,
          trail_distance_r: float, tp_exit: bool, max_hold_hours: float,
          fill_through_frac: float = 0.0, extra_cost_r: float = 0.0,
          gap_open: bool = False, be_trigger_r: float = 0.0, be_lock_r: float = 0.0):
    """Контракт `app.sim.portfolio._live_outcome` -> (kind, r_net|None, fill_dt, closed_dt) | None.

    Поддержан только путь прода (выход трейлом без тейка, вход касанием); исследовательские
    поля оригинала (through/gap/BE) здесь запрещены — их меряют оригиналом."""
    if (tp_exit or not trail_activate_r or fill_through_frac or extra_cost_r or gap_open
            or be_trigger_r or be_lock_r):
        raise ValueError("research_judge: поддержан только путь прода")
    entry, stop, side = sig["entry"], sig["stop"], sig["side"]
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    dts, op, hi, lo, cl = arrays(df1h)
    created = utc(sig["dt"])
    decision = created + pd.Timedelta(minutes=SMCEngine.TF_MINUTES.get(sig["tf"], 60))
    dec64 = decision.to_datetime64()
    start = int(np.searchsorted(dts, (decision - HOUR).to_datetime64(), "left"))
    span = int(MAX_WAIT_HOURS + (max_hold_hours if max_hold_hours > 0 else 24 * 40)) + 8
    end = min(start + span, len(dts))
    i0 = max(start, int(np.searchsorted(dts, dec64, "left")))
    if i0 >= end:
        return None
    # --- ожидание касания: истечение проверяется ДО касания (бар ≥ решение + 50ч) ---
    lim = dec64 + np.timedelta64(int(MAX_WAIT_HOURS * 3600), "s")
    k_exp = i0 + int(np.searchsorted(dts[i0:end], lim, "left"))
    touch = np.nonzero((lo[i0:k_exp] <= entry) & (entry <= hi[i0:k_exp]))[0]
    if not touch.size:
        if k_exp < end:
            return "expired", None, None, decision + pd.Timedelta(hours=MAX_WAIT_HOURS)
        return None
    f = i0 + int(touch[0])
    res = _manage(sig, side, entry, stop, risk, f, end, dts, op, hi, lo, cl,
                  trail_activate_r, trail_distance_r, max_hold_hours)
    if res is None:
        return None
    event, r, j, extra = res
    fill, closed_dt = _ts(dts[f]), _ts(dts[j])
    held_h = max(0.0, (closed_dt - fill).total_seconds() / 3600.0)
    cost = (_cost_r(sig["entry"], sig["stop"], fee_pct, slippage_pct)
            + funding_bps_per_bar / 10000.0 * sig["entry"] / risk * int(held_h)
            + extra_cost_r)
    r_net = float(r) - cost - extra.get("cost_r", 0.0)
    if DIAG is not None:
        DIAG[key(sig["symbol"], sig["dt"], side, sig["setup"])] = {
            "event": event, "r_gross": float(r), "cost": cost, **extra}
    return f"{event}-stop", r_net, fill, closed_dt


def _manage(sig, side, entry, stop, risk, f, end, dts, op, hi, lo, cl,
            act_r, dist_r, max_hold_h):
    """Позиция от бара филла f -> (event, r_gross, j_exit, extra) | None (не разрешилась).
    Порядок внутри бара — как у живого судьи: стоп текущего бара (уровень с прошлого
    бара) → [правило варианта, OPTS['hook']] → подтяжка трейла (не на баре филла) →
    потолок удержания по close.

    hook (только исследования): start(sig, entry, stop, risk, long_) -> state;
    bar(state, elapsed_h, high, low, open, close) -> None | (event, r_gross, exit_px) —
    вызывается на барах ПОСЛЕ бара филла, не выбитых стопом; выход — по close бара;
    finish(state, event, r, j, exit_px) -> (event, r, j, extra)."""
    cur_stop, peak = stop, entry
    hold_ns = np.timedelta64(int(max_hold_h * 3600), "s") if max_hold_h > 0 else None
    long_ = side == "LONG"
    hook = OPTS.get("hook")
    st = hook.start(sig, entry, stop, risk, long_) if hook else None
    for j in range(f, end):
        h_, l_ = float(hi[j]), float(lo[j])
        if long_:
            if l_ <= cur_stop:
                r = round((cur_stop - entry) / risk, 4)
                ev = "stopped" if cur_stop == stop else "trail"
                return _finish(hook, st, ev, r, j, cur_stop)
        else:
            if h_ >= cur_stop:
                r = round((entry - cur_stop) / risk, 4)
                ev = "stopped" if cur_stop == stop else "trail"
                return _finish(hook, st, ev, r, j, cur_stop)
        if hook and j != f:
            elapsed_h = float((dts[j] - dts[f]) / np.timedelta64(3600, "s"))
            ex = hook.bar(st, elapsed_h, h_, l_, float(op[j]), float(cl[j]))
            if ex is not None:
                return _finish(hook, st, ex[0], ex[1], j, ex[2])
        if j != f:
            if long_:
                mfe_r = (h_ - entry) / risk
                if mfe_r >= act_r:
                    peak = max(peak, h_)
                    cur_stop = max(cur_stop, peak - dist_r * risk)
            else:
                mfe_r = (entry - l_) / risk
                if mfe_r >= act_r:
                    peak = min(peak, l_)
                    cur_stop = min(cur_stop, peak + dist_r * risk)
        if hold_ns is not None and dts[j] - dts[f] >= hold_ns:
            close_p = float(cl[j])
            r = round((close_p - entry) / risk if long_ else (entry - close_p) / risk, 4)
            return _finish(hook, st, "timeout", r, j, close_p)
    return None


def _finish(hook, st, ev, r, j, exit_px):
    if hook is None:
        return ev, r, j, {}
    return hook.finish(st, ev, r, j, exit_px)


class NoopHook:
    """Пустое правило: прогон с ним обязан совпасть с базой бит-в-бит (проверка обвязки)."""

    def start(self, sig, entry, stop, risk, long_):
        return None

    def bar(self, st, elapsed_h, high, low, open_, close):
        return None

    def finish(self, st, ev, r, j, exit_px):
        return ev, r, j, {}


def install(**opts) -> None:
    """Подменить судью портфеля репликой (opts — опции варианта; пусто = как прод)."""
    OPTS.clear()
    OPTS.update(opts)
    _pf._live_outcome = judge


def uninstall() -> None:
    OPTS.clear()
    _pf._live_outcome = _ORIG_LIVE_OUTCOME


def simulate(bars: dict, sigs: list):
    """Портфель прода с 27.10: только 1h, живой судья на 1h, трейл 1.5/0.2 без тейка,
    удержание 168ч, cap 6/сторону, тормоз 3 стопа/6ч, дедуп, M2′ (0.055% + слип 0.15% +
    фандинг 0.125 б.п./бар). Судья — тот, что сейчас стоит в app.sim.portfolio."""
    h1 = {sym: df for (sym, tf), df in bars.items() if tf == "1h"}
    return _pf.simulate_portfolio(
        bars, sigs, max_open_per_side=6, cooldown_stops=3, cooldown_hours=6.0, dedup=True,
        fee_pct=0.055, slippage_pct=0.15, funding_bps_per_bar=0.125, trail_activate_r=1.5,
        trail_distance_r=0.2, entry_fill="touch", live_judge_bars=h1, tp_exit=False,
        max_hold_hours=168.0)


def trades(rep) -> list:
    """[(решение, выход, R, ключ)] по времени решения (закрытие сигнального бара)."""
    return sorted((utc(t.signal_dt) + HOUR, utc(t.exit_dt), t.result_r,
                   key(t.symbol, t.signal_dt, t.side, t.setup)) for t in rep.detail)


def load(data: str) -> tuple[dict, list]:
    """'5y' — 9 монет 5 лет (sig_prod_late_entry); 'rot' — ротационная вселенная
    (membership_a). Только 1h-сигналы (прод с 27.10)."""
    import scripts.measure_one_position as mop
    bars, sigs = mop._load5y() if data == "5y" else mop._load_rot()
    return bars, [s for s in sigs if s["tf"] == "1h"]


# --- общий критерий замеров 2026-10-30 (записан в регистрациях ДО прогона) ---
# паритет базы «только 1h» (2026-10-27-1h-only-trail10-m2.md): (n, ΣR) по половинам
PARITY = {("5y", "H1"): (3168, 169.8), ("5y", "H2"): (877, 59.1),
          ("rot", "H1"): (1292, -72.3), ("rot", "H2"): (1440, 192.7)}
MATERIALITY_R = 17.5          # 5% от суммарного ΣR базы по 4 половинам (349.3R)


def halves(tr: list, data: str) -> dict:
    """Половины по времени решения: 5y — 2025-07-28, ротация — 2025-10-24."""
    import scripts.measure_one_position as mop
    split = mop.SPLIT5Y if data == "5y" else mop.SPLIT2Y
    return {"H1": [x for x in tr if x[0] < split], "H2": [x for x in tr if x[0] >= split]}


def stats(tr: list) -> dict:
    """n, ΣR, exp, DD (по времени выхода) + несокращённая сумма для сравнения."""
    import scripts.measure_one_position as mop
    out = mop._stats([x[:3] for x in tr])
    out["sum_raw"] = sum(x[2] for x in tr)
    return out


def check_parity(base: dict) -> bool:
    """base: {data: trades} — база обязана воспроизвести паритет бит-в-бит."""
    ok = True
    for data, tr in base.items():
        for half, part in halves(tr, data).items():
            s = stats(part)
            exp_n, exp_sum = PARITY[(data, half)]
            good = s["n"] == exp_n and s["sum"] == exp_sum
            ok &= good
            print(f"  паритет {data} {half}: n={s['n']} Σ={s['sum']:+.1f} "
                  f"(эталон {exp_n} / {exp_sum:+.1f}) {'OK' if good else 'РАСХОЖДЕНИЕ'}")
    return ok


def verdict(name: str, base: dict, var: dict) -> bool:
    """Критерий: на КАЖДОЙ из 4 половин ΣR(вар) > ΣR(база) и DD(вар) ≤ DD(база);
    сумма приростов ΣR по 4 половинам ≥ MATERIALITY_R."""
    ok, gain = True, 0.0
    print(f"== {name}")
    for data in ("5y", "rot"):
        hb, hv = halves(base[data], data), halves(var[data], data)
        for half in ("H1", "H2"):
            sb, sv = stats(hb[half]), stats(hv[half])
            c1, c2 = sv["sum_raw"] > sb["sum_raw"], sv["dd"] <= sb["dd"]
            ok &= c1 and c2
            gain += sv["sum_raw"] - sb["sum_raw"]
            print(f"  {data:3s} {half} база n={sb['n']} Σ={sb['sum']:+.1f} exp={sb['exp']:+.3f} "
                  f"DD={sb['dd']} | вар n={sv['n']} Σ={sv['sum']:+.1f} exp={sv['exp']:+.3f} "
                  f"DD={sv['dd']} | ΣR↑ {'+' if c1 else '-'} DD {'+' if c2 else '-'}")
    c3 = gain >= MATERIALITY_R
    print(f"  прирост ΣR по 4 половинам {gain:+.1f}R (порог +{MATERIALITY_R}) "
          f"{'+' if c3 else '-'}")
    ok &= c3
    print(f"  ВЕРДИКТ {name}: {'ПРИНЯТ критерием' if ok else 'НЕ ПРИНЯТ'}")
    return ok
