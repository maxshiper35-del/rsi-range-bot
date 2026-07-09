"""Тесты подписки: регистрация, выдача/продление, идемпотентность, список подписчиков."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def test_register_and_free_by_default(fresh_db):
    from app.delivery import payments
    payments.register_user(555, "vasya")
    u = payments.get_user(555)
    assert u["tier"] == "free" and u["username"] == "vasya"
    assert payments.is_paid(555) is False
    # повторный /start обновляет username, не дублирует
    payments.register_user(555, "vasya2")
    assert fresh_db.query_one("SELECT COUNT(*) AS c FROM users")["c"] == 1
    assert payments.get_user(555)["username"] == "vasya2"


def test_grant_subscription_activates(fresh_db):
    from app.delivery import payments
    assert payments.grant_subscription(777, "stars", 1000, "XTR", 30, "charge_A") is True
    assert payments.is_paid(777) is True
    u = payments.get_user(777)
    assert u["tier"] == "paid"
    # paid_until ~ сейчас + 30 дней
    until = datetime.fromisoformat(u["paid_until"])
    assert timedelta(days=29) < (until - datetime.now(timezone.utc)) < timedelta(days=31)
    assert payments.active_subscriber_ids() == [777]


def test_grant_idempotent_and_extends(fresh_db):
    from app.delivery import payments
    payments.grant_subscription(777, "stars", 1000, "XTR", 30, "charge_A")
    first_until = payments.get_user(777)["paid_until"]
    # тот же charge -> дубль, ничего не меняется
    assert payments.grant_subscription(777, "stars", 1000, "XTR", 30, "charge_A") is False
    assert payments.get_user(777)["paid_until"] == first_until
    # новый charge -> продление ещё на 30 дней (не с нуля)
    assert payments.grant_subscription(777, "stars", 1000, "XTR", 30, "charge_B") is True
    second_until = datetime.fromisoformat(payments.get_user(777)["paid_until"])
    assert second_until - datetime.fromisoformat(first_until) > timedelta(days=29)
    assert fresh_db.query_one("SELECT COUNT(*) AS c FROM payments")["c"] == 2


def test_expired_not_in_subscribers(fresh_db):
    from app import clock, db
    from app.delivery import payments
    payments.register_user(888)
    past = "2020-01-01T00:00:00+00:00"
    db.execute("UPDATE users SET tier='paid', paid_until=? WHERE tg_id=?", (past, 888))
    assert payments.is_paid(888) is False
    assert 888 not in payments.active_subscriber_ids()
    _ = clock  # noqa: F841


def test_status_text(fresh_db):
    from app.delivery import payments
    txt = payments.subscription_status_text(999)
    assert "Telegram Stars" in txt and "Нажми кнопку" in txt
    payments.grant_subscription(999, "stars", 1000, "XTR", 30, "c1")
    assert "активна до" in payments.subscription_status_text(999)


# --- воронка free-тира ---
def test_funnel_free_limit_then_teaser(fresh_db):
    from app.delivery import payments
    payments.register_user(100)                                   # free
    payments.grant_subscription(200, "stars", 1, "XTR", 30, "c")  # paid
    admins = (900,)

    def deliver(sid, limit=2):
        plan = payments.plan_recipients(sid, limit, admins)
        for uid, kind in plan:
            payments.record_delivery(uid, sid, kind)
        return dict(plan)

    p1 = deliver(1)
    assert p1[100] == "full" and p1[200] == "full" and p1[900] == "full"
    assert deliver(2)[100] == "full"                              # 2-й в пределах лимита
    p3 = deliver(3)
    assert p3[100] == "teaser"                                    # лимит free исчерпан
    assert p3[200] == "full"                                      # платный всегда полный
    # уже доставленный не дублируется
    assert 100 not in dict(payments.plan_recipients(3, 2, admins))


def test_record_delivery_idempotent(fresh_db):
    from app.delivery import payments
    assert payments.record_delivery(1, 1, "full") is True
    assert payments.record_delivery(1, 1, "full") is False
    assert payments.delivered(1, 1) is True


# --- жизненный цикл ---
def test_expire_overdue(fresh_db):
    from app import db
    from app.delivery import payments
    payments.grant_subscription(300, "stars", 1, "XTR", 30, "c")
    db.execute("UPDATE users SET paid_until=? WHERE tg_id=?", ("2020-01-01T00:00:00+00:00", 300))
    assert payments.expire_overdue() == [300]
    assert payments.get_user(300)["tier"] == "free"
    assert payments.expire_overdue() == []                        # идемпотентно


def test_renewal_reminder(fresh_db):
    from datetime import datetime, timedelta, timezone

    from app import db
    from app.delivery import payments
    payments.grant_subscription(400, "stars", 1, "XTR", 30, "c")
    soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    db.execute("UPDATE users SET paid_until=? WHERE tg_id=?", (soon, 400))
    assert [r["tg_id"] for r in payments.due_for_reminder(3)] == [400]
    payments.mark_reminded(400)
    assert payments.due_for_reminder(3) == []                     # повторно не напоминаем
    payments.grant_subscription(400, "stars", 1, "XTR", 30, "c2")  # оплата сбрасывает метку
    assert payments.get_user(400)["renew_reminded_at"] is None


# --- USDT-заявки: SLA, эскалация, win-back ---
def test_usdt_claims_lifecycle(fresh_db):
    from datetime import datetime, timedelta, timezone

    from app import db
    from app.delivery import payments
    payments.register_user(700)
    cid = payments.record_usdt_claim(700)
    assert cid >= 1
    # свежая заявка не считается зависшей
    assert payments.stale_usdt_claims(6) == []
    # состарим заявку -> попадает в эскалацию
    old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    db.execute("UPDATE usdt_claims SET created_at=? WHERE id=?", (old, cid))
    stale = payments.stale_usdt_claims(6)
    assert len(stale) == 1 and stale[0]["id"] == cid
    # после эскалации — не пингуем повторно
    payments.mark_claim_escalated(cid)
    assert payments.stale_usdt_claims(6) == []
    # /grant закрывает все открытые заявки юзера
    cid2 = payments.record_usdt_claim(700)
    db.execute("UPDATE usdt_claims SET created_at=? WHERE id=?", (old, cid2))
    payments.admin_grant(700, 30)
    assert payments.stale_usdt_claims(6) == []
    open_claims = fresh_db.query_one(
        "SELECT COUNT(*) AS c FROM usdt_claims WHERE tg_id=700 AND resolved_at IS NULL")["c"]
    assert open_claims == 0


def test_winback_flow(fresh_db):
    from datetime import datetime, timedelta, timezone

    from app import db
    from app.delivery import payments
    payments.grant_subscription(710, "stars", 1, "XTR", 30, "wb1")
    # истёк ВЧЕРА: expire понижает, но win-back ещё рано (отложенное касание, не дубль
    # уведомления «подписка истекла» в том же такте)
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    db.execute("UPDATE users SET paid_until=? WHERE tg_id=?", (yesterday, 710))
    payments.expire_overdue()
    assert payments.due_for_winback(2) == []
    # истёк 3 дня назад -> внутри окна [2, 2+3] дней -> пора напомнить
    three_days = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    db.execute("UPDATE users SET paid_until=? WHERE tg_id=?", (three_days, 710))
    due = payments.due_for_winback(2)
    assert [r["tg_id"] for r in due] == [710]
    payments.mark_winback_reminded(710)
    assert payments.due_for_winback(2) == []            # ровно одно напоминание
    # истёкшие давно (за окном) — не беспокоим
    long_ago = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    db.execute("UPDATE users SET paid_until=?, winback_reminded_at=NULL WHERE tg_id=?",
               (long_ago, 710))
    assert payments.due_for_winback(2) == []
    # новая оплата сбрасывает метку win-back
    payments.grant_subscription(710, "stars", 1, "XTR", 30, "wb2")
    assert payments.get_user(710)["winback_reminded_at"] is None


# --- атрибуция источника ---
def test_acquisition_source_first_touch(fresh_db):
    from app.delivery import payments
    payments.register_user(720)
    payments.set_acquisition_source(720, "kol1")
    assert payments.get_user(720)["acquisition_source"] == "kol1"
    payments.set_acquisition_source(720, "ads2")        # повторная ссылка не перезаписывает
    assert payments.get_user(720)["acquisition_source"] == "kol1"
    payments.register_user(721)                          # без метки — NULL
    assert payments.get_user(721)["acquisition_source"] is None


# --- USDT через CryptoBot (мок API) ---
def test_cryptobot_enabled_via_runtime_token(fresh_db):
    from app import runtime
    from app.delivery import payments
    assert payments.cryptobot_enabled() is False         # ни env, ни override
    runtime.set_value("cryptobot_token", "tok_x")        # админ ввёл из Telegram
    assert payments.cryptobot_enabled() is True          # без .env и рестарта


def test_poll_cryptobot_adds_bonus_days(fresh_db, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app import runtime
    from app.delivery import payments
    runtime.set_value("cryptobot_token", "tok")
    runtime.set_value("usdt_bonus_days", "2")

    def fake_call(method, params, post=True):
        if method == "createInvoice":
            return {"invoice_id": 900, "bot_invoice_url": "https://t.me/x"}
        return {"items": [{"invoice_id": 900, "status": "paid"}]}

    monkeypatch.setattr(payments, "_cryptobot_call", fake_call)
    payments.create_cryptobot_invoice(800, 30, amount=20)
    assert payments.poll_cryptobot() == [800]
    until = datetime.fromisoformat(payments.get_user(800)["paid_until"])
    delta = until - datetime.now(timezone.utc)
    assert timedelta(days=31) < delta < timedelta(days=33)   # 30 + 2 бонусных



def test_cryptobot_invoice_and_poll(fresh_db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("CRYPTOBOT_TOKEN", "tok")
    get_settings.cache_clear()
    from app.delivery import payments

    assert payments.cryptobot_enabled() is True

    def fake_call(method, params, post=True):
        if method == "createInvoice":
            return {"invoice_id": 555, "bot_invoice_url": "https://t.me/CryptoBot?start=inv555"}
        return {"items": [{"invoice_id": 555, "status": "paid"}]}

    monkeypatch.setattr(payments, "_cryptobot_call", fake_call)

    pay_url, inv_id = payments.create_cryptobot_invoice(600, 30, amount=20)
    assert inv_id == "555" and "CryptoBot" in pay_url
    assert payments.is_paid(600) is False                          # ждём оплату
    assert payments.poll_cryptobot() == [600]                      # оплачено -> активировано
    assert payments.is_paid(600) is True
    assert payments.poll_cryptobot() == []                         # идемпотентно
    get_settings.cache_clear()
