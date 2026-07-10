"""Тесты реалтайм-стрима (app/data/stream.py): чистые функции + атомарный fill-маркер."""
from __future__ import annotations

import json

import pytest

from app.data import stream


def test_ws_symbol_mapping():
    assert stream.ws_symbol("BTC/USDT:USDT") == "BTCUSDT"
    assert stream.ws_symbol("1000PEPE/USDT:USDT") == "1000PEPEUSDT"
    assert stream.ws_symbol("XAU/USDT:USDT") == "XAUUSDT"


def test_bybit_interval_mapping():
    assert stream.bybit_interval("1h") == "60"
    assert stream.bybit_interval("4h") == "240"
    assert stream.bybit_interval("15m") == "15"
    with pytest.raises(ValueError):
        stream.bybit_interval("3m")


def test_parse_kline():
    raw = json.dumps({"topic": "kline.60.BTCUSDT", "data": [
        {"start": 1, "open": "100", "high": "105.5", "low": "99.1",
         "close": "104", "confirm": False}]})
    evts = stream.parse_kline(raw)
    assert len(evts) == 1
    e = evts[0]
    assert e.symbol_ws == "BTCUSDT" and e.high == 105.5 and e.low == 99.1
    assert e.confirm is False
    # не-kline и мусор -> пусто, без исключений
    assert stream.parse_kline(json.dumps({"op": "pong"})) == []
    assert stream.parse_kline("не json вовсе") == []
    assert stream.parse_kline(json.dumps({"topic": "tickers.BTCUSDT", "data": []})) == []


def test_entry_touched():
    assert stream.entry_touched(100.0, 105.0, 99.0) is True
    assert stream.entry_touched(100.0, 100.0, 100.0) is True    # ровно касание
    assert stream.entry_touched(100.0, 99.9, 99.0) is False
    assert stream.entry_touched(100.0, 105.0, 100.1) is False


def test_try_mark_filled_atomic(fresh_db):
    """Гонка стрим vs bar-скоринг: выигрывает ровно один."""
    from app.brain import journal
    from tests.test_journal import _sig

    sid = journal.record_signal(_sig())
    rows = journal.unfilled_open_signals("BTC/USDT:USDT")
    assert [r["id"] for r in rows] == [sid]
    assert journal.try_mark_filled(sid, "2026-07-11T00:00:00+00:00") is True
    assert journal.try_mark_filled(sid, "2026-07-11T00:01:00+00:00") is False  # второй молчит
    assert journal.unfilled_open_signals("BTC/USDT:USDT") == []
    row = fresh_db.query_one("SELECT filled_at FROM signals WHERE id=?", (sid,))
    assert row["filled_at"] == "2026-07-11T00:00:00+00:00"      # выигравший, не второй
