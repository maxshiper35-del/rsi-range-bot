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


def test_grant_subscription_fractional_days(fresh_db):
    """Тестовый тариф (часы, не дни): 5/24 дня должно продлевать ровно на ~5 часов,
    не падать и не округляться до 0/1 дня."""
    from app.delivery import payments
    assert payments.grant_subscription(777, "stars", 10, "XTR", 5 / 24, "charge_hours") is True
    u = payments.get_user(777)
    until = datetime.fromisoformat(u["paid_until"])
    delta = until - datetime.now(timezone.utc)
    assert timedelta(hours=4, minutes=55) < delta < timedelta(hours=5, minutes=5)


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


def test_fmt_days():
    from app.delivery import payments
    assert payments._fmt_days(30) == "30 дней"
    assert payments._fmt_days(5 / 24) == "5 часов"


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



def test_funnel_teaser_daily_cap(fresh_db):
    """Потолок тизеров (замер 27.10: ~6 замков в день на сигналы из открытого канала, половина
    юзеров заблокировала бота): сверх teaser_per_day за сутки free-юзер сигнал не получает,
    недельный лимит полных и платные не затронуты; None — прежнее поведение без потолка."""
    from app.delivery import payments
    payments.register_user(110)                                   # free, лимит полных = 0
    payments.grant_subscription(210, "stars", 1, "XTR", 30, "c210")
    admins = (900,)

    def deliver(sid, cap):
        plan = payments.plan_recipients(sid, 0, admins, cap)
        for uid, kind in plan:
            payments.record_delivery(uid, sid, kind)
        return dict(plan)

    assert deliver(1, 1)[110] == "teaser"                         # первый тизер за сутки
    p2 = deliver(2, 1)
    assert 110 not in p2                                          # потолок — молчим
    assert p2[210] == "full" and p2[900] == "full"
    assert deliver(3, 2)[110] == "teaser"                         # потолок 2 — ещё один
    assert 110 not in deliver(4, 0)                               # 0 — тизеров нет вовсе
    assert deliver(5, None)[110] == "teaser"                      # None — без потолка

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


def test_grant_trial_new_user(fresh_db):
    from app.delivery import payments
    assert payments.grant_trial(700, 5) is True
    u = payments.get_user(700)
    assert u["tier"] == "paid" and u["trial_granted_at"] is not None
    until = datetime.fromisoformat(u["paid_until"])
    assert timedelta(days=4) < (until - datetime.now(timezone.utc)) < timedelta(days=6)


def test_grant_trial_once_only(fresh_db):
    from app.delivery import payments
    assert payments.grant_trial(701, 5) is True
    first_until = payments.get_user(701)["paid_until"]
    # повторный /start (например после истечения триала) — второй раз НЕ выдаёт
    assert payments.grant_trial(701, 5) is False
    assert payments.get_user(701)["paid_until"] == first_until


def test_grant_trial_not_for_ever_paid_user(fresh_db):
    """Юзер, у которого КОГДА-ЛИБО была подписка (даже истёкшая) — триал не для него,
    иначе лапсед-юзер получал бы бесплатный доступ при каждом возврате."""
    from app import db
    from app.delivery import payments
    payments.grant_subscription(702, "stars", 1000, "XTR", 30, "charge_1")
    db.execute("UPDATE users SET paid_until=? WHERE tg_id=?",
              ("2020-01-01T00:00:00+00:00", 702))            # подписка истекла давно
    assert payments.is_paid(702) is False                     # сейчас не платит
    assert payments.grant_trial(702, 5) is False               # но триал всё равно не положен


def test_grant_trial_expires_via_expire_overdue(fresh_db):
    from app import db
    from app.delivery import payments
    payments.grant_trial(703, 5)
    db.execute("UPDATE users SET paid_until=? WHERE tg_id=?",
              ("2020-01-01T00:00:00+00:00", 703))
    assert payments.expire_overdue() == [703]
    assert payments.get_user(703)["tier"] == "free"


def test_redeem_promo_activates(fresh_db):
    from app.delivery import payments
    payments.create_promo("WELCOME10", 10)
    res = payments.redeem_promo(800, "WELCOME10")
    assert res == {"ok": True, "days": 10, "reason": None}
    u = payments.get_user(800)
    until = datetime.fromisoformat(u["paid_until"])
    assert timedelta(days=9) < (until - datetime.now(timezone.utc)) < timedelta(days=11)


def test_redeem_promo_not_found(fresh_db):
    from app.delivery import payments
    assert payments.redeem_promo(801, "NOPE") == {"ok": False, "days": None, "reason": "not_found"}


def test_redeem_promo_already_used(fresh_db):
    from app.delivery import payments
    payments.create_promo("ONCE", 7)
    assert payments.redeem_promo(802, "ONCE")["ok"] is True
    res2 = payments.redeem_promo(802, "ONCE")
    assert res2 == {"ok": False, "days": None, "reason": "already_used"}
    # дни не начислены дважды — код применён ровно один раз
    n = fresh_db.query_one(
        "SELECT COUNT(*) AS c FROM promo_redemptions WHERE code='ONCE'")["c"]
    assert n == 1
    # used_count тоже не накрутился повторной попыткой того же юзера (аудит [3]/[8])
    uc = fresh_db.query_one("SELECT used_count FROM promo_codes WHERE code='ONCE'")["used_count"]
    assert uc == 1


def test_redeem_promo_max_uses_enforced(fresh_db):
    """Аудит [3]: used_count не превышает max_uses; лишние погашения -> exhausted."""
    from app.delivery import payments
    payments.create_promo("LIMIT2", 5, max_uses=2)
    assert payments.redeem_promo(901, "LIMIT2")["ok"] is True
    assert payments.redeem_promo(902, "LIMIT2")["ok"] is True
    r3 = payments.redeem_promo(903, "LIMIT2")
    assert r3 == {"ok": False, "days": None, "reason": "exhausted"}
    uc = fresh_db.query_one("SELECT used_count FROM promo_codes WHERE code='LIMIT2'")["used_count"]
    assert uc == 2                                       # ровно лимит, не 3


def test_redeem_promo_exhausted(fresh_db):
    from app.delivery import payments
    payments.create_promo("LIMITED", 5, max_uses=1)
    assert payments.redeem_promo(803, "LIMITED")["ok"] is True
    res = payments.redeem_promo(804, "LIMITED")           # другой юзер — лимит уже исчерпан
    assert res == {"ok": False, "days": None, "reason": "exhausted"}


def test_redeem_promo_same_user_low_max_uses_reports_already_used_not_exhausted(fresh_db):
    """Регрессия: при max_uses=1 повторная попытка ТОГО ЖЕ юзера должна честно говорить
    'уже использовали', а не маскироваться под 'код исчерпан' (баг найден живой проверкой)."""
    from app.delivery import payments
    payments.create_promo("SOLO", 5, max_uses=1)
    assert payments.redeem_promo(808, "SOLO")["ok"] is True
    res = payments.redeem_promo(808, "SOLO")               # тот же юзер, повторно
    assert res == {"ok": False, "days": None, "reason": "already_used"}


def test_redeem_promo_expired(fresh_db):
    from app.delivery import payments
    payments.create_promo("OLD", 5, expires_at="2020-01-01T00:00:00+00:00")
    res = payments.redeem_promo(805, "OLD")
    assert res == {"ok": False, "days": None, "reason": "expired"}


def test_redeem_promo_different_users_both_succeed(fresh_db):
    """Лимит — на КОД в целом (max_uses), не блокирует РАЗНЫХ юзеров по отдельности."""
    from app.delivery import payments
    payments.create_promo("SHARED", 3)
    assert payments.redeem_promo(806, "SHARED")["ok"] is True
    assert payments.redeem_promo(807, "SHARED")["ok"] is True


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


def test_acquisition_breakdown_counts_real_payments_only(fresh_db):
    """Ключевой инвариант /growth: триал/промо ставят paid_until, но НЕ считаются 'оплатили' —
    только реальный платёж (payments.status='paid') двигает конверсию."""
    from app.delivery import payments

    payments.register_user(730)
    payments.set_acquisition_source(730, "kol1")
    payments.grant_trial(730, 5)                          # paid_until есть, но не платёж

    payments.register_user(731)
    payments.set_acquisition_source(731, "kol1")
    payments.grant_subscription(731, "stars", 1000, "XTR", 30, "charge_731")  # реальный платёж

    payments.register_user(732)                           # без метки — органика

    b = payments.acquisition_breakdown()
    assert b["kol1"] == {"total": 2, "paid": 1}            # только 731 реально оплатил
    assert b["—"] == {"total": 1, "paid": 0}               # 732 — органика, не платил


def test_acquisition_breakdown_promo_not_counted_as_paid(fresh_db):
    from app.delivery import payments
    payments.register_user(733)
    payments.set_acquisition_source(733, "promo_campaign")
    payments.create_promo("GROWTHTEST", 5)
    payments.redeem_promo(733, "GROWTHTEST")               # paid_until есть, но не платёж
    b = payments.acquisition_breakdown()
    assert b["promo_campaign"] == {"total": 1, "paid": 0}



def test_revenue_by_source_paid_only_currencies_apart(fresh_db):
    """Выручка /growth по источнику: только status='paid', Stars и USDT не складываем."""
    from app.delivery import payments
    payments.register_user(740)
    payments.set_acquisition_source(740, "tg_a")
    payments.grant_subscription(740, "stars", 500, "XTR", 30, "charge_740")
    payments.grant_subscription(740, "stars", 10, "XTR", 1, "charge_740b")
    payments.register_user(741)
    payments.set_acquisition_source(741, "tg_a")
    payments.grant_subscription(741, "cryptobot", 7, "USDT", 30, "inv_741")
    payments.register_user(742)
    payments.grant_trial(742, 5)                           # триал — не выручка
    assert payments.revenue_by_source() == {"tg_a": {"XTR": 510.0, "USDT": 7.0}}

# --- USDT через CryptoBot (мок API) ---
def test_cryptobot_enabled_via_runtime_token(fresh_db, monkeypatch):
    from app import runtime
    from app.config import get_settings
    from app.delivery import payments
    # «ни env» тест обеспечивает сам: в дев-.env с 12.08.2026 живёт реальный токен
    # (резерв отката прода). setenv(""), не delenv — load_dotenv вернул бы удалённую.
    monkeypatch.setenv("CRYPTOBOT_TOKEN", "")
    get_settings.cache_clear()
    try:
        assert payments.cryptobot_enabled() is False     # ни env, ни override
        runtime.set_value("cryptobot_token", "tok_x")    # админ ввёл из Telegram
        assert payments.cryptobot_enabled() is True      # без .env и рестарта
    finally:
        get_settings.cache_clear()


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
    assert payments.poll_cryptobot() == [(800, 30.0)]   # (tg_id, оплаченные дни) для реф-гейта
    until = datetime.fromisoformat(payments.get_user(800)["paid_until"])
    delta = until - datetime.now(timezone.utc)
    assert timedelta(days=31) < delta < timedelta(days=33)   # 30 + 2 бонусных



def test_cryptobot_call_sends_own_user_agent(fresh_db, monkeypatch):
    """Ревью 31.10: Cloudflare перед pay.crypt.bot режет «Python-urllib» (403, error 1010) —
    USDT-кнопки были мертвы. Запрос обязан нести свой User-Agent и токен."""
    import io
    import json

    from app import runtime
    from app.delivery import payments
    runtime.set_value("cryptobot_token", "tok")
    sent = {}

    def fake_urlopen(req, timeout):
        sent.update({k.lower(): v for k, v in req.header_items()})
        return io.BytesIO(json.dumps({"ok": True, "result": {"name": "rsirange"}}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert payments._cryptobot_call("getMe", {}, post=False) == {"name": "rsirange"}
    assert sent["user-agent"] == payments.CRYPTOBOT_USER_AGENT
    assert "urllib" not in sent["user-agent"].lower()
    assert sent["crypto-pay-api-token"] == "tok"


def test_poll_cryptobot_asks_for_all_pending_invoices(fresh_db, monkeypatch):
    """Ревью 31.10: getInvoices без count отдаёт 100 — оплаченный 101-й pending не
    активировался. Опрос просит count и все pending-инвойсы."""
    from app import runtime
    from app.delivery import payments
    runtime.set_value("cryptobot_token", "tok")
    seq = iter(range(1000, 1200))
    calls: list[dict] = []

    def fake_call(method, params, post=True):
        if method == "createInvoice":
            n = next(seq)
            return {"invoice_id": n, "bot_invoice_url": f"https://t.me/x{n}"}
        calls.append(params)
        ids = params["invoice_ids"].split(",")
        return {"items": [{"invoice_id": int(i), "status": "paid" if i == "1101" else "active"}
                          for i in ids]}

    monkeypatch.setattr(payments, "_cryptobot_call", fake_call)
    for uid in range(102):
        payments.create_cryptobot_invoice(9000 + uid, 30, amount=7)
    assert payments.poll_cryptobot() == [(9101, 30.0)]
    assert calls[0]["count"] == 1000 and len(calls[0]["invoice_ids"].split(",")) == 102


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
    assert payments.poll_cryptobot() == [(600, 30.0)]              # оплачено -> активировано
    assert payments.is_paid(600) is True
    assert payments.poll_cryptobot() == []                         # идемпотентно
    get_settings.cache_clear()


def test_refund_flow(fresh_db):
    """Money-back: найти платёж -> (API-возврат снаружи) -> пометить refunded, срезать дни."""
    from datetime import datetime, timezone

    from app.delivery import payments
    # оплата 30 дней Stars с charge_id
    assert payments.grant_subscription(501, "stars", 1000, "XTR", 30, "chg_abc") is True
    pay = payments.find_refundable_stars_payment(501)
    assert pay is not None and pay["charge_id"] == "chg_abc" and pay["days"] == 30
    assert payments.is_paid(501) is True

    payments.mark_refunded(pay["id"], 501, pay["days"])
    # платёж помечен, подписка погасла (paid_until срезан до "не ниже сейчас")
    assert payments.find_refundable_stars_payment(501) is None   # повторно вернуть нечего
    assert payments.is_paid(501) is False
    u = payments.get_user(501)
    now = datetime.now(timezone.utc)
    until = datetime.fromisoformat(u["paid_until"])
    assert abs((until - now).total_seconds()) < 120              # ровно "сейчас", не в минус


def test_refund_keeps_promo_tail(fresh_db):
    """Вычитаются только КУПЛЕННЫЕ дни: промо-хвост поверх платежа не сгорает."""
    from datetime import datetime, timezone

    from app.delivery import payments
    payments.grant_subscription(502, "stars", 10, "XTR", 5, "chg_x")   # 5 дней куплено
    payments.create_promo("TAIL", 10)
    assert payments.redeem_promo(502, "TAIL")["ok"] is True            # +10 дней промо
    pay = payments.find_refundable_stars_payment(502)
    payments.mark_refunded(pay["id"], 502, pay["days"])
    assert payments.is_paid(502) is True                               # промо-хвост жив
    u = payments.get_user(502)
    left = (datetime.fromisoformat(u["paid_until"])
            - datetime.now(timezone.utc)).total_seconds() / 86400
    assert 9.5 < left < 10.5                                           # ~10 промо-дней


def test_find_refundable_ignores_non_stars_and_unpaid(fresh_db):
    from app.delivery import payments
    payments.register_user(503)
    assert payments.find_refundable_stars_payment(503) is None
    payments.grant_subscription(503, "cryptobot", 20, "USDT", 30, "inv_1")
    assert payments.find_refundable_stars_payment(503) is None   # USDT не рефандится этим путём


def _age_trial(db_, tg_id, days_ago):
    from datetime import datetime, timedelta, timezone
    granted = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    db_.execute("UPDATE users SET trial_granted_at=? WHERE tg_id=?", (granted, tg_id))


def test_trial_drip_stages(fresh_db):
    """Дрип: стадии по возрасту триала, одна за такт, метки не откатываются."""
    from app.delivery import payments
    assert payments.grant_trial(601, 5) is True
    assert payments.due_for_trial_drip(5.0) == []           # день 0 — рано
    _age_trial(fresh_db, 601, 1.2)
    assert payments.due_for_trial_drip(5.0) == [(601, 1)]   # день 1
    payments.mark_trial_drip(601, 1)
    assert payments.due_for_trial_drip(5.0) == []           # стадия 1 уже была
    _age_trial(fresh_db, 601, 3.1)
    assert payments.due_for_trial_drip(5.0) == [(601, 2)]   # день 3
    payments.mark_trial_drip(601, 2)
    _age_trial(fresh_db, 601, 4.2)
    assert payments.due_for_trial_drip(5.0) == [(601, 3)]   # за сутки до конца (5-1)
    payments.mark_trial_drip(601, 3)
    assert payments.due_for_trial_drip(5.0) == []
    payments.mark_trial_drip(601, 1)                        # метка вниз не откатывается
    u = payments.get_user(601)
    assert u["trial_drip_stage"] == 3


def test_trial_drip_excludes_paid_and_stale(fresh_db):
    from app.delivery import payments
    # оплативший — исключён навсегда (продавать нечего)
    payments.grant_trial(602, 5)
    _age_trial(fresh_db, 602, 1.5)
    payments.grant_subscription(602, "stars", 1000, "XTR", 30, "chg_drip")
    assert payments.due_for_trial_drip(5.0) == []
    # протухший (триал кончился до включения фичи) — закрывается молча, не спамится
    payments.grant_trial(603, 5)
    _age_trial(fresh_db, 603, 9.0)
    assert payments.due_for_trial_drip(5.0) == []           # не в рассылке
    assert payments.expire_stale_trial_drips(5.0) == 1      # помечен задним числом
    assert payments.get_user(603)["trial_drip_stage"] == 3
    assert payments.expire_stale_trial_drips(5.0) == 0      # идемпотентно


def test_issue_winback_promo_redeemable(fresh_db):
    from app.delivery import payments
    payments.register_user(701, "lapsed")
    code = payments.issue_winback_promo(701, days=7, valid_days=5)
    assert code == "wb701"
    # промокод создан, одноразовый, с датой сгорания
    row = fresh_db.query_one("SELECT * FROM promo_codes WHERE code=?", (code,))
    assert row["days"] == 7 and row["max_uses"] == 1 and row["expires_at"] is not None
    # погашается и продлевает подписку
    res = payments.redeem_promo(701, code)
    assert res["ok"] is True and res["days"] == 7
    assert payments.is_paid(701) is True
    # повторно тем же юзером — уже применял
    assert payments.redeem_promo(701, code)["reason"] == "already_used"


def test_issue_winback_promo_refreshes_each_cycle(fresh_db):
    from app.delivery import payments
    payments.register_user(702, "repeat")
    code = payments.issue_winback_promo(702, 7, 5)
    payments.redeem_promo(702, code)                        # погасил в первый лапс
    # второй цикл win-back: код пересоздаётся начисто -> снова погашаем
    code2 = payments.issue_winback_promo(702, 7, 5)
    assert code2 == code                                    # тот же wb<tg_id>
    assert fresh_db.query_one(
        "SELECT COUNT(*) AS c FROM promo_redemptions WHERE code=?", (code,))["c"] == 0
    assert payments.redeem_promo(702, code2)["ok"] is True  # снова доступен


def test_referral_first_touch_and_guards(fresh_db):
    from app.delivery import payments
    payments.register_user(800, "referrer")
    payments.register_user(801, "invitee")
    # само-реферал и несуществующий реферер отклоняются
    assert payments.set_referrer(801, 801) is False
    assert payments.set_referrer(801, 999999) is False
    # нормальный случай
    assert payments.set_referrer(801, 800) is True
    assert payments.get_user(801)["invited_by"] == 800
    # first-touch: второй реферер не перезаписывает
    payments.register_user(802, "other")
    assert payments.set_referrer(801, 802) is False
    assert payments.get_user(801)["invited_by"] == 800


def test_referral_not_attributed_after_payment(fresh_db):
    from app.delivery import payments
    payments.register_user(810, "ref")
    payments.register_user(811, "already_paid")
    payments.grant_subscription(811, "stars", 1000, "XTR", 30, "chg_pre")
    # уже плативший не может быть ретро-атрибутирован рефереру (анти-накрутка)
    assert payments.set_referrer(811, 810) is False
    assert payments.get_user(811)["invited_by"] is None


def test_referral_reward_on_first_payment_only(fresh_db):
    from app.delivery import payments
    payments.register_user(820, "ref")
    payments.register_user(821, "invitee")
    payments.set_referrer(821, 820)
    ref_until_before = payments.get_user(820)["paid_until"]
    # первый реальный платёж приглашённого -> награда рефереру
    payments.grant_subscription(821, "stars", 1000, "XTR", 30, "chg_1")
    out = payments.reward_referrer_on_payment(821, 7, paid_days=30)
    assert out == (820, 7)
    assert payments.is_paid(820) is True
    assert payments.get_user(820)["paid_until"] != ref_until_before
    rewarded_until = payments.get_user(820)["paid_until"]
    # второй платёж (renewal) НЕ повторяет награду (идемпотентно)
    payments.grant_subscription(821, "stars", 1000, "XTR", 30, "chg_2")
    assert payments.reward_referrer_on_payment(821, 7, paid_days=30) is None
    assert payments.get_user(820)["paid_until"] == rewarded_until


def test_referral_reward_noop_without_referrer(fresh_db):
    from app.delivery import payments
    payments.register_user(830, "solo")
    payments.grant_subscription(830, "stars", 1000, "XTR", 30, "chg_s")
    assert payments.reward_referrer_on_payment(830, 7, paid_days=30) is None


def test_referral_stats(fresh_db):
    from app.delivery import payments
    payments.register_user(840, "ref")
    for uid in (841, 842, 843):
        payments.register_user(uid, f"inv{uid}")
        payments.set_referrer(uid, 840)
    payments.grant_subscription(841, "stars", 1000, "XTR", 30, "chg_a")
    payments.reward_referrer_on_payment(841, 7, paid_days=30)
    st = payments.referral_stats(840)
    assert st == {"invited": 3, "rewarded": 1}


def test_notify_mute_and_prefs(fresh_db):
    from app.delivery import payments
    payments.register_user(850, "u")
    assert payments.notify_prefs(850) == {"muted": False, "quiet_start": None, "quiet_end": None}
    payments.set_muted(850, True)
    assert payments.notify_prefs(850)["muted"] is True
    # мьют подавляет в любой час
    assert payments.is_signal_suppressed(850, 0) is True
    assert payments.is_signal_suppressed(850, 13) is True
    payments.set_muted(850, False)
    assert payments.is_signal_suppressed(850, 13) is False


def test_notify_quiet_hours_windows(fresh_db):
    from app.delivery import payments
    payments.register_user(851, "u")
    # обычное окно [22, 6) НЕ через полночь? тут через полночь: 22>6
    payments.set_quiet_hours(851, 22, 6)
    assert payments.is_signal_suppressed(851, 23) is True     # внутри (после 22)
    assert payments.is_signal_suppressed(851, 3) is True      # внутри (до 6)
    assert payments.is_signal_suppressed(851, 6) is False     # граница end исключена
    assert payments.is_signal_suppressed(851, 12) is False    # день — не подавляем
    # обычное окно без перехода через полночь [9, 12)
    payments.set_quiet_hours(851, 9, 12)
    assert payments.is_signal_suppressed(851, 10) is True
    assert payments.is_signal_suppressed(851, 12) is False
    assert payments.is_signal_suppressed(851, 8) is False
    # выключение
    payments.set_quiet_hours(851, None, None)
    assert payments.is_signal_suppressed(851, 10) is False


def test_is_signal_suppressed_unknown_user(fresh_db):
    from app.delivery import payments
    assert payments.is_signal_suppressed(999999, 3) is False   # незнакомый юзер — не подавляем


# --- Аудит-2: дыры в выручке и денежные гонки ---
def test_winback_requires_real_payment(fresh_db):
    """Аудит-2 [1]: триал/промо ставят paid_until, но win-back — только реально платившим,
    иначе каждый триальщик получал бы вечный цикл бесплатных промо."""
    from datetime import datetime, timedelta, timezone

    from app import db
    from app.delivery import payments
    payments.grant_trial(900, 5)                        # триал, ни одного платежа
    three_days = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    db.execute("UPDATE users SET paid_until=?, tier='free' WHERE tg_id=?", (three_days, 900))
    assert payments.due_for_winback(2) == []            # не платил -> не будим


def test_winback_not_rearmed_by_promo_redemption(fresh_db):
    """Аудит-2 [1]: погашение win-back-промокода НЕ перевзводит win-back — иначе
    «лапс -> промо -> погасил -> лапс -> снова промо» давало бы бесплатный доступ вечно."""
    from datetime import datetime, timedelta, timezone

    from app import db
    from app.delivery import payments
    payments.grant_subscription(901, "stars", 1000, "XTR", 30, "wb_real")  # реальный платёж
    three_days = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    db.execute("UPDATE users SET paid_until=?, tier='free' WHERE tg_id=?", (three_days, 901))
    assert [r["tg_id"] for r in payments.due_for_winback(2)] == [901]      # цикл 1: пора
    code = payments.issue_winback_promo(901, 7, 5)
    payments.mark_winback_reminded(901)
    assert payments.redeem_promo(901, code)["ok"] is True                  # погасил промо
    # промо НЕ сбросило метку -> после нового лапса win-back НЕ повторяется
    db.execute("UPDATE users SET paid_until=?, tier='free' WHERE tg_id=?", (three_days, 901))
    assert payments.due_for_winback(2) == []
    # а вот НОВЫЙ реальный платёж перевзводит цикл
    payments.grant_subscription(901, "stars", 1000, "XTR", 30, "wb_real2")
    db.execute("UPDATE users SET paid_until=?, tier='free' WHERE tg_id=?", (three_days, 901))
    assert [r["tg_id"] for r in payments.due_for_winback(2)] == [901]


def test_personal_promo_owner_binding(fresh_db):
    """Аудит-2 [4]: wb<tg_id> угадываем (id публичны) — погасить может только владелец."""
    from app.delivery import payments
    payments.register_user(910, "victim")
    payments.register_user(911, "attacker")
    code = payments.issue_winback_promo(910, 7, 5)
    res = payments.redeem_promo(911, code)              # чужой код
    assert res["ok"] is False and res["reason"] == "not_found"   # факт существования скрыт
    assert payments.redeem_promo(910, code)["ok"] is True        # владелец гасит нормально


def test_referral_reward_requires_qualifying_days(fresh_db):
    """Аудит-2 [2]: intro-платёж (часы за 10⭐) не квалифицирует реф-награду (анти-ферма);
    флаг rewarded при отказе не тратится — полноценный платёж позже награждает."""
    from app.delivery import payments
    payments.register_user(920, "ref")
    payments.register_user(921, "invitee")
    payments.set_referrer(921, 920)
    payments.grant_subscription(921, "stars", 10, "XTR", 0.21, "intro_chg")
    assert payments.reward_referrer_on_payment(921, 7, paid_days=0.21) is None
    assert payments.get_user(921)["referral_rewarded_at"] is None   # флаг не потрачен
    payments.grant_subscription(921, "stars", 1000, "XTR", 30, "month_chg")
    assert payments.reward_referrer_on_payment(921, 7, paid_days=30) == (920, 7)


def test_due_for_reminder_skips_hour_tariffs(fresh_db):
    """Аудит-2 [5]: intro (часы) не должен получать «истекает» через минуты после покупки."""
    from datetime import datetime, timedelta, timezone

    from app import db
    from app.delivery import payments
    payments.grant_subscription(930, "stars", 10, "XTR", 0.21, "intro5h")  # ~5 часов
    assert payments.due_for_reminder(3) == []           # осталось < 12ч -> молчим
    # а месячная подписка с истечением через 2 дня — напоминаем как раньше
    payments.grant_subscription(931, "stars", 1000, "XTR", 30, "m1")
    soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    db.execute("UPDATE users SET paid_until=? WHERE tg_id=?", (soon, 931))
    assert [r["tg_id"] for r in payments.due_for_reminder(3)] == [931]


def test_due_for_reminder_only_payers(fresh_db):
    """Аудит текстов 30.10: «истекает, продлите» с 8 тарифами уходило триалу (13 из 16
    напоминаний — не платившим). Напоминаем только тем, кто платил."""
    from datetime import datetime, timedelta, timezone

    from app import db
    from app.delivery import payments
    assert payments.grant_trial(940, 5) is True                        # триал без оплаты
    payments.create_promo("GIFT940", 5)
    assert payments.redeem_promo(941, "GIFT940")["ok"] is True         # промо без оплаты
    payments.grant_subscription(942, "stars", 500, "XTR", 30, "m942")  # платил
    assert payments.grant_trial(943, 5) is True                        # триал, потом оплатил
    payments.grant_subscription(943, "stars", 150, "XTR", 7, "w943")
    payments.register_user(944)
    payments.record_usdt_claim(944)                                    # USDT вручную + /grant
    payments.admin_grant(944, 30)
    soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    db.execute("UPDATE users SET paid_until=? WHERE tg_id IN (940, 941, 942, 943, 944)", (soon,))
    assert sorted(r["tg_id"] for r in payments.due_for_reminder(3)) == [942, 943, 944]


def test_free_quota_ignores_deliveries_during_access(fresh_db):
    """Аудит текстов 30.10: квоту free съедали сигналы триала — после него 5–6 дней шли только
    замки. Считаются только полные доставки после конца доступа."""
    from datetime import datetime, timedelta, timezone

    from app import db
    from app.delivery import payments
    now = datetime.now(timezone.utc)
    assert payments.grant_trial(950, 5) is True
    for sid in (1, 2, 3):
        payments.record_delivery(950, sid, "full")                     # пришли в триал
    db.execute("UPDATE deliveries SET created_at=? WHERE tg_id=?",
               ((now - timedelta(days=2)).isoformat(), 950))
    db.execute("UPDATE users SET paid_until=? WHERE tg_id=?",
               ((now - timedelta(hours=1)).isoformat(), 950))
    payments.expire_overdue()
    assert payments.free_full_last_week(950) == 0                     # триал квоту не съел
    assert dict(payments.plan_recipients(4, 2, (900,)))[950] == "full"
    payments.record_delivery(950, 4, "full")
    assert payments.free_full_last_week(950) == 1                     # бесплатные — считаются


def test_trial_summary(fresh_db):
    """К-24: итог триала — только полные доставки за доступ, закрытые — как в /stats;
    оплатившим и не-триалу — None (им обычное «подписка закончилась»)."""
    from datetime import datetime, timedelta, timezone

    from app import db
    from app.delivery import payments
    now = datetime.now(timezone.utc)

    def mk_signal(sid, status, r):
        db.execute(
            "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
            "confidence, snapshot_json, status, result_r, created_at) "
            "VALUES (?, 'BTC/USDT:USDT', 'LONG', '1h', 100, 90, 2.0, 0.7, '{}', ?, ?, ?)",
            (sid, status, r, f"2026-10-2{sid}T00:00:00+00:00"))

    for sid, status, r in ((1, "stopped", -1.0), (2, "tp1", 2.5), (3, "open", None),
                           (4, "expired", None), (5, "stopped", -1.0)):
        mk_signal(sid, status, r)
    assert payments.grant_trial(960, 5) is True
    for sid in (1, 2, 3, 4):
        payments.record_delivery(960, sid, "full")
    payments.record_delivery(960, 5, "teaser")                         # тизер — не в итоге
    db.execute("UPDATE users SET trial_granted_at=? WHERE tg_id=?",
               ((now - timedelta(days=5)).isoformat(), 960))
    assert payments.trial_summary(960) == {"signals": 4, "closed": 2, "total_r": 1.5}

    payments.register_user(961)
    assert payments.trial_summary(961) is None                         # триала не было
    assert payments.grant_trial(962, 5) is True
    payments.grant_subscription(962, "stars", 150, "XTR", 7, "w962")
    assert payments.trial_summary(962) is None                         # оплатил в триал
    assert payments.grant_trial(963, 5) is True
    payments.record_usdt_claim(963)
    payments.admin_grant(963, 30)
    assert payments.trial_summary(963) is None                         # USDT вручную + /grant


def test_expire_overdue_respects_concurrent_payment(fresh_db):
    """Аудит-2 [6]: понижение — условный UPDATE; юзер с будущим paid_until (оплатил между
    SELECT и UPDATE) не понижается."""
    from datetime import datetime, timedelta, timezone

    from app import db
    from app.delivery import payments
    payments.grant_subscription(940, "stars", 1000, "XTR", 30, "e1")
    future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    db.execute("UPDATE users SET paid_until=? WHERE tg_id=?", (future, 940))
    assert payments.expire_overdue() == []              # будущий paid_until -> не трогаем
    assert payments.get_user(940)["tier"] == "paid"


def test_register_user_keeps_username(fresh_db):
    """Аудит-2 [8]: служебный register_user(tg_id) без username не затирает известное имя."""
    from app.delivery import payments
    payments.register_user(950, "known_name")
    payments.register_user(950)                         # служебный вызов (grant/promo/mute)
    assert payments.get_user(950)["username"] == "known_name"


def test_admin_grant_supersedes_pending_cryptobot(fresh_db, monkeypatch):
    """Аудит-2 [3]: /grant гасит pending USDT-инвойсы — авто-поллер не начислит вторично."""
    from app import runtime
    from app.delivery import payments
    runtime.set_value("cryptobot_token", "tok")

    def fake_call(method, params, post=True):
        if method == "createInvoice":
            return {"invoice_id": 970, "bot_invoice_url": "https://t.me/x"}
        return {"items": [{"invoice_id": 970, "status": "paid"}]}   # оплачен ПОСЛЕ гранта

    monkeypatch.setattr(payments, "_cryptobot_call", fake_call)
    payments.create_cryptobot_invoice(960, 30, amount=20)
    payments.admin_grant(960, 30)                       # админ подтвердил вручную
    first_until = payments.get_user(960)["paid_until"]
    assert payments.poll_cryptobot() == []              # инвойс погашен -> двойного нет
    assert payments.get_user(960)["paid_until"] == first_until


def test_poll_cryptobot_marks_provider_expired(fresh_db, monkeypatch):
    """Аудит-2 [7]: истёкший у провайдера инвойс закрывается локально (не вечный pending)."""
    from app import runtime
    from app.delivery import payments
    runtime.set_value("cryptobot_token", "tok")

    def fake_call(method, params, post=True):
        if method == "createInvoice":
            return {"invoice_id": 980, "bot_invoice_url": "https://t.me/x"}
        return {"items": [{"invoice_id": 980, "status": "expired"}]}

    monkeypatch.setattr(payments, "_cryptobot_call", fake_call)
    payments.create_cryptobot_invoice(981, 30, amount=20)
    assert payments.poll_cryptobot() == []
    row = fresh_db.query_one("SELECT status FROM payments WHERE invoice_id='980'")
    assert row["status"] == "expired"
    assert payments.poll_cryptobot() == []              # pending больше нет -> не опрашиваем


def test_mark_refunded_inside_transaction(fresh_db):
    """Аудит-2 [9]: вычет дней читает paid_until под локом; платёж помечается refunded."""
    from app.delivery import payments
    payments.grant_subscription(990, "stars", 1000, "XTR", 30, "r1")
    pay = payments.find_refundable_stars_payment(990)
    assert pay is not None
    payments.mark_refunded(pay["id"], 990, pay["days"])
    assert payments.find_refundable_stars_payment(990) is None     # больше нечего возвращать
    # paid_until срезан к «сейчас» (не в минус)
    from datetime import datetime, timezone
    until = datetime.fromisoformat(payments.get_user(990)["paid_until"])
    assert abs((until - datetime.now(timezone.utc)).total_seconds()) < 120


def test_depo_prefs_roundtrip(fresh_db):
    from app.delivery import payments
    assert payments.depo_prefs(111) is None              # неизвестный юзер
    payments.set_depo(111, 500.0, 1.5)
    assert payments.depo_prefs(111) == (500.0, 1.5)
    payments.set_depo(111, None, None)                   # /depo off
    assert payments.depo_prefs(111) is None


def test_missed_teaser_r_counts_only_teasers_closed_in_window(fresh_db):
    """«Что ты упустил»: только тизерные доставки юзера × закрытия в окне недели;
    полные доставки и чужие сигналы не считаются; знак минусовой недели не прячется."""
    from app import db
    from app.delivery import payments
    payments.register_user(50)

    def mk_signal(sid, r, closed):
        db.execute(
            "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
            "confidence, snapshot_json, status, result_r, created_at, closed_at) "
            "VALUES (?, 'BTC/USDT:USDT', 'LONG', '1h', 100, 90, 2.0, 0.7, '{}', "
            "'stopped', ?, ?, ?)",
            (sid, r, f"2026-08-01T0{sid}:00:00+00:00", closed))   # uniq created_at на бар

    mk_signal(1, 2.26, "2026-08-05T10:00:00+00:00")   # тизер, закрыт в окне -> считается
    mk_signal(2, -1.0, "2026-08-06T10:00:00+00:00")   # тизер, в окне -> минус честно
    mk_signal(3, 3.0, "2026-08-20T10:00:00+00:00")    # тизер, закрыт ПОСЛЕ окна -> нет
    mk_signal(4, 5.0, "2026-08-05T12:00:00+00:00")    # доставлен ПОЛНЫМ -> нет
    payments.record_delivery(50, 1, "teaser")
    payments.record_delivery(50, 2, "teaser")
    payments.record_delivery(50, 3, "teaser")
    payments.record_delivery(50, 4, "full")
    res = payments.missed_teaser_r(50, "2026-08-03T00:00:00+00:00",
                                   "2026-08-10T00:00:00+00:00")
    assert res == {"n": 2, "total_r": 1.26}
    assert payments.missed_teaser_r(999, "2026-08-03T00:00:00+00:00",
                                    "2026-08-10T00:00:00+00:00") == {"n": 0, "total_r": 0.0}


def test_free_active_ids_includes_lapsed_paid(fresh_db):
    from app import db
    from app.delivery import payments
    payments.register_user(60)                                    # free
    payments.grant_subscription(61, "stars", 1, "XTR", 30, "c6")  # активный paid -> НЕ в списке
    payments.grant_subscription(62, "stars", 1, "XTR", 30, "c7")  # истёкший paid -> в списке
    db.execute("UPDATE users SET paid_until='2020-01-01T00:00:00+00:00' WHERE tg_id=62")
    ids = set(payments.free_active_ids())
    assert 60 in ids and 62 in ids and 61 not in ids


def _closed_signal(sym: str, side: str, r: float, closed: str) -> int:
    from app import db
    db.execute(
        "INSERT INTO signals(symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, created_at, filled_at, closed_at, result_r) "
        "VALUES (?,?, '1h', 1, 0.9, 2.0, 0.7, '{}', 'stopped', "
        "'2026-08-01T00:00:00+00:00', '2026-08-01T01:00:00+00:00', ?, ?)",
        (sym, side, closed, r))
    return db.query_one("SELECT MAX(id) AS i FROM signals")["i"]


def _mark(tg_id: int, sid: int) -> None:
    from app import db
    db.execute("INSERT OR IGNORE INTO user_taken(tg_id, signal_id, created_at) "
               "VALUES (?,?, '2026-08-12T00:00:00+00:00')", (tg_id, sid))


def test_weekly_discipline_math(fresh_db):
    """Коуч: взятое vs пропущенное за неделю; None — если отметками не пользуется."""
    from app.delivery import payments

    payments.register_user(700100, "coachee")
    a = _closed_signal("AAA/USDT:USDT", "LONG", 2.0, "2026-08-10T10:00:00+00:00")
    _closed_signal("BBB/USDT:USDT", "SHORT", -1.0, "2026-08-11T10:00:00+00:00")
    _closed_signal("CCC/USDT:USDT", "SHORT", 3.0, "2026-08-12T10:00:00+00:00")
    # не пользуется отметками -> None (нет блока, а не «пропустил всё»)
    assert payments.weekly_discipline(
        700100, "2026-08-10T00:00:00+00:00", "2026-08-17T00:00:00+00:00") is None
    _mark(700100, a)
    c = payments.weekly_discipline(
        700100, "2026-08-10T00:00:00+00:00", "2026-08-17T00:00:00+00:00")
    assert c["closed_n"] == 3 and c["closed_r"] == 4.0
    assert c["taken_n"] == 1 and c["taken_r"] == 2.0
    assert c["skipped_n"] == 2 and c["skipped_r"] == 2.0
    assert c["skipped_short"] == 2 and c["skipped_long"] == 0
    # закрытие ВНЕ окна не считается
    c2 = payments.weekly_discipline(
        700100, "2026-08-10T00:00:00+00:00", "2026-08-11T00:00:00+00:00")
    assert c2["closed_n"] == 1 and c2["taken_n"] == 1


def test_taken_stats_and_discipline_render(fresh_db):
    from app.delivery import payments, templates

    payments.register_user(700200, "renderer")
    a = _closed_signal("AAA/USDT:USDT", "LONG", 1.9, "2026-08-10T10:00:00+00:00")
    b = _closed_signal("BBB/USDT:USDT", "LONG", -1.0, "2026-08-11T10:00:00+00:00")
    _mark(700200, a)
    _mark(700200, b)
    ms = payments.taken_stats(700200)
    assert ms["trades"] == 2 and ms["total_r"] == 0.9 and ms["winrate"] == 0.5
    txt = templates.render_discipline_block({
        "closed_n": 5, "closed_r": 3.2, "taken_n": 2, "taken_r": 0.9,
        "skipped_n": 3, "skipped_r": 2.3, "skipped_short": 3, "skipped_long": 0})
    assert "Твоя дисциплина" in txt and "+3.2R" in txt and "+0.9R" in txt
    assert "Мимо прошло 3" in txt
    assert "пропускаешь шорты" not in txt          # выборка мала (3 < 4) — паттерн молчит
    txt2 = templates.render_discipline_block({
        "closed_n": 6, "closed_r": 1.0, "taken_n": 1, "taken_r": 0.0,
        "skipped_n": 5, "skipped_r": 1.0, "skipped_short": 4, "skipped_long": 1})
    assert "пропускаешь шорты: 4 из 5" in txt2
    assert templates.render_discipline_block(
        {"closed_n": 0, "closed_r": 0, "taken_n": 0, "taken_r": 0,
         "skipped_n": 0, "skipped_r": 0, "skipped_short": 0, "skipped_long": 0}) == ""


def test_subscriber_breakdown_separates_trial_grant_paying(fresh_db):
    """«Платных» = есть оплаченный платёж. Триал ставит тот же tier='paid' и раньше
    считался платным (07.09: 7 «платных» при нуле платежей). Грант — tier=paid без
    платежей и без триала. Истёкшие — не считаются нигде."""
    from app import db
    from app.delivery import payments
    for uid in (1, 2, 3, 4):
        payments.register_user(uid)
    # 1 — реальный платёж
    payments.grant_subscription(1, "stars", 1500, "XTR", 30, "inv-1")
    # 2 — триал (tier=paid, платежей нет)
    assert payments.grant_trial(2, 5.0) is True
    # 3 — ручной грант админом
    payments.admin_grant(3, 30)
    # 4 — истёкший триал
    assert payments.grant_trial(4, 5.0) is True
    db.execute("UPDATE users SET paid_until='2020-01-01T00:00:00+00:00' WHERE tg_id=4")
    bd = payments.subscriber_breakdown()
    assert bd == {"paying": 1, "granted": 1, "trial": 1}
    uc = payments.user_counts()
    assert uc["paying"] == 1 and uc["trial"] == 1 and uc["granted"] == 1
    assert uc["paid"] == 3                       # доставка сигналов: все активные


def test_unlock_delivery_allowed_by_schema(fresh_db):
    """Аудит 05.10: CHECK из 002 не пускал kind='unlock' — купивший сигнал не получал
    текст. Миграция 023: тизер -> unlock проходит, кнопки открываются, «упущенное» чистое."""
    from app.delivery import payments
    payments.register_user(1001, "u")
    assert payments.record_delivery(1001, 5, "teaser") is True
    assert payments.mark_delivery_unlocked(1001, 5) is True
    assert payments.delivered_full(1001, 5) is True
    # купивший из канала (тизерной записи нет) — запись 'unlock' сразу
    assert payments.mark_delivery_unlocked(1001, 6) is False
    assert payments.record_delivery(1001, 6, "unlock") is True
    assert payments.delivered_full(1001, 6) is True


def test_failed_write_does_not_hold_transaction(fresh_db):
    """Аудит 05.10: упавший execute (IntegrityError) оставлял неявную транзакцию открытой —
    соединение держало write-lock. Теперь откат."""
    import sqlite3

    import pytest

    from app import db
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO deliveries(tg_id, signal_id, kind, created_at) "
                   "VALUES (1, 1, 'bogus', 'x')")
    assert db.get_conn().in_transaction is False


def test_admin_grant_keeps_other_tier_invoice(fresh_db, monkeypatch):
    """Аудит 05.10: /grant 30 гасил ВСЕ pending-инвойсы — оплаченный, но не опрошенный
    lifetime терялся. Теперь гасится только инвойс того же тира, прочие поллер зачтёт."""
    from app import runtime
    from app.delivery import payments
    runtime.set_value("cryptobot_token", "tok")

    def fake_call(method, params, post=True):
        if method == "createInvoice":
            return {"invoice_id": 980, "bot_invoice_url": "https://t.me/x"}
        return {"items": [{"invoice_id": 980, "status": "paid"}]}

    monkeypatch.setattr(payments, "_cryptobot_call", fake_call)
    payments.create_cryptobot_invoice(961, 36500, amount=300)
    payments.admin_grant(961, 30)                       # подарок/жест — другой тир
    activated = payments.poll_cryptobot()
    assert [a[0] for a in activated] == [961]           # lifetime зачтён поллером
    from datetime import datetime, timezone
    until = datetime.fromisoformat(payments.get_user(961)["paid_until"])
    assert (until - datetime.now(timezone.utc)).days > 36000


def test_intro_payment_does_not_open_winback(fresh_db):
    """Аудит 05.10: intro 10⭐/5ч перевзводил win-back (+7 дн) — бесконечный цикл почти
    бесплатного доступа. Win-back — только после квалифицирующей оплаты (>= 7 дн)."""
    from datetime import datetime, timedelta, timezone

    from app import db
    from app.delivery import payments
    payments.grant_subscription(902, "stars", 10, "XTR", 5 / 24, "chg_intro")
    three_days = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    db.execute("UPDATE users SET paid_until=?, tier='free' WHERE tg_id=?", (three_days, 902))
    assert payments.due_for_winback(2) == []
    # ранее платил месяц и получил win-back; новый intro НЕ перевзводит напоминание
    payments.grant_subscription(903, "stars", 1000, "XTR", 30, "chg_m")
    db.execute("UPDATE users SET winback_reminded_at='2026-01-01T00:00:00+00:00' "
               "WHERE tg_id=903")
    payments.grant_subscription(903, "stars", 10, "XTR", 5 / 24, "chg_i2")
    assert payments.get_user(903)["winback_reminded_at"] is not None


def test_mark_refunded_idempotent_and_reverses_referral(fresh_db):
    """Аудит 05.10: повторный /refund вычитал дни дважды; возврат не откатывал награду
    реферера (ферма «оплатил -> money-back»)."""
    from datetime import datetime

    from app.delivery import payments
    payments.register_user(830, "ref")
    payments.register_user(831, "invitee")
    payments.set_referrer(831, 830)
    payments.grant_subscription(831, "stars", 2500, "XTR", 90, "chg_q")
    payments.grant_subscription(831, "stars", 1000, "XTR", 30, "chg_m")
    assert payments.reward_referrer_on_payment(831, 7, paid_days=30) == (830, 7)
    ref_until = datetime.fromisoformat(payments.get_user(830)["paid_until"])
    pay = payments.find_refundable_stars_payment(831)       # последний — 30 дн
    until0 = datetime.fromisoformat(payments.get_user(831)["paid_until"])
    assert payments.mark_refunded(pay["id"], 831, pay["days"], 7) is True
    until1 = datetime.fromisoformat(payments.get_user(831)["paid_until"])
    assert round((until0 - until1).total_seconds() / 86400) == 30
    # повтор — no-op, дни второй раз не снимаются
    assert payments.mark_refunded(pay["id"], 831, pay["days"], 7) is False
    assert datetime.fromisoformat(payments.get_user(831)["paid_until"]) == until1
    # остался квалифицирующий 90-дн платёж -> награда реферера сохраняется
    assert datetime.fromisoformat(payments.get_user(830)["paid_until"]) == ref_until
    # вернули и его -> награда откатывается
    pay2 = payments.find_refundable_stars_payment(831)
    assert payments.mark_refunded(pay2["id"], 831, pay2["days"], 7) is True
    ref_after = datetime.fromisoformat(payments.get_user(830)["paid_until"])
    assert ref_after < ref_until
    assert payments.get_user(831)["referral_rewarded_at"] is None


def test_users_by_source_counts_who_stayed(fresh_db):
    """Качество трафика (/growth): сколько пришедших НЕ заблокировали бота."""
    from app.delivery import payments
    for uid in (740, 741, 742):
        payments.register_user(uid)
        payments.set_acquisition_source(uid, "gramads_a")
    payments.register_user(743)                           # органика
    payments.mark_blocked(741)                            # заблокировал бота
    q = payments.users_by_source()
    assert sorted(q["gramads_a"]["ids"]) == [740, 741, 742]
    assert q["gramads_a"]["active"] == 2
    assert q["—"]["ids"] == [743] and q["—"]["active"] == 1
