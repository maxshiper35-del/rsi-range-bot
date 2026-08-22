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

def _mk_closed(sym: str, r: float, closed: str) -> int:
    from app import db
    db.execute(
        "INSERT INTO signals(symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, created_at, filled_at, closed_at, result_r) "
        f"VALUES ('{sym}','LONG','1h',1,0.9,2.0,0.7,'{{}}','stopped',"
        f"'2026-08-01T00:00:00+00:00','2026-08-01T01:00:00+00:00','{closed}',{r})")
    return db.query_one("SELECT MAX(id) AS i FROM signals")["i"]


def test_api_take_auth_matrix(fresh_db, webapp_token):
    """Отметка сделки: 401 без подписи, 403 фри-юзеру, 400/404 на мусор."""
    from app.delivery import payments
    from app.webapp import app as flask_app
    payments.register_user(600100, "free")
    sid = _mk_closed("AAA/USDT:USDT", 1.5, "2026-08-10T00:00:00+00:00")
    with flask_app.test_client() as c:
        assert c.post("/api/take", json={"signal_id": sid, "taken": True}).status_code == 401
        free = {"Authorization": "tma " + _init_data("123:TEST", 600100)}
        assert c.post("/api/take", json={"signal_id": sid, "taken": True},
                      headers=free).status_code == 403     # платный слой — платникам
        payments.register_user(600200, "payer")
        from app import db
        db.execute("UPDATE users SET tier='paid', paid_until='2099-01-01T00:00:00+00:00' "
                   "WHERE tg_id=600200")
        paid = {"Authorization": "tma " + _init_data("123:TEST", 600200)}
        assert c.post("/api/take", json={"signal_id": "x", "taken": True},
                      headers=paid).status_code == 400
        assert c.post("/api/take", json={"signal_id": True, "taken": True},
                      headers=paid).status_code == 400     # bool — не id
        assert c.post("/api/take", json={"signal_id": 999999, "taken": True},
                      headers=paid).status_code == 404


def test_api_take_toggles_and_my_stats(fresh_db, webapp_token, monkeypatch):
    """Отметил две закрытые -> личный итог = их сумма; снял одну -> пересчитался."""
    from app import db, webapp
    from app.delivery import payments
    from app.webapp import app as flask_app
    payments.register_user(600300, "payer")
    db.execute("UPDATE users SET tier='paid', paid_until='2099-01-01T00:00:00+00:00' "
               "WHERE tg_id=600300")
    a = _mk_closed("AAA/USDT:USDT", 2.4, "2026-08-10T00:00:00+00:00")
    b = _mk_closed("BBB/USDT:USDT", -1.0, "2026-08-11T00:00:00+00:00")
    monkeypatch.setattr(webapp, "_last_prices", lambda syms: {})
    hdr = {"Authorization": "tma " + _init_data("123:TEST", 600300)}
    with flask_app.test_client() as c:
        r1 = c.post("/api/take", json={"signal_id": a, "taken": True}, headers=hdr)
        r2 = c.post("/api/take", json={"signal_id": b, "taken": True}, headers=hdr)
        ms = json.loads(r2.get_data(as_text=True))["my_stats"]
        assert ms["trades"] == 2 and ms["total_r"] == 1.4 and ms["winrate"] == 0.5
        assert json.loads(r1.get_data(as_text=True))["ok"] is True
        me = json.loads(c.get("/api/me", headers=hdr).get_data(as_text=True))
        assert sorted(me["taken"]) == sorted([a, b])
        assert {x["id"] for x in me["recent_closed"]} >= {a, b}
        assert "entry" not in json.dumps(me["recent_closed"])  # уровней в списке нет
        r3 = c.post("/api/take", json={"signal_id": b, "taken": False}, headers=hdr)
        ms = json.loads(r3.get_data(as_text=True))["my_stats"]
        assert ms["trades"] == 1 and ms["total_r"] == 2.4

def test_api_take_body_shape_and_range(fresh_db, webapp_token):
    """По ревью 13.08: не-dict JSON и id за 64 бита — чистый 400, не 500."""
    from app import db
    from app.delivery import payments
    from app.webapp import app as flask_app
    payments.register_user(600400, "payer")
    db.execute("UPDATE users SET tier='paid', paid_until='2099-01-01T00:00:00+00:00' "
               "WHERE tg_id=600400")
    hdr = {"Authorization": "tma " + _init_data("123:TEST", 600400),
           "Content-Type": "application/json"}
    with flask_app.test_client() as c:
        for raw in ("[1,2]", '"abc"', "5", "null"):
            assert c.post("/api/take", data=raw, headers=hdr).status_code == 400, raw
        assert c.post("/api/take", json={"signal_id": 2 ** 64, "taken": True},
                      headers=hdr).status_code == 400
        assert c.post("/api/take", json={"signal_id": -5, "taken": True},
                      headers=hdr).status_code == 400


def test_recent_closed_keeps_taken_beyond_limit(fresh_db, webapp_token, monkeypatch):
    """По ревью 13.08: отмеченная сделка старше топ-15 остаётся видимой (и снимаемой)."""
    from app import db, webapp
    from app.delivery import payments
    from app.webapp import app as flask_app
    payments.register_user(600500, "payer")
    db.execute("UPDATE users SET tier='paid', paid_until='2099-01-01T00:00:00+00:00' "
               "WHERE tg_id=600500")
    old = _mk_closed("OLD/USDT:USDT", 2.0, "2026-07-01T00:00:00+00:00")
    for i in range(16):
        _mk_closed(f"S{i}/USDT:USDT", 1.0, f"2026-08-{i + 1:02d}T00:00:00+00:00")
    monkeypatch.setattr(webapp, "_last_prices", lambda syms: {})
    hdr = {"Authorization": "tma " + _init_data("123:TEST", 600500)}
    with flask_app.test_client() as c:
        c.post("/api/take", json={"signal_id": old, "taken": True}, headers=hdr)
        me = json.loads(c.get("/api/me", headers=hdr).get_data(as_text=True))
    ids = [x["id"] for x in me["recent_closed"]]
    assert old in ids and ids[-1] == old                    # старая отметка видима, в конце
    assert len(ids) == 16                                   # топ-15 + отмеченная за пределами
    assert me["my_stats"]["trades"] == 1

def test_og_png_and_meta_injection(fresh_db, monkeypatch, tmp_path):
    """og:image подставляется только когда карточка отрендерена; иначе 404 и base-мета."""
    from app import runtime, webapp
    from app.webapp import app as flask_app
    monkeypatch.setattr(webapp, "_OG_PNG", tmp_path / "og.png")
    runtime.set_value("webapp_url", "https://example.test/")
    with flask_app.test_client() as c:
        assert c.get("/og.png").status_code == 404
        assert "og:image" not in c.get("/").get_data(as_text=True)
        (tmp_path / "og.png").write_bytes(b"\x89PNG fake")
        assert c.get("/og.png").status_code == 200
        body = c.get("/").get_data(as_text=True)
        assert 'og:image" content="https://example.test/og.png"' in body
        assert "summary_large_image" in body

def test_symbols_best_worst_public(fresh_db):
    """Инструменты в публичном API: лучшие И худшие (не витрина), без сетапов."""
    for sym, r in (("AAA/USDT:USDT", 3.0), ("BBB/USDT:USDT", -1.0), ("CCC/USDT:USDT", 1.0)):
        _mk_closed(sym, r, "2026-08-10T00:00:00+00:00")
    from app.webapp import app as flask_app
    with flask_app.test_client() as c:
        raw = c.get("/api/stats").get_data(as_text=True)
    d = json.loads(raw)["symbols"]
    assert d["best"][0]["s"] == "AAA" and d["best"][0]["r"] == 3.0
    assert d["worst"][0]["s"] == "BBB" and d["worst"][0]["r"] == -1.0
    assert "setup" not in raw and "6226" not in raw


def test_my_stats_eq_and_strategy_comparison(fresh_db, webapp_token, monkeypatch):
    """Личная эквити и «ты vs стратегия»: стратегия считается с первого отмеченного
    закрытия, кривая — кумулятивная по отметкам."""
    from app import db, webapp
    from app.delivery import payments
    from app.webapp import app as flask_app
    payments.register_user(700100, "payer")
    db.execute("UPDATE users SET tier='paid', paid_until='2099-01-01T00:00:00+00:00' "
               "WHERE tg_id=700100")
    a = _mk_closed("AAA/USDT:USDT", 2.0, "2026-08-10T00:00:00+00:00")
    _mk_closed("BBB/USDT:USDT", -1.0, "2026-08-11T00:00:00+00:00")   # не отмечена
    b = _mk_closed("CCC/USDT:USDT", 1.5, "2026-08-12T00:00:00+00:00")
    monkeypatch.setattr(webapp, "_last_prices", lambda syms: {})
    hdr = {"Authorization": "tma " + _init_data("123:TEST", 700100)}
    with flask_app.test_client() as c:
        for sid in (a, b):
            c.post("/api/take", json={"signal_id": sid, "taken": True}, headers=hdr)
        ms = json.loads(c.get("/api/me", headers=hdr).get_data(as_text=True))["my_stats"]
    assert [p["r"] for p in ms["eq"]] == [2.0, 3.5]                 # кумулятив
    assert ms["strat_r"] == 2.5 and ms["strat_n"] == 3              # с первого отмеченного
