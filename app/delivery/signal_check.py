"""Бесплатная проверка чужого сигнала: юзер пересылает пост из любого канала — бот по свечам
биржи пишет, чем сигнал закончился на самом деле.

Запрос владельца 27.10.2026 — рост без бюджета: тысячи людей платят сигнальным каналам и
не видят их настоящей статистики; проверка — повод прийти в бота, а в конце — ссылка на
наш открытый журнал. Чистые функции (разбор текста и прогон по барам) — для тестов;
загрузка баров и время поста (forward_origin.date) — в хэндлере бота.

Честность прогона (та же логика, что у живого судьи, journal._outcome_from_bars):
* бары берутся с ПЕРВОГО, открывшегося после публикации, — до поста цена не в счёт;
* в баре филла засчитывается только стоп, цели — со следующего бара (порядок внутри бара
  неизвестен);
* стоп и цель в одном баре -> первым считаем стоп (худший вариант для сигнала).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

import pandas as pd

from app.delivery.templates import _price, esc

MAX_AGE_DAYS = 180
# потолок длины разбираемого текста: Telegram сам не даёт больше 4096 символов (текст) —
# страховка на случай нового вызывающего кода, а не ограничение для реальных постов
MAX_TEXT_CHARS = 4096

# ReDoS (аудит безопасности 30.10.2026): разбор идёт по тексту, присланному КЕМ УГОДНО
# (пересланный пост), поэтому ни один шаблон не должен быть квадратичным по длине строки.
# Было: «\d+…» без якоря в re.sub пробовал каждую позицию внутри длинного числа (4 КБ
# цифр — 5.3 с на ОДНО сообщение), а пары «\s*X?\s*» перебирали все разбиения пробелов.
# Правила: число матчится только с начала серии цифр — «(?<!\d)», необязательный
# разделитель вместе со своими пробелами — «(?:\s*X)?». Язык совпадений тот же.
_NUM = r"\d+(?:\.\d+)?"
_SKIP_LINE = re.compile(r"плеч|leverage|cross|isolated|кросс|изол|маржа|margin|риск|risk|депо|"
                        r"r/r|\brr\b|соотнош|уверенн|confidence", re.I)
# «цена сейчас» — не вход: только явные «вход / entry / цена входа»
_ENTRY_KW = re.compile(r"entry|enter|вход|buy zone|sell zone|цена входа|точка входа", re.I)
_STOP_KW = re.compile(r"stop|стоп|\bsl\b|s/l", re.I)
# номер цели — только слитно («TP1»): «TP 3400» — это цена, а не номер
_TP_KW = re.compile(r"take[ -]?profit|\btp\d?\b|\bтп\d?\b|take|тейк|цел[ьи]|target|профит|"
                    r"profit", re.I)
_KINDS = (("stop", _STOP_KW), ("tp", _TP_KW), ("entry", _ENTRY_KW))
# номер позиции сразу после ключевого слова: «Target 1: 1.55», «Entry 2) 63500»
_INDEX = re.compile(r"^\s*(?:№\s*)?\d\s*(?:[:)=]|\.(?=\s))")
# строка-значение под заголовком секции (Cornix): «1) 64000», «• 0.3150 - 0.3100»
_ITEM = r"^[\s•·*>→\-]*\d+[).]\s+"
_VALUE_LINE = re.compile(r"[\s•·*>→\-]*(?:\d+[).]\s+)?" + _NUM
                         + r"(?:\s*[-/,;]\s*" + _NUM + r")*\s*(?:\(.*\)\s*)?")
_LONG = re.compile(r"\blong\b|лонг|\bbuy\b|покупк|🟢|📈", re.I)
_SHORT = re.compile(r"\bshort\b|шорт|\bsell\b|продаж|🔴|📉", re.I)
_MARKET = re.compile(r"по рынку|market|по текущ", re.I)
# «S/USDT» — однобуквенный тикер только через «/»; слитно («SUSDT») — от двух символов
_PAIR = re.compile(r"\b(?:([A-Z0-9]{1,15})\s*/|([A-Z0-9]{2,15})(?:\s*-)?)\s*"
                   r"(?:USDT|USDC|USD|PERP)\b", re.I)
_TAG = re.compile(r"[#$]([A-Za-z0-9]{2,15})\b")
_COIN_KW = re.compile(r"(?:монета|coin|pair|пара|тикер|ticker)\s*(?:[:\-]\s*)?[#$]?"
                      r"([A-Za-z0-9]{2,15})\b", re.I)
_NOT_COIN = {"LONG", "SHORT", "BUY", "SELL", "SIGNAL", "SIGNALS", "FUTURES", "SPOT", "VIP",
             "TP", "SL", "ENTRY", "USDT", "USD", "CRYPTO", "TRADE", "TRADING", "SCALP",
             "BINANCE", "BYBIT", "BINGX", "OKX", "MEXC", "BITGET"}


@dataclass
class ParsedSignal:
    coin: str
    side: str                       # LONG | SHORT
    entry_lo: float | None          # None — вход «по рынку»
    entry_hi: float | None
    stop: float
    targets: list[float] = field(default_factory=list)
    # другое прочтение неоднозначных чисел («81,500» — 81 500 или 81.5): выбирает
    # resolve_reading по цене рынка в момент поста
    alternatives: list[ParsedSignal] = field(default_factory=list)
    unsure: bool = False            # основное прочтение сам пост не подтверждает

    @property
    def symbol(self) -> str:
        return f"{self.coin}/USDT:USDT"


# Форматы цен VIP-каналов (30.10.2026: «81,500» читалось как 81.5 — запятая-дробь применялась
# ко всему тексту без единой точки; «$81,500.5» распадался на 81 и 500.5; «64000,65000» —
# 64000.65). Число с разделителями внутри читается по стилю поста: «точка — дробь, запятая —
# тысячи» (81,500.5) или наоборот (0,0345 · 81.500,5). ReDoS: начало числа — только с начала
# серии «(?<![\d.,])», разделитель обязан стоять между цифрами.
_SEP_NUM = re.compile(r"(?<![\d.,])\d+(?:[.,]\d+)+")
_US = re.compile(r"\d{1,3}(?:,\d{3})+\.\d+")          # 81,500.5
_EU = re.compile(r"\d{1,3}(?:\.\d{3})+,\d+")          # 81.500,5
_KILO = re.compile(r"(?<![\d.,])(\d+(?:\.\d+)?)[kKкК](?!\w)")    # 81.5k -> 81500


def _read_token(tok: str) -> tuple[str, str, str | None]:
    """Число с разделителями -> (прочтение «точка — дробь», прочтение «запятая — дробь», улика).

    Улика — стиль, который число доказывает само: 'dot' (81,500.5 · 0.0345 · 1,234,567),
    'comma' (0,0345 · 64,25 · 1.234.567); None — само не решает: «81,500» — это 81 500 или
    81.5, «81.500» — 81.5 или 81 500. Склеенный список («1.2,1.3» · «64000,65000») — числа
    через «, »."""
    if "." in tok and "," in tok:
        if _US.fullmatch(tok):
            v = tok.replace(",", "")
            return v, v, "dot"
        if _EU.fullmatch(tok):
            v = tok.replace(".", "").replace(",", ".")
            return v, v, "comma"
        v = tok.replace(",", ", ")
        return v, v, None
    sep = "," if "," in tok else "."
    parts = tok.split(sep)
    head, tail = parts[0], parts[1:]
    grouped = 1 <= len(head) <= 3 and head[0] != "0" and all(len(p) == 3 for p in tail)
    if len(tail) > 1:                               # 1,234,567 / 1.234.567 — только тысячи
        if grouped:
            v = "".join(parts)
            return v, v, "dot" if sep == "," else "comma"
        v = tok.replace(",", ", ")
        return v, v, None
    frac = tail[0]
    if grouped:                                     # ровно три цифры после — двояко
        thousands, decimal = head + frac, f"{head}.{frac}"
        return (thousands, decimal, None) if sep == "," else (decimal, thousands, None)
    if sep == ".":
        return tok, tok, "dot"
    if len(head) >= 4 and len(frac) >= 4 and head[0] != "0":
        v = f"{head}, {frac}"                       # 64000,65000 — список, не дробь
        return v, v, None
    v = f"{head}.{frac}"
    return v, v, "comma"


def _kilo(m: re.Match) -> str:
    s = format(Decimal(m.group(1)) * 1000, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def _readings(text: str) -> tuple[str, str | None, bool]:
    """Нормализованный текст в двух прочтениях. -> (основное, другое | None, unsure).

    Стиль поста задают числа, однозначные сами по себе: все за «точку» — основное прочтение
    «точка — дробь», все за «запятую» — «запятая — дробь». Улик нет или они спорят — основным
    берём «точку» (так пишет большинство каналов); если двояка при этом запятая («81,500»),
    основное прочтение пост не подтверждает (unsure) — решит цена рынка (resolve_reading)."""
    t = text.replace(" ", " ").replace("–", "-").replace("—", "-")
    spans = []
    evidence = set()
    comma_doubt = False
    for m in _SEP_NUM.finditer(t):
        dot, comma, ev = _read_token(m.group())
        spans.append((m.start(), m.end(), dot, comma))
        if ev:
            evidence.add(ev)
        comma_doubt = comma_doubt or (dot != comma and "," in m.group())
    style = next(iter(evidence)) if len(evidence) == 1 else None

    def build(dot_style: bool) -> str:
        out, pos = [], 0
        for start, end, dot, comma in spans:
            out += [t[pos:start], dot if dot_style else comma]
            pos = end
        s = "".join(out) + t[pos:]
        # «64 200» -> «64200»; не трогаем «TP1 100» (перед числом буква) и «0.5 100»
        s = re.sub(r"(?<![\w.])(\d{1,3}) (\d{3})\b", r"\1\2", s)
        return _KILO.sub(_kilo, s)

    primary = build(style != "comma")
    other = build(style == "comma")
    return primary, (other if other != primary else None), style is None and comma_doubt


def _normalize(text: str) -> str:
    """Основное прочтение текста (см. _readings)."""
    return _readings(text)[0]


_PCT = re.compile(r"(?<!\d)" + _NUM + r"\s*%")                     # проценты — не цены
_RMULT = re.compile(r"[+-]?(?<!\d)" + _NUM + r"\s*R\b")             # R-мультипликаторы «(3.0R)»
_RATIO = re.compile(r"(?<!\d)\d+\s*:\s*" + _NUM)                     # соотношения «1:3.0»
_LEVERAGE = re.compile(r"(?<![A-Za-zА-Яа-я])[xх×]\s*\d+|(?<!\d)\d+\s*[xх×](?![A-Za-zА-Яа-я])",
                       re.I)                                         # плечо «x10» / «10x»


def _nums(s: str) -> list[float]:
    for rx in (_PCT, _RMULT, _RATIO, _LEVERAGE):
        s = rx.sub(" ", s)
    return [float(x) for x in re.findall(_NUM, s)]


def _kind(ln: str) -> tuple[str, re.Match] | None:
    """Чья строка: ключевое слово, стоящее РАНЬШЕ всех («Entry Targets» — вход, «Stop
    Targets» — стоп, «Трейлинг-стоп … активация» — стоп, а не вход)."""
    best = None
    for kind, rx in _KINDS:
        m = rx.search(ln)
        if m and (best is None or m.start() < best[1].start()):
            best = (kind, m)
    return best


def _coin(t: str) -> str | None:
    for m in _PAIR.finditer(t):
        c = (m.group(1) or m.group(2)).upper()
        if c not in _NOT_COIN and not c.isdigit():
            return c
    for m in _COIN_KW.finditer(t):
        c = m.group(1).upper()
        if c not in _NOT_COIN and not c.isdigit():
            return c
    for m in _TAG.finditer(t):
        c = m.group(1).upper()
        if c not in _NOT_COIN and not c.isdigit():
            return c
    return None


def _side(t: str) -> str | None:
    lo, sh = _LONG.search(t), _SHORT.search(t)
    if lo and sh:
        return "LONG" if lo.start() < sh.start() else "SHORT"
    return "LONG" if lo else "SHORT" if sh else None


def parse_signal(text: str) -> ParsedSignal | None:
    """Монета, сторона, вход (одно число или зона), стоп, цели. Без монеты/стороны/стопа
    или с несогласованной геометрией (стоп не по ту сторону входа) -> None.

    Неоднозначные числа («81,500») разбираются в обоих прочтениях: основное — результат,
    другое — в .alternatives (выберет resolve_reading по цене рынка). Если основное не
    сходится геометрически, верно другое — оно и возвращается."""
    primary, other_text, unsure = _readings((text or "")[:MAX_TEXT_CHARS])
    sig = _parse_normalized(primary)
    other = _parse_normalized(other_text) if other_text is not None else None
    if sig is None:
        return other
    if other is not None and _levels(other) != _levels(sig):
        sig.alternatives = [other]
        sig.unsure = unsure
    return sig


def _levels(s: ParsedSignal) -> tuple:
    return s.entry_lo, s.entry_hi, s.stop, tuple(s.targets)


def _parse_normalized(t: str) -> ParsedSignal | None:
    coin, side = _coin(t), _side(t)
    if not coin or not side:
        return None
    lines = [ln.strip() for ln in t.splitlines()]
    entry: list[float] = []
    stop: float | None = None
    targets: list[float] = []
    market = bool(_MARKET.search(t))
    section = None                  # секция «заголовок:» + значения строками ниже
    entry_closed = False            # вход берём из ПЕРВОГО упоминания (с его строками)

    def add(kind: str, vals: list[float]) -> None:
        nonlocal stop
        if kind == "stop":
            if stop is None and vals:
                stop = vals[0]      # второй «стоп» («в безубыток после TP1») не в счёт
        elif kind == "tp":
            targets.extend(vals)
        elif not entry_closed:
            entry.extend(vals)

    for ln in lines:
        if not ln:
            continue
        if _SKIP_LINE.search(ln):
            section = None
            continue
        if k := _kind(ln):
            kind, m = k
            if section == "entry" and kind != "entry" and entry:
                entry_closed = True
            if kind == "entry" and entry and section != "entry":
                entry_closed = True
            add(kind, _nums(_INDEX.sub(" ", ln[m.end():])))
            section = kind
        elif section and _VALUE_LINE.fullmatch(ln):
            add(section, _nums(re.sub(_ITEM, "", ln)))
        else:
            if section == "entry" and entry:
                entry_closed = True
            section = None
    if stop is None:
        return None
    lo: float | None
    hi: float | None
    if entry:
        lo, hi = min(entry), max(entry)
    elif market:
        lo = hi = None
    else:
        return None
    # геометрия: стоп по свою сторону входа, цели — по другую
    if lo is not None and hi is not None:
        if side == "LONG" and not stop < lo:
            return None
        if side == "SHORT" and not stop > hi:
            return None
    if side == "LONG":
        targets = sorted({x for x in targets if x > (hi if hi is not None else stop)})
    else:
        targets = sorted({x for x in targets if x < (lo if lo is not None else stop)},
                         reverse=True)
    return ParsedSignal(coin, side, lo, hi, stop, targets[:10])


def _ref_price(bars: pd.DataFrame | None, posted: pd.Timestamp) -> float | None:
    """Цена рынка в момент поста: открытие первого бара после него (иначе последнее закрытие)."""
    if bars is None or bars.empty:
        return None
    after = bars[bars["dt"] >= posted]
    v = float(after["open"].iloc[0]) if not after.empty else float(bars["close"].iloc[-1])
    return v if v > 0 else None


def _near_market(s: ParsedSignal, ref: float) -> bool:
    """Вход (середина зоны) и стоп — в пределах ×2 от цены рынка: прочтения различаются в
    тысячу раз, так что граница с запасом и на широкий стоп, и на движение до поста."""
    levels = [s.stop]
    if s.entry_lo is not None and s.entry_hi is not None:
        levels.append((s.entry_lo + s.entry_hi) / 2)
    return all(0.5 <= x / ref <= 2.0 for x in levels)


def resolve_reading(sig: ParsedSignal, bars: pd.DataFrame | None,
                    posted: pd.Timestamp) -> ParsedSignal | None:
    """Выбор прочтения неоднозначных чисел по цене рынка в момент поста: подходит ровно одно
    — оно. Рынок не рассудил: основное, если его подтверждает сам пост, иначе None — честнее
    не проверять, чем проверить не тот сигнал. Без альтернатив — sig как есть."""
    if not sig.alternatives:
        return sig
    ref = _ref_price(bars, posted)
    if ref is not None:
        fits = [s for s in (sig, *sig.alternatives) if _near_market(s, ref)]
        if len(fits) == 1:
            return fits[0]
    return None if sig.unsure else sig


def render_unsure(sig: ParsedSignal) -> str:
    """Ответ, когда прочтение цен не решили ни пост, ни рынок (resolve_reading -> None)."""
    def level(s: ParsedSignal) -> str:
        return _price(s.entry_lo if s.entry_lo is not None else s.stop)
    alt = sig.alternatives[0] if sig.alternatives else sig
    return (f"🔎 Не понял формат цен в сигнале по {esc(sig.coin)}: уровень читается и как "
            f"<code>{level(sig)}</code>, и как <code>{level(alt)}</code> (запятая — это "
            "тысячи или дробь?), а цена монеты в момент поста не подсказала. Такой пост "
            "проверить не могу — перешлите другой сигнал.")


def pick_timeframe(age_s: float) -> str | None:
    """ТФ свечей под возраст поста: чем свежее, тем мельче (точнее порядок касаний)."""
    if age_s <= 7 * 86400:
        return "15m"
    if age_s <= 40 * 86400:
        return "1h"
    if age_s <= MAX_AGE_DAYS * 86400:
        return "4h"
    return None


@dataclass
class CheckResult:
    status: str                     # no_fill | stop | all_tp | open
    fill_price: float | None = None
    fill_time: pd.Timestamp | None = None
    hit: int = 0                    # сколько целей взято до стопа/сейчас
    end_time: pd.Timestamp | None = None
    mfe_r: float = 0.0
    mae_r: float = 0.0
    last_price: float | None = None
    ambiguous: bool = False         # стоп и новая цель в одном баре


def _r(sig: ParsedSignal, fill: float, price: float) -> float:
    risk = abs(fill - sig.stop)
    if risk <= 0:
        return 0.0
    return (price - fill) / risk if sig.side == "LONG" else (fill - price) / risk


def evaluate(sig: ParsedSignal, bars: pd.DataFrame, posted: pd.Timestamp) -> CheckResult:
    """Прогон сигнала по барам (колонки dt/open/high/low/close, dt — открытие бара, UTC)."""
    b = bars[bars["dt"] >= posted].reset_index(drop=True)
    if b.empty:
        return CheckResult("no_fill")
    long = sig.side == "LONG"
    fill = None
    i0 = 0
    o0 = float(b["open"].iloc[0])
    market = sig.entry_lo is None or sig.entry_hi is None
    e_lo = o0 if sig.entry_lo is None else sig.entry_lo
    e_hi = o0 if sig.entry_hi is None else sig.entry_hi
    # куда цене идти к входу: сверху вниз (лимитка лонга/стоп-ордер шорта) или снизу вверх
    from_above = not market and o0 > e_hi
    for i, row in b.iterrows():
        o, h, lo_ = float(row["open"]), float(row["high"]), float(row["low"])
        if market or (i == 0 and e_lo <= o0 <= e_hi):
            fill = o                                 # по рынку / пост вышел внутри зоны
        elif from_above and lo_ <= e_hi:
            fill = min(o, e_hi)
        elif not from_above and h >= e_lo:
            fill = max(o, e_lo)
        if fill is not None:
            i0 = i
            break
        # цена ушла за стоп, так и не коснувшись входа — сигнал отменился сам
        if (long and lo_ <= sig.stop) or (not long and h >= sig.stop):
            return CheckResult("no_fill", last_price=float(row["close"]))
    if fill is None:
        return CheckResult("no_fill", last_price=float(b["close"].iloc[-1]))
    res = CheckResult("open", fill_price=fill, fill_time=b["dt"].iloc[i0])
    for i in range(i0, len(b)):
        row = b.iloc[i]
        h, lo_ = float(row["high"]), float(row["low"])
        res.mfe_r = max(res.mfe_r, _r(sig, fill, h if long else lo_)) if i > i0 else res.mfe_r
        res.mae_r = min(res.mae_r, _r(sig, fill, lo_ if long else h))
        stop_hit = lo_ <= sig.stop if long else h >= sig.stop
        new_hit = res.hit
        if i > i0:
            while new_hit < len(sig.targets) and (
                    h >= sig.targets[new_hit] if long else lo_ <= sig.targets[new_hit]):
                new_hit += 1
        if stop_hit:
            res.ambiguous = new_hit > res.hit
            res.status, res.end_time = "stop", row["dt"]
            res.mae_r = -1.0
            return res
        res.hit = new_hit
        if sig.targets and res.hit == len(sig.targets):
            res.status, res.end_time = "all_tp", row["dt"]
            return res
    res.last_price = float(b["close"].iloc[-1])
    return res


def _fmt_dt(ts) -> str:
    return pd.Timestamp(ts).strftime("%d.%m.%Y %H:%M UTC")


def render_check(sig: ParsedSignal, res: CheckResult, posted, tf: str,
                 venue: str = "Bybit") -> str:
    """Итог проверки для лички (parse_mode=HTML)."""
    arrow = "🟢 LONG" if sig.side == "LONG" else "🔴 SHORT"
    lines = [f"🔎 <b>Проверка сигнала: {esc(sig.coin)} {arrow}</b>",
             f"Опубликован {_fmt_dt(posted)} · свечи {esc(venue)} {tf}", ""]
    if sig.entry_lo is None or sig.entry_hi is None:
        entry = "по рынку"
    elif sig.entry_lo == sig.entry_hi:
        entry = f"<code>{_price(sig.entry_lo)}</code>"
    else:
        entry = f"<code>{_price(sig.entry_lo)}</code> – <code>{_price(sig.entry_hi)}</code>"
    fill = res.fill_price
    if fill is not None:
        lines.append(f"Вход {entry} → исполнен {_fmt_dt(res.fill_time)} по "
                     f"<code>{_price(fill)}</code>")
    else:
        lines.append(f"Вход {entry}")
    lines.append(f"Стоп <code>{_price(sig.stop)}</code>")
    if sig.targets:
        parts = []
        for k, tp in enumerate(sig.targets):
            mark = "✅" if k < res.hit else "❌"
            rr = f" ({_r(sig, fill, tp):+.1f}R)" if fill is not None else ""
            parts.append(f"TP{k + 1} <code>{_price(tp)}</code> {mark}{rr}")
        lines.append("Цели: " + " · ".join(parts))
    lines.append("")
    if res.status == "no_fill":
        lines.append("Итог: <b>вход не исполнился</b> — цена не дошла до зоны входа "
                     "(или ушла за стоп раньше).")
    elif res.status == "all_tp":
        lines.append(f"Итог: <b>взяты все цели</b> ✅ ({_fmt_dt(res.end_time)}).")
    elif res.status == "stop":
        if res.hit:
            lines.append(f"Итог: взято целей — {res.hit}, затем цена вернулась "
                         f"<b>в стоп</b> ({_fmt_dt(res.end_time)}). Если стоп после первой "
                         "цели не переносили в безубыток — сделка закрылась по стопу.")
        else:
            lines.append(f"Итог: <b>стоп</b> до первой цели ❌ ({_fmt_dt(res.end_time)}), "
                         "−1R.")
    else:
        last = res.last_price if res.last_price is not None else fill
        now_r = _r(sig, fill, last) if fill is not None and last is not None else 0.0
        lines.append(f"Итог: <b>сделка ещё открыта</b>, целей взято — {res.hit}; сейчас "
                     f"{now_r:+.2f}R (цена <code>{_price(last or 0.0)}</code>).")
    if res.fill_price is not None:
        lines.append(f"Максимум в пользу: {res.mfe_r:+.1f}R · против: {res.mae_r:+.1f}R")
    if res.ambiguous:
        lines.append("⚠️ Стоп и цель задеты в одной свече — порядок внутри свечи неизвестен, "
                     "считаем, что первым был стоп.")
    lines += ["", "<i>RSI Range публикует итог каждой своей сделки автоматически, включая "
                  "минусы: /stats · журнал целиком: /trackrecord</i>"]
    return "\n".join(lines)


HELP = (
    "🔎 <b>Проверка любого сигнала</b>\n\n"
    "Перешлите сюда пост с сигналом из любого канала (именно «Переслать», не копировать — "
    "так бот знает время публикации). Бот по свечам Bybit покажет, что было на самом деле: "
    "исполнился ли вход, какие цели взяты, был ли стоп.\n\n"
    "Нужны в тексте: монета, LONG/SHORT, вход (или «по рынку») и стоп; цели — по желанию. "
    "Сигналы старше 180 дней не проверяются. Бесплатно, до 20 проверок в день.\n\n"
    "Или пришлите @username канала — проверю последние сигналы целиком (/audit).")
