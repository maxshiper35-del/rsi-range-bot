from app.delivery import weekly_video as wv


def _t(i, sym, side, r):
    return {"id": i, "symbol": f"{sym}/USDT:USDT", "side": side, "timeframe": "1h",
            "closed_at": f"2026-10-2{i % 7}T10:00:00+00:00", "result_r": r}


TRADES = [_t(1, "ZEC", "LONG", 6.13), _t(2, "ENA", "SHORT", -1.0), _t(3, "NEAR", "LONG", -1.0),
          _t(4, "SOL", "LONG", 1.32), _t(5, "IRYS", "LONG", -1.0)]


def test_week_stats():
    st = wv.week_stats(TRADES)
    assert st["n"] == 5 and st["wins"] == 2 and st["losses"] == 3
    assert round(st["total"], 2) == 4.45 and st["best"]["id"] == 1
    assert [round(x, 2) for x in st["cum"]] == [6.13, 5.13, 4.13, 5.45, 4.45]


def test_reveal_frame_counts_only_shown_trades():
    html = wv.build_week_frame_html("19.10–25.10.2026", TRADES, 2)
    assert "+5.13R" in html and "2 из 5 сделок" in html
    assert html.count("class='tile'") + html.count("class='tile new'") == 2
    assert "19.10–25.10.2026" in html


def test_summary_honest_worst_and_no_taxonomy():
    html = wv.build_week_frame_html("p", TRADES, 5, summary=True, outro=1.0)
    assert "стоп −1R × 3" in html                      # не «случайная» худшая из равных −1R
    assert "Монеты недели" in html and "ZEC" in html
    assert "все закрытые сделки, без отбора" in html and "до комиссий" in html
    for leak in ("6226", "LTF", "SMC", "setup", "scores"):
        assert leak not in html


def test_losing_week_renders_red():
    lose = [_t(1, "ETH", "LONG", -1.0), _t(2, "BTC", "SHORT", -1.0)]
    html = wv.build_week_frame_html("p", lose, 2, summary=True, outro=1.0)
    assert "−2R" in html.replace(".00R", "R") and "neg" in html


def test_equity_svg_scale_fixed_over_week():
    a = wv.equity_svg([1.0, 3.0, -2.0], 1)
    b = wv.equity_svg([1.0, 3.0, -2.0], 3)
    # нулевая линия на одном месте — масштаб по всей неделе, кадры не прыгают
    assert a.split('y1="')[1].split('"')[0] == b.split('y1="')[1].split('"')[0]
