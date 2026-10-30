"""
checkout.py — оркестрация оплаты: Stars-инвойс → pre_checkout → successful_payment,
USDT-инвойс, /refund.

Аудит кода 30.10 (§4.4): доменные функции payments.py покрыты тестами на 93 %, а связки
между ними в хэндлерах bot.py — нет. Хэндлеры теперь только делегируют сюда, а тесты
гоняют эти функции на фейковых объектах aiogram (bot.send_invoice, query.answer,
message.answer). Тарифы — из реестра tariffs.py.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app import runtime
from app.delivery import payments, tariffs
from app.delivery.payments import _fmt_days

log = logging.getLogger("bot")

PRECHECKOUT_REJECT_TEXT = "Тариф изменился — откройте /subscribe и оплатите актуальный."
GRANT_FAILED_TEXT = ("✅ Оплата получена, но доступ не включился из-за технического сбоя. "
                     "Администратор уже получил уведомление и включит доступ вручную. "
                     "Статус: /subscribe")


async def send_stars_invoice(bot, chat_id: int, tier: tariffs.Tier) -> None:
    """Инвойс Stars по тарифу реестра: payload несёт ключ тарифа (см. tariffs)."""
    from aiogram.types import LabeledPrice

    await bot.send_invoice(
        chat_id=chat_id,
        title="Подписка RSI Range Signals",
        description=f"Доступ ко всем сигналам на {tier.human}.",
        payload=tariffs.invoice_payload(tier),
        provider_token="",                              # Stars: провайдер не нужен
        currency="XTR",
        prices=[LabeledPrice(label=f"{tier.human} доступа", amount=tier.stars)],
    )


async def pay_stars_tier(bot, cq, tier: tariffs.Tier | None) -> None:
    """Кнопка тарифа. Гейт выключенного тарифа: старые /subscribe-сообщения в истории чата
    хранят кнопки выключенных тарифов — без гейта клик слал бы invoice с amount=0
    (Bad Request) и вечный спиннер юзеру (аудит-2 [12])."""
    if tier is None or not tier.enabled:
        await cq.answer("Этот тариф сейчас недоступен — см. актуальные в /subscribe.",
                        show_alert=True)
        return
    await send_stars_invoice(bot, cq.from_user.id, tier)
    await cq.answer()


async def answer_pre_checkout(q) -> bool:
    """Отказ — до списания, Stars не уходят. Старые инвойсы (выключенный тариф, прежняя
    цена, пересланные копии) иначе оплачивались бы вечно, а нечитаемый payload — сначала
    списание, потом фолбэк на 30 дн (аудит 05.10)."""
    ok = tariffs.precheckout_ok(q.invoice_payload, q.total_amount, q.currency,
                                tariffs.stars_tiers())
    if ok:
        await q.answer(ok=True)
    else:
        log.warning("pre_checkout отклонён: payload=%r amount=%s %s от %s",
                    q.invoice_payload, q.total_amount, q.currency, q.from_user.id)
        await q.answer(ok=False, error_message=PRECHECKOUT_REJECT_TEXT)
    return ok


def _grant_hint(uid: int, days: float) -> str:
    whole = float(days).is_integer()
    return (f"/grant {uid} {int(days)}" if whole
            else f"выдать {_fmt_days(days)} вручную (/grant принимает целые дни)")


async def process_successful_payment(
        m, *, reward_referrer: Callable[[int, float], Awaitable[object]],
        alert_admins: Callable[[str], Awaitable[object]],
        attempts: int = 3, retry_delay: float = 2.0,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep) -> bool | None:
    """successful_payment → выдача подписки. -> True выдано, False дубль, None не выдано.

    Stars уже списаны, а Telegram этот апдейт не повторит. Поэтому выдача идёт с повторами
    (database is locked и прочие разовые сбои), а если не вышло — юзер узнаёт, что оплата
    дошла, и админам уходит charge id для ручной выдачи (аудит кода 30.10, §3.6 п.5).
    """
    sp = m.successful_payment
    uid = m.from_user.id
    parsed = tariffs.parse_payload(sp.invoice_payload)
    if parsed is not None:
        days = parsed[0]
    else:
        # нечитаемый payload — фолбэк на АКТУАЛЬНЫЙ runtime.sub_days (не boot-time cfg)
        # с громким следом: молча выдать 30 дн за lifetime-чардж нельзя (аудит-2 [10])
        days = float(runtime.sub_days())
        log.warning("successful_payment: нечитаемый payload %r от %s (charge=%s) — "
                    "фолбэк %s дн", sp.invoice_payload, uid,
                    sp.telegram_payment_charge_id, days)
    applied: bool | None = None
    for attempt in range(1, attempts + 1):
        try:
            applied = await asyncio.to_thread(
                payments.grant_subscription, uid, "stars", float(sp.total_amount),
                sp.currency, days, sp.telegram_payment_charge_id)
            break
        except Exception as e:                          # noqa: BLE001 — повтор, затем компенсация
            log.warning("successful_payment: выдача %s (charge=%s), попытка %d/%d: %r",
                        uid, sp.telegram_payment_charge_id, attempt, attempts, e)
            if attempt < attempts:
                await sleep(retry_delay * attempt)
    if applied is None:
        log.error("successful_payment: подписка НЕ выдана после %d попыток: юзер %s, "
                  "%s %s, %s дн, charge=%s", attempts, uid, sp.total_amount, sp.currency,
                  days, sp.telegram_payment_charge_id)
        try:
            await m.answer(GRANT_FAILED_TEXT)
        except Exception as e:                          # noqa: BLE001
            log.warning("successful_payment: сообщение юзеру %s: %s", uid, e)
        await alert_admins(
            f"🚨 Оплата Stars не применилась: юзер {uid}, {sp.total_amount}⭐, "
            f"{_fmt_days(days)}, charge {sp.telegram_payment_charge_id}.\n"
            f"Деньги списаны — {_grant_hint(uid, days)}")
        return None
    if applied:
        await m.answer(f"✅ Подписка активна на {_fmt_days(days)}! Сигналы теперь приходят "
                       f"автоматически на закрытии свечей. Статус: /subscribe")
        await reward_referrer(uid, days)                # реф-награда за реальный платёж
    else:
        await m.answer("Этот платёж уже был учтён. Спасибо! Статус: /subscribe")
    return applied


async def send_usdt_invoice(bot, cq, tier: tariffs.UsdtTier | None) -> None:
    """Общий путь USDT-инвойсов всех тиров: poll_cryptobot активирует по days из платежа,
    бонус-дни добавляются там же."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    if tier is None or not tier.enabled:
        await cq.answer("Тариф выключен.", show_alert=True)
        return
    try:
        res = await asyncio.to_thread(payments.create_cryptobot_invoice,
                                      cq.from_user.id, tier.days, tier.amount)
    except Exception as e:                              # noqa: BLE001 — сеть/невалидный токен
        log.warning("create_cryptobot_invoice %s: %s", cq.from_user.id, e)
        res = None
    if res is None:
        await cq.answer("USDT временно недоступен, попробуй позже "
                        "(или оплати Stars)", show_alert=True)
        return
    pay_url, _ = res
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="💵 Перейти к оплате USDT", url=pay_url)]])
    bonus = runtime.usdt_bonus_days()
    bonus_line = f" +{bonus} дн. бонусом за оплату в USDT." if bonus > 0 else ""
    await bot.send_message(
        cq.from_user.id,
        f"Счёт на оплату в USDT создан ({tier.label}). После оплаты подписка "
        f"активируется автоматически (проверка раз в ~минуту).{bonus_line}",
        reply_markup=kb)
    await cq.answer()


async def refund_last_stars_payment(bot, uid: int,
                                    reply: Callable[[str], Awaitable[object]]) -> bool:
    """Money-back: вернуть последний Stars-платёж юзера. -> True, если возврат проведён.

    Порядок: API-возврат ПЕРВЫМ, пометка в БД — после успеха (см. payments). Ответ админу
    уходит через reply, затем — уведомление юзеру."""
    pay = await asyncio.to_thread(payments.find_refundable_stars_payment, uid)
    if not pay:
        await reply("Оплаченных Stars-платежей у юзера не найдено (или уже возвращены).")
        return False
    try:
        await bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=pay["charge_id"])
    except Exception as e:                              # noqa: BLE001 — отказ Telegram
        if "CHARGE_ALREADY_REFUNDED" not in str(e):
            await reply(f"Telegram отказал в возврате: {e}")
            return False
        # уже возвращён на стороне Telegram (краш между API и БД в прошлый раз) —
        # дочиниваем БД, иначе строка вечно paid и /refund зациклен (аудит-2 [9])
        log.warning("refund %s: уже возвращён у Telegram, дочиниваю запись #%s",
                    uid, pay["id"])
    applied = await asyncio.to_thread(payments.mark_refunded, pay["id"], uid,
                                      pay["days"], runtime.referral_reward_days())
    if not applied:                                     # параллельный /refund уже провёл
        await reply(f"Платёж #{pay['id']} уже помечен возвращённым — дни не "
                    "вычитаются повторно.")
        return False
    await reply(f"↩️ Возвращено {pay['amount']:g}⭐ юзеру {uid}; "
                f"подписка скорректирована (−{_fmt_days(pay['days'])}).")
    try:
        await bot.send_message(uid, "↩️ Ваш платёж возвращён — Stars вернутся на "
                                    "баланс Telegram. Спасибо, что попробовали нас.")
    except Exception as e:                              # noqa: BLE001
        log.warning("refund notify %s: %s", uid, e)
    return True
