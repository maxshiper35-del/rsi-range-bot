"""
bot.py — Telegram-бот (aiogram): /start /stats /calc /subscribe, рассылка по тирам.
Точка входа приложения (make run).

SMC-интеграция реализована чистыми функциями (scan_symbol / format_*), которые
тестируются без aiogram; сам aiogram импортируется лениво в main().
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from app import clock
from app.brain import journal
from app.brain.risk import calculate_position
from app.brain.smc_setups import SMCEngine
from app.core.signal_engine import Signal
from app.delivery import graphics, templates


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
              engine: SMCEngine) -> list[tuple]:
    """Авто-режим: сетапы на КАЖДОМ входном ТФ -> сразу АКТИВНЫЕ сигналы (status=open).

    Возвращает список (Signal, signal_id) — по одному на входной ТФ (15m/1h/4h), где
    есть сетап. Пустой список — сетапов нет или все дубли (тот же импульс на баре)."""
    out = []
    for sig in engine.analyze_multi(frames, symbol):
        if journal.find_duplicate_setup(sig) is not None:
            continue
        sid = journal.record_signal(sig)
        out.append((sig, sid))
    return out


def scan_semiauto(symbol: str, frames: dict[str, pd.DataFrame],
                  engine: SMCEngine) -> list[tuple]:
    """Полуавтомат: сетапы на КАЖДОМ входном ТФ -> КАНДИДАТЫ (не рассылать).

    Возвращает список (Signal, signal_id) — оператор подтверждает/отклоняет контекст.
    Пустой список — сетапов нет ЛИБО все уже записаны (дедуп: не спамим оператора тем
    же импульсом на каждом баре).
    """
    out = []
    for sig in engine.analyze_multi(frames, symbol):
        if journal.find_duplicate_setup(sig) is not None:
            continue
        sid = journal.record_candidate(sig)
        out.append((sig, sid))
    return out


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
        "📊 <b>Публичная статистика</b> (по закрытым сделкам)\n"
        "────────────────────\n"
        f"Сделок: <b>{st['trades']}</b>  |  открыто: {st['open']}\n"
        f"Winrate: <b>{st['winrate']:.0%}</b>  ({st['wins']}/{st['trades']})\n"
        f"Матожидание: <b>{st['expectancy_r']:+.2f}R</b> на сделку\n"
        f"Суммарно: <b>{st['total_r']:+.2f}R</b>\n"
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
    "display_leverage": ("Плечо для показа результата в постах, ×N (0 — выключить строку)",
                         "float"),
    "intro_price_stars": ("Цена пробного тарифа в Stars (0 — выключить пробный)", "int"),
    "intro_hours": ("Длительность пробного тарифа в часах", "float"),
    "paid_signal_stars": ("Цена разовой покупки сигнала в Stars (0 — выключить кнопку)", "int"),
    "quarter_price_stars": ("Цена квартала (90 дней) в Stars (0 — скрыть тариф)", "int"),
    "year_price_stars": ("Цена года (365 дней) в Stars (0 — скрыть тариф)", "int"),
    "lifetime_price_stars": ("Цена lifetime в Stars (0 — скрыть тариф)", "int"),
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


def format_terms() -> str:
    """/terms — условия использования: консолидация того, что уже разбросано по /about,
    /subscribe, ANTISCAM_LINE, в один документ. Добросовестный черновик, НЕ юридическая
    консультация — перед публичным использованием стоит показать юристу."""
    return (
        "📜 <b>Условия использования</b>\n"
        f"{_LINE}\n"
        "<b>1. Что это.</b> RSI Range Signals — автоматический публикатор технических "
        "SMC-сетапов по крипто-фьючерсам. Не инвестиционный совет, не индивидуальная "
        "рекомендация, не услуга управления капиталом. Подробнее: /about.\n\n"
        "<b>2. Риски.</b> Торговля деривативами с плечом сопряжена с риском полной "
        "потери депозита. Бэктест устойчивого края не показал (/backtest). Прошлые "
        "результаты не определяют будущие. Решение о сделке и её размер — всегда ваши.\n\n"
        "<b>3. Подписка и оплата.</b> Оплата через Telegram Stars или USDT, "
        "идемпотентна (не спишется дважды). Подписка продлевает текущий срок, не "
        "перезаписывает его. <b>Money-back 7 дней</b>: не подошло — вернём Stars по "
        "запросу (/refund у админа).\n\n"
        "<b>4. Данные.</b> Храним минимум: Telegram id, username, статус подписки, "
        "историю платежей. Не передаём третьим лицам. Публичный журнал сделок "
        "(/trackrecord) не содержит личных данных.\n\n"
        "<b>5. Ограничение ответственности.</b> Сервис предоставляется «как есть». Мы "
        "не несём ответственности за торговые решения, принятые на основе публикуемых "
        "сигналов, и за технические сбои (биржи, сети, Telegram).\n\n"
        "<b>6. Честность.</b> Публикуем 100% исходов, включая убыточные. Исход "
        "сделки пишется автоматически по цене биржи, задним числом не редактируется.\n\n"
        "<b>7. Возраст и юрисдикция.</b> 18+. Не предназначено для юрисдикций, где "
        "такие услуги запрещены — ответственность за соответствие местному "
        "законодательству лежит на пользователе.\n\n"
        "<b>8. Антискам.</b> Мы никогда не пишем первыми в личку с инвестиционными "
        "предложениями и не просим оплату вне кнопок этого бота.\n\n"
        "<b>9. Изменения.</b> Условия могут обновляться; действующая версия — всегда "
        "здесь, по команде /terms.\n"
        f"{_LINE}\n"
        "⚠️ Этот документ — добросовестное описание практики сервиса, не юридическая "
        "консультация."
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


# Шаблоны постов сигнального цикла (сигнал/тизер/fill/expired/результат) — в
# app/delivery/templates.py (единый HTML-рендерер). Здесь остаются только
# сервисные format_* (статистика, админка, алерты) — они шлются plain text.

_TF_SECONDS_VIEW = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
                    "1h": 3600, "4h": 14400, "1d": 86400}


def fill_is_fresh(c: dict, now: datetime, factor: float = 2.0, slack: float = 900.0) -> bool:
    """Свежий ли филл: не старее factor×TF + slack от dt бара касания.

    Отсекает «исторические» филлы, которые первый тик после деплоя/рестарта
    находит по старым открытым сигналам (filled_at появился миграцией 009) —
    пост «вход исполнен» задним числом только запутает канал."""
    try:
        filled = datetime.fromisoformat(str(c.get("filled_at")))
    except (TypeError, ValueError):
        return False
    if filled.tzinfo is None:
        filled = filled.replace(tzinfo=timezone.utc)
    tf_sec = _TF_SECONDS_VIEW.get(str(c.get("timeframe")), 3600)
    return (now - filled).total_seconds() <= factor * tf_sec + slack


def caption_fits(html_text: str, limit: int = 1024) -> bool:
    """Лимит Telegram на подпись к фото — 1024 символа ВИДИМОГО текста (теги не считаются)."""
    return len(re.sub(r"<[^>]+>", "", html_text)) <= limit


_UNLOCK_DENIALS = {
    "not_found": "Сигнал не найден.",
    "closed": "Сигнал уже закрыт — результат опубликован в канале.",
    "disabled": "Разовая покупка сейчас недоступна.",
}


MILESTONES = (10, 25, 50, 100, 250, 500, 1000)


def parse_vipchannel_args(text: str) -> tuple | None:
    """'/vipchannel @rsi_range_vip 300' -> ('@rsi_range_vip', 300); None если формат неверный
    или цена вне диапазона Telegram (1-10000 Stars — createChatSubscriptionInviteLink)."""
    parts = (text or "").split()
    if len(parts) != 3 or not parts[2].isdecimal():
        return None
    price = int(parts[2])
    if not (1 <= price <= 10000):
        return None
    return parts[1], price


def next_milestone(trades: int, levels: tuple = MILESTONES) -> int | None:
    """Ближайшая непройденная веха (для прогресс-бара дашборда). None — все взяты."""
    for m in levels:
        if trades < m:
            return m
    return None


def dashboard_data(st: dict, equity: list, by_symbol: dict, by_setup: dict,
                   updated_label: str, period_label: str = "всё время") -> dict:
    """Сборка входа для graphics.build_dashboard_html из журнала. Чистая — тестируется без БД."""
    return {
        "period_label": period_label, "updated_label": updated_label,
        "stats": st, "equity": equity, "by_symbol": by_symbol, "by_setup": by_setup,
        "milestone": {"current": st["trades"], "next": next_milestone(st["trades"])},
    }


def milestone_reached(prev: int, trades: int, levels: tuple = MILESTONES) -> int | None:
    """Наибольшая ТОЛЬКО ЧТО достигнутая веха закрытых сделок (prev < m <= trades).

    «Вехи как контент» (мега-план): факт накопления проверяемой истории — сам по себе
    органический пост, нулевая стоимость. Возвращает одну (старшую) веху за раз."""
    hit = [m for m in levels if prev < m <= trades]
    return max(hit) if hit else None


def watchlist_gate_decision(expectancy_r: float, trades: int,
                            min_trades: int = 5, min_expectancy: float = 0.10) -> bool:
    """Допуск нового символа в авто-watchlist по бэктесту (мега-план MAX, ставка 3).

    Порог ужесточён 2026-07-11 решением владельца (было −0.05R «не хуже нейтрали»):
    первая живая ротация (10 символов) дала заметный разрыв в качестве — 3 монеты
    с явным преимуществом (ARB +0.53R, XRP +0.51R, LAB +0.15R) и 4 около нуля
    (XAU +0.09R, DOGE +0.08R, HYPE +0.02R, SKL +0.00R, статистически неотличимо от
    шума на n=15-28). +0.10R — граница РОВНО в этом разрыве, не подгонка задним
    числом под желаемый список: LAB и XAU разделяет наибольший скачок во всей
    выборке. Цена решения: рост watchlist (и темп набора статистики) медленнее,
    чем при мягком пороге — сознательный выбор качества над скоростью. Мало
    сделок (< min_trades) = сетапы почти не встречаются = смысла добавлять нет."""
    return trades >= min_trades and expectancy_r >= min_expectancy


def plan_watchlist_rotation(current: list, top: list, gate_results: dict) -> tuple:
    """Новый состав watchlist из текущего, топа по ликвидности и результатов гейта.

    Правила: символы текущего списка, выпавшие из топа, УДАЛЯЮТСЯ (ликвидность ушла);
    новые кандидаты добавляются только с gate_results[sym] == True. Порядок — как в top
    (по объёму). Если итог пуст — оставляем текущий (не даём боту остаться без рынка).
    -> (new_list, added, removed). Чистая функция."""
    current_set = set(current)
    new_list = [s for s in top if s in current_set or gate_results.get(s)]
    if not new_list:
        return current, [], []
    added = [s for s in new_list if s not in current_set]
    removed = [s for s in current if s not in new_list]
    return new_list, added, removed


def unlock_verdict(row, price: int) -> str:
    """Можно ли продать разовый доступ к сигналу (paid media). Чистая функция.

    'closed' — продавать закрытый сигнал бессмысленно и нечестно: исход уже
    опубликован в канале бесплатно."""
    if price <= 0:
        return "disabled"
    if row is None or row["status"] in ("candidate", "rejected"):
        return "not_found"
    if row["result_r"] is not None or row["status"] in ("tp1", "tp2", "tp3",
                                                        "stopped", "expired"):
        return "closed"
    return "ok"


def build_signal_chart_data(sig: Signal, sid: int, df: pd.DataFrame,
                            updated_label: str, max_bars: int = 56) -> dict:
    """Собрать вход для graphics.build_signal_chart_html из Signal + свежих баров.
    Чистая функция — тестируется без биржи и браузера."""
    candles = df[["open", "high", "low", "close"]].tail(max_bars).values.tolist()
    if not candles:
        raise ValueError(f"нет баров для графика {sig.symbol} {sig.timeframe}")
    return {
        "signal_id": sid, "symbol": sig.symbol, "side": sig.side.value,
        "timeframe": sig.timeframe, "entry": sig.entry, "stop": sig.stop,
        "tps": list(sig.take_profits), "candles": candles,
        "rr": sig.risk_reward, "confidence_pct": sig.confidence * 100,
        "updated_label": updated_label,
    }


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


_VERIFIER_PATH = Path(__file__).parent / "assets" / "verifier.html"


def load_verifier_html() -> bytes:
    """Самодостаточная страница проверки sha256 (офлайн, WebCrypto, без внешних запросов) —
    приложение к /trackrecord: юзер проверяет неизменность CSV без доверия к нам."""
    return _VERIFIER_PATH.read_bytes()


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


def weekly_report_period(now_dt: datetime) -> tuple[datetime, datetime]:
    """[начало, конец) ПРЕДЫДУЩЕЙ полной ISO-недели относительно now_dt.

    should_post_weekly срабатывает на первом такте новой недели (обычно — секунды после
    полуночи понедельника): период «эта неделя с начала до сейчас» на тот момент почти
    пуст. Репортим неделю, которая только что ЗАКОНЧИЛАСЬ, а не только начавшуюся (живой
    инцидент 2026-07-13: карточка с окном 00:00–00:06 показала 0 закрытых сделок)."""
    iso_year, iso_week, _ = now_dt.isocalendar()
    this_week_start = datetime.fromisocalendar(iso_year, iso_week, 1).replace(tzinfo=timezone.utc)
    return this_week_start - timedelta(days=7), this_week_start


def format_channel_live_board(st: dict, by_symbol: dict, cohort: dict | None = None,
                              by_setup: dict | None = None) -> str:
    """Живое табло для закреплённого поста канала: не winrate (самая манипулируемая метрика
    ниши), а матожидание в R, размер выборки, разбивка по символам/сетапам, когорты (semiauto)."""
    lines = [f"📌 <b>RSI RANGE — живое табло</b>\n{_LINE}"]
    if st["trades"] == 0:
        lines.append("Сделки ещё копятся, первые результаты скоро появятся здесь.")
    else:
        lines.append(f"Всего закрыто: <b>{st['trades']}</b>  (открыто {st['open']})")
        lines.append(f"Матожидание: <b>{st['expectancy_r']:+.2f}R</b> на сделку  |  "
                     f"Итог: <b>{st['total_r']:+.2f}R</b>  |  Winrate: {st['winrate']:.0%}")
        warn = _small_sample_warning(st["trades"]).rstrip()
        if warn:
            lines.append(warn)
        if by_symbol:
            lines.append("\n<b>По инструментам:</b>")
            for symbol, s in sorted(by_symbol.items()):
                lines.append(f"  • {templates.esc(symbol)}: {s['trades']} сд, "
                             f"exp {s['expectancy_r']:+.2f}R, итог {s['total_r']:+.2f}R")
        if by_setup:
            lines.append("\n<b>По сетапам:</b>")
            for setup, s in sorted(by_setup.items()):
                lines.append(f"  • {templates.esc(setup)}: {s['trades']} сд, "
                             f"exp {s['expectancy_r']:+.2f}R, итог {s['total_r']:+.2f}R")
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
    ("dashboard", "визуальный дашборд: эквити-кривая, разбивки, вехи"),
    ("calc", "калькулятор позиции: /calc <деп> <риск%> <вход> <стоп>"),
    ("subscribe", "подписка — все сигналы"),
    ("promo", "применить промокод: /promo <код>"),
    ("about", "как устроен сервис, честный дисклеймер"),
    ("terms", "условия использования: риски, оплата, возврат, данные"),
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
    ("refund", "вернуть последний Stars-платёж (money-back): /refund <tg_id>"),
    ("vipchannel", "VIP-канал на нативной Stars-подписке: /vipchannel <@канал> <цена> | status"),
    ("revoke", "снять подписку: /revoke <tg_id>"),
    ("users", "счётчики юзеров"),
    ("announce", "объявление в канал + подписчикам"),
    ("confirm", "подтвердить кандидата (semiauto): /confirm <id>"),
    ("reject", "отклонить кандидата (semiauto): /reject <id>"),
    ("candidates", "кандидаты, ждущие решения (semiauto)"),
    ("report", "edge оператора: confirmed vs rejected (semiauto)"),
]


def main_menu_layout(is_admin: bool = False) -> list:
    """Раскладка постоянного меню (текст кнопок) — чистая функция для тестов.
    Каждой кнопке соответствует F.text-хэндлер в main().

    Раскладки НЕ общие: у админа нет смысла держать на экране кнопки для
    подписчика (О боте/Бэктест/Глоссарий/Промокод — статический контент для
    доверия, Калькулятор/Подписка — самообслуживание подписчика: админ и так
    получает все сигналы вне подписки, /admin_ids). Вместо них — операционные
    отчёты без аргументов (Юзеры/Growth/По сетапам), которые иначе набирались
    бы вручную; конфигурация (пауза/цены/сетапы и т.п.) остаётся ТОЛЬКО в
    инлайн-экранах /admin — не дублируем инструмент выбора в постоянную клавиатуру."""
    if is_admin:
        return [
            ["📊 Статистика", "📄 Трек-рекорд"],
            ["📈 Дашборд", "🛠 Админ-панель"],
            ["📟 Статус", "🩺 Health"],
            ["👥 Юзеры", "📈 Growth"],
            ["🎯 По сетапам", "❓ Помощь"],
        ]
    return [
        ["📊 Статистика", "📄 Трек-рекорд"],
        ["📈 Дашборд", "🧮 Калькулятор"],
        ["💳 Подписка", "ℹ️ О боте"],
        ["🧪 Бэктест", "📖 Глоссарий"],
        ["🎟 Промокод", "❓ Помощь"],
    ]


BOT_DESCRIPTION = (
    "Сигналы по крипто-фьючерсам (SMC) с честной статистикой: публикуем 100% исходов, "
    "включая убыточные. Проверяемый CSV-трек-рекорд: sha256 + офлайн-верификатор "
    "(/trackrecord). Исходы пишутся автоматически по ценам биржи — без ручных правок. "
    "Триал при старте. Не финансовый совет: бэктест устойчивого края не показал — "
    "это инструмент и прозрачная история, не обещание прибыли."
)


async def register_bot_profile(bot) -> None:
    """Описание в профиле бота («Что умеет этот бот?») — было пустым. Идемпотентно,
    ставится на старте. Короткое описание задано владельцем вручную и НЕ трогается."""
    try:
        await bot.set_my_description(description=BOT_DESCRIPTION)
    except Exception:                                  # noqa: BLE001 — профиль не роняет старт
        pass


async def register_bot_commands(bot, admin_ids) -> None:
    """Очистить Telegram-меню команд (кнопка '/') — сознательно ОТКЛЮЧЕНО: дублирует
    постоянную клавиатуру (main_menu_layout) вторым, расходящимся списком. Единственный
    источник документации команд теперь /help (format_help, строится из тех же
    USER_COMMANDS/ADMIN_COMMANDS). Команды текстом по-прежнему работают — Telegram лишь
    не подсказывает их в автокомплите. Explicit clear обязателен: Telegram кэширует
    прошлую регистрацию на своей стороне, простое неисполнение set_my_commands её не уберёт."""
    from aiogram.types import BotCommandScopeChat, BotCommandScopeDefault

    await bot.set_my_commands([], scope=BotCommandScopeDefault())
    for admin_id in admin_ids:
        try:
            await bot.set_my_commands([], scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:                              # noqa: BLE001 — админ не открывал чат с ботом
            pass


def format_help(is_admin: bool) -> str:
    """/help — единственный источник списка команд (замена отключённому Telegram-меню
    '/'); строится из USER_COMMANDS/ADMIN_COMMANDS, чтобы не расходиться с фактическим
    списком хэндлеров (старый ручной текст пропускал /promo)."""
    body = "\n".join(f"/{c} — {d}" for c, d in USER_COMMANDS if c not in ("start", "help"))
    text = f"Команды:\n{body}\n{ANTISCAM_LINE}\n⚠️ Не финансовый совет. Риск на сделку 1–2%."
    if is_admin:
        admin_only = ADMIN_COMMANDS[len(USER_COMMANDS):]
        admin_body = "\n".join(f"/{c} — {d}" for c, d in admin_only)
        text += f"\n\n🛠 Админ-команды:\n{admin_body}"
    return text


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


def poller_tick_slack_seconds(timeframes: list) -> float:
    """Порог 'тик poller устарел' для /health — ЗАВИСИТ от цикла скана: poller спит до
    закрытия младшего ТФ (market_data.run_poller), значит нормальный промежуток между
    тиками растёт вместе с ним. Захардкоженный порог под 15m (900с) после перехода на
    1h,4h (гейт F, docs/UPGRADE_BRIEF_2026_07.md) давал бы ложные тревоги при обычном
    ретрае биржи — считаем от факта: youngest_tf * 1.15 + 300с запаса на скан/ретраи."""
    secs = [_TF_SECONDS_VIEW[tf] for tf in timeframes if tf in _TF_SECONDS_VIEW]
    youngest = min(secs) if secs else 3600
    return max(youngest * 1.15, youngest + 300)


def format_health(uptime_seconds: float, last_tick_at: str | None,
                  last_backup_at: str | None, db_size_bytes: int, wal_mode: str,
                  tick_slack_seconds: float = 3700.0) -> str:
    """/health — быстрый живой чек: аптайм, последний тик поллера, бэкап, БД, WAL.
    tick_slack_seconds — см. poller_tick_slack_seconds (дефолт — старое поведение
    под 15m, для обратной совместимости вызовов без явного расчёта)."""
    from datetime import datetime

    now = datetime.now(timezone.utc)
    up_h = uptime_seconds / 3600
    tick_age = _fmt_age(last_tick_at, now)
    backup_age = _fmt_age(last_backup_at, now)
    tick_warn = "⚠️ " if (not last_tick_at or (now - datetime.fromisoformat(last_tick_at))
                          .total_seconds() > tick_slack_seconds) else "✅ "
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
        InputPaidMediaPhoto,
        KeyboardButton,
        LabeledPrice,
        ReplyKeyboardMarkup,
        WebAppInfo,
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

    def _main_menu_kb(is_admin: bool = False) -> ReplyKeyboardMarkup:
        """Постоянное меню внизу чата — юзеру не нужно помнить команды.
        is_persistent: Telegram держит клавиатуру на экране всегда (Bot API 6.7),
        даже если юзер её сворачивал; у админа — дополнительные кнопки."""
        # «📈 Дашборд»: при заданном webapp_url — web_app-кнопка (Mini App внутри
        # Telegram), иначе обычная (хэндлер отдаёт самодостаточный HTML-файл).
        wurl = runtime.webapp_url()

        def _btn(text: str) -> KeyboardButton:
            if text == "📈 Дашборд" and wurl.startswith("https://"):
                return KeyboardButton(text=text, web_app=WebAppInfo(url=wurl))
            return KeyboardButton(text=text)

        return ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True, keyboard=[
            [_btn(t) for t in row] for row in main_menu_layout(is_admin)])

    def _subscribe_kb() -> InlineKeyboardMarkup:
        rows = []
        if runtime.intro_price_stars() > 0 and runtime.intro_hours() > 0:
            rows.append([InlineKeyboardButton(
                text=f"⭐ {runtime.intro_price_stars()} Stars / "
                     f"{runtime.intro_hours():g} ч — попробовать",
                callback_data="pay_stars_intro")])
        rows.append([InlineKeyboardButton(
            text=f"⭐ {runtime.price_stars()} Stars / {_fmt_days(runtime.sub_days())}",
            callback_data="pay_stars")])
        # длинные тиры (разовые инвойсы: нативные Stars-подписки >30 дней невозможны);
        # скидка считается от актуальной месячной цены — не соврёт при смене цен
        def _disc(price: int, months: float) -> str:
            base = runtime.price_stars() * months
            return (f" (−{round((1 - price / base) * 100)}%)"
                    if 0 < price < base else "")

        q, y, lf = (runtime.quarter_price_stars(), runtime.year_price_stars(),
                    runtime.lifetime_price_stars())
        if q > 0:
            rows.append([InlineKeyboardButton(
                text=f"⭐ {q} Stars / 90 дней{_disc(q, 3)}",
                callback_data="pay_stars_quarter")])
        if y > 0:
            rows.append([InlineKeyboardButton(
                text=f"⭐ {y} Stars / год{_disc(y, 12)}",
                callback_data="pay_stars_year")])
        if lf > 0:
            rows.append([InlineKeyboardButton(
                text=f"⭐ {lf} Stars / навсегда", callback_data="pay_stars_life")])
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
            "👋 <b>RSI Range Signals</b> — инструмент и честная статистика "
            "(не «прибыль 400%»).\n\n"
            "• 100% исходов публикуются — включая убыточные\n"
            "• Исход пишется автоматически по ценам биржи, без ручных правок\n"
            "• Трек-рекорд проверяем: CSV + sha256 + офлайн-верификатор (/trackrecord)\n\n"
            "Меню внизу экрана — всё в один тап; команды тоже работают (/help).\n"
            f"{ANTISCAM_LINE}\n"
            "⚠️ Не финансовый совет. Риск на сделку 1–2%.\n\n"
            "📣 Понравится — поделитесь: t.me/rsirange_bot?start=src_share")
        if applied:
            text = (f"🎁 <b>Открыт полный доступ на {trial_days} дн.</b> — "
                    f"все сигналы без ограничений.\n\n" + text)
        await m.answer(text, reply_markup=_main_menu_kb(_is_admin(m)), parse_mode="HTML")

    @dp.message(Command("about"))
    @dp.message(F.text == "ℹ️ О боте")
    async def _about(m):
        await m.answer(format_about())

    @dp.message(Command("terms"))
    async def _terms(m):
        await m.answer(format_terms(), parse_mode="HTML")

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
            uname = templates.esc(m.from_user.username or m.from_user.id)
            text += (f"\n\n💵 USDT: оплати по кнопке, в КОММЕНТАРИИ к платежу укажи «{uname}», "
                     "затем нажми «Я оплатил» — активируем в течение дня.")
        text += ("\n\n💯 <b>Money-back 7 дней</b>: не подошло — вернём Stars по запросу "
                 "(напишите админу канала @rsi_range).")
        await m.answer(text, reply_markup=_subscribe_kb(), parse_mode="HTML")

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

    async def _send_stars_invoice(chat_id: int, days: float, stars: int,
                                  label: str | None = None):
        human = label or _fmt_days(days)
        await bot.send_invoice(
            chat_id=chat_id,
            title="Подписка RSI Range Signals",
            description=f"Доступ ко всем сигналам на {human}.",
            payload=f"sub:{days}",                      # _paid парсит float — дробные дни ок
            provider_token="",                          # Stars: провайдер не нужен
            currency="XTR",
            prices=[LabeledPrice(label=f"{human} доступа", amount=stars)],
        )

    @dp.callback_query(F.data == "pay_stars")
    async def _pay_stars(cq):
        await _send_stars_invoice(cq.from_user.id, runtime.sub_days(), runtime.price_stars())
        await cq.answer()

    @dp.callback_query(F.data == "pay_stars_intro")
    async def _pay_stars_intro(cq):
        await _send_stars_invoice(cq.from_user.id, round(runtime.intro_hours() / 24, 6),
                                  runtime.intro_price_stars())
        await cq.answer()

    @dp.callback_query(F.data == "pay_stars_quarter")
    async def _pay_stars_quarter(cq):
        await _send_stars_invoice(cq.from_user.id, 90, runtime.quarter_price_stars())
        await cq.answer()

    @dp.callback_query(F.data == "pay_stars_year")
    async def _pay_stars_year(cq):
        await _send_stars_invoice(cq.from_user.id, 365, runtime.year_price_stars(),
                                  label="1 год")
        await cq.answer()

    @dp.callback_query(F.data == "pay_stars_life")
    async def _pay_stars_life(cq):
        # lifetime = 36500 дней (100 лет): без спец-статуса в БД — просто далёкий paid_until
        await _send_stars_invoice(cq.from_user.id, 36500, runtime.lifetime_price_stars(),
                                  label="навсегда (lifetime)")
        await cq.answer()

    @dp.callback_query(F.data.startswith("unlock:"))
    async def _unlock(cq):
        # разовая покупка сигнала: PNG-карта + полный разбор как paid media (Stars).
        # Telegram сам открывает контент после оплаты — своего платёжного цикла нет.
        sid_s = cq.data.split(":", 1)[1]
        price = runtime.paid_signal_stars()
        if not sid_s.isdigit():
            await cq.answer("Сигнал не найден.", show_alert=True)
            return
        sid = int(sid_s)
        row = await asyncio.to_thread(journal.signal_row, sid)
        verdict = unlock_verdict(row, price)
        if verdict != "ok":
            await cq.answer(_UNLOCK_DENIALS[verdict], show_alert=True)
            return
        uid = cq.from_user.id
        sig = journal.row_to_signal(row)
        # платным/админам продавать нечестно — у них и так полный доступ
        if uid in cfg.admin_ids or await asyncio.to_thread(payments.is_paid, uid):
            await bot.send_message(uid, templates.render_signal_dm(sig, sid),
                                   parse_mode="HTML")
            await cq.answer("У вас полный доступ — сигнал отправлен бесплатно.")
            return
        try:
            png = await asyncio.to_thread(_signal_chart_png, sig, sid)
        except Exception as e:                          # noqa: BLE001 — биржа/рендер
            log.warning("unlock #%s: график не собрался: %s", sid, e)
            await cq.answer("Не удалось подготовить сигнал — попробуйте чуть позже.",
                            show_alert=True)
            return
        caption = templates.render_signal_dm(sig, sid)
        try:
            await bot.send_paid_media(
                chat_id=uid, star_count=price,
                media=[InputPaidMediaPhoto(media=BufferedInputFile(
                    png, filename=f"signal_{sid}.png"))],
                payload=f"unlock:{sid}:{price}",
                caption=caption if caption_fits(caption) else None, parse_mode="HTML")
            log.info("unlock #%s: paid media (%d⭐) отправлен юзеру %s", sid, price, uid)
            await cq.answer("Сигнал отправлен в личку — откройте его за ⭐.")
        except Exception as e:                          # noqa: BLE001 — закрытая личка и т.п.
            log.warning("unlock #%s -> %s: %s", sid, uid, e)
            await cq.answer("Откройте личку с ботом (/start) и нажмите кнопку ещё раз.",
                            show_alert=True)

    @dp.purchased_paid_media()
    async def _paid_media_bought(p):
        # учёт: Stars зачисляются на баланс бота платформой; здесь — только след в логе
        log.info("unlock куплен: юзер %s, payload=%s",
                 p.from_user.id, p.paid_media_payload)

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
    @dp.message(F.text == "❓ Помощь")
    async def _help(m):
        is_adm = _is_admin(m)
        # меню прикрепляется и здесь: старые юзеры без /start тоже получают клавиатуру
        await m.answer(format_help(is_adm), reply_markup=_main_menu_kb(is_adm))

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
        await m.answer(await asyncio.to_thread(lambda: format_stats(journal.stats())),
                       parse_mode="HTML")

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
        await m.answer_document(
            BufferedInputFile(load_verifier_html(), filename="verifier.html"),
            caption="🔍 Проверка в 1 клик: откройте файл в браузере, перетащите CSV — "
                    "sha256 посчитается локально (офлайн, без отправки куда-либо).")

    @dp.message(Command("dashboard"))
    @dp.message(F.text == "📈 Дашборд")
    async def _dashboard(m):
        # web_app-кнопка (если webapp_url задан) открывает Mini App сама и сюда не доходит;
        # этот путь — самодостаточный HTML-файл (офлайн, тот же документ, что и Mini App).
        def _build():
            updated = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
            data = dashboard_data(journal.stats(), journal.equity_series(),
                                  journal.stats_by_symbol(), journal.stats_by_setup(), updated)
            return graphics.build_dashboard_html(data)
        html = await asyncio.to_thread(_build)
        await m.answer_document(
            BufferedInputFile(html.encode("utf-8"), filename="dashboard.html"),
            caption="📈 Дашборд трек-рекорда — откройте в браузере: эквити-кривая, "
                    "разбивка по инструментам и сетапам, прогресс до вехи. Работает офлайн.")

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
            [b(text="🖼 График сигнала: вкл" if eff["signal_chart_enabled"]
               else "🖼 График сигнала: выкл", callback_data="adm_charttoggle"),
             b(text="⚡️ Стрим: вкл" if eff["stream_enabled"] else "⚡️ Стрим: выкл",
               callback_data="adm_streamtoggle")],
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
            [b(text=(f"🧪 Пробный: {eff['intro_price_stars']}⭐/{eff['intro_hours']:g}ч"
                     if eff['intro_price_stars'] else "🧪 Пробный тариф: выкл"),
               callback_data="adm_prompt_intro_price_stars"),
             b(text=f"🧪 Часы: {eff['intro_hours']:g}",
               callback_data="adm_prompt_intro_hours")],
            [b(text=(f"🔓 Разовый сигнал: {eff['paid_signal_stars']}⭐"
                     if eff['paid_signal_stars'] else "🔓 Разовый сигнал: выкл"),
               callback_data="adm_prompt_paid_signal_stars")],
            [b(text=f"📅 Квартал: {eff['quarter_price_stars']}⭐",
               callback_data="adm_prompt_quarter_price_stars"),
             b(text=f"📅 Год: {eff['year_price_stars']}⭐",
               callback_data="adm_prompt_year_price_stars")],
            [b(text=f"♾ Lifetime: {eff['lifetime_price_stars']}⭐",
               callback_data="adm_prompt_lifetime_price_stars")],
            [b(text=f"📢 Плечо в постах: ×{eff['display_leverage']:g}"
                    if eff['display_leverage'] else "📢 Плечо в постах: выкл",
               callback_data="adm_prompt_display_leverage")],
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
    @dp.message(F.text == "🛠 Админ-панель")
    async def _admin(m):
        if _is_admin(m):
            await _panel(m.chat.id)

    @dp.message(Command("status"))
    @dp.message(F.text == "📟 Статус")
    async def _statuscmd(m):
        if not _is_admin(m):
            return
        eff = await asyncio.to_thread(runtime.effective)
        users = await asyncio.to_thread(payments.user_counts)
        await m.answer(format_admin_status(eff, users))

    @dp.message(Command("health"))
    @dp.message(F.text == "🩺 Health")
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
        slack = poller_tick_slack_seconds(runtime.timeframes())
        await m.answer(format_health(uptime, runtime.last_poller_tick_at(),
                                     runtime.last_backup_at(), size, wal, slack))

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

    @dp.message(Command("vipchannel"))
    async def _vipchannel(m):
        # VIP-канал на нативной Stars-подписке (мега-план MAX): Telegram сам гейтит
        # членство — платящий добавляется автоматически, при истечении удаляется, без
        # зависимости от того, замьютил ли юзер бота (DM теряет ~треть аудитории — видно
        # по логам send: Forbidden). Требует ОДНОРАЗОВОГО действия владельца: создать
        # приватный канал в приложении Telegram (Bot API каналы не создаёт) и добавить
        # бота админом с правом приглашать пользователей — дальше всё этой командой.
        if not _is_admin(m):
            return
        parts = (m.text or "").split()
        if len(parts) == 2 and parts[1] == "status":
            chan = runtime.vip_channel_id()
            link = runtime.vip_invite_link()
            if not chan:
                await m.answer("VIP-канал не настроен. Формат: /vipchannel <@username|id> "
                               "<цена в Stars>")
                return
            await m.answer(f"Канал: {chan}\nСсылка: {link or '—'}")
            return
        args = parse_vipchannel_args(m.text or "")
        if args is None:
            await m.answer(
                "Формат: /vipchannel <@username|id> <цена в Stars, 1-10000>\n"
                "или: /vipchannel status\n\n"
                "Перед этим: создайте приватный канал в Telegram и добавьте "
                f"@{(await bot.me()).username} админом с правом «Приглашать пользователей».\n"
                "Период подписки фиксирован Telegram — ровно 30 дней.")
            return
        chat_id, price = args
        try:
            link = await bot.create_chat_subscription_invite_link(
                chat_id=chat_id, subscription_period=2592000, subscription_price=price,
                name="RSI Range VIP")
        except Exception as e:                          # noqa: BLE001 — нет прав/канал не найден
            await m.answer(f"Не удалось создать ссылку: {e}\n"
                           "Проверьте: бот — админ канала, есть право «Приглашать "
                           "пользователей», @username/id верны.")
            return
        await asyncio.to_thread(runtime.set_value, "vip_channel_id", chat_id)
        await asyncio.to_thread(runtime.set_value, "vip_invite_link", link.invite_link)
        await m.answer(
            f"✅ VIP-канал настроен: {chat_id}, {price}⭐/30 дней.\n\n"
            f"Ссылка для подписки:\n{link.invite_link}\n\n"
            "Опубликуйте её в /subscribe или основном канале — оплативший добавится "
            "автоматически, при неоплате следующего периода Telegram удалит сам.")

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
        # (settings-ключ, парсер, минимум) — ВАЛИДИРУЕМ до записи: иначе строка '1k' легла
        # бы в числовую настройку и роняла runtime-getter -> мёртвая подписка/админка (аудит [6])
        spec = {"stars": ("sub_price_stars", int, 1),
                "usdt": ("sub_price_usdt", float, 0.0),
                "days": ("sub_days", float, 0.0)}
        if len(parts) == 3 and parts[1] in spec:
            key, parser, lo = spec[parts[1]]
            try:
                v = parser(parts[2])
                assert v >= lo
            except Exception:                          # noqa: BLE001
                await m.answer(f"Неверное значение «{parts[2]}». "
                               f"Формат: /price {parts[1]} <число ≥ {lo:g}>")
                return
            await _set(m, key, v, f"{parts[1]} = {v:g}")
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
    @dp.message(F.text == "🎯 По сетапам")
    async def _bysetup(m):
        if not _is_admin(m):
            return
        by_setup = await asyncio.to_thread(journal.stats_by_setup)
        await m.answer(format_by_setup(by_setup))

    @dp.message(Command("growth"))
    @dp.message(F.text == "📈 Growth")
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

    @dp.message(Command("refund"))
    async def _refund(m):
        # money-back (мега-план волна 1): вернуть последний Stars-платёж юзера.
        # Порядок: API-возврат ПЕРВЫМ, пометка в БД — после успеха (см. payments)
        if not _is_admin(m):
            return
        parts = (m.text or "").split()
        if len(parts) < 2 or not parts[1].isdecimal():
            await m.answer("Формат: /refund <tg_id> — вернуть последний Stars-платёж "
                           "(money-back 7 дней)")
            return
        uid = int(parts[1])
        pay = await asyncio.to_thread(payments.find_refundable_stars_payment, uid)
        if not pay:
            await m.answer("Оплаченных Stars-платежей у юзера не найдено "
                           "(или уже возвращены).")
            return
        try:
            await bot.refund_star_payment(
                user_id=uid, telegram_payment_charge_id=pay["charge_id"])
        except Exception as e:                          # noqa: BLE001 — отказ Telegram
            await m.answer(f"Telegram отказал в возврате: {e}")
            return
        await asyncio.to_thread(payments.mark_refunded, pay["id"], uid, pay["days"])
        await m.answer(f"↩️ Возвращено {pay['amount']:g}⭐ юзеру {uid}; "
                       f"подписка скорректирована (−{_fmt_days(pay['days'])}).")
        try:
            await bot.send_message(uid, "↩️ Ваш платёж возвращён — Stars вернутся на "
                                        "баланс Telegram. Спасибо, что попробовали нас.")
        except Exception as e:                          # noqa: BLE001
            log.warning("refund notify %s: %s", uid, e)

    @dp.message(Command("users"))
    @dp.message(F.text == "👥 Юзеры")
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
        elif data == "adm_charttoggle":
            await asyncio.to_thread(runtime.set_value, "signal_chart_enabled",
                                    "0" if runtime.signal_chart_enabled() else "1")
        elif data == "adm_streamtoggle":
            await asyncio.to_thread(runtime.set_value, "stream_enabled",
                                    "0" if runtime.stream_enabled() else "1")
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
        elif data.startswith("adm_") and data[4:] in _SETUP_CODES_REV:
            # callback = f"adm_{код}" (см. _strategy_kb), в _SETUP_CODES_REV ключ БЕЗ префикса —
            # раньше сравнивали data целиком и тумблеры сетапов были мертвы (аудит [4])
            tag = _SETUP_CODES_REV[data[4:]]
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

    def _signal_chart_png(sig, sid):
        # синхронно (ccxt + Playwright) — вызывать только через asyncio.to_thread
        from app.data.market_data import fetch_closed_ohlcv
        df = fetch_closed_ohlcv(sig.symbol, sig.timeframe, limit=57)
        data = build_signal_chart_data(
            sig, sid, df, datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC"))
        return graphics.render_signal_chart(data)

    def _watchlist_gate(symbol: str) -> tuple:
        # бэктест-гейт кандидата в авто-watchlist: 3 мес 1h на включённых сетапах
        # (порог — watchlist_gate_decision). Синхронно — только через asyncio.to_thread.
        from app.brain.smc_setups import SMCConfig
        from app.data.market_data import _TF_SECONDS, fetch_history
        from app.sim.backtest import run_backtest

        bars = int(3 * 30.44 * 86400 / _TF_SECONDS["1h"])
        df = fetch_history(symbol, "1h", target_bars=bars)
        rep = run_backtest(df, symbol, "1h",
                           cfg=SMCConfig(use_time_filter=False,
                                         enabled_setups=tuple(runtime.enabled_setups())))
        return (watchlist_gate_decision(rep.expectancy_r, rep.trades),
                rep.expectancy_r, rep.trades)

    async def _broadcast(sig, sid):
        try:
            # runtime, не cfg: иначе смена режима из админки без рестарта не меняла
            # бы тело рассылки (poller-то уже решает по runtime.signal_mode())
            if runtime.signal_mode() == "auto":
                full = templates.render_signal_dm(sig, sid)
            else:
                # алерт оператору — plain-шаблон, экранируем под parse_mode=HTML
                full = templates.esc(format_candidate_alert(sig, sid))
        except Exception:                              # noqa: BLE001 — формат не роняет цикл
            log.exception("формат алерта #%s", sid)
            full = f"🔔 Сигнал #{sid} (ошибка форматирования — детали в журнале)"
        teaser = templates.render_teaser(sig, sid, stats=journal.stats())  # соц-пруф на пейволле
        # разовая покупка сигнала (paid media): кнопка на тизере, если включена цена
        unlock_price = runtime.paid_signal_stars()
        unlock_kb = None
        if unlock_price > 0:
            unlock_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text=f"🔓 Открыть этот сигнал — {unlock_price}⭐",
                callback_data=f"unlock:{sid}")]])
        # пост в канал (если включён) — полный или тизер, по режиму канала
        channel = runtime.signals_channel()
        if channel and runtime.channel_enabled():
            ch_is_teaser = runtime.channel_mode() != "full"
            if not ch_is_teaser:
                ch_text = templates.render_signal_channel(sig, sid)
                if runtime.cornix_block_enabled():      # машиночитаемый блок для Cornix-юзеров
                    ch_text += templates.esc(format_cornix_block(sig))
            else:
                ch_text = teaser
            # карта сигнала (фича-флаг): свечи + зоны входа/стопа/тейков; любой сбой
            # рендера/биржи не блокирует пост — падаем обратно на текст
            photo = None
            if runtime.channel_mode() == "full" and runtime.signal_chart_enabled():
                try:
                    photo = await asyncio.to_thread(_signal_chart_png, sig, sid)
                except Exception as e:                 # noqa: BLE001
                    log.warning("график сигнала #%s: %s", sid, e)
            try:
                if photo is not None and caption_fits(ch_text):
                    msg = await bot.send_photo(
                        channel, BufferedInputFile(photo, filename=f"signal_{sid}.png"),
                        caption=ch_text, parse_mode="HTML")
                    log.info("сигнал #%s: пост в канал OK (фото, msg_id=%s)", sid, msg.message_id)
                else:
                    if photo is not None:               # подпись не влезла — фото отдельно
                        await bot.send_photo(
                            channel, BufferedInputFile(photo, filename=f"signal_{sid}.png"))
                    msg = await bot.send_message(
                        channel, ch_text, parse_mode="HTML",
                        reply_markup=unlock_kb if ch_is_teaser else None)
                    log.info("сигнал #%s: пост в канал OK (msg_id=%s%s)", sid, msg.message_id,
                             ", фото отдельно" if photo is not None else "")
            except Exception as e:                     # noqa: BLE001 — канал не роняет рассылку
                log.warning("пост в канал %s: %s", channel, e)
        # воронка: админы/платные -> полный; free -> полный до лимита/нед, дальше тизер
        plan = await asyncio.to_thread(payments.plan_recipients, sid,
                                       runtime.free_per_week(), tuple(cfg.admin_ids))
        sent = 0
        for uid, kind in plan:
            try:
                await bot.send_message(uid, full if kind == "full" else teaser,
                                       parse_mode="HTML",
                                       reply_markup=unlock_kb if kind == "teaser" else None)
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
        # онбординг-дрип триала (мега-план MAX): день 1 / день 3 / за сутки до конца.
        # Оплатившие исключены в самом запросе; одна стадия на юзера за такт.
        # Протухшие (триал истёк до включения фичи) закрываются молча — без спама задним числом.
        stale = await asyncio.to_thread(payments.expire_stale_trial_drips,
                                        float(runtime.trial_days()))
        if stale:
            log.info("дрип: закрыто задним числом %d протухших триалов", stale)
        for uid, stage in await asyncio.to_thread(payments.due_for_trial_drip,
                                                  float(runtime.trial_days())):
            try:
                await bot.send_message(uid, templates.TRIAL_DRIP[stage], parse_mode="HTML")
                await asyncio.to_thread(payments.mark_trial_drip, uid, stage)
                log.info("дрип стадия %d -> %s", stage, uid)
            except Exception as e:                     # noqa: BLE001 — заблокировал бота и т.п.
                log.warning("дрип %d -> %s: %s", stage, uid, e)
                # стадию всё равно отмечаем: юзер с закрытой личкой не должен
                # получать ретраи каждый час до конца триала
                await asyncio.to_thread(payments.mark_trial_drip, uid, stage)
        # win-back: одно напоминание в первые 2 дня после истечения подписки. Персональный
        # одноразовый промокод wb<tg_id> (+7 дн, сгорает за 5) — конкретный повод вернуться
        # сейчас. Промокод выдаём ДО отправки; при сбое отправки всё равно mark_reminded,
        # чтобы юзер с закрытой личкой не ретраился каждый такт (как в дрипе).
        _WB_DAYS, _WB_VALID = 7, 5
        for row in await asyncio.to_thread(payments.due_for_winback, 2):
            uid = row["tg_id"]
            try:
                code = await asyncio.to_thread(
                    payments.issue_winback_promo, uid, _WB_DAYS, _WB_VALID)
                await bot.send_message(
                    uid,
                    templates.render_winback(code, _WB_DAYS, _WB_VALID, stats=journal.stats()),
                    parse_mode="HTML")
            except Exception as e:                     # noqa: BLE001
                log.warning("winback %s: %s", uid, e)
            await asyncio.to_thread(payments.mark_winback_reminded, uid)
        # авто-watchlist (мега-план MAX, ставка 3): раз в сутки пересобрать список по
        # ликвидности; новые символы — только через бэктест-гейт. Дефолт ВЫКЛ (флаг).
        now_iso = clock.utcnow_iso()
        if (runtime.watchlist_auto_enabled()
                and should_refresh_board(runtime.last_watchlist_refresh_at(), now_iso)):
            try:
                from app.data.market_data import top_liquid_symbols
                current = await asyncio.to_thread(runtime.symbols)
                top = await asyncio.to_thread(
                    top_liquid_symbols, runtime.watchlist_size(),
                    runtime.watchlist_blacklist())
                gate: dict = {}
                for sym in top:
                    if sym in current:
                        continue
                    ok, exp, trades = await asyncio.to_thread(_watchlist_gate, sym)
                    gate[sym] = ok
                    log.info("watchlist-гейт %s: exp=%+.3fR, сделок=%d -> %s",
                             sym, exp, trades, "допущен" if ok else "мимо")
                new_list, added, removed = plan_watchlist_rotation(current, top, gate)
                if added or removed:
                    await asyncio.to_thread(runtime.set_value, "symbols",
                                            ",".join(new_list))
                    text = ("🔄 Авто-watchlist обновлён: " + ", ".join(new_list)
                            + (f"\n➕ добавлены: {', '.join(added)}" if added else "")
                            + (f"\n➖ убраны: {', '.join(removed)}" if removed else ""))
                    for admin_id in cfg.admin_ids:
                        try:
                            await bot.send_message(admin_id, text)
                        except Exception as e:          # noqa: BLE001
                            log.warning("watchlist notify %s: %s", admin_id, e)
            except Exception as e:                     # noqa: BLE001 — ротация не роняет такт
                log.warning("авто-watchlist: %s", e)
            finally:
                # метка ставится и при сбое: не молотить биржу каждый час, повтор завтра
                await asyncio.to_thread(runtime.set_last_watchlist_refresh_at, now_iso)
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
                            await bot.edit_message_text(text, chat_id=channel,
                                                        message_id=msg_id, parse_mode="HTML")
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
                            sent = await bot.send_message(channel, text, parse_mode="HTML")
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
                # помечаем неделю СРАЗУ при входе: карточка и CSV — два отдельных best-effort
                # поста, и если карточка ушла, а CSV упал (или наоборот), на следующем такте
                # НЕ перепостим уже отправленное (аудит [5] — дубли карточек/леджера). Цена —
                # редкий полный пропуск при сбое обоих; /trackrecord доступен вручную всегда.
                await asyncio.to_thread(runtime.set_trackrecord_last_week, week_key)
                # карточка недели (PNG) — отдельный try/except: сбой рендера (Chromium)
                # не должен блокировать CSV ниже — тот честностный артефакт критичнее
                try:
                    week_start, week_end = weekly_report_period(now_dt)
                    wk = await asyncio.to_thread(
                        journal.stats_for_period, week_start.isoformat(), week_end.isoformat())
                    st = await asyncio.to_thread(journal.stats)
                    card_data = {
                        "period_label": f"{week_start.strftime('%d.%m')}"
                                       f"–{(week_end - timedelta(days=1)).strftime('%d.%m.%Y')}",
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
                               f"SHA-256: {checksum[:16]}…\n"
                               "🔍 Проверить неизменность: /trackrecord у @rsirange_bot "
                               "(приложит verifier.html — офлайн-проверка в браузере).")
                    await asyncio.to_thread(journal.record_ledger_hash, len(rows), checksum)
                except Exception as e:                      # noqa: BLE001
                    log.warning("еженедельный трек-рекорд: %s", e)
            # недельный ЛИЧНЫЙ дайджест подписчикам (ретеншн) — отдельный маркер от карточки,
            # чтобы card/CSV и рассылка не блокировали друг друга. Маркер ставим ДО рассылки:
            # краш посреди рассылки не должен переслать дайджест всем заново.
            if should_post_weekly(runtime.weekly_digest_last_week(), week_key):
                await asyncio.to_thread(runtime.set_weekly_digest_last_week, week_key)
                try:
                    d_start, d_end = weekly_report_period(now_dt)
                    d_wk = await asyncio.to_thread(
                        journal.stats_for_period, d_start.isoformat(), d_end.isoformat())
                    d_st = await asyncio.to_thread(journal.stats)
                    d_label = (f"{d_start.strftime('%d.%m')}"
                               f"–{(d_end - timedelta(days=1)).strftime('%d.%m')}")
                    d_text = templates.render_weekly_digest(d_label, d_wk, d_st)
                    sent = 0
                    for uid in await asyncio.to_thread(payments.active_subscriber_ids):
                        try:
                            await bot.send_message(uid, d_text, parse_mode="HTML")
                            sent += 1
                        except Exception as e:              # noqa: BLE001 — заблокировал бота
                            log.warning("дайджест -> %s: %s", uid, e)
                    log.info("недельный дайджест разослан: %d подписчикам", sent)
                except Exception as e:                      # noqa: BLE001
                    log.warning("недельный дайджест: %s", e)

    async def _on_close(closed):
        # мягкое уведомление АДМИНУ (не в канал): серия стопов подряд на ВКЛЮЧЁННОМ сетапе —
        # только сигнал, без авто-отключения (решение за оператором). Не зависит от того,
        # включён ли канал — это отдельный контур от постинга результатов ниже.
        enabled = set(runtime.enabled_setups())
        fresh_setups = {c["setup"] for c in closed
                        if not c.get("shadow") and c.get("setup")
                        and c["status"] in ("tp1", "tp2", "tp3", "stopped")}
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

        # публикуем в канал жизненный цикл сделок: вход исполнен / отменён / результат
        channel = runtime.signals_channel()
        if not channel or not runtime.channel_enabled():
            return
        now_dt = datetime.now(timezone.utc)
        resolved_ids = {c["id"] for c in closed if c["status"] != "filled"}
        show_price = runtime.channel_mode() == "full"   # в teaser-режиме цену входа не раскрываем
        for c in closed:
            if c.get("shadow"):
                continue
            try:
                if c["status"] == "filled":
                    # только свежие филлы (не «исторические» после деплоя/рестарта) и только
                    # если сделка не закрылась этим же тиком — результат сам скажет, что вход был
                    if c["id"] in resolved_ids or not fill_is_fresh(c, now_dt):
                        continue
                    msg = await bot.send_message(channel, templates.render_fill(c, show_price),
                                                 parse_mode="HTML")
                    log.info("сигнал #%s: пост filled OK (msg_id=%s)", c["id"], msg.message_id)
                elif c["status"] == "expired":
                    msg = await bot.send_message(channel, templates.render_expired(c),
                                                 parse_mode="HTML")
                    log.info("сигнал #%s: пост expired OK (msg_id=%s)", c["id"], msg.message_id)
                elif c["status"] in ("tp1", "tp2", "tp3", "stopped"):
                    msg = await bot.send_message(
                        channel, templates.render_result(
                            c, runtime.display_leverage(), streak=journal.recent_streak(10)),
                        parse_mode="HTML")
                    log.info("сигнал #%s: пост result(%s) OK (msg_id=%s)",
                             c["id"], c["status"], msg.message_id)
            except Exception as e:                     # noqa: BLE001
                log.warning("пост цикла сделки в канал #%s: %s", c["id"], e)

        # «вехи как контент» (мега-план MAX): рубеж закрытых сделок -> органический пост
        if any(not c.get("shadow") and c["status"] in ("tp1", "tp2", "tp3", "stopped")
               for c in closed):
            st = await asyncio.to_thread(journal.stats)
            m = milestone_reached(runtime.last_milestone(), st.get("trades", 0))
            if m:
                try:
                    msg = await bot.send_message(
                        channel, templates.render_milestone(m, st), parse_mode="HTML")
                    log.info("веха %d сделок: пост OK (msg_id=%s)", m, msg.message_id)
                    # метка — только после успешного поста (иначе повтор на след. закрытии)
                    await asyncio.to_thread(runtime.set_value, "last_milestone", str(m))
                except Exception as e:                 # noqa: BLE001
                    log.warning("пост вехи %d: %s", m, e)

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

    async def _on_intrabar(symbol_ws: str, high: float, low: float):
        # реалтайм-филл: касание entry внутри бара -> мгновенный «вход исполнен».
        # Гонку с bar-скорингом решает атомарный try_mark_filled; официальный исход
        # сделки по-прежнему считает ТОЛЬКО bar-скоринг (контракт трек-рекорда).
        from app.data.stream import entry_touched, ws_symbol
        for symbol in runtime.symbols():
            if ws_symbol(symbol) != symbol_ws:
                continue
            rows = await asyncio.to_thread(journal.unfilled_open_signals, symbol)
            for row in rows:
                if not entry_touched(row["entry"], high, low):
                    continue
                now_iso = clock.utcnow_iso()
                if not await asyncio.to_thread(journal.try_mark_filled, row["id"], now_iso):
                    continue                            # бар-скоринг успел первым
                channel = runtime.signals_channel()
                if not channel or not runtime.channel_enabled():
                    continue
                c = {"id": row["id"], "symbol": row["symbol"], "side": row["side"],
                     "timeframe": row["timeframe"], "status": "filled",
                     "entry": row["entry"], "filled_at": now_iso}
                try:
                    msg = await bot.send_message(
                        channel,
                        templates.render_fill(c, runtime.channel_mode() == "full"),
                        parse_mode="HTML")
                    log.info("сигнал #%s: пост filled OK (msg_id=%s, realtime)",
                             row["id"], msg.message_id)
                except Exception as e:                 # noqa: BLE001
                    log.warning("realtime fill #%s: %s", row["id"], e)
            break

    async def _stream():
        from app.data.market_data import _TF_SECONDS
        from app.data.stream import run_stream

        def _youngest_tf() -> str:
            tfs = [tf for tf in runtime.timeframes() if tf in _TF_SECONDS] or ["1h"]
            return min(tfs, key=lambda t: _TF_SECONDS[t])

        await run_stream(runtime.symbols, _youngest_tf, _on_intrabar,
                         runtime.stream_enabled, log)

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
            await register_bot_profile(bot)
        except Exception as e:                         # noqa: BLE001 — меню не критично для работы
            log.warning("register_bot_commands: %s", e)
        poller = asyncio.create_task(_poller())
        poller.add_done_callback(_poller_done)
        # реалтайм-стрим (за флагом stream_enabled; при выключенном флаге тихо спит)
        stream_task = asyncio.create_task(_stream())
        stream_task.add_done_callback(_poller_done)     # смерть стрима тоже не молчит
        try:
            await dp.start_polling(bot)
        finally:
            poller.cancel()
            stream_task.cancel()

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
