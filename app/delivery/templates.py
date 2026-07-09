"""Единый HTML-рендерер исходящих постов сигнального цикла (канал + личка).

Один источник шаблонов вместо двух разошедшихся (канальный в bot.py и DM в
signal_engine.Signal.format_message). Правила:
- отправитель задаёт parse_mode="HTML"; ВСЕ динамические строки проходят esc();
- цены — в <code> и полной записью (_price): {:g} режет BTC>100k до 6 значащих
  цифр и уходит в экспоненту на мелких альтах;
- разбор «Почему» — в <blockquote expandable> (Bot API 7.3+, aiogram>=3.5);
- дисклеймеры не убирать: продукт — инструмент + честная статистика,
  не обещание доходности.
"""
from __future__ import annotations

import html

from app.core.signal_engine import Signal

LINE = "━━━━━━━━━━━━━━"


def esc(v: object) -> str:
    """Экранировать динамическое значение для parse_mode=HTML."""
    return html.escape(str(v), quote=False)


def _price(x: float) -> str:
    """Полная запись цены без экспоненты и потери точности."""
    return f"{float(x):.8f}".rstrip("0").rstrip(".")


def _arrow(side: str) -> str:
    return "🟢 LONG" if side == "LONG" else "🔴 SHORT"


def _sig_head(sig: Signal) -> str:
    return f"{_arrow(sig.side.value)}   {esc(sig.symbol)}   ·   {esc(sig.timeframe)}"


def _c_head(c: dict) -> str:
    return f"{_arrow(c['side'])}   {esc(c['symbol'])}   ·   {esc(c['timeframe'])}"


def _market_line(sig: Signal) -> str:
    market = (sig.indicators_snapshot or {}).get("market") if isinstance(
        sig.indicators_snapshot, dict) else None
    return f"💹 Цена сейчас:  <code>{_price(market)}</code>\n" if market else ""


def render_signal_channel(sig: Signal, signal_id: int) -> str:
    """Пост нового сигнала в канал (публичная лента)."""
    tps = " / ".join(f"<code>{_price(t)}</code>" for t in sig.take_profits)
    reasons = "\n".join(f"• {esc(r)}" for r in sig.reasons)
    return (
        f"📊 <b>RSI RANGE — Сигнал #{signal_id}</b>\n{LINE}\n"
        f"{_sig_head(sig)}\n\n"
        f"{_market_line(sig)}"
        f"📍 Вход:  <code>{_price(sig.entry)}</code>  (лимит)\n"
        f"🛑 Стоп:  <code>{_price(sig.stop)}</code>\n"
        f"🎯 Цели:  {tps}\n"
        f"⚖️ R/R 1:{sig.risk_reward:.1f}    🔎 {int(sig.confidence * 100)}%\n"
        f"<blockquote expandable>Почему:\n{reasons}</blockquote>\n{LINE}\n"
        "⚠️ Не финсовет. Риск 1–2% на сделку.\n"
        "🔔 Сигналы в личку + статистика: @rsirange_bot"
    )


def render_signal_dm(sig: Signal, signal_id: int) -> str:
    """Полный сигнал в личку подписчику (платные/админы/free до лимита)."""
    conf10 = round(sig.confidence * 10)
    conf_bar = "▰" * conf10 + "▱" * (10 - conf10)
    tps = "\n".join(f"   • TP{i}: <code>{_price(tp)}</code>"
                    for i, tp in enumerate(sig.take_profits, 1))
    reasons = "\n".join(f"— {esc(r)}" for r in sig.reasons)
    return (
        f"🔔 <b>СИГНАЛ #{signal_id}</b>\n"
        f"{_sig_head(sig)}\n{LINE}\n"
        f"{_market_line(sig)}"
        f"📍 Вход:  <code>{_price(sig.entry)}</code>  (лимит)\n"
        f"🛑 Стоп:  <code>{_price(sig.stop)}</code>\n"
        f"🎯 Тейки:\n{tps}\n"
        f"⚖️ R/R до TP{len(sig.take_profits)}: 1:{sig.risk_reward:.1f}\n"
        f"🔎 Уверенность: {conf_bar} {int(sig.confidence * 100)}%\n"
        f"<blockquote expandable>Почему:\n{reasons}</blockquote>\n"
        "⚠️ Не финансовый совет. Риск на сделку — 1–2% депозита."
    )


def render_teaser(sig: Signal, signal_id: int) -> str:
    """Превью для free-юзера сверх недельного лимита / канала в режиме teaser."""
    return (
        f"🔒 <b>СИГНАЛ #{signal_id}</b> — превью\n"
        f"{_sig_head(sig)}\n"
        "Вход, стоп, тейки и разбор — по подписке.\n"
        "/subscribe — открыть все сигналы"
    )


def render_fill(c: dict, show_price: bool = True) -> str:
    """Лимитный вход исполнился (c — словарь из journal.update_outcomes_from_bars).

    show_price=False — канал в режиме teaser: цену входа не раскрываем."""
    price_line = (f"Цена входа: <code>{_price(c['entry'])}</code>\n"
                  if show_price and c.get("entry") else "")
    return (
        f"🎯 <b>Сигнал #{c['id']} — вход исполнен</b>\n"
        f"{_c_head(c)}\n"
        f"{price_line}"
        "Позиция активна. Исход запишется автоматически — стоп или цели."
    )


def render_expired(c: dict) -> str:
    """Лимитка не исполнилась за отведённые бары — сделки не было (честная отмена)."""
    return (
        f"⌛ <b>Сигнал #{c['id']} — отменён</b>\n"
        f"{_c_head(c)}\n"
        "Лимитный вход не исполнился — сделки не было, в статистику не идёт."
    )


def render_result(c: dict, display_leverage: float = 0.0) -> str:
    """Результат закрытой сделки (публичный трек-рекорд).

    display_leverage>0 — ведущим числом показываем результат при плече ×N
    (иллюстративная арифметика: реального плеча бот не знает, сделки не
    исполняет); сырое движение цены остаётся вторым числом. R — всегда."""
    r = c["result_r"] or 0.0
    if c["status"] == "stopped":
        head, emoji = f"🛑 <b>Сигнал #{c['id']} — стоп</b>", "📉"
    else:
        head, emoji = f"✅ <b>Сигнал #{c['id']} — {esc(c['status'].upper())}</b>", "📈"
    pct = c.get("price_move_pct")
    move_line = ""
    if pct is not None:
        if display_leverage > 0:
            move_line = (f"При плече ×{display_leverage:g}: "
                         f"<b>{pct * display_leverage:+.1f}%</b>"
                         f"  (движение цены {pct:+.2f}%)\n")
        else:
            move_line = f"Движение цены: {pct:+.2f}%\n"
    return (
        f"{head}\n{_c_head(c)}\n"
        f"Результат: <b>{r:+.2f}R</b>  {emoji}\n"
        f"{move_line}{LINE}\n"
        "Вся статистика честно: /stats у @rsirange_bot"
    )
