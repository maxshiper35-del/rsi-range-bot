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
    eng = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0))
    found, _ = poll_once(["BTC/USDT:USDT"], ["m15"], engine=eng, frames_loader=_frames,
                         mode="auto")
    assert len(found) == 1
    sig, sid = found[0]
    assert fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))["status"] == "open"
    # повторный такт того же бара — дубль не создаётся
    assert poll_once(["BTC/USDT:USDT"], ["m15"], engine=eng, frames_loader=_frames,
                     mode="auto")[0] == []


def test_poll_once_semiauto_records_candidate(fresh_db):
    eng = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0))
    found, _ = poll_once(["BTC/USDT:USDT"], ["m15"], engine=eng, frames_loader=_frames,
                         mode="semiauto")
    assert len(found) == 1
    sig, sid = found[0]
    row = fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))
    assert row["status"] == "candidate"


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

    eng = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0))
    found, _ = poll_once(["BAD", "BTC/USDT:USDT"], ["m15"], engine=eng, frames_loader=loader,
                         mode="auto")
    assert len(found) == 1                             # BAD пропущен, цикл не упал


def test_poll_once_scores_orphan_symbol_not_in_watchlist(fresh_db):
    # символ выпал из вотчлиста (авто-ротация/ручное удаление), но у него остался
    # открытый сигнал — poll_once обязан его ДОСКОРИТЬ, иначе исход теряется навсегда
    # и публичная статистика недосчитывает закрытых сделок.
    from datetime import datetime, timedelta, timezone

    from app.brain import journal
    from app.core.signal_engine import Side, Signal

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    orphan = Signal(symbol="OLD/USDT:USDT", side=Side.LONG, timeframe="1h",
                    entry=100.0, stop=95.0, take_profits=[110.0], risk_reward=2.0,
                    confidence=0.7, reasons=["ctx"], created_at=t0.isoformat(),
                    indicators_snapshot={"setup": "1:1"})
    sid = journal.record_signal(orphan)                # status=open

    def loader(symbol, timeframes):
        # бары для ЛЮБОГО символа: касание входа (100) на баре 1, тейк (110) на баре 2
        bars = [
            (t0 + timedelta(hours=1), 102, 105, 99, 101),    # fill: low 99 <= 100 <= high 105
            (t0 + timedelta(hours=2), 105, 112, 104, 111),   # tp:   high 112 >= 110
        ]
        dt, o, h, low, c = zip(*bars)
        df = pd.DataFrame({"dt": dt, "open": o, "high": h, "low": low, "close": c,
                           "volume": [1000.0] * len(bars)})
        return {tf: df for tf in timeframes}

    eng = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0))
    # вотчлист НЕ содержит OLD — только ETH; OLD подхватывается orphan-скорингом
    _, closed = poll_once(["ETH/USDT:USDT"], ["1h"], engine=eng, frames_loader=loader,
                          mode="auto")

    row = fresh_db.query_one("SELECT status, result_r FROM signals WHERE id=?", (sid,))
    assert row["status"] == "tp1", f"осиротевший сигнал завис open вместо разрешения: {row}"
    assert row["result_r"] == 2.0
    assert any(c["id"] == sid for c in closed)         # попал в список закрытых (для постинга)


def test_seconds_to_next_close_30m():
    # бриф F: "30m" добавлен в _TF_SECONDS (раньше тихо фильтровался из runtime.timeframes)
    assert seconds_to_next_close("30m", 0) == 30 * 60
    assert seconds_to_next_close("30m", 15 * 60) == 15 * 60


def test_fetch_history_paginates_and_dedupes():
    """Пагинация: страницы по page_limit, без дублей на стыке, последний бар отброшен."""
    from app.data.market_data import fetch_history

    class _StubEx:
        def __init__(self):
            self.calls = []

        def fetch_ohlcv(self, symbol, timeframe, since, limit):
            self.calls.append(since)
            tf_ms = 3600 * 1000
            start = (since // tf_ms) * tf_ms
            # отдаём страницы по 1000 баров, включая бар == since (как реальные биржи)
            return [[start + i * tf_ms, 1.0, 2.0, 0.5, 1.5, 10.0] for i in range(limit)]

    ex = _StubEx()
    df = fetch_history("BTC/USDT:USDT", "1h", target_bars=2500, exchange=ex, page_limit=1000)
    assert len(df) == 2500                              # цель (последний срезан: незакрыт)
    assert len(ex.calls) >= 3                           # минимум 3 страницы по 1000
    assert df["ts"].is_unique and df["ts"].is_monotonic_increasing
    assert "dt" in df.columns


def test_fetch_history_unknown_tf():
    from app.data.market_data import fetch_history
    with pytest.raises(ValueError):
        fetch_history("BTC/USDT:USDT", "3m", target_bars=10, exchange=object())


def test_top_liquid_symbols_filters_and_sorts():
    from app.data.market_data import top_liquid_symbols

    class _StubEx:
        def fetch_tickers(self):
            return {
                "BTC/USDT:USDT": {"quoteVolume": 900e6},
                "ETH/USDT:USDT": {"quoteVolume": 500e6},
                "DOGE/USDT:USDT": {"quoteVolume": 60e6},
                "PEPE/USDT:USDT": {"quoteVolume": 5e6},      # ниже порога объёма
                "BTC/USD:BTC": {"quoteVolume": 800e6},       # inverse — не наш рынок
                "BTC/USDT": {"quoteVolume": 700e6},          # спот — не наш рынок
                "XRP/USDT:USDT": {"quoteVolume": None},      # битый тикер
            }

    top = top_liquid_symbols(10, blacklist=("DOGE/USDT:USDT",), exchange=_StubEx())
    assert top == ["BTC/USDT:USDT", "ETH/USDT:USDT"]         # фильтры + сортировка по объёму
    assert top_liquid_symbols(1, exchange=_StubEx()) == ["BTC/USDT:USDT"]
