"""Проверка сигнального канала целиком: юзер присылает @username — бот читает последние посты
публичного веб-превью t.me/s/<username>, разбирает сигналы и прогоняет каждый по свечам биржи
тем же судьёй, что и проверка пересланного поста (signal_check.evaluate).

Почему (данные 29.10.2026): с рекламы «перешли сигнал — бот проверит» пришли 84 человека, и не
переслал никто — найти пост и переслать слишком долго. Набрать @канал — секунда, а итог по
двум-трём десяткам сигналов говорит о канале больше, чем один пост.

Чистые функции (разбор страницы, классификация постов, агрегат, тексты, HTML карточки) — для
тестов; сеть (t.me и свечи биржи) — в боте через asyncio.to_thread.

Честность аудита:
* уровни — ровно как в посте, время — публикации (<time datetime>); правки задним числом
  веб-превью не показывает — об этом сказано в итоге;
* «без стопа» — только посты с монетой и явным LONG/SHORT без слова «стоп» и без признаков
  отчёта или превью («ТП 1», «результат», «вход исполнен») и не рядом (±сутки) с постом со
  стопом по той же монете: лучше недосчитать, чем зря обвинить канал;
* повтор того же сигнала (тот же вход и стоп) считается один раз;
* итог «на первой цели»: вся позиция закрывается на TP1 — TP1 раньше стопа -> +R(TP1) от цены
  исполнения, стоп раньше -> −1R; не исполнившиеся и открытые без TP1 в итог не входят.
"""
from __future__ import annotations

import http.client
import re
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser

import pandas as pd

from app.delivery import signal_check
from app.delivery.signal_check import ParsedSignal
from app.delivery.templates import esc

MAX_POSTS = 60                  # последних постов на аудит (~3 страницы веб-превью)
MAX_PAGES = 4
MAX_SYMBOLS = 30                # монет на аудит: каждая — 1–2 запроса к бирже на общем IP бота
AUDITS_PER_DAY = 3
TIMEOUT_S = 15.0                # на страницу t.me
PAGES_BUDGET_S = 20.0           # на листание назад: дальше — аудит по тому, что успели прочесть
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_TF_S = {"15m": 900, "1h": 3600, "4h": 14400}

# @name · t.me/name · https://t.me/s/name · t.me/name/123 (ссылка на пост — тоже канал).
# Приватные ссылки (t.me/+…, t.me/joinchat/…, t.me/c/…) сюда не проходят: превью у них нет.
CHANNEL_REF = re.compile(
    r"^\s*(?:@|(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(?:s/)?)"
    r"([A-Za-z][A-Za-z0-9_]{3,31})(?:/\d+)?/?(?:\?\S*)?\s*$", re.I)
_RESERVED = frozenset({"joinchat", "addstickers", "addemoji", "addlist", "addtheme", "proxy",
                       "socks", "share", "login", "boost", "invoice", "contact", "giftcode",
                       "setlanguage", "confirmphone"})

# сторона «призывом»: английское слово — в сигнальном написании (LONG/Long); строчное «long»
# в разборах рынка («short squeeze») и «Short Term» / «Long-term» — не призыв
_SIDE_CALL = re.compile(r"\b(?:LONG|SHORT|Long|Short)\b(?![- ]?(?i:term))|(?i:лонг|шорт)")
# отчёт/апдейт/превью, а не новый сигнал: «ТП 1», «Target 2 ✅» (номер цели без цены),
# «результат», «вход исполнен», «LONG UPDATE», «+35%», «превью … по подписке»
_REPORT = re.compile(
    r"результат|исполнен|достиг|взят[аоы]?\b|выбил|выбит|закрыт[аоы]?\b|закрыл|отработ"
    r"|(?<!тейк-)(?<!тейк )профит|превью|по подписке|для подписчиков|апдейт|обновлени|🔒"
    r"|\bresult|\bfilled|reached|achieved|\bhit\b|\bdone\b|\bclosed|booked|\bupdate"
    r"|(?<!take-)(?<!take )(?<!take)profit|preview|subscribers"
    r"|\+\s?\d+(?:[.,]\d+)?\s?%"
    r"|\b(?:tp|тп|target|цель|тейк)\s?\d\b(?!\s*[:=).\-]?\s*\$?\d)", re.I)
_URL = re.compile(r"(?:https?://|\bt\.me/)\S+", re.I)     # «chart/USDT.D» в ссылке — не монета
_SHORT_CALL = 280               # «#UAI LONG» — призыв; длинный текст без входа — разбор рынка
# «Short #ICNT Now 50X», а через 4 минуты — полный сигнал со стопом (CoingraphSignals, 29.10):
# быстрый призыв рядом с постом со стопом по той же монете — не «без стопа»
_PAIR_WINDOW_S = 24 * 3600


def parse_channel_ref(text: str | None, bare: bool = False) -> str | None:
    """username канала из «@name» / «t.me/name» / «https://t.me/s/name» или None.
    bare=True — ещё и голое «name»: после /audit оно однозначно, а в обычном сообщении
    любое слово походило бы на канал."""
    t = (text or "").strip()
    m = CHANNEL_REF.match(t) or (CHANNEL_REF.match("@" + t) if bare else None)
    if m is None or m.group(1).lower() in _RESERVED:
        return None
    return m.group(1)


@dataclass
class Post:
    channel: str
    post_id: int
    posted: pd.Timestamp            # UTC, время публикации
    text: str

    @property
    def url(self) -> str:
        return f"https://t.me/{self.channel}/{self.post_id}"


@dataclass
class TmePage:
    title: str | None
    posts: list[Post] = field(default_factory=list)
    before: int | None = None       # ?before= для более старой страницы; None — старше нет


class _TmeParser(HTMLParser):
    """Однопроходный разбор t.me/s: пост — div.tgme_widget_message[data-post]; текст —
    div.tgme_widget_message_text, но НЕ цитата ответа (a.tgme_widget_message_reply /
    js-message_reply_text: там чужой текст, разбор принял бы его за новый сигнал); время —
    <time> в ссылке-дате поста; ещё маркер предыдущей страницы и название канала."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.page = TmePage(None)
        self._depth = 0                     # вложенность div
        self._post: str | None = None       # data-post открытого поста («name/123»)
        self._post_depth = 0
        self._text_depth = 0                # >0 — внутри div текста поста
        self._text_done = False
        self._buf: list[str] = []
        self._time: str | None = None
        self._date_link = False             # внутри a.tgme_widget_message_date
        self._reply = False                 # внутри a.tgme_widget_message_reply

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        cls = a.get("class", "").split()
        if self.page.before is None and a.get("data-before", "").isdigit():
            self.page.before = int(a["data-before"])
        if tag == "div":
            self._depth += 1
            if "tgme_widget_message" in cls and a.get("data-post"):
                self._open(a["data-post"])
            elif (self._post and not self._text_depth and not self._text_done
                  and not self._reply and "tgme_widget_message_text" in cls
                  and "js-message_reply_text" not in cls):
                self._text_depth = self._depth
        elif tag == "br":
            if self._text_depth:
                self._buf.append("\n")
        elif tag == "a":
            self._date_link = "tgme_widget_message_date" in cls
            self._reply = "tgme_widget_message_reply" in cls
        elif tag == "time":
            if self._post and a.get("datetime") and (self._date_link or self._time is None):
                self._time = a["datetime"]
        elif tag == "link":
            m = re.search(r"[?&]before=(\d+)", a.get("href", ""))
            if a.get("rel") == "prev" and m and self.page.before is None:
                self.page.before = int(m.group(1))
        elif tag == "meta":
            if a.get("property") == "og:title" and self.page.title is None:
                self.page.title = a.get("content") or None

    def handle_endtag(self, tag: str) -> None:
        if tag == "div":
            if self._text_depth == self._depth:
                self._text_depth, self._text_done = 0, True
            if self._post and self._post_depth == self._depth:
                self._close_post()
            self._depth -= 1
        elif tag == "a":
            self._date_link = self._reply = False

    def handle_data(self, data: str) -> None:
        if self._text_depth:
            self._buf.append(data)

    def _open(self, ref: str) -> None:
        if self._post:
            self._close_post()
        self._post, self._post_depth = ref, self._depth
        self._text_depth, self._text_done, self._buf, self._time = 0, False, [], None

    def _close_post(self) -> None:
        ref, self._post = self._post or "", None
        name, _, num = ref.rpartition("/")
        if not name or not num.isdigit() or not self._time:
            return
        try:
            ts = pd.Timestamp(self._time)
        except ValueError:
            return
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        self.page.posts.append(Post(name, int(num), ts, "".join(self._buf).strip()))

    def close(self) -> None:
        super().close()
        if self._post:
            self._close_post()


def read_tme_page(html: str) -> TmePage:
    """Страница веб-превью -> посты (по возрастанию id), маркер prev, название канала."""
    p = _TmeParser()
    p.feed(html or "")
    p.close()
    p.page.posts.sort(key=lambda x: x.post_id)
    return p.page


def parse_tme_page(html: str) -> list[Post]:
    return read_tme_page(html).posts


class ChannelUnavailable(Exception):
    """Нет публичного канала с таким username: личный аккаунт, бот, группа, приватный канал
    или опечатка — t.me уводит редиректом с /s/ на страницу без постов."""


def fetch_tme_html(name: str, before: int | None = None,
                   timeout: float = TIMEOUT_S) -> str | None:
    """Одна страница веб-превью (~20 постов). None — редирект не на /s/ (не публичный канал).
    name — только из parse_channel_ref ([A-Za-z0-9_]): в URL не попадает ничего чужого."""
    url = f"https://t.me/s/{name}" + (f"?before={int(before)}" if before else "")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept-Language": "ru,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if "/s/" not in resp.geturl():
            return None
        return resp.read(3_000_000).decode("utf-8", "replace")


def fetch_channel(name: str, max_posts: int = MAX_POSTS, max_pages: int = MAX_PAGES,
                  fetch: Callable[..., str | None] = fetch_tme_html,
                  pause: float = 0.5, budget_s: float = PAGES_BUDGET_S) -> TmePage:
    """До max_posts последних постов (страница за страницей назад по ?before=). Синхронно
    (urllib) — только через asyncio.to_thread. Нет канала -> ChannelUnavailable; сбой первой
    страницы — исключение urllib как есть. Старые страницы — по возможности: их сбой или
    исчерпанный бюджет времени (t.me порой тормозит ответ до 15+ с, замер 29.10) только
    заканчивают листание — аудит идёт по уже прочитанным постам."""
    t0 = time.monotonic()
    html = fetch(name)
    if html is None:
        raise ChannelUnavailable(name)
    first = read_tme_page(html)
    if first.title is None and not first.posts:
        raise ChannelUnavailable(name)
    posts = {p.post_id: p for p in first.posts}
    before, pages = first.before, 1
    while before and len(posts) < max_posts and pages < max_pages:
        if time.monotonic() - t0 > budget_s:
            break
        time.sleep(pause)                   # вежливо к t.me: страницы не залпом
        try:
            html = fetch(name, before)
        except (OSError, http.client.HTTPException):      # urllib: таймаут, 429, обрыв
            break
        if html is None:
            break
        page = read_tme_page(html)
        new = [p for p in page.posts if p.post_id not in posts]
        if not new:
            break
        posts.update((p.post_id, p) for p in new)
        before = page.before if page.before and page.before < before else None
        pages += 1
    ordered = sorted(posts.values(), key=lambda p: p.post_id)
    return TmePage(first.title, ordered[-max_posts:], None)


def _clean(text: str) -> str:
    return _URL.sub(" ", signal_check._normalize(text or ""))


def classify(text: str) -> tuple[str, ParsedSignal | None]:
    """signal — разобран (монета, сторона, вход, стоп); no_stop — призыв без стопа;
    unparsed — похоже на сигнал со стопом, но формат боту непонятен; other — всё прочее
    (новости, разборы, отчёты «TP1 ✅», превью)."""
    sig = signal_check.parse_signal(text)
    if sig is not None:
        return "signal", sig
    t = _clean(text)
    if not _SIDE_CALL.search(t) or signal_check._coin(t) is None or _REPORT.search(t):
        return "other", None
    if signal_check._STOP_KW.search(t):
        return "unparsed", None
    if len(t.strip()) <= _SHORT_CALL or signal_check._ENTRY_KW.search(t):
        return "no_stop", None
    return "other", None


def _age_s(now: pd.Timestamp, posted: pd.Timestamp) -> float:
    return float((now - posted).total_seconds())


def _scan(posts: list[Post], now: pd.Timestamp
          ) -> list[tuple[Post, str, ParsedSignal | None]]:
    """Классы постов по времени публикации: повтор уже виденного сигнала -> dup, старше
    MAX_AGE_DAYS -> old (так далеко свечи не проверяем); призыв без стопа, рядом с которым
    (±сутки) по той же монете есть пост со стопом, -> other (стоп канал всё-таки дал)."""
    seen: set[tuple] = set()
    out: list[tuple[Post, str, ParsedSignal | None]] = []
    for p in sorted(posts, key=lambda x: (x.posted, x.post_id)):
        kind, sig = classify(p.text)
        if sig is not None:
            key = (sig.coin, sig.side, sig.entry_lo, sig.entry_hi, sig.stop)
            if key in seen:
                kind = "dup"
            elif _age_s(now, p.posted) > signal_check.MAX_AGE_DAYS * 86400:
                kind = "old"
            seen.add(key)
        out.append((p, kind, sig))
    stops = [(p.posted, sig.coin if sig else signal_check._coin(_clean(p.text)))
             for p, kind, sig in out if kind != "other" and kind != "no_stop"]
    for i, (p, kind, _) in enumerate(out):
        if kind == "no_stop":
            coin = signal_check._coin(_clean(p.text))
            if any(c == coin and abs(_age_s(t, p.posted)) <= _PAIR_WINDOW_S for t, c in stops):
                out[i] = (p, "other", None)
    return out


def checkable(posts: list[Post], now: pd.Timestamp) -> list[tuple[Post, ParsedSignal]]:
    """Уникальные сигналы не старше MAX_AGE_DAYS — то, что пойдёт на свечи."""
    return [(p, s) for p, kind, s in _scan(posts, now) if kind == "signal" and s is not None]


def fetch_plan(posts: list[Post], now: pd.Timestamp) -> dict[str, tuple[str, int]]:
    """symbol -> (ТФ, баров): свечи каждой монеты — ОДИН запрос истории; ТФ — по самому
    старому её сигналу (pick_timeframe: чем старше, тем крупнее), глубина — от него до сейчас.
    Монет не больше MAX_SYMBOLS, свежие сигналы в приоритете."""
    oldest: dict[str, float] = {}
    for p, s in reversed(checkable(posts, now)):            # от свежих к старым
        if s.symbol not in oldest and len(oldest) >= MAX_SYMBOLS:
            continue
        oldest[s.symbol] = max(oldest.get(s.symbol, 0.0), _age_s(now, p.posted))
    plan: dict[str, tuple[str, int]] = {}
    for sym, age in oldest.items():
        tf = signal_check.pick_timeframe(age) or "4h"
        plan[sym] = (tf, max(int(age // _TF_S[tf]), 0) + 3)
    return plan


def tp1_r(sig: ParsedSignal, res: signal_check.CheckResult) -> float | None:
    """Итог при фиксации всей позиции на TP1: TP1 раньше стопа -> +R до цели от цены
    исполнения (не меньше 0: цель, которую цена уже прошла к моменту входа, закрывается по
    рынку, ~0R); стоп раньше -> −1R. Не исполнился / открыт без TP1 -> None (вне итога).
    Без целей фиксировать нечего: в итог попадает только стоп."""
    if res.fill_price is None:
        return None
    if res.hit >= 1 and sig.targets:
        return max(0.0, signal_check._r(sig, res.fill_price, sig.targets[0]))
    if res.status == "stop":
        return -1.0
    return None


def audit(posts: list[Post], bars_by_symbol: dict[str, pd.DataFrame],
          now: pd.Timestamp) -> dict:
    """Агрегат по каналу. bars_by_symbol — свечи по ParsedSignal.symbol (уже в масштабе
    цен канала: 1000-лоты Bybit бот делит сам); нет ключа — монеты нет на бирже или свечи
    не загрузились."""
    agg: dict = dict(posts=len(posts), first=None, last=None, signals=0, duplicates=0,
                     no_stop=0, unparsed=0, too_old=0, no_bars=0, checked=0, no_fill=0,
                     filled=0, tp1=0, stop_first=0, open=0, ambiguous=0, no_targets=0,
                     r_total=0.0, r_trades=0, tp1_avg_r=None, tfs=[], trades=[])
    if posts:
        agg["first"] = min(p.posted for p in posts)
        agg["last"] = max(p.posted for p in posts)
    plan = fetch_plan(posts, now)
    tfs: set[str] = set()
    tp1_rs: list[float] = []
    for p, kind, sig in _scan(posts, now):
        if kind in ("no_stop", "unparsed"):
            agg[kind] += 1
        elif kind == "dup":
            agg["duplicates"] += 1
        if sig is None or kind not in ("signal", "old"):
            continue
        agg["signals"] += 1
        if kind == "old":
            agg["too_old"] += 1
            continue
        bars = bars_by_symbol.get(sig.symbol)
        if bars is None or bars.empty:
            agg["no_bars"] += 1
            continue
        res = signal_check.evaluate(sig, bars, p.posted)
        agg["checked"] += 1
        if sig.symbol in plan:
            tfs.add(plan[sig.symbol][0])
        if not sig.targets:
            agg["no_targets"] += 1
        if res.fill_price is None:
            outcome = "no_fill"
        elif res.hit >= 1:
            outcome = "tp1"
        elif res.status == "stop":
            outcome = "stop"
        else:
            outcome = "open"
        agg["no_fill" if outcome == "no_fill" else "filled"] += 1
        if outcome != "no_fill":
            agg[{"tp1": "tp1", "stop": "stop_first", "open": "open"}[outcome]] += 1
        agg["ambiguous"] += int(res.ambiguous)
        r = tp1_r(sig, res)
        if r is not None:
            agg["r_total"] += r
            agg["r_trades"] += 1
            if outcome == "tp1":
                tp1_rs.append(r)
        agg["trades"].append(dict(post_id=p.post_id, url=p.url, posted=p.posted, coin=sig.coin,
                                  side=sig.side, outcome=outcome, hit=res.hit,
                                  targets=len(sig.targets), r=r))
    agg["r_total"] = round(agg["r_total"], 2)
    # размер первой цели в R: 89% «до TP1» при цели в 0.07R — почти ноль на сделку (binance_360,
    # 29.10) — без этой цифры процент попаданий выглядит сильнее, чем он есть
    if tp1_rs:
        agg["tp1_avg_r"] = round(sum(tp1_rs) / len(tp1_rs), 3)
    agg["tfs"] = sorted(tfs, key=list(_TF_S).index)
    return agg


# ---------------------------------------------------------------------------
# Тексты и карточка
# ---------------------------------------------------------------------------

def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 10 < n < 20:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def _pct(a: int, b: int) -> str:
    return f"{round(100 * a / b)}%" if b else "—"


def _short(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _period(agg: dict) -> str:
    a, b = agg["first"], agg["last"]
    if a is None or b is None:
        return ""
    if a.date() == b.date():
        return b.strftime("%d.%m.%Y")
    left = a.strftime("%d.%m") if a.year == b.year else a.strftime("%d.%m.%Y")
    return f"{left}–{b.strftime('%d.%m.%Y')}"


def _r_fmt(r: float) -> str:
    """«+0.07R» / «−1.0R»: меньше 1R — два знака (цель в 0.07R округлением до «+0.1R» выглядела
    бы вдвое больше), «-0.00» не бывает."""
    if abs(r) < 0.005:
        return "0.00R"
    return f"{r:+.2f}R" if abs(r) < 1 else f"{r:+.1f}R"


def progress_text(n: int) -> str:
    """Промежуточный статус, пока грузятся свечи (10–30 с): видно, что бот работает и над чем."""
    if not n:
        return "🔎 Читаю канал… сигналов с входом и стопом пока не вижу, досчитываю"
    return (f"🔎 Читаю канал… проверяю {n} {_plural(n, 'сигнал', 'сигнала', 'сигналов')} "
            "по свечам биржи")


def card_worthy(agg: dict) -> bool:
    """Карточка имеет смысл, когда есть что показать: исполненные сигналы или посты без стопа."""
    return bool(agg["filled"] or agg["no_stop"])


def log_line(agg: dict) -> str:
    return (f"постов {agg['posts']}, сигналов {agg['signals']}, проверено {agg['checked']}, "
            f"TP1 {agg['tp1']}/{agg['filled']}, стоп {agg['stop_first']}, "
            f"без стопа {agg['no_stop']}, итог на TP1 {_r_fmt(agg['r_total'])} "
            f"({agg['r_trades']} сд.)")


_OUTCOME_TXT = {"tp1": "TP1 ✅", "stop": "стоп ❌", "open": "открыта, TP1 нет",
                "no_fill": "вход не исполнился"}
_SMALL_SAMPLE = 10


def _sample_warning(f: int) -> str:
    return (f"Малая выборка — {f} {_plural(f, 'сделка', 'сделки', 'сделок')}, выводы "
            "предварительные." if 0 < f < _SMALL_SAMPLE else "")


def render_audit(name: str, title: str | None, agg: dict, venue: str = "Bybit",
                 max_rows: int = 12) -> str:
    """Подробный итог для лички (parse_mode=HTML): счётчики, правило итога на TP1, последние
    сигналы со ссылками на посты (каждый можно перепроверить руками)."""
    sub = []
    if title:
        sub.append(f"«{esc(_short(title, 60))}»")
    sub.append(f"{agg['posts']} {_plural(agg['posts'], 'пост', 'поста', 'постов')}")
    if agg["first"] is not None:
        sub.append(_period(agg))
    lines = [f"🔎 <b>Проверка канала @{esc(name)}</b>", " · ".join(sub), ""]
    if not agg["signals"] and not agg["no_stop"]:
        lines.append("В последних постах не нашёл сигналов с входом и стопом — проверять "
                     "нечего. Бот понимает посты вида «монета, LONG/SHORT, вход (или «по "
                     "рынку»), стоп, цели».")
        if agg["unparsed"]:
            lines.append(f"Похожих на сигнал, но в непонятном боту формате: {agg['unparsed']}.")
        lines += ["", "<i>Отдельный пост можно проверить пересылкой сюда — /check</i>"]
        return "\n".join(lines)
    extra = []
    if agg["duplicates"]:
        extra.append(f"повторы не считаем: {agg['duplicates']}")
    if agg["too_old"]:
        extra.append(f"старше {signal_check.MAX_AGE_DAYS} дней: {agg['too_old']}")
    if agg["no_bars"]:
        extra.append(f"нет на {esc(venue)}: {agg['no_bars']}")
    lines.append(f"Сигналов с входом и стопом: <b>{agg['signals']}</b> · проверено по свечам: "
                 f"<b>{agg['checked']}</b>" + (f" ({'; '.join(extra)})" if extra else ""))
    if agg["checked"]:
        lines.append(f"Вход исполнился: <b>{agg['filled']}</b> · цена не дошла до входа: "
                     f"{agg['no_fill']}")
    f = agg["filled"]
    if f:
        size = (f" · цель в среднем {_r_fmt(agg['tp1_avg_r'])}"
                if agg["tp1_avg_r"] is not None else "")
        lines += ["",
                  f"✅ До первой цели: <b>{agg['tp1']} из {f} ({_pct(agg['tp1'], f)})</b>{size}",
                  f"❌ Сразу в стоп: <b>{agg['stop_first']} из {f} "
                  f"({_pct(agg['stop_first'], f)})</b>"]
        if agg["open"]:
            lines.append(f"⏳ Ещё открыты, TP1 не взят: {agg['open']}")
    if agg["no_stop"]:
        lines.append(f"🚫 Без стопа: <b>{agg['no_stop']}</b> "
                     f"{_plural(agg['no_stop'], 'пост', 'поста', 'постов')} — монета и "
                     "LONG/SHORT есть, стоп-лосса нет")
    if agg["unparsed"]:
        lines.append(f"Не разобрал формат: {agg['unparsed']} (в итог не входят)")
    if agg["r_trades"]:
        lines += ["",
                  f"Итог, если закрывать всю позицию на первой цели: "
                  f"<b>{_r_fmt(agg['r_total'])}</b> за {agg['r_trades']} "
                  f"{_plural(agg['r_trades'], 'сделку', 'сделки', 'сделок')}",
                  "<i>TP1 раньше стопа — плюс до цели в R, стоп раньше — −1R; открытые без "
                  "TP1 в итог не входят.</i>"]
        if agg["no_targets"]:
            lines.append(f"<i>Без целей: {agg['no_targets']} — им в итог идёт только стоп.</i>")
    rows = agg["trades"][-max_rows:]
    if rows:
        lines += ["", "<b>Последние сигналы</b> (нажмите — откроется пост):"]
        for t in reversed(rows):
            mark = _OUTCOME_TXT[t["outcome"]]
            r = f" {_r_fmt(t['r'])}" if t["r"] is not None else ""
            lines.append(f"• {t['posted'].strftime('%d.%m')} <a href=\"{esc(t['url'])}\">"
                         f"{esc(t['coin'])} {t['side']}</a> — {mark}{r}")
    tfs = ", ".join(agg["tfs"]) or "15m–4h"
    note = (f"Как считали: вход, стоп и цели — ровно как в посте; исход — по свечам {esc(venue)} "
            f"({tfs}) с первой свечи после публикации.")
    if agg["ambiguous"]:
        note += f" Стоп и цель в одной свече — считаем стоп (таких: {agg['ambiguous']})."
    note += (" Цены мелких монет на разных биржах расходятся, а правки постов задним числом "
             "нам не видны — видим уже исправленный текст.")
    lines += ["", f"<i>{note}</i>"]
    if warn := _sample_warning(f):
        lines.append(f"⚠️ {warn}")
    lines += ["Не финансовый совет.", "",
              "<i>RSI Range публикует итог каждой своей сделки автоматически, включая минусы: "
              "/stats · журнал целиком: /trackrecord</i>"]
    return "\n".join(lines)


def audit_caption(name: str, agg: dict, venue: str = "Bybit") -> str:
    """Подпись к карточке (≤1024 симв.): главные цифры одной строкой."""
    parts = []
    f = agg["filled"]
    if f:
        parts += [f"до первой цели — {_pct(agg['tp1'], f)}",
                  f"сразу в стоп — {_pct(agg['stop_first'], f)}"]
    parts.append(f"без стопа — {agg['no_stop']}")
    if agg["r_trades"]:
        parts.append(f"итог на TP1 — {_r_fmt(agg['r_total'])}")
    return (f"🔎 <b>@{esc(name)}</b> по свечам {esc(venue)}: " + ", ".join(parts)
            + f" ({agg['checked']} {_plural(agg['checked'], 'сигнал', 'сигнала', 'сигналов')}"
            f", {_period(agg)}). Подробности ниже 👇\nНе финансовый совет.")


_CARD_STYLE = """
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#121211; color:#e8e6e1; -webkit-font-smoothing:antialiased;
       font-family:"SF Mono", ui-monospace, Menlo, Consolas, monospace; }
.card { width:1080px; padding:60px 72px 46px; background:
        radial-gradient(1100px 480px at 18% -12%, rgba(57,135,229,.10), transparent 60%),
        #121211; }
.head { display:flex; justify-content:space-between; align-items:baseline;
        border-bottom:1px solid #2a2927; padding-bottom:22px; }
.brand { font-size:26px; font-weight:700; letter-spacing:.28em; }
.tag { font-size:15px; color:#8a877f; letter-spacing:.05em; }
.chan { font-weight:700; margin-top:40px; letter-spacing:-.01em; overflow-wrap:anywhere;
        line-height:1.1; }
.meta { font-size:19px; color:#8a877f; margin-top:12px; line-height:1.4; }
.grid { display:grid; grid-template-columns:1fr 1fr; gap:18px; margin-top:38px; }
.tile { border:1px solid #2a2927; border-radius:14px; padding:26px 30px 24px;
        background:rgba(255,255,255,.015); }
.lbl { font-size:15px; color:#8a877f; letter-spacing:.12em; text-transform:uppercase; }
.val { font-size:86px; font-weight:700; margin-top:12px; line-height:1;
       letter-spacing:-.02em; font-variant-numeric:tabular-nums; }
.sub { font-size:16px; color:#8a877f; margin-top:14px; line-height:1.45; }
.sub b { color:#c9c6bf; font-weight:600; }
.pos { color:#3987e5; } .neg { color:#e66767; } .warn { color:#e0a64a; }
.mute { color:#5f5c55; }
.small { margin-top:22px; font-size:17px; color:#e0a64a; }
.strip { display:flex; flex-wrap:wrap; gap:8px; margin-top:30px; }
.sq { width:22px; height:22px; border-radius:5px; }
.sq.tp1 { background:#3987e5; } .sq.stop { background:#e66767; }
.sq.open { border:2px solid #8a877f; } .sq.no_fill { background:#2a2927; }
.legend { display:flex; gap:26px; margin-top:14px; font-size:14px; color:#8a877f; }
.legend i { display:inline-block; width:12px; height:12px; border-radius:3px;
            margin-right:8px; vertical-align:-1px; }
.foot { display:flex; justify-content:space-between; margin-top:38px;
        border-top:1px solid #2a2927; padding-top:20px; font-size:16px; color:#8a877f; }
.foot b { color:#c9c6bf; font-weight:600; }
.fine { margin-top:10px; font-size:13px; color:#5f5c55; line-height:1.5; }
"""


def _tile(label: str, value: str, cls: str, sub: str) -> str:
    return (f'<div class="tile"><div class="lbl">{label}</div>'
            f'<div class="val {cls}">{value}</div><div class="sub">{sub}</div></div>')


def audit_card_html(name: str, title: str | None, agg: dict, venue: str = "Bybit",
                    bot: str = "rsirange_bot") -> str:
    """Карточка аудита для шеринга (PNG через graphics.render_html_to_png, selector=".card").
    Все строки канала (название) — через esc: это чужой текст."""
    f, ns, rt = agg["filled"], agg["no_stop"], agg["r_trades"]

    def of_filled(k: int) -> str:
        if not f:
            return "нет исполненных входов"
        return f"{k} из {f} {_plural(f, 'исполненного', 'исполненных', 'исполненных')}"

    tp1_sub = of_filled(agg["tp1"])
    if agg["tp1_avg_r"] is not None:
        tp1_sub += f"<br>цель в среднем <b>{_r_fmt(agg['tp1_avg_r'])}</b>"
    tiles = [
        _tile("До первой цели", _pct(agg["tp1"], f), "pos" if f else "mute", tp1_sub),
        _tile("Сразу в стоп", _pct(agg["stop_first"], f), "neg" if f else "mute",
              of_filled(agg["stop_first"])),
        _tile("Без стопа", str(ns), "warn" if ns else "mute",
              f"{_plural(ns, 'сигнал', 'сигнала', 'сигналов')} без стоп-лосса"),
    ]
    if rt:
        r = agg["r_total"]
        tiles.append(_tile("Итог на TP1", _r_fmt(r).replace("-", "−"), "pos" if r >= 0 else "neg",
                           f"{rt} {_plural(rt, 'сделка', 'сделки', 'сделок')} · вся позиция "
                           "на первой цели"))
    else:
        tiles.append(_tile("Итог на TP1", "—", "mute", "нет закрытых сделок"))
    squares = "".join(f'<div class="sq {t["outcome"]}"></div>' for t in agg["trades"][-60:])
    legend = ('<div class="legend"><span><i style="background:#3987e5"></i>TP1</span>'
              '<span><i style="background:#e66767"></i>стоп</span>'
              '<span><i style="border:2px solid #8a877f"></i>открыта</span>'
              '<span><i style="background:#2a2927"></i>вход не исполнился</span></div>')
    strip = f'<div class="strip">{squares}</div>{legend}' if squares else ""
    small = f'<div class="small">⚠ {_sample_warning(f)}</div>' if _sample_warning(f) else ""
    meta = [esc(_short(title, 48))] if title else []
    meta.append(f"{agg['posts']} {_plural(agg['posts'], 'пост', 'поста', 'постов')}")
    if agg["first"] is not None:
        meta.append(_period(agg))
    meta.append(f"{agg['checked']} {_plural(agg['checked'], 'сигнал', 'сигнала', 'сигналов')} "
                "по свечам")
    # моно-шрифт: 32 символа username на 58px не влезают в строку — кегль по длине
    size = 58 if len(name) <= 22 else 48 if len(name) <= 27 else 40
    fine = ("Вход, стоп и цели — как в постах канала; исход — по свечам биржи после публикации, "
            "стоп и цель в одной свече — считаем стоп. Цены мелких монет на разных биржах "
            "расходятся; правки постов задним числом не видны.")
    body = (
        '<div class="card">'
        '<div class="head"><div class="brand">RSI RANGE</div>'
        '<div class="tag">проверка канала по свечам</div></div>'
        f'<div class="chan" style="font-size:{size}px">@{esc(name)}</div>'
        f'<div class="meta">{" · ".join(meta)}</div>'
        f'<div class="grid">{"".join(tiles)}</div>'
        f"{small}{strip}"
        f'<div class="foot"><div>Проверено по свечам <b>{esc(venue)}</b> · <b>@{esc(bot)}</b>'
        '</div><div>Не финансовый совет</div></div>'
        f'<div class="fine">{fine}</div>'
        "</div>")
    return f"<style>{_CARD_STYLE}</style>{body}"


HELP = (
    "🔎 <b>Проверка канала целиком</b>\n\n"
    "Пришлите сюда @username публичного канала с сигналами (или ссылку t.me/…) — бот "
    "прочитает его последние посты и прогонит каждый сигнал по свечам Bybit: сколько дошли "
    "до первой цели, сколько сразу ушли в стоп, сколько постов вообще без стопа.\n\n"
    "Пример: <code>/audit @channel</code>\n"
    f"Только публичные каналы, до {MAX_POSTS} последних постов, сигналы не старше "
    f"{signal_check.MAX_AGE_DAYS} дней. Бесплатно, {AUDITS_PER_DAY} проверки канала в сутки.\n"
    "Не финансовый совет.")
