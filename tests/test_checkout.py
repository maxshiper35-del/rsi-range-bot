"""Оркестрация оплаты на фейковых объектах aiogram (аудит кода 30.10, §4.4): инвойс →
pre_checkout → successful_payment, повторы и компенсация при сбое выдачи, /refund,
USDT-тиры и единое определение «платящего»."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


class FakeBot:
    def __init__(self, refund_error: Exception | None = None):
        self.invoices: list[dict] = []
        self.messages: list[tuple] = []
        self.refunds: list[tuple] = []
        self._refund_error = refund_error

    async def send_invoice(self, **kw):
        self.invoices.append(kw)

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text, kw))

    async def refund_star_payment(self, user_id, telegram_payment_charge_id):
        self.refunds.append((user_id, telegram_payment_charge_id))
        if self._refund_error is not None:
            raise self._refund_error


class FakeCallback:
    def __init__(self, uid: int, data: str):
        self.from_user = SimpleNamespace(id=uid, username=None)
        self.data = data
        self.answers: list[tuple] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakePreCheckout:
    def __init__(self, uid: int, payload: str, total: int, currency: str = "XTR"):
        self.from_user = SimpleNamespace(id=uid)
        self.invoice_payload, self.total_amount, self.currency = payload, total, currency
        self.answers: list[tuple] = []

    async def answer(self, ok, error_message=None):
        self.answers.append((ok, error_message))


class FakeMessage:
    def __init__(self, uid: int, payload: str, total: int, charge: str):
        self.from_user = SimpleNamespace(id=uid)
        self.successful_payment = SimpleNamespace(
            invoice_payload=payload, total_amount=total, currency="XTR",
            telegram_payment_charge_id=charge)
        self.answers: list[str] = []

    async def answer(self, text, **kw):
        self.answers.append(text)


class Recorder:
    def __init__(self):
        self.calls: list[tuple] = []

    async def __call__(self, *args):
        self.calls.append(args)


async def _no_sleep(_s):
    return None


def _prices(runtime, **kw):
    base = {"sub_price_stars": 500, "sub_days": 30, "week_price_stars": 150,
            "quarter_price_stars": 1300, "year_price_stars": 4500,
            "lifetime_price_stars": 9900, "sub_price_usdt": 7}
    base.update(kw)
    for k, v in base.items():
        runtime.set_value(k, v)


def _pay(uid, payload, total, charge, **kw):
    """successful_payment через checkout. -> (результат, сообщение, реферер, алерты)."""
    from app.delivery import checkout
    m, ref, alerts = FakeMessage(uid, payload, total, charge), Recorder(), Recorder()
    res = asyncio.run(checkout.process_successful_payment(
        m, reward_referrer=ref, alert_admins=alerts, sleep=_no_sleep, **kw))
    return res, m, ref, alerts


def test_invoice_precheckout_payment_end_to_end(fresh_db):
    """Кнопка недели → инвойс → pre_checkout по тому же payload → выдача 7 дней."""
    from app import runtime
    from app.delivery import checkout, payments, tariffs
    _prices(runtime)
    bot, cq = FakeBot(), FakeCallback(1001, "pay_stars_week")
    asyncio.run(checkout.pay_stars_tier(bot, cq, tariffs.stars_tier_by_callback(cq.data)))
    inv = bot.invoices[0]
    assert inv["chat_id"] == 1001 and inv["currency"] == "XTR"
    assert inv["payload"] == "sub:7.0:week" and inv["prices"][0].amount == 150
    assert cq.answers == [(None, False)]

    q = FakePreCheckout(1001, inv["payload"], inv["prices"][0].amount)
    assert asyncio.run(checkout.answer_pre_checkout(q)) is True
    assert q.answers == [(True, None)]

    res, m, ref, alerts = _pay(1001, inv["payload"], 150, "charge-w1")
    assert res is True and "Подписка активна на 7 дней" in m.answers[0]
    assert ref.calls == [(1001, 7.0)] and alerts.calls == []
    until = datetime.fromisoformat(payments.get_user(1001)["paid_until"])
    assert timedelta(days=6.9) < until - datetime.now(timezone.utc) < timedelta(days=7.1)
    assert payments.has_paid_ever(1001) is True


def test_precheckout_rejects_changed_price_and_disabled_tier(fresh_db):
    from app import runtime
    from app.delivery import checkout
    _prices(runtime)
    q = FakePreCheckout(1002, "sub:30.0:month", 500)
    runtime.set_value("sub_price_stars", 600)                   # цена выросла после инвойса
    assert asyncio.run(checkout.answer_pre_checkout(q)) is False
    assert q.answers == [(False, checkout.PRECHECKOUT_REJECT_TEXT)]
    old = FakePreCheckout(1002, "sub:30.0", 600)                # инвойс до реестра — принят
    assert asyncio.run(checkout.answer_pre_checkout(old)) is True
    runtime.set_value("year_price_stars", 0)
    y = FakePreCheckout(1002, "sub:365.0:year", 4500)
    assert asyncio.run(checkout.answer_pre_checkout(y)) is False


def test_disabled_tier_button_sends_no_invoice(fresh_db):
    from app import runtime
    from app.delivery import checkout, tariffs
    _prices(runtime, quarter_price_stars=0)
    bot, cq = FakeBot(), FakeCallback(1003, "pay_stars_quarter")
    asyncio.run(checkout.pay_stars_tier(bot, cq, tariffs.stars_tier_by_callback(cq.data)))
    assert bot.invoices == [] and cq.answers[0][1] is True     # алерт, без инвойса


def test_duplicate_payment_not_granted_twice(fresh_db):
    from app import runtime
    _prices(runtime)
    assert _pay(1004, "sub:30.0:month", 500, "charge-d")[0] is True
    res, m, ref, _ = _pay(1004, "sub:30.0:month", 500, "charge-d")
    assert res is False and "уже был учтён" in m.answers[0] and ref.calls == []


def test_grant_retried_after_transient_failure(fresh_db, monkeypatch):
    from app import runtime
    from app.delivery import payments
    _prices(runtime)
    real, calls = payments.grant_subscription, []

    def flaky(*a):
        calls.append(a)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return real(*a)

    monkeypatch.setattr(payments, "grant_subscription", flaky)
    res, m, ref, alerts = _pay(1005, "sub:30.0:month", 500, "charge-f")
    assert res is True and len(calls) == 2 and alerts.calls == []
    assert payments.is_paid(1005) is True


def test_grant_failure_tells_user_and_alerts_admins(fresh_db, monkeypatch):
    """Stars списаны, а выдача не прошла ни с одной попытки: юзер не остаётся в тишине,
    админ получает charge id и готовую команду."""
    from app import runtime
    from app.delivery import checkout, payments
    _prices(runtime)

    def broken(*a):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(payments, "grant_subscription", broken)
    res, m, ref, alerts = _pay(1006, "sub:30.0:month", 500, "charge-x", attempts=3)
    assert res is None and m.answers == [checkout.GRANT_FAILED_TEXT] and ref.calls == []
    text = alerts.calls[0][0]
    assert "charge-x" in text and "/grant 1006 30" in text
    # дробный срок: /grant берёт только целые дни — подсказка без готовой команды
    res, m, ref, alerts = _pay(1006, "sub:0.208333:intro", 10, "charge-y", attempts=1)
    assert "/grant 1006" not in alerts.calls[0][0] and "5 часов" in alerts.calls[0][0]


def test_unreadable_payload_falls_back_to_month(fresh_db):
    from app import runtime
    _prices(runtime)
    res, m, ref, _ = _pay(1007, "мусор", 500, "charge-g")
    assert res is True and "30 дней" in m.answers[0]


def test_refund_paths(fresh_db):
    from app import runtime
    from app.delivery import checkout, payments
    _prices(runtime)
    replies: list[str] = []

    async def reply(text):
        replies.append(text)

    bot = FakeBot()
    assert asyncio.run(checkout.refund_last_stars_payment(bot, 1008, reply)) is False
    assert "не найдено" in replies[-1]

    payments.grant_subscription(1008, "stars", 500, "XTR", 30, "charge-r")
    bot = FakeBot(refund_error=RuntimeError("Bad Request: PAYMENT_NOT_FOUND"))
    assert asyncio.run(checkout.refund_last_stars_payment(bot, 1008, reply)) is False
    assert "отказал" in replies[-1] and payments.find_refundable_stars_payment(1008)

    bot = FakeBot()
    assert asyncio.run(checkout.refund_last_stars_payment(bot, 1008, reply)) is True
    assert bot.refunds == [(1008, "charge-r")] and "Возвращено 500⭐" in replies[-1]
    assert bot.messages[0][0] == 1008                           # юзер узнал о возврате
    assert payments.is_paid(1008) is False

    # краш между API и БД в прошлый раз: Telegram уже вернул — дочиниваем запись
    payments.grant_subscription(1009, "stars", 500, "XTR", 30, "charge-s")
    bot = FakeBot(refund_error=RuntimeError("Bad Request: CHARGE_ALREADY_REFUNDED"))
    assert asyncio.run(checkout.refund_last_stars_payment(bot, 1009, reply)) is True
    assert payments.find_refundable_stars_payment(1009) is None


def test_usdt_tier_invoice_and_activation(fresh_db, monkeypatch):
    from app import runtime
    from app.delivery import checkout, payments, tariffs
    _prices(runtime)
    runtime.set_value("cryptobot_token", "tok")
    runtime.set_value("usdt_bonus_days", "2")
    created: list[dict] = []

    def fake_call(method, params, post=True):
        if method == "createInvoice":
            created.append(params)
            return {"invoice_id": 4242, "bot_invoice_url": "https://t.me/CryptoBot?start=x"}
        return {"items": [{"invoice_id": 4242, "status": "paid"}]}

    monkeypatch.setattr(payments, "_cryptobot_call", fake_call)
    bot, cq = FakeBot(), FakeCallback(1010, "pay_usdt_quarter")
    asyncio.run(checkout.send_usdt_invoice(bot, cq, tariffs.usdt_tier_by_callback(cq.data)))
    assert created[0]["amount"] == "18.2" and created[0]["payload"] == "1010:90.0"
    assert "90 дней" in bot.messages[0][1] and "+2 дн" in bot.messages[0][1]
    assert payments.poll_cryptobot() == [(1010, 90.0)]
    until = datetime.fromisoformat(payments.get_user(1010)["paid_until"])
    assert timedelta(days=91) < until - datetime.now(timezone.utc) < timedelta(days=93)

    runtime.set_value("year_price_stars", 0)
    cq2 = FakeCallback(1010, "pay_usdt_year")
    asyncio.run(checkout.send_usdt_invoice(bot, cq2, tariffs.usdt_tier_by_callback(cq2.data)))
    assert cq2.answers == [("Тариф выключен.", True)] and len(created) == 1

    def down(method, params, post=True):
        raise RuntimeError("network")

    monkeypatch.setattr(payments, "_cryptobot_call", down)
    cq3 = FakeCallback(1010, "pay_usdt")
    asyncio.run(checkout.send_usdt_invoice(bot, cq3, tariffs.usdt_tier_by_callback(cq3.data)))
    assert "временно недоступен" in cq3.answers[0][0]


def test_payment_functions_reject_non_positive_days(fresh_db, monkeypatch):
    from app import runtime
    from app.delivery import payments
    with pytest.raises(ValueError):
        payments.grant_subscription(1011, "stars", 500, "XTR", 0, "charge-z")
    with pytest.raises(ValueError):
        payments.admin_grant(1011, 0)
    runtime.set_value("cryptobot_token", "tok")
    monkeypatch.setattr(payments, "_cryptobot_call", lambda *a, **k: pytest.fail("API"))
    with pytest.raises(ValueError):
        payments.create_cryptobot_invoice(1011, -30, amount=7)
    assert payments.is_paid(1011) is False


def test_grant_usdt_payer_is_a_payer_everywhere(fresh_db):
    """Ручной USDT через /grant — платящий для всех мест сразу (paid_ever_sql)."""
    from app.delivery import payments
    two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    for uid in (1012, 1014):                                    # оба на 3-м дне триала
        payments.grant_trial(uid, 5)
        fresh_db.execute("UPDATE users SET trial_granted_at=? WHERE tg_id=?",
                         (two_days_ago, uid))
    payments.record_usdt_claim(1012)
    payments.admin_grant(1012, 30)                              # строки в payments нет
    assert payments.has_paid_ever(1012) is True
    assert payments.has_paid_ever(1014) is False
    assert payments.subscriber_breakdown() == {"paying": 1, "granted": 0, "trial": 1}
    assert payments.acquisition_breakdown()["—"] == {"total": 2, "paid": 1}
    assert [uid for uid, _ in payments.due_for_trial_drip(5.0)] == [1014]
    payments.register_user(1013)
    assert payments.set_referrer(1012, 1013) is False           # платящего не переатрибутируем
    assert payments.set_referrer(1014, 1013) is True
