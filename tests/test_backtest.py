"""Тесты бэктестера: механика без лукахеда, метрики отчёта."""
from __future__ import annotations

import pandas as pd

from app.brain.smc_setups import SMCConfig
from app.sim.backtest import BacktestReport, run_backtest

# один цикл паттерна (аптренд -> свип хая -> слом вниз), см. test_smc_setups
_PATTERN = [
    (100, 100.5, 99.5, 100), (100, 102.5, 99.8, 102), (102, 104.8, 101.5, 104),
    (104, 103.0, 101.8, 102), (102, 102.2, 100.5, 101), (101, 103.5, 100.8, 103),
    (103, 107.5, 102.8, 107), (107, 110.8, 106.5, 110), (110, 108.5, 106.8, 107),
    (107, 106.0, 104.5, 105), (105, 109.5, 104.8, 109), (109, 113.5, 108.5, 108),
    (108, 108.2, 103.5, 104), (104, 104.2, 99.5, 100), (100, 100.2, 95.5, 96),
    (96, 96.2, 93.5, 94),
]


def _df(bars):
    o, h, low, c = zip(*bars)
    return pd.DataFrame({"open": o, "high": h, "low": low, "close": c,
                         "volume": [1000.0] * len(bars)})


def test_backtest_runs_and_reports():
    df = _df(_PATTERN * 4)                       # повторяем паттерн -> сигналы повторяются
    rep = run_backtest(df, "TEST/USDT", "m15", cfg=SMCConfig(use_time_filter=False),
                       min_bars=16)
    assert isinstance(rep, BacktestReport)
    assert rep.bars == len(_PATTERN) * 4
    assert rep.signals >= 1
    assert rep.trades >= 1
    assert len(rep.equity_curve) == rep.trades
    # инварианты метрик
    assert rep.wins + rep.losses == rep.trades
    assert 0.0 <= rep.winrate <= 1.0
    assert rep.max_drawdown_r >= 0.0
    # каждая сделка резолвится в tp или stop
    assert all(t.outcome in ("tp", "stop") for t in rep.detail)


def test_backtest_no_signal_is_clean_report():
    from app.core.signal_engine import _synthetic_uptrend
    rep = run_backtest(_synthetic_uptrend(), "T", "m15",
                       cfg=SMCConfig(use_time_filter=False))
    assert isinstance(rep, BacktestReport)
    assert rep.trades >= 0
    assert rep.total_r == round(sum(t.result_r for t in rep.detail), 4)


def test_backtest_costs_reduce_result():
    df = _df(_PATTERN * 4)
    cheap = run_backtest(df, "T", "m15", cfg=SMCConfig(use_time_filter=False),
                         min_bars=16, fee_pct=0.0, slippage_pct=0.0)
    pricey = run_backtest(df, "T", "m15", cfg=SMCConfig(use_time_filter=False),
                          min_bars=16, fee_pct=0.1, slippage_pct=0.1)
    # издержки не увеличивают итог
    assert pricey.total_r <= cheap.total_r


def test_backtest_mfe_mae_recorded():
    df = _df(_PATTERN * 4)
    rep = run_backtest(df, "T", "m15", cfg=SMCConfig(use_time_filter=False), min_bars=16)
    assert rep.trades >= 1
    for t in rep.detail:
        assert t.mfe_r >= 0.0 and t.mae_r >= 0.0        # ход в плюс/минус неотрицателен в R


def test_backtest_tp_ladder_partial_exits():
    df = _df(_PATTERN * 4)
    rep = run_backtest(df, "T", "m15", cfg=SMCConfig(use_time_filter=False), min_bars=16,
                       tp_ladder=[(1.0, 0.5), (2.0, 0.3), (3.0, 0.2)], move_be_after=1)
    assert rep.trades >= 1
    # частичные выходы дают дробный R (не только ±1 / фикс. RR)
    assert any(abs(t.result_r - round(t.result_r)) > 1e-6 for t in rep.detail) or rep.trades == 0
    # риск ограничен ~1R: полный стоп = −1R минус издержки
    # (перевод в БУ помогает лишь если TP1 задет)
    assert rep.avg_loss_r >= -1.1


def test_parse_ladder():
    from scripts.run_backtest import parse_ladder
    assert parse_ladder("1:0.5,2:0.5") == [(1.0, 0.5), (2.0, 0.5)]
    assert parse_ladder("1.5:1") == [(1.5, 1.0)]
    import pytest
    with pytest.raises(ValueError):
        parse_ladder("1:0.5,2:0.3")                     # доли не суммируются в 1.0


def test_backtest_cli_args():
    from scripts.run_backtest import parse_args
    d = parse_args([])                                  # make backtest без аргументов — как раньше
    assert d.symbol == "BTC/USDT:USDT" and d.tf == "1h" and d.limit == 1000
    assert d.setups is None and d.ladder is None and d.be is None and d.time_filter is False
    a = parse_args(["--symbol", "ETH/USDT:USDT", "--tf", "30m", "--setups", "1:1",
                    "--ladder", "1:0.5,2.26:0.5", "--be", "1", "--time-filter"])
    assert a.tf == "30m" and a.setups == "1:1" and a.be == 1 and a.time_filter is True


def test_max_wait_bars_single_source():
    """Аудит-2 математики [4]: окно ожидания филла — единый источник (50ч) в барах СВОЕГО ТФ;
    захардкоженные 50 баров на 15m давали 12.5ч против живых 50ч."""
    from app.sim.backtest import _max_wait_bars
    assert _max_wait_bars("1h") == 50
    assert _max_wait_bars("m15") == 200
    assert _max_wait_bars("15m") == 200
    assert _max_wait_bars("4h") == 13                   # ceil(50/4)


def test_resample_htf_no_lookahead_and_aggregation():
    """Ресемпл окна 1h -> 4h: честная агрегация OHLC, последняя HTF-свеча частичная
    (как вживую формирующаяся), будущих данных нет."""
    from datetime import datetime, timedelta, timezone

    import pandas as pd

    from app.sim.backtest import _resample_htf
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    n = 6                                    # 6 часовых баров: полный 4h-бин + частичный
    df = pd.DataFrame({
        "dt": [start + timedelta(hours=i) for i in range(n)],
        "open": [100.0 + i for i in range(n)],
        "high": [110.0 + i for i in range(n)],
        "low": [90.0 + i for i in range(n)],
        "close": [105.0 + i for i in range(n)],
        "volume": [1.0] * n,
    })
    htf = _resample_htf(df, 240)
    assert len(htf) == 2
    # бин 00:00-04:00: open первого, high max, low min, close последнего часа бина
    assert htf["open"].iloc[0] == 100.0 and htf["close"].iloc[0] == 108.0
    assert htf["high"].iloc[0] == 113.0 and htf["low"].iloc[0] == 90.0
    assert htf["volume"].iloc[0] == 4.0
    # частичный бин 04:00-…: только бары 4-5, ничего из будущего
    assert htf["open"].iloc[1] == 104.0 and htf["close"].iloc[1] == 110.0
    assert htf["high"].iloc[1] == 115.0 and htf["low"].iloc[1] == 94.0
    assert pd.to_datetime(htf["dt"].iloc[-1]).hour == 4
