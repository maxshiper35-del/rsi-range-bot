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


def paused_since() -> str | None:
    """С какого момента стоит пауза (updated_at строки settings) или None, если не на паузе."""
    if not paused():
        return None
    row = db.query_one("SELECT updated_at FROM settings WHERE key='paused'")
    return row["updated_at"] if row else None


def shadow_setups() -> set[tuple[str, str]]:
    """Сетапы в ТЕНЕВОМ режиме: {(тег, ТФ)} — сигнал пишется и судится, но не публикуется,
    не исполняется и не входит в публичную стату/гейты. Формат настройки 'LTF-BB@4h,…';
    пустая строка = теней нет. Дефолт LTF-BB@4h (решение владельца 05.10: на паритете минус
    на обеих половинах — −0.052R train / −0.021R OOS; проверка вперёд по
    docs/measurements/2026-10-05-shadow-ltfbb-4h.md)."""
    raw = _raw("shadow_setups")
    raw = "LTF-BB@4h" if raw is None else raw
    out = set()
    for item in _csv(raw):
        tag, _, tf = item.partition("@")
        if tag and tf:
            out.add((tag.strip(), tf.strip()))
    return out


_LATE_ENTRY_GATES = ("off", "stop", "entry")


def late_entry_gate() -> str:
    """Гейт «вход уже пройден» (SMCConfig.late_entry_gate): 'entry' — не публиковать сигнал,
    чей вход рынок уже прошёл к закрытию решающего бара; 'stop' — только уже за стопом;
    'off' — нет. Отсечённый победитель бара пишется в тень (shadow='late_entry').
    Замер 06.10.2026 (docs/measurements/2026-10-06-late-entry-gate.md): 'entry' принят на
    обеих половинах (5 лет M0: TRAIN +2→+252R, OOS +57→+106R; живой журнал — то же)."""
    v = (_raw("late_entry_gate") or "off").strip().lower()
    return v if v in _LATE_ENTRY_GATES else "off"


def backtest_public() -> bool:
    """Показывать ли секцию «Бэктест 5 лет» в публичном дашборде. Дефолт ВЫКЛ (05.10):
    прежний расчёт пропускал стоп на баре входа и мерил выходы по тейку — +622R против
    +76R по правилам живого судьи. Включает владелец после пересборки и решения."""
    return (_raw("backtest_public") or "0").lower() in _TRUE


def last_offsite_at() -> str | None:
    """Когда бэкап последний раз успешно скопирован в iCloud (пишет scripts/backup_db)."""
    return _raw("last_offsite_at")


def last_offsite_alert_at() -> str | None:
    return _raw("last_offsite_alert_at")


def last_overdue_alert_at() -> str | None:
    """Когда последний раз алертили «судья не разрешает сигналы» — троттлинг."""
    return _raw("last_overdue_alert_at")


def last_pause_reminder_at() -> str | None:
    return _raw("last_pause_reminder_at")


def set_last_pause_reminder_at(iso: str) -> None:
    set_value("last_pause_reminder_at", iso)


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


def channel_cta_enabled() -> bool:
    """CTA-кнопка «в личку» под постами канала (bot.channel_cta_markup). Дефолт вкл:
    аудит воронки 12.08.2026 — посты канала были тупиком без единой кнопки."""
    return (_raw("channel_cta_enabled") or "1").lower() in _TRUE


def min_confidence() -> float:
    return _num("min_confidence", float, 0.5)


def min_impulse_atr() -> float:
    return _num("min_impulse_atr", float, 1.0)


def min_rr_open_model() -> float:
    """Мин. reward/risk для сетапа open-SMC — единственного, где entry и take выбираются
    независимо друг от друга (не фикс. доля риска). Без пола сигнал может выйти с riск
    в разы больше вознаграждения (живой пример — сделка #145, RR=0.02)."""
    return _num("min_rr_open_model", float, 0.5)


# Дефолт: 6226 + LTF-BB/LTF-OB, БЕЗ Setup 1:1. Пересмотр 28.07.2026 по 5-летнему
# портфельному прогону (9 символов, 1h+4h, 2021-07..2026-07, живые правила, издержки+фандинг):
#   только 1:1 (прежний дефолт)   4199 сделок  exp +0.029R  Σ  +122R  PF 1.05  DD 82.1R
#   6226 + LTF (текущий)          4325 сделок  exp +0.237R  Σ +1025R  PF 1.40  DD 33.3R
# По отдельности за 5 лет: LTF-BB +0.219R (2643 сделки), 6226 +0.235R (1422), LTF-OB +0.435R
# (260). Прежний вердикт «6226 и LTF стабильно убыточны» опирался на 12 месяцев одного
# режима и на 57 живых сделок LTF-BB, оценённых по СТАРОЙ схеме выхода (фиксированный
# тейк). После перехода на выход только по трейлингу знак меняется: именно эти сетапы дают
# редких дальних победителей, ради которых трейлинг и вводился, а 1:1 их структурно не даёт
# и при этом занимает половину слотов направления. Годовая разбивка положительна во ВСЕХ
# пяти годах, включая медвежий 2022-й (+0.146R), — это не режим одного периода.
_DEFAULT_ENABLED_SETUPS = "6226,LTF"


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


def og_card_last_day() -> str | None:
    """YYYY-MM-DD последнего рендера og-карточки дашборда (бот рисует раз в сутки)."""
    return _raw("og_card_last_day")


def set_og_card_last_day(day: str) -> None:
    set_value("og_card_last_day", day)


def affiliate_url() -> str:
    """Партнёрская ссылка биржи ('' = блоки скрыты) — /affiliate у админа.
    Монетизация без подписки (пункт PR-кита): реф в /about, /start и закрепе."""
    return _raw("affiliate_url") or ""


def exec_report_last_week() -> str | None:
    """ISO-неделя последнего отчёта «исполнение vs журнал» админам (не слать дважды)."""
    return _raw("exec_report_last_week")


def set_exec_report_last_week(week_key: str) -> None:
    set_value("exec_report_last_week", week_key)


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
    if not v:
        return None                         # "" тоже трактуем как «не установлено»
    try:
        return int(v)
    except (TypeError, ValueError):         # битое значение не должно ронять _maintenance
        return None


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
    return _num("last_milestone", int, 0)   # битое значение -> 0, не краш _maintenance


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


def max_open_per_side() -> int:
    """Портфельный лимит направления: максимум одновременных активных сигналов (open,
    включая ждущие лимитки — обе несут риск) в ОДНУ сторону по всему вотчлисту.
    Урок 14.07.2026: 15 одновременных SHORT на коррелированных монетах = одна ставка
    с риском 15R — памп рынка вынес все стопы разом (−12.7R за день). 0 = без лимита."""
    return _num("max_open_per_side", int, 6)


def min_stop_pct() -> float:
    """Мин. дистанция стопа в % от входа — костовый гейт (см. SMCConfig.min_stop_pct).

    Дефолт 1.3 выбран ПО СТОИМОСТИ, а не подгонкой: round-trip (лимитный вход maker +
    стоп taker + проскальзывание) ≈ 0.076% цены, при стопе 1.3% это ≈6% риска сделки.
    Портфельная симуляция 25.07.2026 (9 символов, 1h+4h, живые правила, честная
    OOS-половина янв–июл 2026): без гейта −67.6R (exp −0.051), с гейтом 1.3% +4.8R
    (exp +0.006). Эффект направленно одинаков на всех проверенных срезах.
    0 = выключен."""
    return _num("min_stop_pct", float, 1.3)


def trail_activate_r() -> float:
    """Трейлинг стопа: включается после хода в +N·R. 0 = трейлинга нет (фикс. тейк).

    Дефолт 1.5 по портфельной симуляции 25.07.2026 — единственная схема выхода, которая
    улучшила ЧЕСТНУЮ OOS-половину, и улучшила устойчиво (при активации 1.0/1.5/2.0R):
    OOS exp +0.006R -> +0.043R, просадка 45.2R -> 38.5R, винрейт 40% -> 47%. Проверено
    на другом кэше сигналов (−0.040 -> +0.006) и на пессимистичных taker-издержках
    (−0.022 -> +0.014). Лесенка частичных тейков и стоп-в-безубыток, наоборот, были
    хуже фиксированного тейка в 9 прогонах из 9 — это ДРУГАЯ механика."""
    return _num("trail_activate_r", float, 1.5)


def trail_distance_r() -> float:
    """Дистанция трейлинг-стопа от достигнутого пика, в R. Дефолт 0.2 (было 0.3, изменено
    28.07.2026): при снятом фиксированном тейке (см. trail_only_exit) более тесная подтяжка
    забирает больше, и зависимость МОНОТОННА в обеих независимых выборках — 0.2 < 0.3 < 0.5:
      4 года (2021-07..2025-07):  0.2R +0.247R | 0.3R +0.213R | 0.5R +0.165R
      1 год  (2025-07..2026-07):  0.2R +0.194R | 0.3R +0.178R | 0.5R +0.123R
    Момент активации (trail_activate_r) СОЗНАТЕЛЬНО оставлен на 1.5: там оптимум между
    выборками переворачивается (на 4 годах лучше 1.0R, на 1 годе — 1.5R), то есть разница
    неотличима от шума и менять статус-кво не на чем."""
    return _num("trail_distance_r", float, 0.2)


def executor_enabled() -> bool:
    """Executor V2 (демо BingX): авто-исполнение сигналов на VST-счёте. Дефолт ВЫКЛ.

    v1 работает ТОЛЬКО в песочнице (sandbox зашит в app/v2/bingx_executor.py) — режим
    реальных денег не включается этим флагом в принципе, он потребует отдельного кода
    после гейта живой статистики (сверка 28.08). Executor — ЗЕРКАЛО журнала: сам он
    не принимает торговых решений, только исполняет уже записанные."""
    return (_raw("executor_enabled") or "0").lower() in _TRUE


def executor_risk_pct() -> float:
    """Риск на сделку executor'а в % от баланса VST (демо). Дефолт 1.0."""
    return _num("executor_risk_pct", float, 1.0)


def executor_daily_loss_pct() -> float:
    """Стоп-кран executor: эквити упало за UTC-сутки (от якоря первого такта суток) на этот
    процент и больше -> до 00:00 UTC новые входы не ставятся, ждущие триггеры снимаются,
    открытые позиции ведутся до выхода по журналу. 0 = выключен. Дефолт 5.0: на демо с капом
    номинала 1500 USDT это защита от СБОЯ (зацикленные ордера, аномалия биржи), а не торговое
    правило — обычный плохой день до порога не доходит."""
    return _num("executor_daily_loss_pct", float, 5.0)


def executor_day_anchor() -> str | None:
    """'YYYY-MM-DD|эквити' — эквити на первом такте UTC-суток (база стоп-крана)."""
    return _raw("executor_day_anchor")


def executor_halt_day() -> str | None:
    """UTC-дата, на которую сработал стоп-кран (в другие сутки — не действует)."""
    return _raw("executor_halt_day")


def executor_since() -> str | None:
    """Момент включения executor: исполняются ТОЛЬКО сигналы, созданные ПОСЛЕ него.
    Иначе включение флага бросалось бы догонять старые филлы по уехавшим ценам."""
    return _raw("executor_since")


def set_executor_since(iso: str | None) -> None:
    set_value("executor_since", iso or "")


def trail_only_exit() -> bool:
    """Выход ТОЛЬКО по трейлингу: фиксированный тейк не закрывает позицию.

    Дефолт ВКЛ — самое сильное измеренное улучшение за всю сессию. Причина механическая
    и подтверждается замером самого пака стратегии: до +1R доходят ~50% сделок, а до
    растянутых 2.26R — единицы процентов, то есть фиксированная дальняя цель почти
    никогда не срабатывает, зато режет тех редких победителей, которые могли пройти
    дальше. Портфельный OOS (9 символов, янв–июл 2026, живые правила): +0.043R -> +0.132R
    на сделку, Σ +36.3R -> +107.2R, PF 1.08 -> 1.22. Устойчиво к стрессу: с фандингом
    +0.127R, при двойных издержках +0.087R, с потолком удержания 7 дней +0.122R."""
    return (_raw("trail_only_exit") or "1").lower() in _TRUE


def bot_username() -> str | None:
    """Username бота (пишется ботом при старте) — Mini App строит deep-links."""
    return _raw("bot_username") or None


def trail_max_hold_hours() -> float:
    """Потолок удержания позиции в часах (0 = без потолка). Дефолт 168 (7 дней): без
    фиксированного тейка сделка иначе может висеть неделями (в симуляции до 36 дней),
    занимая слот направления и накапливая фандинг. Цена потолка мала: OOS +0.122R
    против +0.127R без него."""
    return _num("trail_max_hold_hours", float, 168.0)


def time_filter_enabled() -> bool:
    """NY-окно входа для сетапа 6226 (SMCConfig.use_time_filter). Дефолт ВКЛ = статус-кво.

    Посимвольный бэктест говорит «выключить» (6 сравнений из 6, BTC +0.0099 -> +0.2034R),
    НО портфельная симуляция с живыми правилами на OOS-половине этого не подтвердила
    (6 символов: −0.033 против −0.063 без гейта, но +0.013 против +0.028 с костовым
    гейтом — знак эффекта плавает). Посимвольный прогон держит одну позицию за раз и
    поведение живого бота не описывает, поэтому решает портфельный: эффект НЕ доказан,
    статус-кво не меняем. Тумблер оставлен для будущей проверки на живых данных."""
    return (_raw("time_filter_enabled") or "1").lower() in _TRUE


def htf_block_countertrend() -> bool:
    """Жёсткий блок сигналов против ПОДТВЕРЖДЁННОЙ структуры HTF (HH+HL / LH+LL).
    Дефолт задаётся по результату бэктест-гейта (см. коммит с прогоном A/B)."""
    return (_raw("htf_block_countertrend") or "1").lower() in _TRUE


def side_cooldown_stops() -> int:
    """Анти-кластерный тормоз направления: при ≥N стопах одной стороны за окно
    side_cooldown_hours новые сигналы этой стороны не создаются, пока окно не очистится.
    Урок 17.07.2026: max_open_per_side режет одновременную экспозицию, но не
    последовательный перезаход — стопы освобождали слоты, и движок тут же выдавал новые
    LONG в то же сползание BTC (8 стопов за 6 часов, −8R). 0 = выкл."""
    return _num("side_cooldown_stops", int, 3)


def side_cooldown_hours() -> float:
    """Окно анти-кластерного тормоза в часах (скользящее, по closed_at стопов)."""
    return _num("side_cooldown_hours", float, 6.0)


def side_cooldown_alerted() -> str:
    """Стороны, по которым админу уже отправлен алерт о входе в тормоз (CSV LONG,SHORT) —
    алертим один раз на вход в кулдаун, сбрасываем при выходе."""
    return _raw("side_cooldown_alerted") or ""


def set_side_cooldown_alerted(csv_sides: str) -> None:
    set_value("side_cooldown_alerted", csv_sides)


def strategy_epoch() -> str | None:
    """ISO-момент последнего КРУПНОГО обновления стратегии — /stats и /bysetup показывают
    отдельную стату «нового кода», не смешивая её с легаси-бэклогом старых сигналов."""
    return _raw("strategy_epoch")


def referral_reward_days() -> int:
    """Сколько дней подписки получает реферер за реальный первый платёж приглашённого
    (задача 23). Дефолт 7 — соразмерно win-back промо, окупается первым же платежом."""
    return _num("referral_reward_days", int, 7)


def signal_chart_enabled() -> bool:
    """Прикладывать ли к посту сигнала PNG-карту (свечи + зоны входа/стопа/тейков).
    Дефолт выкл: включается из админки после живой проверки рендера (фича-флаг
    из брифа — новый формат постов сначала за флагом)."""
    return (_raw("signal_chart_enabled") or "0").lower() in _TRUE


def lifecycle_chart_enabled() -> bool:
    """Прикладывать ли PNG-карту к постам ЖИЗНЕННОГО ЦИКЛА сделки — «вход исполнен» и
    результат (тейк/стоп). Отдельный от signal_chart флаг: карта сигнала показывает сетап,
    а эти — как отработало. Дефолт ВКЛ (пользователь просил график на филле/тейке); рендерим
    только в full-режиме канала (в teaser карта раскрыла бы скрытые уровни). Сбой рендера —
    откат на текст, пост не теряется."""
    return (_raw("lifecycle_chart_enabled") or "1").lower() in _TRUE


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
        "min_rr_open_model": min_rr_open_model(),
        "max_open_per_side": max_open_per_side(),
        "side_cooldown_stops": side_cooldown_stops(),
        "side_cooldown_hours": side_cooldown_hours(),
        "htf_block_countertrend": htf_block_countertrend(),
        "min_stop_pct": min_stop_pct(),
        "time_filter_enabled": time_filter_enabled(),
        "trail_activate_r": trail_activate_r(),
        "trail_distance_r": trail_distance_r(),
        "trail_only_exit": trail_only_exit(),
        "trail_max_hold_hours": trail_max_hold_hours(),
        "enabled_setups": enabled_setups(),
        "late_entry_gate": late_entry_gate(),
        "price_stars": price_stars(),
        "price_usdt": price_usdt(),
        "sub_days": sub_days(),
        "free_per_week": free_per_week(),
        "cornix_enabled": cornix_block_enabled(),
        "trial_days": trial_days(),
        "display_leverage": display_leverage(),
        "signal_chart_enabled": signal_chart_enabled(),
        "lifecycle_chart_enabled": lifecycle_chart_enabled(),
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
