"""Проверка решений вперёд (правила 24.10): готовность и вердикты по порогам."""
from __future__ import annotations

import json
from datetime import datetime, timezone


def _ins(i: int, r: float, status: str = "stopped", shadow: str | None = None,
         created: str = "2026-10-10T00:00:00+00:00") -> None:
    from app import db
    snap = json.dumps({"shadow": shadow}) if shadow else "{}"
    db.execute(
        "INSERT INTO signals(symbol, side, timeframe, entry, stop, risk_reward, confidence, "
        "snapshot_json, status, created_at, closed_at, result_r) VALUES "
        f"('S{i}/USDT:USDT','LONG','1h',1,0.9,2,0.7,?,?,?, '2026-10-11T00:00:00+00:00',?)",
        (snap, status, created, r))


def test_gate_verdict_needs_data_then_compares(fresh_db):
    from app.brain import forward_check as fc
    early = datetime(2026, 10, 24, tzinfo=timezone.utc)
    late = datetime(2026, 12, 1, tzinfo=timezone.utc)
    for i in range(10):
        _ins(i, 0.5)                                         # опубликованные: +0.5/сд.
    for i in range(10, 50):
        _ins(i, -0.3, status="rejected", shadow="late_entry")
    g = fc.gate_check(early)
    assert g["shadow"]["n"] == 40 and not g["ready"] and g["verdict"] is None   # < 6 нед.
    g = fc.gate_check(late)
    assert g["ready"] and g["verdict"] == "keep"
    assert "Проверка решений" in fc.report(late)


def test_shadow_4h_thresholds(fresh_db):
    from app.brain import forward_check as fc
    for i in range(60):
        _ins(i, 0.06, status="rejected", shadow="setup_off")
    s = fc.shadow_4h_check(datetime(2027, 1, 10, tzinfo=timezone.utc))
    assert s["ready"] and s["verdict"] == "back"


def test_judge_gap_open_exits_at_open(fresh_db):
    """Исследовательский режим: бар открылся за подтянутым стопом — выход по открытию."""
    import json

    import pandas as pd

    from app.brain.journal import _outcome_from_bars
    t0 = pd.Timestamp("2026-01-01T00:00:00Z")
    rows = [(100.5, 101, 99.8, 100.4),        # 01:00 филл 100
            (100.4, 104, 100.2, 101.0),       # 02:00 пик 104 (+2R) — трейл 103.6, закрытие 101
            (101.0, 101.5, 100.5, 101.2)]     # 03:00 открылся 101 < 103.6
    df = pd.DataFrame({"dt": [t0 + pd.Timedelta(hours=i + 1) for i in range(3)],
                       "open": [r[0] for r in rows], "high": [r[1] for r in rows],
                       "low": [r[2] for r in rows], "close": [r[3] for r in rows]})
    row = {"entry": 100.0, "stop": 98.0, "side": "LONG", "timeframe": "1h",
           "created_at": t0.isoformat(), "filled_at": None, "status": "open",
           "tp1": None, "tp2": None, "tp3": None,
           "snapshot_json": json.dumps({"trail": {"activate_r": 1.5, "distance_r": 0.2,
                                                  "tp_exit": False, "max_hold_hours": 168}})}
    assert _outcome_from_bars(row, df)[2] == 1.8                 # журнал: по уровню трейла
    assert _outcome_from_bars(row, df, gap_open=True)[2] == 0.5  # реально: по открытию 101


def _exec_row(sid: int, version: str, fill: float, close: float, j_r: float,
              state: str = "closed", note: str | None = None) -> None:
    from app import db
    db.execute(
        "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, confidence, "
        "snapshot_json, status, created_at, closed_at, result_r) VALUES "
        "(?, ?,'LONG','1h',100,99,2,0.7,'{}','stopped', "
        "'2026-10-25T00:00:00+00:00','2026-10-25T05:00:00+00:00', ?)",
        (sid, f"X{sid}/USDT:USDT", j_r))
    db.execute(
        "INSERT INTO executor_orders(signal_id, symbol, side, qty, entry_px, stop_px, client_id, "
        "state, note, created_at, fill_px, close_px, closed_at, execution_version) VALUES "
        "(?, 'X/USDT:USDT','LONG',10,100,99,'',?,?,'2026-10-25T00:01:00+00:00',?,?,"
        "'2026-10-25T05:01:00+00:00',?)",
        (sid, state, note, fill if state == "closed" else None,
         close if state == "closed" else None, version))


def test_exec_check_breaks_down_by_execution_version(fresh_db):
    """30.10: когорта с 19.10 смешивала механики выхода (e3/e4/e5) — разбивка по версиям,
    вердикт по зарегистрированной когорте не меняется; пропуски «филл раньше ордера» —
    отдельной строкой с R журнала."""
    from app.brain import forward_check as fc
    _exec_row(1, "e4", 100, 101.2, 1.8)            # демо +1.2R против журнала +1.8R
    _exec_row(2, "e4", 100, 99, -1.0)
    _exec_row(3, "e5", 100, 101.7, 1.8)
    _exec_row(4, "e4", 0, 0, -1.0, state="error", note="филл в журнале раньше постановки")
    e = fc.exec_check()
    assert e["n"] == 3 and set(e["by_version"]) == {"e4", "e5"}
    assert e["by_version"]["e4"]["n"] == 2 and e["by_version"]["e4"]["gap"] == -0.3
    assert e["by_version"]["e5"]["gap"] == -0.1
    assert e["prefill"] == {"n": 1, "sum": -1.0, "exp": -1.0}
    txt = fc.report(datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert "по версиям исполнения" in txt and "e5 (нативный трейл, активация по стриму" in txt
    assert "не исполнимо" in txt
