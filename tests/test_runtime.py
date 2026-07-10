"""Тесты рантайм-настроек (правятся из Telegram) и админ-управления подписками."""
from __future__ import annotations


def test_defaults_from_env(fresh_db):
    from app import runtime
    # без override — дефолты из .env/Settings (conftest: SYMBOLS дефолт BTC,ETH)
    assert runtime.paused() is False
    assert runtime.channel_mode() == "full"
    assert isinstance(runtime.symbols(), list) and runtime.symbols()
    assert runtime.signal_mode() in ("auto", "semiauto")


def test_set_and_read_override(fresh_db):
    from app import runtime
    runtime.set_value("paused", "1")
    assert runtime.paused() is True
    runtime.set_value("symbols", "BTC/USDT:USDT,XRP/USDT:USDT")
    assert runtime.symbols() == ["BTC/USDT:USDT", "XRP/USDT:USDT"]
    runtime.set_value("min_confidence", "0.7")
    assert runtime.min_confidence() == 0.7
    runtime.set_value("channel_mode", "teaser")
    assert runtime.channel_mode() == "teaser"
    runtime.set_value("sub_price_stars", "500")
    assert runtime.price_stars() == 500
    # перезапись
    runtime.set_value("paused", "0")
    assert runtime.paused() is False


def test_effective_snapshot(fresh_db):
    from app import runtime
    eff = runtime.effective()
    for k in ("paused", "symbols", "timeframes", "signal_mode", "channel_mode",
              "min_confidence", "price_stars", "sub_days", "free_per_week"):
        assert k in eff


def test_new_runtime_keys(fresh_db):
    from app import runtime
    # cornix: дефолт выключен
    assert runtime.cornix_block_enabled() is False
    runtime.set_value("cornix_block_enabled", "1")
    assert runtime.cornix_block_enabled() is True
    # usdt-бонус: дефолт 2, override
    assert runtime.usdt_bonus_days() == 2
    runtime.set_value("usdt_bonus_days", "5")
    assert runtime.usdt_bonus_days() == 5
    # cryptobot_token: без override и без env — пусто
    assert runtime.cryptobot_token() == ""
    runtime.set_value("cryptobot_token", "tok123")
    assert runtime.cryptobot_token() == "tok123"
    # trial_days: дефолт 5 (нет .env-фолбэка, дефолт зашит в runtime.py), override
    assert runtime.trial_days() == 5
    runtime.set_value("trial_days", "7")
    assert runtime.trial_days() == 7


def test_enabled_setups_default_is_conservative(fresh_db):
    from app import runtime
    # дефолт: только 1:1 (6226/LTF доказанно убыточны в бэктесте, LTF-BB подтвердил живьём)
    assert runtime.enabled_setups() == ("1:1",)
    runtime.set_value("enabled_setups", "1:1,6226")
    assert runtime.enabled_setups() == ("1:1", "6226")
    runtime.set_value("enabled_setups", "6226,1:1,open-SMC,LTF")
    assert runtime.enabled_setups() == ("6226", "1:1", "open-SMC", "LTF")


def test_live_board_state(fresh_db):
    from app import runtime
    assert runtime.last_board_update_at() is None
    assert runtime.live_board_message_id() is None
    runtime.set_last_board_update_at("2026-01-01T00:00:00+00:00")
    assert runtime.last_board_update_at() == "2026-01-01T00:00:00+00:00"
    runtime.set_live_board_message_id(42)
    assert runtime.live_board_message_id() == 42
    # None очищает -> следующий цикл создаст новый пост, а не будет биться в протухший id
    runtime.set_live_board_message_id(None)
    assert runtime.live_board_message_id() is None


def test_admin_grant_and_revoke(fresh_db):
    from app.delivery import payments
    assert payments.admin_grant(4242, 14) is True
    assert payments.is_paid(4242) is True
    assert payments.user_counts() == {"total": 1, "paid": 1}
    payments.admin_revoke(4242)
    assert payments.is_paid(4242) is False
    assert payments.user_counts()["paid"] == 0


def test_format_admin_status():
    from app.delivery import bot
    eff = {"paused": True, "symbols": ["BTC/USDT:USDT"], "timeframes": ["15m", "1h"],
           "signal_mode": "auto", "channel": "-100123", "channel_enabled": True,
           "channel_mode": "full", "min_confidence": 0.5, "min_impulse_atr": 1.0,
           "enabled_setups": ("1:1",),
           "price_stars": 1000, "price_usdt": 20, "sub_days": 30, "free_per_week": 3}
    txt = bot.format_admin_status(eff, {"total": 5, "paid": 2})
    assert "НА ПАУЗЕ" in txt and "BTC/USDT:USDT" in txt and "платных 2" in txt
    assert "1000⭐" in txt


def test_display_leverage(fresh_db):
    from app import runtime
    assert runtime.display_leverage() == 10.0           # дефолт: иллюстрация ×10
    runtime.set_value("display_leverage", 25)
    assert runtime.display_leverage() == 25.0
    runtime.set_value("display_leverage", 0)            # 0 = строка плеча выключена
    assert runtime.display_leverage() == 0.0
    runtime.set_value("display_leverage", -5)           # отрицательное не имеет смысла -> off
    assert runtime.display_leverage() == 0.0
    runtime.set_value("display_leverage", "мусор")      # битое значение -> дефолт, не падение
    assert runtime.display_leverage() == 10.0
    assert runtime.effective()["display_leverage"] == 10.0


def test_signal_chart_enabled_flag(fresh_db):
    from app import runtime
    assert runtime.signal_chart_enabled() is False      # фича-флаг: дефолт выкл
    runtime.set_value("signal_chart_enabled", "1")
    assert runtime.signal_chart_enabled() is True
    assert runtime.effective()["signal_chart_enabled"] is True


def test_intro_tier(fresh_db):
    from app import runtime
    assert runtime.intro_price_stars() == 10            # дефолт: пробный 10⭐/5ч
    assert runtime.intro_hours() == 5.0
    runtime.set_value("intro_price_stars", 0)           # 0 = пробный тариф выключен
    assert runtime.intro_price_stars() == 0
    runtime.set_value("intro_hours", 12)
    assert runtime.intro_hours() == 12.0
    eff = runtime.effective()
    assert eff["intro_price_stars"] == 0 and eff["intro_hours"] == 12.0


def test_paid_signal_stars(fresh_db):
    from app import runtime
    assert runtime.paid_signal_stars() == 0                 # дефолт: выключено (за флагом)
    runtime.set_value("paid_signal_stars", 50)
    assert runtime.paid_signal_stars() == 50
    assert runtime.effective()["paid_signal_stars"] == 50
