"""Тесты детекторов SMC — на детерминированных синтетических OHLCV."""
from __future__ import annotations

import pandas as pd

from app.brain.smc_detectors import (
    Bias,
    Kind,
    LiquidityLevel,
    Swing,
    Zone,
    apply_inversion_rule,
    detect_fvg,
    detect_mitigation,
    detect_order_block,
    detect_smt,
    detect_structure_break,
    detect_sweep,
    find_equal_levels,
    find_pools,
    find_swings,
    market_structure,
)


def make_df(rows) -> pd.DataFrame:
    """rows: список (open, high, low, close). Объём фиксированный."""
    o, h, low, c = zip(*rows)
    return pd.DataFrame({"open": o, "high": h, "low": low, "close": c,
                         "volume": [1000.0] * len(rows)})


# --- 1.1 свинги ------------------------------------------------------------
def test_find_swings_high_and_low():
    rows = [
        (1, 1, 0, 1),
        (2, 2, -1, 2),
        (4, 5, 1, 4),      # idx2: swing HIGH (5 > соседей)
        (2, 2, -1, 2),
        (-3, 1, -5, -3),   # idx4: swing LOW (-5 < соседей)
        (1, 2, -1, 1),
        (1, 1, 0, 1),
    ]
    sw = find_swings(make_df(rows), left=2, right=2)
    assert Swing(2, 5.0, Kind.HIGH) in sw
    assert Swing(4, -5.0, Kind.LOW) in sw


# --- 1.2 FVG ---------------------------------------------------------------
def test_detect_fvg_bullish():
    rows = [
        (9, 10, 8, 9),
        (10, 12, 9.5, 11),
        (11.5, 13, 11, 12),   # low[2]=11 > high[0]=10 -> бычий FVG [10,11]
    ]
    fvg = detect_fvg(make_df(rows))
    assert len(fvg) == 1
    z = fvg[0]
    assert z.bias == Bias.BULL and z.bottom == 10.0 and z.top == 11.0


def test_detect_fvg_bearish():
    rows = [
        (21, 22, 20, 21),
        (20, 20.5, 18.5, 19),
        (18.5, 19, 17, 18),   # high[2]=19 < low[0]=20 -> медвежий FVG [19,20]
    ]
    fvg = detect_fvg(make_df(rows))
    assert len(fvg) == 1
    z = fvg[0]
    assert z.bias == Bias.BEAR and z.bottom == 19.0 and z.top == 20.0


# --- 1.3 ликвидность -------------------------------------------------------
def test_find_equal_levels():
    swings = [Swing(1, 10.0, Kind.HIGH), Swing(3, 10.05, Kind.HIGH)]
    levels = find_equal_levels(swings, atr=1.0, tol_atr=0.1)
    assert len(levels) == 1
    assert levels[0].kind == "EQH"
    assert abs(levels[0].price - 10.025) < 1e-9


def test_find_pools():
    swings = [Swing(1, 5.00, Kind.LOW), Swing(3, 5.05, Kind.LOW), Swing(5, 5.10, Kind.LOW)]
    pools = find_pools(swings, atr=1.0, tol_atr=0.15, min_count=2)
    assert len(pools) == 1
    assert pools[0].kind == "POOL" and abs(pools[0].price - 5.05) < 1e-9


# --- 1.4 свип --------------------------------------------------------------
def test_detect_sweep_high():
    rows = [(9, 9.5, 8.5, 9), (9, 9.6, 8.8, 9.2), (9.5, 10.5, 9.0, 9.8)]  # прокол 10 вверх, закрытие ниже
    ev = detect_sweep(make_df(rows), LiquidityLevel(10.0, 0, "EQH"))
    assert ev is not None and ev.direction == Kind.HIGH and ev.confirmed


def test_detect_sweep_low():
    rows = [(11, 11.5, 10.5, 11), (11, 11.2, 10.6, 10.9), (10.5, 11.0, 9.5, 10.2)]  # прокол 10 вниз, закрытие выше
    ev = detect_sweep(make_df(rows), LiquidityLevel(10.0, 0, "EQL"))
    assert ev is not None and ev.direction == Kind.LOW and ev.confirmed


# --- 1.5 слом структуры ----------------------------------------------------
_UPTREND_SWINGS = [Swing(1, 5.0, Kind.LOW), Swing(3, 10.0, Kind.HIGH), Swing(5, 7.0, Kind.LOW)]


def test_structure_break_choch():
    df = make_df([(6, 6, 5, 6)] * 6 + [(5, 5, 3.5, 4.0)])  # close=4.0 < опорный лой 5.0
    br = detect_structure_break(df, _UPTREND_SWINGS, trend=Bias.BULL)
    assert br is not None and br.kind == "CHOCH"
    assert br.direction == Bias.BEAR and br.pivot_price == 5.0 and br.body_confirmed


def test_structure_break_bos():
    df = make_df([(6, 6, 5, 6)] * 6 + [(10.5, 11.5, 10, 11.0)])  # close=11.0 > хай 10.0
    br = detect_structure_break(df, _UPTREND_SWINGS, trend=Bias.BULL)
    assert br is not None and br.kind == "BOS" and br.direction == Bias.BULL


# --- 1.7 mitigation / инверсия ---------------------------------------------
def test_mitigation_and_inversion():
    zone = Zone("FVG", top=11.0, bottom=10.0, index_start=0, index_end=2, bias=Bias.BULL)
    rows = [
        (9, 10, 8, 9),
        (10, 12, 9.5, 11),
        (11.5, 13, 11, 12),
        (11, 11.5, 10.5, 11),    # idx3: вернулись в зону [10,11] -> mitigation
        (9, 12, 9, 12),          # idx4: тело 9..12 полностью перекрыло зону -> инверсия
    ]
    df = make_df(rows)
    assert detect_mitigation(df, zone, since_index=0) is True
    assert apply_inversion_rule(df, zone).inverted is True


# --- 1.6 OB / BB -----------------------------------------------------------
_BARS_SHORT = [
    (100, 100.5, 99.5, 100), (100, 102.5, 99.8, 102), (102, 104.8, 101.5, 104),
    (104, 103.0, 101.8, 102), (102, 102.2, 100.5, 101), (101, 103.5, 100.8, 103),
    (103, 107.5, 102.8, 107), (107, 110.8, 106.5, 110), (110, 108.5, 106.8, 107),
    (107, 106.0, 104.5, 105), (105, 109.5, 104.8, 109), (109, 113.5, 108.5, 108),
    (108, 108.2, 103.5, 104), (104, 104.2, 99.5, 100), (100, 100.2, 95.5, 96),
    (96, 96.2, 93.5, 94),
]


def test_detect_order_block_breaker():
    df = make_df(_BARS_SHORT)
    sw = find_swings(df)
    br = detect_structure_break(df, sw, market_structure(df, sw))
    ob = detect_order_block(df, sw, br, Bias.BEAR)
    assert ob is not None
    assert ob.kind == "BB"                 # слом = CHOCH -> брейкер
    assert abs(ob.top - 113.5) < 0.1       # вершина рывка перед сломом
    assert ob.bias == Bias.BEAR


# --- 1.8 SMT ---------------------------------------------------------------
def test_detect_smt_direct_divergence():
    # A обновляет лой на последнем баре, B — нет -> прямая дивергенция -> лонг B
    base = [(10, 10.5, 9.5, 10)] * 22
    a = make_df(base + [(10, 10.2, 8.0, 8.2)])   # новый лой
    b = make_df(base + [(10, 10.4, 9.6, 10.1)])  # без нового лоя
    smt = detect_smt(a, b, correlation="direct", lookback=20)
    assert smt is not None and smt.side == "LONG"
