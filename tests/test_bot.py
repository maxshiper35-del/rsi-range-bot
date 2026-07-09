"""Тесты обвязки бота: scan_frames (движок -> журнал) и рендер команд."""
from __future__ import annotations

import pandas as pd

from app.brain.smc_setups import SMCConfig, SMCEngine
from app.delivery import bot

_BARS_SHORT = [
    (100, 100.5, 99.5, 100), (100, 102.5, 99.8, 102), (102, 104.8, 101.5, 104),
    (104, 103.0, 101.8, 102), (102, 102.2, 100.5, 101), (101, 103.5, 100.8, 103),
    (103, 107.5, 102.8, 107), (107, 110.8, 106.5, 110), (110, 108.5, 106.8, 107),
    (107, 106.0, 104.5, 105), (105, 109.5, 104.8, 109), (109, 113.5, 108.5, 108),
    (108, 108.2, 103.5, 104), (104, 104.2, 99.5, 100), (100, 100.2, 95.5, 96),
    (96, 96.2, 93.5, 94),
]


def _df():
    o, h, low, c = zip(*_BARS_SHORT)
    return pd.DataFrame({"open": o, "high": h, "low": low, "close": c,
                         "volume": [1000.0] * len(_BARS_SHORT)})


def test_scan_frames_records_signal(fresh_db):
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    sig = bot.scan_frames("BTC/USDT:USDT", {"m15": _df()}, eng)
    assert sig is not None
    row = fresh_db.query_one("SELECT * FROM signals WHERE symbol=?", ("BTC/USDT:USDT",))
    assert row is not None and row["status"] == "open"
    # записанный сигнал виден в статистике как открытый
    from app.brain import journal
    assert journal.stats()["open"] == 1


def test_scan_frames_no_record(fresh_db):
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    sig = bot.scan_frames("BTC/USDT:USDT", {"m15": _df()}, eng, record=False)
    assert sig is not None
    assert fresh_db.query_one("SELECT COUNT(*) AS c FROM signals")["c"] == 0


def test_scan_auto_records_open_and_dedups(fresh_db):
    """Авто-режим: сигнал сразу open (в журнале/статистике), повтор — дедуп."""
    from app.brain import journal
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    res = bot.scan_auto("BTC/USDT:USDT", {"m15": _df()}, eng)
    assert res is not None
    sig, sid = res
    assert fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))["status"] == "open"
    assert journal.stats()["open"] == 1                 # сразу в статистике как открытый
    assert bot.scan_auto("BTC/USDT:USDT", {"m15": _df()}, eng) is None  # дедуп


def test_scan_semiauto_dedups_same_setup(fresh_db):
    """Тот же импульс на повторном скане не создаёт второго кандидата (и алерта)."""
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    first = bot.scan_semiauto("BTC/USDT:USDT", {"m15": _df()}, eng)
    assert first is not None
    again = bot.scan_semiauto("BTC/USDT:USDT", {"m15": _df()}, eng)
    assert again is None                                # дедуп по (symbol,tf,side,entry,stop)
    assert fresh_db.query_one("SELECT COUNT(*) AS c FROM signals")["c"] == 1
    # после решения оператора тот же сетап тоже не пере-регистрируется
    from app.brain import journal
    journal.reject_candidate(first[1])
    assert bot.scan_semiauto("BTC/USDT:USDT", {"m15": _df()}, eng) is None


def test_scan_semiauto_records_candidate(fresh_db):
    from app.brain import journal
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    res = bot.scan_semiauto("BTC/USDT:USDT", {"m15": _df()}, eng)
    assert res is not None
    sig, sid = res
    row = fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))
    assert row["status"] == "candidate"                 # не авто-open, а кандидат
    alert = bot.format_candidate_alert(sig, sid)
    assert f"#{sid}" in alert and "/confirm" in alert and "killzone" in alert
    # оператор подтверждает контекст -> активен
    assert journal.confirm_candidate(sid) is True
    assert fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))["status"] == "open"


def test_parse_id_arg():
    assert bot.parse_id_arg("/confirm 12") == 12
    assert bot.parse_id_arg("/reject 7") == 7
    assert bot.parse_id_arg("/confirm") is None
    assert bot.parse_id_arg("/confirm abc") is None
    assert bot.parse_id_arg("/confirm ²³") is None      # isdigit-ловушка: не должно падать
    assert bot.parse_id_arg("") is None
    assert bot.parse_id_arg(None) is None


def test_handle_confirm_reject_flow(fresh_db):
    from app.brain import journal
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    _, sid = bot.scan_semiauto("BTC/USDT:USDT", {"m15": _df()}, eng)

    listing = bot.handle_candidates()
    assert f"#{sid}" in listing                        # кандидат виден в списке

    assert "подтверждён" in bot.handle_confirm(sid)
    assert fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))["status"] == "open"
    assert "не найден" in bot.handle_confirm(sid)      # повторно — уже не кандидат
    assert "Использование" in bot.handle_confirm(None)
    assert bot.handle_candidates() == "Кандидатов нет — жду следующего закрытия свечи."


def test_handle_reject(fresh_db):
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    _, sid = bot.scan_semiauto("BTC/USDT:USDT", {"m15": _df()}, eng)
    assert "отклонён" in bot.handle_reject(sid)
    assert fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))["status"] == "rejected"


def test_format_stats_and_calc():
    empty = bot.format_stats({"trades": 0, "open": 0})
    assert "пустая" in empty
    filled = bot.format_stats({"trades": 10, "open": 2, "wins": 6, "winrate": 0.6,
                               "expectancy_r": 0.4, "total_r": 4.0, "losses": 4})
    assert "Winrate: 60%" in filled
    calc = bot.format_calc(1000, 2.0, 100.0, 98.0)
    assert "Объём" in calc and "Плечо" in calc


def test_format_about_contains_disclaimer_parts():
    txt = bot.format_about()
    assert "НЕ инвестиционный совет" in txt
    assert "/backtest" in txt
    assert "18+" in txt
    assert "это не мы" in txt
    assert "Cornix" in txt


def test_format_backtest_summary():
    txt = bot.format_backtest_summary()
    assert "смоделированный" in txt          # CFTC 4.41-стиль дисклеймера
    assert "НЕ обнаружено" in txt
    assert "/stats" in txt
    assert "hindsight" in txt
    # /backtest — статический текст, не зависит от БД (не упадёт на пустом контексте)


def test_format_glossary():
    txt = bot.format_glossary()
    for term in ("BOS", "CHOCH", "FVG", "killzone", "Setup 1:1", "Свип"):
        assert term.lower() in txt.lower(), term


def test_format_cornix_block():
    from app.core.signal_engine import Side, Signal
    sig = Signal(symbol="BTC/USDT:USDT", side=Side.LONG, timeframe="15m", entry=61900.0,
                 stop=61600.0, take_profits=[62800.0, 63500.0], risk_reward=3.0,
                 confidence=0.68, reasons=[], created_at="2026-01-01T00:00:00+00:00",
                 indicators_snapshot={})
    txt = bot.format_cornix_block(sig)
    assert "Pair: BTC/USDT" in txt and ":USDT" not in txt.split("Pair: ")[1].split("\n")[0]
    assert "Type: Long" in txt
    assert "Entry: 61900" in txt
    assert "Take-Profit 1: 62800" in txt
    assert "Take-Profit 2: 63500" in txt
    assert "Stop-Loss: 61600" in txt          # полное слово, не S/L
    assert "Exchange: Bybit" in txt


def test_cornix_price_precision():
    # {:g} режет до 6 значащих цифр (BTC >100k) и даёт экспоненту на мелких альтах —
    # машиночитаемый блок обязан сохранять точность в фиксированной записи
    assert bot._cornix_price(109853.25) == "109853.25"
    assert bot._cornix_price(0.00001234) == "0.00001234"
    assert bot._cornix_price(61900.0) == "61900"
    assert "e" not in bot._cornix_price(0.00001234).lower()


def test_stats_small_sample_and_no_manual_edits_marks():
    small = bot.format_stats({"trades": 10, "open": 2, "wins": 6, "winrate": 0.6,
                              "expectancy_r": 0.4, "total_r": 4.0, "losses": 4})
    assert "Выборка пока мала" in small and "100+" in small
    assert "без ручных правок" in small

    big = bot.format_stats({"trades": 150, "open": 2, "wins": 90, "winrate": 0.6,
                            "expectancy_r": 0.4, "total_r": 60.0, "losses": 60})
    assert "Выборка пока мала" not in big
    assert "без ручных правок" in big

    ch_small = bot.format_channel_stats({"trades": 10, "open": 1, "winrate": 0.5,
                                         "expectancy_r": 0.1, "total_r": 1.0})
    assert "Выборка пока мала" in ch_small and "без ручных правок" in ch_small
    ch_empty = bot.format_channel_stats({"trades": 0, "open": 0})
    assert "без ручных правок" in ch_empty


def test_format_channel_signal():
    from app.core.signal_engine import Side, Signal
    sig = Signal(symbol="BTC/USDT:USDT", side=Side.SHORT, timeframe="15m", entry=62340.0,
                 stop=62980.0, take_profits=[60893.0, 59000.0], risk_reward=2.26,
                 confidence=0.65, reasons=["свип хая", "слом CHOCH"],
                 created_at="2026-01-01T00:00:00+00:00", indicators_snapshot={"setup": "6226"})
    txt = bot.format_channel_signal(sig, 7)
    assert "RSI RANGE" in txt and "Сигнал #7" in txt and "🔴 SHORT" in txt
    assert "62340" in txt and "60893 / 59000" in txt and "@rsirange_bot" in txt


def test_format_channel_result():
    tp = bot.format_channel_result({"id": 5, "symbol": "ETH/USDT:USDT", "side": "LONG",
                                    "timeframe": "15m", "status": "tp1", "result_r": 2.0})
    assert "✅" in tp and "#5" in tp and "+2.00R" in tp
    st = bot.format_channel_result({"id": 6, "symbol": "BTC/USDT:USDT", "side": "SHORT",
                                    "timeframe": "1h", "status": "stopped", "result_r": -1.0})
    assert "🛑" in st and "-1.00R" in st


def test_format_channel_stats():
    assert "копятся" in bot.format_channel_stats({"trades": 0, "open": 0})
    txt = bot.format_channel_stats({"trades": 20, "open": 3, "winrate": 0.55,
                                    "expectancy_r": 0.2, "total_r": 4.0})
    assert "Winrate: 55%" in txt and "+0.20R" in txt


def test_should_refresh_board():
    assert bot.should_refresh_board(None, "2026-01-02T00:00:00+00:00") is True
    assert bot.should_refresh_board("2026-01-01T00:00:00+00:00",
                                    "2026-01-01T12:00:00+00:00", hours=24) is False
    assert bot.should_refresh_board("2026-01-01T00:00:00+00:00",
                                    "2026-01-02T00:00:00+00:00", hours=24) is True
    assert bot.should_refresh_board("2026-01-01T00:00:00+00:00",
                                    "2026-01-02T00:00:00+00:00", hours=48) is False


def test_format_channel_live_board():
    empty = bot.format_channel_live_board({"trades": 0, "open": 0}, {})
    assert "живое табло" in empty and "копятся" in empty

    st = {"trades": 40, "open": 3, "wins": 24, "winrate": 0.6, "expectancy_r": 0.35, "total_r": 14.0}
    by_symbol = {"BTC/USDT:USDT": {"trades": 20, "wins": 12, "winrate": 0.6,
                                   "expectancy_r": 0.4, "total_r": 8.0}}
    txt = bot.format_channel_live_board(st, by_symbol)
    assert "40" in txt and "BTC/USDT:USDT" in txt and "включая убыточные" in txt
    assert "без ручных правок" in txt

    cohort = {"confirmed": {"trades": 10, "expectancy_r": 0.5},
             "rejected": {"trades": 8, "expectancy_r": -0.1}, "edge_r": 0.6}
    txt2 = bot.format_channel_live_board(st, {}, cohort=cohort)
    assert "Edge оператора: +0.60R" in txt2


def test_format_report():
    cs = {
        "confirmed": {"trades": 40, "wins": 24, "winrate": 0.6, "expectancy_r": 0.35, "total_r": 14.0},
        "rejected": {"trades": 35, "wins": 14, "winrate": 0.4, "expectancy_r": -0.1, "total_r": -3.5},
        "all": {"trades": 75, "wins": 38, "winrate": 0.5067, "expectancy_r": 0.14, "total_r": 10.5},
        "pending_candidates": 2,
        "edge_r": 0.45,
    }
    txt = bot.format_report(cs)
    assert "Edge оператора: +0.45R" in txt
    assert "Подтверждённые: 40 сд" in txt
    assert "Ожидают решения: 2" in txt
    assert "Выборка мала" not in txt                    # обе когорты >= 30

    cs["rejected"]["trades"] = 5
    assert "Выборка мала" in bot.format_report(cs)

    cs["confirmed"] = {"trades": 0, "wins": 0, "winrate": 0.0, "expectancy_r": 0.0, "total_r": 0.0}
    assert "закрытых сделок нет" in bot.format_report(cs)
