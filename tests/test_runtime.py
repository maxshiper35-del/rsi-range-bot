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


def test_bad_numeric_override_falls_back_to_default(fresh_db):
    """Аудит [6]: битое значение в числовой настройке -> дефолт, а не краш getter'а
    (иначе одно '/price stars 1k' или ручная правка БД бриксовали бы подписку/админку)."""
    from app import runtime
    for key, getter in (("sub_price_stars", runtime.price_stars),
                         ("sub_price_usdt", runtime.price_usdt),
                         ("sub_days", runtime.sub_days),
                         ("min_confidence", runtime.min_confidence),
                         ("watchlist_size", runtime.watchlist_size),
                         ("quarter_price_stars", runtime.quarter_price_stars)):
        runtime.set_value(key, "мусор-1k")
        val = getter()                                   # не должно бросить
        assert isinstance(val, (int, float))
    # и /status-снимок собирается без падения даже с битыми значениями
    assert isinstance(runtime.effective(), dict)


def test_effective_snapshot(fresh_db):
    from app import runtime
    eff = runtime.effective()
    for k in ("paused", "symbols", "timeframes", "signal_mode", "channel_mode",
              "min_confidence", "price_stars", "sub_days", "free_per_week"):
        assert k in eff


def test_new_runtime_keys(fresh_db, monkeypatch):
    from app import runtime
    from app.config import get_settings
    # cornix: дефолт выключен
    assert runtime.cornix_block_enabled() is False
    runtime.set_value("cornix_block_enabled", "1")
    assert runtime.cornix_block_enabled() is True
    # usdt-бонус: дефолт 2, override
    assert runtime.usdt_bonus_days() == 2
    runtime.set_value("usdt_bonus_days", "5")
    assert runtime.usdt_bonus_days() == 5
    # cryptobot_token: без override и без env — пусто. Условие «без env» тест обязан
    # обеспечить САМ: в дев-.env с 12.08.2026 лежит РЕАЛЬНЫЙ токен (резерв отката
    # прода), и полагаться на пустоту окружения больше нельзя. setenv(""), а не delenv:
    # load_dotenv(override=False) при пересборке настроек вернул бы УДАЛЁННУЮ переменную
    # из .env, а существующую (пусть пустую) не трогает.
    monkeypatch.setenv("CRYPTOBOT_TOKEN", "")
    get_settings.cache_clear()
    try:
        assert runtime.cryptobot_token() == ""
        runtime.set_value("cryptobot_token", "tok123")
        assert runtime.cryptobot_token() == "tok123"
    finally:
        get_settings.cache_clear()      # не отдавать соседним тестам кэш с пустым env
    # trial_days: дефолт 5 (нет .env-фолбэка, дефолт зашит в runtime.py), override
    assert runtime.trial_days() == 5
    runtime.set_value("trial_days", "7")
    assert runtime.trial_days() == 7


def test_trail_defaults_from_five_year_backtest(fresh_db):
    """Схема выхода после пересмотра 28.07.2026 на 5-летнем прогоне.

    Дистанция 0.2R: зависимость монотонна (0.2 < 0.3 < 0.5) в обеих независимых выборках,
    поэтому дефолт изменён. Активация остаётся 1.5R: там оптимум между выборками
    переворачивается, менять статус-кво не на чем (см. docstring trail_distance_r)."""
    from app import db, runtime
    assert runtime.trail_distance_r() == 0.2
    # фикстура гасит трейлинг (см. conftest), поэтому дефолт активации проверяем,
    # сняв её override — иначе тест читал бы значение фикстуры, а не код
    db.execute("DELETE FROM settings WHERE key='trail_activate_r'")
    assert runtime.trail_activate_r() == 1.5
    runtime.set_value("trail_distance_r", "0.5")          # админка по-прежнему перекрывает
    assert runtime.trail_distance_r() == 0.5


def test_enabled_setups_default_excludes_setup_11(fresh_db):
    from app import runtime
    # дефолт: 6226 + LTF-BB/LTF-OB, без 1:1 — 5-летний прогон дал 1:1 exp +0.029R против
    # +0.237R у этого набора; обоснование и цифры в комментарии к _DEFAULT_ENABLED_SETUPS
    assert runtime.enabled_setups() == ("6226", "LTF")
    assert "1:1" not in runtime.enabled_setups()
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
    uc = payments.user_counts()
    assert uc["total"] == 1 and uc["paid"] == 1      # + paying/trial/granted (разбивка 07.09)
    payments.admin_revoke(4242)
    assert payments.is_paid(4242) is False
    assert payments.user_counts()["paid"] == 0


def test_format_admin_status():
    from app.delivery import bot
    eff = {"paused": True, "symbols": ["BTC/USDT:USDT"], "timeframes": ["15m", "1h"],
           "signal_mode": "auto", "channel": "-100123", "channel_enabled": True,
           "channel_mode": "full", "min_confidence": 0.5, "min_impulse_atr": 1.0,
           "min_rr_open_model": 0.5, "max_open_per_side": 6, "enabled_setups": ("6226", "LTF"),
           "price_stars": 1000, "price_usdt": 20, "sub_days": 30, "free_per_week": 3}
    txt = bot.format_admin_status(
        eff, {"total": 5, "paid": 2, "paying": 2, "trial": 0, "granted": 0})
    assert "НА ПАУЗЕ" in txt and "BTC/USDT:USDT" in txt and "платящих 2" in txt
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


def test_lifecycle_chart_enabled_flag(fresh_db):
    from app import runtime
    assert runtime.lifecycle_chart_enabled() is True    # дефолт ВКЛ (просили график на филле/тейке)
    runtime.set_value("lifecycle_chart_enabled", "0")
    assert runtime.lifecycle_chart_enabled() is False
    assert runtime.effective()["lifecycle_chart_enabled"] is False


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


def test_long_tiers_and_watchlist_settings(fresh_db):
    from app import runtime
    # длинные тарифы: дефолты и скрытие нулём
    assert runtime.quarter_price_stars() == 2500
    assert runtime.year_price_stars() == 8000
    assert runtime.lifetime_price_stars() == 15000
    runtime.set_value("year_price_stars", 0)
    assert runtime.year_price_stars() == 0
    # авто-watchlist: дефолт ВЫКЛ, blacklist парсится
    assert runtime.watchlist_auto_enabled() is False
    assert runtime.watchlist_size() == 15
    runtime.set_value("watchlist_blacklist", "DOGE/USDT:USDT, PEPE/USDT:USDT")
    assert runtime.watchlist_blacklist() == ("DOGE/USDT:USDT", "PEPE/USDT:USDT")
    # вехи
    assert runtime.last_milestone() == 0
    runtime.set_value("last_milestone", "25")
    assert runtime.last_milestone() == 25


def test_webapp_url(fresh_db):
    from app import runtime
    assert runtime.webapp_url() == ""                       # пусто -> кнопка отдаёт файл
    runtime.set_value("webapp_url", "https://example.com/app")
    assert runtime.webapp_url() == "https://example.com/app"


def test_vip_channel_settings(fresh_db):
    from app import runtime
    assert runtime.vip_channel_id() == "" and runtime.vip_invite_link() == ""
    runtime.set_value("vip_channel_id", "@rsi_range_vip")
    runtime.set_value("vip_invite_link", "https://t.me/+abc123")
    assert runtime.vip_channel_id() == "@rsi_range_vip"
    assert runtime.vip_invite_link() == "https://t.me/+abc123"
    eff = runtime.effective()
    assert eff["vip_channel_id"] == "@rsi_range_vip"
    assert eff["vip_invite_link"] == "https://t.me/+abc123"


def test_min_rr_open_model_flag(fresh_db):
    from app import runtime
    assert runtime.min_rr_open_model() == 0.5            # дефолт: пол на живой инцидент #145
    runtime.set_value("min_rr_open_model", "0.8")
    assert runtime.min_rr_open_model() == 0.8
    assert runtime.effective()["min_rr_open_model"] == 0.8


def test_min_stop_pct_and_time_filter_defaults(fresh_db):
    """Костовый гейт включён по умолчанию (1.3% — бюджет издержек ≈6% риска сделки),
    NY-окно остаётся статус-кво ВКЛ: портфельный OOS его пользу не подтвердил."""
    from app import runtime
    assert runtime.min_stop_pct() == 1.3
    assert runtime.time_filter_enabled() is True
    runtime.set_value("min_stop_pct", "0")
    assert runtime.min_stop_pct() == 0.0            # 0 = гейт выключается из админки
    runtime.set_value("time_filter_enabled", "0")
    assert runtime.time_filter_enabled() is False
    runtime.set_value("min_stop_pct", "мусор")      # битое значение -> дефолт, не краш
    assert runtime.min_stop_pct() == 1.3
    eff = runtime.effective()
    assert eff["min_stop_pct"] == 1.3 and eff["time_filter_enabled"] is False


def test_ad_spend_accumulates_per_label(fresh_db):
    """Расход на посевы (/ad): копится по метке, минус — поправка, ниже нуля не уходит;
    битое значение в settings пропускается, а не роняет /growth."""
    from app import runtime
    assert runtime.ad_spend() == {}
    assert runtime.add_ad_spend("tg_a", 1500) == 1500
    assert runtime.add_ad_spend("tg_a", 500) == 2000
    assert runtime.add_ad_spend("tg_a", -300) == 1700
    assert runtime.add_ad_spend("tg_b", -100) == 0          # поправка не уводит в минус
    runtime.set_value("ad_spend:tg_c", "мусор")
    runtime.set_value("ad_spend_x", "999")                   # чужой ключ без ':' — не метка
    assert runtime.ad_spend() == {"tg_a": 1700.0, "tg_b": 0.0}


def test_teaser_per_day_default_and_clamp(fresh_db):
    from app import runtime
    assert runtime.teaser_per_day() == 1                    # дефолт: один апселл в сутки
    runtime.set_value("teaser_per_day", "0")
    assert runtime.teaser_per_day() == 0
    runtime.set_value("teaser_per_day", "-3")
    assert runtime.teaser_per_day() == 0                    # отрицательное -> 0, не «без потолка»
    runtime.set_value("teaser_per_day", "мусор")
    assert runtime.teaser_per_day() == 1
    assert runtime.effective()["teaser_per_day"] == 1


def test_week_tier_default_and_override(fresh_db):
    """Неделя 150⭐ (27.10): дороже месяца в пересчёте на неделю, 0 прячет кнопку."""
    from app import runtime
    assert runtime.week_price_stars() == 150
    runtime.set_value("sub_price_stars", "500")             # прод-цена месяца с 27.10
    runtime.set_value("sub_days", "30")
    assert runtime.week_price_stars() > runtime.price_stars() * 7 / runtime.sub_days()
    runtime.set_value("week_price_stars", "0")
    assert runtime.week_price_stars() == 0
    assert runtime.effective()["week_price_stars"] == 0


def test_watchlist_venue_default_bingx(fresh_db):
    """27.10: авто-watchlist — только монеты площадки исполнения (демо BingX)."""
    from app import runtime
    assert runtime.watchlist_venue() == "bingx"
    runtime.set_value("watchlist_venue", "off")
    assert runtime.watchlist_venue() == ""
    runtime.set_value("watchlist_venue", "BingX ")
    assert runtime.watchlist_venue() == "bingx"
