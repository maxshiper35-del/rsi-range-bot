"""Реестр тарифов (tariffs.py): один источник для кнопок, инвойсов, pre_checkout и USDT."""
from __future__ import annotations

import pytest


def _set_prices(runtime, **kw):
    base = {"sub_price_stars": 500, "sub_days": 30, "week_price_stars": 150,
            "quarter_price_stars": 1300, "year_price_stars": 4500,
            "lifetime_price_stars": 9900, "intro_price_stars": 10, "intro_hours": 5,
            "sub_price_usdt": 7}
    base.update(kw)
    for k, v in base.items():
        runtime.set_value(k, v)


def test_stars_tiers_follow_runtime(fresh_db):
    from app import runtime
    from app.delivery import tariffs
    _set_prices(runtime, quarter_price_stars=0)
    tiers = {t.key: t for t in tariffs.stars_tiers()}
    assert [t.key for t in tariffs.stars_tiers()] == list(tariffs.STARS_KEYS)
    assert (tiers["month"].days, tiers["month"].stars) == (30.0, 500)
    assert (tiers["week"].days, tiers["week"].stars) == (7.0, 150)
    assert tiers["intro"].days == 0.208333 and tiers["intro"].stars == 10
    assert tiers["life"].days == 36500.0
    assert tiers["quarter"].enabled is False                  # 0 = тариф выключен
    assert {t.callback for t in tiers.values()} == tariffs.STARS_CALLBACKS
    assert tariffs.stars_tier_by_callback("pay_stars").key == "month"
    assert tariffs.stars_tier_by_callback("pay_stars_life").key == "life"
    assert tariffs.stars_tier_by_callback("мусор") is None


def test_intro_off_when_hours_zero(fresh_db):
    from app import runtime
    from app.delivery import tariffs
    _set_prices(runtime, intro_hours=0)
    assert tariffs.stars_tier("intro").enabled is False


def test_payload_roundtrip_and_parse_rejects_garbage():
    from app.delivery import tariffs
    t = tariffs.Tier("quarter", 90.0, 1300)
    assert tariffs.invoice_payload(t) == "sub:90.0:quarter"
    assert tariffs.parse_payload("sub:90.0:quarter") == (90.0, "quarter")
    assert tariffs.parse_payload("sub:30") == (30.0, None)    # инвойсы до реестра
    for bad in (None, "", "мусор", "sub:", "sub:x", "sub:0", "sub:-30", "sub:nan",
                "sub:inf", "sub:30:нет-такого", "sub:30:month:лишнее", "unlock:5:50"):
        assert tariffs.parse_payload(bad) is None, bad


def test_precheckout_matches_key_minutes_and_current_price():
    """Аудит 05.10 + аудит кода 30.10: сверка тарифа, срока и ТЕКУЩЕЙ цены — по ключу и
    целым минутам, без равенства float."""
    from app.delivery import tariffs
    T = tariffs.Tier
    tiers = [T("intro", 0.208333, 10), T("week", 7.0, 150), T("month", 30.0, 500),
             T("quarter", 90.0, 0), T("life", 36500.0, 9900)]
    ok = tariffs.precheckout_ok
    assert ok("sub:30.0:month", 500, "XTR", tiers) is True
    assert ok("sub:30:month", 500, "XTR", tiers) is True
    assert ok(f"sub:{5 / 24}:intro", 10, "XTR", tiers) is True    # полный float против 6 знаков
    assert ok("sub:0.208333", 10, "XTR", tiers) is True             # старый формат
    assert ok("sub:30.0", 500, "XTR", tiers) is True
    assert ok("sub:30.0:month", 1000, "XTR", tiers) is False        # прежняя цена
    assert ok("sub:90.0:quarter", 1300, "XTR", tiers) is False      # тариф выключен
    assert ok("sub:7.0:month", 150, "XTR", tiers) is False          # ключ не сходится со сроком
    assert ok("sub:30.0:week", 500, "XTR", tiers) is False          # чужой ключ
    assert ok("sub:31:month", 500, "XTR", tiers) is False           # срок изменился
    assert ok("sub:30.0:month", 500, "USD", tiers) is False
    assert ok("мусор", 500, "XTR", tiers) is False
    assert ok(None, 0, "XTR", tiers) is False
    assert ok("unlock:5:50", 50, "XTR", tiers) is True               # paid media — как раньше
    assert tariffs.match_tier("sub:36500.0:life", 9900, "XTR", tiers).key == "life"


def test_usdt_tiers_proportional_to_stars(fresh_db):
    from app import runtime
    from app.delivery import tariffs
    _set_prices(runtime, year_price_stars=0)
    usdt = {t.key: t for t in tariffs.usdt_tiers()}
    assert usdt["month"].amount == 7.0 and usdt["month"].label == "30 дней"
    assert f"{usdt['month'].days:g} дн" == "30 дн"              # кнопка USDT без «30.0 дн»
    assert usdt["quarter"].amount == round(7 * 1300 / 500, 2)   # 18.2
    assert usdt["year"].enabled is False                         # Stars-тариф выключен
    assert usdt["life"].days == 36500.0
    assert {t.callback for t in usdt.values()} == tariffs.USDT_CALLBACKS
    assert tariffs.usdt_tier_by_callback("pay_usdt_quarter").amount == 18.2
    runtime.set_value("sub_price_stars", 0)
    assert tariffs.usdt_tier_price(1300) == 0.0                  # нет базы — нет пропорции


@pytest.mark.parametrize("raw", ["0", "-5", "nan", "мусор"])
def test_runtime_sub_days_never_zero(fresh_db, raw):
    """Ноль дней — оплата без доступа и деление на ноль в кнопках конца триала: битый
    override в settings даёт дефолт (аудит кода 30.10)."""
    from app import runtime
    runtime.set_value("sub_days", raw)
    assert runtime.sub_days() == 30.0


def test_valid_sub_days():
    from app.delivery import tariffs
    assert tariffs.valid_sub_days(30) and tariffs.valid_sub_days(0.01)
    assert tariffs.valid_sub_days(36500)
    for bad in (0, -1, float("nan"), float("inf"), 36501):
        assert not tariffs.valid_sub_days(bad)
