"""
graphics.py — PNG-карточки бренда RSI Range: карточка недели, карта сигнала, «мой итог»,
og-карточка и дашборд трек-рекорда.

HTML/CSS -> PNG через headless Chromium (Playwright). matplotlib для первой попытки был
забракован — даёт эстетику научного графика, не настоящий дизайн; HTML/CSS + Playwright
даёт настоящую типографику и раскладку.

Дизайн-система v5 «терминал» — docs/design/design-system.md. Токены и примитивы —
assets/brand.css; шрифты — встроенные сабсеты Inter и IBM Plex Mono (assets/fonts, OFL),
base64 прямо в HTML через brand_css(): ноль внешних запросов, и карточка на сервере
(Ubuntu: SF/Segoe нет) выглядит так же, как на маке. Новый шаблон — начинать с
<style>{brand_css()}…</style>.

render_html_to_png поднимает/закрывает целый headless-браузер на каждый вызов — тяжёлая
синхронная операция. Вызывать ТОЛЬКО через asyncio.to_thread, никогда напрямую из event loop
(см. использование в bot.py:_maintenance). Приемлемо на еженедельной частоте, не для
частого вызова.
"""
from __future__ import annotations

import base64
import math
from functools import lru_cache
from pathlib import Path
from string import Template

_CARD_WIDTH = 1200
_PAD = 56                                   # поля карточки 1200 (.wrap) — от них ширины SVG
_CHART_W = _CARD_WIDTH - 2 * _PAD - 2       # внутренняя ширина .chart-card (минус бордеры)
_ASSETS = Path(__file__).parent / "assets"

# Цвета для SVG (атрибуты SVG не понимают var()) — зеркало токенов assets/brand.css;
# синхронность сторожит tests/test_graphics.py.
POS, NEG, ACC = "#1f9e6e", "#f6465d", "#f7a600"
INK, INK2, INK3 = "#eaecef", "#8a94a1", "#5c6672"
BG, PANEL, LINE, GRID, ZERO = "#0b0e11", "#14181d", "#232a32", "#1b2027", "#3a424c"
_MINUS = "−"       # типографский минус «−»: по ширине равен плюсу (дефис короче)

# (CSS-семейство, вес, файл в assets/fonts). RR Sans = Inter 3.019, RR Mono = IBM Plex
# Mono 2.3 (у Plex Reserved Font Name — изменённый сабсет обязан носить другое имя).
_FONT_FILES = (("RR Sans", 400, "rr-sans-400"), ("RR Sans", 500, "rr-sans-500"),
               ("RR Sans", 600, "rr-sans-600"), ("RR Sans", 700, "rr-sans-700"),
               ("RR Mono", 400, "rr-mono-400"), ("RR Mono", 700, "rr-mono-700"))


@lru_cache(maxsize=1)
def brand_css() -> str:
    """Общий блок стилей бренда: @font-face (WOFF2 base64) + токены/примитивы brand.css.
    Кэшируется на процесс: ~145 КБ строки, читается с диска один раз."""
    faces = []
    for family, weight, name in _FONT_FILES:
        data = (_ASSETS / "fonts" / f"{name}.woff2").read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        faces.append(f'@font-face{{font-family:"{family}";font-style:normal;'
                     f'font-weight:{weight};font-display:block;'
                     f'src:url(data:font/woff2;base64,{b64}) format("woff2")}}')
    return "".join(faces) + (_ASSETS / "brand.css").read_text(encoding="utf-8")


def _esc(value) -> str:
    """Минимальный HTML-escape для интерполируемых строк (даты/подписи — не пользовательский
    ввод, но дёшево подстраховаться на случай будущих изменений содержимого)."""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _r_class(value: float) -> str:
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "neutral"


def _signed(value: float, digits: int = 1, unit: str = "R") -> str:
    """+1.2R / −1.2R / 0.0R — знак символом (вторичное кодирование, не только цветом),
    минус типографский. Ноль без знака: «+0.0R» читался бы как плюс."""
    r = round(float(value), digits)
    if r == 0:
        return f"{0:.{digits}f}{unit}"
    return f"{'+' if r > 0 else _MINUS}{abs(r):.{digits}f}{unit}"


def _sign_class(value: float, digits: int = 1) -> str:
    """Класс цвета по ОКРУГЛЁННОМУ значению — цвет не спорит с подписью (−0.04 -> «0.0R»)."""
    return _r_class(round(float(value), digits))


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русское согласование с числом: 1 сделка, 3 сделки, 5 сделок, 21 сделка."""
    m10, m100 = abs(n) % 10, abs(n) % 100
    if m10 == 1 and m100 != 11:
        return one
    if 2 <= m10 <= 4 and not 12 <= m100 <= 14:
        return few
    return many


def _hero_px(text: str, width: int, cap: int) -> int:
    """Кегль числа-героя (моно 700: 0.6em на знак, трекинг −0.05em), чтобы строка влезла
    в width: «+9.9R» — во весь кегль, «+123.4R» — мельче, но целиком и со знаком."""
    n = max(len(text), 1)
    return int(min(cap, width / (0.6 * n - 0.05 * (n - 1))))


def _pair(symbol: str) -> tuple[str, str]:
    """'BTC/USDT:USDT' -> ('BTC', 'USDT'): ccxt-хвост «:USDT» людям не нужен."""
    base, _, rest = str(symbol).partition("/")
    return base, rest.split(":")[0]


def _foot(stamp: str, extra: str = "", bot: str = "rsirange_bot") -> str:
    """Единый футер всех карточек: «@бот · исход по ценам биржи · Не финансовый совет»."""
    return (f'<div class="rr-foot"><div><b>@{_esc(bot)}</b> · исход по ценам биржи'
            f' · Не финансовый совет{extra}</div><div class="rr-stamp">{stamp}</div></div>')


# ---------------------------------------------------------------------------
# Карточка недели (канал, раз в ISO-неделю). Классы шапки/панелей ниже — общие:
# их наследует карта сигнала (_STYLE + _CHART_STYLE).
# ---------------------------------------------------------------------------

_STYLE = """
  html, body { width: 1200px; }
  .canvas { position: relative; width: 1200px; background: var(--bg); color: var(--ink);
            display: flex; flex-direction: column; }
  .wrap { flex: 1; padding: 44px 56px 36px; display: flex; flex-direction: column; gap: 28px; }

  .header { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px;
            padding-bottom: 18px; border-bottom: 1px solid var(--line); }
  .brand { display: flex; align-items: center; gap: 14px; }
  .mark { flex: 0 0 auto; width: 16px; height: 16px; border-radius: 3px; background: var(--acc);
          font-size: 0; color: transparent; overflow: hidden; }
  .brand-text { display: flex; align-items: baseline; gap: 14px; }
  .brand-text .name { font: 700 21px/1 var(--mono); letter-spacing: .16em; text-transform: uppercase;
                      color: var(--ink); }
  .brand-text .handle { font: 500 16px/1 var(--sans); color: var(--ink-2); }
  .header-right { text-align: right; }
  .header-right .eyebrow { font: 600 13px/1.2 var(--sans); letter-spacing: .14em; color: var(--ink-2);
                           text-transform: uppercase; font-feature-settings: "case" 1; }
  .header-right .period { font: 700 22px/1 var(--mono); color: var(--ink); margin-top: 9px; }
  .divider { height: 1px; background: var(--line); flex-shrink: 0; }

  .card { background: var(--panel); border: 1px solid var(--line); border-radius: var(--r-lg); }

  .square { min-height: 1200px; }
  .hero { flex: 1; display: flex; flex-direction: column; justify-content: center; min-width: 0; }
  .stat-label { font: 600 13px/1.25 var(--sans); letter-spacing: .14em; text-transform: uppercase;
                color: var(--ink-2); font-feature-settings: "case" 1; }
  .stat-value { font: 700 272px/.86 var(--mono); letter-spacing: -.05em; white-space: nowrap;
                margin: 24px 0 24px -12px; }
  .stat-value.long { font-size: 236px; }
  .stat-value.positive { color: var(--pos); }
  .stat-value.negative { color: var(--neg); }
  .stat-value.neutral { color: var(--ink); }
  .stat-sub { font: 500 23px/1.35 var(--sans); color: var(--ink-2); }
  .stat-sub b { color: var(--ink); font-weight: 600; }

  .strip { display: grid; grid-template-columns: 1fr 1fr; }
  .mrow { display: flex; align-items: center; justify-content: space-between; gap: 16px;
          padding: 22px 26px; border-left: 1px solid var(--line); }
  .mrow:first-child { border-left: none; }
  .mrow .stat-sub { font-size: 15px; margin-top: 8px; }
  .mval { font: 700 50px/1 var(--mono); letter-spacing: -.03em; white-space: nowrap; }
  .mval.positive { color: var(--pos); } .mval.negative { color: var(--neg); }
  .mval.neutral { color: var(--ink); }

  .unit-card { padding: 20px 24px 18px; }
  .unit-head { display: flex; align-items: baseline; justify-content: space-between; gap: 16px;
               margin-bottom: 16px; }
  .unit-title { font: 600 17px/1.25 var(--sans); color: var(--ink); }
  .unit-title .muted { color: var(--ink-2); font-weight: 500; }
  .legend { display: flex; gap: 20px; }
  .legend-item { display: flex; align-items: center; gap: 8px; font: 500 15px/1 var(--sans);
                 color: var(--ink-2); white-space: nowrap; }
  .dot { width: 12px; height: 12px; border-radius: 3px; }
  .dot.win { background: var(--pos); }
  .dot.loss { box-shadow: inset 0 0 0 2px var(--neg); }
  .units { display: flex; flex-wrap: wrap; gap: 6px; }
  .unit { flex: 0 0 auto; width: var(--u, 24px); height: var(--u, 24px); border-radius: 4px; }
  .unit.win { background: var(--pos); }
  .unit.loss { box-shadow: inset 0 0 0 2px var(--neg); }
  .unit-empty { font: 500 17px/1.4 var(--sans); color: var(--ink-2); padding: 6px 0 2px; }
  .unit-foot { display: flex; justify-content: space-between; gap: 16px; margin-top: 16px;
               font: 500 14px/1.3 var(--sans); color: var(--ink-2); }

  .bottom { display: grid; grid-template-columns: 1.3fr 1fr; gap: 32px; }
  .note { padding-top: 14px; border-top: 2px solid var(--line); }
  .note.warning { border-top-color: var(--acc); }
  .note-label { display: flex; align-items: center; gap: 9px; font: 600 13px/1.2 var(--sans);
                letter-spacing: .14em; text-transform: uppercase; color: var(--ink);
                font-feature-settings: "case" 1; }
  .note.warning .note-label { color: var(--acc); }
  .note-label .icon { width: 18px; height: 18px; border-radius: 4px; display: flex;
                      align-items: center; justify-content: center; font-size: 12px;
                      font-weight: 700; background: var(--panel-2); color: var(--ink); }
  .note.warning .icon { background: var(--acc-bg); color: var(--acc); }
  .note p { margin-top: 10px; font: 500 16px/1.5 var(--sans); color: var(--ink-2); }
  .note p b { color: var(--ink); font-weight: 600; }
"""

_BODY = Template("""
<div class="canvas square"><div class="wrap">
  $head
  <div class="hero">
    <div class="stat-label">Итог недели · R до комиссий</div>
    <div class="stat-value $total_r_class$total_r_long">$total_r</div>
    <div class="stat-sub"><b>$closed_words</b> · $open ещё в работе</div>
  </div>
  <div class="card strip">
    <div class="mrow">
      <div><div class="stat-label">Winrate</div><div class="stat-sub">$wins_words · $losses_words</div></div>
      <div class="mval neutral">$winrate%</div>
    </div>
    <div class="mrow">
      <div><div class="stat-label">Матожидание</div><div class="stat-sub">на сделку · среднее по $closed</div></div>
      <div class="mval $expectancy_class">$expectancy</div>
    </div>
  </div>

  <div class="card unit-card">
    <div class="unit-head">
      <div class="unit-title">$closed_words <span class="muted">— каждая учтена</span></div>
      <div class="legend">
        <div class="legend-item"><span class="dot win"></span>$wins_words</div>
        <div class="legend-item"><span class="dot loss"></span>$losses_words</div>
      </div>
    </div>
    $units_html
    <div class="unit-foot">
      <span>Порядок соответствует хронологии закрытия сделок</span>
      <span>Полный CSV трек-рекорд — в закреплённом сообщении канала</span>
    </div>
  </div>

  <div class="bottom">
    <div class="note honesty">
      <div class="note-label"><span class="icon">&check;</span>Почему мы публикуем убытки</div>
      <p>Мы показываем <b>100% исходов</b> — прибыльные и убыточные сделки в равной степени. Если статистика ухудшится, вы увидите это здесь первыми, а не узнаете постфактум.</p>
    </div>
    <div class="note warning">
      <div class="note-label"><span class="icon">!</span>Малая выборка</div>
      <p>Выборка мала (<b>$sample_words</b>) — для статистической значимости нужно <b>100+</b>. Не делайте выводов о долгосрочном edge на этих цифрах.</p>
    </div>
  </div>

  $foot
</div></div>
""")


def _brand_head(eyebrow: str, title: str) -> str:
    """Шапка карточки: знак + «RSI RANGE» + @канал слева, надзаголовок и заголовок справа."""
    return ('<div class="header"><div class="brand"><div class="mark"></div>'
            '<div class="brand-text"><div class="name">RSI Range</div>'
            '<div class="handle">@rsi_range</div></div></div>'
            f'<div class="header-right"><div class="eyebrow">{eyebrow}</div>'
            f'<div class="period">{title}</div></div></div>')


def _units_html(outcomes: list) -> str:
    """Серия исходов: победа — заливка, поражение — контур (не только цветом). Размер
    квадрата подстраивается: мало сделок — крупнее, много — в несколько рядов."""
    n = len(outcomes)
    inner = _CARD_WIDTH - 2 * _PAD - 2 - 48             # .unit-card: бордеры + поля 24px
    pitch = max(28, min(52, (inner + 6) // max(n, 1)))  # n квадратов + (n−1) зазоров по 6px
    cells = "".join(f'<div class="unit {"win" if w else "loss"}"></div>' for w in outcomes)
    return f'<div class="units" style="--u:{pitch - 6}px">{cells}</div>'


def build_weekly_card_html(data: dict) -> str:
    """data: {period_label, total_r, closed, open, winrate_pct, wins, losses, expectancy_r,
    outcomes: list[bool] (хронологически, True=win), updated_label} -> заполненный HTML.

    Чистая функция — тестируется без Playwright/браузера."""
    if data["outcomes"]:
        units_html = _units_html(data["outcomes"])
    else:
        units_html = '<div class="unit-empty">На этой неделе ещё нет закрытых сделок.</div>'
    closed, wins, losses = int(data["closed"]), int(data["wins"]), int(data["losses"])
    total = _signed(data["total_r"], 1)
    body = _BODY.substitute(
        head=_brand_head("Отчёт за неделю", _esc(data["period_label"])),
        total_r=total,
        total_r_class=_sign_class(data["total_r"], 1),
        total_r_long=" long" if len(total) >= 8 else "",
        closed=closed,
        closed_words=f"{closed} {_plural(closed, 'закрытая сделка', 'закрытые сделки', 'закрытых сделок')}",
        sample_words=f"{closed} {_plural(closed, 'сделка', 'сделки', 'сделок')}",
        open=data["open"],
        winrate=f"{data['winrate_pct']:.0f}",
        wins_words=f"{wins} {_plural(wins, 'победа', 'победы', 'побед')}",
        losses_words=f"{losses} {_plural(losses, 'поражение', 'поражения', 'поражений')}",
        expectancy=_signed(data["expectancy_r"], 2),
        expectancy_class=_sign_class(data["expectancy_r"], 2),
        units_html=units_html,
        foot=_foot(f"Обновлено {_esc(data['updated_label'])}"),
    )
    return f"<style>{brand_css()}{_STYLE}</style>{body}"


def render_html_to_png(html: str, width: int = _CARD_WIDTH, scale: int = 2,
                       selector: str | None = None) -> bytes:
    """Синхронный рендер (Playwright sync API) — вызывать ТОЛЬКО из потока
    (asyncio.to_thread), никогда напрямую из event loop. Высота — intrinsic по контенту
    (full_page=True), не зафиксирована: юнит-чарт может перенестись на несколько строк
    в активную неделю без обрезания карточки. selector — скриншот ОДНОГО элемента
    (личная карточка ниже вьюпорта — full_page дорисовал бы пустое поле до 800px).
    Перед снимком ждём document.fonts.ready: встроенные шрифты (font-display:block)
    иначе могли бы не успеть, и текст снялся бы пустым или запасным шрифтом."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(device_scale_factor=scale)
            page.set_viewport_size({"width": width, "height": 800})
            page.set_content(html)
            page.evaluate("() => document.fonts.ready.then(() => true)")
            page.wait_for_timeout(100)
            if selector:
                return page.locator(selector).screenshot()
            return page.screenshot(full_page=True)
        finally:
            browser.close()


def render_weekly_card(data: dict) -> bytes:
    # снимок элемента: квадрат 1200×1200 (сетка «Медиа» в Telegram режет по центру в
    # квадрат — итог со знаком остаётся в кадре); full_page дорисовал бы поле до вьюпорта
    return render_html_to_png(build_weekly_card_html(data), selector=".canvas")


# ---------------------------------------------------------------------------
# Персональная карточка «мой итог» — шеринг подписчиком своего результата по
# отметкам сделок. Без уровней и сетапов — только личные агрегаты.
# ---------------------------------------------------------------------------

_MYCARD_STYLE = """
  .card { width: 1080px; min-height: 1080px; padding: 48px 56px 40px; background: var(--bg);
          color: var(--ink); display: flex; flex-direction: column; }
  .head { display: flex; justify-content: space-between; align-items: center; gap: 24px;
          padding-bottom: 20px; border-bottom: 1px solid var(--line); }
  .head .who { display: flex; align-items: baseline; gap: 14px; }
  .hero { flex: 1; display: flex; flex-direction: column; justify-content: center; }
  .num { font: 700 300px/.86 var(--mono); letter-spacing: -.05em; margin: 26px 0 26px -14px;
         white-space: nowrap; }
  .pos { color: var(--pos); } .neg { color: var(--neg); } .zero { color: var(--ink); }
  .sub { font: 500 26px/1.3 var(--sans); color: var(--ink-2); }
  .sub b { color: var(--ink); font-weight: 600; }
  .row { display: flex; gap: 10px; margin-top: 40px; align-items: center; min-height: 44px; }
  .sq { width: 44px; height: 44px; border-radius: 6px; flex: 0 0 auto; }
  .sq.w { background: var(--pos); }
  .sq.l { box-shadow: inset 0 0 0 3px var(--neg); }
  .row .cap { margin-left: 14px; font: 500 18px/1.2 var(--sans); color: var(--ink-2); }
  .cta { margin-top: 28px; display: flex; align-items: center; justify-content: space-between;
         gap: 24px; padding: 18px 22px; background: var(--panel); border: 1px solid var(--line);
         border-radius: var(--r-lg); }
  .cta .t { font: 500 18px/1.35 var(--sans); color: var(--ink-2); }
  .cta .t b { color: var(--ink); font-weight: 600; }
  .cta .link { font: 700 24px/1 var(--mono); color: var(--ink); white-space: nowrap; }
  .card .rr-foot { margin-top: 28px; }
  .warn { margin-top: 10px; font: 500 14px/1.4 var(--sans); color: var(--ink-2); }
"""

_MYCARD_BODY = Template("""
<div class="card">
  <div class="head">
    <div class="who"><span class="rr-wm">RSI Range</span><span class="rr-handle">@rsi_range</span></div>
    <div class="rr-label">Мой результат · по отметкам сделок</div>
  </div>
  <div class="hero">
    <div class="rr-label">Мой итог · R до комиссий</div>
    <div class="num $num_class" style="font-size:${num_px}px">$total</div>
    <div class="sub"><b>$trades_words</b> · winrate $winrate</div>
    <div class="row">$squares</div>
  </div>
  <div class="cta"><div class="t">Каждая сделка — в публичном журнале:<br><b>и плюсы, и минусы, без отбора</b></div>
    <div class="link">t.me/$bot</div></div>
  $foot
  <div class="warn">Личная выборка сделок — итог может отличаться от стратегии.</div>
</div>
""")


def build_my_card_html(data: dict) -> str:
    """HTML личного итога (payments.taken_stats + ключ bot). Чистая — без браузера."""
    last = list(data.get("last", []))
    squares = "".join(f'<div class="sq {"w" if r > 0 else "l"}"></div>' for r in last)
    if last:
        squares += f'<div class="cap">последние {len(last)}</div>'
    total = data["total_r"]
    text = _signed(total, 1)
    trades = int(data["trades"])
    bot = data.get("bot") or "rsirange_bot"
    body = _MYCARD_BODY.substitute(
        num_class={"positive": "pos", "negative": "neg"}.get(_sign_class(total, 1), "zero"),
        num_px=_hero_px(text, 1080 - 2 * 56 - 16, 300),
        total=text,
        trades_words=f"{trades} {_plural(trades, 'закрытая сделка', 'закрытые сделки', 'закрытых сделок')}",
        winrate=f"{round((data.get('winrate') or 0) * 100)}%",
        squares=squares,
        bot=_esc(bot),
        foot=_foot("канал @rsi_range", bot=bot),
    )
    return f"<style>{brand_css()}{_MYCARD_STYLE}</style>{body}"


def render_my_card(data: dict) -> bytes:
    """PNG личного итога (payments.taken_stats + ключ bot). Только через to_thread."""
    return render_html_to_png(build_my_card_html(data), width=1080, selector=".card")


# ---------------------------------------------------------------------------
# Карта сигнала: свечи + зоны входа/стопа/тейков (SVG внутри той же HTML-темы).
# mplfinance сознательно не используется — matplotlib забракован эстетически
# (см. шапку файла); свечи рисуются своим SVG в стиле карточек.
# ---------------------------------------------------------------------------

_CHART_STYLE = """
  .wrap { gap: 22px; }
  .chart-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; }
  .chart-title { display: flex; align-items: center; gap: 14px; min-width: 0; }
  .chart-symbol { font: 700 var(--t-d1)/1 var(--mono); letter-spacing: -.02em; color: var(--ink);
                  white-space: nowrap; }
  .chart-quote { font: 400 24px/1 var(--mono); color: var(--ink-2); margin: 0 6px 0 -8px;
                 align-self: flex-end; padding-bottom: 5px; }
  .side-pill { display: inline-flex; align-items: center; padding: 8px 12px; border-radius: var(--r);
               font: 700 16px/1 var(--mono); letter-spacing: .08em; }
  .side-pill.long { background: var(--pos-bg); color: var(--pos); box-shadow: inset 0 0 0 1px rgba(31,158,110,.55); }
  .side-pill.short { background: var(--neg-bg); color: var(--neg); box-shadow: inset 0 0 0 1px rgba(246,70,93,.55); }
  .chart-tf { font: 700 16px/1 var(--mono); color: var(--ink-2); padding: 8px 11px; border-radius: var(--r);
              box-shadow: inset 0 0 0 1px var(--line); }
  .chart-left { min-width: 0; }
  .chart-meta { font: 500 16px/1.4 var(--sans); color: var(--ink-2); text-align: right; }
  .chart-meta.left { text-align: left; margin-top: 12px; }
  .chart-res { text-align: right; }
  .chart-res .v { font: 700 60px/1 var(--mono); letter-spacing: -.03em; margin-top: 6px; }
  .chart-res .v.positive { color: var(--pos); } .chart-res .v.negative { color: var(--neg); }
  .chart-res .v.neutral { color: var(--ink); }
  .chart-card { overflow: hidden; }
  /* SVG рисуется ровно во внутреннюю ширину карточки (_CHART_W); width:100% — страховка:
     при любом изменении полей теги цены сожмутся вместе с графиком, а не вылезут за край */
  .chart-card svg { display: block; width: 100%; height: auto; }
  svg .ck { font: 400 13px var(--mono); fill: #8a94a1; }
  svg .tg-l { font: 700 12px var(--sans); letter-spacing: .07em; }
  svg .tg-p { font: 700 15px var(--mono); }
  svg .mk-l { font: 600 12px var(--sans); letter-spacing: .06em; fill: #8a94a1; }
"""

_CHART_BODY = Template("""
<div class="canvas"><div class="wrap">
  $head
  <div class="chart-head">
    <div class="chart-left">
      <div class="chart-title">
        <span class="chart-symbol">$base</span><span class="chart-quote">/$quote</span>
        <span class="side-pill $side_class">$side</span>
        <span class="chart-tf">$timeframe</span>
      </div>
      $sub
    </div>
    $right
  </div>
  <div class="card chart-card">$svg</div>
  $foot
</div></div>
""")

_GREEN, _RED, _INDIGO = POS, NEG, INK          # имена прежней палитры (совместимость)


def _price_label(x: float) -> str:
    """Полная запись цены без экспоненты и потери точности ({:g} режет BTC>100k)."""
    return f"{float(x):.8f}".rstrip("0").rstrip(".")


def _nice_step(span: float, n: int = 5) -> float:
    """«Круглый» шаг сетки цен (1/2/2.5/5 × 10^k), дающий около n линий на диапазоне.
    Общий для карты сигнала и карты раунда игры."""
    raw = span / max(n, 1)
    if raw <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _tick_text(v: float, step: float) -> str:
    k = math.floor(math.log10(step) + 1e-9)
    mant = round(step / 10 ** k, 6)
    dec = max(0, -k) + (1 if mant == 2.5 and k <= 0 else 0)
    return f"{v:.{dec}f}"


def _spread(ys: list, gap: float, lo: float, hi: float) -> list:
    """Развести подписи по вертикали (не наезжают друг на друга), сохраняя порядок;
    lo/hi — границы центров. Классическая 1D-раскладка: вниз, потом назад вверх."""
    order = sorted(range(len(ys)), key=lambda i: ys[i])
    pos = [min(max(ys[i], lo), hi) for i in order]
    for k in range(1, len(pos)):
        pos[k] = max(pos[k], pos[k - 1] + gap)
    if pos and pos[-1] > hi:
        pos[-1] = hi
        for k in range(len(pos) - 2, -1, -1):
            pos[k] = min(pos[k], pos[k + 1] - gap)
    out = [0.0] * len(ys)
    for k, i in enumerate(order):
        out[i] = pos[k]
    return out


def _chart_svg(candles: list, entry: float, stop: float, tps: list,
               w: int = _CHART_W, h: int = 540, gutter: int | None = None, *,
               exit_price: float | None = None, result_r: float | None = None,
               exit_index: int | None = None, signal_index: int | None = None) -> str:
    """SVG свечного графика в стиле терминала: сетка цены с круглыми шагами, зоны риска
    (вход↔стоп) и цели (вход↔дальний TP), уровни линиями и цветными тегами на оси цены
    (теги разводятся, не наезжают). candles: [(o,h,l,c), ...]. Необязательный исход:
    exit_price/result_r — пунктир и тег «ВЫХОД»; exit_index — точка выхода на свече;
    signal_index — свеча публикации (зоны начинаются с неё). Чистая функция."""
    n = len(candles)
    lv = [("ВХОД", float(entry), INK, None), ("СТОП", float(stop), NEG, "6 5")]
    lv += [(f"TP{i}" if len(tps) > 1 else "ТЕЙК", float(tp), POS, "6 5")
           for i, tp in enumerate(tps, 1)]
    if exit_price is not None:
        # цена выхода (трейл) считается с хвостом знаков — показываем с точностью уровней
        dec = max(len(_price_label(p).partition(".")[2]) for _n, p, _c, _d in lv)
        exit_price = round(float(exit_price), dec)
    tag_w = max(len(name) * 8.2 + len(_price_label(p)) * 9.0 for name, p, _c, _d in lv) + 30
    if exit_price is not None:
        tag_w = max(tag_w, 5 * 8.2 + len(_price_label(exit_price)) * 9.0 + 30)
    gutter = gutter or int(min(max(tag_w + 22, 150), 210))
    left, top, bottom = 18, 18, 18
    plot_r = w - gutter
    prices = [p for _n, p, _c, _d in lv] + ([float(exit_price)] if exit_price is not None else [])
    lo = min(min(c[2] for c in candles), *prices)
    hi = max(max(c[1] for c in candles), *prices)
    pad = (hi - lo) * 0.06 or abs(hi) * 0.01 or 1.0
    lo, hi = lo - pad, hi + pad

    def y(p: float) -> float:
        return round(top + (hi - p) / (hi - lo) * (h - top - bottom), 2)

    slot = (plot_r - left) / n

    def x(i: float) -> float:
        return round(left + i * slot + slot / 2, 2)

    parts = [f'<rect x="0" y="0" width="{w}" height="{h}" fill="{PANEL}"/>']
    # сетка цены (круглые шаги) + редкие вертикали — фактура терминала
    step = _nice_step(hi - lo)
    ticks = []
    t = math.ceil(lo / step) * step
    while t <= hi:
        ticks.append(t)
        t += step
    for tv in ticks:
        parts.append(f'<line x1="{left}" y1="{y(tv)}" x2="{plot_r}" y2="{y(tv)}" '
                     f'stroke="{GRID}" stroke-width="1"/>')
    every = max(4, round(96 / slot))                    # ~каждые 96px, как бы ни было баров
    for i in range(every, n, every):
        xx = round(left + i * slot, 2)
        parts.append(f'<line x1="{xx}" y1="{top}" x2="{xx}" y2="{h - bottom}" '
                     f'stroke="{GRID}" stroke-width="1"/>')
    parts.append(f'<line x1="{plot_r}" y1="0" x2="{plot_r}" y2="{h}" stroke="{LINE}"/>')
    # зоны риска / цели — от свечи публикации (если известна) до правого края
    x0 = left if signal_index is None else max(left, round(x(signal_index) - slot / 2, 2))
    far_tp = max(tps, key=lambda tp: abs(tp - entry))
    for a, b, color, op in ((entry, stop, NEG, 0.11), (entry, far_tp, POS, 0.09)):
        t0, t1 = sorted((y(a), y(b)))
        parts.append(f'<rect class="zone" x="{x0}" y="{t0}" width="{plot_r - x0:.2f}" '
                     f'height="{max(t1 - t0, 1):.2f}" fill="{color}" fill-opacity="{op}"/>')
    if signal_index is not None:
        parts.append(f'<line x1="{x0}" y1="{top}" x2="{x0}" y2="{h - bottom}" stroke="{INK2}" '
                     f'stroke-opacity=".55" stroke-dasharray="3 4"/>'
                     f'<text class="mk-l" x="{x0 + 8}" y="{top + 14}">СИГНАЛ</text>')
    # свечи
    bw = max(slot * 0.64, 2.0)
    ww = 1.5 if slot >= 8 else 1.0
    for i, (o, hi_, lo_, c) in enumerate(candles):
        color = POS if c >= o else NEG
        cx = x(i)
        parts.append(f'<line x1="{cx}" y1="{y(hi_)}" x2="{cx}" y2="{y(lo_)}" '
                     f'stroke="{color}" stroke-width="{ww}"/>')
        t0, t1 = sorted((y(o), y(c)))
        parts.append(f'<rect class="c-body" x="{cx - bw / 2:.2f}" y="{t0}" width="{bw:.2f}" '
                     f'height="{max(t1 - t0, 1.4):.2f}" rx="1" fill="{color}"/>')
    # уровни: линии на всю ширину плота
    for _name, price, color, dash in lv:
        da = f' stroke-dasharray="{dash}"' if dash else ""
        parts.append(f'<line x1="{left}" y1="{y(price)}" x2="{plot_r}" y2="{y(price)}" '
                     f'stroke="{color}" stroke-width="1.6" stroke-opacity=".9"{da}/>')
    ex_color = INK2
    if exit_price is not None:
        ex_color = POS if (result_r or 0) > 0 else NEG if (result_r or 0) < 0 else INK2
        parts.append(f'<line x1="{left}" y1="{y(exit_price)}" x2="{plot_r}" '
                     f'y2="{y(exit_price)}" stroke="{ex_color}" stroke-width="1.4" '
                     f'stroke-dasharray="2 4"/>')
    if exit_price is not None and exit_index is not None and 0 <= exit_index < n:
        parts.append(f'<circle cx="{x(exit_index)}" cy="{y(exit_price)}" r="7" '
                     f'fill="{ex_color}" stroke="{PANEL}" stroke-width="3"/>')
    # теги на оси цены (сплошные, тёмный текст) — разведены по вертикали
    tag_h, tx = 28, plot_r + 10
    tags = [(name, price, color, "pill") for name, price, color, _d in lv]
    if exit_price is not None:
        tags.append(("ВЫХОД", float(exit_price), ex_color, "tag-exit"))
    want = [y(p) for _n, p, _c, _k in tags]
    got = _spread(want, tag_h + 4, tag_h / 2 + 2, h - tag_h / 2 - 2)
    tw = w - tx - 8
    for tv in ticks:                                    # подписи сетки — где нет тегов
        if all(abs(y(tv) - g) > tag_h / 2 + 8 for g in got) and top < y(tv) < h - bottom:
            parts.append(f'<text class="ck" x="{tx + 4}" y="{y(tv) + 4.5}">'
                         f'{_tick_text(tv, step)}</text>')
    for (name, price, color, cls), yw, yg in zip(tags, want, got):
        if abs(yw - yg) > 1:
            parts.append(f'<path d="M{plot_r} {yw} L{tx - 3} {yg}" stroke="{color}" '
                         f'stroke-width="1.2" fill="none"/>')
        if cls == "pill":
            box, ink = f'fill="{color}"', BG
        else:                                           # выход — контурный тег
            box = f'fill="{PANEL}" stroke="{color}" stroke-width="1.5"'
            ink = color
        parts.append(
            f'<rect class="{cls}" x="{tx}" y="{yg - tag_h / 2:.2f}" width="{tw:.0f}" '
            f'height="{tag_h}" rx="5" {box}/>'
            f'<text x="{tx + 10}" y="{yg + 5.2:.2f}"><tspan class="tg-l" fill="{ink}" '
            f'fill-opacity=".72">{name}</tspan><tspan class="tg-p" dx="7" fill="{ink}">'
            f'{_price_label(price)}</tspan></text>')
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>')


def build_signal_chart_html(data: dict) -> str:
    """data: {signal_id, symbol, side ('LONG'/'SHORT'), timeframe, entry, stop,
    tps: list[float], candles: [(o,h,l,c),...], rr, confidence_pct, updated_label}
    -> заполненный HTML. Необязательно (исход закрытой сделки): result_r, exit_price,
    exit_index, signal_index — см. _chart_svg. Чистая функция — без Playwright/браузера."""
    result_r = data.get("result_r")
    svg = _chart_svg(data["candles"], data["entry"], data["stop"], list(data["tps"]),
                     exit_price=data.get("exit_price"), result_r=result_r,
                     exit_index=data.get("exit_index"), signal_index=data.get("signal_index"))
    n = len(data["candles"])
    meta = _esc(f"R/R 1:{data['rr']:.1f} · уверенность {data['confidence_pct']:.0f}% · "
                f"{n} {_plural(n, 'закрытый бар', 'закрытых бара', 'закрытых баров')}")
    if result_r is None:
        right, sub = f'<div class="chart-meta">{meta}</div>', ""
    else:                                       # исход — герой справа, план — строкой под тикером
        right = (f'<div class="chart-res"><div class="rr-label">Итог сделки</div>'
                 f'<div class="v {_sign_class(result_r, 2)}">{_signed(result_r, 2)}</div></div>')
        sub = f'<div class="chart-meta left">{meta}</div>'
    base, quote = _pair(data["symbol"])
    body = _CHART_BODY.substitute(
        head=_brand_head("Карта сигнала", f"Сигнал #{int(data['signal_id'])}"),
        base=_esc(base),
        quote=_esc(quote or "USDT"),
        side=_esc(data["side"]),
        side_class="long" if data["side"] == "LONG" else "short",
        timeframe=_esc(data["timeframe"]),
        right=right,
        sub=sub,
        svg=svg,
        foot=_foot(_esc(data["updated_label"]), extra=" · риск 1–2% на сделку"),
    )
    return f"<style>{brand_css()}{_STYLE}{_CHART_STYLE}</style>{body}"


def render_signal_chart(data: dict) -> bytes:
    return render_html_to_png(build_signal_chart_html(data), selector=".canvas")


# ---------------------------------------------------------------------------
# Карта раунда игры «Лонг или шорт?» (app/delivery/game.py): свечи BTC за 48 ч, линия
# «сейчас» и зона «через 24 ч» справа. Та же тема, что у карты сигнала, — но без уровней
# входа/стопа: это вопрос, а не сетап.
# ---------------------------------------------------------------------------

def spaced_price(x: float) -> str:
    """Цена с пробелами по тысячам и без хвостовых нулей: 68123.4 -> '68 123.4'. Один формат
    в подписи раунда, на карте и в итоге — юзер сравнивает одни и те же цифры."""
    return f"{float(x):,.2f}".rstrip("0").rstrip(".").replace(",", " ")


_GAME_W = _CHART_W - 2 * 26                  # внутренняя ширина .game-card (поля 26px)


def _game_svg(bars: list, ref_price: float, w: int = _GAME_W, h: int = 520,
              gutter: int = 170, future_frac: float = 0.2) -> str:
    """SVG карты раунда: свечи истории, линия «сейчас» через всю ширину и зона «+24 ч» справа
    (выше линии — зелёная «▲ выше», ниже — красная «▼ ниже»). bars: [(ts_ms, o, h, l, c), …].
    w = внутренняя ширина .game-card: шире — пилюля цены вылезла бы за карточку (плюс
    страховка width:100% в CSS). Чистая функция — тестируется без браузера."""
    from datetime import datetime, timezone

    axis_h = 30
    plot_h = h - axis_h
    plot_w = w - gutter
    hist_w = round(plot_w * (1 - future_frac), 2)
    lo = min(min(b[3] for b in bars), ref_price)
    hi = max(max(b[2] for b in bars), ref_price)
    pad = (hi - lo) * 0.08 or max(abs(ref_price) * 0.001, 1e-9)
    lo, hi = lo - pad, hi + pad
    # «сейчас» — последний close, часто это экстремум окна: без запаса одна из зон будущего
    # схлопнулась бы в полоску и подпись «выше»/«ниже» легла бы на линию
    span = hi - lo
    hi = max(hi, ref_price + 0.22 * span)
    lo = min(lo, ref_price - 0.22 * span)

    def y(p: float) -> float:
        return round((hi - p) / (hi - lo) * (plot_h - 20) + 10, 2)

    yr = y(ref_price)
    fut_w = round(plot_w - hist_w, 2)
    fx = round(hist_w + fut_w / 2, 2)
    parts = [
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="{PANEL}"/>',
        f'<rect class="future" x="{hist_w}" y="0" width="{fut_w}" height="{yr}" '
        f'fill="{POS}" fill-opacity="0.09"/>',
        f'<rect class="future" x="{hist_w}" y="{yr}" width="{fut_w}" '
        f'height="{round(plot_h - yr, 2)}" fill="{NEG}" fill-opacity="0.09"/>',
        f'<text class="gz" x="{fx}" y="{round(yr / 2 + 8, 2)}" text-anchor="middle" '
        f'fill="{POS}">▲ выше</text>',
        f'<text class="gz" x="{fx}" y="{round(yr + (plot_h - yr) / 2 + 8, 2)}" '
        f'text-anchor="middle" fill="{NEG}">▼ ниже</text>',
        f'<text class="gl" x="{round(plot_w - 12, 2)}" y="24" text-anchor="end">+24 ч</text>',
    ]
    # сетка цен: тонкие линии + подписи в правом жёлобе (возле «сейчас» — не мешаем тегу)
    step = _nice_step(hi - lo, n=6)
    tick = (lo // step + 1) * step
    while tick < hi:
        yy = y(tick)
        parts.append(f'<line class="grid" x1="0" y1="{yy}" x2="{plot_w}" y2="{yy}" '
                     f'stroke="{GRID}" stroke-width="1"/>')
        if abs(yy - yr) > 24:
            parts.append(f'<text class="ck" x="{plot_w + 14}" y="{round(yy + 4.5, 2)}">'
                         f'{spaced_price(tick)}</text>')
        tick += step
    parts.append(f'<line x1="{plot_w}" y1="0" x2="{plot_w}" y2="{plot_h}" stroke="{LINE}"/>')
    # граница «сейчас | будущее»
    parts.append(f'<line x1="{hist_w}" y1="0" x2="{hist_w}" y2="{plot_h}" stroke="{INK2}" '
                 f'stroke-width="1.2" stroke-dasharray="4 5" stroke-opacity="0.7"/>')
    # свечи
    slot = hist_w / len(bars)
    bw = max(slot * 0.64, 2.0)
    for i, (ts, o, hi_, lo_, c) in enumerate(bars):
        color = POS if c >= o else NEG
        cx = round(i * slot + slot / 2, 2)
        parts.append(f'<line x1="{cx}" y1="{y(hi_)}" x2="{cx}" y2="{y(lo_)}" '
                     f'stroke="{color}" stroke-width="1.5"/>')
        top, bot = min(y(o), y(c)), max(y(o), y(c))
        parts.append(f'<rect class="c-body" x="{cx - bw / 2:.2f}" y="{top}" width="{bw:.2f}" '
                     f'height="{max(bot - top, 1.4):.2f}" rx="1" fill="{color}"/>')
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        if dt.hour in (0, 12) and 44 < cx < hist_w - 44:      # подписи у краёв обрезались бы
            parts.append(f'<text class="tick ck" x="{cx}" y="{h - 9}" text-anchor="middle">'
                         f'{dt:%d.%m %H:%M}</text>')
    # линия «сейчас» поверх свечей + тег с ценой (сплошной, как теги уровней карты сигнала)
    ly = min(max(yr - 14, 2), plot_h - 30)
    parts.append(f'<line x1="0" y1="{yr}" x2="{plot_w}" y2="{yr}" stroke="{INK}" '
                 f'stroke-width="1.6" stroke-opacity="0.9"/>')
    parts.append(
        f'<rect class="pill" x="{plot_w + 8}" y="{ly:.2f}" width="{gutter - 12}" height="28" '
        f'rx="5" fill="{INK}"/>'
        f'<text class="gp" x="{plot_w + 18}" y="{ly + 19.2:.2f}" fill="{BG}">'
        f'Сейчас {spaced_price(ref_price)}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>')


_GAME_STYLE = """
  .game-card { padding: 24px 26px 16px; }
  .game-card svg { display: block; width: 100%; height: auto; }
  .game-q { font: 700 31px/1.22 var(--sans); letter-spacing: -.012em; color: var(--ink); }
  .game-q .px { white-space: nowrap; font-variant-numeric: tabular-nums; }
  .game-meta { display: flex; align-items: center; justify-content: space-between; gap: 16px;
               margin: 16px 0 14px; }
  .game-sym { display: flex; align-items: center; gap: 12px; }
  .sym-pill { padding: 8px 11px; border-radius: var(--r); font: 700 15px/1 var(--mono);
              letter-spacing: .04em; color: var(--ink); box-shadow: inset 0 0 0 1px var(--line); }
  .game-tf { font: 500 15px/1 var(--sans); color: var(--ink-2); }
  .game-stats { font: 500 15px/1 var(--sans); color: var(--ink-2); white-space: nowrap; }
  .game-stats .v { font-family: var(--mono); color: var(--ink); word-spacing: -.3em; }
  .game-stats .pos { color: var(--pos); } .game-stats .neg { color: var(--neg); }
  .votes { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .vote { border-radius: var(--r-lg); padding: 16px 20px 15px; text-align: center;
          font: 700 25px/1 var(--mono); letter-spacing: .06em; }
  .vote small { display: block; font: 500 14px/1.2 var(--sans); margin-top: 9px; letter-spacing: 0;
                color: var(--ink-2); }
  .vote.up { background: var(--pos-bg); color: var(--pos); box-shadow: inset 0 0 0 1px rgba(31,158,110,.55); }
  .vote.down { background: var(--neg-bg); color: var(--neg); box-shadow: inset 0 0 0 1px rgba(246,70,93,.55); }
  .game-note { margin-top: -10px; text-align: center; font: 500 15px/1.3 var(--sans);
               color: var(--ink-2); }
  .game-note b { color: var(--ink); font-weight: 600; }
  svg text { font-family: var(--sans); }
  /* моно-пробел разрядов («82 000») шире цифры — поджимаем, чтобы число читалось целым */
  svg .ck { font: 400 13px var(--mono); fill: #8a94a1; word-spacing: -3px; }
  svg .gz { font: 700 23px var(--sans); }
  svg .gl { font: 600 13px var(--sans); letter-spacing: .06em; fill: #8a94a1; }
  svg .gp { font: 700 15px var(--mono); word-spacing: -3px; }
"""

_GAME_BODY = Template("""
<div class="canvas"><div class="wrap">
  $head
  <div class="card game-card">
    <div class="game-q">BTC через 24 часа будет выше или ниже, чем сейчас — <span class="px">$ref</span>?</div>
    <div class="game-meta">
      <div class="game-sym"><span class="sym-pill">$symbol</span><span class="game-tf">1ч · последние $hours ч</span></div>
      <div class="game-stats">$stats</div>
    </div>
    $svg
  </div>
  <div class="votes">
    <div class="vote up">▲ ВЫШЕ<small>кнопка «Выше» под картинкой</small></div>
    <div class="vote down">▼ НИЖЕ<small>кнопка «Ниже» под картинкой</small></div>
  </div>
  <div class="game-note">Игра на интуицию — <b>не торговый сигнал</b></div>
  $foot
</div></div>
""")


def build_game_chart_html(data: dict) -> str:
    """data: {day_label, symbol, bars: [(ts_ms,o,h,l,c), …], ref_price, result_label}
    -> заполненный HTML карты раунда. Чистая функция — тестируется без Playwright."""
    bars = [tuple(b) for b in data["bars"]]
    if not bars:
        raise ValueError("карта раунда без свечей")
    first_open, ref = float(bars[0][1]), float(data["ref_price"])
    move = (ref - first_open) / first_open * 100 if first_open else 0.0
    stats = (f'{len(bars)} ч: <span class="{_sign_class(move, 2)[:3]}">'
             f'{_signed(move, 2, "%")}</span> · '
             f'макс <span class="v">{spaced_price(max(b[2] for b in bars))}</span> · '
             f'мин <span class="v">{spaced_price(min(b[3] for b in bars))}</span>')
    body = _GAME_BODY.substitute(
        head=_brand_head("Игра дня · Лонг или шорт?", f"Раунд {_esc(data['day_label'])}"),
        ref=spaced_price(ref),
        symbol=_esc(data["symbol"]),
        hours=len(bars),
        stats=stats,
        svg=_game_svg(bars, ref),
        foot=_foot(f"итог {_esc(data['result_label'])} · цена Bybit"),
    )
    return f"<style>{brand_css()}{_STYLE}{_GAME_STYLE}</style>{body}"


def render_game_chart(data: dict) -> bytes:
    """PNG карты раунда. Тяжёлый Playwright — только через to_thread (bot._render_png)."""
    return render_html_to_png(build_game_chart_html(data), selector=".canvas")


# ---------------------------------------------------------------------------
# Дашборд трек-рекорда: самодостаточная страница (эквити-кривая + разбивки).
# Отдаётся файлом (как verifier.html) — работает офлайн, без сервера/туннеля;
# PNG того же документа — фолбэк кнопки «Дашборд» без Mini App. Ноль внешних
# запросов: шрифты встроены (brand_css), скриптов нет.
# ---------------------------------------------------------------------------

_DASH_CSS = """
  body { padding: 24px 12px; display: flex; justify-content: center; min-height: 100vh; }
  .wrap { width: 100%; max-width: 900px; display: flex; flex-direction: column; gap: 10px; }
  .top { display: flex; align-items: center; justify-content: space-between; gap: 16px;
         padding: 4px 2px 12px; border-bottom: 1px solid var(--line); margin-bottom: 4px; }
  .top .rr-wm { font-size: 16px; } .top .rr-wm::before { width: 12px; height: 12px; }
  .top .sub { font: 500 12px/1.3 var(--sans); color: var(--ink-2); text-align: right; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: var(--r);
          padding: 14px 16px; }
  .hero { display: grid; grid-template-columns: 1.1fr 1fr; padding: 0; overflow: hidden; }
  .hero-main { padding: 18px 18px 16px; display: flex; flex-direction: column; justify-content: center; }
  .hero-num { font: 700 64px/.95 var(--mono); letter-spacing: -.04em; margin: 10px 0 8px; }
  .hero-sub { font: 500 13px/1.4 var(--sans); color: var(--ink-2); }
  .hero-side { border-left: 1px solid var(--line); display: flex; flex-direction: column; }
  .mrow { flex: 1; display: flex; justify-content: space-between; align-items: center; gap: 12px;
          padding: 8px 16px; border-top: 1px solid var(--grid); }
  .mrow:first-child { border-top: none; }
  .mrow span { font: 500 12px/1.3 var(--sans); color: var(--ink-2); }
  .mrow b { font: 700 17px/1 var(--mono); white-space: nowrap; }
  .positive { color: var(--pos); } .negative { color: var(--neg); } .neutral { color: var(--ink); }
  h3 { font: 600 11px/1.2 var(--sans); color: var(--ink-2); margin-bottom: 10px; letter-spacing: .12em;
       text-transform: uppercase; }
  svg { width: 100%; height: auto; display: block; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; font: 600 10px/1.2 var(--sans); letter-spacing: .1em; text-transform: uppercase;
       color: var(--ink-2); padding: 6px 8px; }
  td { padding: 7px 8px; border-top: 1px solid var(--grid); font: 400 13px/1.2 var(--mono); }
  td b { font-weight: 700; color: var(--ink); } td .q { color: var(--ink-3); }
  th.r, td.r { text-align: right; }
  tr.gap td { color: var(--ink-2); font: 500 12px/1.2 var(--sans); text-align: center;
              letter-spacing: .02em; }
  details { margin-top: 10px; border-top: 1px solid var(--line); padding-top: 10px; }
  summary { cursor: pointer; font: 600 12px/1.2 var(--sans); color: var(--ink-2); letter-spacing: .04em; }
  .bar-track { height: 6px; border-radius: 3px; background: var(--grid); overflow: hidden; margin-top: 10px; }
  .bar-fill { height: 100%; border-radius: 3px; background: var(--acc); }
  .ms { font: 500 14px/1.4 var(--sans); color: var(--ink); }
  .ms b { font-weight: 700; }
  .self { font: 500 11px/1.4 var(--sans); color: var(--ink-2); text-align: center; padding: 2px 0 6px; }
  .dash-empty { color: var(--ink-2); font: 500 13px/1.4 var(--sans); padding: 22px 0; text-align: center; }
  .note { font: 500 12.5px/1.55 var(--sans); color: var(--ink-2); }
  .note b { color: var(--ink); }
  .wrap .rr-foot { font-size: 11.5px; padding-top: 12px; margin-top: 4px; }
  .wrap .rr-foot .rr-stamp { font-size: 11px; }
  @media (max-width: 620px) {
    .hero { grid-template-columns: 1fr; }
    .hero-side { border-left: none; border-top: 1px solid var(--line); }
    .hero-num { font-size: 52px; }
    .wrap .rr-foot { flex-direction: column; align-items: flex-start; gap: 4px; }
  }
"""

_DASH_DOC = Template("""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RSI Range — дашборд трек-рекорда</title><style>$css</style></head>
<body><div class="wrap">
  <div class="top"><div class="rr-wm">RSI Range · терминал</div>
    <div class="sub">@rsi_range · $period<br>офлайн-снимок</div></div>
  <div class="card hero">
    <div class="hero-main"><div class="rr-label">Итог · R до комиссий</div>
      <div class="hero-num $total_class">$total_r</div>
      <div class="hero-sub">$closed закрытых · $wins побед · $losses пораж.</div></div>
    <div class="hero-side">
      <div class="mrow"><span>Матожидание на сделку</span><b class="$exp_class">$exp_r</b></div>
      <div class="mrow"><span>Winrate</span><b class="neutral">$winrate%</b></div>
      <div class="mrow"><span>Чистый ход, без плеча</span><b class="$move_class">$move_pct</b></div>
      <div class="mrow"><span>Просадка max от пика</span><b class="negative">$max_dd</b></div>
      <div class="mrow"><span>В работе</span><b class="neutral">$open</b></div>
    </div>
  </div>
  <div class="card"><h3>Эквити-кривая (накопленный R)</h3>$equity</div>
  <div class="card"><h3>По инструментам</h3>$by_symbol</div>
  <div class="card"><h3>Прогресс до следующей вехи</h3>$milestone</div>
  <div class="card note"><b>Честно:</b> публикуем 100% исходов, включая убыточные.
    Бэктест устойчивого края не показал — это инструмент и проверяемая история,
    не обещание прибыли. Каждая сделка — в CSV с sha256 (/trackrecord).
    Не финансовый совет.</div>
  $foot
  <div class="self">Страница самодостаточна: без внешних скриптов и запросов.</div>
</div></body></html>""")


def _equity_svg(series: list, w: int = 860, h: int = 240, pad: int = 18) -> str:
    """Инлайн-SVG накопленного R по закрытым сделкам (нулевая база пунктиром, заливка по
    знаку итога, подпись итога с тёмным ореолом — не тонет в линии)."""
    if not series:
        return '<div class="dash-empty">Эквити-кривая появится после первых закрытых сделок.</div>'
    cum, run = [], 0.0
    for r in series:
        run += r
        cum.append(run)
    pts = [(0, 0.0)] + [(i, v) for i, v in enumerate(cum, start=1)]
    n = len(pts) - 1
    lo, hi = min(0.0, min(cum)), max(0.0, max(cum))
    span = (hi - lo) or 1.0
    top = pad + 16                                       # место под подпись итога

    def x(i: float) -> float:
        return round(pad + i / n * (w - 2 * pad), 2)

    def y(v: float) -> float:
        return round(h - pad - (v - lo) / span * (h - pad - top), 2)

    zero = y(0.0)
    line = " ".join(f"{x(i)},{y(v)}" for i, v in pts)
    area = f"{x(0)},{zero} {line} {x(n)},{zero}"
    color = POS if cum[-1] >= 0 else NEG
    end_x, end_y = x(n), y(cum[-1])
    ly = end_y - 14 if end_y - 14 > 14 else end_y + 26
    return (
        f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">'
        f'<polygon points="{area}" fill="{color}" fill-opacity="0.10"/>'
        f'<line x1="{pad}" y1="{zero}" x2="{w - pad}" y2="{zero}" '
        f'stroke="{ZERO}" stroke-width="1" stroke-dasharray="4 4"/>'
        f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="2.4" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{end_x}" cy="{end_y}" r="4.5" fill="{color}" stroke="{BG}" stroke-width="2"/>'
        f'<text x="{end_x - 4}" y="{ly:.0f}" text-anchor="end" font-size="15" font-weight="700" '
        f'fill="{color}" stroke="{BG}" stroke-width="5" paint-order="stroke" '
        f'stroke-linejoin="round" font-family="RR Mono, IBM Plex Mono, ui-monospace, monospace">'
        f'{_signed(cum[-1], 1)}</text></svg>'
    )


def _dash_rows(items: list) -> str:
    return "".join(
        f"<tr><td title='{_esc(k)}'><b>{_esc(_pair(k)[0])}</b>"
        f"<span class='q'>/{_esc(_pair(k)[1] or 'USDT')}</span></td>"
        f"<td class='r'>{s['trades']}</td><td class='r'>{s['winrate'] * 100:.0f}%</td>"
        f"<td class='r {_sign_class(s['expectancy_r'], 2)}'>{_signed(s['expectancy_r'], 2)}</td>"
        f"<td class='r {_sign_class(s['total_r'], 1)}'>{_signed(s['total_r'], 1)}</td></tr>"
        for k, s in items)


def _dash_table(d: dict, head: int = 6, tail: int = 6) -> str:
    """Разбивка по символам: сильнейшие И слабейшие поровну (не витрина плюсов), полный
    список — раскрывашкой ниже. Пусто -> заглушка."""
    if not d:
        return '<div class="dash-empty">Данных пока нет.</div>'
    items = sorted(d.items(), key=lambda kv: kv[1]["total_r"], reverse=True)
    thead = ("<thead><tr><th>Инструмент</th><th class='r'>Сделок</th><th class='r'>WR</th>"
             "<th class='r'>Эксп.</th><th class='r'>Итог</th></tr></thead>")
    if len(items) <= head + tail + 1:
        return f"<table>{thead}<tbody>{_dash_rows(items)}</tbody></table>"
    hidden = len(items) - head - tail
    gap = (f"<tr class='gap'><td colspan='5'>··· ещё {hidden} "
           f"{_plural(hidden, 'инструмент', 'инструмента', 'инструментов')} — "
           f"полный список ниже ···</td></tr>")
    return (f"<table>{thead}<tbody>{_dash_rows(items[:head])}{gap}"
            f"{_dash_rows(items[-tail:])}</tbody></table>"
            f"<details><summary>Все {len(items)} "
            f"{_plural(len(items), 'инструмент', 'инструмента', 'инструментов')}</summary>"
            f"<table>{thead}<tbody>{_dash_rows(items)}</tbody></table></details>")


def _dash_milestone(current: int, nxt) -> str:
    if not nxt:
        return '<div class="dash-empty">Все вехи взяты — новые появятся с ростом истории.</div>'
    pct = min(round(current / nxt * 100), 100)
    return (f"<div class='ms'><b>{current} из {nxt}</b> закрытых сделок — впереди веха {nxt}.</div>"
            f'<div class="bar-track"><div class="bar-fill" style="width:{pct}%"></div></div>')


def build_og_card_html(total_r: float, trades: int, equity: list) -> str:
    """Карточка 1200x630 для og:image дашборда (бренд v5). Рендерится ботом раз в сутки ->
    data/og.png, отдаёт webapp по /og.png. Композиция по центру: компактное превью
    ссылки в Telegram режет картинку в квадрат по центру — итог должен остаться в кадре."""
    color = POS if total_r >= 0 else NEG
    sign = "+" if total_r > 0 else _MINUS if total_r < 0 else ""
    words = _plural(int(trades), "закрытая сделка", "закрытые сделки", "закрытых сделок")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>{brand_css()}
html,body{{width:1200px;height:630px;overflow:hidden}}
.card{{position:relative;width:1200px;height:630px;padding:38px 48px 28px;display:flex;
  flex-direction:column;align-items:center;background:var(--bg)}}
.top{{display:flex;align-items:center;gap:16px}}
.top .rr-wm{{font-size:24px}} .top .rr-wm::before{{width:18px;height:18px}}
.top .live{{font:600 15px/1 var(--sans);letter-spacing:.14em;text-transform:uppercase;
  color:var(--ink-2);padding-left:16px;border-left:1px solid var(--line)}}
.num{{font:700 156px/.9 var(--mono);letter-spacing:-.05em;color:{color};margin-top:30px;
  white-space:nowrap}}
.sub{{font:500 25px/1.3 var(--sans);color:var(--ink-2);margin-top:16px;text-align:center}}
.sub b{{color:var(--ink);font-weight:600}}
.eq{{width:100%;margin-top:18px;flex:1;min-height:0;display:flex;align-items:flex-end}}
.eq svg{{width:100%;height:100%}}
.card .rr-foot{{width:100%;font-size:18px;padding-top:14px}}
.card .rr-foot .rr-stamp{{font-size:17px}}
</style></head><body><div class="card">
<div class="top"><span class="rr-wm">RSI Range</span><span class="live">Живой терминал</span></div>
<div class="num">{sign}{abs(total_r):.1f}R</div>
<div class="sub">итог за всю историю · <b>{trades} {words}</b> · каждая в проверяемом журнале</div>
<div class="eq">{_equity_svg(equity, w=1104, h=196, pad=10)}</div>
{_foot("CSV + SHA-256 · минусы не скрываются · 18+")}
</div></body></html>"""


def render_og_card(total_r: float, trades: int, equity: list) -> bytes:
    return render_html_to_png(build_og_card_html(total_r, trades, equity),
                              width=1200, scale=1, selector=".card")


def build_dashboard_html(data: dict) -> str:
    """Самодостаточный HTML-дашборд. data: {period_label, updated_label, stats, equity,
    by_symbol, milestone:{current,next}}. Чистая функция — тестируется без браузера.
    Разбивки по сетапам НЕТ (граница публикации стратегии — таксономия сетапов не
    отдаётся никому; ревью 14.08 нашло эту поверхность пропущенной при зачистке)."""
    st = data["stats"]
    run = peak = dd = 0.0
    for r in data["equity"]:
        run += r
        peak = max(peak, run)
        dd = max(dd, peak - run)
    move = st.get("total_move_pct")
    return _DASH_DOC.substitute(
        css=brand_css() + _DASH_CSS,
        move_pct=(_signed(move, 0, "%") if move is not None else "—"),
        move_class=_sign_class(move or 0.0, 0),
        max_dd=(f"{_MINUS}{dd:.1f}R" if dd > 0 else "0.0R"),
        period=_esc(data["period_label"]),
        foot=_foot(f"Обновлено {_esc(data['updated_label'])}"),
        total_r=_signed(st["total_r"], 1), total_class=_sign_class(st["total_r"], 1),
        exp_r=_signed(st["expectancy_r"], 2), exp_class=_sign_class(st["expectancy_r"], 2),
        winrate=f"{st['winrate'] * 100:.0f}",
        wins=st["wins"], losses=st["losses"], closed=st["trades"], open=st["open"],
        equity=_equity_svg(data["equity"]),
        by_symbol=_dash_table(data["by_symbol"]),
        milestone=_dash_milestone(data["milestone"]["current"], data["milestone"]["next"]),
    )


def render_dashboard_png(data: dict) -> bytes:
    """Дашборд -> PNG (для инлайн-превью/визуального QA). Тяжёлый Playwright — через to_thread."""
    return render_html_to_png(build_dashboard_html(data), width=920)
