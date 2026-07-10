"""
payments.py — подписка через Telegram Stars (XTR).

Stars нативны для Telegram: не нужен внешний провайдер/вебхук. Поток:
  /subscribe -> кнопка -> bot.send_invoice(currency='XTR') -> pre_checkout(ok)
  -> successful_payment -> grant_subscription (идемпотентно по charge_id).

Регистрация юзеров и учёт доступа — здесь же (users/payments таблицы).
USDT через CryptoBot — задел на будущее (создание инвойса ниже, включается при CRYPTOBOT_TOKEN).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from app import clock, db


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def register_user(tg_id: int, username: str | None = None, lang: str = "ru") -> None:
    """Завести юзера (idempotent) или обновить username при повторном /start."""
    now = clock.utcnow_iso()
    db.execute(
        "INSERT INTO users(tg_id, username, lang, tier, joined_at, updated_at) "
        "VALUES(?,?,?, 'free', ?, ?) "
        "ON CONFLICT(tg_id) DO UPDATE SET "
        "username=excluded.username, updated_at=excluded.updated_at",
        (tg_id, username, lang, now, now))


def get_user(tg_id: int):
    return db.query_one("SELECT * FROM users WHERE tg_id=?", (tg_id,))


def is_paid(tg_id: int) -> bool:
    """Активна ли подписка (paid_until в будущем). ISO-UTC сравниваются лексикографически."""
    u = get_user(tg_id)
    return bool(u and u["paid_until"] and u["paid_until"] > clock.utcnow_iso())


def active_subscriber_ids() -> list[int]:
    """tg_id всех активных платных подписчиков (для рассылки сигналов)."""
    now = clock.utcnow_iso()
    return [r["tg_id"] for r in db.query(
        "SELECT tg_id FROM users WHERE tier='paid' AND paid_until > ? AND is_active=1", (now,))]


def grant_subscription(tg_id: int, method: str, amount: float, currency: str,
                       days: float, invoice_id: str) -> bool:
    """Идемпотентно выдать/продлить подписку. -> True если применено, False если дубль платежа.

    paid_until продлевается от максимума (сейчас, текущий paid_until) на days.
    Дедуп по UNIQUE(method, invoice_id) — повторный вебхук/апдейт не начисляет дважды.
    """
    if db.query_one("SELECT 1 FROM payments WHERE method=? AND invoice_id=? AND status='paid'",
                    (method, invoice_id)):
        return False
    register_user(tg_id)
    now = clock.utcnow_iso()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO payments(tg_id, method, amount, currency, days, status, invoice_id, "
            "telegram_payment_charge_id, created_at, paid_at) "
            "VALUES(?,?,?,?,?, 'paid', ?, ?, ?, ?)",
            (tg_id, method, amount, currency, days, invoice_id, invoice_id, now, now))
        _extend_and_activate(conn, tg_id, days)
    return True


def find_refundable_stars_payment(tg_id: int) -> dict | None:
    """Последний оплаченный Stars-платёж юзера с charge_id — кандидат на money-back.

    Только читает: сам возврат делает caller через Bot API refundStarPayment,
    затем фиксирует mark_refunded. Порядок сознательный (API -> БД): если API
    откажет, в БД ничего менять не нужно; обратный порядок требовал бы отката."""
    row = db.query_one(
        "SELECT id, telegram_payment_charge_id AS charge_id, amount, days "
        "FROM payments WHERE tg_id=? AND method='stars' AND status='paid' "
        "AND telegram_payment_charge_id IS NOT NULL AND telegram_payment_charge_id != '' "
        "ORDER BY paid_at DESC LIMIT 1", (tg_id,))
    return dict(row) if row else None


def mark_refunded(payment_id: int, tg_id: int, days: float) -> None:
    """После успешного refundStarPayment: пометить платёж refunded и вычесть купленные
    дни из paid_until (не ниже текущего момента — промо/триал-хвосты не сгорают в минус,
    а сама подписка от возвращённого платежа гаснет)."""
    u = get_user(tg_id)
    new_until = None
    if u and u["paid_until"]:
        cur = datetime.fromisoformat(u["paid_until"])
        new_until = max(_now_dt(), cur - timedelta(days=days)).isoformat()
    with db.transaction() as conn:
        conn.execute("UPDATE payments SET status='refunded' WHERE id=?", (payment_id,))
        if new_until is not None:
            conn.execute("UPDATE users SET paid_until=?, updated_at=? WHERE tg_id=?",
                         (new_until, clock.utcnow_iso(), tg_id))


def grant_trial(tg_id: int, days: float) -> bool:
    """Триал: короткий полный доступ для СОВСЕМ новых юзеров, один раз. НЕ платёж — не пишет
    в payments, не награждает реферала (задача 22) просто тем, что не вызывает эту логику.

    Условие — trial_granted_at IS NULL И paid_until IS NULL (никогда не было НИКАКОЙ
    подписки, включая истёкшую — иначе лапсед-юзер получал бы триал заново при каждом
    возврате). Атомарная условная UPDATE + rowcount — тот же TOCTOU-safe паттерн, что
    journal._resolve_candidate: конкурентные /start не смогут оба «выиграть».
    -> True если применено."""
    register_user(tg_id)
    now = clock.utcnow_iso()
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE users SET trial_granted_at=? WHERE tg_id=? "
            "AND trial_granted_at IS NULL AND paid_until IS NULL",
            (now, tg_id))
        if cur.rowcount != 1:
            return False
        _extend_and_activate(conn, tg_id, days)
    return True


def create_promo(code: str, days: int, max_uses: int | None = None,
                 expires_at: str | None = None) -> None:
    """Создать промокод на +days подписки. code сохраняется как есть (регистр — на совести
    вызывающего кода, /addpromo); max_uses=None — без лимита, expires_at=None — бессрочный."""
    db.execute(
        "INSERT INTO promo_codes(code, days, max_uses, used_count, expires_at, created_at) "
        "VALUES(?,?,?,0,?,?)",
        (code, days, max_uses, expires_at, clock.utcnow_iso()))


def redeem_promo(tg_id: int, code: str) -> dict:
    """Погасить промокод. -> {"ok": bool, "days": int|None, "reason": str|None}; reason при
    ok=False: not_found|expired|exhausted|already_used. НЕ платёж — не пишет в payments, не
    награждает реферала (задача 22), как и триал. Дедуп через UNIQUE(code, tg_id) в
    promo_redemptions — конкурентные попытки применить дважды не смогут обе выиграть."""
    row = db.query_one("SELECT * FROM promo_codes WHERE code=?", (code,))
    if row is None:
        return {"ok": False, "days": None, "reason": "not_found"}
    now = clock.utcnow_iso()
    if row["expires_at"] and row["expires_at"] < now:
        return {"ok": False, "days": None, "reason": "expired"}
    # already_used ПЕРЕД exhausted: иначе при низком max_uses (напр. 1) повторная попытка
    # ТОГО ЖЕ юзера маскируется под "код исчерпан" вместо честного "вы уже применяли" —
    # найдено живой проверкой, не юнит-тестами (max_uses=None в тестах не бил по этой ветке)
    if db.query_one("SELECT 1 FROM promo_redemptions WHERE code=? AND tg_id=?", (code, tg_id)):
        return {"ok": False, "days": None, "reason": "already_used"}
    if row["max_uses"] is not None and row["used_count"] >= row["max_uses"]:
        return {"ok": False, "days": None, "reason": "exhausted"}
    register_user(tg_id)
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO promo_redemptions(code, tg_id, redeemed_at) VALUES(?,?,?)",
                (code, tg_id, now))
            conn.execute(
                "UPDATE promo_codes SET used_count = used_count + 1 WHERE code=?", (code,))
            _extend_and_activate(conn, tg_id, row["days"])
    except sqlite3.IntegrityError:
        return {"ok": False, "days": None, "reason": "already_used"}
    return {"ok": True, "days": row["days"], "reason": None}


def _extend_and_activate(conn, tg_id: int, days: float) -> str:
    """Продлить подписку от max(сейчас, текущий paid_until) на days; tier=paid.

    -> new paid_until."""
    now = clock.utcnow_iso()
    u = get_user(tg_id)
    base = u["paid_until"] if (u and u["paid_until"] and u["paid_until"] > now) else now
    new_until = (datetime.fromisoformat(base) + timedelta(days=days)).isoformat()
    conn.execute("UPDATE users SET tier='paid', paid_until=?, renew_reminded_at=NULL, "
                 "winback_reminded_at=NULL, updated_at=? WHERE tg_id=?",
                 (new_until, now, tg_id))
    return new_until


# ---------------------------------------------------------------------------
# Воронка доставок (free: N полных сигналов/нед, дальше — тизер)
# ---------------------------------------------------------------------------
def all_user_ids() -> list[int]:
    return [r["tg_id"] for r in db.query("SELECT tg_id FROM users WHERE is_active=1")]


def delivered(tg_id: int, signal_id: int) -> bool:
    return db.query_one("SELECT 1 FROM deliveries WHERE tg_id=? AND signal_id=?",
                        (tg_id, signal_id)) is not None


def record_delivery(tg_id: int, signal_id: int, kind: str) -> bool:
    """Отметить доставку (idempotent через UNIQUE). -> False если уже была."""
    try:
        db.execute("INSERT INTO deliveries(tg_id, signal_id, kind, created_at) VALUES(?,?,?,?)",
                   (tg_id, signal_id, kind, clock.utcnow_iso()))
        return True
    except Exception:                               # noqa: BLE001 — IntegrityError = уже доставлено
        return False


def free_full_last_week(tg_id: int) -> int:
    since = (_now_dt() - timedelta(days=7)).isoformat()
    return db.query_one(
        "SELECT COUNT(*) AS c FROM deliveries WHERE tg_id=? AND kind='full' AND created_at >= ?",
        (tg_id, since))["c"]  # type: ignore[index]


def plan_recipients(signal_id: int, free_limit: int, admin_ids) -> list:
    """Кому и в каком виде слать сигнал: -> [(tg_id, 'full'|'teaser')].

    Админы и платные -> 'full'. Free -> 'full', пока не исчерпан недельный лимит, иначе 'teaser'.
    Уже доставленные пропускаются.
    """
    admin_set = set(admin_ids)
    ids = list(dict.fromkeys([*admin_ids, *all_user_ids()]))
    out = []
    for uid in ids:
        if delivered(uid, signal_id):
            continue
        if uid in admin_set or is_paid(uid):
            out.append((uid, "full"))
        elif free_full_last_week(uid) < free_limit:
            out.append((uid, "full"))
        else:
            out.append((uid, "teaser"))
    return out


# ---------------------------------------------------------------------------
# Жизненный цикл подписки: истечение и напоминания
# ---------------------------------------------------------------------------
def expire_overdue() -> list[int]:
    """Понизить истёкших paid -> free. -> tg_id понижённых (для уведомления)."""
    now = clock.utcnow_iso()
    ids = [r["tg_id"] for r in db.query(
        "SELECT tg_id FROM users WHERE tier='paid' AND (paid_until IS NULL OR paid_until <= ?)",
        (now,))]
    for tg_id in ids:
        db.execute("UPDATE users SET tier='free', updated_at=? WHERE tg_id=?", (now, tg_id))
    return ids


def due_for_reminder(days: int = 3) -> list:
    """Платные, у кого подписка истекает в ближайшие `days` и кому ещё не напоминали."""
    now = clock.utcnow_iso()
    horizon = (_now_dt() + timedelta(days=days)).isoformat()
    return db.query(
        "SELECT tg_id, paid_until FROM users WHERE tier='paid' AND paid_until > ? "
        "AND paid_until <= ? AND renew_reminded_at IS NULL", (now, horizon))


def mark_reminded(tg_id: int) -> None:
    db.execute("UPDATE users SET renew_reminded_at=? WHERE tg_id=?",
               (clock.utcnow_iso(), tg_id))


# ---------------------------------------------------------------------------
# USDT через CryptoBot (@CryptoBot). Инертен, пока не задан CRYPTOBOT_TOKEN.
# ---------------------------------------------------------------------------
def cryptobot_enabled() -> bool:
    from app import runtime
    return bool(runtime.cryptobot_token())


def _cryptobot_call(method: str, params: dict, post: bool = True):
    """Вызов CryptoPay API. -> result. Вынесено отдельно для мока в тестах."""
    import json
    import urllib.parse
    import urllib.request

    from app import runtime

    token = runtime.cryptobot_token()
    base = f"https://pay.crypt.bot/api/{method}"
    headers = {"Crypto-Pay-API-Token": token}
    if post:
        req = urllib.request.Request(base, data=urllib.parse.urlencode(params).encode(),
                                     headers=headers)
    else:
        req = urllib.request.Request(base + "?" + urllib.parse.urlencode(params), headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:      # noqa: S310 — фикс. хост CryptoBot
        body = json.loads(r.read())
    if not body.get("ok"):
        raise RuntimeError(f"cryptobot {method}: {body}")
    return body["result"]


def create_cryptobot_invoice(tg_id: int, days: int, amount: float | None = None):
    """Создать USDT-инвойс, записать pending-платёж. -> (pay_url, invoice_id) | None."""
    from app import runtime

    if not runtime.cryptobot_token():
        return None
    amount = amount if amount is not None else runtime.price_usdt()
    register_user(tg_id)
    res = _cryptobot_call("createInvoice", {
        "asset": "USDT", "amount": str(amount),
        "description": f"Подписка RSI Range Signals на {days} дней",
        "payload": f"{tg_id}:{days}",
    })
    inv_id = str(res["invoice_id"])
    pay_url = res.get("bot_invoice_url") or res.get("pay_url") or res.get("mini_app_invoice_url")
    db.execute(
        "INSERT INTO payments(tg_id, method, amount, currency, days, status, "
        "invoice_id, created_at) VALUES(?,?,?,?,?, 'pending', ?, ?)",
        (tg_id, "cryptobot", float(amount), "USDT", days, inv_id, clock.utcnow_iso()))
    return pay_url, inv_id


def poll_cryptobot() -> list[int]:
    """Проверить оплату pending USDT-инвойсов, активировать подписку. -> tg_id активированных.

    К оплаченному инвойсу добавляются бонус-дни (runtime.usdt_bonus_days) — USDT дешевле
    мерчанту, чем Stars (~38% эфф. комиссии), перенаправляем бонусом, не наценкой.
    """
    from app import runtime

    if not runtime.cryptobot_token():
        return []
    pending = db.query("SELECT * FROM payments WHERE method='cryptobot' AND status='pending'")
    if not pending:
        return []
    res = _cryptobot_call("getInvoices",
                          {"invoice_ids": ",".join(p["invoice_id"] for p in pending)}, post=False)
    items = res.get("items", res) if isinstance(res, dict) else res
    paid = {str(it["invoice_id"]) for it in items if it.get("status") == "paid"}
    activated = []
    for p in pending:
        if p["invoice_id"] not in paid:
            continue
        now = clock.utcnow_iso()
        with db.transaction() as conn:
            cur = conn.execute("UPDATE payments SET status='paid', paid_at=? "
                               "WHERE id=? AND status='pending'", (now, p["id"]))
            if cur.rowcount != 1:                       # уже обработан параллельно
                continue
            _extend_and_activate(conn, p["tg_id"], p["days"] + runtime.usdt_bonus_days())
        activated.append(p["tg_id"])
    return activated


def admin_grant(tg_id: int, days: int) -> bool:
    """Ручная выдача подписки админом (без записи в payments — не оплата).

    /grant используется как подтверждение реальной USDT-оплаты по потоку «Я оплатил» —
    закрывает все открытые заявки юзера. Для чистых подарков (giveaway) завести
    отдельную команду, не закрывающую заявки.
    """
    register_user(tg_id)
    with db.transaction() as conn:
        # атомарно с выдачей: при падении между операциями не получим
        # «заявки закрыты, подписка не выдана» (замолкшая SLA-эскалация)
        _extend_and_activate(conn, tg_id, days)
        conn.execute("UPDATE usdt_claims SET resolved_at=? WHERE tg_id=? AND resolved_at IS NULL",
                     (clock.utcnow_iso(), tg_id))
    return True


# ---------------------------------------------------------------------------
# USDT-заявки «Я оплатил»: SLA + эскалация зависших; win-back после истечения
# ---------------------------------------------------------------------------
def record_usdt_claim(tg_id: int) -> int:
    """Зафиксировать заявку «Я оплатил USDT». -> claim id.

    Дедуп: если у юзера уже есть открытая заявка (повторные клики по кнопке),
    возвращаем её id, не создавая дубль и не порождая дубль-пинги админам.
    """
    existing = db.query_one(
        "SELECT id FROM usdt_claims WHERE tg_id=? AND resolved_at IS NULL "
        "ORDER BY id DESC LIMIT 1", (tg_id,))
    if existing:
        return existing["id"]
    return db.execute(
        "INSERT INTO usdt_claims(tg_id, created_at) VALUES(?,?)",
        (tg_id, clock.utcnow_iso()))


def resolve_usdt_claims(tg_id: int) -> None:
    """Закрыть ВСЕ открытые заявки юзера (вызывается из admin_grant)."""
    db.execute("UPDATE usdt_claims SET resolved_at=? WHERE tg_id=? AND resolved_at IS NULL",
               (clock.utcnow_iso(), tg_id))


def stale_usdt_claims(hours: int = 6) -> list:
    """Открытые неэскалированные заявки старше `hours` — для пинга админа (один раз)."""
    cutoff = (_now_dt() - timedelta(hours=hours)).isoformat()
    return db.query(
        "SELECT * FROM usdt_claims WHERE resolved_at IS NULL AND escalated_at IS NULL "
        "AND created_at <= ?", (cutoff,))


def mark_claim_escalated(claim_id: int) -> None:
    db.execute("UPDATE usdt_claims SET escalated_at=? WHERE id=?",
               (clock.utcnow_iso(), claim_id))


def due_for_winback(days_after: int = 2, window_days: int = 3) -> list:
    """Юзеры для win-back: подписка истекла НЕ МЕНЕЕ `days_after` дней назад (отложенное
    касание, не дубль уведомления «истекла» в том же такте), но не давнее
    `days_after + window_days` (давно ушедших не будим). Одно напоминание на цикл."""
    ceil = (_now_dt() - timedelta(days=days_after)).isoformat()
    floor = (_now_dt() - timedelta(days=days_after + window_days)).isoformat()
    return db.query(
        "SELECT tg_id, paid_until FROM users WHERE tier='free' AND paid_until IS NOT NULL "
        "AND paid_until <= ? AND paid_until >= ? AND winback_reminded_at IS NULL",
        (ceil, floor))


def mark_winback_reminded(tg_id: int) -> None:
    db.execute("UPDATE users SET winback_reminded_at=? WHERE tg_id=?",
               (clock.utcnow_iso(), tg_id))


# ---------------------------------------------------------------------------
# Атрибуция роста: источник прихода юзера (deep-link /start src_<label>)
# ---------------------------------------------------------------------------
def set_acquisition_source(tg_id: int, source: str) -> None:
    """First-touch атрибуция: пишем источник один раз, повторные ссылки не перезаписывают."""
    db.execute("UPDATE users SET acquisition_source=? WHERE tg_id=? AND acquisition_source IS NULL",
               (source, tg_id))


def acquisition_breakdown() -> dict:
    """Разбивка по acquisition_source для /growth: сколько приглашено, сколько РЕАЛЬНО
    оплатили. "Оплатили" — EXISTS в payments(status='paid'), НЕ paid_until IS NOT NULL:
    триал (grant_trial) и промокоды (redeem_promo) тоже ставят paid_until, но не пишут в
    payments — иначе конверсия дашборда считала бы бесплатный триал платежом.
    -> {source: {"total": int, "paid": int}}, source="—" — без метки (органика)."""
    rows = db.query(
        "SELECT COALESCE(acquisition_source, '—') AS source, COUNT(*) AS total, "
        "SUM(CASE WHEN EXISTS (SELECT 1 FROM payments p WHERE p.tg_id = users.tg_id "
        "AND p.status='paid') THEN 1 ELSE 0 END) AS paid "
        "FROM users GROUP BY source ORDER BY total DESC")
    return {r["source"]: {"total": r["total"], "paid": r["paid"]} for r in rows}


def admin_revoke(tg_id: int) -> None:
    """Снять подписку (tier=free, paid_until в прошлое)."""
    db.execute("UPDATE users SET tier='free', paid_until=?, updated_at=? WHERE tg_id=?",
               ("2000-01-01T00:00:00+00:00", clock.utcnow_iso(), tg_id))


def user_counts() -> dict:
    total = db.query_one("SELECT COUNT(*) AS c FROM users")["c"]  # type: ignore[index]
    return {"total": total, "paid": len(active_subscriber_ids())}


def _fmt_days(days: float) -> str:
    """Человекочитаемая длительность: дробные дни (тестовые тарифы) -> часы."""
    if days == int(days):
        return f"{int(days)} дней"
    return f"{round(days * 24)} часов"


def subscription_status_text(tg_id: int) -> str:
    """Текст /subscribe: описание, цена и текущий статус."""
    from app import runtime

    head = (
        "💳 <b>Подписка RSI Range Signals</b>\n"
        "────────────────────\n"
        "Все сигналы + автоведение журнала.\n"
        f"Цена: <b>{runtime.price_stars()}⭐</b> за {_fmt_days(runtime.sub_days())} "
        "(тарифы длиннее — кнопками ниже).\n"
    )
    u = get_user(tg_id)
    if u and is_paid(tg_id):
        return head + (f"\n✅ <b>Подписка активна до {u['paid_until'][:10]}</b>. "
                       "Можно продлить кнопкой ниже.")
    return head + "\nНажми кнопку ниже, чтобы оплатить через Telegram Stars 👇"
