"""
portfolio.py — портфельная симуляция ВСЕГО вотчлиста с ЖИВЫМИ правилами бота.

Зачем отдельно от backtest.py: посимвольный бэктест меряет стратегию в вакууме —
каждый символ торгуется изолированно, без ограничений и без учёта того, что 9
коррелированных монет × 2 ТФ выдают сигналы ОДНОВРЕМЕННО и в одну сторону. Именно это
дало живые −12.7R (14.07.2026) и −8R (17.07.2026), которых посимвольный прогон
не показывал в принципе.

Здесь события всех символов сводятся в ЕДИНУЮ временную шкалу и пропускаются через те
же гейты, что стоят в `bot.scan_auto`:
  - дедуп: пока активен сигнал того же (symbol, tf, side, setup) — новый не создаётся;
  - `max_open_per_side`: лимит одновременных активных сигналов в одну сторону;
  - анти-кластерный тормоз: ≥N стопов одной стороны за окно X часов — сторона на паузе;
  - окно ожидания филла = journal.MAX_WAIT_HOURS (единое с живым судьёй и backtest.py).
Исход считается тем же `_simulate_open`, что и в backtest.py — одна математика выходов.

Результат: суммарный R по портфелю, кривая эквити ПО ВРЕМЕНИ (а не по сделкам одного
символа), максимальная просадка портфеля, пиковая одновременная экспозиция и разбивки.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from app.brain.journal import MAX_WAIT_HOURS
from app.brain.smc_setups import SMCEngine
from app.core.signal_engine import Side
from app.sim.backtest import _cost_r, _simulate_open


def _live_outcome(sig: dict, df1h: pd.DataFrame, idx1h: pd.DatetimeIndex, *,
                  fee_pct: float, slippage_pct: float, funding_bps_per_bar: float,
                  trail_activate_r: float | None, trail_distance_r: float,
                  tp_exit: bool, max_hold_hours: float, fill_through_frac: float = 0.0,
                  extra_cost_r: float = 0.0):
    """Исход сигнала САМИМ живым судьёй (journal._outcome_from_bars) на 1h-барах — паритет
    с публичным журналом по построению: стоп на баре филла = −1R, на баре филла нет тейка
    и трейлинга, окно ожидания 50ч, потолок удержания, 4h-сигналы судятся 1h-барами.

    Аудит 05.10: _simulate_open пропускал бар филла и судил 4h-сигналы 4h-барами — итог
    5 лет +622R против +73R по правилам живого судьи. -> (kind, r_net|None, fill_dt,
    closed_dt) | None (не разрешился в данных). kind: 'expired' | '<событие>-stop' | 'tpN'
    (суффикс 'stop' — для тормоза стороны: живой тормоз считает все status='stopped')."""
    import json as _json

    from app.brain.journal import _outcome_from_bars
    created = pd.Timestamp(sig["dt"])
    created = created.tz_localize("UTC") if created.tzinfo is None else created.tz_convert("UTC")
    decision = created + pd.Timedelta(minutes=SMCEngine.TF_MINUTES.get(sig["tf"], 60))
    start = int(idx1h.searchsorted(decision - pd.Timedelta(hours=1)))
    span = int(MAX_WAIT_HOURS + (max_hold_hours if max_hold_hours > 0 else 24 * 40)) + 8
    win = df1h.iloc[start:start + span]
    trail = ({"activate_r": trail_activate_r, "distance_r": trail_distance_r,
              "tp_exit": tp_exit, "max_hold_hours": max_hold_hours}
             if trail_activate_r else None)
    row = {"entry": sig["entry"], "stop": sig["stop"], "side": sig["side"],
           "timeframe": sig["tf"], "created_at": created.isoformat(), "filled_at": None,
           "status": "open", "tp1": sig["tp"], "tp2": None, "tp3": None,
           "snapshot_json": _json.dumps({"setup": sig.get("setup"), "trail": trail})}
    res = _outcome_from_bars(row, win, fill_through_frac=fill_through_frac)
    if res is None or res[0] == "open":
        return None
    status, event, r, _exit_px, fill_dt, exit_dt = res
    closed_dt = pd.Timestamp(exit_dt)
    if status == "expired":
        return "expired", None, None, closed_dt
    fill = pd.Timestamp(fill_dt)
    risk = abs(sig["entry"] - sig["stop"])
    held_h = max(0.0, (closed_dt - fill).total_seconds() / 3600.0)
    cost = (_cost_r(sig["entry"], sig["stop"], fee_pct, slippage_pct)
            + funding_bps_per_bar / 10000.0 * sig["entry"] / risk * int(held_h)
            + extra_cost_r)
    kind = f"{event}-stop" if status == "stopped" else status
    return kind, float(r) - cost, fill, closed_dt


@dataclass
class PortfolioTrade:
    symbol: str
    tf: str
    setup: str
    side: str
    signal_dt: pd.Timestamp
    fill_dt: pd.Timestamp
    exit_dt: pd.Timestamp
    outcome: str
    result_r: float
    confidence: float


@dataclass
class PortfolioReport:
    symbols: list
    timeframes: list
    signals_total: int          # сигналов, выданных движком (до портфельных гейтов)
    signals_taken: int          # прошли гейты и стали активными
    skipped_dedup: int
    skipped_cap: int
    skipped_cooldown: int
    trades: int
    wins: int
    winrate: float
    expectancy_r: float
    total_r: float
    profit_factor: float
    max_drawdown_r: float
    max_concurrent: int
    max_concurrent_side: int
    avg_hold_hours: float
    max_hold_hours: float
    by_symbol: dict
    by_setup: dict
    by_side: dict
    equity_curve: list = field(default_factory=list)
    detail: list = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"портфель {len(self.symbols)} симв. × {','.join(self.timeframes)} | "
            f"сигналов={self.signals_total} взято={self.signals_taken} "
            f"(дедуп -{self.skipped_dedup}, лимит -{self.skipped_cap}, "
            f"тормоз -{self.skipped_cooldown})\n"
            f"сделок={self.trades} winrate={self.winrate:.1%} "
            f"expectancy={self.expectancy_r:+.3f}R итог={self.total_r:+.2f}R "
            f"PF={self.profit_factor:.2f} maxDD={self.max_drawdown_r:.2f}R\n"
            f"пик одновременных: {self.max_concurrent} (в одну сторону "
            f"{self.max_concurrent_side})"
        )


def _pf(wins_r: float, loss_r: float) -> float:
    return round(wins_r / loss_r, 3) if loss_r else float("inf")


def simulate_portfolio(bars: dict, signals: list, *,
                       max_open_per_side: int = 6,
                       cooldown_stops: int = 3,
                       cooldown_hours: float = 6.0,
                       cooldown_losses_only: bool = False,
                       dedup: bool = True,
                       fee_pct: float = 0.055,
                       slippage_pct: float = 0.02,
                       tp_ladder: list | None = None,
                       move_be_after: int | None = None,
                       funding_bps_per_bar: float = 0.0,
                       trail_activate_r: float | None = None,
                       trail_distance_r: float = 1.0,
                       max_hold_bars: int | None = None,
                       entry_fill: str = "touch",
                       through_frac: float = 0.05,
                       live_judge_bars: dict | None = None,
                       tp_exit: bool = False,
                       max_hold_hours: float = 168.0,
                       extra_cost_r: float = 0.0,
                       symbol_cooldown_hours: float = 0.0) -> PortfolioReport:
    """bars: {(symbol, tf): df} — те же кэш-бары, по которым считались сигналы.
    signals: список словарей из scripts.dump_signals (symbol, tf, bar_index, side, ...).

    Решения принимаются в хронологическом порядке ПО ВРЕМЕНИ СИГНАЛА; исход каждой
    позиции считается по будущим барам её символа, но её вклад в лимиты/тормоз виден
    другим символам ровно с того момента, когда она реально активна/закрыта — так
    портфельные правила работают на истории так же, как в живом боте.
    """
    for key, df in bars.items():
        if not isinstance(df.get("dt").iloc[0], pd.Timestamp):
            raise ValueError(f"{key}: колонка dt должна быть datetime")

    # live_judge_bars={symbol: 1h-бары}: исход каждой сделки считает ЖИВОЙ судья (паритет
    # с журналом); без него — прежняя модель _simulate_open (оптимистична, см. _live_outcome)
    live_idx = ({sym: pd.DatetimeIndex(pd.to_datetime(df["dt"], utc=True))
                 for sym, df in live_judge_bars.items()} if live_judge_bars else {})
    events = sorted(signals, key=lambda s: (str(s["dt"]), s["symbol"], s["tf"]))
    active: list[dict] = []          # активные (ждущие филла или в позиции)
    closed: list[dict] = []          # завершённые: {side, closed_dt, result_r, ...}
    trades: list[PortfolioTrade] = []
    skipped = {"dedup": 0, "cap": 0, "cooldown": 0}
    max_concurrent = max_concurrent_side = 0

    def side_counts(now: pd.Timestamp) -> dict:
        out = {"LONG": 0, "SHORT": 0}
        for a in active:
            if a["signal_dt"] <= now < a["closed_dt"]:
                out[a["side"]] += 1
        return out

    def counts_as_stop(c: dict) -> bool:
        """Что взводит анти-кластерный тормоз. Живой аналог — journal.recent_side_stops.

        cooldown_losses_only=True: только УБЫТОЧНЫЙ выход по стопу. При схеме «выход только
        по трейлингу» подтянутый стоп закрывает и прибыльные сделки, а прибыльная фиксация
        не свидетельствует, что рынок гонит тренд против ретрейсмент-входов. ДЕФОЛТ False
        (= живое правило): замер 09.08.2026 показал, что фильтр по знаку хуже на обеих
        выборках — см. journal.recent_side_stops, там таблица."""
        if not c["outcome"].endswith("stop"):
            return False
        return c["result_r"] < 0 if cooldown_losses_only else True

    def recent_stops(side: str, now: pd.Timestamp) -> int:
        cutoff = now - pd.Timedelta(hours=cooldown_hours)
        return sum(1 for c in closed
                   if c["side"] == side and counts_as_stop(c)
                   and cutoff <= c["closed_dt"] <= now)

    def symbol_cooled(sig: dict) -> bool:
        """Пауза монеты+стороны после УБЫТОЧНОГО стопа (замер 07.10.2026): стоп известен к
        решению (бар выхода закрыт), и с него прошло не больше symbol_cooldown_hours."""
        decision = (pd.Timestamp(sig["dt"])
                    + pd.Timedelta(minutes=SMCEngine.TF_MINUTES.get(sig["tf"], 60)))
        for c in closed:
            if (c["symbol"] == sig["symbol"] and c["side"] == sig["side"]
                    and c["outcome"].endswith("stop") and c["result_r"] < 0):
                cdt = pd.Timestamp(c["closed_dt"])
                if (cdt + pd.Timedelta(hours=1) <= decision
                        and decision - cdt <= pd.Timedelta(hours=symbol_cooldown_hours)):
                    return True
        return False

    def duplicate_active(sig: dict, now: pd.Timestamp) -> bool:
        return any(a["symbol"] == sig["symbol"] and a["tf"] == sig["tf"]
                   and a["side"] == sig["side"] and a["setup"] == sig["setup"]
                   and a["signal_dt"] <= now < a["closed_dt"]
                   for a in active)

    for sig in events:
        df = bars[(sig["symbol"], sig["tf"])]
        now = pd.Timestamp(sig["dt"])
        if dedup and duplicate_active(sig, now):
            skipped["dedup"] += 1
            continue
        counts = side_counts(now)
        if max_open_per_side > 0 and counts[sig["side"]] >= max_open_per_side:
            skipped["cap"] += 1
            continue
        if cooldown_stops > 0 and recent_stops(sig["side"], now) >= cooldown_stops:
            skipped["cooldown"] += 1
            continue
        if symbol_cooldown_hours > 0 and symbol_cooled(sig):
            skipped["cooldown"] += 1
            continue

        # --- сигнал принят: считаем исход по барам своего символа ---
        if live_judge_bars is not None:
            lres = _live_outcome(
                sig, live_judge_bars[sig["symbol"]], live_idx[sig["symbol"]],
                fee_pct=fee_pct, slippage_pct=slippage_pct,
                funding_bps_per_bar=funding_bps_per_bar, trail_activate_r=trail_activate_r,
                trail_distance_r=trail_distance_r, tp_exit=tp_exit,
                max_hold_hours=max_hold_hours,
                fill_through_frac=through_frac if entry_fill == "through" else 0.0,
                extra_cost_r=extra_cost_r)
            if lres is None:
                continue
            kind, r_net, fill_dt, closed_dt = lres
            if kind == "expired":
                active.append({**sig, "signal_dt": now, "outcome": "expired",
                               "closed_dt": closed_dt})
                continue
            rec = {**sig, "signal_dt": now, "closed_dt": closed_dt, "outcome": kind,
                   "result_r": r_net}
            active.append(rec)
            closed.append(rec)
            trades.append(PortfolioTrade(
                symbol=sig["symbol"], tf=sig["tf"], setup=sig["setup"], side=sig["side"],
                signal_dt=now, fill_dt=fill_dt, exit_dt=closed_dt, outcome=kind,
                result_r=round(r_net, 4), confidence=sig["confidence"]))
            c = side_counts(now)
            max_concurrent = max(max_concurrent, c["LONG"] + c["SHORT"])
            max_concurrent_side = max(max_concurrent_side, c["LONG"], c["SHORT"])
            continue
        i = sig["bar_index"]
        high = df["high"].to_numpy()
        low = df["low"].to_numpy()
        n = len(df)
        max_wait = max(1, int(MAX_WAIT_HOURS * 60 / SMCEngine.TF_MINUTES.get(sig["tf"], 60)))
        entry, stop = sig["entry"], sig["stop"]
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        # Модель исполнения входа (замер 31.08: журнальные «касания» реальный стакан
        # не исполнял — 17 филлов журнала / 0 на бирже):
        #   touch   — лимитка исполняется КАСАНИЕМ (оптимистичная, текущий контракт);
        #   through — пассивная реальность: филл только если цена ПРОШЛА СКВОЗЬ уровень
        #             на through_frac дистанции стопа (очередь заявок гарантированно снята).
        if entry_fill == "through":
            eps = through_frac * risk
            if sig["side"] == "LONG":
                fill_i = next((j for j in range(i + 1, min(n, i + 1 + max_wait))
                               if low[j] <= entry - eps), None)
            else:
                fill_i = next((j for j in range(i + 1, min(n, i + 1 + max_wait))
                               if high[j] >= entry + eps), None)
        else:
            fill_i = next((j for j in range(i + 1, min(n, i + 1 + max_wait))
                           if low[j] <= entry <= high[j]), None)
        side = Side.LONG if sig["side"] == "LONG" else Side.SHORT
        if fill_i is None:                       # лимитка не исполнилась — истекла
            expire_i = min(n - 1, i + max_wait)
            active.append({**sig, "signal_dt": now, "outcome": "expired",
                           "closed_dt": pd.Timestamp(df["dt"].iloc[expire_i])})
            continue
        if tp_ladder:
            sgn = 1.0 if side == Side.LONG else -1.0
            tranches = [{"level": entry + sgn * rm * risk, "r": rm, "frac": fr, "done": False}
                        for rm, fr in tp_ladder]
        else:
            tranches = [{"level": sig["tp"], "r": sig["rr"], "frac": 1.0, "done": False}]
        res = _simulate_open(df, side, entry, stop, tranches, fill_i, move_be_after,
                             fee_pct, slippage_pct, funding_bps_per_bar,
                             trail_activate_r=trail_activate_r,
                             trail_distance_r=trail_distance_r,
                             max_hold_bars=max_hold_bars)
        if res is None:
            continue
        outcome, result_r, _mfe, _mae, exit_i = res
        closed_dt = pd.Timestamp(df["dt"].iloc[exit_i])
        rec = {**sig, "signal_dt": now, "closed_dt": closed_dt, "outcome": outcome,
               "result_r": result_r}
        active.append(rec)
        closed.append(rec)
        trades.append(PortfolioTrade(
            symbol=sig["symbol"], tf=sig["tf"], setup=sig["setup"], side=sig["side"],
            signal_dt=now, fill_dt=pd.Timestamp(df["dt"].iloc[fill_i]), exit_dt=closed_dt,
            outcome=outcome, result_r=round(result_r, 4), confidence=sig["confidence"]))
        c = side_counts(now)
        max_concurrent = max(max_concurrent, c["LONG"] + c["SHORT"])
        max_concurrent_side = max(max_concurrent_side, c["LONG"], c["SHORT"])

    # --- отчёт: эквити по ВРЕМЕНИ ЗАКРЫТИЯ (портфельная просадка, а не посимвольная) ---
    trades.sort(key=lambda t: t.exit_dt)
    equity, peak, max_dd, run = [], 0.0, 0.0, 0.0
    for t in trades:
        run += t.result_r
        equity.append(round(run, 4))
        peak = max(peak, run)
        max_dd = max(max_dd, peak - run)
    # время удержания: без фиксированного тейка позиция живёт до трейлинг-стопа — надо
    # видеть, не превращается ли «дать прибыли течь» в недельное зависание слота
    holds = [(t.exit_dt - t.fill_dt).total_seconds() / 3600.0 for t in trades]
    wins = [t for t in trades if t.result_r > 0]
    gross_win = sum(t.result_r for t in wins)
    gross_loss = abs(sum(t.result_r for t in trades if t.result_r <= 0))

    def group(key):
        out: dict = {}
        for t in trades:
            b = out.setdefault(getattr(t, key), {"trades": 0, "wins": 0, "total_r": 0.0})
            b["trades"] += 1
            b["wins"] += 1 if t.result_r > 0 else 0
            b["total_r"] = round(b["total_r"] + t.result_r, 3)
        return out

    ntr = len(trades)
    return PortfolioReport(
        symbols=sorted({s for s, _ in bars}), timeframes=sorted({tf for _, tf in bars}),
        signals_total=len(events), signals_taken=len(active),
        skipped_dedup=skipped["dedup"], skipped_cap=skipped["cap"],
        skipped_cooldown=skipped["cooldown"],
        trades=ntr, wins=len(wins),
        winrate=(len(wins) / ntr) if ntr else 0.0,
        expectancy_r=round(sum(t.result_r for t in trades) / ntr, 4) if ntr else 0.0,
        total_r=round(sum(t.result_r for t in trades), 4),
        profit_factor=_pf(gross_win, gross_loss),
        max_drawdown_r=round(max_dd, 3),
        max_concurrent=max_concurrent, max_concurrent_side=max_concurrent_side,
        avg_hold_hours=round(sum(holds) / len(holds), 1) if holds else 0.0,
        max_hold_hours=round(max(holds), 1) if holds else 0.0,
        by_symbol=group("symbol"), by_setup=group("setup"), by_side=group("side"),
        equity_curve=equity, detail=trades,
    )
