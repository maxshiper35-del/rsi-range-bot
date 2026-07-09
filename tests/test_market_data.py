"""Тесты планировщика опроса: выравнивание на закрытие свечи и один цикл сканирования."""
from __future__ import annotations

import pandas as pd
import pytest

from app.brain.smc_setups import SMCConfig, SMCEngine
from app.data.market_data import poll_once, seconds_to_next_close

_BARS_SHORT = [
    (100, 100.5, 99.5, 100), (100, 102.5, 99.8, 102), (102, 104.8, 101.5, 104),
    (104, 103.0, 101.8, 102), (102, 102.2, 100.5, 101), (101, 103.5, 100.8, 103),
    (103, 107.5, 102.8, 107), (107, 110.8, 106.5, 110), (110, 108.5, 106.8, 107),
    (107, 106.0, 104.5, 105), (105, 109.5, 104.8, 109), (109, 113.5, 108.5, 108),
    (108, 108.2, 103.5, 104), (104, 104.2, 99.5, 100), (100, 100.2, 95.5, 96),
    (96, 96.2, 93.5, 94),
]


def _frames(symbol, timeframes):
    o, h, low, c = zip(*_BARS_SHORT)
    df = pd.DataFrame({"open": o, "high": h, "low": low, "close": c,
                       "volume": [1000.0] * len(_BARS_SHORT)})
    return {tf: df for tf in timeframes}


def test_seconds_to_next_close_bounds():
    # 10:15:00 UTC -> до закрытия часовой свечи ровно 45 мин
    ts = 10 * 3600 + 15 * 60
    assert seconds_to_next_close("1h", ts) == 45 * 60
    # всегда в (0, period]
    for tf, period in (("15m", 900), ("1h", 3600), ("4h", 14400)):
        for ts in (0, 1, period - 1, period, period + 1, 123456.7):
            v = seconds_to_next_close(tf, ts)
            assert 0 < v <= period


def test_seconds_to_next_close_unknown_tf():
    with pytest.raises(ValueError):
        seconds_to_next_close("3m", 0)


def test_poll_once_auto_records_open_signal(fresh_db):
    """Авто-режим (прод по умолчанию): сигнал сразу активен."""
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    found, _ = poll_once(["BTC/USDT:USDT"], ["m15"], engine=eng, frames_loader=_frames,
                         mode="auto")
    assert len(found) == 1
    sig, sid = found[0]
    assert fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))["status"] == "open"
    # повторный такт того же бара — дубль не создаётся
    assert poll_once(["BTC/USDT:USDT"], ["m15"], engine=eng, frames_loader=_frames,
                     mode="auto")[0] == []


def test_poll_once_semiauto_records_candidate(fresh_db):
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    found, _ = poll_once(["BTC/USDT:USDT"], ["m15"], engine=eng, frames_loader=_frames,
                         mode="semiauto")
    assert len(found) == 1
    sig, sid = found[0]
    assert fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))["status"] == "candidate"


def test_fetch_closed_ohlcv_retries(monkeypatch):
    """Общий инстанс: 2 отказа (rate-limit) -> ретрай -> успех; отбрасывает незакрытый бар."""
    import app.data.market_data as md

    calls = {"n": 0}

    class FakeEx:
        def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("10006 Too many visits")
            return [[i * 60000, 1, 2, 0.5, 1.5, 10] for i in range(5)]

    monkeypatch.setattr(md, "_exchange", lambda eid: FakeEx())
    df = md.fetch_closed_ohlcv("BTC/USDT:USDT", "15m", exchange_id="bybit",
                               backoff_base=0.0)
    assert calls["n"] == 3
    assert len(df) == 4                                 # 5 сырых - 1 незакрытый
    assert "dt" in df.columns


def test_fetch_closed_ohlcv_raises_after_retries(monkeypatch):
    import pytest

    import app.data.market_data as md

    class DeadEx:
        def fetch_ohlcv(self, *a, **k):
            raise RuntimeError("биржа лежит")

    monkeypatch.setattr(md, "_exchange", lambda eid: DeadEx())
    with pytest.raises(RuntimeError):
        md.fetch_closed_ohlcv("BTC/USDT:USDT", "15m", exchange_id="bybit",
                              retries=3, backoff_base=0.0)


def test_poll_once_symbol_error_is_skipped(fresh_db):
    def loader(symbol, timeframes):
        if symbol == "BAD":
            raise RuntimeError("биржа лежит")
        return _frames(symbol, timeframes)

    eng = SMCEngine(SMCConfig(use_time_filter=False))
    found, _ = poll_once(["BAD", "BTC/USDT:USDT"], ["m15"], engine=eng, frames_loader=loader,
                         mode="auto")
    assert len(found) == 1                             # BAD пропущен, цикл не упал
