"""Тесты журнала: запись сигнала, автофиксация исхода по цене, статистика."""
from __future__ import annotations

from app.core.signal_engine import Side, Signal


def _sig(symbol="BTC/USDT:USDT", side=Side.SHORT, entry=100.0, stop=110.0, tp=80.0,
         created_at="2026-01-01T00:00:00+00:00"):
    return Signal(symbol=symbol, side=side, timeframe="m15", entry=entry, stop=stop,
                  take_profits=[tp], risk_reward=2.0, confidence=0.6, reasons=["тест"],
                  created_at=created_at, indicators_snapshot={"setup": "6226"})


def test_record_signal(fresh_db):
    from app.brain import journal
    sid = journal.record_signal(_sig())
    assert sid == 1
    row = fresh_db.query_one("SELECT * FROM signals WHERE id=?", (sid,))
    assert row["status"] == "open" and row["side"] == "SHORT" and row["tp1"] == 80.0
    ev = fresh_db.query_one("SELECT * FROM signal_events WHERE signal_id=?", (sid,))
    assert ev["event"] == "opened"


def test_update_outcomes_tp(fresh_db):
    from app.brain import journal
    journal.record_signal(_sig())                       # SHORT 100, stop 110, tp 80
    closed = journal.update_outcomes({"BTC/USDT:USDT": 79.0})  # цена ниже tp -> tp1
    assert closed == 1
    row = fresh_db.query_one("SELECT * FROM signals WHERE id=1")
    assert row["status"] == "tp1"
    assert row["result_r"] == 2.0                       # (100-80)/(110-100)
    st = journal.stats()
    assert st["trades"] == 1 and st["wins"] == 1 and st["winrate"] == 1.0


def test_update_outcomes_stop(fresh_db):
    from app.brain import journal
    journal.record_signal(_sig())                       # SHORT, stop 110
    journal.update_outcomes({"BTC/USDT:USDT": 111.0})   # цена выше стопа
    row = fresh_db.query_one("SELECT * FROM signals WHERE id=1")
    assert row["status"] == "stopped" and row["result_r"] == -1.0
    assert journal.stats()["losses"] == 1


def test_stats_by_symbol(fresh_db):
    from app.brain import journal
    journal.record_signal(_sig(symbol="BTC/USDT:USDT"))
    journal.record_signal(_sig(symbol="ETH/USDT:USDT", entry=200.0, stop=210.0, tp=180.0,
                               created_at="2026-01-01T00:15:00+00:00"))
    journal.update_outcomes({"BTC/USDT:USDT": 79.0, "ETH/USDT:USDT": 211.0})  # tp / stop
    by_symbol = journal.stats_by_symbol()
    assert by_symbol["BTC/USDT:USDT"]["trades"] == 1
    assert by_symbol["BTC/USDT:USDT"]["total_r"] == 2.0
    assert by_symbol["ETH/USDT:USDT"]["total_r"] == -1.0
    assert "SOL/USDT:USDT" not in by_symbol           # без сделок — не в разбивке


def test_stats_empty(fresh_db):
    from app.brain import journal
    st = journal.stats()
    assert st["trades"] == 0 and st["winrate"] == 0.0 and st["open"] == 0


def test_semiauto_candidate_confirm(fresh_db):
    from app.brain import journal
    sid = journal.record_candidate(_sig())
    assert fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))["status"] == "candidate"
    assert journal.stats()["open"] == 0                 # кандидат ещё не активен
    assert len(journal.open_candidates()) == 1
    assert journal.confirm_candidate(sid) is True
    assert fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))["status"] == "open"
    assert journal.confirm_candidate(sid) is False      # повторно — уже не кандидат


def test_shadow_outcome_for_rejected(fresh_db):
    from app.brain import journal
    sid_c = journal.record_candidate(_sig())            # SHORT 100, stop 110, tp 80
    sid_r = journal.record_candidate(_sig(created_at="2026-01-01T00:15:00+00:00"))
    journal.confirm_candidate(sid_c)
    journal.reject_candidate(sid_r)

    closed = journal.update_outcomes({"BTC/USDT:USDT": 79.0})   # tp достигнут обоими
    assert closed == 2

    row_c = fresh_db.query_one("SELECT * FROM signals WHERE id=?", (sid_c,))
    assert row_c["status"] == "tp1" and row_c["result_r"] == 2.0

    row_r = fresh_db.query_one("SELECT * FROM signals WHERE id=?", (sid_r,))
    assert row_r["status"] == "rejected"                # статус НЕ меняется
    assert row_r["result_r"] == 2.0                     # но теневой исход посчитан
    ev = fresh_db.query_one(
        "SELECT event FROM signal_events WHERE signal_id=? ORDER BY id DESC", (sid_r,))
    assert ev["event"] == "shadow"

    # публичная статистика видит только подтверждённого
    st = journal.stats()
    assert st["trades"] == 1 and st["wins"] == 1

    # повторный прогон не дублирует теневой исход (result_r уже заполнен)
    assert journal.update_outcomes({"BTC/USDT:USDT": 79.0}) == 0


def test_cohort_stats_edge(fresh_db):
    from app.brain import journal
    # 2 подтверждённых: один в тейк (+2R), один в стоп (-1R); 1 отклонённый: тень в тейк (+2R)
    sid1 = journal.record_candidate(_sig(symbol="BTC/USDT:USDT"))
    sid2 = journal.record_candidate(_sig(symbol="ETH/USDT:USDT",
                                         entry=200.0, stop=210.0, tp=180.0))
    sid3 = journal.record_candidate(_sig(symbol="SOL/USDT:USDT",
                                         entry=300.0, stop=310.0, tp=280.0))
    journal.confirm_candidate(sid1)
    journal.confirm_candidate(sid2)
    journal.reject_candidate(sid3)

    closed = journal.update_outcomes({
        "BTC/USDT:USDT": 79.0,     # sid1: tp (+2R)
        "ETH/USDT:USDT": 211.0,    # sid2: stop (-1R)
        "SOL/USDT:USDT": 279.0,    # sid3: тень tp (+2R)
    })
    assert closed == 3

    cs = journal.cohort_stats()
    assert cs["confirmed"]["trades"] == 2
    assert cs["confirmed"]["expectancy_r"] == 0.5       # (+2 - 1) / 2
    assert cs["rejected"]["trades"] == 1
    assert cs["rejected"]["expectancy_r"] == 2.0
    assert cs["edge_r"] == -1.5                         # оператор отклонил выигрышный
    assert cs["pending_candidates"] == 0
    assert cs["all"]["trades"] == 3


def _bars(rows, start="2026-01-01T00:00:00+00:00", step_min=15):
    import pandas as pd
    o, h, low, c = zip(*rows)
    dts = pd.date_range(start=start, periods=len(rows), freq=f"{step_min}min", tz="UTC")
    return pd.DataFrame({"open": o, "high": h, "low": low, "close": c,
                         "volume": [1.0] * len(rows), "dt": dts})


def test_bars_outcome_requires_fill(fresh_db):
    """Лимитка не исполнена -> сделки нет; после max_wait -> expired (не в статистике)."""
    from app.brain import journal
    # SHORT entry=100 (лимит выше рынка), рынок сразу упал и не вернулся
    journal.record_signal(_sig())                       # created 2026-01-01T00:00
    below = [(90, 91, 89, 90)] * 60                     # 60 баров без касания 100
    res = journal.update_outcomes_from_bars({"BTC/USDT:USDT": _bars(below, start="2026-01-01T00:15:00+00:00")})
    assert len(res) == 1 and res[0]["status"] == "expired"
    row = fresh_db.query_one("SELECT * FROM signals WHERE id=1")
    assert row["status"] == "expired" and row["result_r"] is None
    assert journal.stats()["trades"] == 0               # несостоявшаяся сделка не скорится


def test_bars_outcome_stop_first_same_bar(fresh_db):
    """Бар задел и стоп и тейк -> консервативно стоп."""
    from app.brain import journal
    journal.record_signal(_sig())                       # SHORT 100 / stop 110 / tp 80
    rows = [
        (100, 101, 99, 100),                            # fill (касание 100)
        (100, 111, 79, 90),                             # и стоп (111>110) и тейк (79<80)
    ]
    journal.update_outcomes_from_bars({"BTC/USDT:USDT": _bars(rows, start="2026-01-01T00:15:00+00:00")})
    row = fresh_db.query_one("SELECT * FROM signals WHERE id=1")
    assert row["status"] == "stopped" and row["result_r"] == -1.0


def test_bars_outcome_chronology_tp(fresh_db):
    """Хронология: тейк достигнут ДО стопа -> tp, даже если позже цена дошла бы до стопа."""
    from app.brain import journal
    journal.record_signal(_sig())
    rows = [
        (100, 101, 99, 100),                            # fill
        (95, 96, 79, 80),                               # тейк 80 (стоп не задет)
        (80, 120, 79, 115),                             # позже цена улетела за стоп — уже неважно
    ]
    journal.update_outcomes_from_bars({"BTC/USDT:USDT": _bars(rows, start="2026-01-01T00:15:00+00:00")})
    row = fresh_db.query_one("SELECT * FROM signals WHERE id=1")
    assert row["status"] == "tp1" and row["result_r"] == 2.0


def test_bars_shadow_for_rejected_and_idempotency(fresh_db):
    from app.brain import journal
    sid = journal.record_candidate(_sig())
    journal.reject_candidate(sid)
    rows = [(100, 101, 99, 100), (95, 96, 79, 80)]      # fill -> тейк
    bars = {"BTC/USDT:USDT": _bars(rows, start="2026-01-01T00:15:00+00:00")}
    res = journal.update_outcomes_from_bars(bars)
    assert len(res) == 1 and res[0]["shadow"] is True
    row = fresh_db.query_one("SELECT * FROM signals WHERE id=?", (sid,))
    assert row["status"] == "rejected" and row["result_r"] == 2.0   # тень посчитана
    assert journal.update_outcomes_from_bars(bars) == []           # идемпотентно


def test_cohort_by_operator_events_not_status(fresh_db):
    """Авто-сигнал (record_signal) с исходом НЕ попадает в когорту confirmed."""
    from app.brain import journal
    journal.record_signal(_sig(symbol="BTC/USDT:USDT"))             # авто, без оператора
    sid = journal.record_candidate(_sig(symbol="ETH/USDT:USDT",
                                        entry=200.0, stop=210.0, tp=180.0))
    journal.confirm_candidate(sid)
    journal.update_outcomes({"BTC/USDT:USDT": 79.0, "ETH/USDT:USDT": 179.0})  # оба tp
    cs = journal.cohort_stats()
    assert cs["confirmed"]["trades"] == 1               # только подтверждённый оператором
    assert cs["rejected"]["trades"] == 0


def test_semiauto_candidate_reject(fresh_db):
    from app.brain import journal
    sid = journal.record_candidate(_sig())
    assert journal.reject_candidate(sid) is True
    assert fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))["status"] == "rejected"
    # событий: candidate + rejected
    n = fresh_db.query_one("SELECT COUNT(*) AS c FROM signal_events WHERE signal_id=?", (sid,))["c"]
    assert n == 2
