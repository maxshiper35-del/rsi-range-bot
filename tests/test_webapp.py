"""Mini App: граница публикации и форма API."""
from __future__ import annotations

import json


def test_api_stats_shape_and_no_strategy_leak(fresh_db):
    """Дашборд публичный: НИКАКИХ тегов сетапов (таксономия — собственность стратегии,
    публичный CSV их сознательно не содержит — фидбэк владельца 13.08.2026) и никаких
    уровней открытых сигналов (вход/стоп — платный контент)."""
    from app import db
    from app.webapp import app as flask_app

    db.execute(
        "INSERT INTO signals(symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, created_at) VALUES "
        "('BTC/USDT:USDT','LONG','1h',64000,62000,2.0,0.7,"
        "'{\"setup\":\"6226\"}','open','2026-08-13T10:00:00+00:00')")
    with flask_app.test_client() as c:
        r = c.get("/api/stats")
        assert r.status_code == 200
        raw = r.get_data(as_text=True)
        payload = json.loads(raw)
        for key in ("all", "equity_all", "open_signals", "generated_at"):
            assert key in payload
        assert "setup" not in raw and "6226" not in raw        # стратегия не утекает
        o = payload["open_signals"][0]
        assert o["symbol"] == "BTC" and o["side"] == "LONG"
        assert o["in_pos"] is False                            # лимитка ещё не исполнена
        for forbidden in ("entry", "stop", "tp"):
            assert forbidden not in o                          # уровни не утекают
        assert "64000" not in raw and "62000" not in raw


def test_open_signal_age_counts_from_fill(fresh_db):
    """Возраст «в позиции» — от филла, а не от публикации: неисполненная лимитка
    иначе выглядит сделкой, висящей 50 часов (вопрос владельца 13.08.2026)."""
    from datetime import timedelta

    from app import clock, db
    from app.webapp import app as flask_app

    created = (clock.utcnow() - timedelta(hours=48)).isoformat()
    db.execute(
        "INSERT INTO signals(symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, created_at, filled_at) VALUES "
        "('ETH/USDT:USDT','SHORT','4h',2500,2600,2.0,0.7,'{}','open',"
        "?, ?)", (created, clock.utcnow().isoformat()))
    with flask_app.test_client() as c:
        o = json.loads(c.get("/api/stats").get_data(as_text=True))["open_signals"][0]
    assert o["in_pos"] is True
    assert o["age_h"] is not None and o["age_h"] < 1.0         # от филла (только что)


def test_index_served(fresh_db):
    from app.webapp import app as flask_app
    with flask_app.test_client() as c:
        r = c.get("/")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "живой терминал" in body and "Не финансовый совет" in body
