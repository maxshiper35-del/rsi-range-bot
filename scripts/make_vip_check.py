"""Ролик 9:16 «Проверка VIP-канала»: последние N сигналов публичного канала прогоняются
чекером бота (signal_check.evaluate — те же правила, что у проверки пересланного поста)
по свечам Bybit → ✅ дошёл до цели / 🛑 стоп раньше цели / ⌛ вход не исполнился / ⏳ открыт
→ вердикт: какая доля дошла до первой цели, сколько стоит первая цель в R и что дало бы
закрытие на ней. CTA — проверить свой канал в боте.

АНОНИМНО: имя канала, аватар и текст постов не попадают ни в кадр, ни в подпись («Канал А,
~N подписчиков»); карточка сигнала перерисована нейтрально (монета, сторона, вход, стоп,
первая цель). Имя канала — только аргумент запуска: его нет ни в коде, ни в выходных
файлах, страж kit.check_public падает до рендера, если оно где-то просочилось.

Разбор постов: parse_signal бота + две поправки, найденные на живых постах (30.10.2026):
* «81,500» при точечных десятичных в тексте — разделитель тысяч (парсер читал 81 и 500);
* цели — из явных строк «TP1:/TP2:», если они есть: парсер цеплял числа из абзаца анализа
  («SOL previously hit our TP2 $115» становилось первой целью). Посты, где зона входа
  дальше 25% от цены биржи на момент поста, считаются нераспознанными и пропускаются.

  .venv/bin/python -m scripts.make_vip_check --channel <username> [--n 20] [--preview]

Свечи и страницы канала кэшируются в data/cache/vip/ (повторный рендер — без сети;
--refresh перепроверяет по свежим свечам).
"""
from __future__ import annotations

import argparse
import hashlib
import math
import re
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd

from app.delivery import signal_check
from app.delivery.signal_check import CheckResult, ParsedSignal
from scripts import shorts_kit as kit
from scripts.shorts_kit import G, R, esc, num, price, rr

OUT = Path("data/content/new_formats")
CACHE = Path("data/cache/vip")
LABEL = "Канал А"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")
_TP_LINE = re.compile(r"\bTP\s*([1-9])\s*[:：]\s*\$?\s*([0-9]+(?:\.[0-9]+)?)", re.I)
_TP_PCT = re.compile(r"\bTP\s*[1-9][^\n]*?\(\s*\+\s*([0-9]+(?:[.,][0-9]+)?)\s*%", re.I)
# «⏫ BUY : 81909-80271» в начале строки — зона входа без слова «entry» (шаблон 30.10)
_BUY_LINE = re.compile(r"(?im)^([^\w\n]*)(buy|sell)\s*:\s*(?=\d)")
_TF_S = {"15m": 900, "1h": 3600, "4h": 14400}
_SIDE = {"LONG": "лонг", "SHORT": "шорт"}
BIG_CARDS = 3

_CSS = """
.hk{flex:1;display:flex;flex-direction:column;justify-content:center}
.hk1{font-size:200px;font-weight:900;letter-spacing:-8px;line-height:.95}
.hk2{font-size:96px;font-weight:900;letter-spacing:-3px;line-height:1.03;margin-top:8px}
.hk3{font-size:92px;font-weight:900;letter-spacing:-3px;line-height:1.04;color:#F2B84B;
  margin-top:48px}
.who{margin-top:44px;align-self:flex-start;font-size:38px;font-weight:800;padding:16px 26px;
  border-radius:18px;background:rgba(255,255,255,.06);border:2px solid rgba(255,255,255,.12)}
.who span{color:#8A909C;font-weight:700}
.cl{flex:1;display:flex;flex-direction:column;justify-content:center}
.cl1{font-size:52px;font-weight:800;color:#8A909C}
.cl2{font-size:150px;font-weight:900;letter-spacing:-5px;color:#4ADE9C;line-height:1;
  margin-top:10px}
.cl3{font-size:66px;font-weight:900;letter-spacing:-2px;line-height:1.1;margin-top:46px}
.cl4{font-size:42px;font-weight:700;color:#C7CBD4;margin-top:22px;line-height:1.3}
.bigc{flex:1;display:flex;flex-direction:column;justify-content:center}
.card{border-radius:34px;padding:38px 40px;border:2px solid rgba(255,255,255,.12);
  background:linear-gradient(180deg,rgba(255,255,255,.06),rgba(255,255,255,.02))}
.c-top{display:flex;justify-content:space-between;align-items:center;font-size:36px;
  font-weight:800;color:#8A909C}
.c-coin{display:flex;align-items:center;gap:24px;margin-top:20px}
.c-coin b{font-size:160px;font-weight:900;letter-spacing:-5px;line-height:1}
.pill{font-size:44px;font-weight:900;padding:12px 24px;border-radius:16px;letter-spacing:.04em}
.pill.long{background:rgba(74,222,156,.14);color:#6FE3AC;border:2px solid rgba(74,222,156,.5)}
.pill.short{background:rgba(241,101,101,.13);color:#F58787;border:2px solid rgba(241,101,101,.5)}
.kv{display:flex;justify-content:space-between;align-items:baseline;font-size:56px;
  padding:18px 0;border-bottom:2px solid rgba(255,255,255,.07)}
.kv:last-child{border-bottom:none}.kv .k{color:#8A909C;font-weight:700}
.kv .v{font-weight:900;font-variant-numeric:tabular-nums}
.res{margin-top:34px;border-radius:28px;padding:30px 32px;font-size:76px;font-weight:900;
  display:flex;align-items:center;gap:22px;min-height:190px}
.res small{display:block;font-size:36px;font-weight:700;color:#C7CBD4;margin-top:6px}
.res.tp{background:rgba(74,222,156,.12);border:3px solid rgba(74,222,156,.6);color:#4ADE9C}
.res.stop{background:rgba(241,101,101,.12);border:3px solid rgba(241,101,101,.6);color:#F16565}
.res.nofill{background:rgba(242,184,75,.10);border:3px solid rgba(242,184,75,.55);color:#F2B84B}
.res.open{background:rgba(255,255,255,.06);border:3px solid rgba(255,255,255,.2);color:#C7CBD4}
.res.wait{background:rgba(255,255,255,.04);border:3px dashed rgba(255,255,255,.18);color:#6B7280}
.tally{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:26px}
.tl{border-radius:20px;padding:12px 0;text-align:center;font-size:44px;font-weight:900;
  background:rgba(255,255,255,.04);border:2px solid rgba(255,255,255,.08)}
.tl small{display:block;font-size:24px;font-weight:800;color:#8A909C;letter-spacing:.04em}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:22px}
.mc{height:186px;border-radius:22px;padding:16px 16px 14px;display:flex;flex-direction:column;
  justify-content:space-between;background:rgba(255,255,255,.035);
  border:2px solid rgba(255,255,255,.08)}
.mc .m1{display:flex;justify-content:space-between;align-items:baseline}
.mc .m1 b{font-size:40px;font-weight:900;letter-spacing:-.5px;white-space:nowrap}
.mc .m1 i{font-style:normal;font-size:34px;font-weight:900}
.mc .md{font-size:26px;font-weight:700;color:#8A909C}
.mc .mr{border-radius:14px;padding:8px 0;text-align:center;font-size:29px;font-weight:900;
  white-space:nowrap}
.mc.tp{border-color:rgba(74,222,156,.55)}.mc.tp .mr{background:rgba(74,222,156,.16);color:#4ADE9C}
.mc.stop{border-color:rgba(241,101,101,.5)}.mc.stop .mr{background:rgba(241,101,101,.15);
  color:#F16565}
.mc.nofill .mr{background:rgba(242,184,75,.13);color:#F2B84B}
.mc.open .mr{background:rgba(255,255,255,.08);color:#C7CBD4}
.mc.wait .mr{background:rgba(255,255,255,.05);color:#5F6571}
.mc.new{box-shadow:0 0 0 4px rgba(242,184,75,.6)}
.vd{flex:1;display:flex;flex-direction:column;justify-content:center}
.vd1{font-size:44px;font-weight:850;letter-spacing:.1em;color:#8A909C}
.vd2{font-size:230px;font-weight:900;letter-spacing:-9px;line-height:.95;margin-top:10px;
  font-variant-numeric:tabular-nums}
.vd3{font-size:62px;font-weight:900;letter-spacing:-1.5px;margin-top:8px}
.vrows{display:flex;flex-direction:column;gap:14px;margin-top:46px}
.vr{display:flex;justify-content:space-between;align-items:center;height:96px;border-radius:22px;
  padding:0 28px;font-size:42px;font-weight:800;background:rgba(255,255,255,.04);
  border:2px solid rgba(255,255,255,.08)}
.vr b{font-size:52px;font-variant-numeric:tabular-nums}
.m-h{font-size:80px;font-weight:900;letter-spacing:-2.5px;line-height:1.06}
.m-h em{font-style:normal;color:#F2B84B}
.m-p{font-size:48px;font-weight:700;color:#C7CBD4;line-height:1.3;margin-top:30px}
.m-p b{color:#EDEFF3}
.bars{display:flex;flex-direction:column;gap:22px;margin-top:44px}
.bl .bt{font-size:38px;font-weight:800;color:#C7CBD4}
.bl .bk{display:flex;align-items:center;gap:18px;margin-top:10px}
.bl .tr{flex:1;height:56px;border-radius:16px;background:rgba(255,255,255,.06);overflow:hidden;
  position:relative}
.bl .fl{position:absolute;left:0;top:0;bottom:0;border-radius:16px}
.bl .bv{width:150px;text-align:right;font-size:56px;font-weight:900;
  font-variant-numeric:tabular-nums}
.sumbox{margin-top:46px;border-radius:26px;padding:26px 30px;border:3px solid rgba(241,101,101,.5);
  background:rgba(241,101,101,.08)}
.sumbox .l{font-size:38px;font-weight:800;color:#C7CBD4}
.sumbox .n{font-size:92px;font-weight:900;letter-spacing:-3px;font-variant-numeric:tabular-nums}
.sumbox small{font-size:34px;font-weight:700;color:#8A909C}
"""


@dataclass(frozen=True)
class Post:
    post: str                        # «канал/номер» — только для кэша и отчёта, не в кадр
    dt: pd.Timestamp
    text: str


class _Page(HTMLParser):
    """Публичная веб-версия t.me/s/<канал>: посты (текст, время), заголовок, подписчики."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.posts: list[dict] = []
        self.title: str | None = None
        self.subs: str | None = None
        self._cur: dict | None = None
        self._depth = 0
        self._in_text = False
        self._sub_on = self._title_on = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "div" and "tgme_widget_message " in cls + " " and a.get("data-post"):
            self._cur = {"post": a["data-post"], "text": "", "dt": None}
            self.posts.append(self._cur)
        if self._in_text:
            if tag == "div":
                self._depth += 1
            if tag == "br" and self._cur is not None:
                self._cur["text"] += "\n"
            return
        if (tag == "div" and "js-message_text" in cls and "reply" not in cls
                and self._cur is not None and not self._cur["text"]):
            self._in_text, self._depth = True, 1
        if tag == "time" and self._cur is not None and a.get("datetime") and not self._cur["dt"]:
            self._cur["dt"] = a["datetime"]
        if tag == "span" and cls == "counter_value" and self.subs is None:
            self._sub_on = True
        if tag == "div" and "tgme_channel_info_header_title" in cls:
            self._title_on = True

    def handle_endtag(self, tag: str) -> None:
        if self._in_text and tag == "div":
            self._depth -= 1
            if self._depth == 0:
                self._in_text = False
        if tag == "span":
            self._sub_on = False
        if tag == "div":
            self._title_on = False

    def handle_data(self, data: str) -> None:
        if self._in_text and self._cur is not None:
            self._cur["text"] += data
        if self._sub_on and self.subs is None and data.strip():
            self.subs = data.strip()
        if self._title_on and self.title is None and data.strip():
            self.title = data.strip()


def parse_page(html: str) -> tuple[list[Post], str | None, str | None]:
    p = _Page()
    p.feed(html)
    posts = [Post(x["post"], pd.Timestamp(x["dt"]).tz_convert("UTC"), x["text"])
             for x in p.posts if x["dt"]]
    return posts, p.title, p.subs


def subs_ru(raw: str | None) -> str:
    """«35.4K» → «~35 тыс.», «1.2M» → «~1,2 млн», «980» → «~980» (2 значащие цифры)."""
    if not raw:
        return ""
    m = re.fullmatch(r"([0-9]+(?:[.,][0-9]+)?)\s*([KkMm]?)", raw.strip())
    if not m:
        return ""
    v = float(m.group(1).replace(",", ".")) * {"": 1, "k": 1e3, "m": 1e6}[m.group(2).lower()]
    if v >= 1e6:
        return f"~{num(round(v / 1e6, 1), 1).replace(',0', '')} млн"
    if v >= 1e3:
        k = v / 1e3
        k = round(k, -int(math.floor(math.log10(k))) + 1) if k >= 10 else round(k)
        return f"~{num(k)} тыс."
    return f"~{num(v)}"


def prep_text(text: str) -> str:
    """Поправки разбора: «81,500» — тысячи, если в тексте есть точечные десятичные;
    строка «⏫ BUY : 0.077-0.075» — зона входа (слово «entry» дописывается, сторона остаётся)."""
    t = text.replace(" ", " ")
    if re.search(r"\d\.\d", t):
        t = re.sub(r"(?<![\d.])(\d{1,3}),(\d{3})(?!\d)", r"\1\2", t)
    return _BUY_LINE.sub(r"\1\2 entry: ", t)


def explicit_targets(text: str) -> list[float]:
    """Цели из строк «TP1: 1.23», «TP 2: $4.5» по номеру (первое упоминание номера)."""
    tps: dict[int, float] = {}
    for m in _TP_LINE.finditer(text):
        tps.setdefault(int(m.group(1)), float(m.group(2)))
    return [tps[k] for k in sorted(tps)]


def parse_post(text: str) -> ParsedSignal | None:
    t = prep_text(text)
    sig = signal_check.parse_signal(t)
    if sig is None or sig.entry_lo is None or sig.entry_hi is None:
        return None                          # «по рынку» без цены не проверяем — нет якоря
    tl = explicit_targets(t)
    if tl:
        side_ok = [v for v in tl if (v > sig.entry_hi if sig.side == "LONG" else v < sig.entry_lo)]
        sig.targets = side_ok
    return sig if sig.targets else None


def claimed_pct(texts: list[str]) -> float | None:
    """Самый большой «+NN%» в строках целей — то, что пост обещает словами."""
    vals = [float(m.group(1).replace(",", ".")) for t in texts for m in _TP_PCT.finditer(t)]
    return max(vals) if vals else None


@dataclass
class Checked:
    post: Post
    sig: ParsedSignal
    tf: str
    res: CheckResult
    ref: float                               # цена биржи на момент поста (open 1-го бара)

    @property
    def kind(self) -> str:
        if self.res.hit >= 1:
            return "tp"
        return {"stop": "stop", "no_fill": "nofill"}.get(self.res.status, "open")

    @property
    def tp1_r(self) -> float | None:
        if self.res.fill_price is None:
            return None
        return signal_check._r(self.sig, self.res.fill_price, self.sig.targets[0])


def plausible(sig: ParsedSignal, ref: float) -> bool:
    """Зона входа рядом с ценой биржи, стоп не дальше 30% — иначе разбор ошибся."""
    lo, hi = sig.entry_lo or ref, sig.entry_hi or ref
    mid = (lo + hi) / 2
    return abs(mid / ref - 1) <= 0.25 and abs(sig.stop / mid - 1) <= 0.30


def fetch_pages(channel: str, pages: int,
                refresh: bool) -> tuple[list[Post], str | None, str | None]:
    """Публичные страницы t.me/s/<канал> (кэш под хэшем имени: в путях имени нет)."""
    d = CACHE / hashlib.sha1(channel.lower().encode()).hexdigest()[:12]
    d.mkdir(parents=True, exist_ok=True)
    posts: dict[str, Post] = {}
    title = subs = None
    before: int | None = None
    for _ in range(pages):
        path = d / f"page_{before or 'latest'}.html"
        if path.exists() and not refresh:
            html = path.read_text(encoding="utf-8")
        else:
            url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                html = r.read().decode("utf-8", "replace")
            path.write_text(html, encoding="utf-8")
            time.sleep(1.0)
        got, t, s = parse_page(html)
        title, subs = title or t, subs or s
        if not got:
            break
        for p in got:
            posts[p.post] = p
        before = min(int(p.post.rsplit("/", 1)[1]) for p in got)
    return sorted(posts.values(), key=lambda p: -int(p.post.rsplit("/", 1)[1])), title, subs


def _bars(sig: ParsedSignal, posted: pd.Timestamp, refresh: bool) -> tuple[pd.DataFrame, str]:
    """Свечи Bybit от поста до сейчас — как у бота (_run_check): ТФ по возрасту поста,
    1000-лоты для мелких монет. Кэш по (символ, ТФ, пост)."""
    from app.data.market_data import _EXCHANGE_LOCK, _exchange, fetch_history

    age = (datetime.now(timezone.utc) - posted.to_pydatetime()).total_seconds()
    tf = signal_check.pick_timeframe(age)
    if tf is None:
        raise LookupError("старше 180 дней")
    ex = _exchange("bybit")
    if not ex.markets:
        with _EXCHANGE_LOCK:
            ex.load_markets()
    sym, scale = sig.symbol, 1.0
    if sym not in ex.markets:
        if "1000" + sym not in ex.markets:
            raise LookupError(f"{sig.coin} нет на Bybit")
        sym, scale = "1000" + sym, 1000.0
    path = CACHE / f"bars_{sym.split(':')[0].replace('/', '')}_{tf}_{posted:%Y%m%d%H%M}.csv"
    if path.exists() and not refresh:
        bars = pd.read_csv(path)
        bars["dt"] = pd.to_datetime(bars["dt"], utc=True)
    else:
        bars = fetch_history(sym, tf, int(age // _TF_S[tf]) + 3, exchange_id="bybit")
        CACHE.mkdir(parents=True, exist_ok=True)
        bars.to_csv(path, index=False)
        time.sleep(0.3)
    if scale != 1.0:
        bars[["open", "high", "low", "close"]] /= scale
    return bars, tf


def check_signals(posts: list[Post], n: int, refresh: bool = False) -> tuple[list[Checked], list]:
    """Последние n распознанных сигналов (свежие первыми) → прогон чекером бота."""
    out: list[Checked] = []
    skipped: list = []
    seen: set = set()
    for p in posts:
        if len(out) >= n:
            break
        sig = parse_post(p.text)
        if sig is None:
            continue
        key = (sig.coin, sig.side, sig.entry_lo, sig.entry_hi, sig.stop)
        if key in seen:
            continue
        seen.add(key)
        try:
            bars, tf = _bars(sig, p.dt, refresh)
        except LookupError as e:
            skipped.append((p.post, str(e)))
            continue
        after = bars[bars["dt"] >= p.dt]
        if after.empty:
            skipped.append((p.post, "нет свечей после поста"))
            continue
        ref = float(after["open"].iloc[0])
        if not plausible(sig, ref):
            skipped.append((p.post, f"не распознан: зона {sig.entry_lo}–{sig.entry_hi} "
                                    f"при цене {ref}"))
            continue
        out.append(Checked(p, sig, tf, signal_check.evaluate(sig, bars, p.dt), ref))
    return out, skipped


@dataclass(frozen=True)
class Verdict:
    n: int
    tp: int
    stop: int
    nofill: int
    open: int
    tp1_mean: float                          # средняя первая цель в R (по сработавшим)
    hit_rate: float                          # доля целей среди закрытых исполненных
    breakeven: float                         # нужная доля попаданий при выходе на TP1
    at_tp1: float                            # сумма R, если закрывать всё на TP1


def verdict(rows: list[Checked]) -> Verdict:
    tp = [r for r in rows if r.kind == "tp"]
    stop = [r for r in rows if r.kind == "stop"]
    # средняя первая цель — у СРАБОТАВШИХ: тогда «нужно для нуля» и «закрывать всё на TP1»
    # не противоречат друг другу (по всем сигналам вышло «было 58,8% < нужно 59,4%» рядом
    # с «+0,5R» — короткие цели срабатывают чаще длинных)
    tp1 = [r.tp1_r for r in tp if r.tp1_r is not None] or \
        [r.tp1_r for r in rows if r.tp1_r is not None]
    mean = sum(tp1) / len(tp1) if tp1 else 0.0
    resolved = len(tp) + len(stop)
    return Verdict(
        n=len(rows), tp=len(tp), stop=len(stop),
        nofill=sum(1 for r in rows if r.kind == "nofill"),
        open=sum(1 for r in rows if r.kind == "open"),
        tp1_mean=mean, hit_rate=len(tp) / resolved if resolved else 0.0,
        breakeven=1 / (1 + mean) if mean > 0 else 1.0,
        at_tp1=sum(r.tp1_r or 0.0 for r in tp) - len(stop))


def _pct_digits(v: Verdict) -> int:
    """Знаков у «нужно / было»: один, если в целых процентах они совпали бы."""
    return 1 if round(v.breakeven * 100) == round(v.hit_rate * 100) else 0


_KIND = {"tp": ("✅", "тейк"), "stop": ("🛑", "стоп"), "nofill": ("⌛", "нет входа"),
         "open": ("🔄", "открыт"), "wait": ("", "?")}


def _ago(a: pd.Timestamp, b: pd.Timestamp) -> str:
    h = (b - a).total_seconds() / 3600
    if h < 1:
        return "меньше чем через час"
    if h < 48:
        return f"через {kit.plural(round(h), 'час', 'часа', 'часов')}"
    return f"через {kit.plural(round(h / 24), 'день', 'дня', 'дней')}"


def _entry(sig: ParsedSignal) -> str:
    lo, hi = sig.entry_lo or 0.0, sig.entry_hi or 0.0
    return price(lo) if lo == hi else f"{price(lo)}–{price(hi)}"


def _result_line(r: Checked) -> str:
    k = r.kind
    if k == "tp":
        sub = "первая цель взята" if r.res.status != "all_tp" else "все цели взяты"
        if r.res.status == "stop":
            sub = "цель взята, потом цена ушла в стоп"
        return f"<div class='res tp'>✅<div>Тейк<small>{sub}</small></div></div>"
    if k == "stop":
        when = _ago(r.post.dt, r.res.end_time) if r.res.end_time is not None else ""
        return (f"<div class='res stop'>🛑<div>Стоп<small>{when} после поста, до цели не дошёл"
                "</small></div></div>")
    if k == "nofill":
        return ("<div class='res nofill'>⌛<div>Не исполнился<small>цена не дошла до входа"
                "</small></div></div>")
    return "<div class='res open'>🔄<div>Ещё открыт<small>ни цели, ни стопа</small></div></div>"


def frames(rows: list[Checked], v: Verdict, subs: str, claim: float | None) -> kit.Timeline:
    tl = kit.Timeline()
    tag = "По свечам Bybit"
    who = f"<div class='who'>{LABEL} <span>· {subs} подписчиков</span></div>" if subs else ""

    # 1. крючок
    hook = (kit.brand_row(tag) +
            f"<div class='hk'><div class='kick'>ПРОВЕРКА VIP-КАНАЛА</div>"
            f"<div class='hk1'>{v.n}</div><div class='hk2'>«VIP-сигналов»</div>"
            "<div class='hk3'>Сколько дошли до&nbsp;цели?</div>"
            f"{who}</div>")
    tl.add(kit.page(hook, _CSS), 2.0, "tick")

    # 2. что обещают — дословная цифра из постов
    if claim:
        body = (kit.brand_row(tag) +
                "<div class='cl'><div class='cl1'>В постах — цели до</div>"
                f"<div class='cl2'>+{num(claim)}%</div>"
                "<div class='cl3'>Проверим каждый сигнал по свечам биржи</div>"
                "<div class='cl4'>Bybit, правила как у живой сделки: вход, стоп, цель — "
                "что случилось раньше.</div></div>")
        tl.add(kit.page(body, _CSS), 2.0, "tick")

    # 3. первые карточки крупно: что обещал пост → что было на бирже
    def big(r: Checked, i: int, shown: bool) -> str:
        s = r.sig
        res = _result_line(r) if shown else ("<div class='res wait'>…<div>Проверяю по свечам"
                                             "<small>Bybit</small></div></div>")
        body = (kit.brand_row(tag) +
                f"<div class='bigc'><div class='card'><div class='c-top'>"
                f"<span>Сигнал {i + 1} из {v.n}</span>"
                f"<span>{r.post.dt:%d.%m · %H:%M} UTC</span></div>"
                f"<div class='c-coin'><b>{esc(s.coin)}</b><span class='pill {s.side.lower()}'>"
                f"{_SIDE[s.side].upper()}</span></div>"
                f"<div style='margin-top:18px'><div class='kv'><span class='k'>Вход</span>"
                f"<span class='v'>{_entry(s)}</span></div><div class='kv'><span class='k'>Стоп"
                f"</span><span class='v neg'>{price(s.stop)}</span></div><div class='kv'>"
                f"<span class='k'>Цель 1</span><span class='v pos'>{price(s.targets[0])}</span>"
                f"</div></div></div>{res}</div>")
        return kit.page(body, _CSS)

    for i, r in enumerate(rows[:BIG_CARDS]):
        tl.add(big(r, i, False), 0.8)
        cue = {"tp": "tw", "stop": "tl"}.get(r.kind, "tick")
        tl.add(big(r, i, True), 1.3, cue)

    # 4. сетка: остальные переворачиваются по одной, счёт растёт
    def grid(k: int) -> str:
        cnt = {"tp": 0, "stop": 0, "nofill": 0, "open": 0}
        for r in rows[:k]:
            cnt[r.kind] += 1
        tally = "".join(f"<div class='tl'>{_KIND[x][0]} {cnt[x]}<small>{_KIND[x][1].upper()}"
                        f"</small></div>" for x in ("tp", "stop", "nofill", "open"))
        cells = []
        for i, r in enumerate(rows):
            kind = r.kind if i < k else "wait"
            arrow = ("▲", G) if r.sig.side == "LONG" else ("▼", R)
            icon, word = _KIND[kind]
            if kind == "nofill":                       # в узкой карточке — без значка
                icon = ""
            new = " new" if i == k - 1 and k > BIG_CARDS else ""
            cells.append(f"<div class='mc {kind}{new}'><div class='m1'><b>{esc(r.sig.coin)}</b>"
                         f"<i style='color:{arrow[1]}'>{arrow[0]}</i></div>"
                         f"<div class='md'>{r.post.dt:%d.%m}</div>"
                         f"<div class='mr'>{icon} {word}</div></div>")
        body = (kit.brand_row(tag) + f"<div class='tally'>{tally}</div>"
                f"<div class='grid'>{''.join(cells)}</div>")
        return kit.page(body, _CSS)

    tl.add(grid(min(BIG_CARDS, len(rows))), 0.7, "tick")
    per = min(0.55, max(0.35, 7.5 / max(1, len(rows) - BIG_CARDS)))
    for k in range(BIG_CARDS + 1, len(rows) + 1):
        kind = rows[k - 1].kind
        tl.add(grid(k), per, {"tp": "tw", "stop": "tl"}.get(kind, "tick"))
    tl.add(grid(len(rows)), 1.0)

    # 5. вердикт: доля целей
    body = (kit.brand_row(tag) +
            f"<div class='vd'><div class='vd1'>ДО ПЕРВОЙ ЦЕЛИ ДОШЛИ</div>"
            f"<div class='vd2'><span class='pos'>{v.tp}</span> из {v.n}</div>"
            f"<div class='vd3'>{num(v.tp / v.n * 100 if v.n else 0)}% сигналов</div>"
            "<div class='vrows'>"
            f"<div class='vr'><span>🛑 стоп раньше цели</span><b class='neg'>{v.stop}</b></div>"
            f"<div class='vr'><span>⌛ вход не исполнился</span><b class='amb'>{v.nofill}</b></div>"
            f"<div class='vr'><span>🔄 ещё открыт</span><b class='mut'>{v.open}</b></div>"
            "</div></div>")
    tl.add(kit.page(body, _CSS, glow="rgba(241,101,101,.12)"), 2.8,
           "loss" if v.hit_rate < v.breakeven else "win")

    # 6. математика: сколько нужно попаданий при такой цели — и сколько было
    need, got = v.breakeven * 100, v.hit_rate * 100
    dg = _pct_digits(v)                 # 58,8% против 59,4% не должны выглядеть как 59 и 59
    body = (kit.brand_row(tag) +
            "<div class='vd'><div class='m-h'>Первая цель — "
            f"<em>{rr(v.tp1_mean)}</em></div>"
            f"<div class='m-p'>Сработавшая цель давала в среднем <b>{num(v.tp1_mean, 2)} $</b> "
            "на <b>1 $</b> риска. Чтобы не терять, она должна срабатывать хотя бы:</div>"
            "<div class='bars'>"
            f"<div class='bl'><div class='bt'>нужно для нуля</div><div class='bk'><div class='tr'>"
            f"<div class='fl' style='width:{need:.0f}%;background:#F2B84B'></div></div>"
            f"<div class='bv amb'>{num(need, dg)}%</div></div></div>"
            f"<div class='bl'><div class='bt'>было (из {v.tp + v.stop} закрытых)</div>"
            f"<div class='bk'><div class='tr'><div class='fl' style='width:{got:.0f}%;"
            f"background:{G if got >= need else R}'></div></div>"
            f"<div class='bv {'pos' if got >= need else 'neg'}'>{num(got, dg)}%</div></div></div>"
            "</div>"
            f"<div class='sumbox'><div class='l'>Закрывать всё на первой цели:</div>"
            f"<div class='n {'pos' if v.at_tp1 > 0 else 'neg'}'>{rr(v.at_tp1, 1)}</div>"
            f"<small>≈ {num(v.at_tp1, 1, sign=True)}% депозита при риске 1% · до комиссий</small>"
            "</div></div>")
    tl.add(kit.page(body, _CSS), 4.2, "tick")

    # 7. CTA
    end = kit.end_card("Проверь <em>свой</em> канал",
                       "Перешли пост с сигналом боту — он сверит его со свечами биржи: вход, "
                       "стоп, цели. Бесплатно.",
                       "Перешли сигнал сюда:",
                       "Канал не называем. Свечи Bybit 15m/1h; если стоп и цель в одной свече — "
                       "считаем стоп. R — в долях риска, до комиссий.")
    tl.add(kit.page(end, _CSS), 3.4, "tick")
    return tl


def caption(v: Verdict, subs: str, claim: float | None) -> kit.Caption:
    claim_txt = f" В постах — цели до +{num(claim)}%." if claim else ""
    return kit.Caption(
        title=f"Проверил {v.n} «VIP-сигналов» по бирже: до цели дошли {v.tp} #крипта #сигналы",
        hook=f"{v.n} «VIP-сигналов». Сколько дошли до цели?",
        description=(
            f"Взял последние {v.n} сигналов публичного «VIP»-канала ({subs} подписчиков, "
            f"название не публикуем).{claim_txt} Каждый прогнал по свечам Bybit по тем же "
            f"правилам, что у проверки сигналов в боте: до первой цели дошли {v.tp}, стоп "
            "раньше цели — "
            f"{v.stop}, вход не исполнился — {v.nofill}, ещё открыт — {v.open}. Сработавшая "
            f"первая цель давала в среднем {rr(v.tp1_mean)}: чтобы не терять, ей нужно "
            "срабатывать в "
            f"{num(v.breakeven * 100, _pct_digits(v))}% случаев, а сработала в "
            f"{num(v.hit_rate * 100, _pct_digits(v))}% "
            "закрытых. Проверь свой канал: перешли сигнал боту "
            f"{kit.BOT} — бесплатно. {kit.NOT_ADVICE}."),
        hashtags="#крипта #сигналы #трейдинг #проверка #криптосигналы",
    )


def handles(texts: list[str]) -> list[str]:
    """Ники и подписи-источники из постов — в стоп-лист анонимизации."""
    out = set()
    for t in texts:
        out.update(re.findall(r"@([A-Za-z0-9_]{4,})", t))
        out.update(re.findall(r"☆\s*([A-Za-z0-9_]{4,})", t))
    return sorted(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--channel", required=True, help="username публичного канала (не в кадр)")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--pages", type=int, default=4)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--refresh", action="store_true", help="заново скачать страницы и свечи")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--key", help="имя файла без расширения (по умолчанию proverka_kanala_<дата>;"
                                  " два канала с постами одного дня иначе перезапишут друг друга)")
    a = ap.parse_args()
    posts, title, subs_raw = fetch_pages(a.channel, a.pages, a.refresh)
    rows, skipped = check_signals(posts, a.n, a.refresh)
    if len(rows) < 10:
        raise SystemExit(f"распознано только {len(rows)} сигналов — мало для вывода")
    v = verdict(rows)
    subs = subs_ru(subs_raw)
    claim = claimed_pct([r.post.text for r in rows])
    for r in rows:
        print(f"  {r.post.dt:%d.%m %H:%M} {r.sig.coin} {r.sig.side} {_entry(r.sig)} "
              f"SL {r.sig.stop} TP1 {r.sig.targets[0]} [{r.tf}] → {r.kind} "
              f"({r.res.status}, hit {r.res.hit}, TP1 {r.tp1_r and round(r.tp1_r, 2)}R)")
    for s in skipped:
        print("  пропущен:", s[1])
    print(f"итог: {v}")
    tl = frames(rows, v, subs, claim)
    key = a.key or f"proverka_kanala_{rows[0].post.dt:%Y%m%d}"
    out = Path(a.out)
    forbid = [a.channel, title or ""] + handles([p.text for p in posts])
    if a.preview:
        n = len(tl.frames)
        picks = [0, 1, 2, 3, 2 * BIG_CARDS + 2, n - 5, n - 4, n - 3, n - 2, n - 1]
        kit.check_public((kit.visible_text(tl.frames[i][0]) for i in range(n)), forbid)
        for p in kit.snapshot([tl.frames[i][0] for i in picks], out / "preview", key):
            print(p)
        return
    path = kit.render(tl, out, key, seed=33, caption=caption(v, subs, claim),
                      extra_forbidden=forbid)
    print(f"{path} — {path.stat().st_size // 1024} КБ, {tl.t:.1f} с", flush=True)


if __name__ == "__main__":
    main()
