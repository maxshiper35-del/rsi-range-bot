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


def test_cooldown_losses_only_ignores_profitable_trailing_exit():
    """Ручка cooldown_losses_only: ПРИБЫЛЬНЫЙ выход по подтянутому стопу не взводит тормоз.

    При схеме «выход только по трейлингу» такой выход тоже имеет outcome='stop' (и
    status='stopped' в живом журнале), но свидетельством «рынок гонит тренд против
    ретрейсмент-входов» он не является. Дефолт False — см. замер в docstring
    simulate_portfolio: на 5 годах фильтрация по знаку даёт худшую просадку."""
    prices = [100, 100, 101, 102, 103, 104, 103, 102, 101, 100] + [99.5, 99.0] + [98.0] * 30
    df = pd.DataFrame({
        "dt": pd.date_range("2026-01-01", periods=len(prices), freq="1h", tz="UTC"),
        "open": prices, "close": prices,
        "high": [p + 0.2 for p in prices], "low": [p - 0.2 for p in prices],
    })
    bars = {(s, "1h"): df for s in ("A/USDT:USDT", "B/USDT:USDT", "C/USDT:USDT")}
    # A и B выходят по трейлингу В ПЛЮС на баре 6; C приходит следом (бар 7)
    sigs = [_sig("A/USDT:USDT", 0, tp=200.0), _sig("B/USDT:USDT", 0, tp=200.0),
            _sig("C/USDT:USDT", 7, tp=200.0)]
    kw = dict(max_open_per_side=0, cooldown_stops=2, cooldown_hours=48,
              fee_pct=0.0, slippage_pct=0.0, trail_activate_r=2.0, trail_distance_r=1.0)
    hot = simulate_portfolio(bars, sigs, **kw)
    soft = simulate_portfolio(bars, sigs, cooldown_losses_only=True, **kw)
    # оба выхода — прибыльные «стопы» (подтянутый стоп), именно они и тормозят сторону
    assert [(t.outcome, t.result_r > 0) for t in hot.detail] == [("stop", True)] * 2
    assert hot.skipped_cooldown == 1 and hot.trades == 2
    assert soft.skipped_cooldown == 0 and soft.trades == 3


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


def test_trailing_stop_locks_profit_without_lookahead():
    """Трейлинг: после хода +N·R стоп подтягивается к пику, но вступает в силу СО
    СЛЕДУЮЩЕГО бара — внутри бара порядок хай/лоу неизвестен (тот же принцип
    без-лукахеда, что и во всём скоринге)."""
    import pandas as pd

    from app.core.signal_engine import Side
    from app.sim.backtest import _simulate_open
    # цена растёт до +3R, затем разворот вниз; тейк недостижимо далеко (+10R)
    prices = [100, 101, 102, 103, 104, 103, 102, 101, 100, 99]
    df = pd.DataFrame({
        "dt": pd.date_range("2026-01-01", periods=len(prices), freq="1h", tz="UTC"),
        "open": prices, "close": prices,
        "high": [p + 0.2 for p in prices], "low": [p - 0.2 for p in prices],
    })
    tr = [{"level": 110.0, "r": 10.0, "frac": 1.0, "done": False}]
    plain = _simulate_open(df, Side.LONG, 100.0, 99.0, list(tr), 0, None, 0.0, 0.0, 0.0)
    trailed = _simulate_open(df, Side.LONG, 100.0, 99.0,
                             [dict(t) for t in tr], 0, None, 0.0, 0.0, 0.0,
                             trail_activate_r=2.0, trail_distance_r=1.0)
    # без трейлинга позиция доезжает до стопа 99 -> -1R; с трейлингом фиксируется в плюсе
    assert plain[1] < 0
    assert trailed[1] > 1.0
    assert trailed[0] == "stop"          # выход именно по подтянутому стопу


def test_max_hold_bars_forces_timeout_exit():
    """Ограничение времени в позиции: выход по close через N баров."""
    import pandas as pd

    from app.core.signal_engine import Side
    from app.sim.backtest import _simulate_open
    flat = [100.0] * 12
    df = pd.DataFrame({
        "dt": pd.date_range("2026-01-01", periods=len(flat), freq="1h", tz="UTC"),
        "open": flat, "close": flat,
        "high": [p + 0.3 for p in flat], "low": [p - 0.3 for p in flat],
    })
    tr = [{"level": 110.0, "r": 10.0, "frac": 1.0, "done": False}]
    res = _simulate_open(df, Side.LONG, 100.0, 99.0, tr, 0, None, 0.0, 0.0, 0.0,
                         max_hold_bars=3)
    assert res is not None and res[0] == "timeout" and res[4] == 3


def test_entry_fill_through_requires_trade_through():
    """through: касание уровня БЕЗ прохода сквозь — не филл (пассивная реальность);
    бар, прошедший на eps глубже, — филл. touch на тех же барах исполняется касанием."""
    import pandas as pd

    from app.sim.portfolio import simulate_portfolio
    dts = pd.date_range("2026-01-01", periods=8, freq="1h", tz="UTC")
    # LONG entry=100, stop=98 (risk 2, eps=0.1 при 5%): бар2 low=100 (касание),
    # бар3 low=99.85 (проход), дальше рост до тейка 104
    df = pd.DataFrame({
        "dt": dts,
        "open": [103, 102, 101, 100, 101, 103, 105, 105],
        "high": [104, 103, 102, 101, 103, 105, 106, 106],
        "low":  [102, 101, 100.0, 99.85, 100.5, 102, 104, 104],
        "close": [102, 102, 101, 100.5, 102.5, 104.5, 105.5, 105],
        "volume": [1] * 8,
    })
    sig = {"symbol": "T/USDT:USDT", "tf": "1h", "bar_index": 1, "dt": dts[1],
           "side": "LONG", "entry": 100.0, "stop": 98.0, "tp": 104.0, "rr": 2.0,
           "setup": "6226", "confidence": 0.7}
    bars = {("T/USDT:USDT", "1h"): df}
    thr = simulate_portfolio(bars, [dict(sig)], fee_pct=0.0, slippage_pct=0.0,
                             entry_fill="through", through_frac=0.05)
    tch = simulate_portfolio(bars, [dict(sig)], fee_pct=0.0, slippage_pct=0.0)
    assert tch.trades == 1 and thr.trades == 1
    assert tch.detail[0].fill_dt == dts[2]      # касание на баре 2
    assert thr.detail[0].fill_dt == dts[3]      # проход только на баре 3
    # глубокий eps (50% риска = 99.0) — прохода нет вовсе, сделки нет
    deep = simulate_portfolio(bars, [dict(sig)], fee_pct=0.0, slippage_pct=0.0,
                              entry_fill="through", through_frac=0.5)
    assert deep.trades == 0
