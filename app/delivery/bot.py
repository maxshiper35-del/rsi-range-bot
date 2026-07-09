"""
bot.py — Telegram-бот (aiogram): /start /stats /calc /subscribe, рассылка по тирам.
Точка входа приложения (make run).

SMC-интеграция реализована чистыми функциями (scan_symbol / format_*), которые
тестируются без aiogram; сам aiogram импортируется лениво в main().
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from app import clock
from app.brain import journal
from app.brain.risk import calculate_position
from app.brain.smc_setups import SMCEngine
from app.core.signal_engine import Signal
from app.delivery import graphics


# ---------------------------------------------------------------------------
# SMC-конвейер (тестируется без Telegram)
# ---------------------------------------------------------------------------
def scan_frames(symbol: str, frames: dict[str, pd.DataFrame], engine: SMCEngine,
                record: bool = True) -> Signal | None:
    """Прогнать движок по кадрам ТФ; при наличии сигнала записать в журнал. -> Signal|None."""
    sig = engine.analyze(frames, symbol)
    if sig is not None and record:
        journal.record_signal(sig)
    return sig


def scan_auto(symbol: str, frames: dict[str, pd.DataFrame],
              engine: SMCEngine) -> tuple | None:
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
                  engine: SMCEngine) -> tuple | None:
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


def scan_watchlist(symbols: list[str], engine: SMCEngine | None = None,
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


def parse_id_arg(text: str) -> int | None:
    """Извлечь id из '/confirm 12' -> 12; None, если аргумента нет или он не число."""
    parts = (text or "").split()
    # isdecimal, не isdigit: '²³' проходят isdigit, но роняют int()
    return int(parts[1]) if len(parts) > 1 and parts[1].isdecimal() else None


def handle_confirm(signal_id: int | None) -> str:
    """Оператор подтвердил контекст кандидата. -> текст ответа."""
    if signal_id is None:
        return "Использование: /confirm <id>"
    if journal.confirm_candidate(signal_id):
        return f"✅ Кандидат #{signal_id} подтверждён — сигнал активен, идёт в журнал."
    return f"⚠️ #{signal_id}: не найден или уже не кандидат."


def handle_reject(signal_id: int | None) -> str:
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
        lines.append(f"⚠️ Выборка мала (min когорта: {n_min} сд) — "
                     "выводы преждевременны, копим дальше.")
    return "\n".join(lines)


def format_by_setup(by_setup: dict) -> str:
    """Текст /bysetup — разбивка по тегу сетапа (journal.stats_by_setup()) с лестницей
    порогов значимости, не одним числом: n<20 — рано даже для направления, n≥20 — есть
    направление (без выводов), n≥30 — предварительно (НЕ уровень для публичного канала),
    n≥100 — предупреждение снимается (±~10пп по винрейту). LTF-BB/LTF-OB НЕ схлопнуты —
    именно LTF-BB уже показал проблему в бэктесте и живой практике, LTF-OB отдельно не
    проверялся."""
    if not by_setup:
        return "📐 По сетапам\n────────────────────\nЗакрытых сделок пока нет."
    lines = ["📐 Статистика по сетапам", "────────────────────"]
    for setup, s in sorted(by_setup.items()):
        n = s["trades"]
        lines.append(f"• {setup}: {n} сд | wr {s['winrate']:.0%} | "
                     f"exp {s['expectancy_r']:+.2f}R | Σ {s['total_r']:+.2f}R")
        if n < 20:
            lines.append("   ⚠️ n<20 — рано даже для направления")
        elif n < 30:
            lines.append("   ⚠️ n≥20 — есть направление, выводов пока нет")
        elif n < 100:
            lines.append("   ⚠️ n≥30 — предварительно, не публиковать как факт")
    lines.append("────────────────────")
    lines.append("Порог уверенности: n≥100 закрытых на сетап (±~10пп по винрейту).")
    return "\n".join(lines)


def format_growth(breakdown: dict) -> str:
    """Текст /growth — источник → приглашено → реально оплатили → % конверсии. "Оплатили"
    здесь строго payments.status='paid' (см. journal.acquisition_breakdown) — триал/промо
    не в счёт, иначе конверсия дашборда врала бы себе."""
    if not breakdown:
        return "📈 Атрибуция\n────────────────────\nЮзеров пока нет."
    lines = ["📈 Атрибуция по источникам", "────────────────────"]
    for source, s in sorted(breakdown.items(), key=lambda kv: -kv[1]["total"]):
        total, paid = s["total"], s["paid"]
        pct = f"{paid / total:.0%}" if total else "0%"
        label = "неизвестно/органика" if source == "—" else source
        lines.append(f"• {label}: {total} приглашено, {paid} оплатили ({pct})")
    lines.append("────────────────────")
    lines.append("Рефералы: программа ещё не запущена (задача 23, после первой оплаты).")
    return "\n".join(lines)


# тег сетапа -> короткий код без спецсимволов для callback_data кнопок админ-панели
# (Telegram-лимит 64 байта; ":" в "1:1" технически допустим, но код без него надёжнее/короче)
_KNOWN_SETUPS = ("6226", "1:1", "open-SMC", "LTF")
_SETUP_CODES = {"6226": "s6226", "1:1": "s11", "open-SMC": "sosmc", "LTF": "sltf"}
_SETUP_CODES_REV = {v: k for k, v in _SETUP_CODES.items()}

# ключ настройки -> (лейбл для FSM-промпта, вид парсинга/валидации) — общий словарь для кнопок
# "Цены"/"Стратегия" админ-панели с числовым вводом (см. AdminInput.numeric в main())
_ADMIN_NUMERIC_FIELDS = {
    "sub_price_stars": ("Цена подписки в Stars (целое число)", "int"),
    "sub_price_usdt": ("Цена подписки в USDT", "float"),
    "sub_days": ("Длительность подписки в днях (можно дробное — часы = доли дня)", "float"),
    "free_signals_per_week": ("Free-лимит сигналов в неделю (целое число)", "int"),
    "trial_days": ("Длительность триала для новых юзеров, дней (целое число)", "int"),
    "min_confidence": ("Мин. уверенность сетапа, 0..1", "unit"),
    "min_impulse_atr": ("Мин. размер импульса в ATR", "float"),
}


def _parse_admin_field_value(raw: str, kind: str) -> float:
    """Парсинг+валидация ввода админа через FSM-промпт (см. AdminInput.numeric в main()).
    Бросает исключение при некорректном значении — caller ловит и переспрашивает,
    не роняя диалог."""
    v = float(raw.strip().replace(",", "."))
    if kind == "int":
        assert v == int(v) and v >= 0
        return int(v)
    if kind == "unit":
        assert 0.0 <= v <= 1.0
        return v
    assert v >= 0.0                                         # "float"
    return v


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


def should_refresh_board(last_update: str | None, now_iso: str, hours: int = 24) -> bool:
    """Пора ли обновлять живое табло: нет прошлого обновления ИЛИ прошло >= hours."""
    if last_update is None:
        return True
    from datetime import datetime

    delta = datetime.fromisoformat(now_iso) - datetime.fromisoformat(last_update)
    return delta.total_seconds() >= hours * 3600


def should_alert_backup_stale(last_backup_at: str | None, last_alert_at: str | None,
                              now_iso: str, stale_hours: int = 26,
                              rethrottle_hours: int = 24) -> bool:
    """Пора ли слать админу алерт о пропуске автобэкапа: бэкапа нет/устарел (>=stale_hours,
    буфер над суточным циклом джобы) И (ещё не алертили ИЛИ прошлый алерт был
    >=rethrottle_hours назад — не спамить на каждом такте poller, пока проблема не решена)."""
    from datetime import datetime

    now = datetime.fromisoformat(now_iso)
    if last_backup_at is None:
        stale = True
    else:
        stale = (now - datetime.fromisoformat(last_backup_at)).total_seconds() >= stale_hours * 3600
    if not stale:
        return False
    if last_alert_at is None:
        return True
    return (now - datetime.fromisoformat(last_alert_at)).total_seconds() >= rethrottle_hours * 3600


_TRACKRECORD_COLS = ("id", "symbol", "side", "timeframe", "entry", "stop", "tp1", "tp2", "tp3",
                    "status", "result_r", "created_at", "closed_at")


def build_trackrecord_csv(rows: list) -> bytes:
    """CSV-байты полного публичного журнала (journal.export_public_trades) — единый артефакт
    для /trackrecord и еженедельного поста в канал; хэш считается ИМЕННО от этих байт, не от
    отдельной сериализации, иначе документ и подпись расходятся."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_TRACKRECORD_COLS)
    for r in rows:
        writer.writerow([r[c] for c in _TRACKRECORD_COLS])
    return buf.getvalue().encode("utf-8")


def trackrecord_checksum(csv_bytes: bytes) -> str:
    """sha256 готовых CSV-байт — дешёвый commit-reveal без блокчейна: незаметная правка
    истории задним числом становится криптографически обнаружимой (хэш публикуется вместе
    с файлом, независимая сверка доступна любому)."""
    import hashlib

    return hashlib.sha256(csv_bytes).hexdigest()


def should_post_weekly(last_week_key: str | None, current_week_key: str) -> bool:
    """Пора ли постить трек-рекорд в канал: другая ISO-неделя, чем в прошлый раз — не дважды
    за одну календарную неделю."""
    return last_week_key != current_week_key


def format_channel_live_board(st: dict, by_symbol: dict, cohort: dict | None = None,
                              by_setup: dict | None = None) -> str:
    """Живое табло для закреплённого поста канала: не winrate (самая манипулируемая метрика
    ниши), а матожидание в R, размер выборки, разбивка по символам/сетапам, когорты (semiauto)."""
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
        if by_setup:
            lines.append("\nПо сетапам:")
            for setup, s in sorted(by_setup.items()):
                lines.append(f"  • {setup}: {s['trades']} сд, exp {s['expectancy_r']:+.2f}R, "
                             f"итог {s['total_r']:+.2f}R")
    if cohort is not None:
        c, r = cohort["confirmed"], cohort["rejected"]
        lines.append("\n🧪 Подтверждённые оператором vs отклонённые (форвард-эксперимент):")
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
    ("trackrecord", "полный CSV-журнал сигналов + sha256 (проверяемый трек-рекорд)"),
    ("calc", "калькулятор позиции: /calc <деп> <риск%> <вход> <стоп>"),
    ("subscribe", "подписка — все сигналы"),
    ("promo", "применить промокод: /promo <код>"),
    ("about", "как устроен сервис, честный дисклеймер"),
    ("backtest", "честный итог бэктеста стратегии"),
    ("glossary", "словарь терминов SMC (BOS, FVG, killzone…)"),
]

ADMIN_COMMANDS = USER_COMMANDS + [
    ("admin", "панель управления"),
    ("status", "снимок всех настроек"),
    ("health", "живой чек: аптайм, тик poller, бэкап, БД"),
    ("pause", "остановить поиск сигналов"),
    ("resume", "возобновить поиск сигналов"),
    ("symbols", "список торгуемых символов"),
    ("addsymbol", "добавить символ: /addsymbol BTC/USDT:USDT"),
    ("delsymbol", "убрать символ"),
    ("setmode", "auto|semiauto — режим сигналов"),
    ("setups", "включённые сетапы стратегии"),
    ("bysetup", "статистика по тегу сетапа (6226/1:1/open-SMC/LTF-BB/LTF-OB)"),
    ("growth", "атрибуция: источник -> приглашено -> оплатили -> %"),
    ("minconf", "порог уверенности сетапа (0..1)"),
    ("minimpulse", "мин. размер импульса в ATR"),
    ("channel", "on|off|<id> — канал публикации"),
    ("channelmode", "full|teaser — режим постов канала"),
    ("cornix", "on|off — Cornix-совместимый блок в постах"),
    ("post", "разовая сводка статистики в канал"),
    ("price", "stars|usdt|days <N> — цена подписки"),
    ("freeweek", "лимит бесплатных сигналов/нед"),
    ("addpromo", "создать промокод: /addpromo <код> <дней> [лимит]"),
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
        f"Мин. уверенность: {eff['min_confidence']:.2f}  ·  "
        f"мин. импульс: {eff['min_impulse_atr']:.1f}×ATR\n"
        f"Канал: {eff['channel']} "
        f"({'вкл' if eff['channel_enabled'] else 'выкл'} · {eff['channel_mode']})\n"
        f"Цена: {eff['price_stars']}⭐ / {eff['price_usdt']:g} USDT / {eff['sub_days']} дн\n"
        f"Free-лимит: {eff['free_per_week']}/нед\n"
        f"Юзеров: {users['total']} (платных {users['paid']})"
    )


def _fmt_age(iso: str | None, now: datetime) -> str:
    """Человекочитаемый возраст метки времени: '3м назад' / 'нет данных'."""
    if not iso:
        return "нет данных"
    from datetime import datetime as _dt

    delta = now - _dt.fromisoformat(iso)
    secs = delta.total_seconds()
    if secs < 0:
        return "только что"
    if secs < 3600:
        return f"{int(secs // 60)}м назад"
    if secs < 86400:
        return f"{secs / 3600:.1f}ч назад"
    return f"{secs / 86400:.1f}д назад"


def format_health(uptime_seconds: float, last_tick_at: str | None,
                  last_backup_at: str | None, db_size_bytes: int, wal_mode: str) -> str:
    """/health — быстрый живой чек: аптайм, последний тик поллера, бэкап, БД, WAL."""
    from datetime import datetime

    now = datetime.now(timezone.utc)
    up_h = uptime_seconds / 3600
    tick_age = _fmt_age(last_tick_at, now)
    backup_age = _fmt_age(last_backup_at, now)
    tick_warn = "⚠️ " if (not last_tick_at or (now - datetime.fromisoformat(last_tick_at))
                          .total_seconds() > 3700) else "✅ "
    backup_warn = "⚠️ " if (not last_backup_at or (now - datetime.fromisoformat(last_backup_at))
                            .total_seconds() > 26 * 3600) else "✅ "
    return (
        "❤️ Health check\n"
        "────────────────────\n"
        f"Аптайм процесса: {up_h:.1f}ч\n"
        f"{tick_warn}Последний тик poller: {tick_age}\n"
        f"{backup_warn}Последний бэкап: {backup_age}\n"
        f"БД: {db_size_bytes / 1024:.0f} KB, journal_mode={wal_mode}"
    )


def _truncate_exception(exc: BaseException, tb_limit: int = 3500) -> tuple[str, str]:
    """(сообщение, traceback) с усечением с запасом под Telegram-лимит текста ~4096 —
    сообщение исключения само по себе может быть огромным (напр. дамп данных), traceback
    усекается с ХВОСТА (там сама ошибка важнее точки входа)."""
    import traceback

    msg = str(exc)
    if len(msg) > 300:
        msg = msg[:300] + "…"
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if len(tb) > tb_limit:
        tb = tb[-tb_limit:]
    return msg, tb


def format_crash_alert(exc: BaseException) -> str:
    """Текст алерта админу при необработанном крахе ВСЕГО процесса (top-level, main())."""
    msg, tb = _truncate_exception(exc)
    return f"🔴 RSI Range CRASHED\n{type(exc).__name__}: {msg}\n\n```\n{tb}\n```"


def _describe_update(update) -> str:
    """Короткое описание update — какая команда/callback уронили хендлер, для алерта."""
    msg = getattr(update, "message", None)
    if msg is not None and getattr(msg, "text", None):
        return msg.text[:60]
    cq = getattr(update, "callback_query", None)
    if cq is not None:
        return f"callback:{cq.data}"
    return type(update).__name__ if update is not None else "?"


def format_handler_error_alert(exc: BaseException, update) -> str:
    """Алерт админу: исключение поймано ВНУТРИ одного хендлера aiogram — сам процесс жив,
    dispatcher по умолчанию гасит такие ошибки молча (виден только traceback в логах).
    Менее тревожный заголовок, чем у format_crash_alert — это не смерть бота, а один
    сбойный апдейт."""
    msg, tb = _truncate_exception(exc, tb_limit=3000)
    ctx = _describe_update(update)
    return f"⚠️ Ошибка в хендлере ({ctx})\n{type(exc).__name__}: {msg}\n\n```\n{tb}\n```"


def _send_raw_telegram_message(token: str, chat_id: int, text: str) -> None:
    """Отправка БЕЗ aiogram — прямой HTTP к Bot API. На случай, если сломан именно aiogram/event
    loop, а не просто хэндлер: этот путь не зависит от Dispatcher/Bot-объекта."""
    import json
    import urllib.parse
    import urllib.request

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text[:4096]}).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as r:  # noqa: S310
        json.loads(r.read())


def _fmt_days(days: float) -> str:
    """Человекочитаемая длительность подписки: дробные дни (тестовые тарифы) -> часы."""
    if days == int(days):
        return f"{int(days)} дней"
    return f"{round(days * 24)} часов"


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
        f"Вход {signal.entry:g} | Стоп {signal.stop:g} | Тейк {tps} | "
        f"RR 1:{signal.risk_reward:.1f}",
        f"Механ. уверенность: {int(signal.confidence * 100)}%  |  "
        f"killzone: {', '.join(kz) if kz else 'вне окна'}",
        "Механика:",
        *[f"  — {r}" for r in signal.reasons],
        "",
        f"❓ Контекст ОК (тренд HTF, качество доставки)?  "
        f"/confirm {signal_id}  или  /reject {signal_id}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Точка входа (aiogram — ленивый импорт)
# ---------------------------------------------------------------------------
def main() -> None:
    """Поднять бота: при старте прогнать миграции, зарегистрировать хэндлеры, запустить polling."""
    import asyncio
    from datetime import datetime

    from aiogram import Bot, Dispatcher, F  # ленивый импорт тяжёлой зависимости
    from aiogram.filters import Command
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.types import (
        BufferedInputFile,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        KeyboardButton,
        LabeledPrice,
        ReplyKeyboardMarkup,
    )

    from app import db, runtime
    from app.config import get_settings
    from app.delivery import payments
    from app.logging_conf import get_logger

    log = get_logger("bot")
    start_time = datetime.now(timezone.utc)
    db.init_db()
    cfg = get_settings()
    bot = Bot(token=cfg.tg_bot_token)
    dp = Dispatcher()

    class CalcStates(StatesGroup):
        """/calc через кнопку — пошаговый ввод вместо '/calc 1000 2 62340 62980' строкой."""
        deposit = State()
        risk = State()
        entry = State()
        stop = State()

    class AdminInput(StatesGroup):
        """Общий паттерн 'кнопка -> введите значение' для числовых настроек админ-панели
        (см. _ADMIN_NUMERIC_FIELDS) и добавления символа."""
        numeric = State()
        symbol = State()

    class PromoState(StatesGroup):
        code = State()

    def _main_menu_kb() -> ReplyKeyboardMarkup:
        """Постоянное меню внизу чата — юзеру не нужно помнить команды."""
        return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📄 Трек-рекорд")],
            [KeyboardButton(text="🧮 Калькулятор"), KeyboardButton(text="💳 Подписка")],
            [KeyboardButton(text="ℹ️ О боте"), KeyboardButton(text="🧪 Бэктест")],
            [KeyboardButton(text="📖 Глоссарий"), KeyboardButton(text="🎟 Промокод")],
        ])

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
        trial_days = runtime.trial_days()
        applied = await asyncio.to_thread(payments.grant_trial, m.from_user.id, trial_days)
        text = (
            "👋 RSI Range Signals — инструмент и честная статистика (не «прибыль 400%»).\n"
            "Пользуйтесь меню внизу — команды тоже по-прежнему работают.\n"
            f"{ANTISCAM_LINE}\n"
            "⚠️ Не финансовый совет. Риск на сделку 1–2%.")
        if applied:
            text = (f"🎁 Открыт полный доступ на {trial_days} дн. — "
                    f"все сигналы без ограничений.\n\n" + text)
        await m.answer(text, reply_markup=_main_menu_kb())

    @dp.message(Command("about"))
    @dp.message(F.text == "ℹ️ О боте")
    async def _about(m):
        await m.answer(format_about())

    @dp.message(Command("backtest"))
    @dp.message(F.text == "🧪 Бэктест")
    async def _backtest(m):
        await m.answer(format_backtest_summary())

    @dp.message(Command("glossary"))
    @dp.message(F.text == "📖 Глоссарий")
    async def _glossary(m):
        await m.answer(format_glossary())

    @dp.message(Command("subscribe"))
    @dp.message(F.text == "💳 Подписка")
    async def _subscribe(m):
        await asyncio.to_thread(payments.register_user, m.from_user.id, m.from_user.username)
        text = await asyncio.to_thread(payments.subscription_status_text, m.from_user.id)
        if runtime.usdt_invoice_url() and not payments.cryptobot_enabled():
            uname = m.from_user.username or m.from_user.id
            text += (f"\n\n💵 USDT: оплати по кнопке, в КОММЕНТАРИИ к платежу укажи «{uname}», "
                     "затем нажми «Я оплатил» — активируем в течение дня.")
        await m.answer(text, reply_markup=_subscribe_kb())

    async def _apply_promo(m, code: str):
        res = await asyncio.to_thread(payments.redeem_promo, m.from_user.id, code)
        if res["ok"]:
            await m.answer(f"✅ Промокод применён: +{res['days']} дн. подписки. "
                          "/subscribe — статус.")
            return
        reasons = {
            "not_found": "Промокод не найден.",
            "expired": "Промокод истёк.",
            "exhausted": "Промокод исчерпан (лимит применений).",
            "already_used": "Вы уже использовали этот промокод.",
        }
        await m.answer(f"⚠️ {reasons.get(res['reason'], 'Не удалось применить промокод.')}")

    @dp.message(Command("promo"))
    async def _promo(m):
        parts = (m.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await m.answer("Формат: /promo <код>")
            return
        await _apply_promo(m, parts[1].strip())

    @dp.message(F.text == "🎟 Промокод")
    async def _promo_btn_start(m, state: FSMContext):
        await state.set_state(PromoState.code)
        await m.answer("Введите промокод:")

    @dp.message(PromoState.code, ~F.text.startswith("/"))
    async def _promo_step_code(m, state: FSMContext):
        await state.clear()
        await _apply_promo(m, (m.text or "").strip())

    @dp.callback_query(F.data == "pay_stars")
    async def _pay_stars(cq):
        days, stars = runtime.sub_days(), runtime.price_stars()
        await bot.send_invoice(
            chat_id=cq.from_user.id,
            title="Подписка RSI Range Signals",
            description=f"Доступ ко всем сигналам на {_fmt_days(days)}.",
            payload=f"sub:{days}",
            provider_token="",                          # Stars: провайдер не нужен
            currency="XTR",
            prices=[LabeledPrice(label=f"{_fmt_days(days)} доступа", amount=stars)],
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
        await m.answer(await asyncio.to_thread(
            format_calc, dep, risk, entry, stop, cfg.max_leverage))

    # кнопка "Калькулятор" — пошаговый FSM-ввод вместо одной строки с 4 числами.
    # ~F.text.startswith("/") на каждом шаге: если юзер вместо ответа набрал команду
    # (передумал/нажал другую кнопку) — состояние НЕ перехватывает её молча, команда
    # проходит в свой обычный хендлер (aiogram пробует следующий подходящий хендлер)
    @dp.message(F.text == "🧮 Калькулятор")
    async def _calc_btn_start(m, state: FSMContext):
        await state.set_state(CalcStates.deposit)
        await m.answer("🧮 Калькулятор позиции\nДепозит в USD? Например: 1000")

    @dp.message(CalcStates.deposit, ~F.text.startswith("/"))
    async def _calc_step_deposit(m, state: FSMContext):
        try:
            dep = float((m.text or "").replace(",", "."))
            assert dep > 0
        except Exception:                                  # noqa: BLE001
            await m.answer("Не понял число. Депозит в USD? Например: 1000")
            return
        await state.update_data(deposit=dep)
        await state.set_state(CalcStates.risk)
        await m.answer("Риск на сделку, %? Например: 2")

    @dp.message(CalcStates.risk, ~F.text.startswith("/"))
    async def _calc_step_risk(m, state: FSMContext):
        try:
            risk = float((m.text or "").replace(",", "."))
            assert risk > 0
        except Exception:                                  # noqa: BLE001
            await m.answer("Не понял число. Риск на сделку, %? Например: 2")
            return
        await state.update_data(risk=risk)
        await state.set_state(CalcStates.entry)
        await m.answer("Цена входа? Например: 62340")

    @dp.message(CalcStates.entry, ~F.text.startswith("/"))
    async def _calc_step_entry(m, state: FSMContext):
        try:
            entry = float((m.text or "").replace(",", "."))
            assert entry > 0
        except Exception:                                  # noqa: BLE001
            await m.answer("Не понял число. Цена входа? Например: 62340")
            return
        await state.update_data(entry=entry)
        await state.set_state(CalcStates.stop)
        await m.answer("Цена стопа? Например: 62980")

    @dp.message(CalcStates.stop, ~F.text.startswith("/"))
    async def _calc_step_stop(m, state: FSMContext):
        try:
            stop = float((m.text or "").replace(",", "."))
            assert stop > 0
        except Exception:                                  # noqa: BLE001
            await m.answer("Не понял число. Цена стопа? Например: 62980")
            return
        data = await state.get_data()
        await state.clear()
        await m.answer(await asyncio.to_thread(
            format_calc, data["deposit"], data["risk"], data["entry"], stop, cfg.max_leverage))

    @dp.message(Command("help"))
    async def _help(m):
        base = ("Команды:\n"
                "/stats — публичная статистика\n"
                "/trackrecord — полный CSV-журнал + sha256 (проверяемый трек-рекорд)\n"
                "/calc <деп> <риск%> <вход> <стоп> — калькулятор позиции\n"
                "/subscribe — подписка (все сигналы)\n"
                "/about — как устроен сервис и честный дисклеймер\n"
                "/backtest — честный итог бэктеста стратегии\n"
                "/glossary — словарь терминов SMC\n"
                f"{ANTISCAM_LINE}\n"
                "⚠️ Не финансовый совет. Риск на сделку 1–2%.")
        if _is_admin(m):
            base += ("\n\n🛠 Админ: /admin — панель управления "
                    "(пауза, символы, цены, канал, подписчики)")
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
        days: float = cfg.sub_days
        try:
            days = float(sp.invoice_payload.split(":")[1])
        except Exception:                               # noqa: BLE001
            pass
        applied = await asyncio.to_thread(
            payments.grant_subscription, m.from_user.id, "stars",
            float(sp.total_amount), sp.currency, days, sp.telegram_payment_charge_id)
        if applied:
            await m.answer(f"✅ Подписка активна на {_fmt_days(days)}! Сигналы теперь приходят "
                           f"автоматически на закрытии свечей. Статус: /subscribe")
        else:
            await m.answer("Этот платёж уже был учтён. Спасибо! Статус: /subscribe")

    @dp.message(Command("stats"))
    @dp.message(F.text == "📊 Статистика")
    async def _stats(m):
        await m.answer(await asyncio.to_thread(lambda: format_stats(journal.stats())))

    @dp.message(Command("trackrecord"))
    @dp.message(F.text == "📄 Трек-рекорд")
    async def _trackrecord(m):
        def _build():
            rows = journal.export_public_trades()
            csv_bytes = build_trackrecord_csv(rows)
            return rows, csv_bytes, trackrecord_checksum(csv_bytes)
        rows, csv_bytes, checksum = await asyncio.to_thread(_build)
        await m.answer_document(
            BufferedInputFile(csv_bytes, filename="rsirange_trackrecord.csv"),
            caption=f"📄 Полный публичный журнал ({len(rows)} сигналов). "
                   f"SHA-256: {checksum[:16]}…")

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
            [b(text=f"🤖 Режим: {eff['signal_mode']}", callback_data="adm_modetoggle"),
             b(text="🔕 Cornix выкл" if not eff["cornix_enabled"] else "🔔 Cornix вкл",
               callback_data="adm_cornixtoggle")],
            [b(text="⚙️ Стратегия", callback_data="adm_nav_strategy"),
             b(text="💰 Цены", callback_data="adm_nav_prices")],
            [b(text="📈 Символы", callback_data="adm_nav_symbols")],
            [b(text="📊 Пост статы в канал", callback_data="adm_poststats"),
             b(text="🔄 Обновить", callback_data="adm_refresh")],
        ])

    def _strategy_kb(eff: dict) -> InlineKeyboardMarkup:
        b = InlineKeyboardButton
        enabled = set(eff["enabled_setups"])
        setup_rows = [[b(text=f"{'✅' if s in enabled else '⬜'} {s}",
                         callback_data=f"adm_{_SETUP_CODES[s]}")] for s in _KNOWN_SETUPS]
        return InlineKeyboardMarkup(inline_keyboard=[
            *setup_rows,
            [b(text=f"🎚 Мин. увер.: {eff['min_confidence']:.2f}",
               callback_data="adm_prompt_min_confidence")],
            [b(text=f"🎚 Мин. импульс: {eff['min_impulse_atr']:.1f}×ATR",
               callback_data="adm_prompt_min_impulse_atr")],
            [b(text="◀️ Назад", callback_data="adm_nav_main")],
        ])

    def _prices_kb(eff: dict) -> InlineKeyboardMarkup:
        b = InlineKeyboardButton
        return InlineKeyboardMarkup(inline_keyboard=[
            [b(text=f"⭐ Stars: {eff['price_stars']}",
               callback_data="adm_prompt_sub_price_stars")],
            [b(text=f"💵 USDT: {eff['price_usdt']:g}",
               callback_data="adm_prompt_sub_price_usdt")],
            [b(text=f"📅 Дней: {eff['sub_days']:.3g}", callback_data="adm_prompt_sub_days")],
            [b(text=f"🆓 Free/нед: {eff['free_per_week']}",
               callback_data="adm_prompt_free_signals_per_week")],
            [b(text=f"🎁 Триал: {eff['trial_days']} дн", callback_data="adm_prompt_trial_days")],
            [b(text="◀️ Назад", callback_data="adm_nav_main")],
        ])

    def _symbols_kb(eff: dict) -> InlineKeyboardMarkup:
        b = InlineKeyboardButton
        sym_rows = [[b(text=f"❌ {s}", callback_data=f"adm_delsym_{i}")]
                   for i, s in enumerate(eff["symbols"])]
        return InlineKeyboardMarkup(inline_keyboard=[
            *sym_rows,
            [b(text="➕ Добавить символ", callback_data="adm_prompt_symbol")],
            [b(text="◀️ Назад", callback_data="adm_nav_main")],
        ])

    async def _panel(chat_id):
        eff = await asyncio.to_thread(runtime.effective)
        users = await asyncio.to_thread(payments.user_counts)
        await bot.send_message(chat_id, format_admin_status(eff, users),
                               reply_markup=_admin_kb(eff))

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

    @dp.message(Command("health"))
    async def _health(m):
        if not _is_admin(m):
            return
        def _collect():
            uptime = (datetime.now(timezone.utc) - start_time).total_seconds()
            wal_row = db.query_one("PRAGMA journal_mode")
            wal = str(wal_row[0]) if wal_row else "?"
            size = cfg.db_path.stat().st_size if cfg.db_path.exists() else 0
            return uptime, wal, size
        uptime, wal, size = await asyncio.to_thread(_collect)
        await m.answer(format_health(uptime, runtime.last_poller_tick_at(),
                                     runtime.last_backup_at(), size, wal))

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
            await m.answer("Формат: /cryptobottoken <API-токен Crypto Pay>  ·  "
                          "очистить: /cryptobottoken -")
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
                           "⚠️ Перед on: ручная проверка формата в тестовой группе "
                           "с реальным Cornix.")
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

    @dp.message(Command("bysetup"))
    async def _bysetup(m):
        if not _is_admin(m):
            return
        by_setup = await asyncio.to_thread(journal.stats_by_setup)
        await m.answer(format_by_setup(by_setup))

    @dp.message(Command("growth"))
    async def _growth(m):
        if not _is_admin(m):
            return
        breakdown = await asyncio.to_thread(payments.acquisition_breakdown)
        await m.answer(format_growth(breakdown))

    @dp.message(Command("minimpulse"))
    async def _minimpulse(m):
        if not _is_admin(m):
            return
        parts = (m.text or "").split()
        try:
            v = float(parts[1])
            assert v >= 0
        except Exception:                              # noqa: BLE001
            await m.answer("Формат: /minimpulse 1.5 (мин. размер импульса в ATR; "
                          "больше = реже, но крупнее структура)")
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

    @dp.message(Command("addpromo"))
    async def _addpromo(m):
        if not _is_admin(m):
            return
        parts = (m.text or "").split()
        if len(parts) < 3 or not parts[2].isdecimal():
            await m.answer("Формат: /addpromo <код> <дней> [лимит]")
            return
        code = parts[1]
        days = int(parts[2])
        max_uses = int(parts[3]) if len(parts) > 3 and parts[3].isdecimal() else None
        try:
            await asyncio.to_thread(payments.create_promo, code, days, max_uses)
        except Exception as e:                        # noqa: BLE001 — код уже существует и т.п.
            await m.answer(f"Не удалось создать промокод: {e}")
            return
        limit_text = f", лимит {max_uses}" if max_uses else ""
        await m.answer(f"✅ Промокод {code}: +{days} дн.{limit_text}")

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
    async def _admin_cb(cq, state: FSMContext):
        if cq.from_user.id not in cfg.admin_ids:
            await cq.answer()
            return
        data = cq.data
        screen = "main"

        if data == "adm_pausetoggle":
            await asyncio.to_thread(runtime.set_value, "paused",
                                    "0" if runtime.paused() else "1")
        elif data == "adm_chmode":
            await asyncio.to_thread(runtime.set_value, "channel_mode",
                                    "teaser" if runtime.channel_mode() == "full" else "full")
        elif data == "adm_chtoggle":
            await asyncio.to_thread(runtime.set_value, "channel_enabled",
                                    "0" if runtime.channel_enabled() else "1")
        elif data == "adm_modetoggle":
            await asyncio.to_thread(runtime.set_value, "signal_mode",
                                    "semiauto" if runtime.signal_mode() == "auto" else "auto")
        elif data == "adm_cornixtoggle":
            await asyncio.to_thread(runtime.set_value, "cornix_block_enabled",
                                    "0" if runtime.cornix_block_enabled() else "1")
        elif data == "adm_poststats":
            ch = runtime.signals_channel()
            if ch:
                try:
                    await bot.send_message(ch, format_channel_stats(journal.stats()))
                except Exception as e:                 # noqa: BLE001
                    log.warning("poststats: %s", e)
        elif data == "adm_nav_strategy":
            screen = "strategy"
        elif data == "adm_nav_prices":
            screen = "prices"
        elif data == "adm_nav_symbols":
            screen = "symbols"
        elif data in ("adm_nav_main", "adm_refresh"):
            screen = "main"
        elif data in _SETUP_CODES_REV:
            tag = _SETUP_CODES_REV[data]
            cur = set(await asyncio.to_thread(runtime.enabled_setups))
            cur.discard(tag) if tag in cur else cur.add(tag)
            await asyncio.to_thread(runtime.set_value, "enabled_setups", ",".join(sorted(cur)))
            screen = "strategy"
        elif data.startswith("adm_delsym_"):
            idx_s = data[len("adm_delsym_"):]
            syms = await asyncio.to_thread(runtime.symbols)
            if idx_s.isdecimal() and int(idx_s) < len(syms):
                del syms[int(idx_s)]
                await asyncio.to_thread(runtime.set_value, "symbols", ",".join(syms))
            screen = "symbols"
        elif data.startswith("adm_prompt_"):
            field = data[len("adm_prompt_"):]
            if field == "symbol":
                await state.set_state(AdminInput.symbol)
                await cq.message.answer("Введите символ, например: BTC/USDT:USDT")
            elif field in _ADMIN_NUMERIC_FIELDS:
                label, kind = _ADMIN_NUMERIC_FIELDS[field]
                await state.update_data(field=field, kind=kind)
                await state.set_state(AdminInput.numeric)
                await cq.message.answer(f"Введите новое значение: {label}")
            await cq.answer()
            return                                      # промпт не перерисовывает панель

        eff = await asyncio.to_thread(runtime.effective)
        users = await asyncio.to_thread(payments.user_counts)
        kb = {"main": _admin_kb, "strategy": _strategy_kb,
             "prices": _prices_kb, "symbols": _symbols_kb}[screen](eff)
        try:
            await cq.message.edit_text(format_admin_status(eff, users), reply_markup=kb)
        except Exception:                              # noqa: BLE001 — «message not modified» и т.п.
            pass
        await cq.answer("Готово")

    @dp.message(AdminInput.numeric, ~F.text.startswith("/"))
    async def _admin_numeric_receive(m, state: FSMContext):
        if not _is_admin(m):
            return
        data = await state.get_data()
        field, kind = str(data.get("field", "")), str(data.get("kind", ""))
        label = _ADMIN_NUMERIC_FIELDS.get(field, (field, kind))[0]
        try:
            v = _parse_admin_field_value(m.text or "", kind)
        except Exception:                              # noqa: BLE001
            await m.answer(f"Не понял значение. Введите ещё раз: {label}")
            return
        await state.clear()
        await asyncio.to_thread(runtime.set_value, field, v)
        await m.answer(f"✅ {label} обновлено: {v}")

    @dp.message(AdminInput.symbol, ~F.text.startswith("/"))
    async def _admin_symbol_receive(m, state: FSMContext):
        if not _is_admin(m):
            return
        await state.clear()
        sym = (m.text or "").strip()
        if not sym:
            await m.answer("Пустой символ, не добавлено.")
            return
        syms = await asyncio.to_thread(runtime.symbols)
        if sym not in syms:
            syms.append(sym)
        await asyncio.to_thread(runtime.set_value, "symbols", ",".join(syms))
        await m.answer("Символы: " + ", ".join(syms))

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
        # автобэкап не подтверждён/устарел — алерт админу (троттлинг: не чаще раза в сутки)
        now_ = clock.utcnow_iso()
        if should_alert_backup_stale(runtime.last_backup_at(),
                                     runtime.last_backup_alert_at(), now_):
            backup_age = _fmt_age(runtime.last_backup_at(), datetime.now(timezone.utc))
            text = (f"⚠️ Автобэкап не подтверждён ({backup_age}) "
                    "— проверьте launchd job com.rsirange.backup. /health для деталей.")
            for admin_id in cfg.admin_ids:
                try:
                    await bot.send_message(admin_id, text)
                except Exception as e:                     # noqa: BLE001
                    log.warning("backup stale алерт админу %s: %s", admin_id, e)
            await asyncio.to_thread(runtime.set_last_backup_alert_at, now_)
        # TTL просроченных semiauto-кандидатов (готовим впрок — сейчас signal_mode=auto)
        try:
            expired_ids = await asyncio.to_thread(journal.expire_stale_candidates)
            if expired_ids:
                log.info("TTL: просрочено кандидатов: %s", expired_ids)
        except Exception as e:                             # noqa: BLE001
            log.warning("expire_stale_candidates: %s", e)
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
                await bot.send_message(
                    row["tg_id"],
                    f"⏳ Подписка истекает {row['paid_until'][:10]}. /subscribe — продлить.")
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
                await bot.send_message(
                    uid, "✅ Оплата USDT получена — подписка активна! /subscribe")
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
                    by_setup = await asyncio.to_thread(journal.stats_by_setup)
                    cohort = (await asyncio.to_thread(journal.cohort_stats)
                             if runtime.signal_mode() == "semiauto" else None)
                    text = format_channel_live_board(st, by_symbol, cohort, by_setup)
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
            # еженедельный публичный трек-рекорд (CSV+sha256) — раз в календарную ISO-неделю,
            # не привязан к should_refresh_board (разная периодичность: 24ч vs раз в неделю)
            now_dt = datetime.now(timezone.utc)
            iso_year, iso_week, _ = now_dt.isocalendar()
            week_key = f"{iso_year}-W{iso_week:02d}"
            if should_post_weekly(runtime.trackrecord_last_week(), week_key):
                # карточка недели (PNG) — отдельный try/except: сбой рендера (Chromium)
                # не должен блокировать CSV ниже — тот честностный артефакт критичнее
                try:
                    week_start = datetime.fromisocalendar(iso_year, iso_week, 1).replace(
                        tzinfo=timezone.utc)
                    wk = await asyncio.to_thread(
                        journal.stats_for_period, week_start.isoformat(), now_dt.isoformat())
                    st = await asyncio.to_thread(journal.stats)
                    card_data = {
                        "period_label": f"{week_start.strftime('%d.%m')}"
                                       f"–{now_dt.strftime('%d.%m.%Y')}",
                        "total_r": wk["total_r"],
                        "closed": wk["trades"],
                        "open": st["open"],
                        "winrate_pct": wk["winrate"] * 100,
                        "wins": wk["wins"],
                        "losses": wk["losses"],
                        "expectancy_r": wk["expectancy_r"],
                        "outcomes": wk["outcomes"],
                        "updated_label": now_dt.strftime("%d.%m.%Y"),
                    }
                    png_bytes = await asyncio.to_thread(graphics.render_weekly_card, card_data)
                    await bot.send_photo(
                        channel, BufferedInputFile(png_bytes, filename="weekly_card.png"))
                except Exception as e:                      # noqa: BLE001
                    log.warning("карточка недели: %s", e)
                try:
                    rows = await asyncio.to_thread(journal.export_public_trades)
                    csv_bytes = await asyncio.to_thread(build_trackrecord_csv, rows)
                    checksum = trackrecord_checksum(csv_bytes)
                    await bot.send_document(
                        channel,
                        BufferedInputFile(csv_bytes, filename="rsirange_trackrecord.csv"),
                        caption=f"📄 Еженедельный публичный журнал ({len(rows)} сигналов). "
                               f"SHA-256: {checksum[:16]}…")
                    await asyncio.to_thread(journal.record_ledger_hash, len(rows), checksum)
                    await asyncio.to_thread(runtime.set_trackrecord_last_week, week_key)
                except Exception as e:                      # noqa: BLE001
                    log.warning("еженедельный трек-рекорд: %s", e)

    async def _on_close(closed):
        # мягкое уведомление АДМИНУ (не в канал): серия стопов подряд на ВКЛЮЧЁННОМ сетапе —
        # только сигнал, без авто-отключения (решение за оператором). Не зависит от того,
        # включён ли канал — это отдельный контур от постинга результатов ниже.
        enabled = set(runtime.enabled_setups())
        fresh_setups = {c["setup"] for c in closed if not c.get("shadow") and c.get("setup")}
        for setup in fresh_setups:
            if setup not in enabled:
                continue
            streak = await asyncio.to_thread(journal.consecutive_losses, setup, 3)
            if streak >= 3:
                text = (f"⚠️ Сетап {setup}: {streak} стопа/лосса подряд (result_r≤0). "
                       f"/bysetup — детали. Рассмотреть /setups без {setup}.")
                for admin_id in cfg.admin_ids:
                    try:
                        await bot.send_message(admin_id, text)
                    except Exception as e:              # noqa: BLE001
                        log.warning("consecutive_losses алерт админу %s: %s", admin_id, e)

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

    @dp.error()
    async def _on_handler_error(event) -> bool:
        # dispatcher по умолчанию гасит исключения из ОТДЕЛЬНЫХ хендлеров молча — процесс
        # жив, но админ узнал бы только из логов (см. крах /subscribe на дробных sub_days,
        # найденный этим же способом). Возвращаем True — апдейт считается обработанным,
        # aiogram не пытается повторно доставить/залогировать его сам.
        log.exception("необработанное исключение в хендлере", exc_info=event.exception)
        text = format_handler_error_alert(event.exception, event.update)
        for admin_id in cfg.admin_ids:
            try:
                await bot.send_message(admin_id, text)
            except Exception:                          # noqa: BLE001 — алерт не должен падать сам
                log.exception("не удалось отправить алерт об ошибке хендлера админу %s", admin_id)
        return True

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
    try:
        asyncio.run(_main())
    except Exception as e:                                # noqa: BLE001 — краш = сигнал, не тишина
        log.exception("необработанный краш процесса")
        # БЕЗ aiogram (он мог быть источником краха) — прямой HTTP-алерт админам,
        # затем raise: launchd (KeepAlive=true) перезапустит процесс сам
        for admin_id in cfg.admin_ids:
            try:
                _send_raw_telegram_message(cfg.tg_bot_token, admin_id, format_crash_alert(e))
            except Exception:                             # noqa: BLE001 — алерт не должен маскировать краш
                log.exception("не удалось отправить crash-алерт админу %s", admin_id)
        raise


if __name__ == "__main__":
    main()
