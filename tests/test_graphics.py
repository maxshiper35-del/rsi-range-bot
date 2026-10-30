"""Тесты рендера карточек: чистые HTML-функции без браузера + сквозной PNG-рендер."""
from __future__ import annotations

import re
import struct
from pathlib import Path

import pytest

from app.delivery import graphics


def _png_size(png: bytes) -> tuple[int, int]:
    """(ширина, высота) из IHDR — без Pillow."""
    w, h = struct.unpack(">II", png[16:24])
    return w, h

_DATA = {
    "period_label": "06.07–09.07.2026",
    "total_r": 12.7677,
    "closed": 15,
    "open": 13,
    "winrate_pct": 66.67,
    "wins": 10,
    "losses": 5,
    "expectancy_r": 0.8512,
    "outcomes": [True, True, False, True, False, True, True, True, True, True,
                True, False, False, False, True],
    "updated_label": "09.07.2026",
}


def test_build_weekly_card_html_contains_real_numbers():
    html = graphics.build_weekly_card_html(_DATA)
    assert "+12.8R" in html
    assert "67%" in html                          # winrate округляется до целого
    assert "+0.85R" in html
    assert "15 закрытых сделок" in html
    assert "13 ещё в работе" in html
    assert "10 побед" in html and "5 поражений" in html
    # юнит-чарт: ровно 10 win + 5 loss блоков
    assert html.count('class="unit win"') == 10
    assert html.count('class="unit loss"') == 5
    # честные дисклеймеры на месте
    assert "100% исходов" in html
    assert "Малая выборка" in html and "100+" in html


def test_build_weekly_card_html_negative_total_r_class():
    data = dict(_DATA, total_r=-3.5, expectancy_r=-0.5)
    html = graphics.build_weekly_card_html(data)
    # типографский минус U+2212 (как в og и видео): по ширине равен плюсу
    assert '<div class="stat-value negative">−3.5R</div>' in html
    assert '<div class="mval negative">−0.50R</div>' in html  # expectancy тоже отрицательная


def test_build_weekly_card_html_empty_week_fallback():
    data = dict(_DATA, closed=0, wins=0, losses=0, winrate_pct=0.0, total_r=0.0,
               expectancy_r=0.0, outcomes=[])
    html = graphics.build_weekly_card_html(data)
    assert "unit-empty" in html
    assert "нет закрытых сделок" in html
    assert 'class="unit win"' not in html
    assert 'class="unit loss"' not in html


def test_build_weekly_card_html_escapes_period_label():
    data = dict(_DATA, period_label="<script>alert(1)</script>")
    html = graphics.build_weekly_card_html(data)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_render_weekly_card_produces_valid_png():
    pytest.importorskip("playwright")
    png = graphics.render_weekly_card(_DATA)
    assert isinstance(png, bytes)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"           # PNG magic bytes
    assert len(png) > 10_000                          # не пустая/битая картинка
    # квадрат 1200×1200 @2x: сетка «Медиа» Telegram режет по центру — итог остаётся в кадре
    assert _png_size(png) == (2400, 2400)


_CHART_DATA = {
    "signal_id": 128,
    "symbol": "BTC/USDT:USDT",
    "side": "LONG",
    "timeframe": "1h",
    "entry": 104250.5,
    "stop": 103180.0,
    "tps": [106670.0],
    "candles": [(104000 + i * 10, 104120 + i * 10, 103900 + i * 10, 104080 + i * 10)
                for i in range(56)],
    "rr": 2.26,
    "confidence_pct": 82.0,
    "updated_label": "10.07.2026 14:30 UTC",
}


def test_build_signal_chart_html_structure():
    html = graphics.build_signal_chart_html(_CHART_DATA)
    assert "<svg" in html and "Сигнал #128" in html
    assert html.count('class="c-body"') == 56           # тело каждой свечи
    assert html.count('class="zone"') == 2              # красная (стоп) + зелёная (тейк)
    assert html.count('class="pill"') == 3              # Вход / Стоп / Тейк
    # полные цены в подписях уровней ({:g} срезал бы 104250.5 до 104250)
    assert "104250.5" in html and "103180" in html and "106670" in html
    assert "LONG" in html and 'side-pill long' in html
    assert "R/R 1:2.3" in html and "82%" in html
    assert "Не финансовый совет" in html and "@rsi_range" in html


def test_build_signal_chart_html_short_side_and_escape():
    data = dict(_CHART_DATA, side="SHORT", symbol="<XSS>/USDT")
    html = graphics.build_signal_chart_html(data)
    assert 'side-pill short' in html
    assert "<XSS>" not in html and "&lt;XSS&gt;" in html


def test_build_signal_chart_html_multiple_tps():
    data = dict(_CHART_DATA, tps=[105000.0, 106000.0, 107000.0])
    html = graphics.build_signal_chart_html(data)
    assert "TP1" in html and "TP2" in html and "TP3" in html
    assert html.count('class="pill"') == 5              # Вход + Стоп + 3 TP


def test_render_signal_chart_produces_valid_png():
    pytest.importorskip("playwright")
    png = graphics.render_signal_chart(_CHART_DATA)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 10_000


_DASH = {
    "period_label": "всё время",
    "updated_label": "11.07.2026 00:30 UTC",
    "stats": {"trades": 24, "open": 8, "winrate": 0.5417, "expectancy_r": 0.11,
              "total_r": 2.64, "wins": 13, "losses": 11},
    "equity": [2.26, -1.0, -1.0, 2.26, 1.0, -1.0, 2.26, -1.0, -1.0, 1.0,
               2.26, -1.0, 2.26, -1.0, -1.0, 2.26, 1.0, -1.0, -1.0, 2.26,
               -1.0, 1.0, -1.0, 2.0],
    "by_symbol": {"BTC/USDT:USDT": {"trades": 10, "wins": 6, "winrate": 0.6,
                                    "expectancy_r": 0.3, "total_r": 3.0},
                  "ETH/USDT:USDT": {"trades": 14, "wins": 7, "winrate": 0.5,
                                    "expectancy_r": -0.02, "total_r": -0.36}},
    "milestone": {"current": 24, "next": 25},
}


def test_build_dashboard_html_self_contained_and_real():
    html = graphics.build_dashboard_html(_DASH)
    assert html.startswith("<!DOCTYPE html>") and "<svg" in html          # полный документ + SVG
    assert "+2.6R" in html and "+0.11R" in html and "54%" in html          # тайлы
    assert "BTC/USDT:USDT" in html                                         # разбивка
    # граница публикации: сетапов нет и в офлайн-дашборде (ревью 14.08)
    for leak in ("По сетапам", "6226", "LTF-BB", "1:1"):
        assert leak not in html
    assert "24 из 25" in html                                              # прогресс вехи
    assert "Не финансовый совет" in html                                   # честность
    # самодостаточность: ни одной ЗАГРУЗКИ ресурса. SVG-namespace (xmlns) — не запрос.
    body = html.replace("http://www.w3.org/2000/svg", "")
    for ext in ("http://", "https://", "src=", "@import", "fetch(", "XMLHttpRequest"):
        assert ext not in body, f"внешняя зависимость: {ext}"


def test_build_dashboard_html_empty_fallback():
    data = dict(_DASH, equity=[], by_symbol={},
                stats={"trades": 0, "open": 0, "winrate": 0.0, "expectancy_r": 0.0,
                       "total_r": 0.0, "wins": 0, "losses": 0},
                milestone={"current": 0, "next": 10})
    html = graphics.build_dashboard_html(data)
    assert "появится после первых закрытых" in html and "Данных пока нет" in html
    assert "<svg" not in html                                             # нет кривой на 0 сделок


def test_equity_svg_sign_color():
    up = graphics._equity_svg([1.0, 2.0, 1.0])       # +4R итог -> зелёный (v4)
    assert "#1f9e6e" in up and "+4.0R" in up
    down = graphics._equity_svg([-1.0, -1.0, 1.0])   # -1R итог -> красный (v4)
    assert "#f6465d" in down and "−1.0R" in down


def test_render_dashboard_png():
    pytest.importorskip("playwright")
    png = graphics.render_dashboard_png(_DASH)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10_000


def test_build_og_card_html():
    html = graphics.build_og_card_html(9.2, 228, [1.0, -1.0, 2.0])
    assert "+9.2R" in html and "228 закрытых" in html and "#1f9e6e" in html
    assert "не финансовый совет" in html.lower() and "<svg" in html
    down = graphics.build_og_card_html(-3.0, 10, [-1.0, -1.0, -1.0])
    assert "−3.0R" in down and "#f6465d" in down


# ---------------------------------------------------------------------------
# дизайн-система v5 (docs/design/design-system.md)
# ---------------------------------------------------------------------------
def test_brand_tokens_match_svg_constants():
    """SVG не понимает var() — цвета дублируются константами; они обязаны совпадать с CSS."""
    css = (Path(graphics.__file__).parent / "assets" / "brand.css").read_text().replace(" ", "")
    for token, value in (("--bg", graphics.BG), ("--panel", graphics.PANEL),
                         ("--line", graphics.LINE), ("--grid", graphics.GRID),
                         ("--ink", graphics.INK), ("--ink-2", graphics.INK2),
                         ("--ink-3", graphics.INK3), ("--pos", graphics.POS),
                         ("--neg", graphics.NEG), ("--zero", graphics.ZERO),
                         ("--acc", graphics.ACC)):
        assert f"{token}:{value};" in css, token


def test_brand_css_embeds_fonts_offline():
    css = graphics.brand_css()
    assert css.count("@font-face{") == 6 and css.count("data:font/woff2;base64,") == 6
    for ext in ("http://", "https://", "@import", "src="):   # шрифты — только встроенные
        assert ext not in css


def test_signed_minus_and_plural():
    assert graphics._signed(1.26) == "+1.3R" and graphics._signed(-3.5) == "−3.5R"
    assert graphics._signed(-0.04) == "0.0R"                 # ноль без знака
    assert graphics._signed(214.0, 0, "%") == "+214%"
    forms = ("сделка", "сделки", "сделок")
    assert [graphics._plural(n, *forms) for n in (1, 2, 5, 11, 21, 22, 25, 111)] == [
        "сделка", "сделки", "сделок", "сделок", "сделка", "сделки", "сделок", "сделок"]


def test_weekly_card_footer_and_grammar():
    data = dict(_DATA, closed=21, wins=1, losses=20, outcomes=[True] + [False] * 20)
    html = graphics.build_weekly_card_html(data)
    assert "21 закрытая сделка" in html and "1 победа" in html and "20 поражений" in html
    assert "(<b>21 сделка</b>)" in html                       # «Малая выборка»
    assert "canvas square" in html
    assert "@rsirange_bot" in html and "исход по ценам биржи" in html
    assert "Не финансовый совет" in html


def test_signal_chart_tags_fit_card_and_never_overlap():
    """DOOD-случай (сигнал 23.10): свечи на порядок шире уровней — теги вход/стоп/тейк
    наползали друг на друга нечитаемой кашей, а SVG был шире карточки (теги за краем)."""
    candles = [(0.0025, 0.0050, 0.0019, 0.0030)] + [(0.00195, 0.00197, 0.00192, 0.00194)] * 40
    svg = graphics._chart_svg(candles, 0.0019315, 0.001904, [0.002014])
    assert f'viewBox="0 0 {graphics._CHART_W} ' in svg
    tags = re.findall(r'<rect class="pill" x="([\d.]+)" y="([\d.]+)" width="([\d.]+)"', svg)
    ys = sorted(float(y) for _x, y, _w in tags)
    assert len(ys) == 3 and all(b - a >= 28 for a, b in zip(ys, ys[1:]))
    assert all(float(x) + float(w) <= graphics._CHART_W for x, _y, w in tags)
    html = graphics.build_signal_chart_html(_CHART_DATA)
    assert ".chart-card svg { display: block; width: 100%; height: auto; }" in html


def test_signal_chart_outcome_markers_are_optional():
    data = dict(_CHART_DATA, result_r=2.28, exit_price=106100.123456, exit_index=50)
    html = graphics.build_signal_chart_html(data)
    assert "Итог сделки" in html and "+2.28R" in html
    assert 'class="tag-exit"' in html and "ВЫХОД" in html and "106100.1<" in html
    assert "<circle" in html and html.count('class="pill"') == 3   # уровни плана те же
    plain = graphics.build_signal_chart_html(_CHART_DATA)
    assert "Итог сделки" not in plain and "tag-exit" not in plain and "<circle" not in plain
    assert "/USDT" in plain and ":USDT" not in plain           # ccxt-хвост не рисуем


def test_build_my_card_html():
    html = graphics.build_my_card_html({"trades": 14, "total_r": -2.58, "winrate": 0.2381,
                                        "last": [-1.0, 4.46, -1.0], "bot": "<b>x"})
    assert "−2.6R" in html and 'class="num neg' in html
    assert html.count('class="sq w"') == 1 and html.count('class="sq l"') == 2
    assert "<b>x" not in html and "&lt;b&gt;x" in html
    assert "14 закрытых сделок" in html and "Личная выборка сделок" in html
    assert "Не финансовый совет" in html


def test_render_my_card_is_square_png():
    pytest.importorskip("playwright")
    png = graphics.render_my_card({"trades": 3, "total_r": 1.5, "winrate": 0.67,
                                   "last": [1.0, -1.0, 1.5], "bot": "rsirange_bot"})
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and _png_size(png) == (2160, 2160)


def test_dashboard_table_shows_best_and_worst():
    many = {f"C{i}/USDT:USDT": {"trades": 3, "wins": 1, "winrate": 0.33,
                                "expectancy_r": (10 - i) / 10, "total_r": float(10 - i)}
            for i in range(20)}
    html = graphics.build_dashboard_html(dict(_DASH, by_symbol=many))
    assert "ещё 8 инструментов" in html and "Все 20 инструментов" in html
    assert "<b>C0</b>" in html and "<b>C19</b>" in html        # лучший и худший — на виду
