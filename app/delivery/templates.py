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
import re

from app.core.signal_engine import Signal

LINE = "━━━━━━━━━━━━━━"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


# --- оговорка агрегатов журнала (замер 30.10.2026: docs/measurements/2026-10-30-trail-realism.md)
# Журнал судит выходы по часовым свечам и без издержек: на минутных свечах трейлинг-стоп
# выходит хуже (~0.13R на сделку), а после комиссии, слипа и фандинга (~0.15R) те же правила
# в минусе на всех проверенных отрезках. Любой агрегат журнала на юзер-поверхности (итог R,
# матожидание, $ из R) идёт с этой оговоркой — одной короткой строкой на экран или пост, не
# стеной дисклеймеров. «Демо»/«виртуальные деньги» не пишем (запрет владельца 30.10).
JOURNAL_BASIS = "по журналу, до комиссий"
EXEC_CHECK = "проверка исполнения по минутным свечам — /backtest"
JOURNAL_NOTE = f"ℹ️ Цифры — {JOURNAL_BASIS}. {_cap(EXEC_CHECK)}"          # экраны статистики
CHANNEL_JOURNAL_NOTE = f"{_cap(JOURNAL_BASIS)}. {_cap(EXEC_CHECK)} у @rsirange_bot"  # канал

# --- граница публикации стратегии (мандат владельца: конструкция сетапов — секрет) ---
# Санитизация ТОЛЬКО на рендере: snapshot_json хранит полные reasons для админ-разбора,
# а наружу конструкция (фибо-рецепт, параметры входа от блока) не уходит. Найдено
# аудитом 02.09: «фибо 1/0.62/0/-0.26» = сам рецепт флагмана (имя 6226 из этих уровней).
_REASON_SCRUB: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"фибо [\d./,\- ]+ по импульсу"), "расчётная сетка уровней по импульсу"),
    (re.compile(r"импульс, фибо [\d./,\- ]+"), "импульс, расчётная сетка уровней"),
    (re.compile(r"вход [\d.]+, стоп [\d.]+, тейк [\d.]+ \(RR [\d.:]+\)"),
     "вход/стоп/цель по расчётной сетке"),
    (re.compile(r"вход от (?:OB|BB) \(MT ?\d+%\), стоп за дальнюю границу блока"),
     "вход от зоны интереса, стоп за её границу"),
)


def scrub_reasons(reasons) -> list[str]:
    """Причины сигнала без деталей конструкции сетапа (для канала/DM/любых юзер-поверхностей)."""
    out = []
    for r in reasons or ():
        for pat, repl in _REASON_SCRUB:
            r = pat.sub(repl, r)
        out.append(r)
    return out


def esc(v: object) -> str:
    """Экранировать динамическое значение для parse_mode=HTML."""
    return html.escape(str(v), quote=False)


def _price(x: float) -> str:
    """Полная запись цены без экспоненты и потери точности."""
    return f"{float(x):.8f}".rstrip("0").rstrip(".")


def _arrow(side: str) -> str:
    return "🟢 LONG" if side == "LONG" else "🔴 SHORT"


def _tp_r(entry: float, stop: float, tp: float) -> float:
    """R-мультипликатор тейка: сколько риск-единиц (|вход−стоп|) до цели. Без лукахеда —
    чистая геометрия уровней сигнала."""
    risk = abs(entry - stop)
    return abs(tp - entry) / risk if risk else 0.0


def _stop_dist_pct(entry: float, stop: float) -> float:
    """Ход цены от входа до стопа в %, чтобы юзер сразу видел масштаб риска."""
    return abs(entry - stop) / entry * 100.0 if entry else 0.0


def _sig_head(sig: Signal) -> str:
    return f"{_arrow(sig.side.value)}   {esc(sig.symbol)}   ·   {esc(sig.timeframe)}"


def _c_head(c: dict) -> str:
    return f"{_arrow(c['side'])}   {esc(c['symbol'])}   ·   {esc(c['timeframe'])}"


def _market_line(sig: Signal) -> str:
    market = (sig.indicators_snapshot or {}).get("market") if isinstance(
        sig.indicators_snapshot, dict) else None
    return f"💹 Цена сейчас:  <code>{_price(market)}</code>\n" if market else ""


def trail_line(sig: Signal, entry_prefix: str = "🔁 Трейлинг-стоп ордером на бирже:") -> str:
    """Правило подтяжки стопа ИЗ СНАПШОТА сигнала — то же, по которому судит журнал.

    Без этой строки подписчик держал бы фиксированный стоп, а статистика считала бы
    выход по подтянутому — расхождение публикуемого правила и правила оценки прямо
    нарушает контракт честности. Пусто, если сигнал опубликован без трейлинга."""
    snap = sig.indicators_snapshot if isinstance(sig.indicators_snapshot, dict) else {}
    t = snap.get("trail")
    if not isinstance(t, dict):
        return ""
    try:
        act, dist = float(t["activate_r"]), float(t["distance_r"])
    except (KeyError, TypeError, ValueError):
        return ""
    if act <= 0:
        return ""
    risk = abs(sig.entry - sig.stop)
    trigger = (sig.entry + act * risk) if sig.side.value == "LONG" else (sig.entry - act * risk)
    # параметры НАТИВНОГО трейлинг-ордера биржи (24.10): журнал судит «максимум − dist·R» в
    # реальном времени; стоп, переставляемый руками раз в час, исполняется хуже (−0.27R на
    # выход по трейлу, docs/measurements/2026-10-24-trail-exit-realism.md). Отступ — и ценой
    # (Bybit/BingX), и процентом от цены активации (Binance callback rate).
    gap = dist * risk
    pct = gap / trigger * 100 if trigger else 0.0
    line = (f"{entry_prefix} активация <code>{_price(trigger)}</code> (+{act:g}R), "
            f"отступ <code>{_price(gap)}</code> ({dist:g}R ≈ {pct:.2f}%) — "
            f"стоп за ценой ведёт сама биржа\n")
    if not t.get("tp_exit", True):
        hold = t.get("max_hold_hours") or 0
        hold_txt = f", но не дольше {int(float(hold) / 24)} дн" if float(hold) >= 24 else ""
        line += (f"🏁 Выход: по трейлинг-стопу (цель ниже — ориентир, "
                 f"по ней НЕ закрываем{hold_txt})\n")
    return line


def render_signal_channel(sig: Signal, signal_id: int) -> str:
    """Пост нового сигнала в канал (публичная лента)."""
    tps = " / ".join(f"<code>{_price(t)}</code> ({_tp_r(sig.entry, sig.stop, t):.1f}R)"
                     for t in sig.take_profits)
    stop_dist = _stop_dist_pct(sig.entry, sig.stop)
    reasons = "\n".join(f"• {esc(r)}" for r in scrub_reasons(sig.reasons))
    return (
        f"📊 <b>RSI RANGE — Сигнал #{signal_id}</b>\n{LINE}\n"
        f"{_sig_head(sig)}\n\n"
        f"{_market_line(sig)}"
        f"📍 Вход:  <code>{_price(sig.entry)}</code>  (по касанию)\n"
        f"🛑 Стоп:  <code>{_price(sig.stop)}</code>  (ход {stop_dist:.1f}%)\n"
        f"{trail_line(sig)}"
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
    tps = "\n".join(
        f"   • TP{i}: <code>{_price(tp)}</code>  ({_tp_r(sig.entry, sig.stop, tp):.1f}R)"
        for i, tp in enumerate(sig.take_profits, 1))
    stop_dist = _stop_dist_pct(sig.entry, sig.stop)
    reasons = "\n".join(f"— {esc(r)}" for r in scrub_reasons(sig.reasons))
    return (
        f"🔔 <b>СИГНАЛ #{signal_id}</b>\n"
        f"{_sig_head(sig)}\n{LINE}\n"
        f"{_market_line(sig)}"
        f"📍 Вход:  <code>{_price(sig.entry)}</code>  (по касанию)\n"
        f"🛑 Стоп:  <code>{_price(sig.stop)}</code>  (ход до стопа {stop_dist:.1f}%)\n"
        f"{trail_line(sig)}"
        f"🎯 Тейки:\n{tps}\n"
        f"⚖️ R/R до TP{len(sig.take_profits)}: 1:{sig.risk_reward:.1f}\n"
        f"🔎 Уверенность: {conf_bar} {int(sig.confidence * 100)}%\n"
        f"<blockquote expandable>Почему:\n{reasons}</blockquote>\n"
        "⚠️ Не финансовый совет. Риск на сделку — 1–2% депозита."
    )


def _proof_line(stats: dict | None) -> str:
    """Строка соц-пруфа из journal.stats() (проверяемая статистика в точке пейволла)."""
    if not stats or not stats.get("trades"):
        return ""
    return (f"📊 {stats['trades']} закрытых · winrate {stats['winrate'] * 100:.0f}% · "
            f"матож. {stats['expectancy_r']:+.2f}R/сделку {JOURNAL_BASIS} — "
            "всё проверяемо (/trackrecord)\n")


def render_teaser(sig: Signal, signal_id: int, stats: dict | None = None,
                  open_channel: str | None = None) -> str:
    """Превью для free-юзера сверх недельного лимита / канала в режиме teaser.

    stats (journal.stats()) — соц-пруф прямо на пейволле: не голое «по подписке», а
    проверяемые цифры в точке, где юзер упёрся в лимит.
    open_channel — канал, где этот же сигнал уже опубликован целиком (channel_mode=full):
    тогда «уровни по подписке» было бы неправдой, и тизер говорит, за что на самом деле
    платят — личка без лимита и объём под свой депозит."""
    if open_channel:
        offer = (f"Полный сигнал — в канале {esc(open_channel)}. В личку без лимита, "
                 "с объёмом под твой депозит (/depo) — по подписке.\n"
                 "/subscribe — тарифы")
    else:
        offer = "Вход, стоп, тейки и разбор — по подписке.\n/subscribe — открыть все сигналы"
    return (
        f"🔒 <b>СИГНАЛ #{signal_id}</b> — превью\n"
        f"{_sig_head(sig)}\n"
        f"{_proof_line(stats)}"
        f"{offer}"
    )


def render_winback(promo_code: str, days: int, valid_days: int,
                   stats: dict | None = None) -> str:
    """Win-back ушедшему подписчику: соц-пруф + персональный одноразовый промокод (срочность).

    Это returning-payer копия (в отличие от TRIAL_DRIP — first-purchase). Промокод даёт
    конкретный повод вернуться сейчас, а не абстрактное «возвращайтесь»."""
    return (
        "👋 <b>Скучали по сигналам?</b>\n"
        f"{_proof_line(stats)}"
        f"🎁 Ваш промокод <code>{esc(promo_code)}</code> — +{days} дн. доступа, "
        f"сгорает через {valid_days} дн.\n"
        f"Активировать: <code>/promo {esc(promo_code)}</code>\n"
        "Или /subscribe — все тарифы.\n"
        "⚠️ Не финсовет. Платите за инструмент и проверяемую статистику, не за прибыль."
    )


def render_notify(prefs: dict) -> str:
    """Экран /notify: состояние мьюта и тихих часов (UTC). Гейтят пуш сигналов и раундов игры
    /game в личку — статистика, результаты и доступ всегда доступны по командам."""
    if prefs.get("muted"):
        state = "🔕 <b>Сигналы выключены</b> — пуш в личку не приходит."
    else:
        qs, qe = prefs.get("quiet_start"), prefs.get("quiet_end")
        if qs is not None and qe is not None and qs != qe:
            state = f"🌙 Тихие часы: <b>{qs:02d}:00–{qe:02d}:00 UTC</b> (сигналы вне окна)."
        else:
            state = "🔔 <b>Сигналы включены</b> — приходят сразу на закрытии свечей."
    return (
        "⚙️ <b>Уведомления</b>\n"
        f"{LINE}\n"
        f"{state}\n"
        f"{LINE}\n"
        "Настройки касаются пуша сигналов и раундов игры /game. Статистика (/stats), "
        "трек-рекорд и результаты доступны всегда."
    )


def render_referral_reward(days: int) -> str:
    """Уведомление рефереру: приглашённый оплатил, начислена награда."""
    return (
        f"🎉 <b>+{days} дн. подписки за приглашение!</b>\n"
        "Ваш приглашённый оформил платную подписку — спасибо, что делитесь.\n"
        "Статус: /subscribe"
    )


def render_referral_invite(link: str, ref: dict, reward_days: int) -> str:
    """Экран /referral: личная ссылка + статистика приглашений. reward начисляется только за
    РЕАЛЬНУЮ оплату приглашённого (не триал) — так и пишем, без ложных обещаний."""
    return (
        "🤝 <b>Приглашайте — получайте дни подписки</b>\n"
        f"{LINE}\n"
        f"Ваша ссылка:\n<code>{esc(link)}</code>\n\n"
        f"Пригласили: <b>{ref.get('invited', 0)}</b> · "
        f"оплатили: <b>{ref.get('rewarded', 0)}</b>\n"
        f"🎁 За каждого, кто оформит платную подписку, — <b>+{reward_days} дн.</b> вам.\n"
        f"{LINE}\n"
        "Награда только за реальную оплату приглашённого (не за регистрацию/триал) — "
        "честно и без накрутки."
    )


def render_discipline_block(c: dict) -> str:
    """Коуч дисциплины для платника (по отметкам «я зашёл» в дашборде).

    Только факты за неделю, без нравоучений: что закрыла стратегия, что взял юзер,
    что прошло мимо. Паттерн по сторонам — только при достаточной выборке (>=4
    пропущенных и перекос >=75%), чтобы не сочинять закономерности из шума.
    Пустая строка — если за неделю закрытий не было (блок не рисуем)."""
    if not c or not c["closed_n"]:
        return ""
    lines = [f"{LINE}", "🎯 <b>Твоя дисциплина</b> (по отметкам в дашборде)",
             f"Стратегия закрыла {c['closed_n']} сд: <b>{c['closed_r']:+.1f}R</b>. "
             f"Ты отметил {c['taken_n']}: <b>{c['taken_r']:+.1f}R</b>."]
    if c["skipped_n"]:
        lines.append(f"Мимо прошло {c['skipped_n']} сд на {c['skipped_r']:+.1f}R.")
    sk = c["skipped_short"] + c["skipped_long"]
    if sk >= 4:
        if c["skipped_short"] / sk >= 0.75:
            lines.append(f"Чаще пропускаешь шорты: {c['skipped_short']} из {sk}.")
        elif c["skipped_long"] / sk >= 0.75:
            lines.append(f"Чаще пропускаешь лонги: {c['skipped_long']} из {sk}.")
    lines.append("Отметки: 📈 Дашборд → «Мой итог»")
    return "\n".join(lines) + "\n"


def render_weekly_digest(period_label: str, wk: dict, st: dict,
                         missed: dict | None = None,
                         depo: tuple[float, float] | None = None,
                         ask_depo: bool = False) -> str:
    """Недельный личный дайджест: итоги ЭТОЙ недели (stats_for_period) + всё время.

    Личное касание-ретеншн, дублирует канальную карточку текстом. Только факты по журналу,
    без обещаний. wk — journal.stats_for_period, st — journal.stats.
    missed (только free-юзерам, payments.missed_teaser_r) — блок «что ты упустил»:
    итог полной версии сигналов, которые юзер видел тизерами. Знак ЧЕСТНЫЙ — минусовая
    неделя показывается минусом; блока нет, если тизерных закрытий не было.
    depo — (депозит $, риск %) юзера (/depo): итог недели в его долларах — польза в деньгах,
    а не в абстрактных R (ретеншн 28.10); ask_depo — без депозита предложить /depo."""
    wr = wk.get("winrate", 0) * 100
    usd_line = ""
    usd_shown = bool(depo and wk.get("trades"))     # сумма в $ из R — оговорка называет и её
    if depo and wk.get("trades"):
        dep, risk = depo
        usd = wk.get("total_r", 0) * dep * risk / 100
        usd_line = (f"💵 На ваш депозит ${dep:,.0f} при риске {risk:g}%: все сигналы недели "
                    f"≈ <b>{usd:+,.0f}$</b>\n").replace(",", " ")
    elif ask_depo and wk.get("trades"):
        usd_line = "💵 Итог недели в $ под ваш депозит: <code>/depo 500</code>\n"
    missed_block = ""
    if missed and missed.get("n"):
        missed_block = (
            f"{LINE}\n"
            f"🔒 Ты видел <b>{missed['n']}</b> "
            f"сигнал(ов) этой недели тизерами.\n"
            f"Их итог в полной версии: <b>{missed['total_r']:+.1f}R</b>\n"
            "Полные сигналы без лимита: /subscribe\n"
        )
    # «сколько чистого движения забрали» (фидбэк владельца 30.08): сумма хода цены
    # в пользу сделок за неделю, без плеча — плюсовые ходы минус отданное стопами.
    # Не «забрано»: это ход по выходам журнала, а не взятая кем-то прибыль (замер 30.10)
    week_move = (f"Чистый ход сделок: <b>{wk['total_move_pct']:+.1f}%</b> (без плеча)\n"
                 if wk.get("total_move_pct") is not None and wk.get("trades") else "")
    all_move = (f" · хода {st['total_move_pct']:+.0f}%"
                if st.get("total_move_pct") is not None else "")
    return (
        f"🗓 <b>Итоги недели {esc(period_label)}</b>\n{LINE}\n"
        f"Закрыто: <b>{wk.get('trades', 0)}</b>  "
        f"(✅ {wk.get('wins', 0)} / 🛑 {wk.get('losses', 0)})\n"
        f"Winrate недели: <b>{wr:.0f}%</b> · итог <b>{wk.get('total_r', 0):+.1f}R</b>\n"
        f"{week_move}"
        f"{usd_line}"
        f"Открыто сейчас: {st.get('open', 0)}\n"
        f"{missed_block}{LINE}\n"
        f"За всё время: {st.get('trades', 0)} сделок · матож. "
        f"<b>{st.get('expectancy_r', 0):+.2f}R</b> · итог {st.get('total_r', 0):+.1f}R{all_move}\n"
        f"Полная статистика и журнал: /stats · {EXEC_CHECK}\n"
        f"⚠️ {'R и $' if usd_shown else 'R'} — расчёт {JOURNAL_BASIS}. Не финсовет. "
        "Прошлые результаты не определяют будущие."
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


TRIAL_DRIP: dict = {
    1: (
        "📖 <b>День 1: как читать наши сигналы</b>\n"
        "• <b>Вход (по касанию)</b> — ордер по этой цене; если цена не дошла, сделки нет "
        "(мы честно пишем «отменён»).\n"
        "• <b>Стоп</b> — обязателен, риск 1–2% депозита (посчитать: 🧮 Калькулятор).\n"
        "• <b>Цели</b> — где фиксируется результат; исход запишется автоматически.\n"
        "Свернутый блок «Почему» в сигнале — механика сетапа (термины: /glossary).\n\n"
        "💵 <b>Укажите депозит</b> — и в каждом сигнале будет ваш объём в $: "
        "<code>/depo 500</code> (риск по умолчанию 1%, можно <code>/depo 500 2</code>).\n"
        "🔕 Много уведомлений? /notify — тихие часы и мьют, блокировать бота не нужно.\n\n"
        "⚠️ Не финансовый совет. Никогда не входите без стопа."
    ),
    2: (
        "🔍 <b>День 3: не верьте нам на слово</b>\n"
        "Каждая сделка — в публичном CSV с sha256-хэшем: /trackrecord пришлёт журнал "
        "и офлайн-верификатор (страница считает хэш прямо в браузере, без интернета).\n"
        "Публикуем 100% исходов, включая убыточные — /stats всегда открыта.\n\n"
        "🧰 Ещё бесплатно: перешлите сюда сигнал из любого канала — бот покажет, чем он "
        "закончился (/check); в любом чате наберите <code>@rsirange_bot btc</code> — "
        "карточка рынка. Итоги всех сделок — в канале @rsi_range.\n\n"
        "Так вы проверяете нас, а не доверяете."
    ),
}


def render_trial_last_day(free_per_week: int, st: dict) -> str:
    """Дрип стадии 3 — за сутки до конца триала. Разбор 28.10.2026: 10 из 14 ушедших
    заблокировали бота на 5–6-й день, ровно на конце триала; прежний текст говорил «дальше
    по подписке» + «бэктест края не показал» — и ни слова о том, что остаётся бесплатно.
    Порядок теперь: что остаётся навсегда → журнал как есть (с минусами) → подписка.
    st — journal.stats(); free_per_week — runtime.free_per_week() (0 — без бесплатных)."""
    free = (f"• {free_per_week} полных сигналов в неделю\n" if free_per_week > 0 else "")
    proof = ""
    if st.get("trades"):
        proof = (f"📊 Журнал как есть: {st['trades']} закрытых сделок, в плюс "
                 f"{st.get('winrate', 0) * 100:.0f}%, итог {st.get('total_r', 0):+.1f}R "
                 "до комиссий — минусы не прячем (/stats).\n\n")
    return (
        "⏳ <b>Полный доступ заканчивается завтра</b>\n"
        "Бесплатно остаётся навсегда:\n"
        f"{free}"
        "• проверка чужих сигналов (/check) и карточки рынка (@rsirange_bot btc)\n"
        "• итоги каждой сделки в канале @rsi_range и /stats\n\n"
        f"{proof}"
        "Все сигналы без лимита — /subscribe (от 10⭐, неделя, месяц, год).\n"
        "💯 Money-back 7 дней: не подошло — вернём Stars по запросу.\n\n"
        "Вы платите за инструмент и проверяемую статистику, не за обещание прибыли. "
        "Прошлые результаты не определяют будущие."
    )


def render_trial_ended(summ: dict, free_per_week: int) -> str:
    """Конец триала вместо «⌛ Подписка истекла» (аудит текстов 30.10, К-24): личный итог
    пробных дней в R с честным знаком, правда о том, что остаётся бесплатно (квота free после
    правки 30.10 доступна сразу), подписка — 2 кнопки вместо 8. summ — payments.trial_summary().

    Без оценки в $ и с оговоркой о реальной торговле: замер 30.10
    (docs/measurements/2026-10-30-trail-realism.md) показал, что журнал завышает итог примерно
    на 0.13R за сделку, — сумма в долларах читалась бы как обещанный заработок."""
    n, closed, r = summ["signals"], summ["closed"], summ["total_r"]
    lines = ["🔔 <b>Пробный доступ закончился</b>"]
    if closed:
        lines.append(f"Ваши пробные дни: сигналов {n}, закрылось {closed}, итог по журналу "
                     f"<b>{r:+.1f}R</b> (до комиссий).")
        lines.append("Журнал считает по ценам сигналов — в реальной торговле с комиссиями "
                     "и проскальзыванием результат обычно ниже.")
        if r < 0:
            lines.append("Эти дни вышли в минус — так бывает: серии стопов у стратегии обычны. "
                         "Все сделки с итогами — /stats.")
    elif n:
        lines.append(f"Ваши пробные дни: сигналов {n}, закрытых пока нет — итоги придут в канал "
                     "@rsi_range и в /stats.")
    free = (f"{free_per_week} полных сигналов в неделю (следующий придёт как обычно), "
            if free_per_week > 0 else "")
    lines += ["", f"Дальше бесплатно: {free}итоги всех сделок, проверка чужих сигналов (/check).",
              "Все сигналы в реальном времени — по подписке. Прибыль не обещаем: показываем "
              "каждую сделку как есть."]
    return "\n".join(lines)


def render_access_ended(free_per_week: int) -> str:
    """Конец оплаченного или промо-доступа (аудит текстов 30.10, К-26): сразу сказать, что
    остаётся бесплатно, — «истекла, продлите» читалось как «всё отключили»."""
    free = f"{free_per_week} полных сигналов в неделю и " if free_per_week > 0 else ""
    return (f"⌛ Подписка закончилась — теперь бесплатный режим: {free}итоги всех сделок.\n"
            "Вернуть все сигналы:")


def render_milestone(milestone: int, st: dict) -> str:
    """Пост-веха: N закрытых сделок (честные цифры, вся история проверяема).

    st — journal.stats(): trades/winrate/expectancy_r/total_r. Никаких обещаний —
    только факт накопления проверяемой истории."""
    return (
        f"🏁 <b>Веха: {milestone} закрытых сделок</b>\n{LINE}\n"
        f"Winrate: <b>{st.get('winrate', 0) * 100:.0f}%</b> · "
        f"матожидание <b>{st.get('expectancy_r', 0):+.2f}R</b> · "
        f"итог <b>{st.get('total_r', 0):+.1f}R</b>\n"
        f"{CHANNEL_JOURNAL_NOTE}\n"
        "Каждый исход записан автоматически по ценам биржи — без ручных правок.\n"
        "Полный CSV с sha256: /trackrecord у @rsirange_bot\n"
        f"{LINE}\n⚠️ Не финансовый совет. Прошлые результаты не определяют будущие."
    )


def _streak_footer(streak: dict | None) -> str:
    """Компактный живой скоуборд: серия последних N закрытых сделок (соц-пруф под постом
    результата). streak — journal.recent_streak(): {n, sum_r, wins}."""
    if not streak or not streak.get("n"):
        return ""
    n, wins = streak["n"], streak["wins"]
    return (f"📈 Серия {n}: <b>{streak['sum_r']:+.1f}R</b> · {wins}/{n} в плюс\n")


def render_result(c: dict, display_leverage: float = 0.0, streak: dict | None = None) -> str:
    """Результат закрытой сделки (публичный трек-рекорд).

    display_leverage>0 — ведущим числом показываем результат при плече ×N
    (иллюстративная арифметика: реального плеча бот не знает, сделки не
    исполняет); сырое движение цены остаётся вторым числом. R — всегда.
    streak (journal.recent_streak()) — футер серии последних N сделок."""
    r = c["result_r"] or 0.0
    if c["status"] == "stopped" and r > 0:
        # выход по ПОДТЯНУТОМУ трейлинг-стопу в прибыли: с 25.07 фиксированных тейков
        # нет, победители закрываются ТОЛЬКО так — «🛑 стоп 📉» на +2.6R путал даже
        # владельца (живой вопрос по #315, 12.08.2026), не то что подписчиков
        head, emoji = f"✅ <b>Сигнал #{c['id']} — трейлинг-выход</b>", "📈"
    elif c["status"] == "stopped":
        head, emoji = f"🛑 <b>Сигнал #{c['id']} — стоп</b>", "📉"
    else:
        head, emoji = f"✅ <b>Сигнал #{c['id']} — {esc(c['status'].upper())}</b>", "📈"
    pct = c.get("price_move_pct")
    move_line = ""
    if pct is not None:
        if display_leverage > 0:
            # клэмп −100%: ниже позиция ликвидирована — «−120% депозита» физически невозможен
            lev_pct = max(pct * display_leverage, -100.0)
            move_line = (f"При плече ×{display_leverage:g}: "
                         f"<b>{lev_pct:+.1f}%</b>"
                         f"  (движение цены {pct:+.2f}%)\n")
        else:
            move_line = f"Движение цены: {pct:+.2f}%\n"
    # одна строка оговорки на пост: R сделки, сумма серии и «при плече ×N» — всё из журнала
    return (
        f"{head}\n{_c_head(c)}\n"
        f"Результат: <b>{r:+.2f}R</b>  {emoji}\n"
        f"{move_line}{LINE}\n"
        f"{_streak_footer(streak)}"
        f"{'R и %' if move_line else 'R'} — расчёт {JOURNAL_BASIS}. "
        "Вся статистика: /stats у @rsirange_bot"
    )


def personal_size_line(sig, depo_usd: float, risk_pct: float, max_leverage: int = 20) -> str:
    """Персональная строка объёма под ПОЛНЫМ DM-сигналом (юзер задал /depo).

    Считает calculate_position (риск от депозита): при срабатывании стопа юзер теряет
    ровно risk_pct% депозита. Превышение лимита плеча — предупреждение вместо объёма:
    показать «удобный» объём с недостижимым плечом = подтолкнуть к пересиживанию риска.
    """
    from app.brain.risk import calculate_position

    pos = calculate_position(depo_usd, risk_pct, sig.entry, sig.stop, max_leverage)
    if pos.get("error"):
        return ""
    base = sig.symbol.split("/")[0]
    if pos["warning"]:
        return (f"\n💼 <b>Твой объём</b> (депо ${depo_usd:g}, риск {risk_pct:g}%): "
                f"⚠️ {esc(pos['warning'])}")
    # объём — через _price: {:g} уводил бы количества мелких альтов в экспоненту
    # (0.0000025 -> «2.5e-06 PEPE»), а нотионал/риск в долларах безопасны для {:g}
    return (f"\n💼 <b>Твой объём:</b> {_price(pos['position_qty'])} {esc(base)}"
            f" ≈ ${pos['notional_usd']:g} (риск ${pos['risk_amount']:g},"
            f" плечо ×{pos['implied_leverage']:g})"
            f"\n   Настроено /depo: ${depo_usd:g}, {risk_pct:g}% на сделку")


def render_xray(x: dict) -> str:
    """«Рентген сделки» (deep-link из канала): почему движок вошёл и как вёл позицию.

    Никто на рынке не показывает «почему» — все показывают «что». Рентген: уверенность
    движка (то же число, что в посте сигнала), путь трейлинга, тайминги — только факты
    журнала. Суб-скоры confidence (x["scores"]) наружу не идут: это внутренняя таксономия
    скоринга, та же граница, что у публичного CSV и /api/backtest (правило владельца)."""
    r = x.get("result_r")
    if x["status"] == "expired":
        head_r = "не исполнился (лимитка истекла)"
    else:
        head_r = f"итог по журналу <b>{r:+.2f}R</b>" if r is not None else "итог —"
    lines = [
        f"🔬 <b>Рентген сделки #{x['id']}</b>",
        # тег сетапа НЕ публикуется (собственность стратегии — фидбэк владельца 13.08;
        # та же граница, что у публичного CSV и дашборда)
        f"{esc(x['symbol'])} {esc(x['side'])} {esc(x['timeframe'])} · {head_r}",
        LINE,
        f"Вход <code>{_price(x['entry'])}</code> · стоп <code>{_price(x['stop'])}</code>"
        + (f" · выход <code>{_price(x['exit_price'])}</code>" if x.get("exit_price") else "")
        + (f"\nСтоп-дистанция {x['stop_pct']:g}% цены" if x.get("stop_pct") else ""),
    ]
    if x.get("wait_hours") is not None:
        lines.append(f"Лимитка ждала филла {x['wait_hours']:g}ч"
                     + (f", в позиции {x['hold_hours']:g}ч"
                        if x.get("hold_hours") is not None else ""))
    lines.append(f"{LINE}\nУверенность движка: {int(x['confidence'] * 100)}% (как в сигнале)")
    flags = []
    if x.get("swept"):
        flags.append("свип ликвидности перед входом — был")
    if x.get("clean_impulse"):
        flags.append("импульс чистый (малые тени)")
    if flags:
        lines.append(" · ".join(flags))
    if x.get("trail_moves"):
        peak = (f", максимум зафиксированного <b>+{x['trail_peak_r']:.2f}R</b>"
                if x.get("trail_peak_r") is not None else "")
        lines.append(f"Трейлинг: {x['trail_moves']} подтяжек стопа{peak}")
    lines += [LINE,
              "Так выглядит каждая наша сделка изнутри. Полные сигналы: /subscribe",
              "⚠️ Не финсовет. Прошлые результаты не определяют будущие."]
    return "\n".join(lines)
