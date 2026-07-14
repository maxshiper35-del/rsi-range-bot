"""
backtest.py — офлайн-прогон SMC-движка на истории БЕЗ лукахеда.

Принцип: на каждом баре движку подаётся окно ТОЛЬКО закрытых баров до текущего.
Сигнал = pending-ордер; далее симулируется исполнение по будущим барам:
  - fill, когда цена бара касается entry;
  - если entry не наполнился за max_wait баров — ордер пропускается;
  - выход по stop/лесенке TP; при попадании стопа и TP в один бар — стоп первым;
  - опциональная лесенка частичных выходов (tp_ladder) и перевод стопа в безубыток;
  - комиссии + проскальзывание + (опц.) фандинг вычитаются из результата (в R);
  - по каждой сделке пишутся MFE/MAE (макс. ход в плюс/минус, в R).
Одновременно не более одной позиции; после выхода поиск сигнала возобновляется за баром выхода.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from app.brain.smc_setups import SMCConfig, SMCEngine
from app.core.signal_engine import Side


@dataclass
class Trade:
    side: Side
    entry: float
    stop: float
    tp: float
    rr: float
    signal_index: int
    fill_index: int
    exit_index: int
    outcome: str            # "tp" | "stop" | "tp{k}" | "tp{k}+stop" | "eod"
    result_r: float
    confidence: float
    mfe_r: float = 0.0
    mae_r: float = 0.0
    setup: str = ""


@dataclass
class BacktestReport:
    symbol: str
    timeframe: str
    bars: int
    signals: int
    trades: int
    wins: int
    losses: int
    winrate: float
    expectancy_r: float
    total_r: float
    profit_factor: float
    max_drawdown_r: float
    avg_win_r: float
    avg_loss_r: float
    avg_mfe_r: float
    avg_mae_r: float
    by_side: dict
    by_setup: dict = field(default_factory=dict)
    equity_curve: list = field(default_factory=list)
    detail: list = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.symbol} {self.timeframe} | баров={self.bars} сигналов={self.signals} "
            f"сделок={self.trades}\n"
            f"winrate={self.winrate:.1%} | expectancy={self.expectancy_r:+.2f}R | "
            f"итог={self.total_r:+.2f}R | PF={self.profit_factor:.2f} | "
            f"maxDD={self.max_drawdown_r:.2f}R | MFE={self.avg_mfe_r:.2f} MAE={self.avg_mae_r:.2f}"
        )


def _cost_r(entry: float, stop: float, fee_pct: float, slippage_pct: float) -> float:
    """Издержки round-trip в единицах R (комиссия ×2 + проскальзывание)."""
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    return entry * (2.0 * fee_pct + slippage_pct) / 100.0 / risk


def _simulate_open(df: pd.DataFrame, side: Side, entry: float, stop: float,
                   tranches: list, fill_index: int, move_be_after: int | None,
                   fee_pct: float, slippage_pct: float, funding_bps: float):
    """Симуляция позиции от fill до выхода. -> (outcome, result_r, mfe, mae, exit_index) | None."""
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()
    n = len(df)
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    is_long = side == Side.LONG
    cur_stop = stop
    remaining, realized, filled = 1.0, 0.0, 0
    mfe = mae = 0.0

    def close_out(outcome, exit_i):
        bars = max(exit_i - fill_index, 0)
        cost = (_cost_r(entry, stop, fee_pct, slippage_pct)
                + funding_bps / 10000.0 * entry / risk * bars)
        return outcome, realized - cost, mfe, mae, exit_i

    for i in range(fill_index + 1, n):
        if is_long:
            mfe = max(mfe, (high[i] - entry) / risk)
            mae = max(mae, (entry - low[i]) / risk)
            stop_hit = low[i] <= cur_stop
        else:
            mfe = max(mfe, (entry - low[i]) / risk)
            mae = max(mae, (high[i] - entry) / risk)
            stop_hit = high[i] >= cur_stop
        if stop_hit:                                      # стоп первым (консервативно)
            r_stop = (cur_stop - entry) / risk if is_long else (entry - cur_stop) / risk
            realized += remaining * r_stop
            return close_out("stop" if filled == 0 else f"tp{filled}+stop", i)
        for t in tranches:                                # частичные тейки
            if t["done"]:
                continue
            hit = high[i] >= t["level"] if is_long else low[i] <= t["level"]
            if hit:
                realized += t["frac"] * t["r"]
                remaining -= t["frac"]
                t["done"] = True
                filled += 1
                if move_be_after and filled >= move_be_after:
                    cur_stop = entry                      # стоп в безубыток
        if remaining <= 1e-9:
            return close_out("tp" if len(tranches) == 1 else f"tp{filled}", i)

    if filled > 0:                                        # конец данных — остаток по close
        r_eod = (close[-1] - entry) / risk if is_long else (entry - close[-1]) / risk
        realized += remaining * r_eod
        return close_out("eod", n - 1)
    return None


def _max_wait_bars(timeframe: str) -> int:
    """Окно ожидания филла В БАРАХ своего ТФ из ЕДИНОГО источника (journal.MAX_WAIT_HOURS).
    Захардкоженные 50 баров на 15m давали 12.5ч против живых 50ч — исследовательские
    прогоны молча воспроизводили инцидент 2026-07-13 (аудит-2 математики [4])."""
    from math import ceil

    from app.brain.journal import MAX_WAIT_HOURS
    from app.brain.smc_setups import SMCEngine
    return ceil(MAX_WAIT_HOURS * 60 / SMCEngine.TF_MINUTES.get(timeframe, 60))


def run_backtest(df: pd.DataFrame, symbol: str, timeframe: str = "m15",
                 engine: SMCEngine | None = None, cfg: SMCConfig | None = None,
                 window: int = 300, min_bars: int = 20, max_wait: int | None = None,
                 fee_pct: float = 0.055, slippage_pct: float = 0.02,
                 tp_ladder: list | None = None, move_be_after: int | None = None,
                 funding_bps_per_bar: float = 0.0) -> BacktestReport:
    """tp_ladder: список (R_множитель, доля), напр. [(1.0,0.5),(2.0,0.3),(3.0,0.2)].
    None -> один тейк = take_profits[0] с result = risk_reward (совместимо со старым поведением).
    max_wait: None -> journal.MAX_WAIT_HOURS в барах ЭТОГО ТФ (единое окно с живым судьёй)."""
    if max_wait is None:
        max_wait = _max_wait_bars(timeframe)
    engine = engine or SMCEngine(cfg or SMCConfig(use_time_filter=False))
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    n = len(df)

    trades: list[Trade] = []
    signals = 0
    i = min_bars
    while i < n:
        win = df.iloc[max(0, i - window + 1):i + 1].reset_index(drop=True)
        sig = engine.analyze_single(win, symbol, timeframe)
        if sig is None:
            i += 1
            continue
        signals += 1
        risk = abs(sig.entry - sig.stop)
        if tp_ladder:
            sgn = 1.0 if sig.side == Side.LONG else -1.0
            tranches = [{"level": sig.entry + sgn * rm * risk, "r": rm, "frac": fr, "done": False}
                        for rm, fr in tp_ladder]
        else:
            tranches = [{"level": sig.take_profits[0], "r": sig.risk_reward,
                         "frac": 1.0, "done": False}]

        filled_at = next((j for j in range(i + 1, min(n, i + 1 + max_wait))
                          if low[j] <= sig.entry <= high[j]), None)
        if filled_at is None:
            i += 1
            continue
        res = _simulate_open(df, sig.side, sig.entry, sig.stop, tranches, filled_at,
                             move_be_after, fee_pct, slippage_pct, funding_bps_per_bar)
        if res is None:
            i = filled_at + 1
            continue
        outcome, result_r, mfe, mae, exit_i = res
        trades.append(Trade(
            side=sig.side, entry=sig.entry, stop=sig.stop, tp=sig.take_profits[0],
            rr=sig.risk_reward, signal_index=i, fill_index=filled_at, exit_index=exit_i,
            outcome=outcome, result_r=round(result_r, 4), confidence=sig.confidence,
            mfe_r=round(mfe, 3), mae_r=round(mae, 3),
            setup=(sig.indicators_snapshot or {}).get("setup", ""),
        ))
        i = exit_i + 1
    return _report(symbol, timeframe, n, signals, trades)


def _report(symbol, timeframe, bars, signals, trades):
    wins = [t for t in trades if t.result_r > 0]
    losses = [t for t in trades if t.result_r <= 0]
    total_r = sum(t.result_r for t in trades)
    gross_win = sum(t.result_r for t in wins)
    gross_loss = abs(sum(t.result_r for t in losses))
    equity, peak, max_dd, run = [], 0.0, 0.0, 0.0
    for t in trades:
        run += t.result_r
        equity.append(round(run, 4))
        peak = max(peak, run)
        max_dd = max(max_dd, peak - run)

    by_side = {}
    for s in (Side.LONG, Side.SHORT):
        st = [t for t in trades if t.side == s]
        by_side[s.value] = {"trades": len(st), "wins": sum(1 for t in st if t.result_r > 0),
                            "total_r": round(sum(t.result_r for t in st), 3)}
    by_setup = {}
    for t in trades:
        b = by_setup.setdefault(t.setup or "?", {"trades": 0, "wins": 0, "total_r": 0.0})
        b["trades"] += 1
        b["wins"] += 1 if t.result_r > 0 else 0
        b["total_r"] = round(b["total_r"] + t.result_r, 3)

    ntr = len(trades)
    return BacktestReport(
        symbol=symbol, timeframe=timeframe, bars=bars, signals=signals, trades=ntr,
        wins=len(wins), losses=len(losses),
        winrate=(len(wins) / ntr) if ntr else 0.0,
        expectancy_r=round(total_r / ntr, 4) if ntr else 0.0,
        total_r=round(total_r, 4),
        profit_factor=round(gross_win / gross_loss, 3) if gross_loss else float("inf"),
        max_drawdown_r=round(max_dd, 4),
        avg_win_r=round(gross_win / len(wins), 4) if wins else 0.0,
        avg_loss_r=round(-gross_loss / len(losses), 4) if losses else 0.0,
        avg_mfe_r=round(sum(t.mfe_r for t in trades) / ntr, 3) if ntr else 0.0,
        avg_mae_r=round(sum(t.mae_r for t in trades) / ntr, 3) if ntr else 0.0,
        by_side=by_side, by_setup=by_setup, equity_curve=equity, detail=trades,
    )
