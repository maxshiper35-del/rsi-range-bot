"""
graphics.py — рендер PNG-карточек для канала (карточка недели трек-рекорда).

HTML/CSS -> PNG через headless Chromium (Playwright). Дизайн выбран пользователем из трёх
параллельно сгенерированных вариантов (matplotlib для первой попытки был забракован —
даёт эстетику научного графика, не настоящий дизайн; HTML/CSS + Playwright даёт настоящую
типографику и раскладку).

render_html_to_png поднимает/закрывает целый headless-браузер на каждый вызов — тяжёлая
синхронная операция. Вызывать ТОЛЬКО через asyncio.to_thread, никогда напрямую из event loop
(см. использование в bot.py:_maintenance). Приемлемо на еженедельной частоте, не для
частого вызова.
"""
from __future__ import annotations

from string import Template

_CARD_WIDTH = 1200

_STYLE = """
  * { margin: 0; padding: 0; box-sizing: border-box; }

  html, body {
    width: 1200px;
    background: #0B0D11;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    -webkit-font-smoothing: antialiased;
  }

  .canvas {
    position: relative;
    width: 1200px;
    background:
      radial-gradient(ellipse 900px 500px at 50% -8%, rgba(90,110,150,0.14), rgba(90,110,150,0) 60%),
      linear-gradient(180deg, #0D0F14 0%, #0B0D11 55%, #0A0C10 100%);
    color: #EDEFF3;
  }

  .wrap {
    padding: 40px 44px 30px 44px;
    display: flex;
    flex-direction: column;
    gap: 18px;
  }

  .header { display: flex; align-items: center; justify-content: space-between; }
  .brand { display: flex; align-items: center; gap: 14px; }

  .mark {
    width: 44px; height: 44px; border-radius: 12px;
    background: linear-gradient(155deg, #4C6EF5, #7C5CFC);
    display: flex; align-items: center; justify-content: center;
    font-weight: 800; font-size: 20px; color: #F5F6FA; letter-spacing: -0.5px;
    box-shadow: 0 6px 16px rgba(76,110,245,0.28);
  }

  .brand-text .name { font-size: 20px; font-weight: 700; color: #F5F6F8; letter-spacing: -0.2px; line-height: 1.15; }
  .brand-text .handle { font-size: 14px; font-weight: 500; color: #6B7280; margin-top: 2px; }

  .header-right { text-align: right; }
  .header-right .eyebrow { font-size: 11.5px; font-weight: 700; letter-spacing: 0.11em; color: #6B7280; text-transform: uppercase; }
  .header-right .period { font-size: 17px; font-weight: 600; color: #C7CBD4; margin-top: 5px; font-variant-numeric: tabular-nums; }

  .divider { height: 1px; background: rgba(255,255,255,0.08); flex-shrink: 0; }

  .stats { display: grid; grid-template-columns: 1.15fr 1fr 1fr; gap: 16px; }

  .card {
    background: linear-gradient(180deg, rgba(255,255,255,0.035), rgba(255,255,255,0.012));
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 18px;
    padding: 20px 24px;
  }

  .stat-label { font-size: 11.5px; font-weight: 700; letter-spacing: 0.09em; text-transform: uppercase; color: #767C88; }
  .stat-value { font-size: 46px; font-weight: 800; letter-spacing: -1.2px; line-height: 1.05; margin-top: 10px; font-variant-numeric: tabular-nums; }
  .stat-value.positive { color: #4ADE9C; }
  .stat-value.negative { color: #F16565; }
  .stat-value.neutral { color: #F1F2F5; }
  .stat-sub { font-size: 13px; color: #868C97; margin-top: 8px; font-weight: 500; }

  .bar-track { margin-top: 12px; height: 6px; border-radius: 4px; background: rgba(255,255,255,0.07); overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 4px; background: linear-gradient(90deg, #34C77B, #4ADE9C); }

  .unit-card { padding: 20px 24px 22px; }
  .unit-head { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 16px; }
  .unit-title { font-size: 14px; font-weight: 700; color: #D6D9E0; }
  .unit-title .muted { color: #6B7280; font-weight: 600; }

  .legend { display: flex; gap: 18px; }
  .legend-item { display: flex; align-items: center; gap: 7px; font-size: 13px; font-weight: 600; color: #A6ABB5; }
  .dot { width: 8px; height: 8px; border-radius: 50%; }
  .dot.win { background: #4ADE9C; }
  .dot.loss { background: #F16565; }

  .units { display: flex; flex-wrap: wrap; gap: 8px; }
  .unit {
    width: 64px; height: 46px; flex: 0 0 auto;
    border-radius: 9px; display: flex; align-items: center; justify-content: center;
    font-size: 12px; font-weight: 800;
  }
  .unit.win { background: rgba(74,222,156,0.16); border: 1px solid rgba(74,222,156,0.4); color: #6FE3AC; }
  .unit.loss { background: rgba(241,101,101,0.14); border: 1px solid rgba(241,101,101,0.4); color: #F58787; }

  .unit-empty { font-size: 14px; color: #767C88; padding: 12px 0; }

  .unit-foot { display: flex; justify-content: space-between; margin-top: 14px; font-size: 12.5px; color: #6B7280; font-weight: 500; }

  .bottom { display: grid; grid-template-columns: 1.55fr 1fr; gap: 16px; }

  .honesty { display: flex; flex-direction: column; justify-content: center; gap: 14px; padding: 38px 34px; }
  .honesty-label { display: flex; align-items: center; gap: 8px; font-size: 11.5px; font-weight: 700; letter-spacing: 0.09em; text-transform: uppercase; color: #7C8CF8; }
  .honesty-label .icon { width: 16px; height: 16px; border-radius: 50%; background: rgba(124,140,248,0.16); display: flex; align-items: center; justify-content: center; font-size: 10px; color: #9BA8FA; }
  .honesty p { font-size: 17.5px; line-height: 1.55; color: #D4D7DE; font-weight: 500; max-width: 560px; }
  .honesty p b { color: #F1F2F5; font-weight: 700; }

  .warning {
    border-color: rgba(224,166,74,0.28);
    background: linear-gradient(180deg, rgba(224,166,74,0.09), rgba(224,166,74,0.03));
    display: flex; flex-direction: column; justify-content: center; gap: 14px; padding: 38px 30px;
  }
  .warning-label { display: flex; align-items: center; gap: 8px; font-size: 11.5px; font-weight: 700; letter-spacing: 0.09em; text-transform: uppercase; color: #E0A64A; }
  .warning-label .icon { width: 16px; height: 16px; border-radius: 5px; background: rgba(224,166,74,0.18); display: flex; align-items: center; justify-content: center; font-size: 10px; font-weight: 800; color: #E0A64A; }
  .warning p { font-size: 15.5px; line-height: 1.55; color: #C7CBD4; font-weight: 500; }
  .warning p b { color: #EFC783; font-weight: 700; }

  .footer { display: flex; align-items: center; justify-content: space-between; padding-top: 2px; margin-top: auto; }
  .footer-left { font-size: 12.5px; color: #565C68; font-weight: 500; }
  .footer-left b { color: #8B92A5; font-weight: 700; }
  .footer-right { font-size: 12.5px; color: #565C68; font-weight: 500; }
"""

_BODY = Template("""
<div class="canvas"><div class="wrap">

  <div class="header">
    <div class="brand">
      <div class="mark">R</div>
      <div class="brand-text">
        <div class="name">RSI Range Signals</div>
        <div class="handle">@rsi_range</div>
      </div>
    </div>
    <div class="header-right">
      <div class="eyebrow">Отчёт за неделю</div>
      <div class="period">$period</div>
    </div>
  </div>

  <div class="divider"></div>

  <div class="stats">
    <div class="card">
      <div class="stat-label">Итог за период</div>
      <div class="stat-value $total_r_class">$total_r</div>
      <div class="stat-sub">$closed закрытых сделок · $open ещё в работе</div>
    </div>
    <div class="card">
      <div class="stat-label">Winrate</div>
      <div class="stat-value neutral">$winrate%</div>
      <div class="stat-sub">$wins побед · $losses поражений</div>
      <div class="bar-track"><div class="bar-fill" style="width: $winrate%;"></div></div>
    </div>
    <div class="card">
      <div class="stat-label">Матожидание / сделку</div>
      <div class="stat-value $expectancy_class">$expectancy</div>
      <div class="stat-sub">среднее по $closed закрытым</div>
    </div>
  </div>

  <div class="card unit-card">
    <div class="unit-head">
      <div class="unit-title">$closed закрытых сделок <span class="muted">— каждая учтена</span></div>
      <div class="legend">
        <div class="legend-item"><span class="dot win"></span>$wins побед</div>
        <div class="legend-item"><span class="dot loss"></span>$losses поражений</div>
      </div>
    </div>
    $units_html
    <div class="unit-foot">
      <span>Порядок соответствует хронологии закрытия сделок</span>
      <span>Полный CSV трек-рекорд — в закреплённом сообщении канала</span>
    </div>
  </div>

  <div class="bottom">
    <div class="card honesty">
      <div class="honesty-label"><span class="icon">&check;</span>Почему мы публикуем убытки</div>
      <p>Мы показываем <b>100% исходов</b> — прибыльные и убыточные сделки в равной степени. Если статистика ухудшится, вы увидите это здесь первыми, а не узнаете постфактум.</p>
    </div>
    <div class="card warning">
      <div class="warning-label"><span class="icon">!</span>Малая выборка</div>
      <p>Выборка мала (<b>$closed сделок</b>) — для статистической значимости нужно <b>100+</b>. Не делайте выводов о долгосрочном edge на этих цифрах.</p>
    </div>
  </div>

  <div class="footer">
    <div class="footer-left"><b>RSI Range Signals</b> — сигналы без прикрас</div>
    <div class="footer-right">Обновлено $updated</div>
  </div>

</div></div>
""")


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


def build_weekly_card_html(data: dict) -> str:
    """data: {period_label, total_r, closed, open, winrate_pct, wins, losses, expectancy_r,
    outcomes: list[bool] (хронологически, True=win), updated_label} -> заполненный HTML.

    Чистая функция — тестируется без Playwright/браузера."""
    if data["outcomes"]:
        units_html = '<div class="units">' + "".join(
            f'<div class="unit {"win" if w else "loss"}">{"W" if w else "L"}</div>'
            for w in data["outcomes"]) + "</div>"
    else:
        units_html = '<div class="unit-empty">На этой неделе ещё нет закрытых сделок.</div>'

    body = _BODY.substitute(
        period=_esc(data["period_label"]),
        total_r=f"{data['total_r']:+.1f}R",
        total_r_class=_r_class(data["total_r"]),
        closed=data["closed"],
        open=data["open"],
        winrate=f"{data['winrate_pct']:.0f}",
        wins=data["wins"],
        losses=data["losses"],
        expectancy=f"{data['expectancy_r']:+.2f}R",
        expectancy_class=_r_class(data["expectancy_r"]),
        units_html=units_html,
        updated=_esc(data["updated_label"]),
    )
    return f"<style>{_STYLE}</style>{body}"


def render_html_to_png(html: str, width: int = _CARD_WIDTH, scale: int = 2,
                       selector: str | None = None) -> bytes:
    """Синхронный рендер (Playwright sync API) — вызывать ТОЛЬКО из потока
    (asyncio.to_thread), никогда напрямую из event loop. Высота — intrinsic по контенту
    (full_page=True), не зафиксирована: юнит-чарт может перенестись на несколько строк
    в активную неделю без обрезания карточки. selector — скриншот ОДНОГО элемента
    (личная карточка ниже вьюпорта — full_page дорисовал бы пустое поле до 800px)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(device_scale_factor=scale)
            page.set_viewport_size({"width": width, "height": 800})
            page.set_content(html)
            page.wait_for_timeout(100)
            if selector:
                return page.locator(selector).screenshot()
            return page.screenshot(full_page=True)
        finally:
            browser.close()


def render_weekly_card(data: dict) -> bytes:
    return render_html_to_png(build_weekly_card_html(data))


# ---------------------------------------------------------------------------
# Персональная карточка «мой итог» — шеринг подписчиком своего результата по
# отметкам сделок. Терминальная эстетика дашборда v3 (моно, hairline, тёмный
# фон, сине-красная пара). Без уровней и сетапов — только личные агрегаты.
# ---------------------------------------------------------------------------

_MYCARD_STYLE = """
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#121211; font-family:"SF Mono", ui-monospace, Menlo, Consolas, monospace;
         color:#e8e6e1; -webkit-font-smoothing:antialiased; }
  .card { width:1080px; padding:64px 72px 52px; background:
          radial-gradient(1100px 480px at 18% -12%, rgba(57,135,229,.10), transparent 60%), #121211; }
  .head { display:flex; justify-content:space-between; align-items:baseline;
          border-bottom:1px solid #2a2927; padding-bottom:22px; }
  .brand { font-size:26px; font-weight:700; letter-spacing:.28em; }
  .tag { font-size:15px; color:#8a877f; letter-spacing:.05em; }
  .num { font-size:150px; font-weight:700; letter-spacing:-.02em; margin:46px 0 8px;
         font-variant-numeric:tabular-nums; }
  .pos { color:#3987e5; } .neg { color:#e66767; }
  .sub { font-size:20px; color:#8a877f; }
  .row { display:flex; gap:10px; margin-top:42px; align-items:center; min-height:34px; }
  .sq { width:34px; height:34px; border-radius:7px; }
  .foot { display:flex; justify-content:space-between; margin-top:52px;
          border-top:1px solid #2a2927; padding-top:22px; font-size:15px; color:#8a877f; }
  .foot b { color:#c9c6bf; font-weight:600; }
  .warn { margin-top:10px; font-size:13px; color:#5f5c55; }
"""

_MYCARD_BODY = Template("""
<div class="card">
  <div class="head"><div class="brand">RSI RANGE</div>
    <div class="tag">мой результат · по отметкам сделок</div></div>
  <div class="num $num_class">$total</div>
  <div class="sub">$trades закрытых · winrate $winrate</div>
  <div class="row">$squares</div>
  <div class="foot"><div>канал: <b>@rsi_range</b></div><div>бот: <b>t.me/$bot</b></div></div>
  <div class="warn">Не финансовый совет. Личная выборка сделок — итог может отличаться от стратегии.</div>
</div>
""")


def render_my_card(data: dict) -> bytes:
    """PNG личного итога (payments.taken_stats + ключ bot). Только через to_thread."""
    squares = "".join(
        f'<div class="sq" style="background:{"#3987e5" if r > 0 else "#e66767"}"></div>'
        for r in data.get("last", []))
    total = data["total_r"]
    body = _MYCARD_BODY.substitute(
        num_class="pos" if total >= 0 else "neg",
        total=f"{total:+.1f}R",
        trades=data["trades"],
        winrate=f"{round((data.get('winrate') or 0) * 100)}%",
        squares=squares,
        bot=_esc(data.get("bot") or "rsirange_bot"),
    )
    return render_html_to_png(f"<style>{_MYCARD_STYLE}</style>{body}", width=1080,
                              selector=".card")


# ---------------------------------------------------------------------------
# График сигнала: свечи + зоны входа/стопа/тейков (SVG внутри той же HTML-темы).
# mplfinance сознательно не используется — matplotlib забракован эстетически
# (см. шапку файла); свечи рисуются своим SVG в стиле карточки недели.
# ---------------------------------------------------------------------------

_CHART_STYLE = """
  .chart-card { padding: 22px 24px 18px; }
  .chart-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
  .chart-title { display: flex; align-items: center; gap: 12px; }
  .side-pill {
    padding: 6px 14px; border-radius: 9px; font-size: 14px; font-weight: 800; letter-spacing: 0.04em;
  }
  .side-pill.long { background: rgba(74,222,156,0.14); border: 1px solid rgba(74,222,156,0.45); color: #6FE3AC; }
  .side-pill.short { background: rgba(241,101,101,0.13); border: 1px solid rgba(241,101,101,0.45); color: #F58787; }
  .chart-symbol { font-size: 19px; font-weight: 700; color: #F1F2F5; letter-spacing: -0.2px; }
  .chart-tf { font-size: 14px; font-weight: 600; color: #6B7280; }
  .chart-meta { font-size: 13.5px; font-weight: 600; color: #A6ABB5; font-variant-numeric: tabular-nums; }
  svg text { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
"""

_CHART_BODY = Template("""
<div class="canvas"><div class="wrap">

  <div class="header">
    <div class="brand">
      <div class="mark">R</div>
      <div class="brand-text">
        <div class="name">RSI Range Signals</div>
        <div class="handle">@rsi_range</div>
      </div>
    </div>
    <div class="header-right">
      <div class="eyebrow">Карта сигнала</div>
      <div class="period">Сигнал #$signal_id</div>
    </div>
  </div>

  <div class="divider"></div>

  <div class="card chart-card">
    <div class="chart-head">
      <div class="chart-title">
        <span class="side-pill $side_class">$side</span>
        <span class="chart-symbol">$symbol</span>
        <span class="chart-tf">$timeframe</span>
      </div>
      <div class="chart-meta">$meta</div>
    </div>
    $svg
  </div>

  <div class="footer">
    <div class="footer-left">⚠️ Не финансовый совет · риск 1–2% на сделку</div>
    <div class="footer-right">@rsi_range · $updated</div>
  </div>

</div></div>
""")

_GREEN, _RED, _INDIGO = "#4ADE9C", "#F16565", "#9BA8FA"


def _price_label(x: float) -> str:
    """Полная запись цены без экспоненты и потери точности ({:g} режет BTC>100k)."""
    return f"{float(x):.8f}".rstrip("0").rstrip(".")


def _chart_svg(candles: list, entry: float, stop: float, tps: list,
               w: int = 1112, h: int = 560, gutter: int = 176) -> str:
    """SVG свечного графика с зонами: красная entry<->stop, зелёная entry<->дальний TP,
    уровни пунктиром + подписи-пилюли в правом жёлобе. candles: [(o,h,l,c), ...].
    Чистая функция — тестируется без браузера."""
    plot_w = w - gutter
    levels = [entry, stop, *tps]
    lo = min(min(c[2] for c in candles), *levels)
    hi = max(max(c[1] for c in candles), *levels)
    pad = (hi - lo) * 0.06 or 1.0
    lo, hi = lo - pad, hi + pad

    def y(p: float) -> float:
        return round((hi - p) / (hi - lo) * (h - 24) + 12, 2)

    parts = []
    # зоны риска/цели на всю ширину плота (под свечами)
    far_tp = max(tps, key=lambda t: abs(t - entry))
    for a, b, color, op in ((entry, stop, _RED, 0.09), (entry, far_tp, _GREEN, 0.07)):
        top, bot = min(y(a), y(b)), max(y(a), y(b))
        parts.append(f'<rect class="zone" x="0" y="{top}" width="{plot_w}" '
                     f'height="{max(bot - top, 1):.2f}" fill="{color}" fill-opacity="{op}"/>')
    # свечи
    slot = plot_w / len(candles)
    bw = max(slot * 0.62, 2.0)
    for i, (o, hi_, lo_, c) in enumerate(candles):
        color = _GREEN if c >= o else _RED
        cx = round(i * slot + slot / 2, 2)
        parts.append(f'<line x1="{cx}" y1="{y(hi_)}" x2="{cx}" y2="{y(lo_)}" '
                     f'stroke="{color}" stroke-width="1.4" stroke-opacity="0.85"/>')
        top, bot = min(y(o), y(c)), max(y(o), y(c))
        parts.append(f'<rect class="c-body" x="{cx - bw / 2:.2f}" y="{top}" width="{bw:.2f}" '
                     f'height="{max(bot - top, 1.4):.2f}" rx="1.5" fill="{color}"/>')
    # уровни: вход сплошным, стоп/тейки пунктиром + подписи-пилюли справа
    labels = [("Вход", entry, _INDIGO, "none"), ("Стоп", stop, _RED, "6 5")]
    labels += [(f"TP{i}" if len(tps) > 1 else "Тейк", tp, _GREEN, "6 5")
               for i, tp in enumerate(tps, 1)]
    for name, price, color, dash in labels:
        yy = y(price)
        dash_attr = f' stroke-dasharray="{dash}"' if dash != "none" else ""
        parts.append(f'<line x1="0" y1="{yy}" x2="{plot_w}" y2="{yy}" stroke="{color}" '
                     f'stroke-width="1.6" stroke-opacity="0.9"{dash_attr}/>')
        ly = min(max(yy - 14, 4), h - 32)               # пилюля не вылезает за края
        parts.append(
            f'<rect class="pill" x="{plot_w + 10}" y="{ly:.2f}" width="{gutter - 16}" height="28" '
            f'rx="8" fill="{color}" fill-opacity="0.13" stroke="{color}" stroke-opacity="0.45"/>'
            f'<text x="{plot_w + 22}" y="{ly + 19:.2f}" font-size="13.5" font-weight="700" '
            f'fill="{color}">{name}  {_price_label(price)}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>')


def build_signal_chart_html(data: dict) -> str:
    """data: {signal_id, symbol, side ('LONG'/'SHORT'), timeframe, entry, stop,
    tps: list[float], candles: [(o,h,l,c),...], rr, confidence_pct, updated_label}
    -> заполненный HTML. Чистая функция — тестируется без Playwright/браузера."""
    svg = _chart_svg(data["candles"], data["entry"], data["stop"], list(data["tps"]))
    body = _CHART_BODY.substitute(
        signal_id=int(data["signal_id"]),
        side=_esc(data["side"]),
        side_class="long" if data["side"] == "LONG" else "short",
        symbol=_esc(data["symbol"]),
        timeframe=_esc(data["timeframe"]),
        meta=_esc(f"R/R 1:{data['rr']:.1f} · уверенность {data['confidence_pct']:.0f}% · "
                  f"{len(data['candles'])} закрытых баров"),
        svg=svg,
        updated=_esc(data["updated_label"]),
    )
    return f"<style>{_STYLE}{_CHART_STYLE}</style>{body}"


def render_signal_chart(data: dict) -> bytes:
    return render_html_to_png(build_signal_chart_html(data))


# ---------------------------------------------------------------------------
# Карта раунда игры «Лонг или шорт?» (app/delivery/game.py): свечи BTC за 48 ч, линия
# «сейчас» и зона «через 24 ч» справа. Та же тема, что у карты сигнала, — но без уровней
# входа/стопа: это вопрос, а не сетап.
# ---------------------------------------------------------------------------

def spaced_price(x: float) -> str:
    """Цена с пробелами по тысячам и без хвостовых нулей: 68123.4 -> '68 123.4'. Один формат
    в подписи раунда, на карте и в итоге — юзер сравнивает одни и те же цифры."""
    return f"{float(x):,.2f}".rstrip("0").rstrip(".").replace(",", " ")


def _nice_step(span: float, n: int = 4) -> float:
    """«Круглый» шаг сетки цен (1/2/2.5/5 × 10^k), дающий около n линий на диапазоне."""
    import math

    raw = span / max(n, 1)
    if raw <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _game_svg(bars: list, ref_price: float, w: int = 1060, h: int = 520,
              gutter: int = 170, future_frac: float = 0.2) -> str:
    """SVG карты раунда: свечи истории, линия «сейчас» через всю ширину и зона «+24 ч» справа
    (выше линии — зелёная «▲ выше», ниже — красная «▼ ниже»). bars: [(ts_ms, o, h, l, c), …].
    w = внутренняя ширина .game-card (1112 − 2×26): шире — пилюля цены вылезает за карточку.
    Чистая функция — тестируется без браузера."""
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
        f'<rect class="future" x="{hist_w}" y="0" width="{fut_w}" height="{yr}" '
        f'fill="{_GREEN}" fill-opacity="0.08"/>',
        f'<rect class="future" x="{hist_w}" y="{yr}" width="{fut_w}" '
        f'height="{round(plot_h - yr, 2)}" fill="{_RED}" fill-opacity="0.08"/>',
        f'<text x="{fx}" y="{round(yr / 2 + 8, 2)}" text-anchor="middle" font-size="22" '
        f'font-weight="800" fill="{_GREEN}">▲ выше</text>',
        f'<text x="{fx}" y="{round(yr + (plot_h - yr) / 2 + 8, 2)}" text-anchor="middle" '
        f'font-size="22" font-weight="800" fill="{_RED}">▼ ниже</text>',
        f'<text x="{round(plot_w - 10, 2)}" y="22" text-anchor="end" font-size="13" '
        f'font-weight="700" fill="#A6ABB5">+24 ч</text>',
    ]
    # сетка цен: тонкие линии + подписи в правом жёлобе (возле «сейчас» — не мешаем пилюле)
    step = _nice_step(hi - lo, n=6)
    tick = (lo // step + 1) * step
    while tick < hi:
        yy = y(tick)
        parts.append(f'<line class="grid" x1="0" y1="{yy}" x2="{plot_w}" y2="{yy}" '
                     f'stroke="#FFFFFF" stroke-opacity="0.05" stroke-width="1"/>')
        if abs(yy - yr) > 24:
            parts.append(f'<text x="{plot_w + 14}" y="{round(yy + 4.5, 2)}" font-size="12.5" '
                         f'font-weight="600" fill="#565C68">{spaced_price(tick)}</text>')
        tick += step
    # граница «сейчас | будущее»
    parts.append(f'<line x1="{hist_w}" y1="0" x2="{hist_w}" y2="{plot_h}" stroke="{_INDIGO}" '
                 f'stroke-width="1.4" stroke-dasharray="5 5" stroke-opacity="0.6"/>')
    # свечи
    slot = hist_w / len(bars)
    bw = max(slot * 0.62, 2.0)
    for i, (ts, o, hi_, lo_, c) in enumerate(bars):
        color = _GREEN if c >= o else _RED
        cx = round(i * slot + slot / 2, 2)
        parts.append(f'<line x1="{cx}" y1="{y(hi_)}" x2="{cx}" y2="{y(lo_)}" '
                     f'stroke="{color}" stroke-width="1.4" stroke-opacity="0.85"/>')
        top, bot = min(y(o), y(c)), max(y(o), y(c))
        parts.append(f'<rect class="c-body" x="{cx - bw / 2:.2f}" y="{top}" width="{bw:.2f}" '
                     f'height="{max(bot - top, 1.4):.2f}" rx="1.5" fill="{color}"/>')
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        if dt.hour in (0, 12) and 44 < cx < hist_w - 44:      # подписи у краёв обрезались бы
            parts.append(f'<text class="tick" x="{cx}" y="{h - 9}" text-anchor="middle" '
                         f'font-size="12.5" font-weight="600" fill="#6B7280">'
                         f'{dt:%d.%m %H:%M}</text>')
    # линия «сейчас» поверх свечей + пилюля с ценой
    ly = min(max(yr - 14, 2), plot_h - 30)
    parts.append(f'<line x1="0" y1="{yr}" x2="{plot_w}" y2="{yr}" stroke="{_INDIGO}" '
                 f'stroke-width="1.8" stroke-opacity="0.95"/>')
    parts.append(
        f'<rect class="pill" x="{plot_w + 8}" y="{ly:.2f}" width="{gutter - 12}" height="28" '
        f'rx="8" fill="{_INDIGO}" fill-opacity="0.14" stroke="{_INDIGO}" stroke-opacity="0.5"/>'
        f'<text x="{plot_w + 18}" y="{ly + 19:.2f}" font-size="13.5" font-weight="700" '
        f'fill="{_INDIGO}">Сейчас {spaced_price(ref_price)}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>')


_GAME_STYLE = """
  .game-card { padding: 24px 26px 18px; }
  .game-q { font-size: 31px; font-weight: 800; letter-spacing: -0.6px; line-height: 1.22; color: #F1F2F5; }
  .game-q .px { color: #B8C2FB; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .game-meta { display: flex; align-items: center; justify-content: space-between; margin: 14px 0 12px; }
  .game-sym { display: flex; align-items: center; gap: 10px; }
  .sym-pill { padding: 6px 12px; border-radius: 9px; font-size: 14px; font-weight: 800; letter-spacing: 0.03em;
              background: rgba(155,168,250,0.12); border: 1px solid rgba(155,168,250,0.42); color: #B8C2FB; }
  .game-tf { font-size: 14px; font-weight: 600; color: #6B7280; }
  .game-stats { font-size: 13.5px; font-weight: 600; color: #A6ABB5; font-variant-numeric: tabular-nums; }
  .game-stats .pos { color: #6FE3AC; }
  .game-stats .neg { color: #F58787; }
  .votes { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .vote { border-radius: 16px; padding: 15px 20px; text-align: center; font-size: 21px; font-weight: 800; letter-spacing: 0.03em; }
  .vote small { display: block; font-size: 13px; font-weight: 600; margin-top: 4px; opacity: 0.75; letter-spacing: 0; }
  .vote.up { background: rgba(74,222,156,0.11); border: 1px solid rgba(74,222,156,0.42); color: #6FE3AC; }
  .vote.down { background: rgba(241,101,101,0.11); border: 1px solid rgba(241,101,101,0.42); color: #F58787; }
  svg text { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
"""

_GAME_BODY = Template("""
<div class="canvas"><div class="wrap">

  <div class="header">
    <div class="brand">
      <div class="mark">R</div>
      <div class="brand-text">
        <div class="name">RSI Range Signals</div>
        <div class="handle">@rsirange_bot</div>
      </div>
    </div>
    <div class="header-right">
      <div class="eyebrow">Игра дня · Лонг или шорт?</div>
      <div class="period">Раунд $day</div>
    </div>
  </div>

  <div class="divider"></div>

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

  <div class="footer">
    <div class="footer-left">Игра на интуицию — <b>не торговый сигнал</b></div>
    <div class="footer-right">Цена Bybit · итог $result</div>
  </div>

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
    cls = "pos" if move > 0 else "neg" if move < 0 else ""
    stats = (f'{len(bars)} ч: <span class="{cls}">{move:+.2f}%</span> · '
             f'макс {spaced_price(max(b[2] for b in bars))} · '
             f'мин {spaced_price(min(b[3] for b in bars))}')
    body = _GAME_BODY.substitute(
        day=_esc(data["day_label"]),
        ref=spaced_price(ref),
        symbol=_esc(data["symbol"]),
        hours=len(bars),
        stats=stats,
        svg=_game_svg(bars, ref),
        result=_esc(data["result_label"]),
    )
    return f"<style>{_STYLE}{_GAME_STYLE}</style>{body}"


def render_game_chart(data: dict) -> bytes:
    """PNG карты раунда. Тяжёлый Playwright — только через to_thread (bot._render_png)."""
    return render_html_to_png(build_game_chart_html(data))


# ---------------------------------------------------------------------------
# Дашборд трек-рекорда: самодостаточная страница (эквити-кривая + разбивки).
# Отдаётся файлом (как verifier.html) — работает офлайн, без сервера/туннеля;
# тот же документ переиспользуется как страница Telegram Mini App, когда у бота
# появится публичный HTTPS-URL (runtime.webapp_url). Ноль внешних запросов.
# ---------------------------------------------------------------------------

_DASH_CSS = """
  /* v4 «биржевой терминал» — та же палитра, что Mini App (вкус владельца 14.08):
     тёмный всегда, панели с бордерами, моно-цифры, оранжевый бренд-акцент,
     зелёный/красный #1f9e6e/#f6465d. Без градиентов и карточек-облаков. */
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#0b0e11; color:#eaecef;
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         -webkit-font-smoothing:antialiased; padding:24px 12px; display:flex;
         justify-content:center; min-height:100vh;
         font-variant-numeric:tabular-nums; }
  .mono { font-family:ui-monospace,"SF Mono",Menlo,monospace; }
  .wrap { width:100%; max-width:900px; display:flex; flex-direction:column; gap:10px; }
  .brand { display:flex; align-items:center; gap:12px; padding:2px 2px 6px; }
  .mark { width:14px; height:14px; border-radius:3px; flex:0 0 auto; background:#f7a600; }
  .brand .name { font-size:16px; font-weight:700; letter-spacing:.08em;
                 font-family:ui-monospace,"SF Mono",Menlo,monospace; }
  .brand .sub { font-size:12px; color:#5c6672; margin-top:2px; }
  .tiles { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; }
  @media (max-width:620px){ .tiles{ grid-template-columns:repeat(2,1fr); } }
  .card { background:#14181d; border:1px solid #232a32; border-radius:8px; padding:13px 15px; }
  .tile .lbl { font-size:10px; font-weight:600; letter-spacing:.1em; text-transform:uppercase;
               color:#5c6672; }
  .tile .val { font-size:26px; font-weight:700; letter-spacing:-.5px; margin-top:5px;
               font-family:ui-monospace,"SF Mono",Menlo,monospace; }
  .positive { color:#1f9e6e; } .negative { color:#f6465d; } .neutral { color:#eaecef; }
  .tile .sub { font-size:11px; color:#5c6672; margin-top:3px; }
  h3 { font-size:11px; color:#8a94a1; margin-bottom:10px; letter-spacing:.1em;
       text-transform:uppercase; font-weight:600; }
  svg { width:100%; height:auto; display:block; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { text-align:left; font-size:10px; font-weight:600; letter-spacing:.08em;
       text-transform:uppercase; color:#5c6672; padding:5px 8px; }
  td { padding:6px 8px; border-top:1px solid #1e242b;
       font-family:ui-monospace,"SF Mono",Menlo,monospace; font-size:12.5px; }
  td.r { text-align:right; }
  .bar-track { height:6px; border-radius:3px; background:#1e242b; overflow:hidden; margin-top:10px; }
  .bar-fill { height:100%; border-radius:3px; background:#f7a600; }
  .dash-empty { color:#5c6672; font-size:13px; padding:22px 0; text-align:center; }
  .note { font-size:12.5px; line-height:1.55; color:#8a94a1; }
  .note b { color:#eaecef; }
  .foot { font-size:11px; color:#5c6672; text-align:center; padding-bottom:8px; }
"""

_DASH_DOC = Template("""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RSI Range Signals — дашборд трек-рекорда</title><style>$css</style></head>
<body><div class="wrap">
  <div class="brand"><div class="mark"></div><div>
    <div class="name">RSI RANGE · ТЕРМИНАЛ</div>
    <div class="sub">@rsi_range · $period · офлайн-снимок</div></div></div>
  <div class="tiles">
    <div class="card tile"><div class="lbl">Итог</div>
      <div class="val $total_class">$total_r</div><div class="sub">$closed закрытых</div></div>
    <div class="card tile"><div class="lbl">Матожидание</div>
      <div class="val $exp_class">$exp_r</div><div class="sub">на сделку</div></div>
    <div class="card tile"><div class="lbl">Winrate</div>
      <div class="val neutral">$winrate%</div><div class="sub">$wins побед · $losses пораж.</div></div>
    <div class="card tile"><div class="lbl">Чистый ход</div>
      <div class="val $move_class">$move_pct</div><div class="sub">забрано без плеча</div></div>
    <div class="card tile"><div class="lbl">Просадка max</div>
      <div class="val negative">−$max_dd</div><div class="sub">от пика эквити</div></div>
    <div class="card tile"><div class="lbl">В работе</div>
      <div class="val neutral">$open</div><div class="sub">открытых сигналов</div></div>
  </div>
  <div class="card"><h3>Эквити-кривая (накопленный R)</h3>$equity</div>
  <div class="card"><h3>По инструментам</h3>$by_symbol</div>
  <div class="card"><h3>Прогресс до следующей вехи</h3>$milestone</div>
  <div class="card note"><b>Честно:</b> публикуем 100% исходов, включая убыточные.
    Бэктест устойчивого края не показал — это инструмент и проверяемая история,
    не обещание прибыли. Каждая сделка — в CSV с sha256 (/trackrecord).
    Не финансовый совет.</div>
  <div class="foot">Обновлено $updated · страница самодостаточна: без внешних скриптов и запросов.</div>
</div></body></html>""")


def _equity_svg(series: list, w: int = 860, h: int = 240, pad: int = 18) -> str:
    """Инлайн-SVG накопленного R по закрытым сделкам (нулевая база, заливка по знаку)."""
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

    def x(i: float) -> float:
        return round(pad + i / n * (w - 2 * pad), 2)

    def y(v: float) -> float:
        return round(h - pad - (v - lo) / span * (h - 2 * pad), 2)

    zero = y(0.0)
    line = " ".join(f"{x(i)},{y(v)}" for i, v in pts)
    area = f"{x(0)},{zero} {line} {x(n)},{zero}"
    color = "#1f9e6e" if cum[-1] >= 0 else "#f6465d"
    end_x, end_y = x(n), y(cum[-1])
    return (
        f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">'
        f'<polygon points="{area}" fill="{color}" fill-opacity="0.10"/>'
        f'<line x1="{pad}" y1="{zero}" x2="{w - pad}" y2="{zero}" '
        f'stroke="#3a424c" stroke-width="1" stroke-dasharray="4 4"/>'
        f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="2.4" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{end_x}" cy="{end_y}" r="4" fill="{color}"/>'
        f'<text x="{end_x - 6}" y="{max(end_y - 10, 14):.0f}" text-anchor="end" '
        f'font-size="14" font-weight="700" fill="{color}" '
        f'font-family="-apple-system,sans-serif">{cum[-1]:+.1f}R</text></svg>'
    )


def _dash_table(d: dict) -> str:
    """Таблица разбивки (символы/сетапы) -> HTML tbody-строки; пусто -> заглушка."""
    if not d:
        return '<div class="dash-empty">Данных пока нет.</div>'
    rows = "".join(
        f"<tr><td>{_esc(k)}</td><td class='r'>{s['trades']}</td>"
        f"<td class='r'>{s['winrate'] * 100:.0f}%</td>"
        f"<td class='r {_r_class(s['expectancy_r'])}'>{s['expectancy_r']:+.2f}R</td>"
        f"<td class='r {_r_class(s['total_r'])}'>{s['total_r']:+.1f}R</td></tr>"
        for k, s in sorted(d.items(), key=lambda kv: kv[1]["total_r"], reverse=True))
    return ("<table><thead><tr><th>—</th><th class='r'>сд</th><th class='r'>WR</th>"
            "<th class='r'>эксп</th><th class='r'>итог</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>")


def _dash_milestone(current: int, nxt) -> str:
    if not nxt:
        return '<div class="dash-empty">Все вехи взяты — новые появятся с ростом истории.</div>'
    pct = min(round(current / nxt * 100), 100)
    return (f"<div>{current} из {nxt} закрытых сделок — впереди веха {nxt}.</div>"
            f'<div class="bar-track"><div class="bar-fill" style="width:{pct}%"></div></div>')


def build_og_card_html(total_r: float, trades: int, equity: list) -> str:
    """Карточка 1200x630 для og:image дашборда (палитра v4 «биржевой терминал»).
    Рендерится ботом раз в сутки -> data/og.png, отдаёт webapp по /og.png."""
    color = "#1f9e6e" if total_r >= 0 else "#f6465d"
    sign = "+" if total_r > 0 else "−" if total_r < 0 else ""
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
*{{margin:0;box-sizing:border-box}}
body{{width:1200px;height:630px;background:#0b0e11;color:#eaecef;
  font:26px/1.4 -apple-system,system-ui,sans-serif;font-variant-numeric:tabular-nums}}
.card{{width:1200px;height:630px;padding:48px 56px;display:flex;flex-direction:column}}
.head{{display:flex;align-items:center;gap:14px;font-family:ui-monospace,Menlo,monospace;
  font-weight:700;font-size:30px;letter-spacing:.12em}}
.head i{{width:16px;height:16px;background:#f7a600;border-radius:4px}}
.num{{font-family:ui-monospace,Menlo,monospace;font-weight:700;font-size:120px;
  line-height:1.05;color:{color};margin:18px 0 4px}}
.sub{{color:#8a94a1;font-size:27px}}
.eq{{margin-top:26px;background:#14181d;border:1px solid #232a32;border-radius:12px;
  padding:16px 14px 8px;flex:1}}
.eq svg{{width:100%;height:100%}}
.foot{{color:#5c6672;font-size:21px;margin-top:22px}}
</style></head><body><div class="card">
<div class="head"><i></i>RSI RANGE · ЖИВОЙ ТЕРМИНАЛ</div>
<div class="num">{sign}{abs(total_r):.1f}R</div>
<div class="sub">итог за всю историю · {trades} закрытых · каждая сделка в проверяемом журнале</div>
<div class="eq">{_equity_svg(equity, w=1080, h=250)}</div>
<div class="foot">CSV + SHA-256 · минусовые периоды не скрываются · не финансовый совет · 18+</div>
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
        css=_DASH_CSS,
        move_pct=(f"{move:+.0f}%" if move is not None else "—"),
        move_class=_r_class(move or 0.0),
        max_dd=f"{dd:.1f}R",
        period=_esc(data["period_label"]),
        updated=_esc(data["updated_label"]),
        total_r=f"{st['total_r']:+.1f}R", total_class=_r_class(st["total_r"]),
        exp_r=f"{st['expectancy_r']:+.2f}R", exp_class=_r_class(st["expectancy_r"]),
        winrate=f"{st['winrate'] * 100:.0f}",
        wins=st["wins"], losses=st["losses"], closed=st["trades"], open=st["open"],
        equity=_equity_svg(data["equity"]),
        by_symbol=_dash_table(data["by_symbol"]),
        milestone=_dash_milestone(data["milestone"]["current"], data["milestone"]["next"]),
    )


def render_dashboard_png(data: dict) -> bytes:
    """Дашборд -> PNG (для инлайн-превью/визуального QA). Тяжёлый Playwright — через to_thread."""
    return render_html_to_png(build_dashboard_html(data), width=920)
