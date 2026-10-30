"""Новые форматы 9:16 (scripts/make_bot_diary, make_crash_minute, make_vip_check):
арифметика, разбор, стражи публикации и анонимизации — без сети и без браузера."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.delivery.signal_check import CheckResult, ParsedSignal
from scripts import make_bot_diary as diary
from scripts import make_crash_minute as crash
from scripts import make_vip_check as vip
from scripts import shorts_kit as kit


# --- kit -------------------------------------------------------------------------------
def test_number_formats_are_russian():
    assert kit.money(10194.45) == "10 194 $"
    assert kit.money(363.2, sign=True) == "+363 $"
    assert kit.money(-98.3, sign=True) == "−98 $"
    assert kit.pct(-2.97) == "−3,0%"
    assert kit.rr(-1.0) == "−1R" and kit.rr(2.3966) == "+2,40R"
    assert kit.price(0.2068) == "0,2068" and kit.price(78191.6) == "78 192"
    assert kit.plural(3, "стоп", "стопа", "стопов") == "3 стопа"
    assert kit.plural(11, "стоп", "стопа", "стопов") == "11 стопов"


def test_public_guard_blocks_taxonomy_but_not_clock_or_prices():
    for bad in ("сетап LTF-BB", "по 6226", "сетап 1:1", "open-SMC", "суб-скор 0.8", "иксы"):
        with pytest.raises(AssertionError):
            kit.check_public([bad])
    kit.check_public(["21:10 UTC", "цена 0,62261", "R:R 1:2.5", "вход 101"])
    with pytest.raises(AssertionError):
        kit.check_public(["Канал SecretChan пишет"], extra=["secretchan"])


def test_timeline_snaps_to_frames_and_places_cues():
    tl = kit.Timeline()
    tl.add("<a>", 0.11, "tick")
    tl.add("<b>", 0.5, "win")
    assert [round(d * kit.FPS) for _h, d in tl.frames] == [3, 15]
    assert tl.cues[0] == (0.0, "tick") and tl.cues[1][0] == pytest.approx(0.1)


def test_visible_text_drops_css():
    t = kit.visible_text("<style>.x{color:red}</style><div class='x'>Привет</div>")
    assert "color" not in t and "Привет" in t


# --- дневник робота --------------------------------------------------------------------
def _t(i, day, hour, r, side="LONG", sym="WLD/USDT:USDT"):
    return {"id": i, "symbol": sym, "side": side, "timeframe": "1h",
            "closed_at": f"{day}T{hour:02d}:00:00+00:00", "result_r": r}


def test_account_compounds_one_percent_risk():
    st = diary.account_steps([_t(1, "2026-10-27", 1, 2.0), _t(2, "2026-10-27", 2, -1.0)])
    assert st[0].pnl == pytest.approx(200.0)
    assert st[1].before == pytest.approx(10200.0) and st[1].pnl == pytest.approx(-102.0)
    assert st[1].after == pytest.approx(10098.0)


def test_diary_days_and_numbering_keep_minus_days():
    trades = [_t(1, "2026-10-27", 5, 1.0), _t(2, "2026-10-28", 5, -1.0),
              _t(3, "2026-10-28", 6, -1.0), _t(4, "2026-10-29", 5, 3.0)]
    d = diary.build_diary(trades, date(2026, 10, 29))
    assert d.n == 3 and len(d.days) == 3 and len(d.today) == 1
    assert d.days[1][2] < d.days[1][1]                       # день 2 — минус, не выкинут
    assert d.close_balance == pytest.approx(10000 * 1.01 * 0.99 * 0.99 * 1.03)
    html = "".join(h for h, _d in diary.frames(d).frames)
    assert "расчёт по журналу" in html and kit.NOT_ADVICE in html and kit.BOT in html
    assert len(diary.caption(d).title) <= 100


def test_diary_empty_day_in_series_carries_balance():
    trades = [_t(1, "2026-10-27", 5, 1.0), _t(2, "2026-10-29", 5, 1.0)]
    d = diary.build_diary(trades, date(2026, 10, 29))
    assert d.days[1][1] == d.days[1][2] == pytest.approx(10100.0)


# --- обвал -----------------------------------------------------------------------------
def _bars(rows, start="2026-10-29T00:00:00Z", freq="1h"):
    dt = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC")
    return pd.DataFrame({"dt": dt, "open": [r[0] for r in rows], "high": [r[1] for r in rows],
                         "low": [r[2] for r in rows], "close": [r[3] for r in rows]})


def test_biggest_move_is_largest_body_not_largest_wick():
    b = _bars([(100, 103, 97, 100.5), (100, 100.5, 99, 98), (98, 99, 97.5, 98.5)])
    assert crash.biggest_move(b) == 1


def test_exit_minute_and_close_r():
    m1 = _bars([(10, 10, 9.9, 9.95), (9.95, 9.96, 9.4, 9.5), (9.5, 9.6, 9.0, 9.2)],
               start="2026-10-29T20:00:00Z", freq="1min")
    hour = pd.Timestamp("2026-10-29T20:00:00Z")
    assert crash.exit_minute(m1, "LONG", 9.5, hour) == m1["dt"].iloc[1]
    assert crash.exit_minute(m1, "SHORT", 10.5, hour) is None
    t = {"side": "LONG", "entry": 10.0, "stop": 9.5}
    assert crash.close_r(m1, t, pd.Timestamp("2026-10-30T00:00:00Z")) == pytest.approx(-1.6)


def _crash_fixture():
    ctx = _bars([(100 + i * 0.1, 100.6 + i * 0.1, 99.6 + i * 0.1, 100.2 + i * 0.1)
                 for i in range(5)], start="2026-10-29T15:00:00Z")
    bar = _bars([(101, 101.2, 97, 98)], start="2026-10-29T20:00:00Z").iloc[0]
    m1 = _bars([(101 - k * 0.05, 101.1 - k * 0.05, 100.8 - k * 0.07, 100.9 - k * 0.07)
                for k in range(60)], start="2026-10-29T20:00:00Z", freq="1min")
    lo = {"id": 1, "symbol": "1000PEPE/USDT:USDT", "side": "LONG", "entry": 1.0, "stop": 0.98,
          "exit_price": 0.98, "result_r": -1.0, "closed_at": "2026-10-29T20:00:00+00:00"}
    lo2 = dict(lo, id=2)
    sh = {"id": 3, "symbol": "ETH/USDT:USDT", "side": "SHORT", "entry": 2365.0, "stop": 2417.0,
          "exit_price": 2240.8, "result_r": 2.3966, "closed_at": "2026-10-29T21:00:00+00:00"}
    exits = [crash.Exit(lo, m1["dt"].iloc[10], -2.5), crash.Exit(lo2, m1["dt"].iloc[10], -2.5),
             crash.Exit(sh, pd.Timestamp("2026-10-29T21:00:00Z"), None, 2230.4)]
    return crash.Crash(bar, ctx, m1, exits, m1.iloc[30], m1.iloc[59])


def test_crash_frames_merge_twins_and_stay_public():
    c = _crash_fixture()
    assert [n for _e, n in crash._merge(c.exits)] == [2, 1]
    tl = crash.frames(c)
    html = "".join(kit.visible_text(h) for h, _d in tl.frames)
    kit.check_public([html])
    assert "PEPE" in html and "1000PEPE" not in html and kit.NOT_ADVICE in html
    assert "×2" in html
    assert 20 <= tl.t <= 35
    assert "стоп" in crash.caption(c).description


# --- проверка канала -------------------------------------------------------------------
_POST = """💎 VIP SIGNAL — SOL/USDT SHORT

📍 Entry Zone: $115.50 – $117.50
🎯 TP1: $107.00 (+8%)
🎯 TP2: $99.50 (+14%)
🛑 SL: $120.50

Analysis:
• SOL previously hit our TP2 $115 target, BTC at 84,355 ✅
"""


def test_parse_post_takes_targets_from_explicit_lines():
    sig = vip.parse_post(_POST)
    assert sig is not None and sig.side == "SHORT" and sig.coin == "SOL"
    assert (sig.entry_lo, sig.entry_hi, sig.stop) == (115.5, 117.5, 120.5)
    assert sig.targets == [107.0, 99.5]                     # не 115 из абзаца анализа


def test_thousands_separator_fixed_only_with_dot_decimals():
    t = "BTC/USDT LONG\nEntry Zone: 81,500 - 84,500\nTP1: 91,000 (+8.5%)\nSL: 77,500"
    sig = vip.parse_post(t)
    assert sig is not None and (sig.entry_lo, sig.entry_hi, sig.stop) == (81500, 84500, 77500)
    assert vip.prep_text("Вход 0,4512 стоп 0,4400") == "Вход 0,4512 стоп 0,4400"


def test_subscribers_rounding_and_claim():
    assert vip.subs_ru("35.4K") == "~35 тыс." and vip.subs_ru("2.95K") == "~3 тыс."
    assert vip.subs_ru("123K") == "~120 тыс." and vip.subs_ru("980") == "~980"
    assert vip.claimed_pct([_POST, "TP2: 0.790 (+52%)"]) == 52.0


def test_parse_page_reads_posts_not_reply_quotes():
    html = ("<div class='tgme_channel_info_header_title'><span>Chan</span></div>"
            "<span class='counter_value'>35.4K</span>"
            "<div class='tgme_widget_message ' data-post='chan/7'>"
            "<div class='tgme_widget_message_reply'><div class='tgme_widget_message_metatext "
            "js-message_reply_text'>quoted</div></div>"
            "<div class='tgme_widget_message_text js-message_text'>line1<br/>line2</div>"
            "<time datetime='2026-10-29T12:39:01+00:00'></time></div>")
    posts, title, subs = vip.parse_page(html)
    assert title == "Chan" and subs == "35.4K"
    assert len(posts) == 1 and posts[0].text == "line1\nline2"


def _checked(kind: str, tp1_r: float = 1.5) -> vip.Checked:
    sig = ParsedSignal("SOL", "LONG", 100.0, 100.0, 99.0, [100.0 + tp1_r])
    res = {"tp": CheckResult("all_tp", 100.0, hit=1), "stop": CheckResult("stop", 100.0),
           "nofill": CheckResult("no_fill"), "open": CheckResult("open", 100.0)}[kind]
    post = vip.Post("chan/1", pd.Timestamp("2026-10-29T12:00:00Z"), "текст SECRETCHAN")
    return vip.Checked(post, sig, "15m", res, 100.0)


def test_verdict_math_and_anonymized_frames():
    rows = ([_checked("tp", 1.5)] * 2 + [_checked("stop")] * 6 + [_checked("nofill")]
            + [_checked("open")])
    v = vip.verdict(rows)
    assert (v.n, v.tp, v.stop, v.nofill, v.open) == (10, 2, 6, 1, 1)
    assert v.tp1_mean == pytest.approx(1.5)
    assert v.breakeven == pytest.approx(0.4) and v.hit_rate == pytest.approx(0.25)
    assert v.at_tp1 == pytest.approx(2 * 1.5 - 6)
    tl = vip.frames(rows, v, "~35 тыс.", 52.0)
    texts = [kit.visible_text(h) for h, _d in tl.frames]
    kit.check_public(texts, extra=["secretchan"])        # текст постов в кадр не попадает
    assert any(vip.LABEL in t for t in texts) and any(kit.BOT in t for t in texts)
    assert 20 <= tl.t <= 35


def test_plausible_rejects_misparsed_zone():
    good = ParsedSignal("BTC", "LONG", 81500.0, 84500.0, 77500.0, [91000.0])
    bad = ParsedSignal("BTC", "LONG", 81.0, 500.0, 77.0, [])
    assert vip.plausible(good, 84311.6) and not vip.plausible(bad, 84311.6)


def test_handles_collects_source_tags():
    assert vip.handles(["Join @SomeVipChan ☆leaksz now"]) == ["SomeVipChan", "leaksz"]


_BUY_POST = """🔰 #BTCUSDT

⏫ BUY : 81909-80271

👁‍🗨 Leverage: Cross (10.00X)

📍TARGETS

1) 82564
2) 83220
5) 88052+

❌ STOPLOSS: 78633"""


def test_buy_sell_line_is_entry_zone():
    sig = vip.parse_post(_BUY_POST)
    assert sig is not None and (sig.coin, sig.side) == ("BTC", "LONG")
    assert (sig.entry_lo, sig.entry_hi, sig.stop) == (80271, 81909, 78633)
    assert sig.targets[:2] == [82564, 83220]
    short = (_BUY_POST.replace("⏫ BUY : 81909-80271", "⏬ SELL : 0.7655-0.78081")
             .replace("82564", "0.759376").replace("83220", "0.753252")
             .replace("88052", "0.70").replace("78633", "0.79612"))
    s = vip.parse_post(short)
    assert s is not None and s.side == "SHORT" and (s.entry_lo, s.stop) == (0.7655, 0.79612)
    assert vip.prep_text("Buy zone 1,2") == "Buy zone 1,2"         # не строка «BUY :»


def test_verdict_breakeven_agrees_with_closing_at_tp1():
    # сработали короткие цели (+1R), длинные (+3R) ушли в стоп: по всем сигналам «нужно»
    # вышло бы 1/(1+2) = 33% при факте 50% — а итог на TP1 при этом в нуле
    rows = [_checked("tp", 1.0)] * 2 + [_checked("stop", 3.0)] * 2
    v = vip.verdict(rows)
    assert v.tp1_mean == pytest.approx(1.0) and v.at_tp1 == pytest.approx(0.0)
    assert v.breakeven == pytest.approx(v.hit_rate)                # 50% и 50%: ровно ноль
    assert vip._pct_digits(v) == 1                                 # равные в целых — со знаком


def test_public_guard_blocks_demo_wording():
    for bad in ("демо-счёт робота", "НА ДЕМО-СЧЁТЕ", "виртуальные деньги", "Демо: без денег"):
        with pytest.raises(AssertionError):
            kit.check_public([bad])
    kit.check_public(["демонстрация входа", "демографический спрос"])  # не «демо»


def test_quiz_math_and_captions_pass_guards():
    from scripts import make_quiz as quiz

    quiz._check()                                       # арифметика: каждое число двумя путями
    keys = [q.key for q in quiz.QUIZZES]
    assert len(keys) == len(set(keys)) and len({q.title for q in quiz.QUIZZES}) == len(keys)
    for q in quiz.QUIZZES:
        if q.key[:2] >= "21":
            cap = quiz.caption(q)
            kit.check_public([cap.render()])
            assert cap.hashtags.startswith("#") and "#" not in cap.description
            frames, _cues = quiz.build(q)
            kit.check_public(kit.visible_text(h) for h, _d in frames)
