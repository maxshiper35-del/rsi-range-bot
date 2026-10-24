"""
bot.py — Telegram-бот (aiogram): /start /stats /calc /subscribe, рассылка по тирам.
Точка входа приложения (make run).

SMC-интеграция реализована чистыми функциями (scan_symbol / format_*), которые
тестируются без aiogram; сам aiogram импортируется лениво в main().
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic as _monotonic

import pandas as pd

from app import clock
from app.brain import journal
from app.brain.risk import calculate_position
from app.brain.smc_setups import SMCEngine
from app.core.signal_engine import Signal
from app.delivery import graphics, templates
from app.power import battery_alert_text, should_alert_battery
from app.power import pmset_batt as _pmset_batt


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


# Все PNG-рендеры (Playwright поднимает целый Chromium ~300МБ на вызов) — через один
# семафор: без него параллельные рендеры занимают дефолтный ThreadPoolExecutor целиком
# (на 2 vCPU это 6 воркеров) и ВСЕ to_thread-вызовы бота — БД, доставка, оплаты —
# встают в очередь за браузерами (ревью 14.08, starvation воспроизведён). 2 слота:
# юзерский рендер не блокирует канальный пост и наоборот, а пула хватает на остальное.
_RENDER_SEM = asyncio.Semaphore(2)


class _ErrorBuffer(logging.Handler):
    """ERROR+ из логов -> DM админам (батч в maintenance-цикле). aiogram-логгеры
    исключены: сетевые обрывы Telegram самолечатся ретраями и завалили бы DM шумом."""

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.buf: deque = deque(maxlen=20)
        self.last_sent: float = 0.0

    def emit(self, record) -> None:
        if record.name.startswith("aiogram"):
            return
        try:
            msg = f"{record.name}: {record.getMessage()}"[:300]
            if record.exc_info and record.exc_info[1] is not None:
                msg += f" [{type(record.exc_info[1]).__name__}]"
            self.buf.append(msg)
        except Exception:                               # noqa: BLE001 — логгер не падает
            pass


_ERRBUF = _ErrorBuffer()

# cwd бота = корень репо (systemd WorkingDirectory) — og.png живёт рядом с БД
DATAOG_DIR = Path("data")


async def _render_png(fn, *args):
    async with _RENDER_SEM:
        return await asyncio.to_thread(fn, *args)


def _side_capacity_ok(side_value: str, symbol: str) -> bool:
    """Портфельный лимит направления: 9 коррелированных монет × ТФ × сетапы без лимита
    набирали 15+ однонаправленных позиций = одна ставка с риском 15R (день −12.7R,
    14.07.2026). Считаем ВСЕ активные open (ждущие лимитки тоже несут риск). 0 = выкл."""
    from app import runtime
    from app.logging_conf import get_logger

    cap = runtime.max_open_per_side()
    if cap <= 0 or journal.open_side_counts().get(side_value, 0) < cap:
        return True
    get_logger("scan").info("лимит направления: %s %s пропущен (max_open_per_side=%d)",
                            side_value, symbol, cap)
    return False


def _side_cooldown_ok(side_value: str, symbol: str) -> bool:
    """Анти-кластерный тормоз: лимит/сторону режет одновременную экспозицию, но не
    последовательный перезаход — 17.07.2026 каждый стоп освобождал слот, и движок тут же
    выдавал новый LONG в то же сползание BTC (8 стопов за 6ч, −8R). Серия стопов стороны =
    рынок гонит тренд против ретрейсмент-входов; пауза стороны, пока окно не очистится."""
    from app import runtime
    from app.logging_conf import get_logger

    n = runtime.side_cooldown_stops()
    if n <= 0:
        return True
    hours = runtime.side_cooldown_hours()
    stops = journal.recent_side_stops(side_value, hours)
    if stops < n:
        return True
    get_logger("scan").info("тормоз стороны: %s %s пропущен (%d стопов за %.0fч, порог %d)",
                            side_value, symbol, stops, hours, n)
    return False


def _active_cooldowns() -> dict:
    """Стороны, стоящие сейчас на анти-кластерном тормозе -> {side: стопов_в_окне}.
    Для отображения в /admin и /status (sync, дёргать через to_thread)."""
    from app import runtime

    n = runtime.side_cooldown_stops()
    if n <= 0:
        return {}
    hours = runtime.side_cooldown_hours()
    out = {}
    for side in ("LONG", "SHORT"):
        stops = journal.recent_side_stops(side, hours)
        if stops >= n:
            out[side] = stops
    return out


def scan_auto(symbol: str, frames: dict[str, pd.DataFrame],
              engine: SMCEngine) -> list[tuple]:
    """Авто-режим: сетапы на КАЖДОМ входном ТФ -> сразу АКТИВНЫЕ сигналы (status=open).

    Возвращает список (Signal, signal_id) — по одному на входной ТФ (15m/1h/4h), где
    есть сетап. Пустой список — сетапов нет, все дубли, или сторона упёрлась в
    портфельный лимит (max_open_per_side)."""
    out = []
    now = datetime.now(timezone.utc)
    for sig in engine.analyze_multi(frames, symbol):
        if not decision_is_fresh(sig, now):
            continue                                    # решающий бар давно закрыт
        if journal.find_duplicate_setup(sig) is not None:
            continue
        if not _side_capacity_ok(sig.side.value, symbol):
            continue
        if not _side_cooldown_ok(sig.side.value, symbol):
            continue
        if is_shadow_setup(sig):
            # теневой режим: прошёл ВСЕ гейты как живой (честный контрфакт), но не
            # публикуется/не исполняется — исход судится в тень
            journal.record_shadow(sig)
            continue
        sid = journal.record_signal(sig)
        out.append((sig, sid))
    _record_late_shadows(engine, now)
    return out


def scan_semiauto(symbol: str, frames: dict[str, pd.DataFrame],
                  engine: SMCEngine) -> list[tuple]:
    """Полуавтомат: сетапы на КАЖДОМ входном ТФ -> КАНДИДАТЫ (не рассылать).

    Возвращает список (Signal, signal_id) — оператор подтверждает/отклоняет контекст.
    Пустой список — сетапов нет ЛИБО все уже записаны (дедуп: не спамим оператора тем
    же импульсом на каждом баре), либо лимит направления.
    """
    out = []
    now = datetime.now(timezone.utc)
    for sig in engine.analyze_multi(frames, symbol):
        if not decision_is_fresh(sig, now):
            continue                                    # решающий бар давно закрыт
        if journal.find_duplicate_setup(sig) is not None:
            continue
        if not _side_capacity_ok(sig.side.value, symbol):
            continue
        if not _side_cooldown_ok(sig.side.value, symbol):
            continue
        if is_shadow_setup(sig):
            journal.record_shadow(sig)
            continue
        sid = journal.record_candidate(sig)
        out.append((sig, sid))
    _record_late_shadows(engine, now)
    return out


def _record_late_shadows(engine, now: datetime) -> None:
    """Победители бара, отсечённые гейтом «вход уже пройден» (SMCEngine.late_shadow), —
    в тень через те же гейты, что прошёл бы живой сигнал: честный контрфакт для проверки
    вперёд (docs/measurements/2026-10-06-late-entry-gate.md). Не публикуются."""
    for sig in list(getattr(engine, "late_shadow", None) or []):
        if not decision_is_fresh(sig, now):
            continue
        if journal.find_duplicate_setup(sig) is not None:
            continue
        if not _side_capacity_ok(sig.side.value, sig.symbol):
            continue
        if not _side_cooldown_ok(sig.side.value, sig.symbol):
            continue
        journal.record_shadow(sig, reason="late_entry")


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
def format_stats(st: dict, epoch_st: dict | None = None,
                 epoch_label: str | None = None) -> str:
    """Текст /stats из journal.stats(). epoch_st/epoch_label — блок «новый код»
    (journal.stats_since_created от runtime.strategy_epoch): легаси-бэклог не
    смешивается с оценкой обновлённой стратегии."""
    if st["trades"] == 0:
        return ("📊 Статистика пока пустая — сделки ещё не закрывались.\n"
                f"Открытых сигналов: {st['open']}.\n"
                f"{NO_MANUAL_EDITS_LINE}")
    # чистое движение цены (без плеча): R-результат в процентах рынка
    move_block = ""
    if st.get("avg_tp_move_pct") or st.get("avg_stop_move_pct"):
        move_block = (
            f"Ход цены (без плеча): тейк в среднем <b>{st['avg_tp_move_pct']:+.2f}%</b>, "
            f"стоп <b>{st['avg_stop_move_pct']:+.2f}%</b>\n"
            f"Чистое движение суммарно: <b>{st.get('total_move_pct', 0):+.1f}%</b>\n")
    epoch_block = ""
    if epoch_st is not None and epoch_label:
        if epoch_st["trades"]:
            ep_move = (f" · хода <b>{epoch_st['total_move_pct']:+.1f}%</b>"
                       if epoch_st.get("total_move_pct") is not None else "")
            epoch_block = (
                f"🆕 Новый код (сигналы с {epoch_label}): {epoch_st['trades']} закрыто · "
                f"wr {epoch_st['winrate']:.0%} · Σ <b>{epoch_st['total_r']:+.2f}R</b>{ep_move} · "
                f"открыто {epoch_st['open']}\n")
        else:
            epoch_block = (f"🆕 Новый код (сигналы с {epoch_label}): закрытых пока нет, "
                           f"открыто {epoch_st['open']}\n")
    return (
        "📊 <b>Публичная статистика</b> (по закрытым сделкам)\n"
        "────────────────────\n"
        f"Сделок: <b>{st['trades']}</b>  |  открыто: {st['open']}\n"
        f"Winrate: <b>{st['winrate']:.0%}</b>  ({st['wins']}/{st['trades']})\n"
        f"Матожидание: <b>{st['expectancy_r']:+.2f}R</b> на сделку\n"
        f"Суммарно: <b>{st['total_r']:+.2f}R</b>\n"
        f"{move_block}"
        f"{epoch_block}"
        "────────────────────\n"
        f"{_small_sample_warning(st['trades'])}"
        f"{NO_MANUAL_EDITS_LINE}\n"
        "⚠️ Не финансовый совет. Прошлые результаты не определяют будущие."
    )


def format_kelly_line() -> str:
    """Справка Келли по трек-рекорду ТЕКУЩЕЙ стратегии — приложение к /calc.
    Пустая строка, если выборка мала или эджа нет (journal.kelly_stats -> None):
    Келли на шуме опаснее его отсутствия. Наружу — только ПОЛУ-Келли."""
    from app import runtime
    ks = journal.kelly_stats(runtime.strategy_epoch())
    if not ks:
        return ""
    return ("\n📐 Справка по трек-рекорду стратегии: полу-Келли ≈ "
            f"{ks['half']:g}% риска на сделку (WR {ks['wr']}%, "
            f"плюс/минус ×{ks['b']:g}, n={ks['n']}).\n"
            "Полный Келли агрессивен и растёт на шуме — больше половины не берут.")


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


def format_by_setup(by_setup: dict, title: str = "📐 Статистика по сетапам") -> str:
    """Текст /bysetup — разбивка по тегу сетапа (journal.stats_by_setup()) с лестницей
    порогов значимости, не одним числом: n<20 — рано даже для направления, n≥20 — есть
    направление (без выводов), n≥30 — предварительно (НЕ уровень для публичного канала),
    n≥100 — предупреждение снимается (±~10пп по винрейту). LTF-BB/LTF-OB НЕ схлопнуты —
    именно LTF-BB уже показал проблему в бэктесте и живой практике, LTF-OB отдельно не
    проверялся."""
    if not by_setup:
        return f"{title}\n────────────────────\nЗакрытых сделок пока нет."
    lines = [title, "────────────────────"]
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
    "min_rr_open_model": ("Мин. reward/risk для сетапа open-SMC", "float"),
    "max_open_per_side": ("Макс. активных сигналов в одну сторону, 0 = без лимита", "int"),
    "min_stop_pct": ("Мин. дистанция стопа, % от входа (костовый гейт, 0 = выкл)", "float"),
    "side_cooldown_stops": ("Тормоз стороны: стопов за окно для паузы, 0 = выкл", "int"),
    "side_cooldown_hours": ("Тормоз стороны: окно в часах", "float"),
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
        f"{_affiliate_block()}"
    )


def _affiliate_block() -> str:
    """Реф-блок для /about и /start ('' если ссылка не задана). Честная пометка
    «реферальная» обязательна — тот же стандарт прозрачности, что и везде."""
    from app import runtime
    url = runtime.affiliate_url()
    if not url.startswith("https://"):
        return ""
    return (f"\n\n🤝 Торгуешь сам? Регистрация на бирже по нашей ссылке "
            f"поддерживает проект (реферальная): {url}")


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
        "Вывод: устойчивого механического края НЕ обнаружено. Все кандидаты после "
        "издержек около нуля или в минусе; лучший из них фальсифицирован на генуинной "
        "out-of-sample проверке.\n\n"
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
        "сетапы статистически чаще отрабатывают.\n\n"
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


def is_shadow_setup(sig) -> bool:
    """Сетап сигнала в теневом режиме (runtime.shadow_setups: {(тег, ТФ)})."""
    from app import runtime
    snap = sig.indicators_snapshot if isinstance(sig.indicators_snapshot, dict) else {}
    return (str(snap.get("setup", "")), str(sig.timeframe)) in runtime.shadow_setups()


DECISION_MAX_LAG_MIN = 45.0


def decision_is_fresh(sig, now: datetime, max_lag_min: float = DECISION_MAX_LAG_MIN) -> bool:
    """Записывать сигнал только на первом такте после закрытия его решающего бара.

    Поллер каждый час пересканирует и последний закрытый 4h-бар: сигнал, заблокированный
    на 04:00 капом/тормозом/дедупом, проходил гейты в 05:00–07:00 со СТАРЫМ created_at и
    судился с решающего бара — в публичный журнал попадали ходы ДО публикации (#360 HYPE:
    опубликован 07:00, вход засчитан на баре 05:00, +3.91R; 83 из 472 сигналов записаны
    позже 30 мин — аудит 05.10). Симулятор действует только на своём баре — паритет.
    45 мин: обычный такт идёт через секунды после закрытия, а после сна Mac — с опозданием,
    но часовой бар ещё свеж."""
    try:
        created = datetime.fromisoformat(str(sig.created_at))
    except (TypeError, ValueError):
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    decision = created + timedelta(minutes=SMCEngine.TF_MINUTES.get(sig.timeframe, 60))
    return (now - decision).total_seconds() <= max_lag_min * 60


def precheckout_ok(payload: str | None, total: int, currency: str,
                   tiers: list[tuple[float, int]]) -> bool:
    """Пропустить ли оплату: 'sub:<дни>' должен совпасть с ВКЛЮЧЁННЫМ тарифом по дням и
    ТЕКУЩЕЙ цене в XTR. 'unlock:' (paid media) — платформенный поток, пропускаем как раньше."""
    p = payload or ""
    if p.startswith("unlock:"):
        return True
    if currency != "XTR" or not p.startswith("sub:"):
        return False
    try:
        days = float(p.split(":", 1)[1])
    except ValueError:
        return False
    return any(price > 0 and abs(d - days) < 1e-6 and int(total) == int(price)
               for d, price in tiers)


def result_is_fresh(c: dict, now: datetime, max_age_hours: float = 12.0) -> bool:
    """Свежий ли исход (результат/expired) для поста в канал: бар выхода не старше
    max_age_hours. Запоздалые закрытия (простой бота, пауза, рестарт) в канал не уходят —
    пачка трёхнедельных «результатов» с графиками только запутает подписчиков; журнал и
    дашборд свой учёт получают в любом случае. Нет closed_at -> считаем свежим (поведение
    как раньше)."""
    raw = c.get("closed_at")
    if not raw:
        return True
    try:
        closed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return True
    if closed.tzinfo is None:
        closed = closed.replace(tzinfo=timezone.utc)
    return (now - closed).total_seconds() <= max_age_hours * 3600


def should_remind_pause(paused_since: str | None, last_reminder_at: str | None,
                        now_iso: str, min_age_hours: float = 12.0,
                        rethrottle_hours: float = 24.0) -> bool:
    """Пора ли напомнить админу, что сигналы на паузе: пауза висит >= min_age_hours И
    (напоминаний ещё не было ИЛИ прошлое >= rethrottle_hours назад). Пауза 13.09–04.10.2026
    простояла три недели незамеченной — молчащий бот выглядит как сломанный."""
    if not paused_since:
        return False
    now = datetime.fromisoformat(now_iso)
    since = datetime.fromisoformat(paused_since)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if (now - since).total_seconds() < min_age_hours * 3600:
        return False
    if not last_reminder_at:
        return True
    last = datetime.fromisoformat(last_reminder_at)
    return (now - last).total_seconds() >= rethrottle_hours * 3600


def pause_age_text(since_iso: str, now: datetime | None = None) -> str:
    """Возраст паузы для напоминания: часы до 2 суток, дальше дни (.days давал «0 дн.»)."""
    now = now or datetime.now(timezone.utc)
    since = datetime.fromisoformat(since_iso)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    hours = (now - since).total_seconds() / 3600
    return f"{hours:.0f} ч" if hours < 48 else f"{hours / 24:.0f} дн."


def caption_fits(html_text: str, limit: int = 1024) -> bool:
    """Лимит Telegram на подпись к фото — 1024 символа ВИДИМОГО текста (теги не считаются)."""
    return len(re.sub(r"<[^>]+>", "", html_text)) <= limit


_UNLOCK_DENIALS = {
    "not_found": "Сигнал не найден.",
    "closed": "Сигнал уже закрыт — результат опубликован в канале.",
    "disabled": "Разовая покупка сейчас недоступна.",
}


MILESTONES = (10, 25, 50, 100, 250, 500, 1000)


def parse_alert_args(text: str) -> tuple | None:
    """'/alert BTC 65000' -> ('BTC', 65000.0); '/alert' -> ('list',); None — формат неверный.
    Резолв тикера в unified-символ — в хэндлере (нужен живой вотчлист из runtime)."""
    import math
    parts = (text or "").split()
    if len(parts) == 1:
        return ("list",)
    if len(parts) != 3:
        return None
    try:
        price = float(parts[2].replace(",", "."))
    except ValueError:
        return None
    # isfinite: '/alert BTC inf|nan' проходил float() и вешал мёртвый алерт в слоте лимита
    if not math.isfinite(price) or not (0 < price <= 1e9):
        return None
    return (parts[1], price)


def parse_depo_args(text: str) -> tuple | None:
    """'/depo 500 1.5' -> (500.0, 1.5); '/depo 500' -> (500.0, 1.0); '/depo off' -> ('off',);
    '/depo' -> ('show',); None — формат/диапазон неверный. Риск капится 5%: сайзинг от
    риска теряет смысл, когда «риск на сделку» превращается в «депозит за три стопа»."""
    parts = (text or "").split()
    if len(parts) == 1:
        return ("show",)
    if len(parts) == 2 and parts[1].lower() in ("off", "выкл", "-"):
        return ("off",)
    import math
    try:
        depo = float(parts[1].replace(",", "."))
        risk = float(parts[2].replace(",", ".")) if len(parts) > 2 else 1.0
    except (ValueError, IndexError):
        return None
    # isfinite: '/depo inf' давал «объём inf ≈ $inf, плечо ×nan» без предупреждения
    # (nan > max_leverage == False — гейт плеча молча пропускал)
    if not (math.isfinite(depo) and math.isfinite(risk)):
        return None
    if not (0 < depo <= 1e9 and 0 < risk <= 5):
        return None
    return (depo, risk)


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


def dashboard_data(st: dict, equity: list, by_symbol: dict,
                   updated_label: str, period_label: str = "всё время") -> dict:
    """Сборка входа для graphics.build_dashboard_html из журнала. Чистая — тестируется
    без БД. Сетапов нет — граница публикации (ревью 14.08: /dashboard отдавал
    разбивку по сетапам любому фри-юзеру, поверхность пропущена зачисткой 13.08)."""
    return {
        "period_label": period_label, "updated_label": updated_label,
        "stats": st, "equity": equity, "by_symbol": by_symbol,
        "milestone": {"current": st["trades"], "next": next_milestone(st["trades"])},
    }


def milestone_reached(prev: int, trades: int, levels: tuple = MILESTONES) -> int | None:
    """Наибольшая ТОЛЬКО ЧТО достигнутая веха закрытых сделок (prev < m <= trades).

    «Вехи как контент» (мега-план): факт накопления проверяемой истории — сам по себе
    органический пост, нулевая стоимость. Возвращает одну (старшую) веху за раз."""
    hit = [m for m in levels if prev < m <= trades]
    return max(hit) if hit else None


def watchlist_gate_decision(expectancy_r: float, trades: int,
                            min_trades: int = 20, min_expectancy: float = 0.10) -> bool:
    """Допуск нового символа в авто-watchlist по бэктесту (мега-план MAX, ставка 3).

    Порог ужесточён 2026-07-11 решением владельца (было −0.05R «не хуже нейтрали»):
    первая живая ротация (10 символов) дала заметный разрыв в качестве — 3 монеты
    с явным преимуществом (ARB +0.53R, XRP +0.51R, LAB +0.15R) и 4 около нуля
    (XAU +0.09R, DOGE +0.08R, HYPE +0.02R, SKL +0.00R, статистически неотличимо от
    шума на n=15-28). +0.10R — граница РОВНО в этом разрыве, не подгонка задним
    числом под желаемый список: LAB и XAU разделяет наибольший скачок во всей
    выборке. Цена решения: рост watchlist (и темп набора статистики) медленнее,
    чем при мягком пороге — сознательный выбор качества над скоростью. Мало
    сделок (< min_trades) = сетапы почти не встречаются = смысла добавлять нет.

    min_trades поднят 5 -> 20 (25.07.2026): порог в 5 сделок пропускал символ по чистому
    шуму — при разбросе исходов ~1.4R стандартная ошибка на n=5 составляет 0.63R, то есть
    в шесть раз больше самого порога +0.10R. Живая цена этой дыры: экзотические символы,
    попавшие в вотчлист через неё, дали 13 сделок с матожиданием −0.385R (половину
    убытка эпохи на 20% сделок). На n=20 ошибка 0.31R — всё ещё много, но это минимум,
    ниже которого решение не отличимо от подбрасывания монеты."""
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


def lifecycle_chart_wanted(chart_enabled: bool, channel_mode: str) -> bool:
    """Рисовать ли карту на посте цикла сделки (филл/результат): флаг ВКЛ И канал в full —
    в teaser карта раскрыла бы скрытые уровни входа/стопа/тейков. Чистая функция для тестов."""
    return chart_enabled and channel_mode == "full"


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


def should_alert_poller_stale(last_tick_at: str | None, last_alert_at: str | None,
                              now_iso: str, stale_seconds: float, uptime_seconds: float,
                              rethrottle_hours: int = 6) -> bool:
    """Пора ли алертить админу о зависшем такте поллера. Отдельный контур от краша процесса:
    launchd KeepAlive перезапускает УПАВШИЙ процесс, а этот watchdog ловит случай, когда процесс
    ЖИВ (обслуживание тикает), но таск поллера встал — зависший fetch, тихая смерть корутины.
    Гейты: (1) аптайм < stale_seconds — процесс только поднялся, дать поллеру шанс тикнуть (иначе
    персистентный last_tick с прошлого запуска дал бы ложную тревогу на старте); (2) тик устарел
    >= stale_seconds ИЛИ тика не было вовсе после грейса; (3) троттлинг — не спамить каждый такт."""
    from datetime import datetime

    now = datetime.fromisoformat(now_iso)
    if uptime_seconds < stale_seconds:
        return False
    if last_tick_at is None:
        stale = True
    else:
        stale = (now - datetime.fromisoformat(last_tick_at)).total_seconds() >= stale_seconds
    if not stale:
        return False
    if last_alert_at is None:
        return True
    return (now - datetime.fromisoformat(last_alert_at)).total_seconds() >= rethrottle_hours * 3600


_healthcheck_ok_logged = False


def healthcheck_ping(url: str | None, poller_fresh_seconds: float = 2 * 3600) -> bool:
    """Dead-man's switch: пульс на healthchecks.io из maintenance-цикла (синхронная, звать
    через to_thread). Дополняет should_alert_poller_stale с ДРУГОЙ стороны: watchdog жив
    только пока жив процесс, а внешний сервис ловит именно смерть/зависание процесса —
    урок простоя 30.07-09.08 («лежал 9 суток молча», все алерты жили внутри трупа).

    Пульс НЕ шлётся, если последний тик поллера старше poller_fresh_seconds — тогда
    молчание доносит до оператора и «процесс жив, но сигналов нет» (совместно с грейсом
    healthchecks это единственный канал, работающий при сломанном Telegram-контуре).
    Тика не было вовсе (свежая БД) — пульс идёт: поллер ещё не успел, это не поломка.
    Ошибки сети глотаются: сбой доставки пульса не должен трогать обслуживание, а
    «нет пульса» — это и есть сигнал, только на стороне сервиса. -> факт отправки."""
    global _healthcheck_ok_logged
    from app import runtime
    from app.logging_conf import get_logger

    log = get_logger("bot")
    if not url:
        return False
    last = runtime.last_poller_tick_at()
    if last is not None:
        age = (clock.utcnow() - datetime.fromisoformat(last)).total_seconds()
        if age >= poller_fresh_seconds:
            return False                     # поллер завис — пусть молчание алертит
    try:
        import urllib.request
        urllib.request.urlopen(url, timeout=10)
    except Exception as e:                   # noqa: BLE001 — см. docstring
        log.debug("healthcheck ping: %s", e)
        return False
    if not _healthcheck_ok_logged:
        _healthcheck_ok_logged = True
        log.info("dead-man's switch: пульс активен (%s)", url.split("/")[-1][:8] + "…")
    return True


def channel_cta_markup(bot_username: str | None, src: str):
    """Инлайн-CTA под постом КАНАЛА: единственная кнопка-ссылка в бота с deep-link
    атрибуцией (/start src_<src> -> payments.set_acquisition_source, first-touch).

    Зачем (аудит воронки 12.08.2026): в full-режиме посты канала не имели ни одной
    кнопки — зритель упирался в тупик, и 17 из 18 юзеров пришли с «неизвестного»
    источника. Кнопка = и путь в бота, и измеримость канала как источника.
    src на каждую поверхность свой: chsig (сигнал), chres (результат/филл), chcard
    (карточка недели) — конверсию поверхностей можно сравнивать по /sources.
    -> InlineKeyboardMarkup | None (нет username — кнопки нет, пост не блокируем)."""
    if not bot_username:
        return None
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup  # ленивый импорт

    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="⚡ Получать сигналы в личку",
        url=f"https://t.me/{bot_username}?start=src_{src}")]])


def xray_channel_markup(bot_username: str | None, sid: int):
    """Кнопки под постом РЕЗУЛЬТАТА в канале: «Рентген сделки» (deep-link xray_<sid> ->
    анатомия в личке: суб-скоры, путь трейлинга) + стандартный CTA. Рентген — двойной
    агент: уникальный трест-артефакт И вход в воронку (юзер попадает в бота)."""
    if not bot_username:
        return None
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup  # ленивый импорт

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔬 Рентген сделки — почему вход и как вели",
                              url=f"https://t.me/{bot_username}?start=xray_{sid}")],
        [InlineKeyboardButton(text="⚡ Получать сигналы в личку",
                              url=f"https://t.me/{bot_username}?start=src_chres")],
    ])


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
                              open_rows: list | None = None,
                              now_iso: str | None = None) -> str:
    """Живое табло для закреплённого поста канала: не winrate (самая манипулируемая
    метрика ниши), а матожидание в R, размер выборки, топ инструментов, живой список
    открытых. Сетапов здесь НЕТ (граница публикации стратегии — фидбэк владельца
    13.08; блок «По сетапам» удалён 14.08). Открытые — символ/сторона/ТФ/возраст,
    без уровней и unrealized R: по временнОму ряду uR восстанавливаются вход и стоп."""
    lines = [f"📌 <b>RSI RANGE — живое табло</b>\n{_LINE}"]
    if st["trades"] == 0:
        lines.append("Сделки ещё копятся, первые результаты скоро появятся здесь.")
    else:
        lines.append(f"Всего закрыто: <b>{st['trades']}</b>")
        lines.append(f"Матожидание: <b>{st['expectancy_r']:+.2f}R</b> на сделку  |  "
                     f"Итог: <b>{st['total_r']:+.2f}R</b>  |  Winrate: {st['winrate']:.0%}")
        warn = _small_sample_warning(st["trades"]).rstrip()
        if warn:
            lines.append(warn)
        if by_symbol:
            top = sorted(by_symbol.items(),
                         key=lambda kv: kv[1]["trades"], reverse=True)[:8]
            lines.append("\n<b>Топ инструментов:</b>")
            for symbol, s in top:
                lines.append(f"  • {templates.esc(symbol)}: {s['trades']} сд, "
                             f"exp {s['expectancy_r']:+.2f}R, итог {s['total_r']:+.2f}R")
            if len(by_symbol) > 8:
                lines.append(f"  …и ещё {len(by_symbol) - 8} в /trackrecord")
    if open_rows:
        from datetime import datetime
        lines.append(f"\n<b>Открыты сейчас ({len(open_rows)}):</b>")
        for o in open_rows[:10]:
            arrow = "▲" if o["side"] == "LONG" else "▼"
            in_pos = bool(o.get("filled_at"))
            anchor = o.get("filled_at") if in_pos else o.get("created_at")
            age = ""
            if anchor and now_iso:
                h = (datetime.fromisoformat(now_iso)
                     - datetime.fromisoformat(anchor)).total_seconds() / 3600
                age = f" · {h:.0f}ч"
            state = "в позиции" if in_pos else "ждёт входа"
            sym = templates.esc(o["symbol"].split("/")[0])
            lines.append(f"  {arrow} {sym} {templates.esc(o['timeframe'])} — {state}{age}")
        if len(open_rows) > 10:
            lines.append(f"  …и ещё {len(open_rows) - 10}")
        lines.append("  Уровни и живой uR — подписчикам: @rsirange_bot")
    from app import runtime as _rt
    if _rt.affiliate_url().startswith("https://"):
        lines.append(f"🤝 Биржа проекта (реф): {_rt.affiliate_url()}")
    if cohort is not None:
        c, r = cohort["confirmed"], cohort["rejected"]
        lines.append("\n🧪 Подтверждённые оператором vs отклонённые (форвард-эксперимент):")
        lines.append(f"  ✅ Подтверждено: {c['trades']} сд, exp {c['expectancy_r']:+.2f}R")
        lines.append(f"  🚫 Отклонено: {r['trades']} сд, exp {r['expectancy_r']:+.2f}R")
        lines.append(f"  Edge оператора: {cohort['edge_r']:+.2f}R")
    lines.append(f"\n{NO_MANUAL_EDITS_LINE}")
    lines.append("Публикуем 100% исходов — включая убыточные.")
    if now_iso:
        lines.append(f"⏱ обновлено {now_iso[11:16]} UTC")
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
    ("referral", "пригласить друга и получить дни подписки"),
    ("depo", "твой объём в каждом сигнале: /depo <депозит$> [риск%]"),
    ("mycard", "карточка твоего итога по отмеченным сделкам"),
    ("alert", "бесплатный прайс-алерт: /alert BTC 65000"),
    ("notify", "уведомления: мьют и тихие часы"),
    ("cancel", "отменить текущий пошаговый ввод"),
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
    ("executor", "демо-исполнение на BingX (VST): status | on | off"),
    ("bysetup", "статистика по тегу сетапа (6226/1:1/open-SMC/LTF-BB/LTF-OB)"),
    ("shadow", "теневые сетапы: итог проверки вперёд (не публикуются)"),
    ("growth", "атрибуция: источник -> приглашено -> оплатили -> %"),
    ("minconf", "порог уверенности сетапа (0..1)"),
    ("minimpulse", "мин. размер импульса в ATR"),
    ("channel", "on|off|<id> — канал публикации"),
    ("channelmode", "full|teaser — режим постов канала"),
    ("cornix", "on|off — Cornix-совместимый блок в постах"),
    ("post", "разовая сводка статистики в канал"),
    ("price", "stars|usdt|days <N> — цена подписки"),
    ("freeweek", "лимит бесплатных сигналов/нед"),
    ("affiliate", "реф-ссылка биржи в /about и закреп: /affiliate <url>|off"),
    ("addpromo", "создать промокод: /addpromo <код> <дней> [лимит]"),
    ("usdtlink", "ссылка на ручной USDT-счёт"),
    ("cryptobottoken", "API-токен Crypto Pay — вкл. авто-USDT"),
    ("usdtbonus", "бонус-дней за оплату в USDT"),
    ("grant", "выдать подписку: /grant <tg_id> [дней]"),
    ("refund", "вернуть последний Stars-платёж (money-back): /refund <tg_id>"),
    ("vipchannel", "VIP-канал на нативной Stars-подписке: /vipchannel <@канал> <цена> | status"),
    ("revoke", "снять подписку: /revoke <tg_id>"),
    ("users", "счётчики юзеров"),
    ("review", "разбор недели в канал сейчас (не ждать понедельника)"),
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
        ["💳 Подписка", "🤝 Пригласить"],
        ["ℹ️ О боте", "🧪 Бэктест"],
        ["📖 Глоссарий", "🎟 Промокод"],
        ["❓ Помощь"],
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


def format_admin_status(eff: dict, users: dict, exposure: dict | None = None,
                        cooldowns: dict | None = None) -> str:
    """Статус управления для /admin и /status. exposure — journal.open_side_counts();
    cooldowns — {side: стопов_в_окне} по сторонам, стоящим на анти-кластерном тормозе."""
    exposure = exposure or {}
    cd_line = ""
    if cooldowns:
        parts = ", ".join(f"{s} ({n} стопов)" for s, n in sorted(cooldowns.items()))
        cd_line = (f"⛔ Тормоз стороны: {parts} — пауза "
                   f"{eff.get('side_cooldown_hours', 0):.0f}ч-окна\n")
    return (
        "🛠 Управление RSI Range\n"
        f"{'⏸ НА ПАУЗЕ' if eff['paused'] else '▶️ РАБОТАЕТ'}\n"
        "────────────────────\n"
        f"Символы: {', '.join(eff['symbols']) or '—'}\n"
        f"ТФ: {', '.join(eff['timeframes'])}\n"
        f"Режим сигналов: {eff['signal_mode']}\n"
        f"Сетапы: {', '.join(eff['enabled_setups']) or '—'}\n"
        f"Мин. уверенность: {eff['min_confidence']:.2f}  ·  "
        f"мин. импульс: {eff['min_impulse_atr']:.1f}×ATR  ·  "
        f"мин. RR open-SMC: {eff['min_rr_open_model']:.2f}\n"
        f"Экспозиция: LONG {exposure.get('LONG', 0)} / SHORT {exposure.get('SHORT', 0)} "
        f"(лимит {eff['max_open_per_side']}/сторону)\n"
        f"{cd_line}"
        f"Канал: {eff['channel']} "
        f"({'вкл' if eff['channel_enabled'] else 'выкл'} · {eff['channel_mode']})\n"
        f"Цена: {eff['price_stars']}⭐ / {eff['price_usdt']:g} USDT / {eff['sub_days']} дн\n"
        f"Free-лимит: {eff['free_per_week']}/нед\n"
        f"Юзеров: {users['total']} (платящих {users.get('paying', 0)}, "
        f"триал {users.get('trial', 0)}, грантов {users.get('granted', 0)})"
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
    from aiogram.exceptions import TelegramBadRequest
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
    logging.getLogger().addHandler(_ERRBUF)   # ERROR из любых наших логгеров -> DM админам
    bot = Bot(token=cfg.tg_bot_token)
    dp = Dispatcher()
    # username бота для deep-link CTA под постами канала; заполняется в _main() после
    # get_me() — до этого кнопки просто нет (channel_cta_markup(None) -> None)
    bot_info: dict = {"username": None}

    def _channel_kb(src: str):
        """CTA-кнопка для поста канала либо None (выключено/username ещё неизвестен)."""
        if not runtime.channel_cta_enabled():
            return None
        return channel_cta_markup(bot_info["username"], src)

    async def _reward_referrer(tg_id: int, paid_days: float) -> None:
        """Реф-награда за РЕАЛЬНЫЙ платёж приглашённого + уведомление реферера. Best-effort:
        идемпотентна (одна награда на приглашённого), вызывается из всех путей платежа.
        paid_days — длительность оплаченного периода: короткие (intro) не квалифицируют."""
        try:
            reward = await asyncio.to_thread(
                payments.reward_referrer_on_payment, tg_id,
                runtime.referral_reward_days(), paid_days)
        except Exception as e:                             # noqa: BLE001
            log.warning("referral reward %s: %s", tg_id, e)
            return
        if not reward:
            return
        ref_id, rdays = reward
        try:
            await bot.send_message(ref_id, templates.render_referral_reward(rdays),
                                   parse_mode="HTML")
        except Exception as e:                             # noqa: BLE001 — реферер заблокировал бота
            log.warning("referral notify %s: %s", ref_id, e)

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
        # «📈 Дашборд» — ВСЕГДА обычная кнопка. web_app в reply-клавиатуре хранится
        # на клиенте вечно и протухает при смене URL (мёртвый сервер 30.08, ротация
        # quick-туннеля). Живой дашборд уходит СВЕЖЕЙ инлайн-кнопкой из хэндлера.
        return ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True, keyboard=[
            [KeyboardButton(text=t) for t in row] for row in main_menu_layout(is_admin)])

    def _usdt_tier_price(stars: int) -> float:
        """Цена тира в USDT: пропорция его Stars-цены к базовому тарифу × базовая
        USDT-цена — длинные тиры в USDT без отдельных настроек (запрос владельца 02.09)."""
        base = runtime.price_stars()
        if base <= 0 or stars <= 0 or runtime.price_usdt() <= 0:
            return 0.0
        return round(runtime.price_usdt() * stars / base, 2)

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
            # длинные тиры в USDT: цена — пропорцией от Stars-цены тира к базовому
            # (скидки переносятся автоматически, отдельных настроек не нужно)
            tier_row = []
            if q > 0 and _usdt_tier_price(q) > 0:
                tier_row.append(InlineKeyboardButton(
                    text=f"💵 90д · {_usdt_tier_price(q):g}$",
                    callback_data="pay_usdt_quarter"))
            if y > 0 and _usdt_tier_price(y) > 0:
                tier_row.append(InlineKeyboardButton(
                    text=f"💵 год · {_usdt_tier_price(y):g}$",
                    callback_data="pay_usdt_year"))
            if lf > 0 and _usdt_tier_price(lf) > 0:
                tier_row.append(InlineKeyboardButton(
                    text=f"💵 ∞ · {_usdt_tier_price(lf):g}$",
                    callback_data="pay_usdt_life"))
            if tier_row:
                rows.append(tier_row)
        elif runtime.usdt_invoice_url():                # полу-ручной: ссылка на счёт + «Я оплатил»
            rows.append([InlineKeyboardButton(
                text=f"💵 Оплатить USDT (от {runtime.price_usdt():g}$)",
                url=runtime.usdt_invoice_url())])
            rows.append([InlineKeyboardButton(
                text="✅ Я оплатил USDT", callback_data="usdt_claim")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    _mycard_last: dict[int, float] = {}

    async def _send_mycard(m):
        """Личная карточка итога по отметкам (шеринг). Платникам/админам.

        Кулдаун 60с на юзера + общий семафор рендеров (_RENDER_SEM): Chromium
        поднимается на каждый рендер, и без троттлинга один спамящий /mycard
        платник занимал бы весь thread-pool бота (ревью 14.08)."""
        uid = m.from_user.id
        if not (await asyncio.to_thread(payments.is_paid, uid) or uid in cfg.admin_ids):
            await m.answer("Карточка итога — для подписчиков: /subscribe")
            return
        import time as _t
        if _t.monotonic() - _mycard_last.get(uid, -1e9) < 60:
            await m.answer("Карточка только что рисовалась — подожди минуту 🙃")
            return
        ms = await asyncio.to_thread(payments.taken_stats, uid)
        if not ms["trades"]:
            await m.answer("Пока нечего рисовать: открой 📈 Дашборд → «Мой итог» и "
                           "отметь ▢ сделки, в которые заходил — карточка соберётся "
                           "по ним.")
            return
        _mycard_last[uid] = _t.monotonic()      # столбим ДО рендера: спам в очередь не встаёт
        ms["bot"] = bot_info["username"] or "rsirange_bot"
        from aiogram.types import BufferedInputFile as _BIF
        png = await _render_png(graphics.render_my_card, ms)
        await m.answer_photo(
            _BIF(png, filename="my_result.png"),
            caption="Твой итог по отмеченным сделкам. Перешли, куда захочешь 📤")

    @dp.message(Command("mycard"))
    async def _mycard_cmd(m):
        await _send_mycard(m)

    @dp.message(Command("start"))
    async def _start(m):
        await asyncio.to_thread(payments.register_user, m.from_user.id, m.from_user.username)
        # deep-link атрибуция: /start src_<label> — источник; /start ref_<tg_id> — реферал
        # (first-touch; ref также помечает источник 'referral' для /growth)
        parts = (m.text or "").split()
        arg = parts[1] if len(parts) > 1 else ""
        if arg.startswith("src_") and len(arg) > 4:
            await asyncio.to_thread(payments.set_acquisition_source, m.from_user.id, arg[4:])
        elif arg.startswith("ref_") and arg[4:].isdecimal():  # isdecimal: '²³' роняют int()
            await asyncio.to_thread(payments.set_referrer, m.from_user.id, int(arg[4:]))
            await asyncio.to_thread(payments.set_acquisition_source, m.from_user.id, "referral")
        elif arg == "mycard":
            # кнопка «поделиться карточкой» из Mini App: дашборд не умеет слать
            # файлы — deep-link приводит в DM, где бот рисует и отдаёт PNG
            await _send_mycard(m)
            return
        elif arg == "sub":
            # кнопка «Продлить» из Mini App (24.10): сразу экран подписки с тарифами
            await _subscribe(m)
            return
        elif arg.startswith("xray_") and arg[5:].isdecimal():
            # «Рентген сделки» из канала: показать анатомию ЗАКРЫТОГО сигнала и НЕ
            # вываливать следом весь онбординг — юзер пришёл за конкретной вещью.
            # Атрибуция first-touch: рентген = самостоятельная поверхность воронки.
            await asyncio.to_thread(payments.set_acquisition_source, m.from_user.id, "xray")
            x = await asyncio.to_thread(journal.signal_xray, int(arg[5:]))
            if x is not None:
                await m.answer(templates.render_xray(x), parse_mode="HTML",
                               reply_markup=_main_menu_kb(m.from_user.id in cfg.admin_ids))
            else:
                await m.answer("Сделка ещё открыта или не найдена — рентген доступен "
                               "после закрытия. Пока: /stats")
            # главная точка входа из канала: НОВЫЙ юзер не должен уйти без триала
            # (аудит 02.09) — grant_trial идемпотентен, бывалых не трогает
            xr_days = runtime.trial_days()
            if await asyncio.to_thread(payments.grant_trial, m.from_user.id, xr_days):
                await m.answer(f"🎁 Открыт полный доступ на {xr_days:g} дн. — все "
                               "сигналы без ограничений. Меню внизу; /help — команды.")
            return
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

    @dp.message(Command("referral"))
    @dp.message(F.text == "🤝 Пригласить")
    async def _referral(m):
        await asyncio.to_thread(payments.register_user, m.from_user.id, m.from_user.username)
        link = f"https://t.me/rsirange_bot?start=ref_{m.from_user.id}"
        ref = await asyncio.to_thread(payments.referral_stats, m.from_user.id)
        await m.answer(
            templates.render_referral_invite(link, ref, runtime.referral_reward_days()),
            parse_mode="HTML")

    _QUIET_PRESET = (22, 6)                                 # тихие часы по умолчанию, UTC

    def _notify_kb(prefs: dict) -> InlineKeyboardMarkup:
        b = InlineKeyboardButton
        muted = prefs.get("muted")
        qs, qe = prefs.get("quiet_start"), prefs.get("quiet_end")
        quiet_on = qs is not None and qe is not None and qs != qe
        rows = [[b(text="🔔 Включить сигналы" if muted else "🔕 Выключить все сигналы",
                   callback_data="notif_mute")]]
        if not muted:
            rows.append([b(
                text="☀️ Убрать тихие часы" if quiet_on
                else f"🌙 Тихие часы {_QUIET_PRESET[0]:02d}–{_QUIET_PRESET[1]:02d} UTC",
                callback_data="notif_quiet")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @dp.message(Command("alert"))
    async def _alert(m):
        from app.delivery import alerts
        await asyncio.to_thread(payments.register_user, m.from_user.id, m.from_user.username)
        parsed = parse_alert_args(m.text)
        watchlist = runtime.symbols()
        names = ", ".join(sorted(alerts.base_of(s) for s in watchlist))
        if parsed is None:
            await m.answer(f"Формат: <code>/alert BTC 65000</code> — пинг, когда цена "
                           f"коснётся уровня.\nДоступны сейчас: {templates.esc(names)}",
                           parse_mode="HTML")
            return
        if parsed[0] == "list":
            act = await asyncio.to_thread(alerts.active_alerts, m.from_user.id)
            if not act:
                await m.answer(f"Алертов нет. <code>/alert BTC 65000</code> — поставить.\n"
                               f"Доступны сейчас: {templates.esc(names)}", parse_mode="HTML")
                return
            rows_txt = "\n".join(
                f" • #{a['id']} {alerts.base_of(a['symbol'])} @ {templates._price(a['price'])}"
                f"  (снять: /delalert_{a['id']})" for a in act)
            await m.answer(f"⏰ Твои алерты:\n{rows_txt}\n\nОдин алерт срабатывает один раз.",
                           parse_mode="HTML")
            return
        token, price = parsed
        symbol = alerts.resolve_symbol(token, watchlist)
        if symbol is None:
            await m.answer(f"Не слежу за «{templates.esc(token)}». Доступны сейчас: "
                           f"{templates.esc(names)}\n(список меняется с ротацией вотчлиста)",
                           parse_mode="HTML")
            return
        res = await asyncio.to_thread(alerts.create_alert, m.from_user.id, symbol, price)
        if not res["ok"]:
            await m.answer(f"Лимит {alerts.MAX_ACTIVE_PER_USER} активных алертов — "
                           "сними лишние: /alert")
            return
        await m.answer(f"⏰ Готово: пингую, когда {alerts.base_of(symbol)} коснётся "
                       f"<code>{templates._price(price)}</code>. Сработает один раз.\n"
                       "Слежу, пока монета в нашем вотчлисте (он ротируется — актуальный "
                       "список: /alert).", parse_mode="HTML")

    @dp.message(F.text.regexp(r"^/delalert[_ ](\d+)$"))
    async def _delalert(m):
        from app.delivery import alerts
        aid = int(re.search(r"(\d+)$", m.text).group(1))
        ok = await asyncio.to_thread(alerts.delete_alert, m.from_user.id, aid)
        await m.answer("Снят." if ok else "Не нашёл такой алерт (или он уже сработал).")

    @dp.message(Command("depo"))
    async def _depo(m):
        parsed = parse_depo_args(m.text)
        if parsed is None:
            await m.answer("Формат: <code>/depo 500 1</code> — депозит $500, риск 1% "
                           "на сделку (риск не выше 5%). Выключить: <code>/depo off</code>",
                           parse_mode="HTML")
            return
        if parsed[0] == "off":
            await asyncio.to_thread(payments.set_depo, m.from_user.id, None, None)
            await m.answer("💼 Персональный объём выключен — сигналы снова без расчёта.")
            return
        if parsed[0] == "show":
            cur = await asyncio.to_thread(payments.depo_prefs, m.from_user.id)
            if cur is None:
                await m.answer("Персональный объём не настроен.\n"
                               "<code>/depo 500 1</code> — и каждый полный сигнал будет "
                               "приходить с твоим размером позиции (риск 1% от $500).",
                               parse_mode="HTML")
            else:
                await m.answer(f"💼 Настроено: депозит ${cur[0]:g}, риск {cur[1]:g}% "
                               f"на сделку.\nИзменить: <code>/depo 800 1.5</code>, "
                               f"выключить: <code>/depo off</code>", parse_mode="HTML")
            return
        depo, risk = parsed
        await asyncio.to_thread(payments.set_depo, m.from_user.id, depo, risk)
        await m.answer(f"💼 Готово: депозит ${depo:g}, риск {risk:g}% (${depo * risk / 100:.2f} "
                       f"на сделку).\nТеперь каждый полный сигнал придёт с твоим объёмом "
                       f"позиции. Выключить: /depo off")

    @dp.message(Command("notify"))
    async def _notify(m):
        prefs = await asyncio.to_thread(payments.notify_prefs, m.from_user.id)
        await m.answer(templates.render_notify(prefs), parse_mode="HTML",
                       reply_markup=_notify_kb(prefs))

    @dp.callback_query(F.data == "notif_mute")
    async def _notif_mute(cq):
        prefs = await asyncio.to_thread(payments.notify_prefs, cq.from_user.id)
        await asyncio.to_thread(payments.set_muted, cq.from_user.id, not prefs["muted"])
        prefs = await asyncio.to_thread(payments.notify_prefs, cq.from_user.id)
        await cq.message.edit_text(templates.render_notify(prefs), parse_mode="HTML",
                                   reply_markup=_notify_kb(prefs))
        await cq.answer("Готово.")

    @dp.callback_query(F.data == "notif_quiet")
    async def _notif_quiet(cq):
        prefs = await asyncio.to_thread(payments.notify_prefs, cq.from_user.id)
        quiet_on = prefs["quiet_start"] is not None and prefs["quiet_end"] is not None \
            and prefs["quiet_start"] != prefs["quiet_end"]
        if quiet_on:
            await asyncio.to_thread(payments.set_quiet_hours, cq.from_user.id, None, None)
        else:
            await asyncio.to_thread(payments.set_quiet_hours, cq.from_user.id, *_QUIET_PRESET)
        prefs = await asyncio.to_thread(payments.notify_prefs, cq.from_user.id)
        await cq.message.edit_text(templates.render_notify(prefs), parse_mode="HTML",
                                   reply_markup=_notify_kb(prefs))
        await cq.answer("Готово.")

    @dp.message(F.text == "🎟 Промокод")
    async def _promo_btn_start(m, state: FSMContext):
        await state.set_state(PromoState.code)
        await m.answer("Введите промокод:")

    @dp.message(PromoState.code, ~F.text.startswith("/"))
    async def _promo_step_code(m, state: FSMContext):
        if await _fsm_menu_escape(m, state):
            return
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

    async def _pay_tier(cq, days: float, stars: int, label: str | None = None):
        """Общий путь кнопок оплаты. Гейт stars<=0: старые /subscribe-сообщения в истории
        чата хранят кнопки выключенных тарифов — без гейта клик слал бы invoice с amount=0
        (Bad Request) и вечный спиннер юзеру (аудит-2 [12])."""
        if stars <= 0:
            await cq.answer("Этот тариф сейчас недоступен — см. актуальные в /subscribe.",
                            show_alert=True)
            return
        await _send_stars_invoice(cq.from_user.id, days, stars, label)
        await cq.answer()

    @dp.callback_query(F.data == "pay_stars")
    async def _pay_stars(cq):
        await _pay_tier(cq, runtime.sub_days(), runtime.price_stars())

    @dp.callback_query(F.data == "pay_stars_intro")
    async def _pay_stars_intro(cq):
        await _pay_tier(cq, round(runtime.intro_hours() / 24, 6), runtime.intro_price_stars())

    @dp.callback_query(F.data == "pay_stars_quarter")
    async def _pay_stars_quarter(cq):
        await _pay_tier(cq, 90, runtime.quarter_price_stars())

    @dp.callback_query(F.data == "pay_stars_year")
    async def _pay_stars_year(cq):
        await _pay_tier(cq, 365, runtime.year_price_stars(), label="1 год")

    @dp.callback_query(F.data == "pay_stars_life")
    async def _pay_stars_life(cq):
        # lifetime = 36500 дней (100 лет): без спец-статуса в БД — просто далёкий paid_until
        await _pay_tier(cq, 36500, runtime.lifetime_price_stars(), label="навсегда (lifetime)")

    _unlock_last: dict[int, float] = {}

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
        # троттлинг: рендер идёт ДО оплаты — без кулдауна это бесплатная DoS-поверхность
        if _monotonic() - _unlock_last.get(uid, -1e9) < 30:
            await cq.answer("Сигнал уже готовится — подождите полминуты.")
            return
        _unlock_last[uid] = _monotonic()
        try:
            png = await _render_png(_signal_chart_png, sig, sid)
        except Exception as e:                          # noqa: BLE001 — биржа/рендер
            log.warning("unlock #%s: график не собрался: %s", sid, e)
            await cq.answer("Не удалось подготовить сигнал — попробуйте чуть позже.",
                            show_alert=True)
            return
        # caption у paid media виден ДО оплаты (пейволлится только сама медиа!) —
        # уровни в него класть нельзя (аудит 02.09): тизер до покупки, полный текст
        # досылает _paid_media_bought после факта оплаты
        caption = templates.render_teaser(sig, sid)
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
        # учёт: Stars зачисляются на баланс бота платформой; здесь — след в логе +
        # перепометка доставки: купленный сигнал не должен попадать в «что ты упустил»
        log.info("unlock куплен: юзер %s, payload=%s",
                 p.from_user.id, p.paid_media_payload)
        parts = (p.paid_media_payload or "").split(":")
        if not (len(parts) >= 2 and parts[0] == "unlock" and parts[1].isdigit()):
            return
        uid, sid = p.from_user.id, int(parts[1])
        # 1) ДОСТАВКА — первой и отдельно: купивший обязан получить уровни. Раньше учёт шёл
        # первым в общем try, падал на CHECK (kind='unlock') и текст не уходил (аудит 05.10).
        # Полный текст — ПОСЛЕ оплаты: в caption paid media его класть нельзя (виден до
        # покупки), а купивший должен получить уровни, даже если caption не влез в лимит
        try:
            row = await asyncio.to_thread(journal.signal_row, sid)
            if row is None:
                raise LookupError(f"сигнал #{sid} не найден")
            await bot.send_message(
                uid, templates.render_signal_dm(journal.row_to_signal(row), sid),
                parse_mode="HTML", reply_markup=_signal_actions_kb(sid))
        except Exception as e:                          # noqa: BLE001
            log.error("unlock #%s оплачен юзером %s, но текст не доставлен: %s", sid, uid, e)
            for admin_id in cfg.admin_ids:
                try:
                    await bot.send_message(
                        admin_id, f"⚠️ Юзер {uid} купил сигнал #{sid} (paid media), но полный "
                                  f"текст не доставлен: {str(e)[:120]}. Отправьте вручную "
                                  "или верните Stars.")
                except Exception:                      # noqa: BLE001
                    log.exception("unlock alert админу %s", admin_id)
        # 2) УЧЁТ: купленный сигнал не должен попадать в «что ты упустил», а кнопки
        # «График/Риск» должны открываться (delivered_full). Купивший из канала тизерной
        # записи не имеет — пишем 'unlock' сразу.
        try:
            if not await asyncio.to_thread(payments.mark_delivery_unlocked, uid, sid):
                await asyncio.to_thread(payments.record_delivery, uid, sid, "unlock")
        except Exception as e:                          # noqa: BLE001 — учёт не критичен
            log.warning("mark_delivery_unlocked #%s %s: %s", sid, uid, e)

    def _signal_actions_kb(sid: int) -> InlineKeyboardMarkup:
        """Инлайн-панель под полным DM-сигналом: быстрый график и расчёт риска без набора команд."""
        b = InlineKeyboardButton
        return InlineKeyboardMarkup(inline_keyboard=[[
            b(text="📈 График", callback_data=f"sigchart:{sid}"),
            b(text="🧮 Риск", callback_data=f"sigcalc:{sid}"),
        ]])

    async def _load_signal_for_action(cq):
        """Общая часть sigchart/sigcalc: распарсить sid, достать сигнал, ПРОВЕРИТЬ ПРАВО.

        Гейт (аудит 02.09): callback_data подделывается тривиально — уровни через
        «график/риск» отдаём только тем, кто имеет полный доступ или уже получил
        ЭТОТ сигнал полным (full/unlock), teaser не считается.
        -> (sid, sig), либо (None, None) если не найден/нет прав (ответ уже отправлен)."""
        sid_s = cq.data.split(":", 1)[1]
        if not sid_s.isdigit():
            await cq.answer("Сигнал не найден.", show_alert=True)
            return None, None
        sid = int(sid_s)
        uid = cq.from_user.id
        if uid not in cfg.admin_ids \
                and not await asyncio.to_thread(payments.is_paid, uid) \
                and not await asyncio.to_thread(payments.delivered_full, uid, sid):
            await cq.answer("Доступно получателям полного сигнала — /subscribe.",
                            show_alert=True)
            return None, None
        row = await asyncio.to_thread(journal.signal_row, sid)
        if row is None:
            await cq.answer("Сигнал не найден.", show_alert=True)
            return None, None
        return sid, journal.row_to_signal(row)

    _chart_last: dict[int, float] = {}

    @dp.callback_query(F.data.startswith("sigchart:"))
    async def _sig_chart(cq):
        sid, sig = await _load_signal_for_action(cq)
        if sig is None:
            return
        # троттлинг Chromium-рендера: 30с на юзера (аудит 02.09 — DoS-поверхность)
        now = _monotonic()
        if now - _chart_last.get(cq.from_user.id, 0.0) < 30:
            await cq.answer("График уже готовится — подождите полминуты.")
            return
        _chart_last[cq.from_user.id] = now
        # ack ДО рендера: биржа+Chromium занимают до десятков секунд, просроченный callback
        # давал вечный спиннер и алерт админу на каждый повторный тап (аудит-2 UX [7])
        await cq.answer("Готовлю график…")
        try:
            png = await _render_png(_signal_chart_png, sig, sid)
        except Exception as e:                              # noqa: BLE001 — биржа/рендер
            log.warning("sigchart #%s: %s", sid, e)
            await cq.message.answer("График сейчас недоступен — попробуйте позже.")
            return
        cap = f"📈 Сигнал #{sid}: {templates.esc(sig.symbol)} · {templates.esc(sig.timeframe)}"
        await cq.message.answer_photo(
            BufferedInputFile(png, filename=f"signal_{sid}.png"), caption=cap)

    @dp.callback_query(F.data.startswith("sigcalc:"))
    async def _sig_calc(cq):
        sid, sig = await _load_signal_for_action(cq)
        if sig is None:
            return
        entry, stop = templates._price(sig.entry), templates._price(sig.stop)
        await cq.message.answer(
            f"🧮 Расчёт позиции для сигнала #{sid}:\n"
            f"<code>/calc &lt;депозит&gt; &lt;риск%&gt; {entry} {stop}</code>\n"
            "Подставьте свой депозит и риск% (обычно 1–2) — бот вернёт объём и требуемое плечо.",
            parse_mode="HTML")
        await cq.answer()

    async def _usdt_invoice(cq, days: float, amount: float | None, label: str):
        """Общий путь USDT-инвойсов всех тиров (poll_cryptobot активирует по days
        из payload, бонус-дни добавляются там же — тиры едут без изменений ядра)."""
        try:
            res = await asyncio.to_thread(payments.create_cryptobot_invoice,
                                          cq.from_user.id, days, amount)
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
            f"Счёт на оплату в USDT создан ({label}). После оплаты подписка "
            f"активируется автоматически (проверка раз в ~минуту).{bonus_line}",
            reply_markup=kb)
        await cq.answer()

    @dp.callback_query(F.data == "pay_usdt")
    async def _pay_usdt(cq):
        await _usdt_invoice(cq, runtime.sub_days(), None,
                            f"{_fmt_days(runtime.sub_days())}")

    @dp.callback_query(F.data == "pay_usdt_quarter")
    async def _pay_usdt_quarter(cq):
        amount = _usdt_tier_price(runtime.quarter_price_stars())
        if amount <= 0:
            await cq.answer("Тариф выключен.", show_alert=True)
            return
        await _usdt_invoice(cq, 90, amount, "90 дней")

    @dp.callback_query(F.data == "pay_usdt_year")
    async def _pay_usdt_year(cq):
        amount = _usdt_tier_price(runtime.year_price_stars())
        if amount <= 0:
            await cq.answer("Тариф выключен.", show_alert=True)
            return
        await _usdt_invoice(cq, 365, amount, "год")

    @dp.callback_query(F.data == "pay_usdt_life")
    async def _pay_usdt_life(cq):
        amount = _usdt_tier_price(runtime.lifetime_price_stars())
        if amount <= 0:
            await cq.answer("Тариф выключен.", show_alert=True)
            return
        await _usdt_invoice(cq, 36500, amount, "навсегда (lifetime)")

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
            lambda: format_calc(dep, risk, entry, stop, cfg.max_leverage)
            + format_kelly_line()))

    # кнопка "Калькулятор" — пошаговый FSM-ввод вместо одной строки с 4 числами.
    # ~F.text.startswith("/") на каждом шаге: если юзер вместо ответа набрал команду
    # (передумал/нажал другую кнопку) — состояние НЕ перехватывает её молча, команда
    # проходит в свой обычный хендлер (aiogram пробует следующий подходящий хендлер)
    _MENU_TEXTS = frozenset(t for row in (main_menu_layout(False) + main_menu_layout(True))
                            for t in row)

    async def _fsm_menu_escape(m, state: FSMContext) -> bool:
        """Кнопка меню посреди пошагового ввода — выход из состояния, а не «Не понял число»
        (аудит-2 UX [6]: половина меню зарегистрирована ПОСЛЕ FSM-шагов и попадала в ловушку
        «Статистика -> Не понял число» без выхода). -> True, если перехвачено."""
        if (m.text or "") in _MENU_TEXTS:
            await state.clear()
            await m.answer("Ввод отменён — нажмите кнопку ещё раз.")
            return True
        return False

    @dp.message(Command("cancel"))
    async def _cancel(m, state: FSMContext):
        await state.clear()
        await m.answer("Ок, отменил.", reply_markup=_main_menu_kb(_is_admin(m)))

    @dp.message(F.text == "🧮 Калькулятор")
    async def _calc_btn_start(m, state: FSMContext):
        await state.set_state(CalcStates.deposit)
        await m.answer("🧮 Калькулятор позиции\nДепозит в USD? Например: 1000\n"
                       "(передумали — /cancel)")

    @dp.message(CalcStates.deposit, ~F.text.startswith("/"))
    async def _calc_step_deposit(m, state: FSMContext):
        if await _fsm_menu_escape(m, state):
            return
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
        if await _fsm_menu_escape(m, state):
            return
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
        if await _fsm_menu_escape(m, state):
            return
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
        if await _fsm_menu_escape(m, state):
            return
        try:
            stop = float((m.text or "").replace(",", "."))
            assert stop > 0
        except Exception:                                  # noqa: BLE001
            await m.answer("Не понял число. Цена стопа? Например: 62980")
            return
        data = await state.get_data()
        await state.clear()
        await m.answer(await asyncio.to_thread(
            lambda: format_calc(data["deposit"], data["risk"], data["entry"], stop,
                                cfg.max_leverage) + format_kelly_line()))

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
                        f"📢 Бот стал админом канала «{templates.esc(chat.title)}».\n"
                        f"ID канала: <code>{chat.id}</code>\n\n"
                        f"Чтобы постить сигналы сюда — пришли мне этот ID "
                        f"(или добавь в .env: SIGNALS_CHANNEL={chat.id}).",
                        parse_mode="HTML")
                except Exception as e:                 # noqa: BLE001
                    log.warning("notify admin channel-id %s: %s", admin_id, e)

    def _current_stars_tiers() -> list[tuple[float, int]]:
        """(дни, цена⭐) включённых сейчас тарифов — те же, что строит _subscribe_kb."""
        tiers = [(float(runtime.sub_days()), runtime.price_stars()),
                 (90.0, runtime.quarter_price_stars()), (365.0, runtime.year_price_stars()),
                 (36500.0, runtime.lifetime_price_stars())]
        if runtime.intro_hours() > 0:
            tiers.append((round(runtime.intro_hours() / 24, 6), runtime.intro_price_stars()))
        return tiers

    @dp.pre_checkout_query()
    async def _pre_checkout(q):
        # старые инвойсы (выключенный тариф, прежняя цена, пересланные копии) иначе
        # оплачивались бы вечно, а нечитаемый payload — сначала списание, потом фолбэк
        # на 30 дн (аудит 05.10). Отказ — до списания, Stars не уходят.
        ok = precheckout_ok(q.invoice_payload, q.total_amount, q.currency,
                            _current_stars_tiers())
        if ok:
            await q.answer(ok=True)
        else:
            log.warning("pre_checkout отклонён: payload=%r amount=%s %s от %s",
                        q.invoice_payload, q.total_amount, q.currency, q.from_user.id)
            await q.answer(ok=False, error_message="Тариф изменился — откройте /subscribe "
                                                   "и оплатите актуальный.")

    @dp.message(F.successful_payment)
    async def _paid(m):
        sp = m.successful_payment
        try:
            days = float(sp.invoice_payload.split(":")[1])
        except Exception:                               # noqa: BLE001
            # нечитаемый payload — фолбэк на АКТУАЛЬНЫЙ runtime.sub_days (не boot-time cfg)
            # с громким следом: молча выдать 30 дн за lifetime-чардж нельзя (аудит-2 [10])
            days = float(runtime.sub_days())
            log.warning("successful_payment: нечитаемый payload %r от %s (charge=%s) — "
                        "фолбэк %s дн", sp.invoice_payload, m.from_user.id,
                        sp.telegram_payment_charge_id, days)
        applied = await asyncio.to_thread(
            payments.grant_subscription, m.from_user.id, "stars",
            float(sp.total_amount), sp.currency, days, sp.telegram_payment_charge_id)
        if applied:
            await m.answer(f"✅ Подписка активна на {_fmt_days(days)}! Сигналы теперь приходят "
                           f"автоматически на закрытии свечей. Статус: /subscribe")
            await _reward_referrer(m.from_user.id, days)   # реф-награда за реальный платёж
        else:
            await m.answer("Этот платёж уже был учтён. Спасибо! Статус: /subscribe")

    @dp.message(Command("stats"))
    @dp.message(F.text == "📊 Статистика")
    async def _stats(m):
        def _collect():
            st = journal.stats()
            epoch = runtime.strategy_epoch()
            if not epoch:
                return format_stats(st)
            label = f"{epoch[8:10]}.{epoch[5:7]}"        # ISO -> ДД.ММ
            return format_stats(st, journal.stats_since_created(epoch), label)
        await m.answer(await asyncio.to_thread(_collect), parse_mode="HTML")

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

    async def _send_offline_dashboard(m):
        """Самодостаточный HTML-файл (офлайн-версия того же дашборда)."""
        def _build():
            updated = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
            data = dashboard_data(journal.stats(), journal.equity_series(),
                                  journal.stats_by_symbol(), updated)
            return graphics.build_dashboard_html(data)
        html = await asyncio.to_thread(_build)
        await m.answer_document(
            BufferedInputFile(html.encode("utf-8"), filename="dashboard.html"),
            caption="📈 Дашборд трек-рекорда — откройте в браузере: эквити-кривая, "
                    "разбивка по инструментам, прогресс до вехи. Работает офлайн.")

    @dp.message(Command("dashboard"))
    async def _dashboard_cmd(m):
        # команда — всегда офлайн-файл (сознательная фича: переслать/хранить)
        await _send_offline_dashboard(m)

    _dash_last: dict[int, float] = {}

    @dp.message(F.text == "📈 Дашборд")
    async def _dashboard_btn(m):
        # Сюда доходит только СТАРАЯ reply-клавиатура (выданная до Mini App): её
        # кнопка — плоский текст. Новая (web_app) открывает Mini App сама. Reply-
        # клавиатуры живут на клиенте вечно — сами не обновятся: даём inline-кнопку
        # Mini App прямо сейчас И перевыдаём клавиатуру (фидбэк владельца 14.08
        # «хочу чтоб дашборд в тг был»). Без webapp_url — прежний офлайн-файл.
        wurl = runtime.webapp_url()
        if not wurl.startswith("https://"):
            # Mini App выключен (локальный режим): шлём КАРТИНКУ терминала — на телефоне
            # HTML-файл нечитаем без скачивания (фидбэк владельца 31.08 «он мне отправляет
            # html»). Кулдаун и общий семафор — Chromium на каждый рендер (см. /mycard).
            uid = m.from_user.id
            if _monotonic() - _dash_last.get(uid, -1e9) < 60:
                await m.answer("Дашборд только что рисовался — подожди минуту 🙃")
                return
            _dash_last[uid] = _monotonic()
            try:
                def _build_html():
                    updated = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
                    return graphics.build_dashboard_html(dashboard_data(
                        journal.stats(), journal.equity_series(),
                        journal.stats_by_symbol(), updated))
                html = await asyncio.to_thread(_build_html)
                png = await _render_png(graphics.render_html_to_png, html, 900)
                await m.answer_photo(
                    BufferedInputFile(png, filename="dashboard.png"),
                    caption="📈 Терминал на текущий момент. "
                            "Интерактивная HTML-версия (офлайн): /dashboard")
            except Exception as e:                      # noqa: BLE001 — рендер не критичен
                log.warning("dashboard png: %s — отдаю файлом", e)
                await _send_offline_dashboard(m)
            return
        vurl = f"{wurl}{'&' if '?' in wurl else '?'}v={int(_monotonic())}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="📈 Открыть живой терминал", web_app=WebAppInfo(url=vurl))]])
        await m.answer("Живой терминал — кнопкой ниже. Офлайн-файл: /dashboard",
                       reply_markup=kb)

    def _is_admin(m) -> bool:
        return bool(m.from_user) and m.from_user.id in cfg.admin_ids

    @dp.message(Command("confirm"))
    async def _confirm(m):
        if not _is_admin(m):
            return
        sid = parse_id_arg(m.text)
        answer = await asyncio.to_thread(handle_confirm, sid)
        await m.answer(answer)
        # доставка подтверждённого: раньше кандидат становился open молча — ни канала,
        # ни подписчиков (аудит 02.09). Успех подтверждения различаем по тексту ответа
        if sid is not None and answer.startswith("✅"):
            row = await asyncio.to_thread(journal.signal_row, sid)
            if row is not None:
                try:
                    await _deliver_signal(journal.row_to_signal(row), sid)
                except Exception:                       # noqa: BLE001 — доставка не роняет хендлер
                    log.exception("доставка подтверждённого #%s", sid)
                    await m.answer(f"⚠️ #{sid}: подтверждён, но доставка упала — "
                                   "детали в журнале.")

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
            [b(text="📈 График филл/тейк: вкл" if eff["lifecycle_chart_enabled"]
               else "📈 График филл/тейк: выкл", callback_data="adm_lifechartoggle")],
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
            [b(text=f"🎚 Мин. RR open-SMC: {eff['min_rr_open_model']:.2f}",
               callback_data="adm_prompt_min_rr_open_model")],
            [b(text=(f"💸 Мин. стоп: {eff['min_stop_pct']:.2f}%"
                     if eff['min_stop_pct'] else "💸 Мин. стоп: выкл"),
               callback_data="adm_prompt_min_stop_pct"),
             b(text=("🕐 NY-окно: вкл" if eff['time_filter_enabled'] else "🕐 NY-окно: выкл"),
               callback_data="adm_timefiltertoggle")],
            [b(text=f"⚖️ Лимит/сторону: {eff['max_open_per_side']}",
               callback_data="adm_prompt_max_open_per_side")],
            [b(text=(f"⛔ Тормоз: {eff['side_cooldown_stops']} стопов"
                     f"/{eff['side_cooldown_hours']:g}ч"
                     if eff['side_cooldown_stops'] else "⛔ Тормоз стороны: выкл"),
               callback_data="adm_prompt_side_cooldown_stops"),
             b(text=f"🕐 Окно: {eff['side_cooldown_hours']:g}ч",
               callback_data="adm_prompt_side_cooldown_hours")],
            [b(text=("🧭 HTF-блок контртренда: вкл" if eff['htf_block_countertrend']
                     else "🧭 HTF-блок контртренда: выкл"),
               callback_data="adm_htfblocktoggle")],
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
        # символ — в callback по ЗНАЧЕНИЮ: позиция в списке меняется авто-ротацией, и
        # старая кнопка удаляла соседа (аудит 05.10); лимит callback_data 64 байта
        sym_rows = [[b(text=f"❌ {s}", callback_data=f"adm_delsym_{s}")]
                   for s in eff["symbols"] if len(f"adm_delsym_{s}".encode()) <= 64]
        return InlineKeyboardMarkup(inline_keyboard=[
            *sym_rows,
            [b(text="➕ Добавить символ", callback_data="adm_prompt_symbol")],
            [b(text="◀️ Назад", callback_data="adm_nav_main")],
        ])

    async def _panel(chat_id):
        eff = await asyncio.to_thread(runtime.effective)
        users = await asyncio.to_thread(payments.user_counts)
        exposure = await asyncio.to_thread(journal.open_side_counts)
        cooldowns = await asyncio.to_thread(_active_cooldowns)
        await bot.send_message(chat_id, format_admin_status(eff, users, exposure, cooldowns),
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
        exposure = await asyncio.to_thread(journal.open_side_counts)
        cooldowns = await asyncio.to_thread(_active_cooldowns)
        await m.answer(format_admin_status(eff, users, exposure, cooldowns))

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
            if await asyncio.to_thread(runtime.paused):
                # повторный /pause не сдвигает начало паузы (иначе обнулял отсчёт напоминания)
                await m.answer("⏸ Сигналы уже на паузе. Возобновить: /resume")
                return
            await _set(m, "paused", "1", "⏸ Сигналы на паузе. Открытые сделки ведутся "
                                         "до выхода. Возобновить: /resume")

    @dp.message(Command("resume"))
    async def _resume(m):
        if _is_admin(m):
            await asyncio.to_thread(runtime.set_value, "last_pause_reminder_at", "")
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

    @dp.message(Command("executor"))
    async def _executor_cmd(m):
        if m.from_user.id not in cfg.admin_ids:
            return
        from app.v2 import bingx_executor
        arg = ((m.text or "").split(maxsplit=1) + [""])[1].strip().lower()
        if arg == "on":
            # since = сейчас: исполняются только сигналы, созданные ПОСЛЕ включения
            await asyncio.to_thread(runtime.set_executor_since, clock.utcnow_iso())
            await asyncio.to_thread(runtime.set_value, "executor_enabled", "1")
            await m.answer("🤖 Executor ВКЛ (демо BingX, VST). Исполняются только новые "
                           "сигналы. Реальные деньги в v1 невозможны технически.")
            return
        if arg in ("orphans", "sweep"):
            # сироты — позиции биржи без учёта executor; sweep закрывает по рынку
            lines = await asyncio.to_thread(bingx_executor.sweep_orphans, arg == "sweep")
            await m.answer("🤖 Executor · сироты:\n" + "\n".join(lines))
            return
        if arg.startswith("limit"):
            try:
                v = float(arg.split()[1].replace(",", "."))
                assert 0.0 <= v <= 50.0
            except Exception:                          # noqa: BLE001
                await m.answer("Формат: /executor limit 5 (стоп-кран, % эквити за сутки; "
                               "0 = выкл)")
                return
            await asyncio.to_thread(runtime.set_value, "executor_daily_loss_pct", v)
            await m.answer(f"⛔ Стоп-кран executor: {v:g}% эквити за UTC-сутки"
                           + (" (выключен)" if v == 0 else ""))
            return
        if arg == "off":
            await asyncio.to_thread(runtime.set_value, "executor_enabled", "0")
            await m.answer("🤖 Executor ВЫКЛ. Открытые демо-позиции журналом донашиваться "
                           "НЕ будут: закрой их и висящие лимитки руками в BingX (Demo) "
                           "или включи executor обратно. Аварийные стопы −1R на бирже "
                           "остаются.")
            return
        st = await asyncio.to_thread(bingx_executor.status)
        orders = ", ".join(f"{k}: {v}" for k, v in st["orders"].items()) or "нет"
        await m.answer(
            f"🤖 <b>Executor (демо BingX)</b>\n"
            f"Состояние: {'ВКЛ' if st['enabled'] else 'выкл'}\n"
            f"VST-баланс: {st['vst'] if st['vst'] is not None else '—'}\n"
            f"Риск на сделку: {st['risk_pct']:g}%\n"
            f"Стоп-кран: {st['loss_limit_pct']:g}% эквити за сутки"
            f"{' — СРАБОТАЛ сегодня' if st['halted_today'] else ''}\n"
            f"Ордеров в учёте: {orders}\n"
            f"Включён с: {st['since'] or '—'}\n"
            "Команды: /executor on | off | orphans | sweep | limit N", parse_mode="HTML")

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

    @dp.message(Command("forward"))
    async def _forward_cmd(m):
        # прогресс по правилам проверки вперёд (docs/measurements/2026-10-24-forward-rules.md)
        if not _is_admin(m):
            return
        from app.brain import forward_check
        txt = await asyncio.to_thread(forward_check.report, datetime.now(timezone.utc))
        await m.answer(txt, parse_mode="HTML")

    @dp.message(Command("shadow"))
    async def _shadow_cmd(m):
        # теневой режим сетапов: итог контрфакта (не публикуется) — к решению по
        # docs/measurements/2026-10-05-shadow-ltfbb-4h.md
        if not _is_admin(m):
            return
        cur = await asyncio.to_thread(runtime.shadow_setups)
        st = await asyncio.to_thread(journal.shadow_stats)
        lines = ["🌑 <b>Теневые сетапы</b> (пишутся и судятся, не публикуются)",
                 "В тени сейчас: " + (", ".join(f"{t}@{tf}" for t, tf in sorted(cur)) or "—")]
        for (tag, tf), d in sorted(st.items()):
            lines.append(f"{templates.esc(tag)}@{templates.esc(tf)}: n={d['n']} · "
                         f"ΣR {d['sum']:+.2f} · "
                         f"exp {d['exp']:+.3f}R · побед {d['wins']}")
        if not st:
            lines.append("Закрытых теневых исходов пока нет.")
        lines.append("Правило решения: ≥60 закрытых и ≥3 мес — exp ≤ 0 → выключить насовсем; "
                     "exp ≥ +0.05R → вернуть в публикацию; между — продолжать.")
        gate = await asyncio.to_thread(runtime.late_entry_gate)
        late = await asyncio.to_thread(journal.shadow_stats, None, "late_entry")
        lines.append("")
        lines.append(f"⏱ <b>Гейт «вход уже пройден»</b>: {templates.esc(gate)} "
                     "(отсечённые — в тени)")
        for (tag, tf), d in sorted(late.items()):
            lines.append(f"{templates.esc(tag)}@{templates.esc(tf)}: n={d['n']} · "
                         f"ΣR {d['sum']:+.2f} · exp {d['exp']:+.3f}R · побед {d['wins']}")
        if not late:
            lines.append("Закрытых отсечённых исходов пока нет.")
        await m.answer("\n".join(lines), parse_mode="HTML")

    @dp.message(Command("bysetup"))
    @dp.message(F.text == "🎯 По сетапам")
    async def _bysetup(m):
        if not _is_admin(m):
            return
        by_setup = await asyncio.to_thread(journal.stats_by_setup)
        text = format_by_setup(by_setup)
        epoch = runtime.strategy_epoch()
        if epoch:
            fresh = await asyncio.to_thread(journal.stats_by_setup, epoch)
            label = f"{epoch[8:10]}.{epoch[5:7]}"
            text += "\n\n" + format_by_setup(
                fresh, title=f"🆕 По сетапам — новый код (сигналы с {label})")
        await m.answer(text)

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

    @dp.message(Command("affiliate"))
    async def _affiliate(m):
        if not _is_admin(m):
            return
        parts = (m.text or "").split(maxsplit=1)
        if len(parts) < 2:
            cur = runtime.affiliate_url() or "не задана"
            await m.answer(f"Реф-ссылка: {cur}\nФормат: /affiliate <https://…> | off")
            return
        arg = parts[1].strip()
        if arg.lower() == "off":
            await asyncio.to_thread(runtime.set_value, "affiliate_url", "")
            await m.answer("Реф-блоки выключены")
            return
        if not arg.startswith("https://"):
            await m.answer("Ссылка должна начинаться с https://")
            return
        await asyncio.to_thread(runtime.set_value, "affiliate_url", arg)
        await m.answer("✅ Реф-ссылка задана: покажется в /about и закрепе-табло "
                       "(обновится в течение часа)")

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
        await _reward_referrer(tg, days)                   # /grant = подтверждение USDT-оплаты

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
            if "CHARGE_ALREADY_REFUNDED" not in str(e):
                await m.answer(f"Telegram отказал в возврате: {e}")
                return
            # уже возвращён на стороне Telegram (краш между API и БД в прошлый раз) —
            # дочиниваем БД, иначе строка вечно paid и /refund зациклен (аудит-2 [9])
            log.warning("refund %s: уже возвращён у Telegram, дочиниваю запись #%s",
                        uid, pay["id"])
        applied = await asyncio.to_thread(payments.mark_refunded, pay["id"], uid,
                                          pay["days"], runtime.referral_reward_days())
        if not applied:                                 # параллельный /refund уже провёл
            await m.answer(f"Платёж #{pay['id']} уже помечен возвращённым — дни не "
                           "вычитаются повторно.")
            return
        await m.answer(f"↩️ Возвращено {pay['amount']:g}⭐ юзеру {uid}; "
                       f"подписка скорректирована (−{_fmt_days(pay['days'])}).")
        try:
            await bot.send_message(uid, "↩️ Ваш платёж возвращён — Stars вернутся на "
                                        "баланс Telegram. Спасибо, что попробовали нас.")
        except Exception as e:                          # noqa: BLE001
            log.warning("refund notify %s: %s", uid, e)

    @dp.message(Command("review"))
    async def _review_cmd(m):
        """Ручной «разбор недели»: прошлая полная неделя + текущая до сейчас —
        всегда есть контент, не надо ждать понедельничного такта."""
        if not _is_admin(m):
            return
        channel = runtime.signals_channel()
        if not channel or not runtime.channel_enabled():
            await m.answer("Канал не настроен или постинг выключен.")
            return
        now_dt = datetime.now(timezone.utc)
        w_start, _ = weekly_report_period(now_dt)
        await m.answer("Собираю разбор недели (карты рендерятся ~полминуты)…")
        try:
            await _post_weekly_review(channel, w_start.isoformat(), now_dt.isoformat())
            await m.answer("✅ Разбор недели запощен в канал.")
        except Exception as e:                          # noqa: BLE001
            log.exception("ручной разбор недели")
            await m.answer(f"⚠️ Не получилось: {str(e)[:150]}")

    @dp.message(Command("users"))
    @dp.message(F.text == "👥 Юзеры")
    async def _users(m):
        if not _is_admin(m):
            return
        u = await asyncio.to_thread(payments.user_counts)
        await m.answer(f"Юзеров: {u['total']} · платящих: {u['paying']} · "
                       f"грантов: {u['granted']} · на триале: {u['trial']}\n"
                       f"(получают сигналы как платные: {u['paid']})")

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
            if runtime.paused():
                await asyncio.to_thread(runtime.set_value, "last_pause_reminder_at", "")
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
        elif data == "adm_lifechartoggle":
            await asyncio.to_thread(runtime.set_value, "lifecycle_chart_enabled",
                                    "0" if runtime.lifecycle_chart_enabled() else "1")
        elif data == "adm_streamtoggle":
            await asyncio.to_thread(runtime.set_value, "stream_enabled",
                                    "0" if runtime.stream_enabled() else "1")
        elif data == "adm_timefiltertoggle":
            await asyncio.to_thread(runtime.set_value, "time_filter_enabled",
                                    "0" if runtime.time_filter_enabled() else "1")
            screen = "strategy"
        elif data == "adm_htfblocktoggle":
            await asyncio.to_thread(runtime.set_value, "htf_block_countertrend",
                                    "0" if runtime.htf_block_countertrend() else "1")
            screen = "strategy"
        elif data == "adm_poststats":
            ch = runtime.signals_channel()
            posted = False
            if ch:
                try:
                    st = await asyncio.to_thread(journal.stats)
                    await bot.send_message(ch, format_channel_stats(st))
                    posted = True
                except Exception as e:                 # noqa: BLE001
                    log.warning("poststats: %s", e)
            if not posted:                             # честный ответ вместо ложного «Готово»
                await cq.answer("⚠️ Не удалось отправить в канал.", show_alert=True)
                return
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
            target = data[len("adm_delsym_"):]
            syms = await asyncio.to_thread(runtime.symbols)
            # старые кнопки с индексом (до 05.10) игнорируются — экран обновится свежими
            if not target.isdecimal() and target in syms:
                syms.remove(target)
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
            exposure = await asyncio.to_thread(journal.open_side_counts)
            cooldowns = await asyncio.to_thread(_active_cooldowns)
            await cq.message.edit_text(format_admin_status(eff, users, exposure, cooldowns),
                                       reply_markup=kb)
        except Exception:                              # noqa: BLE001 — «message not modified» и т.п.
            pass
        await cq.answer("Готово")

    @dp.message(AdminInput.numeric, ~F.text.startswith("/"))
    async def _admin_numeric_receive(m, state: FSMContext):
        if not _is_admin(m):
            return
        if await _fsm_menu_escape(m, state):            # заброшенный промпт не съедает число
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

    def _review_chart_png(row):
        # синхронно (ccxt + Playwright) — только через _render_png. Окно баров тянется
        # до создания сделки: разбор недели должен показывать ЖИЗНЬ сделки целиком,
        # стандартные 57 баров у старых закрытий оставляли вход за левым краем
        from app.data.market_data import fetch_closed_ohlcv
        sig = journal.row_to_signal(row)
        tf_s = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}.get(sig.timeframe, 3600)
        age_s = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(row["created_at"])).total_seconds()
        need = min(400, max(57, int(age_s / tf_s) + 20))
        df = fetch_closed_ohlcv(sig.symbol, sig.timeframe, limit=need)
        data = build_signal_chart_data(
            sig, row["id"], df, datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC"))
        return graphics.render_signal_chart(data)

    async def _post_weekly_review(channel, start_iso, end_iso):
        """«Разбор недели» в канал: лучшая и худшая сделка с картой и deep-link
        «Рентген» (идея вкладки Lessons из CryptoCouncil поверх честного журнала).
        Публикуем И минус — статистика без минусов была бы рекламой."""
        best, worst = await asyncio.to_thread(journal.best_worst_closed,
                                              start_iso, end_iso)
        posts = [(best, "🏆 <b>Сделка недели</b>", "Полный разбор — в один тап ↓")]
        if worst is not None and worst["result_r"] < 0:
            posts.append((worst, "🧨 <b>Худшая сделка недели</b>",
                          "Публикуем и минусы: без них статистика — реклама. Разбор ↓"))
        for row, head, note in posts:
            if row is None:
                continue
            txt = (f"{head}: {templates.esc(row['symbol'].split('/')[0])} "
                   f"{row['side']} · {templates.esc(row['timeframe'])} — "
                   f"<b>{row['result_r']:+.2f}R</b>\n{note}")
            kb = xray_channel_markup(bot_info["username"], row["id"])
            try:
                png = await _render_png(_review_chart_png, row)
                await bot.send_photo(
                    channel, BufferedInputFile(png, filename=f"review_{row['id']}.png"),
                    caption=txt, parse_mode="HTML", reply_markup=kb)
            except Exception as e:                     # noqa: BLE001 — карта не критична
                log.warning("разбор недели #%s: карта не собралась: %s", row["id"], e)
                await bot.send_message(channel, txt, parse_mode="HTML", reply_markup=kb)

    def _chart_for_id(sid):
        # синхронно (DB + ccxt + Playwright) — только через asyncio.to_thread. Достаём сигнал
        # по id и рисуем ту же карту (свечи + зоны вход/стоп/тейки); свежие бары уже включают
        # развязку сделки. None -> сигнал не найден (у вызывающего откат на текст).
        row = journal.signal_row(sid)
        if row is None:
            return None
        return _signal_chart_png(journal.row_to_signal(row), sid)

    async def _send_lifecycle_post(channel, sid, text, want_chart, kb=None):
        """Пост цикла сделки (филл/результат) с картой, если want_chart. Любой сбой рендера
        /биржи -> откат на чистый текст (карта не критична, пост не теряется). -> Message.
        kb — переопределение клавиатуры (результат несёт «Рентген», филл — только CTA)."""
        if kb is None:
            kb = _channel_kb("chres")
        photo = None
        if want_chart:
            try:
                photo = await _render_png(_chart_for_id, sid)
            except Exception as e:                     # noqa: BLE001 — биржа/рендер
                log.warning("карта поста #%s: %s", sid, e)
        if photo is None:
            return await bot.send_message(channel, text, parse_mode="HTML", reply_markup=kb)
        file = BufferedInputFile(photo, filename=f"signal_{sid}.png")
        try:
            if caption_fits(text):
                return await bot.send_photo(channel, file, caption=text, parse_mode="HTML",
                                            reply_markup=kb)
            await bot.send_photo(channel, file)        # подпись не влезла — фото отдельно
        except Exception as e:                         # noqa: BLE001 — сбой ОТПРАВКИ фото
            # тоже откат на текст: раньше терялся весь пост (аудит-2 UX [10])
            log.warning("фото поста #%s: %s", sid, e)
        return await bot.send_message(channel, text, parse_mode="HTML", reply_markup=kb)

    def _watchlist_gate(symbol: str) -> tuple:
        # бэктест-гейт кандидата в авто-watchlist: 6 мес 1h на включённых сетапах
        # (порог — watchlist_gate_decision). Синхронно — только через asyncio.to_thread.
        from app.brain.smc_setups import SMCConfig
        from app.data.market_data import _TF_SECONDS, fetch_history
        from app.sim.backtest import run_backtest

        # 6 мес вместо 3 (25.07.2026): на 3 мес символ набирал 5-15 сделок — решение по
        # такой выборке неотличимо от шума. Свежий листинг столько истории не отдаст;
        # это и нужно — непроверяемый символ в бой не идёт (живой урок: экзотика в
        # вотчлисте дала −0.385R на сделку и никогда не проходила честной проверки).
        bars = int(6 * 30.44 * 86400 / _TF_SECONDS["1h"])
        df = fetch_history(symbol, "1h", target_bars=bars)
        if len(df) < bars * 0.9:                       # короткая история = новый листинг
            log.info("watchlist-гейт: %s отклонён — истории %d баров из %d",
                     symbol, len(df), bars)
            return (False, 0.0, 0)
        # ПАРИТЕТ с живым движком (аудит-2 математики [3]): гейт мерил другую стратегию —
        # тайм-фильтр выключен (в проде дефолт ВКЛ), рантайм-пороги не передавались,
        # окно 300 вместо живых 200 баров. Порог допуска должен мериться на той же
        # популяции сигналов, которой символ будет реально торговаться.
        rep = run_backtest(df, symbol, "1h", window=200,
                           cfg=SMCConfig(min_confidence=runtime.min_confidence(),
                                         min_impulse_atr=runtime.min_impulse_atr(),
                                         min_rr_open_model=runtime.min_rr_open_model(),
                                         enabled_setups=tuple(runtime.enabled_setups()),
                                         htf_block_countertrend=(
                                             runtime.htf_block_countertrend()),
                                         min_stop_pct=runtime.min_stop_pct(),
                                         late_entry_gate=runtime.late_entry_gate(),
                                         use_time_filter=runtime.time_filter_enabled()),
                           htf_tf="4h")
        return (watchlist_gate_decision(rep.expectancy_r, rep.trades),
                rep.expectancy_r, rep.trades)

    def _dead_recipient(e: Exception) -> bool:
        """Мёртвый получатель (заблокировал бота/удалил чат) — пометить, не ретраить."""
        low_ = str(e).lower()
        return "blocked" in low_ or "chat not found" in low_ or "deactivated" in low_

    async def _send_retry(uid: int, text: str, *, parse_mode: str | None = None,
                          reply_markup=None) -> None:
        """Один повтор после 429 (flood-wait): без него rate-limit Telegram молча
        съедал сообщение — для рассылки сигналов это потеря ядра продукта
        (аудит 02.09; паттерн из дайджеста, ревью 14.08)."""
        from aiogram.exceptions import TelegramRetryAfter
        try:
            await bot.send_message(uid, text, parse_mode=parse_mode,
                                   reply_markup=reply_markup)
        except TelegramRetryAfter as e:
            await asyncio.sleep(min(float(e.retry_after), 30.0) + 1.0)
            await bot.send_message(uid, text, parse_mode=parse_mode,
                                   reply_markup=reply_markup)

    async def _broadcast(sig, sid):
        # semiauto: КАНДИДАТ уходит ТОЛЬКО оператору — ни канала, ни подписчиков.
        # Раньше _broadcast был слеп к режиму дальше выбора текста: неподтверждённый сетап
        # публиковался в канал как сигнал, а платные получали операторский алерт с мёртвыми
        # для них /confirm (аудит-2 UX [2]). Рассылка подтверждённого — после /confirm.
        if runtime.signal_mode() != "auto":
            try:
                alert = templates.esc(format_candidate_alert(sig, sid))
            except Exception:                          # noqa: BLE001 — формат не роняет цикл
                log.exception("формат алерта кандидата #%s", sid)
                alert = f"🕵️ Кандидат #{sid} (ошибка форматирования — детали в журнале)"
            for admin_id in cfg.admin_ids:
                try:
                    await bot.send_message(admin_id, alert, parse_mode="HTML")
                except Exception as e:                 # noqa: BLE001
                    log.warning("candidate alert %s: %s", admin_id, e)
            return
        await _deliver_signal(sig, sid)

    async def _deliver_signal(sig, sid):
        """Доставка ГОТОВОГО сигнала: пост в канал + рассылка по воронке.

        Отдельно от _broadcast, потому что зовётся из ДВУХ мест: auto-ветки поллера
        и /confirm в semiauto — раньше подтверждённый кандидат не доставлялся никому
        (аудит 02.09: комментарий обещал «рассылка после /confirm», кода не было)."""
        try:
            full = templates.render_signal_dm(sig, sid)
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
                    photo = await _render_png(_signal_chart_png, sig, sid)
                except Exception as e:                 # noqa: BLE001
                    log.warning("график сигнала #%s: %s", sid, e)
            try:
                if photo is not None and caption_fits(ch_text):
                    msg = await bot.send_photo(
                        channel, BufferedInputFile(photo, filename=f"signal_{sid}.png"),
                        caption=ch_text, parse_mode="HTML",
                        reply_markup=_channel_kb("chsig"))
                    log.info("сигнал #%s: пост в канал OK (фото, msg_id=%s)", sid, msg.message_id)
                else:
                    if photo is not None:               # подпись не влезла — фото отдельно
                        await bot.send_photo(
                            channel, BufferedInputFile(photo, filename=f"signal_{sid}.png"))
                    # тизер всегда несёт хотя бы CTA-кнопку в бота: «/subscribe» в тексте
                    # канала некликабелен — пост без кнопки был тупиком воронки (аудит 02.09)
                    msg = await bot.send_message(
                        channel, ch_text, parse_mode="HTML",
                        reply_markup=(unlock_kb or _channel_kb("chsig")) if ch_is_teaser
                        else _channel_kb("chsig"))
                    log.info("сигнал #%s: пост в канал OK (msg_id=%s%s)", sid, msg.message_id,
                             ", фото отдельно" if photo is not None else "")
            except Exception as e:                     # noqa: BLE001 — канал не роняет рассылку
                log.warning("пост в канал %s: %s", channel, e)
        # воронка: админы/платные -> полный; free -> полный до лимита/нед, дальше тизер
        plan = await asyncio.to_thread(payments.plan_recipients, sid,
                                       runtime.free_per_week(), tuple(cfg.admin_ids))
        sent = 0
        hour_utc = datetime.now(timezone.utc).hour
        for uid, kind in plan:
            # мьют/тихие часы гейтят только DM-пуш сигнала (не квоту): подавлённого free-юзера
            # не списываем — получит сигналы, когда снимет тишину (в пределах недельного лимита)
            if await asyncio.to_thread(payments.is_signal_suppressed, uid, hour_utc):
                continue
            # под полным сигналом — инлайн-панель (график/риск); под тизером — кнопка разблокировки
            markup = _signal_actions_kb(sid) if kind == "full" else unlock_kb
            text = full if kind == "full" else teaser
            if kind == "full":
                # персональный объём (/depo): строка ТОЛЬКО в полной версии — по тизеру
                # без уровней размер позиции не посчитать. Сбой расчёта не роняет доставку.
                try:
                    prefs = await asyncio.to_thread(payments.depo_prefs, uid)
                    if prefs is not None:
                        text = text + templates.personal_size_line(
                            sig, prefs[0], prefs[1], cfg.max_leverage)
                except Exception as e:                 # noqa: BLE001
                    log.warning("depo-строка %s: %s", uid, e)
            try:
                await _send_retry(uid, text, parse_mode="HTML", reply_markup=markup)
                await asyncio.to_thread(payments.record_delivery, uid, sid, kind)
                sent += 1
            except Exception as e:                     # noqa: BLE001 — один получатель не роняет рассылку
                log.warning("send %s: %s", uid, e)
                if _dead_recipient(e):
                    # мёртвый получатель — не слать каждый сигнал; /start вернёт (аудит-2 ops [8])
                    await asyncio.to_thread(payments.mark_blocked, uid)
            # пауза между отправками: ~20 msg/с — под глобальным лимитом Telegram,
            # без неё flood-wait гарантирован при росте базы
            await asyncio.sleep(0.05)
        log.info("сигнал #%s: отправлено %d из %d (план)", sid, sent, len(plan))

    _webapp_watch = {"fails": 0}

    async def _watch_webapp():
        """Сторож Mini App: публичный URL должен отвечать. Инцидент 31.08-02.09 —
        cloudflared 2 суток висел в ретрай-лупе, дашборд лежал БЕЗ единого алерта
        (dead-man switch пингует только пульс бота). 5 фейлов подряд -> алерт админу
        + kickstart туннель-агента (launchd поднимет новый URL и впишет его сам)."""
        wurl = runtime.webapp_url()
        if not wurl.startswith("https://"):
            return
        def _probe() -> bool:
            import urllib.request
            req = urllib.request.Request(wurl.rstrip("/") + "/healthz",
                                         headers={"User-Agent": "rsirange-watchdog"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        try:
            ok = await asyncio.to_thread(_probe)
        except Exception:                              # noqa: BLE001 — любой сбой = не отвечает
            ok = False
        if ok:
            _webapp_watch["fails"] = 0
            return
        _webapp_watch["fails"] += 1
        n = _webapp_watch["fails"]
        if n == 5 or n % 60 == 0:              # алерт через ~5 мин, дальше ~раз в час
            for admin_id in cfg.admin_ids:
                try:
                    await bot.send_message(
                        admin_id, f"🕸 Mini App не отвечает {n} мин ({wurl}) — "
                                  "перезапускаю туннель.")
                except Exception as e:                 # noqa: BLE001
                    log.warning("webapp-watch алерт %s: %s", admin_id, e)
            try:
                import os as _os
                import subprocess as _sp
                # в потоке: синхронный run до 15с замораживал event loop (поллинг, стрим)
                await asyncio.to_thread(
                    _sp.run, ["launchctl", "kickstart", "-k",
                              f"gui/{_os.getuid()}/com.rsirange.tunnel"],
                    timeout=15, check=False, capture_output=True)
            except Exception as e:                     # noqa: BLE001 — не-launchd окружение
                log.warning("kickstart туннеля: %s", e)

    async def _maintenance():
        try:
            await _watch_webapp()
        except Exception as e:                         # noqa: BLE001 — сторож не роняет такт
            log.warning("webapp watch: %s", e)
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
        # оффсайт (iCloud) молча отвалился: локальные бэкапы идут, «другого диска» нет —
        # алерт раз в сутки, если оффсайт был и протух > 50ч (аудит 05.10)
        last_off = runtime.last_offsite_at()
        if last_off and should_alert_backup_stale(last_off, runtime.last_offsite_alert_at(),
                                                  now_, stale_hours=50):
            text = (f"⚠️ Оффсайт-копия бэкапа в iCloud не обновлялась "
                    f"{_fmt_age(last_off, datetime.now(timezone.utc))} — проверьте iCloud Drive "
                    "(вход, место) и logs/backup.log. Локальные бэкапы при этом идут.")
            for admin_id in cfg.admin_ids:
                try:
                    await bot.send_message(admin_id, text)
                except Exception as e:                     # noqa: BLE001
                    log.warning("offsite stale алерт админу %s: %s", admin_id, e)
            await asyncio.to_thread(runtime.set_value, "last_offsite_alert_at", now_)
        # питание: Mac на батарее — бот живёт, пока не сядет (09.10: 3.5 дня простоя)
        try:
            batt = await asyncio.to_thread(_pmset_batt)
            last_b = runtime.last_battery_alert_at()
            if should_alert_battery(batt, last_b, now_):
                for admin_id in cfg.admin_ids:
                    try:
                        await bot.send_message(admin_id, battery_alert_text(batt))
                    except Exception as e:                 # noqa: BLE001
                        log.warning("battery алерт админу %s: %s", admin_id, e)
                await asyncio.to_thread(runtime.set_value, "last_battery_alert_at", now_)
            elif batt and not batt["on_battery"] and last_b:
                for admin_id in cfg.admin_ids:
                    try:
                        await bot.send_message(admin_id, "🔌 Питание восстановлено — Mac "
                                               "снова на зарядке.")
                    except Exception as e:                 # noqa: BLE001
                        log.warning("battery ok админу %s: %s", admin_id, e)
                await asyncio.to_thread(runtime.set_value, "last_battery_alert_at", "")
        except Exception as e:                             # noqa: BLE001
            log.warning("battery check: %s", e)
        # watchdog такта поллера: процесс жив (обслуживание тикает), но скан-цикл встал —
        # порог = 2× slack (полный пропущенный цикл + буфер, не единичный медленный тик)
        tick_stale = poller_tick_slack_seconds(runtime.timeframes()) * 2
        uptime_s = (datetime.now(timezone.utc) - start_time).total_seconds()
        if should_alert_poller_stale(runtime.last_poller_tick_at(),
                                     runtime.last_poller_alert_at(), now_, tick_stale, uptime_s):
            tick_age = _fmt_age(runtime.last_poller_tick_at(), datetime.now(timezone.utc))
            text = (f"⚠️ Такт поллера завис ({tick_age}) — скан-цикл не тикал. "
                    "Проверьте logs/bot.log; при необходимости рестарт. /health для деталей.")
            for admin_id in cfg.admin_ids:
                try:
                    await bot.send_message(admin_id, text)
                except Exception as e:                     # noqa: BLE001
                    log.warning("poller stale алерт админу %s: %s", admin_id, e)
            await asyncio.to_thread(runtime.set_last_poller_alert_at, now_)
        # пауза висит давно — напомнить админу (раз в сутки), чтобы молчание бота не
        # выглядело поломкой и не стоило недель без сигналов
        try:
            since_ = await asyncio.to_thread(runtime.paused_since)
            if should_remind_pause(since_, runtime.last_pause_reminder_at(), now_):
                text = (f"⏸ Сигналы на паузе уже {pause_age_text(since_)} — новых сделок нет, "
                        "канал молчит. "
                        "Открытые позиции ведутся как обычно. Возобновить: /resume.")
                for admin_id in cfg.admin_ids:
                    try:
                        await bot.send_message(admin_id, text)
                    except Exception as e:                 # noqa: BLE001
                        log.warning("pause reminder админу %s: %s", admin_id, e)
                await asyncio.to_thread(runtime.set_last_pause_reminder_at, now_)
        except Exception as e:                             # noqa: BLE001
            log.warning("pause reminder: %s", e)
        # сторож судьи: открытые сигналы, переросшие контрактный потолок жизни, значат, что
        # исход не скорится (дашборд копит фальшивый плавающий R) — алерт раз в 6ч
        try:
            overdue = await asyncio.to_thread(journal.overdue_open_signals, now_)
            last_od = runtime.last_overdue_alert_at()
            due = last_od is None or (datetime.fromisoformat(now_) - datetime.fromisoformat(
                last_od)).total_seconds() >= 6 * 3600
            if overdue and due:
                items = ", ".join(f"#{o['id']} {o['symbol'].split('/')[0]} "
                                  f"(+{o['over_h']:.0f}ч)" for o in overdue[:10])
                text = (f"⚠️ Судья не разрешает {len(overdue)} сигн.: {items} — открыты "
                        "дольше потолка жизни сделки. Проверьте logs/bot.log (фетч баров, "
                        "символ на бирже данных); дашборд по ним показывает неверный R.")
                for admin_id in cfg.admin_ids:
                    try:
                        await bot.send_message(admin_id, text)
                    except Exception as e:                 # noqa: BLE001
                        log.warning("overdue алерт админу %s: %s", admin_id, e)
                await asyncio.to_thread(runtime.set_value, "last_overdue_alert_at", now_)
        except Exception as e:                             # noqa: BLE001
            log.warning("overdue check: %s", e)
        # анти-кластерный тормоз: алерт админу ОДИН раз на вход стороны в кулдаун
        # (транзишен-детект через settings; при выходе из окна метка снимается)
        try:
            cd_n = runtime.side_cooldown_stops()
            if cd_n > 0:
                cd_h = runtime.side_cooldown_hours()
                alerted = set(s for s in runtime.side_cooldown_alerted().split(",") if s)
                now_in = set()
                for side_ in ("LONG", "SHORT"):
                    stops_ = await asyncio.to_thread(journal.recent_side_stops, side_, cd_h)
                    if stops_ >= cd_n:
                        now_in.add(side_)
                        if side_ not in alerted:
                            text = (f"⛔ Тормоз стороны {side_}: {stops_} стопов за "
                                    f"{cd_h:.0f}ч — новые {side_}-сигналы на паузе, пока "
                                    "окно не очистится. /bysetup — картина по сетапам; "
                                    "выключается в /admin → Стратегия.")
                            for admin_id in cfg.admin_ids:
                                try:
                                    await bot.send_message(admin_id, text)
                                except Exception as e:     # noqa: BLE001
                                    log.warning("cooldown алерт %s: %s", admin_id, e)
                if now_in != alerted:
                    await asyncio.to_thread(
                        runtime.set_side_cooldown_alerted, ",".join(sorted(now_in)))
        except Exception as e:                             # noqa: BLE001
            log.warning("side cooldown check: %s", e)
        # TTL просроченных semiauto-кандидатов (готовим впрок — сейчас signal_mode=auto)
        try:
            expired_ids = await asyncio.to_thread(journal.expire_stale_candidates)
            if expired_ids:
                log.info("TTL: просрочено кандидатов: %s", expired_ids)
        except Exception as e:                             # noqa: BLE001
            log.warning("expire_stale_candidates: %s", e)
        # истёкшие подписки -> free + уведомление (точка максимального интента:
        # кнопки оплаты прямо в сообщении, не голый /subscribe — аудит 02.09)
        for uid in await asyncio.to_thread(payments.expire_overdue):
            try:
                await _send_retry(uid, "⌛ Подписка истекла. Продлить — и снова получать "
                                       "все сигналы:", reply_markup=_subscribe_kb())
            except Exception as e:                     # noqa: BLE001
                log.warning("expire notify %s: %s", uid, e)
                if _dead_recipient(e):
                    await asyncio.to_thread(payments.mark_blocked, uid)
        # напоминания о продлении (за 3 дня, единожды). mark_reminded ставится и при
        # сбое: заблокировавший бота платник ретраился каждые 60с все ~2.5 суток окна
        # (аудит 02.09; паттерн дрипа)
        for row in await asyncio.to_thread(payments.due_for_reminder, 3):
            try:
                await _send_retry(
                    row["tg_id"],
                    f"⏳ Подписка истекает {row['paid_until'][:10]}. Продлить:",
                    reply_markup=_subscribe_kb())
            except Exception as e:                     # noqa: BLE001
                log.warning("reminder %s: %s", row["tg_id"], e)
                if _dead_recipient(e):
                    await asyncio.to_thread(payments.mark_blocked, row["tg_id"])
            await asyncio.to_thread(payments.mark_reminded, row["tg_id"])
        # автоактивация оплаченных USDT-инвойсов (уведомления изолированы per-юзер)
        try:
            activated = await asyncio.to_thread(payments.poll_cryptobot)
        except Exception as e:                         # noqa: BLE001
            log.warning("poll_cryptobot: %s", e)
            activated = []
        for uid, paid_days in activated:
            try:
                await bot.send_message(
                    uid, "✅ Оплата USDT получена — подписка активна! /subscribe")
            except Exception as e:                     # noqa: BLE001
                log.warning("usdt activate notify %s: %s", uid, e)
            await _reward_referrer(uid, paid_days)     # реф-награда за реальный USDT-платёж
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
            except Exception as e:                     # noqa: BLE001 — сбой БД: ретрай след. тактом
                log.warning("winback issue %s: %s", uid, e)
                continue                                # штамп НЕ ставим — касание не потрачено
            try:
                await bot.send_message(
                    uid,
                    templates.render_winback(code, _WB_DAYS, _WB_VALID, stats=journal.stats()),
                    parse_mode="HTML")
            except Exception as e:                     # noqa: BLE001 — закрытая личка: не ретраить
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
            # раз в час (был раз в сутки): живой список открытых с возрастами делает
            # закреп «дышащим», а таймстемп в подвале гарантирует изменение текста
            if should_refresh_board(runtime.last_board_update_at(), now, hours=1):
                try:
                    st = await asyncio.to_thread(journal.stats)
                    by_symbol = await asyncio.to_thread(journal.stats_by_symbol)
                    open_rows = await asyncio.to_thread(journal.open_signals_brief)
                    cohort = (await asyncio.to_thread(journal.cohort_stats)
                             if runtime.signal_mode() == "semiauto" else None)
                    text = format_channel_live_board(st, by_symbol, cohort,
                                                     open_rows, now)
                except Exception as e:                  # noqa: BLE001
                    # сбой рендера/чтения БД — состояние табло не трогаем, повторим на след. такте
                    log.warning("live board render: %s", e)
                else:
                    board_ok = True
                    msg_id = runtime.live_board_message_id()
                    if msg_id:
                        try:
                            await bot.edit_message_text(text, chat_id=channel,
                                                        message_id=msg_id, parse_mode="HTML")
                        except Exception as e:          # noqa: BLE001
                            err = str(e).lower()
                            gone = isinstance(e, TelegramBadRequest) and (
                                "not found" in err or "can't be edited" in err
                                or "message_id_invalid" in err)
                            if "message is not modified" in err:
                                pass                     # текст не изменился — не сбой, id валиден
                            elif not gone:
                                # сеть/сервер/лимит: табло живо, повторим следующим тактом.
                                # Раньше любой сбой сбрасывал id — 13 дублей табло в канале
                                # с уведомлением на каждый (27.08–30.09, аудит 05.10)
                                log.warning("edit live board (повторю): %s", e)
                                board_ok = False
                            else:
                                # протухло/удалено/нет прав — пересоздать. Метку времени НЕ
                                # ставим (ревью 14.08): иначе «след. цикл» превращался в
                                # «следующее часовое окно» — до 2ч канал без табло
                                log.warning("edit live board (пересоздам на след. цикле): %s", e)
                                await asyncio.to_thread(runtime.set_live_board_message_id, None)
                                board_ok = False
                    else:
                        try:
                            sent = await bot.send_message(channel, text, parse_mode="HTML")
                        except Exception as e:          # noqa: BLE001
                            log.warning("send live board: %s", e)
                            board_ok = False
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
                    if board_ok:
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
                    png_bytes = await _render_png(graphics.render_weekly_card, card_data)
                    await bot.send_photo(
                        channel, BufferedInputFile(png_bytes, filename="weekly_card.png"),
                        reply_markup=_channel_kb("chcard"))
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
                try:
                    await _post_weekly_review(channel, week_start.isoformat(),
                                              week_end.isoformat())
                except Exception as e:                      # noqa: BLE001
                    log.warning("разбор недели: %s", e)
        # недельный ЛИЧНЫЙ дайджест подписчикам (ретеншн) — ВНЕ гейта канала: выключение
        # канала не должно молча убивать подписческую фичу (аудит-2 UX [4]). Отдельный
        # маркер от карточки; маркер ставим ДО рассылки: краш посреди рассылки не должен
        # переслать дайджест всем заново.
        d_now = datetime.now(timezone.utc)
        d_year, d_week, _ = d_now.isocalendar()
        d_week_key = f"{d_year}-W{d_week:02d}"
        # не раньше 08:00 UTC: первый такт новой ISO-недели — полночь понедельника,
        # личный пуш уходил прямо в тихие часы аудитории (аудит 02.09); маркер недели
        # ставится при фактической отправке, так что дайджест просто ждёт утра
        if d_now.hour >= 8 and should_post_weekly(runtime.weekly_digest_last_week(),
                                                  d_week_key):
            await asyncio.to_thread(runtime.set_weekly_digest_last_week, d_week_key)
            try:
                # заодно раз в неделю чистим давно сработавшие прайс-алерты
                from app.delivery import alerts as alerts_mod
                purged = await asyncio.to_thread(alerts_mod.purge_old_triggered)
                if purged:
                    log.info("прайс-алерты: вычищено %d старых сработавших", purged)
            except Exception as e:                          # noqa: BLE001
                log.warning("purge алертов: %s", e)
            try:
                d_start, d_end = weekly_report_period(d_now)
                d_wk = await asyncio.to_thread(
                    journal.stats_for_period, d_start.isoformat(), d_end.isoformat())
                d_st = await asyncio.to_thread(journal.stats)
                d_label = (f"{d_start.strftime('%d.%m')}"
                           f"–{(d_end - timedelta(days=1)).strftime('%d.%m')}")
                d_text = templates.render_weekly_digest(d_label, d_wk, d_st)
                # рассылка — ОТДЕЛЬНОЙ таской: она растёт с аудиторией, а внутри
                # _maintenance блокировала бы 60с-цикл (пульс dead-man's switch, watchdog
                # поллера, активацию оплат) на минуты (ревью 12.08). Маркер недели уже
                # стоит — повторный вход в _maintenance рассылку не продублирует.
                asyncio.create_task(_send_digests(
                    d_text, d_label, d_wk, d_st,
                    d_start.isoformat(), d_end.isoformat()))
            except Exception as e:                          # noqa: BLE001
                log.warning("недельный дайджест: %s", e)

    async def _send_digests(d_text, d_label, d_wk, d_st, start_iso, end_iso):
        """Рассылка недельного дайджеста ВНЕ maintenance-цикла (create_task).

        Подписчикам — общий текст; free-юзерам — с блоком «что ты упустил»
        (payments.missed_teaser_r). Мьют (/notify) уважается: непрошеный маркетинговый
        пуш замьютившему = прямой путь к блокировке бота. Мёртвые получатели
        списываются mark_blocked — не молотить по ним каждую неделю."""
        async def _muted(uid: int) -> bool:
            prefs = await asyncio.to_thread(payments.notify_prefs, uid)
            return bool(prefs.get("muted"))

        sent = f_sent = 0
        try:
            for uid in await asyncio.to_thread(payments.active_subscriber_ids):
                try:
                    if await _muted(uid):
                        continue
                    # коуч дисциплины — только пользующимся отметками (иначе None);
                    # сбой коуча не лишает юзера всего дайджеста
                    block = ""
                    try:
                        coach = await asyncio.to_thread(
                            payments.weekly_discipline, uid, start_iso, end_iso)
                        block = templates.render_discipline_block(coach) if coach else ""
                    except Exception as e:              # noqa: BLE001
                        log.warning("коуч -> %s: %s", uid, e)
                    await _send_retry(uid, d_text + ("\n" + block if block else ""),
                                      parse_mode="HTML")
                    sent += 1
                except Exception as e:                  # noqa: BLE001
                    log.warning("дайджест -> %s: %s", uid, e)
                    if _dead_recipient(e):
                        await asyncio.to_thread(payments.mark_blocked, uid)
            # тизерных закрытий не было — блок молча отсутствует, дайджест уходит всё равно
            for uid in await asyncio.to_thread(payments.free_active_ids):
                try:
                    if await _muted(uid):
                        continue
                    missed = await asyncio.to_thread(
                        payments.missed_teaser_r, uid, start_iso, end_iso)
                    await _send_retry(
                        uid, templates.render_weekly_digest(d_label, d_wk, d_st, missed),
                        parse_mode="HTML")
                    f_sent += 1
                except Exception as e:                  # noqa: BLE001
                    log.warning("дайджест(free) -> %s: %s", uid, e)
                    if _dead_recipient(e):
                        await asyncio.to_thread(payments.mark_blocked, uid)
            log.info("недельный дайджест разослан: %d подписчикам + %d free", sent, f_sent)
        except Exception as e:                          # noqa: BLE001 — таска не роняет процесс
            log.warning("рассылка дайджеста: %s", e)

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
        stale_ids: list = []
        for c in closed:
            if c.get("shadow"):
                continue
            if c["status"] != "filled" and not result_is_fresh(c, now_dt):
                stale_ids.append(c["id"])               # учёт в журнале есть, поста не будет
                continue
            try:
                if c["status"] == "filled":
                    # только свежие филлы (не «исторические» после деплоя/рестарта) и только
                    # если сделка не закрылась этим же тиком — результат сам скажет, что вход был
                    if c["id"] in resolved_ids or not fill_is_fresh(c, now_dt):
                        continue
                    want_chart = lifecycle_chart_wanted(
                        runtime.lifecycle_chart_enabled(), runtime.channel_mode())
                    msg = await _send_lifecycle_post(
                        channel, c["id"], templates.render_fill(c, show_price), want_chart)
                    log.info("сигнал #%s: пост filled OK (msg_id=%s)", c["id"], msg.message_id)
                elif c["status"] == "expired":
                    msg = await bot.send_message(channel, templates.render_expired(c),
                                                 parse_mode="HTML",
                                                 reply_markup=_channel_kb("chres"))
                    log.info("сигнал #%s: пост expired OK (msg_id=%s)", c["id"], msg.message_id)
                elif c["status"] in ("tp1", "tp2", "tp3", "stopped"):
                    want_chart = lifecycle_chart_wanted(
                        runtime.lifecycle_chart_enabled(), runtime.channel_mode())
                    # под результатом — «Рентген сделки» (анатомия по deep-link) + CTA
                    xray_kb = (xray_channel_markup(bot_info["username"], c["id"])
                               if runtime.channel_cta_enabled() else None)
                    msg = await _send_lifecycle_post(
                        channel, c["id"],
                        templates.render_result(c, runtime.display_leverage(),
                                                streak=journal.recent_streak(10)),
                        want_chart, kb=xray_kb)
                    log.info("сигнал #%s: пост result(%s) OK (msg_id=%s)",
                             c["id"], c["status"], msg.message_id)
            except Exception as e:                     # noqa: BLE001
                log.warning("пост цикла сделки в канал #%s: %s", c["id"], e)

        if stale_ids:
            log.warning("запоздалые закрытия без поста в канал: %s", stale_ids)
            note = (f"ℹ️ Доскорено задним числом {len(stale_ids)} сделок "
                    f"({', '.join('#' + str(i) for i in stale_ids[:15])}) — "
                    "посты в канал не отправлялись (исход устарел), журнал и дашборд обновлены.")
            for admin_id in cfg.admin_ids:
                try:
                    await bot.send_message(admin_id, note)
                except Exception as e:                  # noqa: BLE001
                    log.warning("stale-closed заметка админу %s: %s", admin_id, e)

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

    # последняя цена по символу (WS) — кроссинг-окно прайс-алертов между тиками
    _last_price: dict[str, float] = {}

    async def _push_alerts(fired: list) -> None:
        """Пуши сработавших алертов ВНЕ горячего цикла стрима (create_task): send_message
        с его RTT и ретраями не должен тормозить детект реалтайм-филлов и ping WS.
        Транзиентный сбой (не блокировка) перевзводит алерт — «one-shot пинг» это
        обещание доставить один раз, а не сжечь молча (ревью 12.08: at-most-once)."""
        from app.delivery import alerts as alerts_mod
        for a in fired:
            try:
                await bot.send_message(
                    a["tg_id"],
                    f"⏰ <b>{alerts_mod.base_of(a['symbol'])} коснулся "
                    f"{templates._price(a['price'])}</b>\nАлерт сработал и снят. "
                    "Новый: /alert · сигналы по этой монете: /subscribe",
                    parse_mode="HTML")
            except Exception as e:                     # noqa: BLE001
                low_ = str(e).lower()
                if "blocked" in low_ or "chat not found" in low_ or "deactivated" in low_:
                    log.warning("alert push %s: юзер недоступен", a["tg_id"])
                else:
                    await asyncio.to_thread(alerts_mod.rearm, a["id"])
                    log.warning("alert push %s: %s — алерт перевзведён", a["tg_id"], e)

    async def _on_intrabar(symbol_ws: str, high: float, low: float, close: float):
        # реалтайм-филл: касание entry внутри бара -> мгновенный «вход исполнен».
        # Гонку с bar-скорингом решает атомарный try_mark_filled; официальный исход
        # сделки по-прежнему считает ТОЛЬКО bar-скоринг (контракт трек-рекорда).
        from app.data.stream import entry_touched, ws_symbol
        from app.delivery import alerts as alerts_mod
        for symbol in runtime.symbols():
            if ws_symbol(symbol) != symbol_ws:
                continue
            # прайс-алерты (/alert): триггер по КРОССИНГУ уровня последней ценой между
            # тиками, НЕ по экстремумам бара — high/low включают движение ДО постановки
            # алерта, и алерт «на уровень, где бар уже был» стрелял бы мгновенно и ложно
            # (ревью 12.08). Цена первого тика после старта — только сид, без выстрела.
            # Отправка — отдельной таской, горячий цикл стрима не ждёт Telegram.
            try:
                prev = _last_price.get(symbol_ws)
                _last_price[symbol_ws] = close
                if prev is not None and prev != close:
                    fired = await asyncio.to_thread(
                        alerts_mod.check_and_trigger, symbol,
                        max(prev, close), min(prev, close))
                    if fired:
                        asyncio.create_task(_push_alerts(fired))
            except Exception as e:                     # noqa: BLE001
                log.warning("alerts %s: %s", symbol, e)
            rows = await asyncio.to_thread(journal.unfilled_open_signals, symbol)
            scoring_tf = min((SMCEngine.TF_MINUTES.get(tf, 60) for tf in runtime.timeframes()),
                             default=60)
            now_dt = datetime.now(timezone.utc)
            for row in rows:
                if not entry_touched(row["entry"], high, low):
                    continue
                if not journal.fill_window_open(row, now_dt, scoring_tf):
                    continue                            # окно ожидания вышло — не сделка
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
                    want_chart = lifecycle_chart_wanted(
                        runtime.lifecycle_chart_enabled(), runtime.channel_mode())
                    msg = await _send_lifecycle_post(
                        channel, row["id"],
                        templates.render_fill(c, runtime.channel_mode() == "full"), want_chart)
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
        # engine=None -> poller строит движок из runtime.min_confidence() каждый такт.
        # on_tick=None: обслуживание вынесено в НЕЗАВИСИМУЮ таску _maintenance_loop —
        # watchdog поллера не может жить внутри наблюдаемой таски (аудит-2 ops [1]:
        # зависший поллер отключал и watchdog, и весь жизненный цикл подписок)
        await run_poller(_broadcast, engine=None, on_tick=None, on_close=_on_close)

    async def _maintenance_loop():
        # 60с вместо часового такта поллера: USDT-активация/алерты не ждут закрытия свечи,
        # watchdog реально ловит зависший поллер. Все блоки идемпотентны или за маркерами
        # (карточка/дайджест/табло — раз в сутки/неделю независимо от частоты цикла).
        while True:
            try:
                await _maintenance()
                # executor V2 (демо BingX, флаг выкл по умолчанию): сверка журнал->биржа.
                # Отдельный try: сбой биржи не должен глушить пульс dead-man's switch ниже.
                executor_report: list = []
                if runtime.executor_enabled():
                    try:
                        from app.v2 import bingx_executor
                        executor_report = await asyncio.to_thread(bingx_executor.reconcile)
                        for line in executor_report:
                            log.info("executor: %s", line)
                    except Exception:                   # noqa: BLE001 — цикл живёт всегда
                        log.exception("executor reconcile")
                # пульс dead-man's switch ПОСЛЕ успешного цикла: упавшее обслуживание
                # прекращает пульс -> внешний алерт (грейс healthchecks поглощает
                # разовые сбои). Сеть/URL глотается внутри healthcheck_ping.
                await asyncio.to_thread(healthcheck_ping, get_settings().healthcheck_url)
                # алерты executor — ПОСЛЕ пульса и ОДНИМ сообщением на админа: таймауты
                # Telegram не должны сдвигать пинг за грейс healthchecks (ревью 13.08)
                if executor_report:
                    joined = "🤖 Executor:\n" + "\n".join(executor_report)
                    for admin_id in cfg.admin_ids:
                        try:
                            await bot.send_message(admin_id, joined)
                        except Exception:               # noqa: BLE001
                            pass
                # проверка решений вперёд (правила 24.10) — раз в ISO-неделю, маркер ДО отправки
                _fy, _fw, _ = datetime.now(timezone.utc).isocalendar()
                _fwk = f"{_fy}-W{_fw:02d}"
                if runtime._raw("forward_report_last_week") != _fwk:
                    await asyncio.to_thread(runtime.set_value, "forward_report_last_week", _fwk)
                    try:
                        from app.brain import forward_check
                        _ftxt = await asyncio.to_thread(forward_check.report,
                                                        datetime.now(timezone.utc))
                    except Exception:                   # noqa: BLE001
                        log.exception("forward report")
                        _ftxt = None
                    for admin_id in (cfg.admin_ids if _ftxt else ()):
                        try:
                            await bot.send_message(admin_id, _ftxt, parse_mode="HTML")
                        except Exception:               # noqa: BLE001
                            pass
                # недельный отчёт «исполнение vs журнал» — админам. Маркер недели
                # СТАВИТСЯ ДО отправки (принцип трек-рекорда: пропуск лучше дубля)
                if runtime.executor_enabled():
                    _now = datetime.now(timezone.utc)
                    _iy, _iw, _ = _now.isocalendar()
                    _wk = f"{_iy}-W{_iw:02d}"
                    if should_post_weekly(runtime.exec_report_last_week(), _wk):
                        await asyncio.to_thread(runtime.set_exec_report_last_week, _wk)
                        try:
                            from app.v2 import bingx_executor
                            _txt = await asyncio.to_thread(
                                bingx_executor.execution_report,
                                (_now - timedelta(days=7)).isoformat(), _now.isoformat())
                        except Exception:               # noqa: BLE001
                            log.exception("execution report")
                            _txt = None
                        if _txt:
                            for admin_id in cfg.admin_ids:
                                try:
                                    await bot.send_message(admin_id, _txt,
                                                           parse_mode="HTML")
                                except Exception:       # noqa: BLE001
                                    pass
                # og-карточка дашборда (превью ссылки с эквити): раз в сутки, через
                # общий семафор рендеров; webapp отдаёт файл по /og.png
                try:
                    _day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    if (runtime.og_card_last_day() != _day and runtime.webapp_url()
                            and DATAOG_DIR.exists()):
                        await asyncio.to_thread(runtime.set_og_card_last_day, _day)
                        st_all = await asyncio.to_thread(journal.stats)
                        if st_all.get("trades", 0) > 1:
                            png_ = await _render_png(
                                graphics.render_og_card, st_all.get("total_r", 0.0),
                                st_all.get("trades", 0),
                                await asyncio.to_thread(journal.equity_series))
                            (DATAOG_DIR / "og.png").write_bytes(png_)
                            log.info("og-карточка обновлена (%d байт)", len(png_))
                except Exception:                       # noqa: BLE001
                    log.exception("og-карточка")
                # ERROR-алерты админам: батч из буфера логов, кулдаун 10 мин.
                # Без parse_mode: тексты ошибок содержат <>&. Дедуп повторов.
                if _ERRBUF.buf and _monotonic() - _ERRBUF.last_sent > 600:
                    _ERRBUF.last_sent = _monotonic()
                    uniq = list(dict.fromkeys(_ERRBUF.buf))[:6]
                    _ERRBUF.buf.clear()
                    err_msg = "🚨 Ошибки бота (см. журнал сервера):\n" + \
                              "\n".join(f"— {u}" for u in uniq)
                    for admin_id in cfg.admin_ids:
                        try:
                            await bot.send_message(admin_id, err_msg)
                        except Exception:               # noqa: BLE001
                            pass
            except Exception:                          # noqa: BLE001 — цикл живёт всегда
                log.exception("maintenance-цикл")
            await asyncio.sleep(60)

    def _task_died(task) -> None:
        # смерть фоновой таски не должна быть молчаливой: лог + ПРЯМОЙ HTTP-алерт админам
        # (не aiogram — вдруг сломан именно он). Процесс жив (KeepAlive не поможет),
        # но сигналы/обслуживание встали — оператору нужен рестарт.
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        name = task.get_name()
        log.error("фоновая таска %s умерла: %r", name, exc)
        for admin_id in cfg.admin_ids:
            try:
                _send_raw_telegram_message(
                    cfg.tg_bot_token, admin_id,
                    f"🔥 Фоновая таска «{name}» умерла: {exc!r}\n"
                    f"Процесс жив, но «{name}» не работает — нужен рестарт "
                    "(launchctl kickstart -k gui/$UID/com.rsirange.bot).")
            except Exception:                          # noqa: BLE001 — алерт не роняет колбэк
                log.exception("не удалось отправить алерт о смерти таски %s", name)

    async def _main():
        try:
            await register_bot_commands(bot, cfg.admin_ids)
            await register_bot_profile(bot)
        except Exception as e:                         # noqa: BLE001 — меню не критично для работы
            log.warning("register_bot_commands: %s", e)
        # bot.me() кешируется aiogram; start_polling зовёт его без защиты — при старте без
        # сети (Mac проснулся без DNS) процесс падал целиком (4 краша 01.10, аудит 05.10)
        for attempt in range(8):
            try:
                await bot.me()
                break
            except Exception as e:                     # noqa: BLE001
                delay = min(60, 5 * 2 ** attempt)
                log.warning("bot.me() при старте: %s — повтор через %dс", e, delay)
                await asyncio.sleep(delay)
        try:
            bot_info["username"] = (await bot.get_me()).username   # для CTA-кнопок канала
            # username в settings — Mini App строит deep-link «поделиться карточкой»
            await asyncio.to_thread(runtime.set_value, "bot_username", bot_info["username"])
        except Exception as e:                         # noqa: BLE001 — кнопки не критичны
            log.warning("get_me (CTA-кнопки канала будут без ссылки): %s", e)
        poller = asyncio.create_task(_poller(), name="poller")
        poller.add_done_callback(_task_died)
        # реалтайм-стрим (за флагом stream_enabled; при выключенном флаге тихо спит)
        stream_task = asyncio.create_task(_stream(), name="stream")
        stream_task.add_done_callback(_task_died)
        maint_task = asyncio.create_task(_maintenance_loop(), name="maintenance")
        maint_task.add_done_callback(_task_died)
        try:
            await dp.start_polling(bot)
        finally:
            poller.cancel()
            stream_task.cancel()
            maint_task.cancel()

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
