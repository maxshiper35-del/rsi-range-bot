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
        # итоги журнала — с оговоркой и ссылкой на минутную проверку; полу-Келли (размер
        # риска под перевес журнала до комиссий, не подтверждённый проверкой) не показываем
        from app.delivery import templates
        assert body.count(templates.JOURNAL_BASIS) >= 4      # обзор, сделки, счёт, «моё»
        assert "проверка исполнения по минутным свечам — /backtest" in body
        assert "полу-Келли по журналу" not in body and "kj.half" not in body

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

@pytest.fixture
def admin_env(monkeypatch):
    """Токен + админ-id для админ-эндпоинтов."""
    from app.config import get_settings
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TEST")
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "900500")
    get_settings.cache_clear()
    yield 900500
    get_settings.cache_clear()


def test_api_admin_requires_admin(fresh_db, admin_env):
    """403 без подписи, 403 подписчику-не-админу; админу — полный пульт."""
    from app import db
    from app.delivery import payments
    from app.webapp import app as flask_app
    payments.register_user(800100, "payer")
    db.execute("UPDATE users SET tier='paid', paid_until='2099-01-01T00:00:00+00:00' "
               "WHERE tg_id=800100")
    with flask_app.test_client() as c:
        assert c.get("/api/admin").status_code == 403
        sub = c.get("/api/admin", headers={"Authorization":
                                           "tma " + _init_data("123:TEST", 800100)})
        assert sub.status_code == 403                      # платник — не админ
        adm = c.get("/api/admin", headers={"Authorization":
                                           "tma " + _init_data("123:TEST", 900500)})
    assert adm.status_code == 200
    d = json.loads(adm.get_data(as_text=True))
    for key in ("health", "users", "money", "settings", "open_signals"):
        assert key in d
    assert any(s["key"] == "paused" for s in d["settings"])


def test_api_admin_set_whitelist_and_validation(fresh_db, admin_env):
    """Только белый список ключей и валидные значения; изменение реально применяется."""
    from app import runtime
    from app.webapp import app as flask_app
    hdr = {"Authorization": "tma " + _init_data("123:TEST", 900500)}
    with flask_app.test_client() as c:
        assert c.post("/api/admin/set", json={"key": "symbols", "value": "x"},
                      headers=hdr).status_code == 400      # ключ вне списка
        assert c.post("/api/admin/set", json={"key": "min_confidence", "value": 5},
                      headers=hdr).status_code == 400      # вне диапазона
        assert c.post("/api/admin/set", json={"key": "affiliate_url",
                                              "value": "http://insecure"},
                      headers=hdr).status_code == 400      # не https
        ok = c.post("/api/admin/set", json={"key": "paused", "value": True}, headers=hdr)
        assert ok.status_code == 200
        assert runtime.paused() is True
        ok2 = c.post("/api/admin/set", json={"key": "executor_enabled", "value": True},
                     headers=hdr)
        assert ok2.status_code == 200
        assert runtime.executor_since()                    # since выставлен как /executor on


def test_api_admin_user_grant_revoke(fresh_db, admin_env):
    from app.delivery import payments
    from app.webapp import app as flask_app
    payments.register_user(800200, "target")
    hdr = {"Authorization": "tma " + _init_data("123:TEST", 900500)}
    with flask_app.test_client() as c:
        assert c.post("/api/admin/user", json={"tg_id": 800200, "action": "grant",
                                               "days": 7}, headers=hdr).status_code == 200
        assert payments.is_paid(800200) is True
        assert c.post("/api/admin/user", json={"tg_id": 800200, "action": "revoke"},
                      headers=hdr).status_code == 200
        assert payments.is_paid(800200) is False
        assert c.post("/api/admin/user", json={"tg_id": "x", "action": "grant"},
                      headers=hdr).status_code == 400
        assert c.post("/api/admin/user", json={"tg_id": 800200, "action": "grant",
                                               "days": 0.5},
                      headers=hdr).status_code == 400     # int(0.5)=0 давал ложный успех


def test_trade_chart_closed_only_and_shape(fresh_db, monkeypatch):
    """«Сделка на графике»: закрытая отдаёт свечи+уровни+метки; ОТКРЫТАЯ — 404
    (уровни открытых — платный контент, та же граница, что /trackrecord CSV);
    мусорный id — 400. Свечи мокаются — сети в тестах нет."""
    from app import db
    from app import webapp as W
    from app.webapp import app as flask_app

    db.execute(
        "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, tp1, risk_reward, "
        "confidence, snapshot_json, status, created_at, closed_at, result_r, exit_price, "
        "filled_at) VALUES (700,'BTC/USDT:USDT','LONG','1h',100,98,106,3.0,0.7,'{}',"
        "'stopped','2026-08-10T00:00:00+00:00','2026-08-11T00:00:00+00:00',2.5,105.0,"
        "'2026-08-10T02:00:00+00:00')")
    db.execute(
        "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, created_at) VALUES "
        "(701,'ETH/USDT:USDT','SHORT','1h',10,11,2.0,0.7,'{}','open',"
        "'2026-08-10T00:00:00+00:00')")
    fake = [[1754770800000 + i * 3600000, 100 + i, 101 + i, 99 + i, 100.5 + i]
            for i in range(30)]
    monkeypatch.setattr(W, "_chart_candles", lambda *a, **k: fake)
    W._CHART_CACHE.clear()
    c = flask_app.test_client()

    r = c.get("/api/trade_chart?id=700")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] and d["entry"] == 100 and d["stop"] == 98 and d["tps"] == [106]
    assert d["exit_price"] == 105.0 and d["result_r"] == 2.5
    assert len(d["candles"]) == 30 and len(d["candles"][0]) == 5
    assert d["candles"][0][0] == 1754770800          # секунды, не миллисекунды
    assert "setup" not in d and "snapshot" not in d  # таксономия не утекает

    assert c.get("/api/trade_chart?id=701").status_code == 404   # открытая — нельзя
    assert c.get("/api/trade_chart?id=abc").status_code == 400
    assert c.get("/api/trade_chart?id=99999").status_code == 404

    # эскалация ТФ: сделка на 1h длиной в 3 месяца рисуется укрупнёнными свечами
    db.execute(
        "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, created_at, closed_at, result_r) VALUES "
        "(702,'BTC/USDT:USDT','LONG','1h',100,98,2.0,0.7,'{}','stopped',"
        "'2026-05-01T00:00:00+00:00','2026-08-01T00:00:00+00:00',-1.0)")
    d = c.get("/api/trade_chart?id=702").get_json()
    assert d["ok"] and d["tf"] in ("4h", "1d") and d["tf_signal"] == "1h"


def test_stats_market_and_last_closed(fresh_db, monkeypatch):
    """/api/stats: секция «Монеты» (цена+суточный ход из кеша тикеров) и публичный
    список последних закрытых с id (для «сделки на графике»)."""
    from app import db, runtime
    from app import webapp as W
    from app.webapp import app as flask_app

    db.execute(
        "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, created_at, closed_at, result_r) VALUES "
        "(710,'BTC/USDT:USDT','LONG','1h',100,98,2.0,0.7,'{}','tp1',"
        "'2026-08-10T00:00:00+00:00','2026-08-11T00:00:00+00:00',1.0)")
    runtime.set_value("symbols", "BTC/USDT:USDT,ETH/USDT:USDT")
    monkeypatch.setattr(W, "_last_prices",
                        lambda syms: {"BTC/USDT:USDT": 76000.0})
    W._TICKERS["pcts"]["BTC/USDT:USDT"] = -1.7
    d = flask_app.test_client().get("/api/stats").get_json()
    assert d["market"] == [{"s": "BTC", "px": 76000.0, "pct": -1.7}]
    lc = d["last_closed"]
    assert lc and lc[0]["id"] == 710 and lc[0]["s"] == "BTC" and lc[0]["r"] == 1.0
    assert "entry" not in lc[0] and "stop" not in lc[0]   # уровни не в этом списке


def test_stats_r_series_for_simulator(fresh_db):
    """r_all/r_epoch — последовательность result_r в порядке закрытий: сырьё
    симулятора депозита (сложный процент считается на клиенте)."""
    from app import db
    from app.webapp import app as flask_app
    for i, (r, closed) in enumerate([(1.5, "2026-08-10"), (-1.0, "2026-08-11"),
                                     (2.2, "2026-08-12")]):
        db.execute(
            "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
            "confidence, snapshot_json, status, created_at, closed_at, result_r) VALUES "
            f"(72{i},'BTC/USDT:USDT','LONG','1h',100,98,2.0,0.7,'{{}}','stopped',"
            f"'2026-08-0{i + 1}T00:00:00+00:00','{closed}T00:00:00+00:00',{r})")
    d = flask_app.test_client().get("/api/stats").get_json()
    assert d["r_all"] == [1.5, -1.0, 2.2]
    assert d["sp_all"] == [2.0, 2.0, 2.0]                 # |100−98|/100 = 2% на сделку
    assert d["h_all"] == [0.0, 0.0, 0.0]                  # без filled_at — часов в позиции нет


def test_trade_chart_open_only_for_private(fresh_db, monkeypatch, webapp_token):
    """График ОТКРЫТОЙ сделки: публике 404 (уровни — платный контент), валидному
    подписчику/админу — 200 с флагом open и без закрывающих полей."""
    from app import db
    from app import webapp as W
    from app.delivery import payments
    from app.webapp import app as flask_app
    db.execute(
        "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, tp1, risk_reward, "
        "confidence, snapshot_json, status, created_at, filled_at) VALUES "
        "(730,'BTC/USDT:USDT','LONG','1h',100,98,106,3.0,0.7,'{}','open',"
        "'2026-08-10T00:00:00+00:00','2026-08-10T02:00:00+00:00')")
    fake = [[1754784000000 + i * 3600000, 100 + i * .1, 101, 99, 100.5] for i in range(20)]
    monkeypatch.setattr(W, "_chart_candles", lambda *a, **k: fake)
    payments.register_user(800200, "sub")
    db.execute("UPDATE users SET tier='paid', paid_until='2099-01-01T00:00:00+00:00' "
               "WHERE tg_id=800200")
    c = flask_app.test_client()
    assert c.get("/api/trade_chart?id=730").status_code == 404          # публика
    r = c.get("/api/trade_chart?id=730",
              headers={"Authorization": "tma " + _init_data(webapp_token, 800200)})
    assert r.status_code == 200
    d = r.get_json()
    assert d["open"] is True and d["in_pos"] is True and d["result_r"] is None
    assert d["entry"] == 100 and d["tps"] == [106]
    # фри-юзер с валидной подписью — тоже 404
    payments.register_user(800201, "free")
    assert c.get("/api/trade_chart?id=730",
                 headers={"Authorization": "tma " + _init_data(webapp_token, 800201)}
                 ).status_code == 404


def test_api_backtest_static_json(fresh_db):
    """/api/backtest отдаёт помесячный бэктест из репо (scripts/backtest_monthly.py):
    60 месяцев, паритетные тоталы; кеш по mtime. Эндпоинт ПУБЛИЧНЫЙ: имена сетапов
    (закрытая таксономия) не должны встречаться в ответе нигде — регрессия утечки
    by_setup 05.09–05.10.2026, когда этот же тест закреплял их наличие."""
    from app import runtime
    from app.webapp import app as flask_app
    c = flask_app.test_client()
    assert c.get("/api/backtest").status_code == 404     # по умолчанию снят с витрины
    runtime.set_value("backtest_public", "1")
    r = c.get("/api/backtest")
    assert r.status_code == 200
    d = r.get_json()
    # паритет с живым судьёй (05.10), снимок с гейтами движка прода (06.10) и гейт «вход
    # уже пройден» (07.10): 5819 сделок, +358.1R (min_stop 1.3; 2.0 откатан 24.10)
    assert len(d["months"]) == 60 and d["totals"]["trades"] == 5819
    assert "by_setup" not in d
    body = r.get_data(as_text=True)
    for token in ("6226", "LTF", "1:1", "open-SMC", "bias", "sweep", "confl"):
        assert token not in body, f"таксономия в публичном /api/backtest: {token}"
    assert d["period"] == ["2021-07", "2026-07"] and d["fee_pct"] == 0.055
    m = d["months"]["2025-04"]
    assert {"n", "r", "w"} <= set(m) and m["n"] > 0
    assert "max-age=3600" in r.headers.get("Cache-Control", "")


def test_last_prices_per_symbol_freshness(fresh_db, monkeypatch):
    """Аудит 05.10: общий штамп свежести морозил цену символа вне вотчлиста — платный
    unrealized R показывал часовую цену как живую. Теперь свежесть по символу."""
    import app.webapp as W
    clock_ = {"t": 1000.0}
    monkeypatch.setattr(W.time, "monotonic", lambda: clock_["t"])
    market = {"BTC/USDT:USDT": 100.0, "XRP/USDT:USDT": 1.15}
    calls = []

    def fake_fetch(symbols):
        calls.append(list(symbols))
        return {s: {"px": market[s], "pct": 0.0} for s in symbols if s in market}

    monkeypatch.setattr(W, "_fetch_tickers", fake_fetch)
    assert W._last_prices(["XRP/USDT:USDT"]) == {"XRP/USDT:USDT": 1.15}
    market["XRP/USDT:USDT"] = 0.91
    for _ in range(5):                       # цикл клиента: stats (вотчлист) -> me (позиция)
        clock_["t"] += 60
        W._last_prices(["BTC/USDT:USDT"])
        got = W._last_prices(["XRP/USDT:USDT"])
    assert got == {"XRP/USDT:USDT": 0.91}    # позиция перезапрошена, не заморожена
    # в окне 45с повторный запрос того же символа сеть не трогает
    n = len(calls)
    W._last_prices(["XRP/USDT:USDT"])
    assert len(calls) == n
    # отдаются только запрошенные символы
    assert set(W._last_prices(["BTC/USDT:USDT"])) == {"BTC/USDT:USDT"}
    # символ пропал из ответа биржи: через 10 мин его цена больше не отдаётся
    del market["XRP/USDT:USDT"]
    clock_["t"] += 700
    assert W._last_prices(["XRP/USDT:USDT"]) == {}


def test_trade_chart_rejects_unicode_digits(fresh_db):
    """Аудит 05.10: '²'.isdigit() == True, int('²') -> ValueError -> 500 без авторизации."""
    from app.webapp import app as flask_app
    c = flask_app.test_client()
    assert c.get("/api/trade_chart?id=%C2%B2").status_code == 400
    assert c.get("/api/trade_chart?id=1234567890123").status_code == 400


def test_avg_hold_excludes_shadow_trades(fresh_db):
    """Аудит 05.10: «в сделке» усреднял и теневые (rejected) исходы."""
    from app import db
    from app.webapp import _avg_hold_hours
    db.execute(
        "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, result_r, created_at, filled_at, closed_at) "
        "VALUES (970,'AAA/USDT:USDT','LONG','1h',1,0.9,2,0.7,'{}','stopped',-1.0,"
        "'2026-10-01T00:00:00+00:00','2026-10-01T01:00:00+00:00','2026-10-01T03:00:00+00:00'),"
        "(971,'BBB/USDT:USDT','LONG','1h',1,0.9,2,0.7,'{}','rejected',1.0,"
        "'2026-10-01T00:00:00+00:00','2026-10-01T01:00:00+00:00','2026-10-04T00:00:00+00:00')")
    assert _avg_hold_hours() == 2.0


def test_admin_panel_grant_rewards_referrer(fresh_db, admin_env):
    """Аудит 05.10: выдача из панели не награждала реферера, в отличие от /grant."""
    from app.delivery import payments
    from app.webapp import app as flask_app
    payments.register_user(800300, "ref")
    payments.register_user(800301, "invitee")
    payments.set_referrer(800301, 800300)
    hdr = {"Authorization": "tma " + _init_data("123:TEST", 900500)}
    with flask_app.test_client() as c:
        assert c.post("/api/admin/user", json={"tg_id": 800301, "action": "grant",
                                               "days": 30}, headers=hdr).status_code == 200
    assert payments.get_user(800301)["referral_rewarded_at"] is not None
    assert payments.is_paid(800300) is True


# ------------------- v6 (24.10.2026): расширенный дашборд -------------------

_SEQ = iter(range(10, 99))


def _closed(sym: str, r: float, closed: str) -> int:
    """Закрытая сделка с УНИКАЛЬНЫМ created_at (у _mk_closed он общий)."""
    from app import db
    n = next(_SEQ)
    db.execute(
        "INSERT INTO signals(symbol, side, timeframe, entry, stop, risk_reward, confidence, "
        "snapshot_json, status, created_at, filled_at, closed_at, result_r) VALUES "
        f"('{sym}','LONG','1h',1,0.9,2.0,0.7,'{{}}','stopped',"
        f"'2026-07-01T{n % 24:02d}:{n:02d}:00+00:00','2026-07-01T00:00:00+00:00','{closed}',{r})")
    return db.query_one("SELECT MAX(id) AS i FROM signals")["i"]

def _mk_open_with_setup(sym: str = "AAA/USDT:USDT") -> int:
    from app import db
    db.execute(
        "INSERT INTO signals(symbol, side, timeframe, entry, stop, tp1, risk_reward, "
        "confidence, snapshot_json, status, created_at) VALUES "
        f"('{sym}','LONG','1h',123.456,120.111,130.0,2.0,0.7,"
        "'{\"setup\": \"6226\"}','open','2026-10-24T10:00:00+00:00')")
    return db.query_one("SELECT MAX(id) AS i FROM signals")["i"]


def test_api_trades_public_filters_pagination_and_no_leak(fresh_db):
    """История закрытых: фильтры, итог по всей выборке, пагинация; без сетапов и без
    открытых сделок (их уровни — платный слой)."""
    from app.webapp import app as flask_app
    for i in range(5):
        _closed("BTC/USDT:USDT", 1.5 if i % 2 else -1.0, f"2026-08-0{i + 2}T00:00:00+00:00")
    _closed("ETH/USDT:USDT", 2.0, "2026-08-09T00:00:00+00:00")
    _mk_open_with_setup()
    with flask_app.test_client() as c:
        d = json.loads(c.get("/api/trades?limit=2").get_data(as_text=True))
        assert d["total"] == 6 and len(d["items"]) == 2 and d["items"][0]["s"] == "ETH"
        assert abs(d["sum_r"] - (2.0 + 1.5 * 2 - 1.0 * 3)) < 1e-9 and d["wins"] == 3
        page2 = json.loads(c.get("/api/trades?limit=2&offset=2").get_data(as_text=True))
        assert [x["id"] for x in page2["items"]] != [x["id"] for x in d["items"]]
        btc_win = json.loads(c.get("/api/trades?q=btc&res=win").get_data(as_text=True))
        assert btc_win["total"] == 2 and all(x["r"] > 0 for x in btc_win["items"])
        body = c.get("/api/trades?limit=100").get_data(as_text=True)
        for bad in ("6226", "LTF", "setup", "123.456", "AAA"):
            assert bad not in body
        for q in ("limit=0", "limit=101", "offset=-1", "side=up", "res=x", "tf=2h", "q=a;b"):
            assert c.get("/api/trades?" + q).status_code == 400, q


def test_api_stats_splits_and_daily(fresh_db):
    from app.webapp import app as flask_app
    _closed("BTC/USDT:USDT", 2.0, "2026-10-20T05:00:00+00:00")
    _closed("BTC/USDT:USDT", -1.0, "2026-10-20T09:00:00+00:00")
    from app import clock
    with flask_app.test_client() as c:
        d = json.loads(c.get("/api/stats").get_data(as_text=True))
    assert d["split_all"]["side"]["LONG"] == {"n": 2, "r": 1.0, "w": 1}
    assert d["split_all"]["tf"]["1h"]["n"] == 2
    if clock.utcnow().isoformat() < "2027-10-20":
        assert {"d": "2026-10-20", "n": 2, "r": 1.0} in d["daily_all"]


def test_api_me_sub_referral_and_prefs(fresh_db, webapp_token):
    """«Моё»: карточка подписки, реф-ссылка, депозит/уведомления; /api/prefs — только
    платнику, с валидацией."""
    from app import db, runtime
    from app.delivery import payments
    from app.webapp import app as flask_app
    payments.register_user(500300, "sub")
    db.execute("UPDATE users SET tier='paid', paid_until='2099-01-01T00:00:00+00:00' "
               "WHERE tg_id=500300")
    payments.register_user(500301, "free")
    runtime.set_value("bot_username", "testbot")
    hdr = {"Authorization": "tma " + _init_data("123:TEST", 500300),
           "Content-Type": "application/json"}
    with flask_app.test_client() as c:
        me = json.loads(c.get("/api/me", headers=hdr).get_data(as_text=True))
        assert me["sub"]["kind"] == "grant" and me["sub"]["days_left"] > 1000
        assert me["referral"]["link"] == "https://t.me/testbot?start=ref_500300"
        assert me["depo"] is None and me["notify"]["muted"] is False
        ok = c.post("/api/prefs", headers=hdr, data=json.dumps({"depo_usd": 2500, "risk_pct": 1.5}))
        assert ok.status_code == 200 and json.loads(ok.get_data(as_text=True))["depo"] == {
            "usd": 2500.0, "risk": 1.5}
        assert c.post("/api/prefs", headers=hdr,
                      data=json.dumps({"depo_usd": 5, "risk_pct": 1})).status_code == 400
        assert c.post("/api/prefs", headers=hdr,
                      data=json.dumps({"quiet_start": 23, "quiet_end": 7})).status_code == 200
        assert c.post("/api/prefs", headers=hdr,
                      data=json.dumps({"quiet_start": 24, "quiet_end": 7})).status_code == 400
        muted = json.dumps({"muted": True})
        assert c.post("/api/prefs", headers=hdr, data=muted).status_code == 200
        free = {"Authorization": "tma " + _init_data("123:TEST", 500301),
                "Content-Type": "application/json"}
        assert c.post("/api/prefs", headers=free, data=muted).status_code == 403
        assert c.post("/api/prefs", data=muted).status_code == 401
    assert payments.notify_prefs(500300) == {"muted": True, "quiet_start": 23, "quiet_end": 7}


def test_admin_v6_endpoints_auth_and_no_secrets(fresh_db, admin_env):
    """Новые админ-ручки: 403 не-админу; карточка юзера без api-ключей; журнал без тегов
    сетапов; executor и расширенное здоровье отдаются."""
    from app import db
    from app.delivery import payments
    from app.webapp import app as flask_app
    payments.register_user(700100, "alice")
    db.execute("UPDATE users SET api_key_enc='SECRETKEY', api_secret_enc='SECRETSEC', "
               "acquisition_source='chres' WHERE tg_id=700100")
    _mk_open_with_setup()
    adm = {"Authorization": "tma " + _init_data("123:TEST", 900500)}
    usr = {"Authorization": "tma " + _init_data("123:TEST", 700100)}
    paths = ("/api/admin/users", "/api/admin/user?id=700100", "/api/admin/signals",
             "/api/admin/executor")
    with flask_app.test_client() as c:
        for p in paths:
            assert c.get(p).status_code == 403, p
            assert c.get(p, headers=usr).status_code == 403, p
        lst = json.loads(c.get("/api/admin/users?q=ali", headers=adm).get_data(as_text=True))
        assert lst["total"] == 1 and lst["items"][0]["status"] == "free"
        one = c.get("/api/admin/user?id=700100", headers=adm).get_data(as_text=True)
        assert "SECRET" not in one and json.loads(one)["user"]["src"] == "chres"
        assert c.get("/api/admin/user?id=1", headers=adm).status_code == 404
        sig = c.get("/api/admin/signals?view=open", headers=adm).get_data(as_text=True)
        d = json.loads(sig)
        assert d["total"] == 1 and d["items"][0]["entry"] == 123.456
        assert "6226" not in sig and "setup" not in sig
        assert c.get("/api/admin/signals?view=bad", headers=adm).status_code == 400
        ex = json.loads(c.get("/api/admin/executor", headers=adm).get_data(as_text=True))
        assert ex["ok"] and "summary" in ex and "live" in ex
        full = json.loads(c.get("/api/admin", headers=adm).get_data(as_text=True))
    assert {"funnel", "daily", "by_method"} <= set(full["money"])
    assert {"log", "overdue", "power", "offsite_min"} <= set(full["health"])
    assert set(full["shadow"]) == {"setup_off", "late_entry"}


def test_admin_set_symbols_validation(fresh_db, admin_env):
    from app import runtime
    from app.webapp import app as flask_app
    hdr = {"Authorization": "tma " + _init_data("123:TEST", 900500),
           "Content-Type": "application/json"}
    with flask_app.test_client() as c:
        ok = c.post("/api/admin/set", headers=hdr, data=json.dumps(
            {"key": "symbols", "value": "btc/usdt:usdt, ETH/USDT:USDT,\nBTC/USDT:USDT"}))
        assert ok.status_code == 200
        # верхний регистр, без дублей
        assert runtime.symbols() == ["BTC/USDT:USDT", "ETH/USDT:USDT"]
        bad = c.post("/api/admin/set", headers=hdr, data=json.dumps(
            {"key": "symbols", "value": "BTCUSDT"}))
        assert bad.status_code == 400
        assert c.post("/api/admin/set", headers=hdr, data=json.dumps(
            {"key": "min_stop_pct", "value": 11})).status_code == 400
        assert c.post("/api/admin/set", headers=hdr, data=json.dumps(
            {"key": "min_stop_pct", "value": 1.3})).status_code == 200


def test_api_me_affiliate_link_for_subscriber(fresh_db, webapp_token):
    """Партнёрская ссылка в «Моё»: только https, нет ссылки — null."""
    from app import db, runtime
    from app.delivery import payments
    from app.webapp import app as flask_app
    payments.register_user(500300, "payer")
    db.execute("UPDATE users SET tier='paid', paid_until='2099-01-01T00:00:00+00:00' "
               "WHERE tg_id=500300")
    hdr = {"Authorization": "tma " + _init_data("123:TEST", 500300)}
    with flask_app.test_client() as c:
        assert c.get("/api/me", headers=hdr).get_json()["affiliate"] is None
        runtime.set_value("affiliate_url", "https://partner.bybit.com/b/RSIRANGE")
        assert c.get("/api/me", headers=hdr).get_json()["affiliate"] == \
            "https://partner.bybit.com/b/RSIRANGE"


def test_api_account_auth_registration_and_shape(fresh_db, webapp_token):
    """«Счёт»: без подписи — 401; валидная подпись без /start — registered=false; юзер —
    счёт в $ по публичным закрытым после прихода; ни уровней, ни сетапов в ответе."""
    from app import db
    from app.delivery import payments
    from app.webapp import app as flask_app
    payments.register_user(500400, "acc")                      # фри-юзер: счёт ему тоже
    db.execute("UPDATE users SET joined_at='2026-06-01T00:00:00+00:00' WHERE tg_id=500400")
    _closed("BTC/USDT:USDT", 2.0, "2026-08-01T00:00:00+00:00")
    _closed("ETH/USDT:USDT", -1.0, "2026-08-02T00:00:00+00:00")
    _mk_open_with_setup()                                      # открытая с тегом — не в счёте
    hdr = {"Authorization": "tma " + _init_data("123:TEST", 500400)}
    with flask_app.test_client() as c:
        assert c.get("/api/account").status_code == 401
        bad = {"Authorization": "tma " + _init_data("123:TEST", 500400, spoil=True)}
        assert c.get("/api/account", headers=bad).status_code == 401
        nobody = {"Authorization": "tma " + _init_data("123:TEST", 777)}
        r0 = c.get("/api/account", headers=nobody)
        assert r0.status_code == 200 and r0.get_json()["registered"] is False
        r = c.get("/api/account", headers=hdr)
        raw = r.get_data(as_text=True)
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    d = json.loads(raw)
    assert d["registered"] and d["is_default"] and d["trades"] == 2
    assert abs(d["balance"] - 1000 * 1.02 * 0.99) < 0.01 and d["push"] is True
    assert d["items"][0]["s"] == "ETH" and d["items"][0]["usd"] < 0   # свежие сверху, минус честно
    for leak in ("6226", "setup", "123.456", "AAA", "entry", '"stop"'):
        assert leak not in raw


def test_api_account_post_depo_and_push(fresh_db, webapp_token):
    """Настройки счёта из Mini App: валидация как у /api/prefs, атомарность тела,
    без /start — 404 (юзера не заводим), тумблер утреннего отчёта."""
    from app.delivery import my_account, payments
    from app.webapp import app as flask_app
    payments.register_user(500401, "acc2")
    hdr = {"Authorization": "tma " + _init_data("123:TEST", 500401),
           "Content-Type": "application/json"}
    with flask_app.test_client() as c:
        assert c.post("/api/account", data=json.dumps({"push": False})).status_code == 401
        ghost = {"Authorization": "tma " + _init_data("123:TEST", 778),
                 "Content-Type": "application/json"}
        assert c.post("/api/account", headers=ghost,
                      data=json.dumps({"push": False})).status_code == 404
        for bad in ({"depo_usd": 5, "risk_pct": 1}, {"depo_usd": 500, "risk_pct": 11},
                    {"depo_usd": True, "risk_pct": 1}, {"push": "no"}, {}, [1]):
            assert c.post("/api/account", headers=hdr, data=json.dumps(bad)).status_code == 400
        # битое поле в паре с валидным — НИЧЕГО не пишем (тело атомарно)
        assert c.post("/api/account", headers=hdr,
                      data=json.dumps({"depo_usd": 500, "risk_pct": 1, "push": 1})
                      ).status_code == 400
        assert payments.depo_prefs(500401) is None
        ok = c.post("/api/account", headers=hdr,
                    data=json.dumps({"depo_usd": 2500, "risk_pct": 1.5, "push": False}))
        d = ok.get_json()
    assert ok.status_code == 200 and d["start"] == 2500 and d["risk_pct"] == 1.5
    assert d["is_default"] is False and d["push"] is False
    assert payments.depo_prefs(500401) == (2500.0, 1.5) and not my_account.push_enabled(500401)
