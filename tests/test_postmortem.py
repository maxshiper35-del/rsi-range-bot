"""Разбор сделок для владельца (app/delivery/postmortem.py): категории и метрики по барам."""
from __future__ import annotations

import pandas as pd

from app.delivery import postmortem as pm


def _bars(rows, start="2026-10-01T00:00:00+00:00"):
    o, h, low, c = zip(*rows)
    return pd.DataFrame({"open": o, "high": h, "low": low, "close": c,
                         "dt": pd.date_range(start, periods=len(rows), freq="h", tz="UTC")})


def _row(**kw):
    base = {"id": 1, "symbol": "SOL/USDT:USDT", "side": "LONG", "entry": 100.0, "stop": 90.0,
            "status": "stopped", "result_r": -1.0, "created_at": "2026-10-01T00:00:00+00:00",
            "filled_at": "2026-10-01T00:20:00+00:00", "closed_at": "2026-10-01T03:00:00+00:00"}
    return {**base, **kw}


def test_classify_order():
    assert pm.classify(2.1, 3.0, 5) == "win"               # плюс — всегда «трейл-выход»
    assert pm.classify(-1.0, 1.36, 3) == "round_trip"      # ≥+1R важнее кластера
    assert pm.classify(-1.0, 0.6, 2) == "market"
    assert pm.classify(-1.0, 0.1, 0) == "instant"
    assert pm.classify(-1.0, 0.6, 1) == "chop"


def test_excursions_skip_fill_bar_like_judge():
    """Бар филла не в счёт: ход до исполнения входа подписчик не получил бы."""
    bars = _bars([(100, 125, 99, 101),     # бар филла (00:00) — выброс до +2.5R не считаем
                  (101, 112, 98, 99),      # 01:00 — +1.2R
                  (99, 100, 89, 90),       # 02:00
                  (90, 91, 88, 89)])       # 03:00 — бар выхода
    mfe, mae = pm.excursions(_row(), bars)
    assert round(mfe, 2) == 1.2 and round(mae, 2) == 1.2
    short = _row(side="SHORT", entry=100.0, stop=110.0)
    mfe_s, _ = pm.excursions(short, bars)
    assert round(mfe_s, 2) == 1.2                          # (100 − 88) / 10
    assert pm.excursions(_row(), pd.DataFrame()) is None


def test_cluster_counts_other_same_side_stops_within_3h():
    stops = [(1, "LONG", "2026-10-01T03:00:00+00:00"), (2, "LONG", "2026-10-01T05:30:00+00:00"),
             (3, "LONG", "2026-10-01T07:00:00+00:00"), (4, "SHORT", "2026-10-01T03:10:00+00:00")]
    assert pm.cluster_count(_row(), stops) == 1            # #2 в окне, #3 за окном, #4 — шорт


def test_analyze_and_digest():
    bars = _bars([(100, 101, 99, 100), (100, 112, 99, 105), (105, 106, 89, 90),
                  (90, 91, 88, 89)])
    btc = _bars([(50000, 50100, 49900, 50000)] * 3 + [(50000, 50000, 49000, 49400)])
    rows = [_row(), _row(id=2, symbol="ETH/USDT:USDT", result_r=1.8, status="stopped")]
    items = pm.analyze(rows, {"SOL/USDT:USDT": bars, "ETH/USDT:USDT": bars}, btc)
    assert items[0]["cat"] == "round_trip" and round(items[0]["mfe"], 2) == 1.2
    assert items[0]["btc"] < 0                             # BTC упал во время лонга
    assert items[1]["cat"] == "win"
    txt = pm.render_digest("Разбор сделок за 01.10", items, items)
    assert "#1 SOL LONG" in txt and "был +1.20R — развернулся в стоп" in txt
    assert "против нас" in txt
    assert "Главная утечка: ↩️" in txt
    assert pm.summary_lines([]) == ["Сделок нет."]
    for leak in ("6226", "LTF", "1:1"):                    # таксономия сетапов не выводится
        assert leak not in txt
