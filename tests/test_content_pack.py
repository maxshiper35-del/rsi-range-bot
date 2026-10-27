from datetime import date

from app.delivery import content_pack as cp
from app.delivery import weekly_video as wv


def _t(i, sym, side, r):
    return {"id": i, "symbol": f"{sym}/USDT:USDT", "side": side, "timeframe": "1h",
            "closed_at": f"2026-10-26T{i:02d}:00:00+00:00", "result_r": r}


TRADES = [_t(1, "ORDER", "LONG", -1.0), _t(3, "HYPE", "SHORT", 1.52), _t(8, "ENA", "SHORT", -1.0)]
LEAKS = ("6226", "LTF", "SMC", "setup", "scores", "1:1")


def test_n_trades_plural():
    assert [wv.n_trades(n) for n in (1, 3, 5, 11, 12, 21, 22, 25, 111)] == [
        "1 сделка", "3 сделки", "5 сделок", "11 сделок", "12 сделок", "21 сделка",
        "22 сделки", "25 сделок", "111 сделок"]


def test_day_period_is_utc_day():
    s, e = cp.day_period(date(2026, 10, 26))
    assert s.isoformat() == "2026-10-26T00:00:00+00:00"
    assert e.isoformat() == "2026-10-27T00:00:00+00:00"


def test_carousel_lists_every_trade_without_taxonomy():
    slides = cp.build_carousel_html("26.10.2026", TRADES)
    assert len(slides) == 4                          # обложка, список, что такое R, призыв
    assert "−0.48R" in slides[0] and "3 сделки · 1 в плюс / 2 в минус" in slides[0]
    for sym in ("ORDER", "HYPE", "ENA"):
        assert sym in slides[1]
    assert "1/4" in slides[0] and "4/4" in slides[3]
    for leak in LEAKS:
        assert all(leak not in s for s in slides)


def test_carousel_caps_list_slides():
    many = [_t(i % 24, "BTC", "LONG", 1.0) for i in range(30)]
    slides = cp.build_carousel_html("p", many)
    assert len(slides) == 1 + cp.MAX_LIST_SLIDES + 2
    assert "+ ещё 6 — в канале" in slides[cp.MAX_LIST_SLIDES]


def test_story_and_captions():
    html = cp.build_story_html("26.10.2026", TRADES)
    assert "−0.48R" in html and "Итог дня" in html
    caps = cp.captions("26.10.2026", TRADES)
    assert caps["video"].startswith("Итог дня 26.10.2026: 3 сделки, 1 в плюс / 2 в минус, −0.48R")
    assert "без отбора" in caps["video"] and "Не финансовый совет" in caps["video"]
    assert "(3 сделки)" in caps["carousel"]
    for leak in LEAKS:
        assert all(leak not in c for c in caps.values())


def test_replay_caption_exit_kind():
    row = {"symbol": "ENA/USDT:USDT", "side": "SHORT", "result_r": -1.0}
    assert "ENA SHORT, −1R до комиссий, выход по стопу" in cp.replay_caption(row)
    row["result_r"] = 2.5
    assert "+2.50R до комиссий, выход по трейлингу" in cp.replay_caption(row)


def test_week_video_day_scope_labels():
    html = wv.build_week_frame_html("26.10.2026", TRADES, 3, summary=True, outro=1.0,
                                    scope="day")
    assert "Итог дня" in html and "Результат дня" in html and "Монеты дня" in html
    assert "недел" not in html
