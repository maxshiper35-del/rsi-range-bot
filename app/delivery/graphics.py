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


def render_html_to_png(html: str, width: int = _CARD_WIDTH, scale: int = 2) -> bytes:
    """Синхронный рендер (Playwright sync API) — вызывать ТОЛЬКО из потока
    (asyncio.to_thread), никогда напрямую из event loop. Высота — intrinsic по контенту
    (full_page=True), не зафиксирована: юнит-чарт может перенестись на несколько строк
    в активную неделю без обрезания карточки."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(device_scale_factor=scale)
            page.set_viewport_size({"width": width, "height": 800})
            page.set_content(html)
            page.wait_for_timeout(100)
            return page.screenshot(full_page=True)
        finally:
            browser.close()


def render_weekly_card(data: dict) -> bytes:
    return render_html_to_png(build_weekly_card_html(data))


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
