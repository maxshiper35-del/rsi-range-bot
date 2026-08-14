"""Тесты рендера карточки недели: чистая HTML-функция без браузера + сквозной PNG-рендер."""
from __future__ import annotations

import pytest

from app.delivery import graphics

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
    assert '<div class="stat-value negative">-3.5R</div>' in html
    assert "-0.5" in html  # expectancy тоже отрицательная


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
    up = graphics._equity_svg([1.0, 2.0, 1.0])       # +4R итог -> зелёный
    assert "#4ADE9C" in up and "+4.0R" in up
    down = graphics._equity_svg([-1.0, -1.0, 1.0])   # -1R итог -> красный
    assert "#F16565" in down and "-1.0R" in down


def test_render_dashboard_png():
    pytest.importorskip("playwright")
    png = graphics.render_dashboard_png(_DASH)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10_000


def test_build_og_card_html():
    html = graphics.build_og_card_html(9.2, 228, [1.0, -1.0, 2.0])
    assert "+9.2R" in html and "228 закрытых" in html and "#1f9e6e" in html
    assert "не финансовый совет" in html and "<svg" in html
    down = graphics.build_og_card_html(-3.0, 10, [-1.0, -1.0, -1.0])
    assert "−3.0R" in down and "#f6465d" in down
