"""Минутный судья трейла (30.10): чистая модель E, снапшот-флаг, интеграция с журналом,
кэш минуток, строки о методике. Часовой судья старых сигналов не меняется — его тесты
в test_journal.py остаются зелёными без правок."""
from __future__ import annotations

import json
import logging

import numpy as np
import pytest

from app.brain.judge_1m import HOUR_MS, MIN_MS, judge_path
from app.core.signal_engine import Side, Signal

T0 = 1_700_000_000_000 // HOUR_MS * HOUR_MS        # ровное начало часа, мс UTC


def _bars(rows, start=T0, step=MIN_MS) -> np.ndarray:
    """rows: [(o, h, l, c), ...] -> (n, 5) с минутами подряд от start."""
    return np.array([[start + i * step, *r] for i, r in enumerate(rows)], dtype=float)


def _judge(bars, side="LONG", entry=100.0, stop=98.0, **kw):
    kw.setdefault("decision_ms", T0)
    kw.setdefault("fill_until_ms", T0 + 50 * HOUR_MS)
    kw.setdefault("act_r", 1.5)
    kw.setdefault("dist_r", 0.2)
    kw.setdefault("max_hold_h", 168.0)
    return judge_path(entry, stop, side, bars, **kw)


# --- чистая модель ------------------------------------------------------------------------

def test_fill_activation_and_trail_exit_long():
    """LONG 100/98 (R=2): касание входа, ход до 103.5 активирует трейл (+1.5R=103), уровень
    103.5−0.4=103.1; откат следующей минуты до 102.9 — выход по уровню, +1.55R."""
    bars = _bars([
        (101, 101.5, 100.5, 101),        # входа нет
        (100.8, 100.9, 99.9, 100.2),     # медвежья O→H→L→C: касание 100 на H→L — вход
        (100.2, 103.5, 100.1, 103.2),    # бычья O→L→H→C: на H активация, уровень 103.1
        (103.2, 103.3, 102.9, 103.0),    # O→H→L: L 102.9 ≤ 103.1 — выход по трейлу
    ])
    v = _judge(bars)
    assert v.event == "trail" and v.r == 1.55 and v.exit_price == 103.1
    assert v.fill_ms == T0 + MIN_MS and v.exit_ms == T0 + 3 * MIN_MS and v.active


def test_short_is_mirror_of_long():
    bars = _bars([
        (99.2, 100.1, 99.1, 99.8),       # касание 100 — вход SHORT
        (99.8, 99.9, 96.5, 96.8),        # ход до 96.5 = +1.75R: активация, уровень 96.9
        (96.8, 97.1, 96.7, 97.0),        # H 97.1 ≥ 96.9 — выход
    ])
    v = _judge(bars, side="SHORT", entry=100.0, stop=102.0)
    assert v.event == "trail" and v.r == 1.55 and v.exit_price == 96.9


def test_stop_in_same_minute_as_fill_uses_worst_order():
    """Бычья минута O→L→H→C: вход на O→L и там же стоп, если L ≤ стопа — худший порядок."""
    bars = _bars([(100.5, 101, 97.5, 100.9)])
    v = _judge(bars)
    assert v.event == "stopped" and v.r == -1.0 and v.exit_price == 98.0


def test_gap_open_fills_at_open_but_parity_mode_at_level():
    """Следующая минута открылась ниже стопа: прод (gap_open) — по открытию 97 = −1.5R,
    режим паритета с замером — по уровню 98 = −1R."""
    bars = _bars([
        (100.2, 100.3, 99.9, 100.1),     # вход
        (100.1, 100.4, 100.0, 100.2),
        (97.0, 97.2, 96.8, 97.1),        # гэп сквозь стоп
    ])
    prod = _judge(bars, gap_open=True)
    assert prod.event == "stopped" and prod.r == -1.5 and prod.exit_price == 97.0
    par = _judge(bars, gap_open=False)
    assert par.event == "stopped" and par.r == -1.0 and par.exit_price == 98.0


def test_timeout_at_close_of_hold_hour_from_fill_hour_start():
    """Потолок 1 час: вход в минуте :07, якорь — начало часа входа; выход по close минуты,
    закрывающей час anchor+1h (как у часового судьи)."""
    rows = [(100.5, 100.6, 100.4, 100.5)] * 7 + [(100.3, 100.4, 99.9, 100.2)]   # вход в :07
    rows += [(100.2, 100.4, 100.1, 100.3)] * 200
    bars = _bars(rows)
    v = _judge(bars, max_hold_h=1.0)
    assert v.event == "timeout" and v.exit_ms == T0 + 2 * HOUR_MS - MIN_MS
    assert v.r == round((100.3 - 100) / 2, 4) and v.fill_ms == T0 + 7 * MIN_MS


def test_timeout_over_data_hole_uses_last_close():
    rows = [(100.3, 100.4, 99.9, 100.2)] + [(100.2, 100.4, 100.1, 100.3)] * 30
    bars = _bars(rows)
    bars = np.vstack([bars, [T0 + 5 * HOUR_MS, 100.3, 100.4, 100.2, 100.3]])  # дыра
    v = _judge(bars, max_hold_h=1.0)
    assert v.event == "timeout" and v.exit_ms == T0 + 30 * MIN_MS and v.r == 0.15


def test_no_fill_window_expires_and_waiting_before():
    rows = [(101, 101.5, 100.5, 101)] * 10
    assert _judge(_bars(rows), fill_until_ms=T0 + 5 * MIN_MS).event == "nofill"
    assert _judge(_bars(rows)).event == "waiting"
    assert _judge(np.empty((0, 5))).event == "waiting"


def test_open_when_data_ends_reports_current_level():
    bars = _bars([
        (100.2, 100.3, 99.9, 100.1),     # вход
        (100.1, 104.0, 100.0, 103.8),    # активация на 104: уровень 103.6
    ])
    v = _judge(bars)
    assert v.event == "open" and v.active and abs(v.level_price - 103.6) < 1e-9
    assert v.r is None and v.exit_ms is None and v.fill_ms == T0


def test_open_before_activation_keeps_initial_stop_level():
    v = _judge(_bars([(100.2, 100.3, 99.9, 100.1)]))
    assert v.event == "open" and not v.active and v.level_price == 98.0


def test_take_profit_exit_only_when_enabled():
    bars = _bars([
        (100.2, 100.3, 99.9, 100.1),
        (100.1, 104.5, 100.0, 104.2),    # 104 достигнут на H
    ])
    with_tp = _judge(bars, tp=104.0, tp_exit=True)
    assert with_tp.event == "tp" and with_tp.r == 2.0 and with_tp.exit_price == 104.0
    assert _judge(bars, tp=104.0, tp_exit=False).event == "open"


def test_breakeven_lock_from_snapshot_fields():
    bars = _bars([
        (100.2, 100.3, 99.9, 100.1),
        (100.1, 102.2, 100.0, 102.0),    # +1.1R ≥ триггер 1.0 — стоп на 100.4
        (102.0, 102.1, 100.3, 100.5),    # L 100.3 ≤ 100.4 — выход
    ])
    v = _judge(bars, be_trigger_r=1.0, be_lock_r=0.2)
    assert v.event == "trail" and v.r == 0.2 and v.exit_price == 100.4


def test_parity_mode_matches_research_fine_judge_on_synthetic_path():
    """Режим паритета (окно входа — час входа, gap_open=False) совпадает с эталоном замера
    на разнообразном пути: гэпы, активация в минуте входа, откаты, потолок."""
    from scripts.research_fine_judge import judge as ref_judge

    rng = np.random.default_rng(7)
    mism = 0
    for _ in range(300):
        n = int(rng.integers(5, 400))
        close = 100 + np.cumsum(rng.normal(0, 0.25, n))
        o = np.r_[close[0], close[:-1]] + rng.normal(0, 0.05, n)
        h = np.maximum(o, close) + np.abs(rng.normal(0, 0.15, n))
        lo = np.minimum(o, close) - np.abs(rng.normal(0, 0.15, n))
        bars = np.column_stack([T0 + np.arange(n) * MIN_MS, o, h, lo, close])
        side = "LONG" if rng.random() < 0.5 else "SHORT"
        entry = 100.0 + float(rng.normal(0, 0.3))
        stop = entry - 1.5 if side == "LONG" else entry + 1.5
        hold = float(rng.choice([1.0, 2.0, 168.0]))
        ev, r, ex = ref_judge(entry, stop, side, T0, bars, bar_ms=MIN_MS, mode="path",
                              immediate=True, order="color", max_hold_h=hold)
        v = judge_path(entry, stop, side, bars, decision_ms=T0, fill_from_ms=T0,
                       fill_until_ms=T0 + HOUR_MS, act_r=1.5, dist_r=0.2, max_hold_h=hold,
                       gap_open=False)
        mine = ({"open": "unresolved", "waiting": "unresolved"}.get(v.event, v.event),
                v.r, v.exit_ms)
        mism += mine != (ev, r, ex)
    assert mism == 0


# --- журнал: снапшот, судья, персистенс ----------------------------------------------------

def _sig_1h(side=Side.LONG, entry=100.0, stop=98.0, tp=110.0,
            created_at="2026-01-01T00:00:00+00:00"):
    return Signal(symbol="BTC/USDT:USDT", side=side, timeframe="1h", entry=entry, stop=stop,
                  take_profits=[tp], risk_reward=5.0, confidence=0.6, reasons=["тест"],
                  created_at=created_at, indicators_snapshot={"setup": "6226"})


DEC = 1_767_225_600_000 + HOUR_MS        # 2026-01-01T01:00:00Z — закрытие решающего бара


def _trail_on(runtime, judge_1m: bool):
    runtime.set_value("trail_activate_r", "1.5")
    runtime.set_value("trail_distance_r", "0.2")
    runtime.set_value("trail_only_exit", "1")
    runtime.set_value("trail_judge_1m", "1" if judge_1m else "0")


def test_snapshot_judge_key_only_with_flag_and_changes_config_version(fresh_db):
    from app import runtime
    from app.brain import journal
    assert runtime.trail_judge_1m() is False                 # дефолт ВЫКЛ
    _trail_on(runtime, False)
    journal.record_signal(_sig_1h())
    _trail_on(runtime, True)
    journal.record_signal(_sig_1h(created_at="2026-01-01T02:00:00+00:00"))
    r1, r2 = fresh_db.query("SELECT snapshot_json, config_version FROM signals ORDER BY id")
    assert "judge" not in json.loads(r1["snapshot_json"])["trail"]
    assert json.loads(r2["snapshot_json"])["trail"]["judge"] == "1m"
    assert r1["config_version"] != r2["config_version"]       # когорты forward_check разные
    rows = fresh_db.query("SELECT * FROM signals ORDER BY id")
    assert journal._trail_of(rows[0])["judge"] == "1h"
    assert journal._trail_of(rows[1])["judge"] == "1m"
    assert journal.trail_judge_1m_since() == "2026-01-01T02:00:00+00:00"


def test_trail_judge_1m_since_ignores_shadows(fresh_db):
    from app import runtime
    from app.brain import journal
    _trail_on(runtime, True)
    assert journal.trail_judge_1m_since() is None
    journal.record_shadow(_sig_1h())
    assert journal.trail_judge_1m_since() is None


def test_minute_judge_closes_trade_and_persists_minute_times(fresh_db, monkeypatch):
    """Сигнал с judge=1m закрывается минутным судьёй: filled_at/closed_at — минуты,
    событие trail, exit_price по уровню; часовые бары такта не нужны."""
    from app import runtime
    from app.brain import journal
    _trail_on(runtime, True)
    journal.record_signal(_sig_1h())
    bars = _bars([
        (101, 101.5, 100.5, 101),
        (100.8, 100.9, 99.9, 100.2),     # вход 01:01
        (100.2, 103.5, 100.1, 103.2),    # активация, уровень 103.1
        (103.2, 103.3, 102.9, 103.0),    # выход 01:03
    ], start=DEC)
    seen = {}

    def provider(symbol, start_ms, end_ms):
        seen.update(symbol=symbol, start=start_ms, end=end_ms)
        return bars

    monkeypatch.setattr(journal, "_MINUTE_BARS", provider)
    res = journal.update_outcomes_from_bars({})               # без часовых баров
    assert seen["symbol"] == "BTC/USDT:USDT" and seen["start"] == DEC
    closed = [c for c in res if c["status"] != "filled"]
    assert len(closed) == 1 and closed[0]["status"] == "stopped"
    assert closed[0]["result_r"] == 1.55 and closed[0]["exit_price"] == 103.1
    row = fresh_db.query_one("SELECT * FROM signals WHERE id=1")
    assert row["filled_at"] == "2026-01-01T01:01:00+00:00"
    assert row["closed_at"] == "2026-01-01T01:03:00+00:00"
    ev = fresh_db.query_one(
        "SELECT event FROM signal_events WHERE signal_id=1 ORDER BY id DESC LIMIT 1")
    assert ev["event"] == "trail"


def test_minute_judge_open_persists_trail_stop_and_fill_event(fresh_db, monkeypatch):
    from app import runtime
    from app.brain import journal
    _trail_on(runtime, True)
    journal.record_signal(_sig_1h())
    bars = _bars([(100.2, 100.3, 99.9, 100.1), (100.1, 104.0, 100.0, 103.8)], start=DEC)
    monkeypatch.setattr(journal, "_MINUTE_BARS", lambda *a: bars)
    res = journal.update_outcomes_from_bars({})
    assert [c["status"] for c in res] == ["filled"]
    row = fresh_db.query_one("SELECT status, filled_at, trail_stop FROM signals WHERE id=1")
    assert row["status"] == "open" and row["filled_at"] == "2026-01-01T01:00:00+00:00"
    assert abs(row["trail_stop"] - 103.6) < 1e-9
    assert journal.update_outcomes_from_bars({}) == []        # второй такт — без дублей


def test_minute_judge_without_minutes_keeps_trade_open_and_warns(fresh_db, monkeypatch,
                                                                 caplog):
    from app import runtime
    from app.brain import journal
    _trail_on(runtime, True)
    journal.record_signal(_sig_1h())
    monkeypatch.setattr(journal, "_MINUTE_BARS", lambda *a: None)
    with caplog.at_level(logging.WARNING, logger="journal"):
        assert journal.update_outcomes_from_bars({}) == []
    assert "минуток для судьи нет" in caplog.text
    row = fresh_db.query_one("SELECT status, result_r, filled_at FROM signals WHERE id=1")
    assert row["status"] == "open" and row["result_r"] is None and row["filled_at"] is None


def test_minute_judge_expires_without_touch_but_never_persisted_fill(fresh_db, monkeypatch,
                                                                     caplog):
    from app import db, runtime
    from app.brain import journal
    _trail_on(runtime, True)
    journal.record_signal(_sig_1h())
    rows = [(101, 101.5, 100.5, 101)] * (60 * 60)             # 60 часов без касания
    monkeypatch.setattr(journal, "_MINUTE_BARS", lambda *a: _bars(rows, start=DEC))
    res = journal.update_outcomes_from_bars({})
    assert len(res) == 1 and res[0]["status"] == "expired"
    assert db.query_one("SELECT closed_at FROM signals WHERE id=1")["closed_at"] == \
        "2026-01-03T03:00:00+00:00"                          # decision + 50ч
    # зафиксированный стримом вход истечь не может — ждём и предупреждаем
    journal.record_signal(_sig_1h(created_at="2026-01-01T02:00:00+00:00"))
    db.execute("UPDATE signals SET filled_at='2026-01-01T03:30:00+00:00' WHERE id=2")
    with caplog.at_level(logging.WARNING, logger="journal"):
        assert journal.update_outcomes_from_bars({}) == []
    assert "не истекаем" in caplog.text
    assert db.query_one("SELECT status FROM signals WHERE id=2")["status"] == "open"


def test_legacy_signal_still_judged_by_hourly_bars(fresh_db, monkeypatch):
    """Сигнал без judge=1m не трогает минутный провайдер и судится часовыми барами."""
    import pandas as pd

    from app import runtime
    from app.brain import journal
    _trail_on(runtime, False)
    journal.record_signal(_sig_1h())
    monkeypatch.setattr(journal, "_MINUTE_BARS",
                        lambda *a: pytest.fail("минутки не должны запрашиваться"))
    dts = pd.date_range("2026-01-01T01:00:00+00:00", periods=2, freq="60min", tz="UTC")
    df = pd.DataFrame({"open": [101, 100.5], "high": [101.5, 100.6], "low": [100.5, 97.5],
                       "close": [100.6, 97.6], "volume": [1, 1], "dt": dts})
    res = journal.update_outcomes_from_bars({"BTC/USDT:USDT": df})
    closed = [c for c in res if c["status"] != "filled"]
    assert len(closed) == 1 and closed[0]["result_r"] == -1.0


# --- кэш минуток ----------------------------------------------------------------------------

def test_minute_cache_fetches_only_missing_and_requires_full_coverage(tmp_path, monkeypatch):
    from app.data import minute_bars as mb
    monkeypatch.setattr(mb, "cache_root", lambda: tmp_path / "m1")
    monkeypatch.setattr(mb, "_now_ms", lambda: T0 + 100 * MIN_MS)
    calls = []

    def fake_fetch(symbol, s, e):
        calls.append((s, e))
        return [[t, 1.0, 2.0, 0.5, 1.5] for t in range(s, e, MIN_MS)]

    monkeypatch.setattr(mb, "_fetch_pages", fake_fetch)
    a = mb.load_window("BTC/USDT:USDT", T0, T0 + 10 * MIN_MS)
    assert a.shape == (10, 5) and calls == [(T0, T0 + 10 * MIN_MS)]
    b = mb.load_window("BTC/USDT:USDT", T0 + 5 * MIN_MS, T0 + 15 * MIN_MS)
    assert b.shape == (10, 5) and calls[-1] == (T0 + 10 * MIN_MS, T0 + 15 * MIN_MS)
    assert mb.load_window("BTC/USDT:USDT", T0, T0 + 15 * MIN_MS, fetch=False).shape == (15, 5)
    # окно за пределами кэша без докачки — None, а не обрезанный массив
    assert mb.load_window("BTC/USDT:USDT", T0, T0 + 20 * MIN_MS, fetch=False) is None
    # биржа не отдала минуты — покрытие не помечается, ответ None
    monkeypatch.setattr(mb, "_fetch_pages", lambda *a: [])
    assert mb.load_window("ETH/USDT:USDT", T0, T0 + 3 * MIN_MS) is None
    # сетевая ошибка — None и без исключения наружу
    def boom(*a):
        raise RuntimeError("сеть")
    monkeypatch.setattr(mb, "_fetch_pages", boom)
    assert mb.load_window("ETH/USDT:USDT", T0, T0 + 3 * MIN_MS) is None
    # незакрытая минута не отдаётся
    monkeypatch.setattr(mb, "_fetch_pages", fake_fetch)
    monkeypatch.setattr(mb, "_now_ms", lambda: T0 + 2 * MIN_MS + 30_000)
    assert mb.load_window("SOL/USDT:USDT", T0, T0 + 5 * MIN_MS).shape == (2, 5)


def test_minute_cache_short_page_does_not_mark_window_covered(tmp_path, monkeypatch):
    from app.data import minute_bars as mb
    monkeypatch.setattr(mb, "cache_root", lambda: tmp_path / "m1")
    monkeypatch.setattr(mb, "_now_ms", lambda: T0 + 100 * MIN_MS)
    def three(symbol, s, e):
        return [[t, 1, 2, 0.5, 1.5] for t in range(s, s + 3 * MIN_MS, MIN_MS)]

    monkeypatch.setattr(mb, "_fetch_pages", three)
    assert mb.load_window("BTC/USDT:USDT", T0, T0 + 10 * MIN_MS) is None
    assert mb._merge(json.loads((tmp_path / "m1" / "BTC-USDT-USDT_1m.json").read_text())) \
        == [[T0, T0 + 3 * MIN_MS]]


# --- тексты -----------------------------------------------------------------------------------

def test_methodology_line_in_texts(fresh_db):
    from app.delivery import bot, templates
    assert templates.trail_judge_line(None) == ""
    line = templates.trail_judge_line("2026-11-01T07:00:00+00:00")
    assert line.startswith("С 01.11.2026 выход по трейлу считается по минутным свечам")
    st = {"trades": 10, "open": 2, "wins": 6, "winrate": 0.6, "expectancy_r": 0.4,
          "total_r": 4.0, "losses": 4}
    assert "минутным свечам" not in bot.format_stats(st)
    assert line in bot.format_stats(st, judge_since="2026-11-01T07:00:00+00:00")
    assert line not in bot.format_about() and line in bot.format_about("2026-11-01T07:00:00+00:00")
