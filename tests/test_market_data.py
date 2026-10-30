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
    # площадка исполнения (BingX): недоступная монета выпадает ДО отсечения топ-N и не
    # занимает место в списке
    allowed = {"ETH/USDT:USDT", "DOGE/USDT:USDT"}
    assert top_liquid_symbols(1, exchange=_StubEx(), allowed=allowed) == ["ETH/USDT:USDT"]
    assert top_liquid_symbols(10, exchange=_StubEx(), allowed=set()) == []


def test_poll_once_paused_scores_open_but_finds_no_new(fresh_db):
    """Пауза (scan_new=False): новых сигналов нет, но открытая сделка доскоривается.
    Регрессия 04.10.2026: пауза без скоринга оставляла сигналы open неделями, дашборд
    показывал плавающие −86R по давно закрытым сделкам (реально +0.66R)."""
    from datetime import datetime, timedelta, timezone

    from app.brain import journal
    from app.core.signal_engine import Side, Signal

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    held = Signal(symbol="ETH/USDT:USDT", side=Side.LONG, timeframe="1h",
                  entry=100.0, stop=95.0, take_profits=[110.0], risk_reward=2.0,
                  confidence=0.7, reasons=["ctx"], created_at=t0.isoformat(),
                  indicators_snapshot={"setup": "1:1"})
    sid = journal.record_signal(held)

    def loader(symbol, timeframes):
        bars = [
            (t0 + timedelta(hours=1), 102, 105, 99, 101),    # fill
            (t0 + timedelta(hours=2), 105, 112, 104, 111),   # tp
        ]
        dt, o, h, low, c = zip(*bars)
        df = pd.DataFrame({"dt": dt, "open": o, "high": h, "low": low, "close": c,
                           "volume": [1000.0] * len(bars)})
        return {tf: df for tf in timeframes}

    eng = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0))
    found, closed = poll_once(["ETH/USDT:USDT"], ["1h"], engine=eng, frames_loader=loader,
                              mode="auto", scan_new=False)

    assert found == []                                  # новых сигналов пауза не плодит
    assert fresh_db.query_one("SELECT COUNT(*) AS n FROM signals")["n"] == 1
    row = fresh_db.query_one("SELECT status, result_r FROM signals WHERE id=?", (sid,))
    assert row["status"] == "tp1" and row["result_r"] == 2.0
    done = [c for c in closed if c["id"] == sid and c["status"] == "tp1"]
    assert done and done[0]["closed_at"]                # время бара выхода — для гейта свежести


def test_poll_once_engine_sees_engine_window_while_scoring_gets_deep_frame(fresh_db):
    """Углублённый под скоринг фетч (до 999 баров) не доходит до движка: длина окна
    меняла сигналы (06.10.2026). Движку — последние ENGINE_WINDOW, скорингу — всё."""
    from app.data import market_data

    seen = []

    class _Spy:
        def analyze_multi(self, frames, symbol):
            seen.append({tf: len(df) for tf, df in frames.items()})
            return []

    def deep_loader(symbol, timeframes):
        n = 600
        df = pd.DataFrame({"open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
                           "close": [100.0] * n, "volume": [1.0] * n})
        return {tf: df for tf in timeframes}

    poll_once(["BTC/USDT:USDT"], ["1h", "4h"], engine=_Spy(), frames_loader=deep_loader,
              mode="auto")
    assert seen == [{"1h": market_data.ENGINE_WINDOW, "4h": market_data.ENGINE_WINDOW}]


# ---------------------------------------------------------------------------
# 10006 (аудит надёжности 30.10): кеш старших ТФ, ретраи такта, итог такта
# ---------------------------------------------------------------------------
_PERIOD_S = {"1h": 3600, "4h": 14400}
# 2026-10-29 04:00:02 UTC — закрытие 4h-бара 00:00 (и часового 03:00)
_T_0400 = 1793246402.0


class _ClockEx:
    """Фейковая биржа: бары, выровненные по «текущему» времени теста, последний —
    формирующийся (как отдаёт Bybit). Значения — функция времени бара: один и тот же бар
    в любом ответе одинаков (закрытый бар биржа не переписывает). lag=True — биржа ещё
    не выкатила новый бар на закрытии (последний сырой бар — только что закрытый)."""

    def __init__(self, clock: dict):
        self.clock = clock
        self.calls: list = []
        self.lag = False
        self.fail_first = 0                              # сколько первых вызовов — 10006

    def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
        self.calls.append(timeframe)
        if self.fail_first > 0:
            self.fail_first -= 1
            raise RuntimeError('bybit {"retCode":10006,"retMsg":"Too many visits. '
                               'Exceeded the API Rate Limit."}')
        period_ms = _PERIOD_S[timeframe] * 1000
        forming = int(self.clock["now"] * 1000) // period_ms * period_ms
        last = forming - period_ms if self.lag else forming
        rows = []
        for k in range(limit):
            ts = last - (limit - 1 - k) * period_ms
            base = 100.0 + (ts // period_ms) % 17
            vol = 10.0 + (ts // period_ms) % 5
            rows.append([ts, base, base + 1.5, base - 1.0, base + 0.5, vol])
        return rows


@pytest.fixture()
def clock_ex(monkeypatch):
    import app.data.market_data as md

    clock = {"now": _T_0400}
    ex = _ClockEx(clock)
    monkeypatch.setattr(md, "_exchange", lambda eid: ex)
    monkeypatch.setattr(md, "_now_ts", lambda: clock["now"])
    md._HTF_CACHE.clear()
    yield ex
    md._HTF_CACHE.clear()


def test_last_closed_open_ms_alignment():
    from app.data.market_data import last_closed_open_ms

    h0000 = 1793232000 * 1000                            # 2026-10-29 00:00 UTC, мс
    assert last_closed_open_ms("4h", _T_0400) == h0000              # 04:00:02 -> бар 00:00
    assert last_closed_open_ms("4h", _T_0400 + 3 * 3600) == h0000   # 07:00:02 -> всё ещё 00:00
    assert last_closed_open_ms("4h", _T_0400 - 3) == h0000 - 14400 * 1000  # 03:59:59 -> 20:00
    assert last_closed_open_ms("1h", _T_0400) == h0000 + 3 * 3600 * 1000   # 04:00:02 -> 03:00


def test_htf_cache_reuses_until_next_close_and_matches_fresh_fetch(clock_ex):
    """4h-бары между закрытиями — те же: кеш экономит 3 запроса из 4, и отдаёт ровно то,
    что вернул бы свежий фетч (вход движка бит-в-бит прежний)."""
    import app.data.market_data as md

    first = md.fetch_closed_ohlcv_cached("BTC/USDT:USDT", "4h", limit=200, exchange_id="bybit")
    assert clock_ex.calls == ["4h"] and len(first) == 199
    for hours in (1, 2, 3):                              # 05:00, 06:00, 07:00 — из кеша
        clock_ex.clock["now"] = _T_0400 + hours * 3600
        got = md.fetch_closed_ohlcv_cached("BTC/USDT:USDT", "4h", limit=200, exchange_id="bybit")
        fresh = md.fetch_closed_ohlcv("BTC/USDT:USDT", "4h", limit=200, exchange_id="bybit")
        pd.testing.assert_frame_equal(got, fresh)       # паритет со свежим фетчем
    assert clock_ex.calls.count("4h") == 1 + 3          # только сверочные fresh-вызовы теста
    clock_ex.calls.clear()
    clock_ex.clock["now"] = _T_0400 + 4 * 3600          # 08:00:02 — закрылся новый 4h-бар
    nxt = md.fetch_closed_ohlcv_cached("BTC/USDT:USDT", "4h", limit=200, exchange_id="bybit")
    assert clock_ex.calls == ["4h"]                      # промах -> фетч
    assert int(nxt["ts"].iloc[-1]) == md.last_closed_open_ms("4h", clock_ex.clock["now"])


def test_htf_cache_does_not_store_lagging_response(clock_ex):
    """Биржа на закрытии ещё не выкатила новый бар: такой ответ отдаём как есть (как без
    кеша), но НЕ кешируем — иначе запаздывание прожило бы 4 часа вместо одного такта."""
    import app.data.market_data as md

    clock_ex.lag = True
    stale = md.fetch_closed_ohlcv_cached("ETH/USDT:USDT", "4h", limit=200, exchange_id="bybit")
    assert int(stale["ts"].iloc[-1]) < md.last_closed_open_ms("4h", _T_0400)
    clock_ex.lag = False
    clock_ex.clock["now"] = _T_0400 + 3600
    md.fetch_closed_ohlcv_cached("ETH/USDT:USDT", "4h", limit=200, exchange_id="bybit")
    assert clock_ex.calls == ["4h", "4h"]                # второй вызов — снова биржа


def test_htf_cache_limit_and_copy_semantics(clock_ex):
    import app.data.market_data as md

    md.fetch_closed_ohlcv_cached("SOL/USDT:USDT", "4h", limit=200, exchange_id="bybit")
    clock_ex.clock["now"] = _T_0400 + 3600
    small = md.fetch_closed_ohlcv_cached("SOL/USDT:USDT", "4h", limit=100, exchange_id="bybit")
    assert len(small) == 99 and clock_ex.calls == ["4h"]        # меньший limit — хвост кеша
    small.loc[:, "close"] = -1.0                                # потребитель портит свою копию
    again = md.fetch_closed_ohlcv_cached("SOL/USDT:USDT", "4h", limit=200, exchange_id="bybit")
    assert (again["close"] > 0).all()                           # кеш не задет
    md.fetch_closed_ohlcv_cached("SOL/USDT:USDT", "4h", limit=500, exchange_id="bybit")
    assert clock_ex.calls == ["4h", "4h"]                       # больший limit — фетч


def test_poll_once_fetches_htf_once_per_its_close(fresh_db, clock_ex):
    """Боевой загрузчик такта: 1h — каждый час свежий, 4h — раз на закрытие 4h-бара;
    движок видит тот же 4h-кадр, что и при фетче каждый час."""
    import app.data.market_data as md

    seen = []

    class _Spy:
        def analyze_multi(self, frames, symbol):
            seen.append({tf: df.copy() for tf, df in frames.items()})
            return []

    for hours in (0, 1, 2, 3, 4):                        # 04:00 … 08:00
        clock_ex.clock["now"] = _T_0400 + hours * 3600
        poll_once(["BTC/USDT:USDT"], ["1h", "4h"], engine=_Spy(), mode="auto")
    assert clock_ex.calls.count("1h") == 5
    assert clock_ex.calls.count("4h") == 2               # 04:00 и 08:00 (было бы 5)
    for i in (1, 2, 3):                                  # 05–07: тот же 4h-кадр, что в 04:00
        pd.testing.assert_frame_equal(seen[i]["4h"], seen[0]["4h"])
    assert len(seen[0]["4h"]) == md.ENGINE_WINDOW and len(seen[0]["1h"]) == md.ENGINE_WINDOW
    assert not seen[4]["4h"].equals(seen[0]["4h"])       # 08:00 — новый закрытый бар
    st = md.LAST_POLL_STATS
    assert (st["ok"], st["failed"], st["requests"], st["cache_hits"]) == (1, 0, 2, 0)


def test_poll_once_stats_count_cache_hits(fresh_db, clock_ex):
    import app.data.market_data as md

    class _Null:
        def analyze_multi(self, frames, symbol):
            return []

    poll_once(["BTC/USDT:USDT", "ETH/USDT:USDT"], ["1h", "4h"], engine=_Null(), mode="auto")
    clock_ex.clock["now"] = _T_0400 + 3600
    poll_once(["BTC/USDT:USDT", "ETH/USDT:USDT"], ["1h", "4h"], engine=_Null(), mode="auto")
    st = md.LAST_POLL_STATS
    assert (st["ok"], st["failed"], st["requests"], st["cache_hits"]) == (2, 0, 2, 2)


def test_poll_once_survives_three_rate_limits_in_a_row(fresh_db, clock_ex, monkeypatch):
    """Три 10006 подряд (t, +2с, +6с — всё в горячем окне) раньше выбрасывали символ из
    такта целиком; четвёртая попытка такта его спасает."""
    import time

    import app.data.market_data as md

    monkeypatch.setattr(time, "sleep", lambda s: None)
    clock_ex.fail_first = 3

    class _Null:
        def analyze_multi(self, frames, symbol):
            return []

    poll_once(["BTC/USDT:USDT"], ["1h"], engine=_Null(), mode="auto")
    st = md.LAST_POLL_STATS
    assert (st["ok"], st["failed"], st["rate_limited"], st["requests"]) == (1, 0, 3, 4)


def test_poll_once_counts_failed_loads(fresh_db):
    import app.data.market_data as md

    def dead(symbol, timeframes):
        raise RuntimeError("биржа лежит")

    poll_once(["BTC/USDT:USDT", "ETH/USDT:USDT"], ["1h"], engine=SMCEngine(), frames_loader=dead,
              mode="auto")
    assert (md.LAST_POLL_STATS["ok"], md.LAST_POLL_STATS["failed"]) == (0, 2)


def test_rate_limit_retry_is_info_other_errors_warn(monkeypatch, caplog):
    """Штатный 10006 после закрытия бара — INFO (ретрай его гасит, счётчик в итоге такта);
    прочие сбои — WARNING. Хвост WARNING/ERROR — то, что видит админ-панель."""
    import logging

    import app.data.market_data as md

    errors = [RuntimeError('bybit {"retCode":10006,"retMsg":"Too many visits."}'),
              RuntimeError("Connection reset by peer")]

    class _Ex:
        def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
            if errors:
                raise errors.pop(0)
            return [[i * 3600000, 1, 2, 0.5, 1.5, 10] for i in range(3)]

    monkeypatch.setattr(md, "_exchange", lambda eid: _Ex())
    before = md._stats_snapshot()
    with caplog.at_level(logging.INFO, logger="market_data"):
        md.fetch_closed_ohlcv("BTC/USDT:USDT", "1h", exchange_id="bybit", backoff_base=0.0)
    levels = [(r.levelno, "10006" in r.getMessage()) for r in caplog.records
              if r.name == "market_data"]
    assert levels == [(logging.INFO, True), (logging.WARNING, False)]
    after = md._stats_snapshot()
    assert after["rate_limited"] - before["rate_limited"] == 1
    assert after["requests"] - before["requests"] == 3


def test_poll_data_ok_semantics():
    """Метка «бары биржи пришли» для сторожа слепоты: хоть один символ загрузился или
    грузить было нечего; все загрузки упали / итога нет — нет метки."""
    from app.data.market_data import poll_data_ok

    assert poll_data_ok({"ok": 1, "failed": 13}) is True
    assert poll_data_ok({"ok": 0, "failed": 0}) is True         # пустой вотчлист — не слепота
    assert poll_data_ok({"ok": 0, "failed": 14}) is False       # Bybit лежит/бан IP
    assert poll_data_ok({}) is False                            # такт не дошёл до итога


def test_is_rate_limit_classification():
    import ccxt

    from app.data.market_data import _is_rate_limit

    assert _is_rate_limit(ccxt.RateLimitExceeded("bybit 10006"))
    assert _is_rate_limit(ccxt.DDoSProtection("403"))
    assert _is_rate_limit(RuntimeError('{"retCode":10006}'))
    assert not _is_rate_limit(ccxt.NetworkError("DNS"))
    assert not _is_rate_limit(ccxt.BadSymbol("bybit does not have market symbol"))
