"""
tariffs.py — единый реестр тарифов Stars и USDT.

Аудит кода 30.10 (топ-15 п.5): один и тот же набор «(дни, цена)» жил в четырёх местах
bot.py — кнопки /subscribe, колбэки оплаты, сверка pre_checkout и USDT-тиры. Поправь
одно — и pre_checkout молча отклонял бы оплату. Теперь все они строятся отсюда.

Инвойс несёт ключ тарифа: payload 'sub:<дни>:<ключ>'. Сверка — по ключу и длительности
в целых минутах, а не по равенству float. Старый формат 'sub:<дни>' (инвойсы, отправленные
до выкатки) принимается по длительности, как раньше.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app import runtime
from app.delivery.payments import _fmt_days

# lifetime = 100 лет: без спец-статуса в БД, просто далёкий paid_until
LIFETIME_DAYS = 36500.0

# порядок показа в /subscribe; month — основной тариф (sub_days/sub_price_stars)
STARS_KEYS = ("intro", "week", "month", "quarter", "year", "life")
USDT_KEYS = ("month", "quarter", "year", "life")


@dataclass(frozen=True)
class Tier:
    """Stars-тариф. stars=0 — тариф выключен: кнопка скрыта, оплата не проходит."""
    key: str
    days: float
    stars: int
    label: str | None = None        # срок в инвойсе; None — из длительности

    @property
    def enabled(self) -> bool:
        return self.stars > 0

    @property
    def callback(self) -> str:
        return "pay_stars" if self.key == "month" else f"pay_stars_{self.key}"

    @property
    def human(self) -> str:
        return self.label or _fmt_days(self.days)


@dataclass(frozen=True)
class UsdtTier:
    """USDT-тариф (Crypto Pay). amount=0 — тариф выключен."""
    key: str
    days: float
    amount: float
    label: str

    @property
    def enabled(self) -> bool:
        return self.amount > 0

    @property
    def callback(self) -> str:
        return "pay_usdt" if self.key == "month" else f"pay_usdt_{self.key}"


STARS_CALLBACKS = frozenset(
    "pay_stars" if k == "month" else f"pay_stars_{k}" for k in STARS_KEYS)
USDT_CALLBACKS = frozenset(
    "pay_usdt" if k == "month" else f"pay_usdt_{k}" for k in USDT_KEYS)


def stars_tiers() -> list[Tier]:
    """Все Stars-тарифы по текущим настройкам, в порядке показа (выключенные — stars=0)."""
    hours = runtime.intro_hours()
    intro_on = hours > 0
    return [
        # round(…, 6) — тот же срок, что в инвойсах до реестра: 5 ч = 0.208333 дн
        Tier("intro", round(hours / 24, 6) if intro_on else 0.0,
             runtime.intro_price_stars() if intro_on else 0),
        Tier("week", 7.0, runtime.week_price_stars(), "7 дней"),
        Tier("month", float(runtime.sub_days()), runtime.price_stars()),
        Tier("quarter", 90.0, runtime.quarter_price_stars()),
        Tier("year", 365.0, runtime.year_price_stars(), "1 год"),
        Tier("life", LIFETIME_DAYS, runtime.lifetime_price_stars(), "навсегда (lifetime)"),
    ]


def stars_tier(key: str) -> Tier | None:
    return next((t for t in stars_tiers() if t.key == key), None)


def stars_tier_by_callback(data: str | None) -> Tier | None:
    return next((t for t in stars_tiers() if t.callback == data), None)


def usdt_tier_price(stars: int) -> float:
    """Цена тира в USDT: пропорция его Stars-цены к месячной × месячная USDT-цена —
    длинные тиры в USDT без отдельных настроек, скидки переносятся сами (запрос владельца
    02.09)."""
    base, usdt = runtime.price_stars(), runtime.price_usdt()
    if base <= 0 or stars <= 0 or usdt <= 0:
        return 0.0
    return round(usdt * stars / base, 2)


def usdt_tiers() -> list[UsdtTier]:
    """USDT-тарифы: месяц по своей цене, квартал/год/lifetime — пропорцией к Stars."""
    by_key = {t.key: t for t in stars_tiers()}
    month = by_key["month"]
    return [
        UsdtTier("month", month.days, max(runtime.price_usdt(), 0.0), _fmt_days(month.days)),
        UsdtTier("quarter", 90.0, usdt_tier_price(by_key["quarter"].stars), "90 дней"),
        UsdtTier("year", 365.0, usdt_tier_price(by_key["year"].stars), "год"),
        UsdtTier("life", LIFETIME_DAYS, usdt_tier_price(by_key["life"].stars),
                 "навсегда (lifetime)"),
    ]


def usdt_tier_by_callback(data: str | None) -> UsdtTier | None:
    return next((t for t in usdt_tiers() if t.callback == data), None)


def invoice_payload(tier: Tier) -> str:
    return f"sub:{tier.days}:{tier.key}"


def parse_payload(payload: str | None) -> tuple[float, str | None] | None:
    """'sub:<дни>' или 'sub:<дни>:<ключ>' -> (дни, ключ|None). Иное, нечисловые и
    неположительные дни -> None."""
    parts = (payload or "").split(":")
    if parts[0] != "sub" or len(parts) not in (2, 3):
        return None
    try:
        days = float(parts[1])
    except ValueError:
        return None
    if not math.isfinite(days) or days <= 0:
        return None
    key = parts[2] if len(parts) == 3 else None
    if key is not None and key not in STARS_KEYS:
        return None
    return days, key


def _minutes(days: float) -> int:
    """Длительность в целых минутах — ключ сравнения вместо равенства float: '30', '30.0'
    и 0.208333 против 5/24 сходятся, а реальные тарифы различаются на часы и дни."""
    return round(days * 1440)


def match_tier(payload: str | None, total: int, currency: str,
               tiers: list[Tier]) -> Tier | None:
    """Включённый тариф, которому соответствует оплата: валюта XTR, ключ (если он есть в
    payload), длительность и ТЕКУЩАЯ цена. None — оплату не пропускать: старый инвойс
    выключенного или подорожавшего тарифа иначе оплачивался бы вечно (аудит 05.10)."""
    if currency != "XTR":
        return None
    parsed = parse_payload(payload)
    if parsed is None:
        return None
    days, key = parsed
    for t in tiers:
        if not t.enabled or int(total) != t.stars:
            continue
        if key is not None and t.key != key:
            continue
        if _minutes(t.days) == _minutes(days):
            return t
    return None


def precheckout_ok(payload: str | None, total: int, currency: str,
                   tiers: list[Tier]) -> bool:
    """Пропустить ли оплату. 'unlock:' (paid media) — платформенный поток, как раньше."""
    if (payload or "").startswith("unlock:"):
        return True
    return match_tier(payload, total, currency, tiers) is not None


def valid_sub_days(days: float) -> bool:
    """Срок основного тарифа: больше нуля (0 дал бы платную подписку на 0 дней и деление
    на ноль в кнопках конца триала) и не длиннее lifetime."""
    return math.isfinite(days) and 0 < days <= LIFETIME_DAYS
