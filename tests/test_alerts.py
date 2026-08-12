"""Прайс-алерты (/alert): CRUD, one-shot семантика, резолв тикера, лимит."""
from __future__ import annotations


def test_resolve_symbol():
    from app.delivery import alerts
    wl = ("BTC/USDT:USDT", "1000PEPE/USDT:USDT")
    assert alerts.resolve_symbol("btc", wl) == "BTC/USDT:USDT"
    assert alerts.resolve_symbol("BTC/USDT", wl) == "BTC/USDT:USDT"
    assert alerts.resolve_symbol("1000pepe", wl) == "1000PEPE/USDT:USDT"
    assert alerts.resolve_symbol("SOL", wl) is None          # не в вотчлисте
    assert alerts.resolve_symbol("", wl) is None


def test_alert_lifecycle_one_shot(fresh_db):
    from app.delivery import alerts
    res = alerts.create_alert(10, "BTC/USDT:USDT", 65000.0)
    assert res["ok"] and res["id"]
    assert len(alerts.active_alerts(10)) == 1
    # бар не дотянулся -> не стреляет
    assert alerts.check_and_trigger("BTC/USDT:USDT", high=64900, low=64000) == []
    # касание диапазоном бара -> стреляет ровно один раз (one-shot)
    fired = alerts.check_and_trigger("BTC/USDT:USDT", high=65100, low=64500)
    assert len(fired) == 1 and fired[0]["tg_id"] == 10 and fired[0]["price"] == 65000.0
    assert alerts.check_and_trigger("BTC/USDT:USDT", high=65100, low=64500) == []
    assert alerts.active_alerts(10) == []                    # сработавший не в списке


def test_alert_delete_own_only(fresh_db):
    from app.delivery import alerts
    aid = alerts.create_alert(10, "BTC/USDT:USDT", 65000.0)["id"]
    assert alerts.delete_alert(999, aid) is False            # чужой — отказ
    assert alerts.delete_alert(10, aid) is True
    assert alerts.delete_alert(10, aid) is False             # повторно — уже нет


def test_alert_limit(fresh_db):
    from app.delivery import alerts
    for i in range(alerts.MAX_ACTIVE_PER_USER):
        assert alerts.create_alert(10, "BTC/USDT:USDT", 1000.0 + i)["ok"]
    over = alerts.create_alert(10, "BTC/USDT:USDT", 9999.0)
    assert over == {"ok": False, "reason": "limit", "id": None}
    # чужие алерты лимит не съедают
    assert alerts.create_alert(11, "BTC/USDT:USDT", 500.0)["ok"]


def test_alerts_per_symbol_isolation(fresh_db):
    from app.delivery import alerts
    alerts.create_alert(10, "BTC/USDT:USDT", 65000.0)
    alerts.create_alert(10, "ETH/USDT:USDT", 2000.0)
    fired = alerts.check_and_trigger("ETH/USDT:USDT", high=2100, low=1900)
    assert len(fired) == 1 and fired[0]["symbol"] == "ETH/USDT:USDT"
    assert len(alerts.active_alerts(10)) == 1                # BTC-алерт жив
