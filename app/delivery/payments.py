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


class _PromoExhausted(Exception):
    """Внутренний сигнал: лимит промокода выбран — откатить транзакцию redeem_promo."""


class _RefRewarded(Exception):
    """Внутренний сигнал: реферер уже награждён (гонка) — откатить транзакцию награды."""


# Минимальная длительность ОПЛАЧЕННОГО периода, квалифицирующая реф-награду: intro-тариф
# (часы за 10⭐) не считается — иначе ферма «10⭐ intro с мультиаккаунта -> +7 дн (~233⭐)
# рефереру» с положительным ROI для накрутчика (аудит-2 [2]). 7 дн отсекает часовые тарифы
# и пропускает все реальные (месяц/квартал/год/lifetime и /grant-подтверждение месяца).
REFERRAL_MIN_PAID_DAYS = 7.0


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def register_user(tg_id: int, username: str | None = None, lang: str = "ru") -> None:
    """Завести юзера (idempotent) или обновить username при повторном /start."""
    now = clock.utcnow_iso()
    db.execute(
        "INSERT INTO users(tg_id, username, lang, tier, joined_at, updated_at) "
        "VALUES(?,?,?, 'free', ?, ?) "
        "ON CONFLICT(tg_id) DO UPDATE SET "
        # COALESCE: служебные вызовы (grant/promo/mute) идут без username и не должны
        # затирать известное имя в NULL (аудит-2 [8]); is_active=1 — возврат юзера
        # (повторный /start) снимает пометку «заблокировал бота»
        "username=COALESCE(excluded.username, username), is_active=1, "
        "updated_at=excluded.updated_at",
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


def free_active_ids() -> list[int]:
    """tg_id живых free-юзеров (недельный дайджест с блоком «что ты упустил»)."""
    now = clock.utcnow_iso()
    return [r["tg_id"] for r in db.query(
        "SELECT tg_id FROM users WHERE is_active=1 AND (tier='free' "
        "OR (tier='paid' AND paid_until <= ?))", (now,))]


def missed_teaser_r(tg_id: int, start_iso: str, end_iso: str) -> dict:
    """«Что ты упустил»: итог ПОЛНОЙ версии сигналов, которые юзер получил ТИЗЕРАМИ,
    а их сделка закрылась на этой неделе [start, end).

    Считается по факту доставок (deliveries.kind='teaser') × закрытым исходам журнала —
    никаких гипотетических «мог бы»: ровно те сигналы, что юзер реально видел без уровней.
    Знак честный: минусовая неделя показывается минусом (доверие дороже конверсии).
    -> {'n': int, 'total_r': float}"""
    row = db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(s.result_r), 0) AS total_r "
        "FROM deliveries d JOIN signals s ON s.id = d.signal_id "
        "WHERE d.tg_id = ? AND d.kind = 'teaser' AND s.result_r IS NOT NULL "
        "AND s.closed_at >= ? AND s.closed_at < ?", (tg_id, start_iso, end_iso))
    if row is None:                     # COUNT всегда отдаёт строку; ветка — для mypy
        return {"n": 0, "total_r": 0.0}
    return {"n": int(row["n"]), "total_r": round(float(row["total_r"]), 2)}


def mark_delivery_unlocked(tg_id: int, signal_id: int) -> bool:
    """Разовая покупка сигнала (paid media): тизерная доставка становится 'unlock' —
    иначе «что ты упустил» показывал бы юзеру как упущенный сигнал, который он КУПИЛ
    (ревью 12.08). -> была ли тизерная запись."""
    return bool(db.execute("UPDATE deliveries SET kind='unlock' "
                           "WHERE tg_id=? AND signal_id=? AND kind='teaser'",
                           (tg_id, signal_id)))


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
        _extend_and_activate(conn, tg_id, days, real_payment=True)
    return True


def due_for_trial_drip(trial_days: float) -> list:
    """Кому и какую стадию онбординг-дрипа слать: -> [(tg_id, stage)].

    Пороги от trial_granted_at: стадия 1 — через 1 день (как читать сигнал),
    стадия 2 — через 3 дня (проверь трек-рекорд сам), стадия 3 — за сутки до конца
    триала (trial_days-1, но не раньше стадии 2). Оплатившие исключаются НАВСЕГДА
    (реальный платёж в payments) — им продавать нечего; заблокировавшие бота тоже.
    Одна стадия за вызов на юзера (следующая — следующим тактом)."""
    thresholds = [(1, 1.0), (2, 3.0), (3, max(trial_days - 1.0, 3.5))]
    now = _now_dt()
    out = []
    rows = db.query(
        "SELECT u.tg_id, u.trial_granted_at, u.trial_drip_stage FROM users u "
        "WHERE u.trial_granted_at IS NOT NULL AND u.trial_drip_stage < 3 AND u.is_active=1 "
        "AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.tg_id=u.tg_id AND p.status='paid')")
    for r in rows:
        granted = datetime.fromisoformat(r["trial_granted_at"])
        age_days = (now - granted).total_seconds() / 86400
        if age_days > trial_days + 1.0:
            continue                                    # поезд ушёл — см. expire_stale_trial_drips
        for stage, after in thresholds:
            if r["trial_drip_stage"] < stage and age_days >= after:
                out.append((r["tg_id"], stage))
                break                                   # одна стадия за такт
    return out


def expire_stale_trial_drips(trial_days: float) -> int:
    """Молча закрыть дрип тем, чей триал истёк до включения фичи (или письмо опоздало) —
    иначе первый же такт после деплоя разослал бы онбординг задним числом. -> сколько."""
    cutoff = (_now_dt() - timedelta(days=trial_days + 1.0)).isoformat()
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE users SET trial_drip_stage=3, updated_at=? "
            "WHERE trial_granted_at IS NOT NULL AND trial_drip_stage < 3 "
            "AND trial_granted_at < ?",
            (clock.utcnow_iso(), cutoff))
        return cur.rowcount


def mark_trial_drip(tg_id: int, stage: int) -> None:
    db.execute("UPDATE users SET trial_drip_stage=?, updated_at=? "
               "WHERE tg_id=? AND trial_drip_stage < ?",
               (stage, clock.utcnow_iso(), tg_id, stage))


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
    а сама подписка от возвращённого платежа гаснет).

    Чтение paid_until — ВНУТРИ транзакции (под _write_lock): снаружи покупка, закоммиченная
    между чтением и записью, была бы затёрта устаревшим new_until (аудит-2 [9])."""
    with db.transaction() as conn:
        conn.execute("UPDATE payments SET status='refunded' WHERE id=?", (payment_id,))
        u = conn.execute("SELECT paid_until FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        if u and u["paid_until"]:
            cur = datetime.fromisoformat(u["paid_until"])
            new_until = max(_now_dt(), cur - timedelta(days=days)).isoformat()
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
    # персональный код (win-back wb<tg_id>) погашает ТОЛЬКО владелец: id в Telegram
    # публичны, «первый погасивший забирает» позволял угонять чужой win-back (аудит-2 [4]).
    # Чужому отвечаем not_found — не раскрываем сам факт существования кода.
    if row["owner_tg_id"] is not None and row["owner_tg_id"] != tg_id:
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
        return {"ok": False, "days": None, "reason": "exhausted"}  # быстрый отказ (не авторитетный)
    register_user(tg_id)
    # АВТОРИТЕТНЫЙ гейт — в транзакции: чек max_uses выше был чтением ВНЕ _write_lock, две
    # конкурентные попытки могли обе пройти его до инкремента и выдать код сверх лимита
    # (аудит [3]). Порядок: сперва INSERT redemption (UNIQUE(code,tg_id) режет дабл-тап того
    # же юзера ДО инкремента), затем УСЛОВНЫЙ инкремент used_count с гейтом по rowcount.
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO promo_redemptions(code, tg_id, redeemed_at) VALUES(?,?,?)",
                (code, tg_id, now))
            cur = conn.execute(
                "UPDATE promo_codes SET used_count = used_count + 1 "
                "WHERE code=? AND (max_uses IS NULL OR used_count < max_uses)", (code,))
            if cur.rowcount != 1:                        # лимит выбран между чеком и транзакцией
                raise _PromoExhausted
            _extend_and_activate(conn, tg_id, row["days"])
    except _PromoExhausted:
        return {"ok": False, "days": None, "reason": "exhausted"}
    except sqlite3.IntegrityError:
        return {"ok": False, "days": None, "reason": "already_used"}
    return {"ok": True, "days": row["days"], "reason": None}


def _extend_and_activate(conn, tg_id: int, days: float, real_payment: bool = False) -> str:
    """Продлить подписку от max(сейчас, текущий paid_until) на days; tier=paid.

    real_payment=True (Stars / USDT / /grant-подтверждение) дополнительно перевзводит
    win-back (winback_reminded_at=NULL). Промо/триал НЕ перевзводят: иначе цикл
    «триал -> win-back-промо -> погасил -> снова win-back» раздавал бы полный доступ
    бесплатно вечно (аудит-2 [1]). renew-напоминание перевзводится всегда — оно про
    новую дату истечения, а не про источник дней. -> new paid_until."""
    now = clock.utcnow_iso()
    u = get_user(tg_id)
    base = u["paid_until"] if (u and u["paid_until"] and u["paid_until"] > now) else now
    new_until = (datetime.fromisoformat(base) + timedelta(days=days)).isoformat()
    wb_reset = ", winback_reminded_at=NULL" if real_payment else ""
    conn.execute(f"UPDATE users SET tier='paid', paid_until=?, renew_reminded_at=NULL"
                 f"{wb_reset}, updated_at=? WHERE tg_id=?",
                 (new_until, now, tg_id))
    return new_until


# ---------------------------------------------------------------------------
# Воронка доставок (free: N полных сигналов/нед, дальше — тизер)
# ---------------------------------------------------------------------------
def all_user_ids() -> list[int]:
    return [r["tg_id"] for r in db.query("SELECT tg_id FROM users WHERE is_active=1")]


def mark_blocked(tg_id: int) -> None:
    """403/«chat not found» при отправке: юзер недостижим — is_active=0, чтобы не слать
    мёртвому получателю каждый сигнал (142 отказа за 500 строк лога, аудит-2 ops [8]).
    Повторный /start снимает пометку (register_user ставит is_active=1)."""
    db.execute("UPDATE users SET is_active=0, updated_at=? WHERE tg_id=?",
               (clock.utcnow_iso(), tg_id))


def delivered(tg_id: int, signal_id: int) -> bool:
    return db.query_one("SELECT 1 FROM deliveries WHERE tg_id=? AND signal_id=?",
                        (tg_id, signal_id)) is not None


def delivered_full(tg_id: int, signal_id: int) -> bool:
    """Получал ли юзер ПОЛНЫЙ сигнал (full/unlock, не teaser) — гейт кнопок
    «график/риск»: поддельный callback_data не должен отдавать платные уровни
    (аудит 02.09)."""
    return db.query_one(
        "SELECT 1 FROM deliveries WHERE tg_id=? AND signal_id=? "
        "AND kind IN ('full','unlock')", (tg_id, signal_id)) is not None


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
    """Понизить истёкших paid -> free. -> tg_id РЕАЛЬНО понижённых (для уведомления).

    Понижение — условный UPDATE с повторной проверкой: между SELECT и UPDATE юзер мог
    оплатить (платёжные хендлеры конкурентны с обслуживанием), безусловный UPDATE понизил
    бы только что заплатившего и выкинул его из /announce и напоминаний (аудит-2 [6])."""
    now = clock.utcnow_iso()
    ids = [r["tg_id"] for r in db.query(
        "SELECT tg_id FROM users WHERE tier='paid' AND (paid_until IS NULL OR paid_until <= ?)",
        (now,))]
    demoted = []
    for tg_id in ids:
        n = db.execute(
            "UPDATE users SET tier='free', updated_at=? WHERE tg_id=? AND tier='paid' "
            "AND (paid_until IS NULL OR paid_until <= ?)", (now, tg_id, now))
        if n == 1:
            demoted.append(tg_id)
    return demoted


def due_for_reminder(days: int = 3, min_left_hours: int = 12) -> list:
    """Платные, у кого подписка истекает в ближайшие `days` и кому ещё не напоминали.

    min_left_hours: не напоминать, когда осталось МЕНЬШЕ N часов — часовые тарифы (intro)
    иначе получают «истекает» через минуты после покупки (подтверждено на обоих реальных
    intro-покупателях, аудит-2 [5]); напоминание в последние часы — шум, не ценность."""
    floor_ = (_now_dt() + timedelta(hours=min_left_hours)).isoformat()
    horizon = (_now_dt() + timedelta(days=days)).isoformat()
    return db.query(
        "SELECT tg_id, paid_until FROM users WHERE tier='paid' AND paid_until > ? "
        "AND paid_until <= ? AND renew_reminded_at IS NULL", (floor_, horizon))


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
        # сутки на оплату: без expires_in инвойсы вечные — pending-хвост рос бы
        # безгранично, а старый инвойс оплачивался бы по устаревшей цене (аудит-2 [7])
        "expires_in": 86400,
    })
    inv_id = str(res["invoice_id"])
    pay_url = res.get("bot_invoice_url") or res.get("pay_url") or res.get("mini_app_invoice_url")
    db.execute(
        "INSERT INTO payments(tg_id, method, amount, currency, days, status, "
        "invoice_id, created_at) VALUES(?,?,?,?,?, 'pending', ?, ?)",
        (tg_id, "cryptobot", float(amount), "USDT", days, inv_id, clock.utcnow_iso()))
    return pay_url, inv_id


def poll_cryptobot() -> list[tuple[int, float]]:
    """Проверить оплату pending USDT-инвойсов, активировать подписку.
    -> [(tg_id, оплаченные_дни)] активированных — days нужны гейту реф-награды.

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
    dead = {str(it["invoice_id"]) for it in items if it.get("status") == "expired"}
    activated = []
    for p in pending:
        if p["invoice_id"] in dead:                     # истёк у провайдера — закрыть локально
            db.execute("UPDATE payments SET status='expired' WHERE id=? AND status='pending'",
                       (p["id"],))
            continue
        if p["invoice_id"] not in paid:
            continue
        now = clock.utcnow_iso()
        with db.transaction() as conn:
            cur = conn.execute("UPDATE payments SET status='paid', paid_at=? "
                               "WHERE id=? AND status='pending'", (now, p["id"]))
            if cur.rowcount != 1:                       # уже обработан параллельно
                continue
            _extend_and_activate(conn, p["tg_id"], p["days"] + runtime.usdt_bonus_days(),
                                 real_payment=True)
        # days платежа — для гейта реф-награды (intro-длительности не квалифицируют)
        activated.append((p["tg_id"], float(p["days"])))
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
        _extend_and_activate(conn, tg_id, days, real_payment=True)
        # погасить pending USDT-инвойсы юзера: /grant = ручное подтверждение оплаты,
        # авто-поллер не должен вторично начислить те же деньги (аудит-2 [3]);
        # 'expired' — из существующего CHECK-набора статусов
        conn.execute("UPDATE payments SET status='expired' "
                     "WHERE tg_id=? AND method='cryptobot' AND status='pending'", (tg_id,))
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
    # чек+вставка в ОДНОЙ транзакции (под _write_lock) — иначе дабл-тап кнопки «Я оплатил»
    # создаёт две открытые заявки и дубль-пинги админам по SLA (аудит [8]).
    with db.transaction() as conn:
        existing = conn.execute(
            "SELECT id FROM usdt_claims WHERE tg_id=? AND resolved_at IS NULL "
            "ORDER BY id DESC LIMIT 1", (tg_id,)).fetchone()
        if existing:
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO usdt_claims(tg_id, created_at) VALUES(?,?)",
            (tg_id, clock.utcnow_iso()))
        return cur.lastrowid


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
    `days_after + window_days` (давно ушедших не будим). Одно напоминание на цикл.

    ТОЛЬКО реально плативших (EXISTS в payments): триал/промо тоже ставят paid_until,
    и без этого гейта каждый триальщик получал бы вечный цикл бесплатных win-back-промо
    (аудит-2 [1] — «вечная бесплатная подписка»)."""
    ceil = (_now_dt() - timedelta(days=days_after)).isoformat()
    floor = (_now_dt() - timedelta(days=days_after + window_days)).isoformat()
    return db.query(
        "SELECT tg_id, paid_until FROM users WHERE tier='free' AND paid_until IS NOT NULL "
        "AND paid_until <= ? AND paid_until >= ? AND winback_reminded_at IS NULL "
        "AND EXISTS (SELECT 1 FROM payments p WHERE p.tg_id = users.tg_id "
        "AND p.status='paid')",
        (ceil, floor))


def mark_winback_reminded(tg_id: int) -> None:
    db.execute("UPDATE users SET winback_reminded_at=? WHERE tg_id=?",
               (clock.utcnow_iso(), tg_id))


def issue_winback_promo(tg_id: int, days: int, valid_days: int) -> str:
    """Персональный одноразовый промокод wb<tg_id> для win-back: +days подписки, max_uses=1,
    сгорает через valid_days (срочность). Пересоздаётся НАЧИСТО каждый цикл win-back — иначе
    повторно ушедший юзер получил бы уже погашенный код (already_used). Win-back срабатывает
    раз на цикл лапса (winback_reminded_at сбрасывается в _extend_and_activate при реактивации),
    поэтому пересоздание безопасно. -> код для подстановки в сообщение."""
    code = f"wb{tg_id}"
    expires = (_now_dt() + timedelta(days=valid_days)).isoformat()
    with db.transaction() as conn:
        conn.execute("DELETE FROM promo_redemptions WHERE code=?", (code,))
        conn.execute("DELETE FROM promo_codes WHERE code=?", (code,))
        conn.execute(
            "INSERT INTO promo_codes(code, days, max_uses, used_count, expires_at, "
            "created_at, owner_tg_id) VALUES(?,?,1,0,?,?,?)",
            (code, days, expires, clock.utcnow_iso(), tg_id))
    return code


# ---------------------------------------------------------------------------
# Реферальная программа (задача 23): /start ref_<tg_id> -> invited_by (first-touch),
# награда рефереру ТОЛЬКО за реальный первый платёж приглашённого (анти-накрутка).
# ---------------------------------------------------------------------------
def set_referrer(tg_id: int, referrer_id: int) -> bool:
    """First-touch реферер: пишем invited_by ОДИН раз и только НЕ-плативших юзеров
    (ретро-атрибуция уже вовлечённого = накрутка). Само-реферал и несуществующий реферер
    отсекаются. -> True если записано."""
    if referrer_id == tg_id or get_user(referrer_id) is None:
        return False
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE users SET invited_by=?, updated_at=? WHERE tg_id=? AND invited_by IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.tg_id=? AND p.status='paid')",
            (referrer_id, clock.utcnow_iso(), tg_id, tg_id))
        return cur.rowcount == 1


def reward_referrer_on_payment(tg_id: int, days: int,
                               paid_days: float) -> tuple[int, int] | None:
    """Наградить реферера за приглашённого при его РЕАЛЬНОМ платеже (+days подписки рефереру).
    Вызывать из путей реального платежа (Stars, USDT, /grant). Идемпотентно:
    referral_rewarded_at даёт ОДНУ награду на приглашённого — на renewal не повторяется
    (флаг уже стоит), триал/промо сюда не заходят. Само-реферал отсечён в set_referrer.

    paid_days — длительность ОПЛАЧЕННОГО периода: короткие тарифы (intro, часы) награду
    не квалифицируют (REFERRAL_MIN_PAID_DAYS) — анти-ферма (аудит-2 [2]); флаг rewarded
    при этом НЕ ставится, полноценный платёж позже всё ещё наградит.
    -> (referrer_id, days) для уведомления."""
    if paid_days < REFERRAL_MIN_PAID_DAYS:
        return None
    u = get_user(tg_id)
    if u is None or u["invited_by"] is None or u["referral_rewarded_at"] is not None:
        return None
    referrer = u["invited_by"]
    now = clock.utcnow_iso()
    try:
        with db.transaction() as conn:
            cur = conn.execute(
                "UPDATE users SET referral_rewarded_at=?, updated_at=? "
                "WHERE tg_id=? AND referral_rewarded_at IS NULL", (now, now, tg_id))
            if cur.rowcount != 1:                          # гонка: уже награждён
                raise _RefRewarded
            _extend_and_activate(conn, referrer, days)
    except _RefRewarded:
        return None
    return (referrer, days)


def referral_stats(tg_id: int) -> dict:
    """Для /referral: сколько приглашено и за скольких начислена награда (реально оплатили)."""
    total = db.query_one("SELECT COUNT(*) AS c FROM users WHERE invited_by=?", (tg_id,))
    paid = db.query_one(
        "SELECT COUNT(*) AS c FROM users WHERE invited_by=? AND referral_rewarded_at IS NOT NULL",
        (tg_id,))
    return {"invited": total["c"] if total else 0, "rewarded": paid["c"] if paid else 0}


# ---------------------------------------------------------------------------
# Персональный журнал (отметки «я зашёл» из Mini App) и коуч дисциплины
# ---------------------------------------------------------------------------
_CLOSED_SQL = ("status IN ('tp1','tp2','tp3','stopped','expired') "
               "AND result_r IS NOT NULL")


def taken_stats(tg_id: int) -> dict:
    """Личный итог по отмеченным закрытым (для карточки и Mini App)."""
    rs = [r["result_r"] for r in db.query(
        f"SELECT s.result_r FROM user_taken ut JOIN signals s ON s.id = ut.signal_id "
        f"WHERE ut.tg_id=? AND {_CLOSED_SQL} ORDER BY s.closed_at", (tg_id,))]
    wins = sum(1 for r in rs if r > 0)
    return {"trades": len(rs), "total_r": round(sum(rs), 2),
            "winrate": round(wins / len(rs), 4) if rs else None,
            "last": [round(r, 2) for r in rs[-10:]]}


def weekly_discipline(tg_id: int, start_iso: str, end_iso: str) -> dict | None:
    """Разбор дисциплины за период: взятое юзером vs всё закрытое стратегией.

    None — юзер не пользуется отметками вовсе (ни одной за всю историю): блока в
    дайджесте у него нет, это не «пропустил всё». Пропущенное считается только по
    закрытиям периода — открытые ещё ничем не кончились."""
    if not db.query_one("SELECT 1 FROM user_taken WHERE tg_id=? LIMIT 1", (tg_id,)):
        return None
    marked = {r["signal_id"] for r in db.query(
        "SELECT signal_id FROM user_taken WHERE tg_id=?", (tg_id,))}
    closed = list(db.query(
        f"SELECT id, side, result_r FROM signals WHERE {_CLOSED_SQL} "
        f"AND closed_at >= ? AND closed_at < ?", (start_iso, end_iso)))
    taken = [r for r in closed if r["id"] in marked]
    skipped = [r for r in closed if r["id"] not in marked]
    return {
        "closed_n": len(closed),
        "closed_r": round(sum(r["result_r"] for r in closed), 2),
        "taken_n": len(taken),
        "taken_r": round(sum(r["result_r"] for r in taken), 2),
        "skipped_n": len(skipped),
        "skipped_r": round(sum(r["result_r"] for r in skipped), 2),
        "skipped_short": sum(1 for r in skipped if r["side"] == "SHORT"),
        "skipped_long": sum(1 for r in skipped if r["side"] == "LONG"),
    }


# ---------------------------------------------------------------------------
# Настройки уведомлений: мьют и тихие часы (гейтят только DM-пуш сигналов)
# ---------------------------------------------------------------------------
def set_muted(tg_id: int, muted: bool) -> None:
    register_user(tg_id)
    db.execute("UPDATE users SET notify_muted=?, updated_at=? WHERE tg_id=?",
               (1 if muted else 0, clock.utcnow_iso(), tg_id))


def set_quiet_hours(tg_id: int, start: int | None, end: int | None) -> None:
    """Тихие часы в UTC [start, end). start=end=None — выключить."""
    register_user(tg_id)
    db.execute("UPDATE users SET quiet_start=?, quiet_end=?, updated_at=? WHERE tg_id=?",
               (start, end, clock.utcnow_iso(), tg_id))


def set_depo(tg_id: int, depo_usd: float | None, risk_pct: float | None) -> None:
    """Персональный сайзинг для DM-сигналов (/depo). None/None — выключить."""
    register_user(tg_id)
    db.execute("UPDATE users SET depo_usd=?, depo_risk_pct=?, updated_at=? WHERE tg_id=?",
               (depo_usd, risk_pct, clock.utcnow_iso(), tg_id))


def depo_prefs(tg_id: int) -> tuple[float, float] | None:
    """(депозит, риск%) юзера или None (фича выключена/юзер неизвестен)."""
    u = get_user(tg_id)
    if u is None or u["depo_usd"] is None:
        return None
    return float(u["depo_usd"]), float(u["depo_risk_pct"] or 1.0)


def notify_prefs(tg_id: int) -> dict:
    u = get_user(tg_id)
    if u is None:
        return {"muted": False, "quiet_start": None, "quiet_end": None}
    return {"muted": bool(u["notify_muted"]),
            "quiet_start": u["quiet_start"], "quiet_end": u["quiet_end"]}


def is_signal_suppressed(tg_id: int, hour_utc: int) -> bool:
    """Подавлять ли DM-пуш сигнала сейчас: мьют ИЛИ попадание в тихие часы (UTC).
    Окно [start, end); start>end — окно через полночь; любая граница NULL — тихих часов нет."""
    u = get_user(tg_id)
    if u is None:
        return False
    if u["notify_muted"]:
        return True
    qs, qe = u["quiet_start"], u["quiet_end"]
    if qs is None or qe is None or qs == qe:
        return False
    return qs <= hour_utc < qe if qs < qe else (hour_utc >= qs or hour_utc < qe)


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
