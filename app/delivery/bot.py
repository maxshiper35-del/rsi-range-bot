"""
bot.py — Telegram-бот (aiogram): /start /stats /calc /subscribe, рассылка по тирам.
Точка входа приложения (make run).

SMC-интеграция реализована чистыми функциями (scan_symbol / format_*), которые
тестируются без aiogram; сам aiogram импортируется лениво в main().
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from app import clock
from app.brain import journal
from app.brain.smc_setups import SMCEngine
from app.brain.risk import calculate_position
from app.core.signal_engine import Signal


# ---------------------------------------------------------------------------
# SMC-конвейер (тестируется без Telegram)
# ---------------------------------------------------------------------------
def scan_frames(symbol: str, frames: dict[str, pd.DataFrame], engine: SMCEngine,
                record: bool = True) -> Optional[Signal]:
    """Прогнать движок по кадрам ТФ; при наличии сигнала записать в журнал. -> Signal|None."""
    sig = engine.analyze(frames, symbol)
    if sig is not None and record:
        journal.record_signal(sig)
    return sig


def scan_auto(symbol: str, frames: dict[str, pd.DataFrame],
              engine: SMCEngine) -> Optional[tuple]:
    """Авто-режим: сетап -> сразу АКТИВНЫЙ сигнал (status=open) в журнал + алерт.

    Возвращает (Signal, signal_id); None — нет сетапа или дубль (тот же импульс)."""
    sig = engine.analyze(frames, symbol)
    if sig is None:
        return None
    if journal.find_duplicate_setup(sig) is not None:
        return None
    sid = journal.record_signal(sig)
    return sig, sid


def scan_semiauto(symbol: str, frames: dict[str, pd.DataFrame],
                  engine: SMCEngine) -> Optional[tuple]:
    """Полуавтомат: движок находит сетап -> записать КАНДИДАТА (не рассылать).

    Возвращает (Signal, signal_id) — оператор подтверждает/отклоняет контекст.
    None — если сетапа нет ЛИБО этот же сетап уже записан (дедуп: не спамим оператора
    тем же импульсом на каждом баре).
    """
    sig = engine.analyze(frames, symbol)
    if sig is None:
        return None
    if journal.find_duplicate_setup(sig) is not None:
        return None
    sid = journal.record_candidate(sig)
    return sig, sid


def scan_watchlist(symbols: list[str], engine: Optional[SMCEngine] = None,
                   timeframes: tuple = ("H1", "M15"), record: bool = True) -> list[Signal]:
    """Обойти watchlist: загрузить кадры, найти сетапы, записать. -> список новых сигналов.

    Загрузка данных вынесена в market_data.load_frames (ccxt). Ошибка по одному символу
    логируется и пропускается — цикл не падает целиком.
    """
    from app.data.market_data import load_frames
    from app.logging_conf import get_logger

    log = get_logger("scan")
    engine = engine or SMCEngine()
    out: list[Signal] = []
    for symbol in symbols:
        try:
            frames = load_frames(symbol, list(timeframes))
            sig = scan_frames(symbol, frames, engine, record=record)
            if sig is not None:
                out.append(sig)
        except Exception as e:                      # noqa: BLE001 — один символ не роняет цикл
            log.warning("scan %s: %s", symbol, e)
    return out


# ---------------------------------------------------------------------------
# Рендер команд
# ---------------------------------------------------------------------------
def format_stats(st: dict) -> str:
    """Текст /stats из journal.stats()."""
    if st["trades"] == 0:
        return ("📊 Статистика пока пустая — сделки ещё не закрывались.\n"
                f"Открытых сигналов: {st['open']}.\n"
                f"{NO_MANUAL_EDITS_LINE}")
    return (
        "📊 Публичная статистика (по закрытым сделкам)\n"
        "────────────────────\n"
        f"Сделок: {st['trades']}  |  открыто: {st['open']}\n"
        f"Winrate: {st['winrate']:.0%}  ({st['wins']}/{st['trades']})\n"
        f"Матожидание: {st['expectancy_r']:+.2f}R на сделку\n"
        f"Суммарно: {st['total_r']:+.2f}R\n"
        "────────────────────\n"
        f"{_small_sample_warning(st['trades'])}"
        f"{NO_MANUAL_EDITS_LINE}\n"
        "⚠️ Не финансовый совет. Прошлые результаты не определяют будущие."
    )


def format_calc(deposit: float, risk_pct: float, entry: float, stop: float,
                max_leverage: int = 20) -> str:
    """Текст /calc: калькулятор позиции через risk.calculate_position."""
    pos = calculate_position(deposit, risk_pct, entry, stop, max_leverage)
    if "error" in pos:
        return f"⚠️ {pos['error']}"
    lines = [
        "🧮 Калькулятор позиции",
        "────────────────────",
        f"Депозит: {deposit:g}  |  риск: {risk_pct:g}%",
        f"Вход: {entry:g}  |  стоп: {stop:g}",
        f"Объём: {pos['position_qty']:g}",
        f"Номинал: {pos['notional_usd']:g}$",
        f"Плечо: ×{pos['implied_leverage']:g}",
        f"Убыток по стопу: {pos['loss_if_stop']:g}$",
    ]
    if pos.get("warning"):
        lines.append(f"⚠️ {pos['warning']}")
    return "\n".join(lines)


def parse_id_arg(text: str) -> Optional[int]:
    """Извлечь id из '/confirm 12' -> 12; None, если аргумента нет или он не число."""
    parts = (text or "").split()
    # isdecimal, не isdigit: '²³' проходят isdigit, но роняют int()
    return int(parts[1]) if len(parts) > 1 and parts[1].isdecimal() else None


def handle_confirm(signal_id: Optional[int]) -> str:
    """Оператор подтвердил контекст кандидата. -> текст ответа."""
    if signal_id is None:
        return "Использование: /confirm <id>"
    if journal.confirm_candidate(signal_id):
        return f"✅ Кандидат #{signal_id} подтверждён — сигнал активен, идёт в журнал."
    return f"⚠️ #{signal_id}: не найден или уже не кандидат."


def handle_reject(signal_id: Optional[int]) -> str:
    """Оператор отклонил кандидата (контекст против)."""
    if signal_id is None:
        return "Использование: /reject <id>"
    if journal.reject_candidate(signal_id):
        return f"🚫 Кандидат #{signal_id} отклонён (теневой исход продолжаем считать)."
    return f"⚠️ #{signal_id}: не найден или уже не кандидат."


def handle_candidates() -> str:
    """Список кандидатов, ожидающих решения оператора."""
    rows = journal.open_candidates()
    if not rows:
        return "Кандидатов нет — жду следующего закрытия свечи."
    return "\n\n".join(
        format_candidate_alert(journal.row_to_signal(r), r["id"]) for r in rows)


def format_report(cs: dict) -> str:
    """Текст /report из journal.cohort_stats(): edge подтверждённых vs отклонённых."""
    def block(name: str, c: dict) -> str:
        if c["trades"] == 0:
            return f"{name}: закрытых сделок нет"
        return (f"{name}: {c['trades']} сд | wr {c['winrate']:.0%} | "
                f"exp {c['expectancy_r']:+.2f}R | Σ {c['total_r']:+.2f}R")

    lines = [
        "🧪 Форвард-эксперимент: человек vs механика",
        "────────────────────",
        block("✅ Подтверждённые", cs["confirmed"]),
        block("🚫 Отклонённые (тень)", cs["rejected"]),
        f"⏳ Ожидают решения: {cs['pending_candidates']}",
        "────────────────────",
        f"Edge оператора: {cs['edge_r']:+.2f}R на сделку "
        f"(подтверждённые − отклонённые)",
    ]
    n_min = min(cs["confirmed"]["trades"], cs["rejected"]["trades"])
    if n_min < 30:
        lines.append(f"⚠️ Выборка мала (min когорта: {n_min} сд) — выводы преждевременны, копим дальше.")
    return "\n".join(lines)


_LINE = "━━━━━━━━━━━━━━"

ANTISCAM_LINE = ("🔒 Мы никогда не пишем первыми в личку с предложениями инвестировать "
                 "и не просим оплату вне этого бота (только кнопки /subscribe).")

NO_MANUAL_EDITS_LINE = "✅ 100% исходов — автоматический скоринг по OHLCV, без ручных правок"


def _small_sample_warning(trades: int) -> str:
    """Предупреждение о статистически малой выборке (порог значимости 100+ сделок)."""
    if trades >= 100:
        return ""
    return (f"⚠️ Выборка пока мала ({trades} закрытых сделок) — для статистической "
            f"значимости нужно 100+. Не делай выводов о точности на этом объёме.\n")


def format_about() -> str:
    """/about — честный трёхчастный дисклеймер + анти-имперсонация (стандарт UK FCA / Bracewell)."""
    return (
        "ℹ️ Как устроен RSI Range Signals\n"
        f"{_LINE}\n"
        "RSI Range Signals — автоматический публикатор технических SMC-сетапов. "
        "Это НЕ инвестиционный совет, НЕ индивидуальная рекомендация и НЕ услуга "
        "управления капиталом — регистрации как CTA/RIA у сервиса нет.\n\n"
        "По собственному walk-forward бэктесту за 12 мес на BTC/ETH/SOL устойчивого "
        "преимущества после издержек не обнаружено (подробнее: /backtest). Сигналы — "
        "инструмент для проверки собственной гипотезы, не обещание результата. "
        "Вся статистика по факту исполнения: /stats — исходы пишутся автоматически, "
        "без ручных правок.\n\n"
        "Формат сигналов совместим с ручной настройкой парсера Cornix — "
        "Entry/Stop-Loss/Take-Profit размечены явно.\n\n"
        "18+. Не предназначено для юрисдикций, где такие услуги запрещены. "
        "Мы никогда не пишем первыми в личку и не просим оплату вне этого бота — "
        "если кто-то пишет от имени RSI Range, это не мы."
    )


def format_backtest_summary() -> str:
    """/backtest — честный итог бэктеста + дисклеймер в духе CFTC Rule 4.41 (статический текст)."""
    return (
        "🧪 Бэктест стратегии — честный итог\n"
        f"{_LINE}\n"
        "Период: 12 месяцев, BTC/ETH/SOL (Bybit perpetuals).\n"
        "Метод: walk-forward без лукахеда — движку подавались только закрытые бары, "
        "комиссии и проскальзывание учтены, при касании стопа и тейка в одном баре "
        "консервативно засчитывался стоп.\n\n"
        "Вывод: устойчивого механического края НЕ обнаружено. Все сетапы после издержек "
        "около нуля или в минусе; лучший кандидат (Setup 1:1 на ETH/15m) фальсифицирован "
        "на генуинной out-of-sample проверке.\n\n"
        "⚠️ Важно: бэктест — это смоделированный расчёт задним числом. Гипотетические "
        "результаты имеют врождённые ограничения: hindsight bias, идеализированное "
        "исполнение, не учитывают реальную ликвидность. В отличие от бэктеста, /stats "
        "считает исходы по фактически опубликованным сигналам — автоматически и без "
        "ручных правок.\n\n"
        "Мы публикуем этот отрицательный результат сознательно: сервис продаёт "
        "инструмент и честную статистику, а не обещание доходности."
    )


def format_glossary() -> str:
    """/glossary — короткий словарь SMC/ICT-терминов из сигналов и постов канала."""
    return (
        "📖 Глоссарий терминов (SMC/ICT)\n"
        f"{_LINE}\n"
        "• BOS (Break of Structure) — пробой предыдущего экстремума ПО тренду: "
        "подтверждение продолжения движения.\n"
        "• CHOCH (Change of Character) — первый пробой ПРОТИВ тренда: возможный разворот.\n"
        "• FVG (Fair Value Gap) — «разрыв» между свечами, куда цена часто возвращается "
        "перед продолжением.\n"
        "• OB (Order Block) — свеча-зона, откуда начался импульс; потенциальная зона "
        "реакции цены.\n"
        "• Свип ликвидности — прокол очевидного максимума/минимума со сбором стопов "
        "и возвратом: часто предшествует развороту.\n"
        "• Killzone — окно повышенной активности (Азия / Лондон / Нью-Йорк), когда "
        "сетапы статистически чаще отрабатывают.\n"
        "• Setup 1:1 — сетап с равным риском и целью (RR 1:1) после свипа и импульса.\n"
        "• LTF-POI — зона интереса на младшем таймфрейме (вход от блока после слома "
        "структуры).\n\n"
        "Эти термины встречаются в блоке «Почему» каждого сигнала."
    )


def _cornix_price(x: float) -> str:
    """Цена фиксированной записью без потери точности: {:g} режет до 6 значащих цифр
    (BTC >100k округляется!) и уходит в экспоненту на мелких альтах — Cornix такое не парсит."""
    return f"{x:.8f}".rstrip("0").rstrip(".")


def format_cornix_block(signal: Signal) -> str:
    """Доп. блок для авто-парсинга Cornix (help.cornix.io/signal-posting-format): открытым
    текстом, без эмодзи рядом с цифрами, TP пронумерованы, полное слово 'Stop-Loss'.
    Добавляется К обычному посту канала, не заменяет его."""
    from app.config import get_settings

    pair = signal.symbol.split(":")[0]
    side = "Long" if signal.side.value == "LONG" else "Short"
    tps = "\n".join(f"Take-Profit {i}: {_cornix_price(t)}"
                    for i, t in enumerate(signal.take_profits, 1))
    exchange = get_settings().exchange_id.capitalize()
    return (
        f"\n\nPair: {pair}\nType: {side}\nEntry: {_cornix_price(signal.entry)}\n"
        f"{tps}\nStop-Loss: {_cornix_price(signal.stop)}\nExchange: {exchange}"
    )


def format_channel_signal(signal: Signal, signal_id: int) -> str:
    """Брендированный пост сигнала для канала (публичная лента)."""
    arrow = "🟢 LONG" if signal.side.value == "LONG" else "🔴 SHORT"
    tps = " / ".join(f"{t:g}" for t in signal.take_profits)
    reasons = "\n".join(f"• {r}" for r in signal.reasons)
    market = (signal.indicators_snapshot or {}).get("market")
    market_line = f"💹 Цена сейчас:  {market:g}\n" if market else ""
    return (
        f"📊 RSI RANGE — Сигнал #{signal_id}\n{_LINE}\n"
        f"{arrow}   {signal.symbol}   ·   {signal.timeframe}\n\n"
        f"{market_line}"
        f"📍 Вход:  {signal.entry:g}  (лимит)\n"
        f"🛑 Стоп:  {signal.stop:g}\n"
        f"🎯 Цели:  {tps}\n"
        f"⚖️ R/R 1:{signal.risk_reward:.1f}    🔎 {int(signal.confidence * 100)}%\n\n"
        f"Почему:\n{reasons}\n{_LINE}\n"
        "⚠️ Не финсовет. Риск 1–2% на сделку.\n"
        "🔔 Сигналы в личку + статистика: @rsirange_bot"
    )


def format_channel_result(c: dict) -> str:
    """Пост-результат закрытой сделки (публичный трек-рекорд)."""
    side = "🟢 LONG" if c["side"] == "LONG" else "🔴 SHORT"
    r = c["result_r"] or 0.0
    if c["status"] == "stopped":
        head, emoji = f"🛑 Сигнал #{c['id']} — стоп", "📉"
    else:
        head, emoji = f"✅ Сигнал #{c['id']} — {c['status'].upper()}", "📈"
    return (
        f"{head}\n{side}   {c['symbol']}   ·   {c['timeframe']}\n"
        f"Результат: {r:+.2f}R  {emoji}\n{_LINE}\n"
        "Вся статистика честно: /stats у @rsirange_bot"
    )


def should_refresh_board(last_update: "str | None", now_iso: str, hours: int = 24) -> bool:
    """Пора ли обновлять живое табло: нет прошлого обновления ИЛИ прошло >= hours."""
    if last_update is None:
        return True
    from datetime import datetime

    delta = datetime.fromisoformat(now_iso) - datetime.fromisoformat(last_update)
    return delta.total_seconds() >= hours * 3600


def format_channel_live_board(st: dict, by_symbol: dict, cohort: "dict | None" = None) -> str:
    """Живое табло для закреплённого поста канала: не winrate (самая манипулируемая метрика
    ниши), а матожидание в R, размер выборки, разбивка по символам, когорты (если semiauto)."""
    lines = [f"📌 RSI RANGE — живое табло\n{_LINE}"]
    if st["trades"] == 0:
        lines.append("Сделки ещё копятся, первые результаты скоро появятся здесь.")
    else:
        lines.append(f"Всего закрыто: {st['trades']}  (открыто {st['open']})")
        lines.append(f"Матожидание: {st['expectancy_r']:+.2f}R на сделку  |  "
                     f"Итог: {st['total_r']:+.2f}R  |  Winrate: {st['winrate']:.0%}")
        warn = _small_sample_warning(st["trades"]).rstrip()
        if warn:
            lines.append(warn)
        if by_symbol:
            lines.append("\nПо инструментам:")
            for symbol, s in sorted(by_symbol.items()):
                lines.append(f"  • {symbol}: {s['trades']} сд, exp {s['expectancy_r']:+.2f}R, "
                             f"итог {s['total_r']:+.2f}R")
    if cohort is not None:
        c, r = cohort["confirmed"], cohort["rejected"]
        lines.append(f"\n🧪 Подтверждённые оператором vs отклонённые (форвард-эксперимент):")
        lines.append(f"  ✅ Подтверждено: {c['trades']} сд, exp {c['expectancy_r']:+.2f}R")
        lines.append(f"  🚫 Отклонено: {r['trades']} сд, exp {r['expectancy_r']:+.2f}R")
        lines.append(f"  Edge оператора: {cohort['edge_r']:+.2f}R")
    lines.append(f"\n{NO_MANUAL_EDITS_LINE}")
    lines.append("Публикуем 100% исходов — включая убыточные.")
    return "\n".join(lines)


def format_channel_stats(st: dict) -> str:
    """Сводка статистики для канала."""
    if st["trades"] == 0:
        return ("📊 RSI RANGE — статистика\nСделки ещё копятся, скоро первые "
                f"результаты.\n{NO_MANUAL_EDITS_LINE}\n🔔 @rsirange_bot")
    return (
        f"📊 RSI RANGE — статистика\n{_LINE}\n"
        f"Закрыто сделок: {st['trades']}  (открыто {st['open']})\n"
        f"Winrate: {st['winrate']:.0%}\n"
        f"Матожидание: {st['expectancy_r']:+.2f}R на сделку\n"
        f"Итог: {st['total_r']:+.2f}R\n{_LINE}\n"
        f"{_small_sample_warning(st['trades'])}"
        f"{NO_MANUAL_EDITS_LINE}\n"
        "🔔 Сигналы: @rsirange_bot"
    )


USER_COMMANDS = [
    ("start", "начать / перезапустить бота"),
    ("help", "список команд"),
    ("stats", "публичная статистика (честный журнал)"),
    ("calc", "калькулятор позиции: /calc <деп> <риск%> <вход> <стоп>"),
    ("subscribe", "подписка — все сигналы"),
    ("about", "как устроен сервис, честный дисклеймер"),
    ("backtest", "честный итог бэктеста стратегии"),
    ("glossary", "словарь терминов SMC (BOS, FVG, killzone…)"),
]

ADMIN_COMMANDS = USER_COMMANDS + [
    ("admin", "панель управления"),
    ("status", "снимок всех настроек"),
    ("pause", "остановить поиск сигналов"),
    ("resume", "возобновить поиск сигналов"),
    ("symbols", "список торгуемых символов"),
    ("addsymbol", "добавить символ: /addsymbol BTC/USDT:USDT"),
    ("delsymbol", "убрать символ"),
    ("setmode", "auto|semiauto — режим сигналов"),
    ("setups", "включённые сетапы стратегии"),
    ("minconf", "порог уверенности сетапа (0..1)"),
    ("minimpulse", "мин. размер импульса в ATR"),
    ("channel", "on|off|<id> — канал публикации"),
    ("channelmode", "full|teaser — режим постов канала"),
    ("cornix", "on|off — Cornix-совместимый блок в постах"),
    ("post", "разовая сводка статистики в канал"),
    ("price", "stars|usdt|days <N> — цена подписки"),
    ("freeweek", "лимит бесплатных сигналов/нед"),
    ("usdtlink", "ссылка на ручной USDT-счёт"),
    ("cryptobottoken", "API-токен Crypto Pay — вкл. авто-USDT"),
    ("usdtbonus", "бонус-дней за оплату в USDT"),
    ("grant", "выдать подписку: /grant <tg_id> [дней]"),
    ("revoke", "снять подписку: /revoke <tg_id>"),
    ("users", "счётчики юзеров"),
    ("announce", "объявление в канал + подписчикам"),
    ("confirm", "подтвердить кандидата (semiauto): /confirm <id>"),
    ("reject", "отклонить кандидата (semiauto): /reject <id>"),
    ("candidates", "кандидаты, ждущие решения (semiauto)"),
    ("report", "edge оператора: confirmed vs rejected (semiauto)"),
]


async def register_bot_commands(bot, admin_ids) -> None:
    """Зарегистрировать меню команд в Telegram (кнопка '/'): общее для всех + расширенное
    для админов (BotCommandScopeChat). Без этого вызова getMyCommands пуст и меню не
    показывает ничего, хотя сами хэндлеры работают по прямому вводу текста."""
    from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

    await bot.set_my_commands(
        [BotCommand(command=c, description=d) for c, d in USER_COMMANDS],
        scope=BotCommandScopeDefault())
    for admin_id in admin_ids:
        try:
            await bot.set_my_commands(
                [BotCommand(command=c, description=d) for c, d in ADMIN_COMMANDS],
                scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:                              # noqa: BLE001 — админ не открывал чат с ботом
            pass


def format_admin_status(eff: dict, users: dict) -> str:
    """Статус управления для /admin и /status."""
    return (
        "🛠 Управление RSI Range\n"
        f"{'⏸ НА ПАУЗЕ' if eff['paused'] else '▶️ РАБОТАЕТ'}\n"
        "────────────────────\n"
        f"Символы: {', '.join(eff['symbols']) or '—'}\n"
        f"ТФ: {', '.join(eff['timeframes'])}\n"
        f"Режим сигналов: {eff['signal_mode']}\n"
        f"Сетапы: {', '.join(eff['enabled_setups']) or '—'}\n"
        f"Мин. уверенность: {eff['min_confidence']:.2f}  ·  мин. импульс: {eff['min_impulse_atr']:.1f}×ATR\n"
        f"Канал: {eff['channel']} "
        f"({'вкл' if eff['channel_enabled'] else 'выкл'} · {eff['channel_mode']})\n"
        f"Цена: {eff['price_stars']}⭐ / {eff['price_usdt']:g} USDT / {eff['sub_days']} дн\n"
        f"Free-лимит: {eff['free_per_week']}/нед\n"
        f"Юзеров: {users['total']} (платных {users['paid']})"
    )


def format_teaser(signal: Signal, signal_id: int) -> str:
    """Превью сигнала для free-юзера сверх недельного лимита (без уровней входа)."""
    arrow = "🟢 LONG" if signal.side.value == "LONG" else "🔴 SHORT"
    return (
        f"🔒 СИГНАЛ #{signal_id} — превью\n"
        f"{arrow}  {signal.symbol}  ({signal.timeframe})\n"
        "Вход, стоп, тейки и разбор — по подписке.\n"
        "/subscribe — открыть все сигналы"
    )


def format_candidate_alert(signal: Signal, signal_id: int) -> str:
    """Алерт оператору по кандидату: механический контекст для подтверждения дискреции."""
    from datetime import datetime

    from app.brain.smc_time import active_windows

    try:
        kz = active_windows(datetime.fromisoformat(signal.created_at))
    except Exception:                               # noqa: BLE001
        kz = []
    arrow = "🟢 LONG" if signal.side.value == "LONG" else "🔴 SHORT"
    setup = (signal.indicators_snapshot or {}).get("setup", "?")
    tps = ", ".join(f"{t:g}" for t in signal.take_profits)
    lines = [
        f"🕵️ КАНДИДАТ #{signal_id} — проверь контекст",
        f"{arrow}  {signal.symbol} ({signal.timeframe})  сетап: {setup}",
        f"Вход {signal.entry:g} | Стоп {signal.stop:g} | Тейк {tps} | RR 1:{signal.risk_reward:.1f}",
        f"Механ. уверенность: {int(signal.confidence * 100)}%  |  "
        f"killzone: {', '.join(kz) if kz else 'вне окна'}",
        "Механика:",
        *[f"  — {r}" for r in signal.reasons],
        "",
        f"❓ Контекст ОК (тренд HTF, качество доставки)?  /confirm {signal_id}  или  /reject {signal_id}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Точка входа (aiogram — ленивый импорт)
# ---------------------------------------------------------------------------
def main() -> None:
    """Поднять бота: при старте прогнать миграции, зарегистрировать хэндлеры, запустить polling."""
    import asyncio

    from aiogram import Bot, Dispatcher  # ленивый импорт тяжёлой зависимости
    from aiogram.filters import Command

    from app import db
    from app.config import get_settings
    from aiogram import F
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice

    from app import runtime
    from app.delivery import payments
    from app.logging_conf import get_logger

    log = get_logger("bot")
    db.init_db()
    cfg = get_settings()
    bot = Bot(token=cfg.tg_bot_token)
    dp = Dispatcher()

    def _subscribe_kb() -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(
            text=f"⭐ {runtime.price_stars()} Stars / {runtime.sub_days()} дн",
            callback_data="pay_stars")]]
        if payments.cryptobot_enabled():                # авто через Crypto Pay API
            rows.append([InlineKeyboardButton(
                text=f"💵 {runtime.price_usdt():g} USDT / {runtime.sub_days()} дн",
                callback_data="pay_usdt")])
        elif runtime.usdt_invoice_url():                # полу-ручной: ссылка на счёт + «Я оплатил»
            rows.append([InlineKeyboardButton(
                text=f"💵 Оплатить USDT (от {runtime.price_usdt():g}$)",
                url=runtime.usdt_invoice_url())])
            rows.append([InlineKeyboardButton(
                text="✅ Я оплатил USDT", callback_data="usdt_claim")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @dp.message(Command("start"))
    async def _start(m):
        await asyncio.to_thread(payments.register_user, m.from_user.id, m.from_user.username)
        # deep-link атрибуция: /start src_<label> (first-touch, ref_ добавится отдельно)
        parts = (m.text or "").split()
        if len(parts) > 1 and parts[1].startswith("src_") and len(parts[1]) > 4:
            await asyncio.to_thread(payments.set_acquisition_source,
                                    m.from_user.id, parts[1][4:])
        await m.answer(
            "👋 RSI Range Signals — инструмент и честная статистика (не «прибыль 400%»).\n"
            "Команды: /stats — статистика, /calc — калькулятор позиции, /subscribe — подписка.\n"
            "/about — как устроен сервис и честный дисклеймер, /backtest — честный итог бэктеста.\n"
            "Не знаком с терминами SMC? /glossary — коротко объясним.\n"
            f"{ANTISCAM_LINE}\n"
            "⚠️ Не финансовый совет. Риск на сделку 1–2%.")

    @dp.message(Command("about"))
    async def _about(m):
        await m.answer(format_about())

    @dp.message(Command("backtest"))
    async def _backtest(m):
        await m.answer(format_backtest_summary())

    @dp.message(Command("glossary"))
    async def _glossary(m):
        await m.answer(format_glossary())

    @dp.message(Command("subscribe"))
    async def _subscribe(m):
        await asyncio.to_thread(payments.register_user, m.from_user.id, m.from_user.username)
        text = await asyncio.to_thread(payments.subscription_status_text, m.from_user.id)
        if runtime.usdt_invoice_url() and not payments.cryptobot_enabled():
            uname = m.from_user.username or m.from_user.id
            text += (f"\n\n💵 USDT: оплати по кнопке, в КОММЕНТАРИИ к платежу укажи «{uname}», "
                     "затем нажми «Я оплатил» — активируем в течение дня.")
        await m.answer(text, reply_markup=_subscribe_kb())

    @dp.callback_query(F.data == "pay_stars")
    async def _pay_stars(cq):
        days, stars = runtime.sub_days(), runtime.price_stars()
        await bot.send_invoice(
            chat_id=cq.from_user.id,
            title="Подписка RSI Range Signals",
            description=f"Доступ ко всем сигналам на {days} дней.",
            payload=f"sub:{days}",
            provider_token="",                          # Stars: провайдер не нужен
            currency="XTR",
            prices=[LabeledPrice(label=f"{days} дней доступа", amount=stars)],
        )
        await cq.answer()

    @dp.callback_query(F.data == "pay_usdt")
    async def _pay_usdt(cq):
        try:
            res = await asyncio.to_thread(payments.create_cryptobot_invoice,
                                          cq.from_user.id, runtime.sub_days())
        except Exception as e:                         # noqa: BLE001 — сеть/невалидный токен
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
            "Счёт на оплату в USDT создан. После оплаты подписка активируется "
            f"автоматически (проверка раз в ~минуту).{bonus_line}", reply_markup=kb)
        await cq.answer()

    @dp.callback_query(F.data == "usdt_claim")
    async def _usdt_claim(cq):
        u = cq.from_user
        await asyncio.to_thread(payments.register_user, u.id, u.username)
        # заявка пишется ДО уведомлений: иначе мгновенный /grant по пингу закрывает
        # 0 заявок, а заявка создаётся после — и висит открытой до ложной эскалации
        await asyncio.to_thread(payments.record_usdt_claim, u.id)
        for admin_id in cfg.admin_ids:
            try:
                await bot.send_message(
                    admin_id,
                    f"💵 Заявка на оплату USDT\n"
                    f"Юзер: @{u.username or '—'} (id {u.id})\n"
                    f"Проверь платёж в @CryptoBot и выдай: /grant {u.id}")
            except Exception as e:                     # noqa: BLE001
                log.warning("usdt claim notify %s: %s", admin_id, e)
        await cq.answer("Спасибо! Обычно подтверждаем в течение нескольких часов "
                        "(максимум — сутки).", show_alert=True)

    @dp.message(Command("calc"))
    async def _calc(m):
        parts = (m.text or "").split()
        if len(parts) != 5:
            await m.answer("🧮 Калькулятор позиции\nФормат: /calc <депозит> <риск%> <вход> <стоп>\n"
                           "Пример: /calc 1000 2 62340 62980")
            return
        try:
            dep, risk, entry, stop = (float(x.replace(",", ".")) for x in parts[1:5])
        except ValueError:
            await m.answer("Не понял числа. Пример: /calc 1000 2 62340 62980")
            return
        await m.answer(await asyncio.to_thread(format_calc, dep, risk, entry, stop, cfg.max_leverage))

    @dp.message(Command("help"))
    async def _help(m):
        base = ("Команды:\n"
                "/stats — публичная статистика\n"
                "/calc <деп> <риск%> <вход> <стоп> — калькулятор позиции\n"
                "/subscribe — подписка (все сигналы)\n"
                "/about — как устроен сервис и честный дисклеймер\n"
                "/backtest — честный итог бэктеста стратегии\n"
                "/glossary — словарь терминов SMC\n"
                f"{ANTISCAM_LINE}\n"
                "⚠️ Не финансовый совет. Риск на сделку 1–2%.")
        if _is_admin(m):
            base += "\n\n🛠 Админ: /admin — панель управления (пауза, символы, цены, канал, подписчики)"
        await m.answer(base)

    @dp.my_chat_member()
    async def _added_to_chat(event):
        # бота добавили/повысили в канале -> прислать админу ID для SIGNALS_CHANNEL
        chat = event.chat
        status = event.new_chat_member.status
        if chat.type == "channel" and status in ("administrator", "creator"):
            for admin_id in cfg.admin_ids:
                try:
                    await bot.send_message(
                        admin_id,
                        f"📢 Бот стал админом канала «{chat.title}».\n"
                        f"ID канала: <code>{chat.id}</code>\n\n"
                        f"Чтобы постить сигналы сюда — пришли мне этот ID "
                        f"(или добавь в .env: SIGNALS_CHANNEL={chat.id}).",
                        parse_mode="HTML")
                except Exception as e:                 # noqa: BLE001
                    log.warning("notify admin channel-id %s: %s", admin_id, e)

    @dp.pre_checkout_query()
    async def _pre_checkout(q):
        await q.answer(ok=True)

    @dp.message(F.successful_payment)
    async def _paid(m):
        sp = m.successful_payment
        days = cfg.sub_days
        try:
            days = int(sp.invoice_payload.split(":")[1])
        except Exception:                               # noqa: BLE001
            pass
        applied = await asyncio.to_thread(
            payments.grant_subscription, m.from_user.id, "stars",
            float(sp.total_amount), sp.currency, days, sp.telegram_payment_charge_id)
        if applied:
            await m.answer(f"✅ Подписка активна на {days} дней! Сигналы теперь приходят "
                           f"автоматически на закрытии свечей. Статус: /subscribe")
        else:
            await m.answer("Этот платёж уже был учтён. Спасибо! Статус: /subscribe")

    @dp.message(Command("stats"))
    async def _stats(m):
        await m.answer(await asyncio.to_thread(lambda: format_stats(journal.stats())))

    def _is_admin(m) -> bool:
        return bool(m.from_user) and m.from_user.id in cfg.admin_ids

    @dp.message(Command("confirm"))
    async def _confirm(m):
        if not _is_admin(m):
            return
        await m.answer(await asyncio.to_thread(handle_confirm, parse_id_arg(m.text)))

    @dp.message(Command("reject"))
    async def _reject(m):
        if not _is_admin(m):
            return
        await m.answer(await asyncio.to_thread(handle_reject, parse_id_arg(m.text)))

    @dp.message(Command("candidates"))
    async def _candidates(m):
        if not _is_admin(m):
            return
        await m.answer(await asyncio.to_thread(handle_candidates))

    @dp.message(Command("report"))
    async def _report(m):
        if not _is_admin(m):
            return
        await m.answer(await asyncio.to_thread(lambda: format_report(journal.cohort_stats())))

    @dp.message(Command("post"))
    async def _post(m):
        # админ: вручную опубликовать сводку статистики в канал
        if not _is_admin(m):
            return
        if not cfg.signals_channel:
            await m.answer("Канал не задан (SIGNALS_CHANNEL).")
            return
        text = await asyncio.to_thread(lambda: format_channel_stats(journal.stats()))
        try:
            await bot.send_message(runtime.signals_channel(), text)
            await m.answer("Сводка опубликована в канал.")
        except Exception as e:                         # noqa: BLE001
            await m.answer(f"Не удалось опубликовать: {e}")

    # ======================= ПОЛНОЕ УПРАВЛЕНИЕ (админ) =======================
    def _admin_kb(eff: dict) -> InlineKeyboardMarkup:
        b = InlineKeyboardButton
        return InlineKeyboardMarkup(inline_keyboard=[
            [b(text="▶️ Возобновить" if eff["paused"] else "⏸ Пауза",
               callback_data="adm_pausetoggle")],
            [b(text=f"📢 Канал: {eff['channel_mode']}", callback_data="adm_chmode"),
             b(text="🔕 Выкл канал" if eff["channel_enabled"] else "🔔 Вкл канал",
               callback_data="adm_chtoggle")],
            [b(text="📊 Пост статы в канал", callback_data="adm_poststats"),
             b(text="🔄 Обновить", callback_data="adm_refresh")],
        ])

    async def _panel(chat_id):
        eff = await asyncio.to_thread(runtime.effective)
        users = await asyncio.to_thread(payments.user_counts)
        await bot.send_message(chat_id, format_admin_status(eff, users), reply_markup=_admin_kb(eff))

    async def _set(m, key, value, reply):
        await asyncio.to_thread(runtime.set_value, key, value)
        await m.answer(reply)

    @dp.message(Command("admin"))
    async def _admin(m):
        if _is_admin(m):
            await _panel(m.chat.id)

    @dp.message(Command("status"))
    async def _statuscmd(m):
        if not _is_admin(m):
            return
        eff = await asyncio.to_thread(runtime.effective)
        users = await asyncio.to_thread(payments.user_counts)
        await m.answer(format_admin_status(eff, users))

    @dp.message(Command("pause"))
    async def _pause(m):
        if _is_admin(m):
            await _set(m, "paused", "1", "⏸ Сигналы на паузе.")

    @dp.message(Command("resume"))
    async def _resume(m):
        if _is_admin(m):
            await _set(m, "paused", "0", "▶️ Сигналы возобновлены.")

    @dp.message(Command("symbols"))
    async def _symbols(m):
        if not _is_admin(m):
            return
        syms = await asyncio.to_thread(runtime.symbols)
        await m.answer("Символы: " + (", ".join(syms) or "—") +
                       "\n/addsymbol <symbol> · /delsymbol <symbol>")

    @dp.message(Command("addsymbol"))
    async def _addsymbol(m):
        if not _is_admin(m):
            return
        parts = (m.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await m.answer("Формат: /addsymbol BTC/USDT:USDT")
            return
        syms = await asyncio.to_thread(runtime.symbols)
        if parts[1].strip() not in syms:
            syms.append(parts[1].strip())
        await _set(m, "symbols", ",".join(syms), "Символы: " + ", ".join(syms))

    @dp.message(Command("delsymbol"))
    async def _delsymbol(m):
        if not _is_admin(m):
            return
        parts = (m.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await m.answer("Формат: /delsymbol BTC/USDT:USDT")
            return
        syms = [s for s in await asyncio.to_thread(runtime.symbols) if s != parts[1].strip()]
        await _set(m, "symbols", ",".join(syms), "Символы: " + (", ".join(syms) or "—"))

    @dp.message(Command("setmode"))
    async def _setmode(m):
        if not _is_admin(m):
            return
        parts = (m.text or "").split()
        if len(parts) < 2 or parts[1] not in ("auto", "semiauto"):
            await m.answer("Формат: /setmode auto | semiauto")
            return
        await _set(m, "signal_mode", parts[1], f"Режим сигналов: {parts[1]}")

    @dp.message(Command("channel"))
    async def _channelcmd(m):
        if not _is_admin(m):
            return
        parts = (m.text or "").split()
        if len(parts) >= 2 and parts[1] in ("on", "off"):
            await _set(m, "channel_enabled", "1" if parts[1] == "on" else "0",
                       f"Канал: {'вкл' if parts[1] == 'on' else 'выкл'}")
        elif len(parts) >= 2:
            await _set(m, "signals_channel", parts[1], f"Канал задан: {parts[1]}")
        else:
            await m.answer("Формат: /channel on|off  или  /channel <@username|id>")

    @dp.message(Command("cryptobottoken"))
    async def _cryptobottoken(m):
        if not _is_admin(m):
            return
        parts = (m.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await m.answer("Формат: /cryptobottoken <API-токен Crypto Pay>  ·  очистить: /cryptobottoken -")
            return
        token = parts[1].strip()
        if token == "-":
            await _set(m, "cryptobot_token", "", "Токен CryptoBot очищен — авто-USDT выключен.")
        else:
            # токен не эхоим целиком — только хвост
            await _set(m, "cryptobot_token", token,
                       f"Токен сохранён (…{token[-4:]}). Авто-USDT включён — проверь /subscribe.")

    @dp.message(Command("usdtbonus"))
    async def _usdtbonus(m):
        if not _is_admin(m):
            return
        parts = (m.text or "").split()
        if len(parts) < 2 or not parts[1].isdecimal():
            await m.answer("Формат: /usdtbonus 2 (бонус-дней за оплату USDT; 0 = выключить)")
            return
        await _set(m, "usdt_bonus_days", parts[1], f"Бонус за USDT: +{parts[1]} дн.")

    @dp.message(Command("cornix"))
    async def _cornix(m):
        if not _is_admin(m):
            return
        parts = (m.text or "").split()
        if len(parts) < 2 or parts[1] not in ("on", "off"):
            await m.answer("Формат: /cornix on|off — Cornix-блок в постах канала.\n"
                           "⚠️ Перед on: ручная проверка формата в тестовой группе с реальным Cornix.")
            return
        await _set(m, "cornix_block_enabled", "1" if parts[1] == "on" else "0",
                   f"Cornix-блок: {'вкл' if parts[1] == 'on' else 'выкл'}")

    @dp.message(Command("usdtlink"))
    async def _usdtlink(m):
        if not _is_admin(m):
            return
        parts = (m.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await m.answer("Формат: /usdtlink <ссылка счёта CryptoBot>  ·  очистить: /usdtlink -")
            return
        url = parts[1].strip()
        await _set(m, "usdt_invoice_url", "" if url == "-" else url,
                   "USDT-ссылка обновлена." if url != "-" else "USDT-ссылка очищена.")

    @dp.message(Command("channelmode"))
    async def _channelmode(m):
        if not _is_admin(m):
            return
        parts = (m.text or "").split()
        if len(parts) < 2 or parts[1] not in ("full", "teaser"):
            await m.answer("Формат: /channelmode full | teaser")
            return
        await _set(m, "channel_mode", parts[1], f"Режим канала: {parts[1]}")

    @dp.message(Command("price"))
    async def _price(m):
        if not _is_admin(m):
            return
        parts = (m.text or "").split()
        keys = {"stars": "sub_price_stars", "usdt": "sub_price_usdt", "days": "sub_days"}
        if len(parts) == 3 and parts[1] in keys:
            await _set(m, keys[parts[1]], parts[2], f"{parts[1]} = {parts[2]}")
        else:
            await m.answer("Формат: /price stars 1000 | /price usdt 20 | /price days 30")

    @dp.message(Command("minconf"))
    async def _minconf(m):
        if not _is_admin(m):
            return
        parts = (m.text or "").split()
        try:
            v = float(parts[1])
            assert 0.0 <= v <= 1.0
        except Exception:                              # noqa: BLE001
            await m.answer("Формат: /minconf 0.6 (0..1)")
            return
        await _set(m, "min_confidence", v, f"Мин. уверенность: {v}")

    @dp.message(Command("setups"))
    async def _setups(m):
        if not _is_admin(m):
            return
        parts = (m.text or "").split(maxsplit=1)
        known = ("6226", "1:1", "open-SMC", "LTF")
        if len(parts) < 2:
            cur = ", ".join(await asyncio.to_thread(runtime.enabled_setups))
            await m.answer(
                f"Включённые сетапы: {cur or '—'}\n"
                f"Доступны: {', '.join(known)} (LTF покрывает LTF-BB и LTF-OB)\n"
                "Формат: /setups 1:1,6226  ·  /setups all — включить все\n\n"
                "⚠️ Честный бэктест (12 мес, BTC/ETH/SOL) показал 6226 и LTF стабильно "
                "убыточными после издержек — по умолчанию включён только 1:1.")
            return
        value = "6226,1:1,open-SMC,LTF" if parts[1].strip().lower() == "all" else parts[1].strip()
        await _set(m, "enabled_setups", value, f"Включённые сетапы: {value}")

    @dp.message(Command("minimpulse"))
    async def _minimpulse(m):
        if not _is_admin(m):
            return
        parts = (m.text or "").split()
        try:
            v = float(parts[1])
            assert v >= 0
        except Exception:                              # noqa: BLE001
            await m.answer("Формат: /minimpulse 1.5 (мин. размер импульса в ATR; больше = реже, но крупнее структура)")
            return
        await _set(m, "min_impulse_atr", v, f"Мин. импульс: {v}×ATR")

    @dp.message(Command("freeweek"))
    async def _freeweek(m):
        if not _is_admin(m):
            return
        parts = (m.text or "").split()
        if len(parts) < 2 or not parts[1].isdecimal():
            await m.answer("Формат: /freeweek 3")
            return
        await _set(m, "free_signals_per_week", parts[1], f"Free-лимит: {parts[1]}/нед")

    @dp.message(Command("grant"))
    async def _grant(m):
        if not _is_admin(m):
            return
        parts = (m.text or "").split()
        if len(parts) < 2 or not parts[1].isdecimal():
            await m.answer("Формат: /grant <tg_id> [дней]")
            return
        tg = int(parts[1])
        days = int(parts[2]) if len(parts) > 2 and parts[2].isdecimal() else runtime.sub_days()
        await asyncio.to_thread(payments.admin_grant, tg, days)
        await m.answer(f"✅ Подписка выдана {tg} на {days} дн.")

    @dp.message(Command("revoke"))
    async def _revoke(m):
        if not _is_admin(m):
            return
        parts = (m.text or "").split()
        if len(parts) < 2 or not parts[1].isdecimal():
            await m.answer("Формат: /revoke <tg_id>")
            return
        await asyncio.to_thread(payments.admin_revoke, int(parts[1]))
        await m.answer(f"🚫 Подписка {parts[1]} снята.")

    @dp.message(Command("users"))
    async def _users(m):
        if not _is_admin(m):
            return
        u = await asyncio.to_thread(payments.user_counts)
        await m.answer(f"Юзеров: {u['total']}, платных: {u['paid']}")

    @dp.message(Command("announce"))
    async def _announce(m):
        if not _is_admin(m):
            return
        parts = (m.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await m.answer("Формат: /announce <текст> — пошлю в канал и подписчикам")
            return
        text, n = parts[1], 0
        channel = runtime.signals_channel()
        if channel and runtime.channel_enabled():
            try:
                await bot.send_message(channel, text)
                n += 1
            except Exception as e:                     # noqa: BLE001
                log.warning("announce channel: %s", e)
        for uid in await asyncio.to_thread(payments.active_subscriber_ids):
            try:
                await bot.send_message(uid, text)
                n += 1
            except Exception as e:                     # noqa: BLE001
                log.warning("announce %s: %s", uid, e)
        await m.answer(f"📣 Отправлено получателям: {n}")

    @dp.callback_query(F.data.startswith("adm_"))
    async def _admin_cb(cq):
        if cq.from_user.id not in cfg.admin_ids:
            await cq.answer()
            return
        data = cq.data
        if data == "adm_pausetoggle":
            await asyncio.to_thread(runtime.set_value, "paused",
                                    "0" if runtime.paused() else "1")
        elif data == "adm_chmode":
            await asyncio.to_thread(runtime.set_value, "channel_mode",
                                    "teaser" if runtime.channel_mode() == "full" else "full")
        elif data == "adm_chtoggle":
            await asyncio.to_thread(runtime.set_value, "channel_enabled",
                                    "0" if runtime.channel_enabled() else "1")
        elif data == "adm_poststats":
            ch = runtime.signals_channel()
            if ch:
                try:
                    await bot.send_message(ch, format_channel_stats(journal.stats()))
                except Exception as e:                 # noqa: BLE001
                    log.warning("poststats: %s", e)
        eff = await asyncio.to_thread(runtime.effective)
        users = await asyncio.to_thread(payments.user_counts)
        try:
            await cq.message.edit_text(format_admin_status(eff, users), reply_markup=_admin_kb(eff))
        except Exception:                              # noqa: BLE001 — «message not modified» и т.п.
            pass
        await cq.answer("Готово")

    async def _broadcast(sig, sid):
        try:
            if cfg.signal_mode == "auto":
                full = f"🔔 СИГНАЛ #{sid}\n\n" + sig.format_message()
            else:
                full = format_candidate_alert(sig, sid)
        except Exception:                              # noqa: BLE001 — формат не роняет цикл
            log.exception("формат алерта #%s", sid)
            full = f"🔔 Сигнал #{sid} (ошибка форматирования — детали в журнале)"
        teaser = format_teaser(sig, sid)
        # пост в канал (если включён) — полный или тизер, по режиму канала
        channel = runtime.signals_channel()
        if channel and runtime.channel_enabled():
            if runtime.channel_mode() == "full":
                ch_text = format_channel_signal(sig, sid)
                if runtime.cornix_block_enabled():      # машиночитаемый блок для Cornix-юзеров
                    ch_text += format_cornix_block(sig)
            else:
                ch_text = teaser
            try:
                await bot.send_message(channel, ch_text)
            except Exception as e:                     # noqa: BLE001 — канал не роняет рассылку
                log.warning("пост в канал %s: %s", channel, e)
        # воронка: админы/платные -> полный; free -> полный до лимита/нед, дальше тизер
        plan = await asyncio.to_thread(payments.plan_recipients, sid,
                                       runtime.free_per_week(), tuple(cfg.admin_ids))
        sent = 0
        for uid, kind in plan:
            try:
                await bot.send_message(uid, full if kind == "full" else teaser)
                await asyncio.to_thread(payments.record_delivery, uid, sid, kind)
                sent += 1
            except Exception as e:                     # noqa: BLE001 — один получатель не роняет рассылку
                log.warning("send %s: %s", uid, e)
        log.info("сигнал #%s: отправлено %d из %d (план)", sid, sent, len(plan))

    async def _maintenance():
        # истёкшие подписки -> free + уведомление
        for uid in await asyncio.to_thread(payments.expire_overdue):
            try:
                await bot.send_message(uid, "⌛ Подписка истекла. /subscribe — продлить и снова "
                                            "получать все сигналы.")
            except Exception as e:                     # noqa: BLE001
                log.warning("expire notify %s: %s", uid, e)
        # напоминания о продлении (за 3 дня, единожды)
        for row in await asyncio.to_thread(payments.due_for_reminder, 3):
            try:
                await bot.send_message(row["tg_id"], f"⏳ Подписка истекает {row['paid_until'][:10]}. "
                                                     "/subscribe — продлить.")
                await asyncio.to_thread(payments.mark_reminded, row["tg_id"])
            except Exception as e:                     # noqa: BLE001
                log.warning("reminder %s: %s", row["tg_id"], e)
        # автоактивация оплаченных USDT-инвойсов (уведомления изолированы per-юзер)
        try:
            activated = await asyncio.to_thread(payments.poll_cryptobot)
        except Exception as e:                         # noqa: BLE001
            log.warning("poll_cryptobot: %s", e)
            activated = []
        for uid in activated:
            try:
                await bot.send_message(uid, "✅ Оплата USDT получена — подписка активна! /subscribe")
            except Exception as e:                     # noqa: BLE001
                log.warning("usdt activate notify %s: %s", uid, e)
        # эскалация зависших USDT-заявок (>6ч без /grant): пингуем один раз;
        # метка ставится безусловно — сбой доставки одному админу не даёт вечный повтор
        for claim in await asyncio.to_thread(payments.stale_usdt_claims, 6):
            for admin_id in cfg.admin_ids:
                try:
                    await bot.send_message(
                        admin_id,
                        f"⏰ Заявка на оплату USDT от {claim['tg_id']} висит без ответа >6ч — "
                        f"/grant {claim['tg_id']}")
                except Exception as e:                 # noqa: BLE001 — один админ не блокирует метку
                    log.warning("escalate claim #%s -> %s: %s", claim["id"], admin_id, e)
            try:
                await asyncio.to_thread(payments.mark_claim_escalated, claim["id"])
            except Exception as e:                     # noqa: BLE001
                log.warning("mark_claim_escalated #%s: %s", claim["id"], e)
        # win-back: одно напоминание в первые 2 дня после истечения подписки
        for row in await asyncio.to_thread(payments.due_for_winback, 2):
            try:
                await bot.send_message(row["tg_id"],
                                       "👋 Соскучились по сигналам? /subscribe — вернуть доступ "
                                       "за минуту.")
                await asyncio.to_thread(payments.mark_winback_reminded, row["tg_id"])
            except Exception as e:                     # noqa: BLE001
                log.warning("winback %s: %s", row["tg_id"], e)
        # живое табло: авто-обновляемый закреплённый пост статистики в канале (раз в 24ч).
        # Состояние (live_board_message_id) намеренно синхронизировано с ФАКТИЧЕСКИМ успехом
        # каждого Telegram-вызова по отдельности — иначе частичный сбой (нет прав на pin,
        # сообщение удалено админом канала, edit протух через Telegram-лимит 48ч) либо
        # молча "хоронит" табло навсегда, либо заваливает канал дублями каждые ~15 мин.
        channel = runtime.signals_channel()
        if channel and runtime.channel_enabled():
            now = clock.utcnow_iso()
            if should_refresh_board(runtime.last_board_update_at(), now):
                try:
                    st = await asyncio.to_thread(journal.stats)
                    by_symbol = await asyncio.to_thread(journal.stats_by_symbol)
                    cohort = (await asyncio.to_thread(journal.cohort_stats)
                             if runtime.signal_mode() == "semiauto" else None)
                    text = format_channel_live_board(st, by_symbol, cohort)
                except Exception as e:                  # noqa: BLE001
                    # сбой рендера/чтения БД — состояние табло не трогаем, повторим на след. такте
                    log.warning("live board render: %s", e)
                else:
                    msg_id = runtime.live_board_message_id()
                    if msg_id:
                        try:
                            await bot.edit_message_text(text, chat_id=channel, message_id=msg_id)
                        except Exception as e:          # noqa: BLE001
                            if "message is not modified" in str(e).lower():
                                pass                     # текст не изменился — не сбой, id валиден
                            else:
                                # протухло/удалено/нет прав — пересоздадим на следующем цикле,
                                # а не будем молча биться в одну и ту же ошибку вечно
                                log.warning("edit live board (пересоздам на след. цикле): %s", e)
                                await asyncio.to_thread(runtime.set_live_board_message_id, None)
                    else:
                        try:
                            sent = await bot.send_message(channel, text)
                        except Exception as e:          # noqa: BLE001
                            log.warning("send live board: %s", e)
                        else:
                            # id сохраняем СРАЗУ после успешной отправки, ДО pin — иначе сбой
                            # пина (нет права "Pin Messages") оставит отправленный пост
                            # непривязанным к состоянию, и каждый такт будет слать новый дубль
                            await asyncio.to_thread(runtime.set_live_board_message_id,
                                                    sent.message_id)
                            try:
                                await bot.pin_chat_message(channel, sent.message_id,
                                                           disable_notification=True)
                            except Exception as e:      # noqa: BLE001 — опубликовано, но не закреплено
                                log.warning("pin live board: %s", e)
                    await asyncio.to_thread(runtime.set_last_board_update_at, now)

    async def _on_close(closed):
        # публикуем в канал результаты закрытых сделок (не тени, реальные TP/SL)
        channel = runtime.signals_channel()
        if not channel or not runtime.channel_enabled():
            return
        for c in closed:
            if c.get("shadow") or c["status"] not in ("tp1", "tp2", "tp3", "stopped"):
                continue
            try:
                await bot.send_message(channel, format_channel_result(c))
            except Exception as e:                     # noqa: BLE001
                log.warning("результат в канал #%s: %s", c["id"], e)

    async def _poller():
        from app.data.market_data import run_poller
        # engine=None -> poller строит движок из runtime.min_confidence() каждый такт
        await run_poller(_broadcast, engine=None, on_tick=_maintenance, on_close=_on_close)

    def _poller_done(task) -> None:
        # наблюдаем судьбу фоновой таски: смерть poller не должна быть молчаливой
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("poller умер: %r", exc)

    async def _main():
        try:
            await register_bot_commands(bot, cfg.admin_ids)
        except Exception as e:                         # noqa: BLE001 — меню не критично для работы
            log.warning("register_bot_commands: %s", e)
        poller = asyncio.create_task(_poller())
        poller.add_done_callback(_poller_done)
        try:
            await dp.start_polling(bot)
        finally:
            poller.cancel()

    # TODO: /calc интерактивный (пока формат через format_calc); USDT через CryptoBot.
    log.info("bot запущен, engine=SMC, polling+poller, платежи=Stars")
    asyncio.run(_main())


if __name__ == "__main__":
    main()
