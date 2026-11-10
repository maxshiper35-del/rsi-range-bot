"""Режим «сигналы бесплатны всем» (решение 10.11.2026, free_signals_per_week >= 1000):
подписку не продаём. Бот не принимает оплату, не зовёт в /subscribe и не закрывает
функции «для подписчиков» — иначе он противоречит сообщению «сигналы теперь бесплатны»."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.core.signal_engine import Side, Signal
from app.delivery import bot, templates
from tests.test_checkout import FakeBot, FakeCallback, FakePreCheckout, _prices

FREE = "10000"


def _free(runtime, on: bool = True) -> None:
    runtime.set_value("free_signals_per_week", FREE if on else "3")


def _sig() -> Signal:
    return Signal(symbol="BTC/USDT:USDT", side=Side.SHORT, timeframe="1h", entry=62340.0,
                  stop=62980.0, take_profits=[60893.0], risk_reward=2.26, confidence=0.65,
                  reasons=["свип хая"], created_at="2026-01-01T00:00:00+00:00",
                  indicators_snapshot={"setup": "6226"})


def test_full_access_follows_mode(fresh_db):
    """Гейты «для подписчиков» открыты всем только в режиме «бесплатно всем»."""
    from app import runtime
    from app.delivery import payments
    payments.register_user(1, "free")
    payments.register_user(2, "paid")
    payments.grant_trial(2, 5)
    _free(runtime, False)
    assert payments.has_full_access(1) is False and payments.has_full_access(2) is True
    assert payments.full_access_ids() == [2] and payments.limited_ids() == [1]
    _free(runtime)
    assert payments.has_full_access(1) is True and payments.has_full_access(2) is True
    # объявления и дайджест уходят всем живым, «упущенного» блока нет ни у кого
    assert sorted(payments.full_access_ids()) == [1, 2] and payments.limited_ids() == []
    payments.mark_blocked(1)
    assert payments.full_access_ids() == [2]


def test_start_onboarding_marks_without_access_period(fresh_db):
    """Новичку в режиме «бесплатно всем» ставится только отметка начала (для дрипа): нет
    paid_until — нет ни «триала», ни сообщения о его конце."""
    from app.delivery import payments
    assert payments.start_onboarding(10) is True
    u = payments.get_user(10)
    assert u["trial_granted_at"] and u["paid_until"] is None and u["tier"] == "free"
    assert payments.is_paid(10) is False and payments.trial_summary(10) is None
    assert payments.start_onboarding(10) is False               # второй /start — не новичок
    assert payments.expire_overdue() == []                      # понижать и уведомлять некого
    # дрип идёт от отметки: через сутки — стадия 1
    fresh_db.execute("UPDATE users SET trial_granted_at=? WHERE tg_id=10",
                     ((datetime.now(timezone.utc) - timedelta(days=1.1)).isoformat(),))
    assert payments.due_for_trial_drip(5.0) == [(10, 1)]
    # у кого доступ уже был (триал, оплата) — отметку не трогаем
    payments.grant_trial(11, 5)
    assert payments.start_onboarding(11) is False


def test_checkout_refuses_money_when_free_for_all(fresh_db, monkeypatch):
    """Старые кнопки тарифов и старые инвойсы остаются в чатах: инвойс не создаётся,
    pre_checkout отклоняется до списания, USDT-счёт не выписывается."""
    from app import runtime
    from app.delivery import checkout, payments, tariffs
    _prices(runtime)
    _free(runtime)
    tg, cq = FakeBot(), FakeCallback(1001, "pay_stars_week")
    asyncio.run(checkout.pay_stars_tier(tg, cq, tariffs.stars_tier_by_callback(cq.data)))
    assert tg.invoices == [] and cq.answers == [(checkout.SALES_CLOSED_TEXT, True)]

    q = FakePreCheckout(1001, "sub:7.0:week", 150)              # payload и цена верные
    assert asyncio.run(checkout.answer_pre_checkout(q)) is False
    assert q.answers == [(False, checkout.SALES_CLOSED_TEXT)]

    def no_call(*a, **kw):
        raise AssertionError("счёт CryptoBot создаваться не должен")

    monkeypatch.setattr(payments, "_cryptobot_call", no_call)
    cq2 = FakeCallback(1001, "pay_usdt")
    asyncio.run(checkout.send_usdt_invoice(tg, cq2, tariffs.usdt_tier_by_callback(cq2.data)))
    assert cq2.answers == [(checkout.SALES_CLOSED_TEXT, True)] and tg.messages == []
    assert len(checkout.SALES_CLOSED_TEXT) <= 200               # лимит алерта кнопки Telegram

    # продажу вернули — тот же инвойс проходит
    _free(runtime, False)
    q2 = FakePreCheckout(1001, "sub:7.0:week", 150)
    assert asyncio.run(checkout.answer_pre_checkout(q2)) is True


def test_free_for_all_status_text(fresh_db):
    """Экран /subscribe без тарифов: ни цены, ни «оплатить»; оплаченный доступ назван
    только у того, кто действительно платил."""
    from app import runtime
    from app.delivery import payments
    _prices(runtime)
    _free(runtime)
    payments.register_user(1, "a")
    txt = payments.free_for_all_status_text(1)
    assert "бесплатны для всех" in txt and "/check" in txt and "/audit" in txt
    for word in ("Stars", "⭐", "USDT", "оплатить", "Оплатить", "тариф", "Money-back"):
        assert word not in txt
    assert "оплаченный доступ" not in txt
    payments.grant_trial(2, 5)                                  # триал — не оплата
    assert "оплаченный доступ" not in payments.free_for_all_status_text(2)
    payments.grant_subscription(3, "stars", 500.0, "XTR", 30.0, "charge-1")
    assert "оплаченный доступ до" in payments.free_for_all_status_text(3)


def test_templates_without_subscription_pitch():
    """Тизер, рентген и «Пригласить» в режиме «бесплатно всем» не зовут в подписку."""
    teaser = templates.render_teaser(_sig(), 9, free_for_all=True)
    assert "бесплатно в боте" in teaser and "62340" not in teaser and "62980" not in teaser
    x = {"id": 7, "symbol": "ETH/USDT:USDT", "side": "LONG", "timeframe": "4h",
         "status": "expired", "setup": "LTF-BB", "entry": 2000.0, "stop": 1900.0,
         "exit_price": None, "result_r": None, "confidence": 0.61, "stop_pct": 5.0,
         "scores": None, "swept": False, "clean_impulse": False, "reasons": [],
         "wait_hours": None, "hold_hours": None, "trail_moves": 0, "trail_peak_r": None}
    xray = templates.render_xray(x, True)
    link = "https://t.me/rsirange_bot?start=ref_5"
    invite = templates.render_referral_invite(link, {"invited": 4, "rewarded": 2}, 7, True)
    assert link in invite and "<b>4</b>" in invite
    for txt in (teaser, xray, invite):
        assert "/subscribe" not in txt and "подписк" not in txt.lower() and "🔒" not in txt
    # обычный режим не изменился
    assert "/subscribe" in templates.render_teaser(_sig(), 9)
    assert "/subscribe" in templates.render_xray(x)
    assert "дни подписки" in templates.render_referral_invite(link, {}, 7)


def test_menu_and_help_put_checking_first():
    """Меню и /help режима «бесплатно всем»: проверка сигнала и канала — первой, кнопок и
    команд подписки нет. Старые кнопки остаются известными меню (закрывают диалог)."""
    menu = bot.main_menu_layout(False, True)
    flat = [t for row in menu for t in row]
    assert menu[0] == ["🔎 Проверить сигнал", "📡 Проверить канал"]
    assert "🏆 Рейтинг каналов" in flat and "📊 Статистика" in flat
    assert not {"💳 Подписка", "🤝 Пригласить", "🎟 Промокод"} & set(flat)
    assert len(flat) == len(set(flat)) and all(len(row) == 2 for row in menu)
    assert set(flat) <= bot.MENU_TEXTS
    assert {"💳 Подписка", "🤝 Пригласить", "🎟 Промокод"} <= bot.MENU_TEXTS
    assert bot.main_menu_layout(False) == bot.main_menu_layout(False, False)
    assert bot.main_menu_layout(True, True) == bot.main_menu_layout(True)      # админ — как был

    txt = bot.format_help(False, True)
    lines = txt.splitlines()
    assert [ln.split(" — ")[0] for ln in lines[1:4]] == ["/check", "/audit", "/rating"]
    assert "/subscribe" not in txt and "/promo" not in txt
    assert "/referral — поделиться ботом" in txt and bot.ANTISCAM_LINE_FREE in txt
    assert "/subscribe — подписка" in bot.format_help(False)    # обычный режим не изменился


def test_start_text_free_for_all():
    """Приветствие режима: проверка — первой, сигналы бесплатно, ни слова о подписке,
    триале и оплате кнопками."""
    txt = bot.START_FREE_TEXT
    assert txt.index("Проверьте любой сигнал") < txt.index("Наши сигналы — бесплатно")
    assert "@rsi_range" in txt and "/rating" in txt and "Прибыль не обещаем" in txt
    for word in ("подписк", "/subscribe", "триал", "пробн", "дн."):
        assert word not in txt.lower()
    assert len(txt) < 1500


def test_about_and_terms_say_payment_is_off():
    about = bot.format_about(free_for_all=True)
    assert "Сигналы бесплатны для всех" in about and "/check, /audit, /rating" in about
    assert "не просим перевести деньги" in about and "оплату вне этого бота" not in about
    assert "оплату вне этого бота" in bot.format_about()        # обычный режим не изменился
    terms = bot.format_terms(True)
    assert "оплата в боте отключена" in terms and "money-back 7 дней" in terms
    assert "Оплата через Telegram Stars или USDT" not in terms
    assert "не просим оплату вне кнопок" not in terms
    assert terms.count("<b>") == bot.format_terms().count("<b>")      # те же 9 пунктов
    assert "Оплата через Telegram Stars или USDT" in bot.format_terms()


def test_game_prize_is_zero_when_free_for_all(fresh_db):
    """Приз игры — дни доступа: в режиме «бесплатно всем» он ничего не даёт, тексты игры
    его не обещают; настройка сохраняется и возвращается вместе с продажей."""
    from app import runtime
    from app.delivery import game
    runtime.set_value("game_prize_days", "5")
    _free(runtime, False)
    assert runtime.game_prize_days() == 5
    _free(runtime)
    assert runtime.game_prize_days() == 0
    assert "полного доступа" not in game.render_rules(runtime.game_prize_days())
    _free(runtime, False)
    assert runtime.game_prize_days() == 5


def test_drip_last_stage_is_moot_only_without_access_period():
    """«Пробный доступ заканчивается завтра» не шлём тому, у кого доступа «на время» нет;
    обучающие стадии и юзеры с идущим триалом не затронуты."""
    no_period, with_period = {"paid_until": None}, {"paid_until": "2026-11-12T00:00:00+00:00"}
    assert bot.drip_stage_is_moot(3, True, no_period) is True
    assert bot.drip_stage_is_moot(3, True, None) is True
    assert bot.drip_stage_is_moot(3, True, with_period) is False
    assert bot.drip_stage_is_moot(3, False, no_period) is False
    for stage in templates.TRIAL_DRIP:
        assert bot.drip_stage_is_moot(stage, True, no_period) is False
