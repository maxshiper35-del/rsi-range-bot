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
