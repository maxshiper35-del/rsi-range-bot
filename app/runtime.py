"""
runtime.py — настройки, правимые из Telegram без рестарта.

Значение берётся из таблицы settings (override), иначе — дефолт из .env (Settings).
Читается дёшево (SQLite WAL) на каждом такте poller / рассылке.
"""
from __future__ import annotations

from app import clock, db
from app.config import get_settings

_TRUE = {"1", "true", "on", "yes", "да", "вкл"}


def _raw(key: str):
    row = db.query_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else None


def _num(key: str, cast, default):
    """Числовой override с ЗАЩИТОЙ: битое значение (ручная правка БД, кривой ввод) ->
    дефолт, а не краш getter'а. Иначе одно плохое число бриксовало бы подписку/админку
    (аудит [6]). Дефолт может быть значением или вызываемым (ленивый get_settings)."""
    v = _raw(key)
    if v is None:
        return default() if callable(default) else default
    try:
        return cast(v)
    except (TypeError, ValueError):
        return default() if callable(default) else default


def set_value(key: str, value) -> None:
    db.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, str(value), clock.utcnow_iso()))


def _csv(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def symbols() -> list[str]:
    v = _raw("symbols")
    return _csv(v) if v is not None else list(get_settings().symbols)


def timeframes() -> list[str]:
    v = _raw("timeframes")
    return _csv(v) if v is not None else list(get_settings().timeframes)


def signal_mode() -> str:
    return _raw("signal_mode") or get_settings().signal_mode


def paused() -> bool:
    return (_raw("paused") or "0").lower() in _TRUE


def signals_channel() -> str:
    v = _raw("signals_channel")
    return v if v is not None else get_settings().signals_channel


def channel_enabled() -> bool:
    v = _raw("channel_enabled")
    if v is not None:
        return v.lower() in _TRUE
    return bool(signals_channel())


def channel_mode() -> str:
    """full — полный сигнал в канал; teaser — превью с призывом подписаться."""
    return _raw("channel_mode") or "full"


def usdt_invoice_url() -> str:
    """Ссылка на ручной счёт CryptoBot (полу-ручной приём USDT). Пусто = выкл."""
    return _raw("usdt_invoice_url") or ""


def cryptobot_token() -> str:
    """API-токен Crypto Pay (авто-USDT). Админский override приоритетнее .env.

    Пустой override («/cryptobottoken -») РЕАЛЬНО выключает авто-USDT, даже если
    токен задан в .env — различаем «нет override» (None) и «override пустой» ("").
    """
    v = _raw("cryptobot_token")
    return v if v is not None else (get_settings().cryptobot_token or "")


def usdt_bonus_days() -> int:
    """Бонус-дни за оплату USDT вместо Stars (Stars дороже мерчанту ~38% эфф. комиссии)."""
    return _num("usdt_bonus_days", int, 2)


def cornix_block_enabled() -> bool:
    """Добавлять ли Cornix-совместимый блок к постам канала. Дефолт выкл —
    перед включением нужна ручная сверка с реальным Cornix (формат может меняться)."""
    return (_raw("cornix_block_enabled") or "0").lower() in _TRUE


def min_confidence() -> float:
    return _num("min_confidence", float, 0.5)


def min_impulse_atr() -> float:
    return _num("min_impulse_atr", float, 1.0)


# Дефолт: только Setup 1:1 включён. Честный walk-forward бэктест (docs/ — вынесены из репо)
# показал 6226 и LTF-BB/LTF-OB стабильно убыточными на BTC/ETH/SOL 12 мес; живая эксплуатация
# (BTC LONG LTF-BB ×3 подряд, все в стоп, −3R за один день) подтвердила это на практике.
_DEFAULT_ENABLED_SETUPS = "1:1"


def last_poller_tick_at() -> str | None:
    """Метка времени последнего успешного такта poller'а — сигнал живости для /health."""
    return _raw("last_poller_tick_at")


def set_last_poller_tick_at(iso: str) -> None:
    set_value("last_poller_tick_at", iso)


def last_poller_alert_at() -> str | None:
    """Когда последний раз слали админу алерт о зависшем такте поллера — троттлинг."""
    return _raw("last_poller_alert_at")


def set_last_poller_alert_at(iso: str) -> None:
    set_value("last_poller_alert_at", iso)


def last_backup_at() -> str | None:
    return _raw("last_backup_at")


def set_last_backup_at(iso: str) -> None:
    set_value("last_backup_at", iso)


def last_backup_alert_at() -> str | None:
    """Когда последний раз слали админу алерт о пропуске бэкапа — троттлинг (не спамить
    на каждом такте poller, пока бэкап не появится)."""
    return _raw("last_backup_alert_at")


def set_last_backup_alert_at(iso: str) -> None:
    set_value("last_backup_alert_at", iso)


def trackrecord_last_week() -> str | None:
    """ISO-неделя (YYYY-Www) последней публикации трек-рекорда в канал — не постить дважды
    за одну календарную неделю."""
    return _raw("trackrecord_last_week")


def set_trackrecord_last_week(week_key: str) -> None:
    set_value("trackrecord_last_week", week_key)


def weekly_digest_last_week() -> str | None:
    """ISO-неделя последнего личного дайджеста подписчикам — не рассылать дважды за неделю."""
    return _raw("weekly_digest_last_week")


def set_weekly_digest_last_week(week_key: str) -> None:
    set_value("weekly_digest_last_week", week_key)


def last_board_update_at() -> str | None:
    return _raw("last_board_update_at")


def set_last_board_update_at(iso: str) -> None:
    set_value("last_board_update_at", iso)


def live_board_message_id() -> int | None:
    v = _raw("live_board_message_id")
    return int(v) if v else None            # "" тоже трактуем как «не установлено»


def set_live_board_message_id(message_id: int | None) -> None:
    """message_id=None очищает — следующий цикл живого табло создаст новый закреплённый
    пост вместо повторных попыток редактировать протухшее/удалённое сообщение."""
    set_value("live_board_message_id", message_id if message_id is not None else "")


def enabled_setups() -> tuple:
    """Какие сетапы SMCEngine разрешено торговать. CSV в settings, дефолт см. выше."""
    v = _raw("enabled_setups")
    raw = v if v is not None else _DEFAULT_ENABLED_SETUPS
    return tuple(x.strip() for x in raw.split(",") if x.strip())


def price_stars() -> int:
    return _num("sub_price_stars", int, lambda: get_settings().sub_price_stars)


def price_usdt() -> float:
    return _num("sub_price_usdt", float, lambda: get_settings().sub_price_usdt)


def sub_days() -> float:
    """Длительность подписки в днях. float (не int) — админ может выставить дробный срок
    (напр. тестовая подписка на несколько часов: /price days 0.2083333333333333)."""
    return _num("sub_days", float, lambda: get_settings().sub_days)


def free_per_week() -> int:
    return _num("free_signals_per_week", int, lambda: get_settings().free_signals_per_week)


def intro_price_stars() -> int:
    """Цена пробного тарифа в Stars (0 — пробный тариф выключен, остаётся только основной).
    Решение владельца 2026-07-10: тестовая цена 10⭐/5ч показала реальные платежи —
    оставлена постоянным вторым тарифом рядом с основным 1000⭐/30д."""
    return _num("intro_price_stars", int, 10)


def intro_hours() -> float:
    """Длительность пробного тарифа в часах."""
    return _num("intro_hours", float, 5.0)


def vip_channel_id() -> str:
    """Chat_id/@username приватного VIP-канала (мега-план MAX: платная подписка вместо DM,
    которую часть юзеров мьютит/блокирует). Пусто — фича не настроена владельцем."""
    return _raw("vip_channel_id") or ""


def vip_invite_link() -> str:
    """Последняя сгенерированная subscription-invite-ссылка (для повторного показа
    в /vipchannel без пересоздания — revoke делает старую нерабочей)."""
    return _raw("vip_invite_link") or ""


def webapp_url() -> str:
    """Публичный HTTPS-URL страницы Mini App (Cloudflare Tunnel / статик-хост). Пусто —
    кнопка «Дашборд» отдаёт самодостаточный HTML-файл; при заданном URL становится
    web_app-кнопкой (открывает Mini App внутри Telegram). Мега-план: «один URL away»."""
    return _raw("webapp_url") or ""


def stream_enabled() -> bool:
    """Реалтайм-стрим Bybit (мгновенное «вход исполнен»). Дефолт ВЫКЛ — фича за флагом;
    официальный bar-скоринг исходов от стрима не зависит, poller — бэкстоп."""
    return (_raw("stream_enabled") or "0").lower() in _TRUE


def watchlist_auto_enabled() -> bool:
    """Авто-ротация watchlist по ликвидности (мега-план MAX, ставка 3). Дефолт ВЫКЛ —
    включает владелец; кандидаты проходят бэктест-гейт перед добавлением."""
    return (_raw("watchlist_auto_enabled") or "0").lower() in _TRUE


def watchlist_size() -> int:
    return _num("watchlist_size", int, 15)


def watchlist_blacklist() -> tuple:
    """Символы, которые авто-ротация никогда не добавит (CSV, задаёт владелец)."""
    raw = _raw("watchlist_blacklist") or ""
    return tuple(s.strip() for s in raw.split(",") if s.strip())


def last_milestone() -> int:
    """Последняя опубликованная веха закрытых сделок (см. bot.MILESTONES)."""
    v = _raw("last_milestone")
    return int(v) if v is not None else 0


def last_watchlist_refresh_at() -> str | None:
    return _raw("last_watchlist_refresh_at")


def set_last_watchlist_refresh_at(iso: str) -> None:
    set_value("last_watchlist_refresh_at", iso)


def _stars_tier(key: str, default: int) -> int:
    return _num(key, int, default)


def quarter_price_stars() -> int:
    """Квартал (90 дней) разовым Stars-инвойсом. 0 = кнопка скрыта.
    Нативные Stars-подписки длиннее 30 дней невозможны (проверено в доках) —
    длинные тиры только разовыми платежами."""
    return _stars_tier("quarter_price_stars", 2500)


def year_price_stars() -> int:
    """Год (365 дней) разовым Stars-инвойсом. 0 = кнопка скрыта."""
    return _stars_tier("year_price_stars", 8000)


def lifetime_price_stars() -> int:
    """Lifetime (навсегда: 36500 дней) разовым Stars-инвойсом. 0 = кнопка скрыта.
    Бенчмарк рынка: $729 lifetime у CoinCodeCap (~15000⭐)."""
    return _stars_tier("lifetime_price_stars", 15000)


def paid_signal_stars() -> int:
    """Цена разовой покупки одного сигнала в Stars (paid media, мега-план волна 1).
    0 = кнопка «Открыть сигнал» не показывается (дефолт — фича за флагом: включает
    владелец из админки после живой проверки)."""
    return _num("paid_signal_stars", int, 0)


def trial_days() -> int:
    """Длительность триала (задача 20) — короткий полный доступ для новых юзеров.
    Нет .env-фолбэка (не настройка через Settings, как остальные) — дефолт 5 дней
    зашит здесь же, по рекомендации бенчмарков конверсии (firstpagesage.com/1capture.io:
    time-limited триал 3-7 дней даёт на порядок выше конверсию, чем бессрочный free-тир)."""
    return _num("trial_days", int, 5)


def referral_reward_days() -> int:
    """Сколько дней подписки получает реферер за реальный первый платёж приглашённого
    (задача 23). Дефолт 7 — соразмерно win-back промо, окупается первым же платежом."""
    return _num("referral_reward_days", int, 7)


def signal_chart_enabled() -> bool:
    """Прикладывать ли к посту сигнала PNG-карту (свечи + зоны входа/стопа/тейков).
    Дефолт выкл: включается из админки после живой проверки рендера (фича-флаг
    из брифа — новый формат постов сначала за флагом)."""
    return (_raw("signal_chart_enabled") or "0").lower() in _TRUE


def display_leverage() -> float:
    """Плечо для ИЛЛЮСТРАЦИИ результата в постах канала (×N; 0 — строка выключена).
    Реального плеча бот не знает — сделки не исполняет, каждый выбирает своё;
    строка в посте — арифметика «цена ×N», сырое движение цены показывается рядом."""
    v = _raw("display_leverage")
    try:
        lev = float(v) if v is not None else 10.0
    except (TypeError, ValueError):
        lev = 10.0
    return max(lev, 0.0)


def effective() -> dict:
    """Все действующие настройки — для /status и /admin."""
    return {
        "paused": paused(),
        "symbols": symbols(),
        "timeframes": timeframes(),
        "signal_mode": signal_mode(),
        "channel": signals_channel() or "—",
        "channel_enabled": channel_enabled(),
        "channel_mode": channel_mode(),
        "min_confidence": min_confidence(),
        "min_impulse_atr": min_impulse_atr(),
        "enabled_setups": enabled_setups(),
        "price_stars": price_stars(),
        "price_usdt": price_usdt(),
        "sub_days": sub_days(),
        "free_per_week": free_per_week(),
        "cornix_enabled": cornix_block_enabled(),
        "trial_days": trial_days(),
        "display_leverage": display_leverage(),
        "signal_chart_enabled": signal_chart_enabled(),
        "intro_price_stars": intro_price_stars(),
        "intro_hours": intro_hours(),
        "paid_signal_stars": paid_signal_stars(),
        "quarter_price_stars": quarter_price_stars(),
        "year_price_stars": year_price_stars(),
        "lifetime_price_stars": lifetime_price_stars(),
        "watchlist_auto_enabled": watchlist_auto_enabled(),
        "watchlist_size": watchlist_size(),
        "stream_enabled": stream_enabled(),
        "vip_channel_id": vip_channel_id(),
        "vip_invite_link": vip_invite_link(),
    }
