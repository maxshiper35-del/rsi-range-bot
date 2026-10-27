import pandas as pd
import pytest

from app.delivery import signal_check as sc

T0 = pd.Timestamp("2026-10-20 12:00", tz="UTC")


def _bars(rows, start=T0, step="15min"):
    """rows: [(open, high, low, close), ...] — бары подряд, первый открывается в start."""
    dt = pd.date_range(start, periods=len(rows), freq=step)
    return pd.DataFrame([dict(dt=d, open=o, high=h, low=lo, close=c)
                         for d, (o, h, lo, c) in zip(dt, rows)])


# --- разбор текста ---

def test_parse_typical_channel_post():
    txt = """🔥 #BTC/USDT LONG
Плечо: x10 (cross)
Вход: 64 200 - 64 500
Стоп: 63 500
🎯 TP1: 65 300
🎯 TP2: 66 000
🎯 TP3: 67 000"""
    s = sc.parse_signal(txt)
    assert (s.coin, s.side, s.entry_lo, s.entry_hi, s.stop) == ("BTC", "LONG", 64200, 64500, 63500)
    assert s.targets == [65300, 66000, 67000]
    assert s.symbol == "BTC/USDT:USDT"


def test_parse_english_short_with_list_targets():
    txt = """SOLUSDT SHORT
Entry zone: 182.5-184
Targets: 178, 175.5, 170
Stop loss: 187.2
Leverage 20x"""
    s = sc.parse_signal(txt)
    assert (s.coin, s.side, s.entry_lo, s.entry_hi, s.stop) == ("SOL", "SHORT", 182.5, 184, 187.2)
    assert s.targets == [178, 175.5, 170]


def test_parse_comma_decimals_and_keyword_on_own_line():
    txt = """#WLD шорт
Вход:
0,4520
Стоп:
0,4700
Цель: 0,4300"""
    s = sc.parse_signal(txt)
    assert (s.coin, s.side, s.entry_lo, s.stop, s.targets) == ("WLD", "SHORT", 0.452, 0.47, [0.43])


def test_parse_market_entry():
    s = sc.parse_signal("ETH/USDT лонг по рынку\nSL 3100\nTP 3400")
    assert s.entry_lo is None and s.stop == 3100 and s.targets == [3400]


def test_breakeven_phrase_does_not_steal_next_line():
    txt = "#ADA LONG\nВход 0.50\nСтоп 0.48\nстоп в безубыток после TP1\nTP1 0.53\nTP2 0.56"
    s = sc.parse_signal(txt)
    assert s.stop == 0.48 and s.targets == [0.53, 0.56]


def test_tp_index_not_glued_to_price():
    s = sc.parse_signal("#DOGE LONG\nEntry 100\nSL 90\nTP1 110\nTP2 120")
    assert s.targets == [110, 120]


def test_parse_one_letter_ticker():
    s = sc.parse_signal("🟢 LONG S/USDT:USDT\nВход: 0.05555\nСтоп: 0.05404")
    assert s.coin == "S" and s.entry_lo == 0.05555


def test_parse_cornix_lists():
    txt = """#BTC/USDT
Exchange: Binance Futures
Signal Type: Regular (Long)
Leverage: Cross (20.0X)
Entry Targets:
1) 64000
2) 63500
Take-Profit Targets:
1) 65000
2) 66000
3) 67500
Stop Targets:
1) 62000"""
    s = sc.parse_signal(txt)
    assert (s.coin, s.side, s.entry_lo, s.entry_hi, s.stop) == ("BTC", "LONG", 63500, 64000, 62000)
    assert s.targets == [65000, 66000, 67500]


def test_parse_numbered_targets_and_coin_label():
    txt = """📍Монета: ARB
Направление: Long 🟢
Плечо: 10x-20x
Точка входа: 0.3150 - 0.3100
🎯 Target 1: 0.3200
🎯 Target 2: 0.3260
❌ StopLoss: 0.3040"""
    s = sc.parse_signal(txt)
    assert (s.coin, s.side, s.entry_lo, s.entry_hi, s.stop) == ("ARB", "LONG", 0.31, 0.315, 0.304)
    assert s.targets == [0.32, 0.326]


def test_parse_our_own_dm_format():
    txt = """🔔 СИГНАЛ #715 | 🔴 SHORT   NEAR/USDT:USDT   ·   1h
💹 Цена сейчас:  4.507
📍 Вход:  4.5915  (по касанию)
🛑 Стоп:  4.664  (ход до стопа 1.6%)
🔁 Трейлинг-стоп ордером на бирже: активация 4.48275 (+1.5R), отступ 0.0145 (0.2R ≈ 0.32%)
🎯 Тейки:
   • TP1: 4.374  (3.0R)
⚖️ R/R до TP1: 1:3.0
🔎 Уверенность: 51%"""
    s = sc.parse_signal(txt)
    assert (s.coin, s.side, s.entry_lo, s.stop, s.targets) == ("NEAR", "SHORT", 4.5915, 4.664,
                                                              [4.374])


@pytest.mark.parametrize("txt", [
    "",
    "Биткоин сегодня интересно себя ведёт",
    "#BTC LONG вход 64000",                      # нет стопа
    "#BTC LONG\nВход 64000\nСтоп 65000",         # стоп над входом лонга
    "BTC вход 64000 стоп 63000",                 # нет стороны
])
def test_parse_rejects(txt):
    assert sc.parse_signal(txt) is None


def test_wrong_side_targets_dropped():
    s = sc.parse_signal("#BTC LONG\nВход 100\nСтоп 90\nTP 95\nTP 110")
    assert s.targets == [110]


# --- прогон по барам ---

LONG = sc.ParsedSignal("BTC", "LONG", 100.0, 100.0, 90.0, [110.0, 120.0])


def test_pullback_fill_then_all_targets():
    bars = _bars([(105, 106, 99, 101), (101, 111, 100, 110), (110, 121, 109, 120)])
    r = sc.evaluate(LONG, bars, T0)
    assert r.status == "all_tp" and r.hit == 2 and r.fill_price == 100.0
    assert r.mfe_r == pytest.approx(2.1)


def test_tp1_then_stop_counts_both():
    bars = _bars([(101, 101, 99, 100), (100, 112, 100, 111), (111, 111, 89, 90)])
    r = sc.evaluate(LONG, bars, T0)
    assert r.status == "stop" and r.hit == 1 and not r.ambiguous


def test_stop_and_target_same_bar_is_stop_first():
    bars = _bars([(101, 101, 99, 100), (100, 115, 85, 100)])
    r = sc.evaluate(LONG, bars, T0)
    assert r.status == "stop" and r.hit == 0 and r.ambiguous


def test_fill_bar_only_counts_stop_not_targets():
    bars = _bars([(101, 125, 99, 120)])          # в баре филла задета и цель — не засчитана
    r = sc.evaluate(LONG, bars, T0)
    assert r.status == "open" and r.hit == 0


def test_no_fill_when_price_never_comes():
    bars = _bars([(105, 108, 102, 107), (107, 115, 104, 114)])
    assert sc.evaluate(LONG, bars, T0).status == "no_fill"


def test_bars_before_post_ignored():
    bars = _bars([(101, 101, 85, 95), (105, 106, 101, 105)], start=T0 - pd.Timedelta("15min"))
    assert sc.evaluate(LONG, bars, T0).status == "no_fill"   # стоп-бар был ДО поста


def test_breakout_long_from_below():
    s = sc.ParsedSignal("BTC", "LONG", 100.0, 100.0, 95.0, [110.0])
    bars = _bars([(97, 99, 96, 98), (98, 101, 97, 100.5), (100.5, 111, 100, 110)])
    r = sc.evaluate(s, bars, T0)
    assert r.fill_price == 100.0 and r.status == "all_tp"


def test_breakout_long_invalidated_by_stop_first():
    s = sc.ParsedSignal("BTC", "LONG", 100.0, 100.0, 95.0, [110.0])
    bars = _bars([(97, 98, 94, 95), (95, 111, 95, 110)])
    assert sc.evaluate(s, bars, T0).status == "no_fill"


def test_short_limit_from_below_and_stop():
    s = sc.ParsedSignal("SOL", "SHORT", 182.5, 184.0, 187.2, [178.0])
    bars = _bars([(180, 183, 179, 182), (182, 188, 181, 187.5)])
    r = sc.evaluate(s, bars, T0)
    assert r.fill_price == 182.5 and r.status == "stop"


def test_market_entry_fills_first_open():
    s = sc.ParsedSignal("ETH", "LONG", None, None, 3100.0, [3400.0])
    bars = _bars([(3200, 3210, 3190, 3205), (3205, 3401, 3200, 3390)])
    r = sc.evaluate(s, bars, T0)
    assert r.fill_price == 3200 and r.status == "all_tp"


def test_pick_timeframe():
    assert sc.pick_timeframe(3600) == "15m"
    assert sc.pick_timeframe(20 * 86400) == "1h"
    assert sc.pick_timeframe(100 * 86400) == "4h"
    assert sc.pick_timeframe(200 * 86400) is None


def test_render_contains_verdict_and_cta():
    bars = _bars([(101, 101, 99, 100), (100, 112, 100, 111), (111, 111, 89, 90)])
    r = sc.evaluate(LONG, bars, T0)
    txt = sc.render_check(LONG, r, T0, "15m")
    assert "TP1" in txt and "✅" in txt and "в стоп" in txt
    assert "/trackrecord" in txt
    nf = sc.render_check(LONG, sc.CheckResult("no_fill"), T0, "15m")
    assert "не исполнился" in nf
