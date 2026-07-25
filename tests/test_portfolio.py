"""Тесты портфельной симуляции: живые правила (дедуп, лимит стороны, тормоз) на истории."""
from __future__ import annotations

import pandas as pd

from app.sim.portfolio import simulate_portfolio


def _bars(n=80, start="2026-01-01", tf_hours=1, base=100.0, drift=0.0):
    """Ровные бары с узким диапазоном: цена доходит и до входа, и до стопа/тейка,
    только если уровни заданы внутри диапазона — тесты задают их явно."""
    dts = pd.date_range(start, periods=n, freq=f"{tf_hours}h", tz="UTC")
    close = [base + drift * i for i in range(n)]
    return pd.DataFrame({
        "dt": dts,
        "open": close,
        "high": [c + 2.0 for c in close],
        "low": [c - 2.0 for c in close],
        "close": close,
        "volume": [1.0] * n,
    })


def _sig(symbol, i, side="LONG", setup="6226", entry=100.0, stop=99.0, tp=102.0,
         tf="1h", conf=0.7, dt=None):
    return {"symbol": symbol, "tf": tf, "bar_index": i,
            "dt": str(dt if dt is not None else pd.Timestamp("2026-01-01", tz="UTC")
                      + pd.Timedelta(hours=i)),
            "side": side, "setup": setup, "entry": entry, "stop": stop, "tp": tp,
            "rr": abs(tp - entry) / abs(entry - stop), "confidence": conf}


def test_portfolio_dedup_blocks_same_setup_while_active():
    """Дедуп живого бота: тот же (symbol, tf, side, setup), пока прежний активен."""
    bars = {("A/USDT:USDT", "1h"): _bars()}
    sigs = [_sig("A/USDT:USDT", 10), _sig("A/USDT:USDT", 11)]      # второй — дубль
    rep = simulate_portfolio(bars, sigs, max_open_per_side=0, cooldown_stops=0)
    assert rep.skipped_dedup == 1 and rep.signals_taken == 1
    off = simulate_portfolio(bars, sigs, max_open_per_side=0, cooldown_stops=0, dedup=False)
    assert off.skipped_dedup == 0 and off.signals_taken == 2


def test_portfolio_side_cap_counts_across_symbols():
    """Лимит стороны — ПОРТФЕЛЬНЫЙ: считает позиции по всем символам сразу
    (посимвольный бэктест этого эффекта не видит в принципе)."""
    bars = {(s, "1h"): _bars() for s in ("A/USDT:USDT", "B/USDT:USDT", "C/USDT:USDT")}
    sigs = [_sig("A/USDT:USDT", 10), _sig("B/USDT:USDT", 10), _sig("C/USDT:USDT", 10)]
    rep = simulate_portfolio(bars, sigs, max_open_per_side=2, cooldown_stops=0)
    assert rep.signals_taken == 2 and rep.skipped_cap == 1
    free = simulate_portfolio(bars, sigs, max_open_per_side=0, cooldown_stops=0)
    assert free.signals_taken == 3 and free.skipped_cap == 0


def test_portfolio_cooldown_pauses_side_after_stop_series():
    """Тормоз стороны: серия стопов в окне -> новые сигналы этой стороны не берутся."""
    # цена падает: LONG-вход 100 исполняется и идёт в стоп
    down = _bars(drift=-0.5)
    bars = {(s, "1h"): down for s in ("A/USDT:USDT", "B/USDT:USDT", "C/USDT:USDT")}
    sigs = [_sig("A/USDT:USDT", 5, entry=97.0, stop=96.0, tp=103.0),
            _sig("B/USDT:USDT", 6, entry=94.0, stop=93.0, tp=100.0),
            _sig("C/USDT:USDT", 30, entry=85.0, stop=84.0, tp=91.0)]
    hot = simulate_portfolio(bars, sigs, max_open_per_side=0,
                             cooldown_stops=2, cooldown_hours=48)
    cold = simulate_portfolio(bars, sigs, max_open_per_side=0, cooldown_stops=0)
    assert hot.skipped_cooldown == 1 and cold.skipped_cooldown == 0
    assert hot.trades == 2 and cold.trades == 3


def test_portfolio_report_equity_by_time_and_breakdowns():
    """Эквити строится по времени ЗАКРЫТИЯ (портфельная просадка), есть разбивки."""
    up = _bars(drift=0.5)
    bars = {("A/USDT:USDT", "1h"): up, ("B/USDT:USDT", "1h"): up}
    sigs = [_sig("A/USDT:USDT", 5, entry=103.0, stop=101.0, tp=107.0, setup="6226"),
            _sig("B/USDT:USDT", 20, entry=112.0, stop=110.0, tp=116.0, setup="LTF-BB")]
    rep = simulate_portfolio(bars, sigs, max_open_per_side=0, cooldown_stops=0)
    assert rep.trades == 2 and len(rep.equity_curve) == 2
    assert set(rep.by_setup) == {"6226", "LTF-BB"}
    assert rep.by_side["LONG"]["trades"] == 2
    assert rep.max_concurrent >= 1
    # эквити монотонно накапливает результаты в хронологии выходов
    assert rep.equity_curve[-1] == round(rep.total_r, 4)


def test_portfolio_unfilled_limit_occupies_slot_then_expires():
    """Неисполненная лимитка ЗАНИМАЕТ слот стороны (она несёт риск), потом истекает."""
    flat = _bars(n=200)
    bars = {("A/USDT:USDT", "1h"): flat, ("B/USDT:USDT", "1h"): flat}
    far = _sig("A/USDT:USDT", 5, entry=500.0, stop=490.0, tp=520.0)   # недостижимый вход
    near = _sig("B/USDT:USDT", 6, entry=100.0, stop=99.0, tp=102.0)
    rep = simulate_portfolio(bars, [far, near], max_open_per_side=1, cooldown_stops=0)
    assert rep.signals_taken == 1 and rep.skipped_cap == 1     # слот занят висящей лимиткой
    assert rep.trades == 0                                     # исполнения не было
