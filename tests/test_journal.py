"""Тесты журнала: запись сигнала, автофиксация исхода по цене, статистика."""
from __future__ import annotations

from app.core.signal_engine import Side, Signal


def _sig(symbol="BTC/USDT:USDT", side=Side.SHORT, entry=100.0, stop=110.0, tp=80.0,
         created_at="2026-01-01T00:00:00+00:00", setup="6226"):
    return Signal(symbol=symbol, side=side, timeframe="m15", entry=entry, stop=stop,
                  take_profits=[tp], risk_reward=2.0, confidence=0.6, reasons=["тест"],
                  created_at=created_at, indicators_snapshot={"setup": setup})


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


def test_stats_for_period_scoped_by_closed_at(fresh_db):
    """Карточка недели: только сделки, ЗАКРЫТЫЕ внутри окна, а не всё время жизни бота."""
    from app.brain import journal
    journal.record_signal(_sig(symbol="BTC/USDT:USDT"))
    journal.record_signal(_sig(symbol="ETH/USDT:USDT", entry=200.0, stop=210.0, tp=180.0,
                               created_at="2026-01-01T00:15:00+00:00"))
    journal.update_outcomes({"BTC/USDT:USDT": 79.0, "ETH/USDT:USDT": 211.0})  # tp / stop
    # ETH "закрылась" давно — переносим closed_at за пределы окна недели
    fresh_db.execute("UPDATE signals SET closed_at=? WHERE symbol='ETH/USDT:USDT'",
                     ("2020-01-01T00:00:00+00:00",))
    from app import clock
    wk = journal.stats_for_period("2025-01-01T00:00:00+00:00", clock.utcnow_iso())
    assert wk["trades"] == 1                      # только BTC — ETH вне окна
    assert wk["total_r"] == 2.0
    assert wk["outcomes"] == [True]


def test_stats_for_period_outcomes_chronological(fresh_db):
    from app.brain import journal
    journal.record_signal(_sig(symbol="BTC/USDT:USDT"))                      # закроется stop
    journal.record_signal(_sig(symbol="ETH/USDT:USDT", entry=200.0, stop=210.0, tp=180.0,
                               created_at="2026-01-01T00:15:00+00:00"))      # закроется tp
    journal.update_outcomes({"BTC/USDT:USDT": 111.0, "ETH/USDT:USDT": 179.0})
    # BTC закрылась раньше ETH -> outcomes должны идти в этом порядке
    fresh_db.execute("UPDATE signals SET closed_at=? WHERE symbol='BTC/USDT:USDT'",
                     ("2026-06-01T00:00:00+00:00",))
    fresh_db.execute("UPDATE signals SET closed_at=? WHERE symbol='ETH/USDT:USDT'",
                     ("2026-06-02T00:00:00+00:00",))
    wk = journal.stats_for_period("2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00")
    assert wk["outcomes"] == [False, True]        # BTC (stop) раньше ETH (tp)


def test_stats_for_period_empty(fresh_db):
    from app.brain import journal
    wk = journal.stats_for_period("2020-01-01T00:00:00+00:00", "2020-01-02T00:00:00+00:00")
    assert wk == {"trades": 0, "wins": 0, "losses": 0, "winrate": 0.0,
                  "expectancy_r": 0.0, "total_r": 0.0, "outcomes": []}


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


def test_stats_by_setup(fresh_db):
    from app.brain import journal
    journal.record_signal(_sig(symbol="BTC/USDT:USDT", setup="6226"))
    journal.record_signal(_sig(symbol="ETH/USDT:USDT", entry=200.0, stop=210.0, tp=180.0,
                               created_at="2026-01-01T00:15:00+00:00", setup="1:1"))
    journal.update_outcomes({"BTC/USDT:USDT": 79.0, "ETH/USDT:USDT": 211.0})  # tp / stop
    by_setup = journal.stats_by_setup()
    assert by_setup["6226"]["trades"] == 1
    assert by_setup["6226"]["total_r"] == 2.0
    assert by_setup["1:1"]["trades"] == 1
    assert by_setup["1:1"]["total_r"] == -1.0


def test_stats_by_setup_ltf_bb_ob_not_collapsed(fresh_db):
    from app.brain import journal
    journal.record_signal(_sig(symbol="BTC/USDT:USDT", setup="LTF-BB"))
    journal.record_signal(_sig(symbol="ETH/USDT:USDT", entry=200.0, stop=210.0, tp=180.0,
                               created_at="2026-01-01T00:15:00+00:00", setup="LTF-OB"))
    journal.update_outcomes({"BTC/USDT:USDT": 79.0, "ETH/USDT:USDT": 179.0})  # оба tp
    by_setup = journal.stats_by_setup()
    assert "LTF-BB" in by_setup and "LTF-OB" in by_setup
    assert by_setup["LTF-BB"]["trades"] == 1 and by_setup["LTF-OB"]["trades"] == 1


def test_consecutive_losses(fresh_db):
    from app.brain import journal
    # win, затем стопы подряд на LTF-BB, закрываем по одному звонку -> нарастающий closed_at
    journal.record_signal(_sig(symbol="BTC/USDT:USDT", setup="LTF-BB"))
    journal.update_outcomes({"BTC/USDT:USDT": 79.0})           # tp -> win
    journal.record_signal(_sig(symbol="ETH/USDT:USDT", entry=200.0, stop=210.0, tp=180.0,
                               created_at="2026-01-01T00:15:00+00:00", setup="LTF-BB"))
    journal.update_outcomes({"ETH/USDT:USDT": 211.0})          # stop -> loss #1
    journal.record_signal(_sig(symbol="SOL/USDT:USDT", entry=50.0, stop=55.0, tp=40.0,
                               created_at="2026-01-01T00:30:00+00:00", setup="LTF-BB"))
    journal.update_outcomes({"SOL/USDT:USDT": 56.0})           # stop -> loss #2
    assert journal.consecutive_losses("LTF-BB", n=5) == 2
    assert journal.consecutive_losses("LTF-BB", n=2) == 2      # порог достигнут -> ранний возврат
    assert journal.consecutive_losses("1:1", n=5) == 0          # другой сетап — не задет


def test_stats_empty(fresh_db):
    from app.brain import journal
    st = journal.stats()
    assert st["trades"] == 0 and st["winrate"] == 0.0 and st["open"] == 0


def test_semiauto_candidate_confirm(fresh_db):
    from app.brain import journal
    sid = journal.record_candidate(_sig())
    row = fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))
    assert row["status"] == "candidate"
    assert journal.stats()["open"] == 0                 # кандидат ещё не активен
    assert len(journal.open_candidates()) == 1
    assert journal.confirm_candidate(sid) is True
    assert fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))["status"] == "open"
    assert journal.confirm_candidate(sid) is False      # повторно — уже не кандидат


def test_expire_stale_candidates(fresh_db):
    from app import clock
    from app.brain import journal
    # старый кандидат: created_at из фикстуры (2026-01-01) — давно в прошлом относительно "сейчас"
    sid_old = journal.record_candidate(_sig())
    # свежий кандидат: created_at = реальное "сейчас"
    sid_fresh = journal.record_candidate(_sig(symbol="ETH/USDT:USDT", entry=200.0, stop=210.0,
                                              tp=180.0, created_at=clock.utcnow_iso()))
    expired = journal.expire_stale_candidates(max_age_minutes=60)
    assert expired == [sid_old]
    assert fresh_db.query_one(
        "SELECT status FROM signals WHERE id=?", (sid_old,))["status"] == "expired"
    assert fresh_db.query_one(
        "SELECT status FROM signals WHERE id=?", (sid_fresh,))["status"] == "candidate"
    ev = fresh_db.query_one(
        "SELECT event FROM signal_events WHERE signal_id=? ORDER BY id DESC LIMIT 1", (sid_old,))
    assert ev["event"] == "expired"
    assert journal.stats()["trades"] == 0               # result_r=NULL -> не в публичной стате
    assert journal.expire_stale_candidates(max_age_minutes=60) == []  # идемпотентно


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
    bars = {"BTC/USDT:USDT": _bars(below, start="2026-01-01T00:15:00+00:00")}
    res = journal.update_outcomes_from_bars(bars)
    assert len(res) == 1 and res[0]["status"] == "expired"
    row = fresh_db.query_one("SELECT * FROM signals WHERE id=1")
    assert row["status"] == "expired" and row["result_r"] is None
    assert journal.stats()["trades"] == 0               # несостоявшаяся сделка не скорится


def test_bars_outcome_stop_first_same_bar(fresh_db):
    """Бар задел и стоп и тейк -> консервативно стоп. Fill идёт отдельным событием."""
    from app.brain import journal
    journal.record_signal(_sig(setup="LTF-BB"))         # SHORT 100 / stop 110 / tp 80
    rows = [
        (100, 101, 99, 100),                            # fill (касание 100)
        (100, 111, 79, 90),                             # и стоп (111>110) и тейк (79<80)
    ]
    res = journal.update_outcomes_from_bars(
        {"BTC/USDT:USDT": _bars(rows, start="2026-01-01T00:15:00+00:00")})
    row = fresh_db.query_one("SELECT * FROM signals WHERE id=1")
    assert row["status"] == "stopped" and row["result_r"] == -1.0
    assert [c["status"] for c in res] == ["filled", "stopped"]  # fill хронологически первым
    assert res[1]["setup"] == "LTF-BB"                  # тег сетапа доступен caller'у (_on_close)
    # расширенные поля для поста результата: цены и движение в пользу сделки (SHORT в стоп < 0)
    assert res[1]["entry"] == 100.0 and res[1]["exit_price"] == 110.0
    assert res[1]["price_move_pct"] == -10.0
    assert row["filled_at"] is not None                 # филл персистится (миграция 009)


def test_bars_fill_event_once_then_outcome(fresh_db):
    """Fill без развязки -> событие 'filled' ровно один раз; исход приходит позже."""
    from app.brain import journal
    journal.record_signal(_sig())                       # SHORT 100 / stop 110 / tp 80
    fill_only = {"BTC/USDT:USDT": _bars([(100, 101, 99, 100)],
                                        start="2026-01-01T00:15:00+00:00")}
    res = journal.update_outcomes_from_bars(fill_only)
    assert len(res) == 1 and res[0]["status"] == "filled"
    assert res[0]["filled_at"] is not None and res[0]["result_r"] is None
    row = fresh_db.query_one("SELECT * FROM signals WHERE id=1")
    assert row["status"] == "open" and row["filled_at"] == res[0]["filled_at"]
    assert journal.update_outcomes_from_bars(fill_only) == []   # повторно не сообщается
    # развязка на новых барах -> терминальный исход, без второго 'filled'
    rows = [(100, 101, 99, 100), (95, 96, 79, 80)]      # тейк 80
    res2 = journal.update_outcomes_from_bars(
        {"BTC/USDT:USDT": _bars(rows, start="2026-01-01T00:15:00+00:00")})
    assert [c["status"] for c in res2] == ["tp1"]
    assert res2[0]["exit_price"] == 80.0 and res2[0]["price_move_pct"] == 20.0


def test_bars_outcome_chronology_tp(fresh_db):
    """Хронология: тейк достигнут ДО стопа -> tp, даже если позже цена дошла бы до стопа."""
    from app.brain import journal
    journal.record_signal(_sig())
    rows = [
        (100, 101, 99, 100),                            # fill
        (95, 96, 79, 80),                               # тейк 80 (стоп не задет)
        (80, 120, 79, 115),                             # позже цена улетела за стоп — уже неважно
    ]
    bars = {"BTC/USDT:USDT": _bars(rows, start="2026-01-01T00:15:00+00:00")}
    journal.update_outcomes_from_bars(bars)
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
    row = fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))
    assert row["status"] == "rejected"
    # событий: candidate + rejected
    n = fresh_db.query_one("SELECT COUNT(*) AS c FROM signal_events WHERE signal_id=?", (sid,))["c"]
    assert n == 2


def test_export_public_trades_excludes_candidate_and_rejected(fresh_db):
    from app.brain import journal
    journal.record_signal(_sig(symbol="BTC/USDT:USDT"))              # open -> публичный
    sid_cand = journal.record_candidate(_sig(symbol="ETH/USDT:USDT", entry=200.0, stop=210.0,
                                             tp=180.0, created_at="2026-01-01T00:15:00+00:00"))
    sid_rej = journal.record_candidate(_sig(symbol="SOL/USDT:USDT", entry=50.0, stop=55.0,
                                            tp=40.0, created_at="2026-01-01T00:30:00+00:00"))
    journal.reject_candidate(sid_rej)
    rows = journal.export_public_trades()
    statuses = {r["status"] for r in rows}
    assert "candidate" not in statuses and "rejected" not in statuses
    assert len(rows) == 1                                # только открытый BTC-сигнал
    assert rows[0]["symbol"] == "BTC/USDT:USDT"
    assert sid_cand and sid_rej                          # использованы (не мусорные id)


def test_record_ledger_hash(fresh_db):
    from app.brain import journal
    journal.record_ledger_hash(5, "deadbeef")
    row = fresh_db.query_one("SELECT * FROM ledger_hashes")
    assert row["row_count"] == 5 and row["sha256"] == "deadbeef"


def test_equity_series_matches_total(fresh_db):
    """equity_series хронологична и суммируется в тот же total_r, что stats()."""
    from app.brain import journal
    # 2 закрытых сделки: tp (+2R) и стоп (-1R)
    journal.record_signal(_sig(symbol="BTC/USDT:USDT"))
    journal.record_signal(_sig(symbol="ETH/USDT:USDT", entry=200.0, stop=210.0, tp=180.0))
    b1 = _bars([(100, 101, 99, 100), (95, 96, 79, 80)], start="2026-01-01T00:15:00+00:00")
    b2 = _bars([(200, 201, 199, 200), (205, 215, 204, 214)], start="2026-01-01T00:15:00+00:00")
    journal.update_outcomes_from_bars({"BTC/USDT:USDT": b1, "ETH/USDT:USDT": b2})
    series = journal.equity_series()
    assert len(series) == 2
    assert abs(sum(series) - journal.stats()["total_r"]) < 1e-9
