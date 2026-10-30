"""Исследовательская копия портфельного цикла — ТОЛЬКО для замера «обновление заявки»
(docs/measurements/2026-10-30-order-refresh.md). Прод её не импортирует.

Это живой путь `app.sim.portfolio.simulate_portfolio` (live_judge_bars, прод с 27.10:
cap 6/сторону, тормоз 3 стопа/6ч, дедуп (монета, ТФ, сторона, сетап), M2′, трейл 1.5/0.2
без тейка, удержание 168ч) плюс одно правило за флагом `refresh`. С refresh=False обязана
давать сделки бит-в-бит с оригиналом — замер проверяет это до вердикта.

Правило R1 (refresh=True): новый сигнал с тем же (монета, ТФ, сторона, сетап), что у
активного, раньше отбрасывался дедупом. Теперь, если старая заявка ЕЩЁ НЕ ИСПОЛНЕНА к
решению нового сигнала (бар её филла открывается не раньше закрытия сигнального бара
нового, либо она истечёт без филла), уровни нового (вход или стоп) ОТЛИЧАЮТСЯ и сторона
не на тормозе — старая заявка снимается в момент нового сигнала, новая встаёт вместо неё
(свои уровни, своё окно 50ч). Иначе — дедуп, как в проде.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

import app.sim.portfolio as _pf
from app.brain.smc_setups import SMCEngine
from app.sim.portfolio import PortfolioTrade

CAP, COOL_STOPS, COOL_HOURS = 6, 3, 6.0
JUDGE_KW = dict(fee_pct=0.055, slippage_pct=0.15, funding_bps_per_bar=0.125,
                trail_activate_r=1.5, trail_distance_r=0.2, tp_exit=False,
                max_hold_hours=168.0, fill_through_frac=0.0, extra_cost_r=0.0,
                gap_open=False, be_trigger_r=0.0, be_lock_r=0.0)


@dataclass
class Report:
    detail: list
    skipped: dict
    # (время нового сигнала, снятая сделка | None, новый сигнал)
    refreshed: list = field(default_factory=list)


def simulate(bars: dict, signals: list, *, refresh: bool) -> Report:
    live = {sym: df for (sym, tf), df in bars.items() if tf == "1h"}
    live_idx = {sym: pd.DatetimeIndex(pd.to_datetime(df["dt"], utc=True))
                for sym, df in live.items()}
    events = sorted(signals, key=lambda s: (str(s["dt"]), s["symbol"], s["tf"]))
    active: list[dict] = []
    closed: list[dict] = []
    trades: list = []
    skipped = {"dedup": 0, "cap": 0, "cooldown": 0}
    refreshed: list = []

    def side_counts(now: pd.Timestamp) -> dict:
        out = {"LONG": 0, "SHORT": 0}
        for a in active:
            if a["signal_dt"] <= now < a["closed_dt"]:
                out[a["side"]] += 1
        return out

    def recent_stops(side: str, now: pd.Timestamp) -> int:
        cutoff = now - pd.Timedelta(hours=COOL_HOURS)
        return sum(1 for c in closed if c["side"] == side and c["outcome"].endswith("stop")
                   and cutoff <= c["closed_dt"] <= now)

    def duplicate(sig: dict, now: pd.Timestamp):
        return next((a for a in active
                     if a["symbol"] == sig["symbol"] and a["side"] == sig["side"]
                     and a["tf"] == sig["tf"] and a["setup"] == sig["setup"]
                     and a["signal_dt"] <= now < a["closed_dt"]), None)

    for sig in events:
        now = pd.Timestamp(sig["dt"])
        dup = duplicate(sig, now)
        if dup is not None:
            decision = now + pd.Timedelta(minutes=SMCEngine.TF_MINUTES.get(sig["tf"], 60))
            pending = dup["outcome"] == "expired" or dup["fill_dt"] >= decision
            differs = sig["entry"] != dup["entry"] or sig["stop"] != dup["stop"]
            if not (refresh and pending and differs
                    and recent_stops(sig["side"], now) < COOL_STOPS):
                skipped["dedup"] += 1
                continue
            # снять старую заявку в момент нового сигнала: исполнения у неё не было
            gone = None
            if dup["outcome"] != "expired":
                closed[:] = [c for c in closed if c is not dup]
                gone = dup["trade"]
                trades[:] = [t for t in trades if t is not gone]
            dup["closed_dt"] = now
            dup["outcome"] = "cancelled"
            refreshed.append((now, gone, sig))
        counts = side_counts(now)
        if counts[sig["side"]] >= CAP:
            skipped["cap"] += 1
            continue
        if recent_stops(sig["side"], now) >= COOL_STOPS:
            skipped["cooldown"] += 1
            continue
        lres = _pf._live_outcome(sig, live[sig["symbol"]], live_idx[sig["symbol"]], **JUDGE_KW)
        if lres is None:
            continue
        kind, r_net, fill_dt, closed_dt = lres
        if kind == "expired":
            active.append({**sig, "signal_dt": now, "outcome": "expired",
                           "closed_dt": closed_dt})
            continue
        trade = PortfolioTrade(
            symbol=sig["symbol"], tf=sig["tf"], setup=sig["setup"], side=sig["side"],
            signal_dt=now, fill_dt=fill_dt, exit_dt=closed_dt, outcome=kind,
            result_r=round(r_net, 4), confidence=sig["confidence"])
        rec = {**sig, "signal_dt": now, "closed_dt": closed_dt, "outcome": kind,
               "result_r": r_net, "fill_dt": fill_dt, "trade": trade}
        active.append(rec)
        closed.append(rec)
        trades.append(trade)
    trades.sort(key=lambda t: t.exit_dt)
    return Report(detail=trades, skipped=skipped, refreshed=refreshed)
