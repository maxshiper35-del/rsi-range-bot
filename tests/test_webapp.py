"""Mini App: граница публикации и форма API."""
from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest


def _init_data(token: str, user_id: int, age_s: int = 0, spoil: bool = False) -> str:
    """Валидная (или порченая) initData по официальной схеме Telegram."""
    from app import clock
    pairs = {"auth_date": str(int(clock.utcnow().timestamp()) - age_s),
             "query_id": "AAE1", "user": json.dumps({"id": user_id, "first_name": "T"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if spoil:
        h = ("0" if h[0] != "0" else "1") + h[1:]
    return urlencode({**pairs, "hash": h})


@pytest.fixture
def webapp_token(monkeypatch):
    """Фиксированный токен бота для подписи initData в тестах."""
    from app.config import get_settings
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TEST")
    get_settings.cache_clear()
    yield "123:TEST"
    get_settings.cache_clear()


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

def test_api_me_rejects_without_or_bad_signature(fresh_db, webapp_token):
    """Платный слой без валидной подписи Telegram не отдаётся: 401 и no-store."""
    from app.webapp import app as flask_app
    with flask_app.test_client() as c:
        assert c.get("/api/me").status_code == 401
        bad = c.get("/api/me", headers={"Authorization":
                                        "tma " + _init_data("123:TEST", 1, spoil=True)})
        assert bad.status_code == 401
        assert bad.headers["Cache-Control"] == "no-store"
        stale = c.get("/api/me", headers={"Authorization":
                                          "tma " + _init_data("123:TEST", 1, age_s=25 * 3600)})
        assert stale.status_code == 401                    # replay суточной давности


def test_api_me_free_user_gets_no_levels(fresh_db, webapp_token):
    """Валидная подпись, но не подписчик: private=false и НИКАКИХ уровней в ответе."""
    from app.delivery import payments
    from app.webapp import app as flask_app
    payments.register_user(500100, "freeuser")
    with flask_app.test_client() as c:
        r = c.get("/api/me", headers={"Authorization": "tma " + _init_data("123:TEST", 500100)})
    assert r.status_code == 200
    raw = r.get_data(as_text=True)
    d = json.loads(raw)
    assert d["private"] is False
    assert "entry" not in raw and "positions" not in raw


def test_api_me_subscriber_levels_and_ur(fresh_db, webapp_token, monkeypatch):
    """Подписчик видит уровни и unrealized R; тегов сетапов нет и в платном слое."""
    from app import db, webapp
    from app.delivery import payments
    from app.webapp import app as flask_app
    payments.register_user(500200, "payer")
    db.execute("UPDATE users SET tier='paid', paid_until='2099-01-01T00:00:00+00:00' "
               "WHERE tg_id=500200")
    db.execute(
        "INSERT INTO signals(symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, created_at, filled_at) VALUES "
        "('BTC/USDT:USDT','SHORT','1h',64000,65000,2.0,0.7,"
        "'{\"setup\":\"6226\"}','open','2026-08-13T10:00:00+00:00',"
        "'2026-08-13T11:00:00+00:00')")
    monkeypatch.setattr(webapp, "_last_prices",
                        lambda syms: {"BTC/USDT:USDT": 62000.0})
    with flask_app.test_client() as c:
        r = c.get("/api/me", headers={"Authorization": "tma " + _init_data("123:TEST", 500200)})
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"        # персональное не кешируется
    raw = r.get_data(as_text=True)
    d = json.loads(raw)
    assert d["private"] is True
    o = d["positions"][0]
    assert o["entry"] == 64000 and o["stop"] == 65000 and o["in_pos"] is True
    assert o["ur"] == 2.0                                  # шорт: (64000−62000)/1000
    assert "setup" not in raw and "6226" not in raw        # стратегия — никому

def test_api_me_non_ascii_hash_is_401_not_500(fresh_db, webapp_token):
    """Мусорный hash с не-ASCII: compare_digest кидал бы TypeError -> 500 без
    аутентификации (ревью 13.08). Должен быть чистый 401."""
    from app.webapp import app as flask_app
    with flask_app.test_client() as c:
        r = c.get("/api/me", headers={"Authorization":
                                      "tma auth_date=1&user=x&hash=%C3%A9"})
    assert r.status_code == 401


def test_api_me_expired_subscription_is_public(fresh_db, webapp_token):
    """Истёкшая подписка (tier='paid', paid_until в прошлом) — главный путь оттока:
    платный слой закрыт до продления."""
    from app import db
    from app.delivery import payments
    from app.webapp import app as flask_app
    payments.register_user(500300, "lapsed")
    db.execute("UPDATE users SET tier='paid', paid_until='2026-01-01T00:00:00+00:00' "
               "WHERE tg_id=500300")
    with flask_app.test_client() as c:
        r = c.get("/api/me", headers={"Authorization":
                                      "tma " + _init_data("123:TEST", 500300)})
    raw = r.get_data(as_text=True)
    assert r.status_code == 200
    assert json.loads(raw)["private"] is False
    assert "entry" not in raw and "positions" not in raw
