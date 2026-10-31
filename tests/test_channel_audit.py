"""Проверка канала целиком (channel_audit): разбор веб-превью t.me/s, классы постов, план
свечей, агрегат с итогом на TP1, тексты и карточка. Сеть не нужна: страницы и свечи — синтетика
по структуре реальных страниц (снято с t.me/s 29.10.2026)."""
from __future__ import annotations

import pandas as pd
import pytest

from app.delivery import channel_audit as ca
from app.delivery import signal_check as sc

T0 = pd.Timestamp("2026-10-20 12:00", tz="UTC")

# фрагмент реальной разметки t.me/s/<name>: пост, ответ с цитатой (чужой текст в
# js-message_reply_text НЕ должен попасть в пост), пост-картинка без текста, служебный пост
REAL_LIKE = """<!DOCTYPE html><html><head>
<meta property="og:title" content="Test &amp; Signals">
<link rel="canonical" href="/s/testsig">
<link rel="prev" href="/s/testsig?before=101">
</head><body>
<section class="tgme_channel_history js-message_history">
<div class="tgme_widget_message_centered js-messages_more_wrap"><a href="/s/testsig?before=101"
 class="tme_messages_more js-messages_more" data-before="101"></a></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message
 text_not_supported_wrap js-widget_message" data-post="testsig/101" data-view="eyJjIjo">
  <div class="tgme_widget_message_user"><a href="https://t.me/testsig"><i
   class="tgme_widget_message_user_photo bgcolor3" data-content="T"><img src="u.jpg"></i></a></div>
  <div class="tgme_widget_message_bubble">
    <div class="tgme_widget_message_author accent_color"><a class="tgme_widget_message_owner_name"
     href="https://t.me/testsig"><span dir="auto">Test</span></a></div>
    <div class="tgme_widget_message_text js-message_text" dir="auto"><i class="emoji"
     style="background-image:url('//telegram.org/img/emoji/40/F09F9FA2.png')"><b>🟢</b></i>
<b>#BTC/USDT LONG</b><br/>Вход: 64200 - 64500<br/>Стоп: 63500<br/>TP1: 65300<br/><a
 href="https://t.me/testsig">канал</a></div>
    <div class="tgme_widget_message_footer compact js-message_footer">
      <div class="tgme_widget_message_info short js-message_info">
        <span class="tgme_widget_message_views">204</span><span class="tgme_widget_message_meta"><a
         class="tgme_widget_message_date" href="https://t.me/testsig/101"><time
         datetime="2026-10-20T12:00:00+00:00" class="time">12:00</time></a></span>
      </div></div>
  </div>
</div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message
 text_not_supported_wrap js-widget_message" data-post="testsig/102">
  <div class="tgme_widget_message_bubble">
    <a class="tgme_widget_message_reply" href="https://t.me/testsig/101"><div
     class="tgme_widget_message_author accent_color"><span class="tgme_widget_message_author_name"
     dir="auto">Test</span></div><div class="tgme_widget_message_text js-message_reply_text"
     dir="auto">#ETH/USDT SHORT Вход: 3000 Стоп: 3100</div></a>
    <div class="tgme_widget_message_text js-message_text" dir="auto">TP1 &#036;65300 взят</div>
    <div class="tgme_widget_message_footer compact js-message_footer"><div
     class="tgme_widget_message_info short js-message_info"><span
     class="tgme_widget_message_meta"><a
     class="tgme_widget_message_date" href="https://t.me/testsig/102"><time
     datetime="2026-10-20T15:30:00+00:00" class="time">15:30</time></a></span></div></div>
  </div>
</div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message
 text_not_supported_wrap js-widget_message" data-post="testsig/103">
  <div class="tgme_widget_message_bubble">
    <a class="tgme_widget_message_photo_wrap" href="https://t.me/testsig/103"
     style="background-image:url('p.jpg')"></a>
    <div class="tgme_widget_message_footer compact js-message_footer"><div
     class="tgme_widget_message_info short js-message_info"><span
     class="tgme_widget_message_meta"><a
     class="tgme_widget_message_date" href="https://t.me/testsig/103"><time
     datetime="2026-10-21T08:00:00+00:00" class="time">08:00</time></a></span></div></div>
  </div>
</div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message service_message"
 data-post="testsig/104"><div class="tgme_widget_message_bubble"><div
 class="tgme_widget_message_text js-message_text">Channel photo updated</div></div></div></div>
</section></body></html>"""


def _page(name: str, posts: list[tuple[int, str, str]], before: int | None = None,
          title: str | None = "Chan") -> str:
    """Минимальная страница той же структуры: posts — [(id, ISO-время, текст)]."""
    head = f'<meta property="og:title" content="{title}">' if title else ""
    if before:
        head += f'<link rel="prev" href="/s/{name}?before={before}">'
    body = "".join(
        f'<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" '
        f'data-post="{name}/{i}"><div class="tgme_widget_message_text js-message_text">'
        f'{text.replace(chr(10), "<br/>")}</div><a class="tgme_widget_message_date" '
        f'href="https://t.me/{name}/{i}"><time datetime="{ts}"></time></a></div></div>'
        for i, ts, text in posts)
    return f"<html><head>{head}</head><body>{body}</body></html>"


def _post(i: int, posted: pd.Timestamp, text: str, name: str = "testsig") -> ca.Post:
    return ca.Post(name, i, posted, text)


def _bars(rows, start=T0, step="15min"):
    """rows: [(open, high, low, close), ...] — бары подряд, первый открывается в start."""
    dt = pd.date_range(start, periods=len(rows), freq=step)
    return pd.DataFrame([dict(dt=d, open=o, high=h, low=lo, close=c)
                         for d, (o, h, lo, c) in zip(dt, rows)])


# --- ссылка на канал ---

@pytest.mark.parametrize("txt,name", [
    ("@binance_360", "binance_360"),
    ("  @Oracle_Easy  ", "Oracle_Easy"),
    ("t.me/oracle_easy", "oracle_easy"),
    ("https://t.me/oracle_easy", "oracle_easy"),
    ("https://t.me/s/oracle_easy", "oracle_easy"),
    ("http://telegram.me/oracle_easy/", "oracle_easy"),
    ("https://t.me/oracle_easy/96979", "oracle_easy"),          # ссылка на пост — тоже канал
    ("t.me/oracle_easy?embed=1", "oracle_easy"),
])
def test_channel_ref_accepts(txt, name):
    assert ca.parse_channel_ref(txt) == name


@pytest.mark.parametrize("txt", [
    None, "", "@abc", "@1channel", "hello @oracle_easy", "@oracle_easy спасибо",
    "https://t.me/+AbCdEf123", "https://t.me/joinchat/AbCdEf", "t.me/joinchat",
    "https://t.me/c/123456/78", "https://example.com/oracle_easy", "@oracle easy",
    "📊 Статистика", "/audit @oracle_easy",
])
def test_channel_ref_rejects(txt):
    assert ca.parse_channel_ref(txt) is None


def test_channel_ref_bare_only_after_command():
    assert ca.parse_channel_ref("oracle_easy") is None             # обычное слово — не канал
    assert ca.parse_channel_ref("oracle_easy", bare=True) == "oracle_easy"
    assert ca.parse_channel_ref(" @oracle_easy ", bare=True) == "oracle_easy"
    assert ca.parse_channel_ref("t.me/oracle_easy", bare=True) == "oracle_easy"
    for bad in ("oracle easy", "joinchat", "abc", "@@oracle_easy"):
        assert ca.parse_channel_ref(bad, bare=True) is None, bad


# --- разбор веб-превью ---

def test_parse_tme_page_real_structure():
    page = ca.read_tme_page(REAL_LIKE)
    assert page.title == "Test & Signals" and page.before == 101
    assert [p.post_id for p in page.posts] == [101, 102, 103]      # служебный без времени — мимо
    p1, p2, p3 = page.posts
    assert p1.channel == "testsig" and p1.url == "https://t.me/testsig/101"
    assert p1.posted == pd.Timestamp("2026-10-20 12:00", tz="UTC")
    assert p1.text.splitlines() == ["🟢", "#BTC/USDT LONG", "Вход: 64200 - 64500", "Стоп: 63500",
                                    "TP1: 65300", "канал"]
    assert sc.parse_signal(p1.text).stop == 63500
    assert p2.text == "TP1 $65300 взят"                # цитата ответа не подмешана, &#036; -> $
    assert p3.text == ""
    assert ca.parse_tme_page(REAL_LIKE) == page.posts


def test_parse_tme_page_empty_and_garbage():
    assert ca.read_tme_page("") == ca.TmePage(None, [], None)
    page = ca.read_tme_page("<html><head><title>Telegram</title></head><body>"
                            "<div class='tgme_page'>x</div></body></html>")
    assert page.posts == [] and page.title is None and page.before is None


def test_fetch_channel_paginates_until_limit():
    pages = {
        None: _page("ch", [(i, f"2026-10-20T{i - 80:02d}:00:00+00:00", f"p{i}")
                           for i in range(101, 104)], before=101),
        101: _page("ch", [(i, f"2026-10-19T{i - 80:02d}:00:00+00:00", f"p{i}")
                          for i in range(98, 101)], before=98),
        98: _page("ch", [(i, f"2026-10-18T{i - 80:02d}:00:00+00:00", f"p{i}")
                         for i in range(95, 98)]),
    }
    calls = []

    def fetch(name, before=None):
        calls.append(before)
        return pages[before]

    page = ca.fetch_channel("ch", max_posts=5, fetch=fetch, pause=0)
    assert [p.post_id for p in page.posts] == [99, 100, 101, 102, 103]   # 5 последних
    assert calls == [None, 101]                                         # лимит набран — стоп
    page = ca.fetch_channel("ch", max_posts=60, fetch=fetch, pause=0)
    assert [p.post_id for p in page.posts] == list(range(95, 104)) and calls[-1] == 98
    assert page.title == "Chan"


def test_fetch_channel_stops_on_repeat_and_page_cap():
    calls = []

    def fetch(name, before=None):
        calls.append(before)
        return _page("ch", [(5, "2026-10-20T10:00:00+00:00", "x")], before=5)

    page = ca.fetch_channel("ch", fetch=fetch, pause=0)
    assert [p.post_id for p in page.posts] == [5] and calls == [None, 5]  # новых нет — стоп

    n = iter(range(1000, 0, -10))

    def endless(name, before=None):
        i = next(n)
        calls.append(before)
        return _page("ch", [(i, "2026-10-20T10:00:00+00:00", "x")], before=i)

    calls.clear()
    ca.fetch_channel("ch", max_pages=3, fetch=endless, pause=0)
    assert len(calls) == 3


def test_fetch_channel_older_pages_best_effort(monkeypatch):
    first = _page("ch", [(10, "2026-10-20T10:00:00+00:00", "x")], before=10)

    def flaky(name, before=None):
        if before is None:
            return first
        raise TimeoutError("t.me молчит")               # urllib: socket timeout ⊂ OSError

    page = ca.fetch_channel("ch", fetch=flaky, pause=0)
    assert [p.post_id for p in page.posts] == [10]      # аудит идёт по первой странице

    calls = []

    def slow(name, before=None):
        calls.append(before)
        return _page("ch", [(100 - len(calls), "2026-10-20T10:00:00+00:00", "x")],
                     before=100 - len(calls))

    clock = iter([0.0, 30.0, 60.0])
    monkeypatch.setattr(ca.time, "monotonic", lambda: next(clock))
    ca.fetch_channel("ch", fetch=slow, pause=0)
    assert calls == [None]                              # бюджет листания исчерпан

    def broken(name, before=None):
        raise TimeoutError("первая страница")

    with pytest.raises(TimeoutError):                    # первая страница обязательна
        ca.fetch_channel("ch", fetch=broken, pause=0)


def test_fetch_channel_unavailable():
    with pytest.raises(ca.ChannelUnavailable):
        ca.fetch_channel("nobody", fetch=lambda name, before=None: None, pause=0)
    with pytest.raises(ca.ChannelUnavailable):                           # страница без постов
        ca.fetch_channel("nobody", fetch=lambda name, before=None: "<html></html>", pause=0)
    empty = ca.fetch_channel("quiet", fetch=lambda n, before=None: _page("quiet", []), pause=0)
    assert empty.title == "Chan" and empty.posts == []          # канал есть, постов нет — не ошибка


def test_fetch_tme_html_detects_redirect(monkeypatch):
    class _Resp:
        def __init__(self, url, body=b"<html>ok</html>"):
            self.url, self.body = url, body

        def geturl(self):
            return self.url

        def read(self, n=-1):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen = []

    def fake_urlopen(req, timeout):
        seen.append((req.full_url, req.get_header("User-agent"), timeout))
        if "nobody" in req.full_url:
            return _Resp("https://t.me/nobody")                 # t.me увёл с /s/ — не канал
        return _Resp(req.full_url)

    monkeypatch.setattr(ca.urllib.request, "urlopen", fake_urlopen)
    assert ca.fetch_tme_html("nobody") is None
    assert ca.fetch_tme_html("chan", before=77) == "<html>ok</html>"
    assert seen[1][0] == "https://t.me/s/chan?before=77"
    assert "Mozilla" in seen[1][1] and seen[1][2] == ca.TIMEOUT_S


# --- классы постов ---

@pytest.mark.parametrize("txt,kind", [
    ("#BTC/USDT LONG\nВход: 64200\nСтоп: 63500\nTP1: 65300", "signal"),
    ("#UAI $UAI #UAIUSDT $UAIUSDT  LONG", "no_stop"),
    ("Coin: #SEI/USDT\nSet-up: LONG\nEntry: 0.625 - 0.689\nTarget: 0.75 - 0.80", "no_stop"),
    ("#UAI $UAI #UAIUSDT $UAIUSDT  LONG НЫТИКИ, ТП 1", "other"),            # отчёт о цели
    ("TRBUSDT LONG UPDATE", "other"),
    ("#BTC/USDT Take-Profit target 1 ✅\nProfit: 4.1696% 📈", "other"),
    ("🛑 Сигнал #745 — стоп\n🟢 LONG   DOGE/USDT:USDT   ·   1h\nРезультат: -1.00R", "other"),
    ("🔒 СИГНАЛ #744 — превью\n🟢 LONG   1000PEPE/USDT:USDT   ·   1h\n"
     "Вход, стоп, тейки и разбор — по подписке.", "other"),                     # наш тизер
    ("Important Overall Prediction November (Short Term) 2026\n"
     "https://www.tradingview.com/chart/USDT.D/CzAHSAky-US", "other"),
    ("FOCUS ON THE LONG TERM!\n$BTC", "other"),
    ("#BTC is squeezing shorts, long liquidations " + "and more words " * 30, "other"),
    ("🏴AUDIO/USDT | ШОРТ | X20 | Вход : 1.3$ и выше\nВыход : 1.17$\n"
     "После первой цели, всегда ставится стоп лосс в безубыток", "unparsed"),
    ("Биткоин сегодня интересно себя ведёт", "other"),
    ("", "other"),
])
def test_classify(txt, kind):
    assert ca.classify(txt)[0] == kind


def test_quick_call_next_to_full_signal_is_not_no_stop():
    full = "#ICNT/USDT SHORT\nВход: 0.19\nСтоп: 0.2\nTP1: 0.18"
    near = [_post(1, T0, "Short #ICNT Now 50X"), _post(2, T0 + pd.Timedelta("4min"), full)]
    agg = ca.audit(near, {}, T0 + pd.Timedelta("1D"))
    assert agg["no_stop"] == 0 and agg["signals"] == 1
    far = [_post(1, T0 - pd.Timedelta("3D"), "Short #ICNT Now 50X"), near[1]]
    assert ca.audit(far, {}, T0 + pd.Timedelta("1D"))["no_stop"] == 1


# --- план свечей ---

def test_fetch_plan_one_request_per_symbol_tf_from_oldest():
    now = T0
    posts = [
        _post(1, now - pd.Timedelta("20D"), "#AAA/USDT LONG\nВход: 10\nСтоп: 9\nTP1: 11"),
        _post(2, now - pd.Timedelta("2D"), "#AAA/USDT SHORT\nВход: 12\nСтоп: 13\nTP1: 11"),
        _post(3, now - pd.Timedelta("3h"), "#BBB/USDT LONG\nВход: 5\nСтоп: 4\nTP1: 6"),
        _post(4, now - pd.Timedelta("100D"), "#CCC/USDT LONG\nВход: 5\nСтоп: 4\nTP1: 6"),
        _post(5, now - pd.Timedelta("200D"), "#DDD/USDT LONG\nВход: 5\nСтоп: 4\nTP1: 6"),
    ]
    plan = ca.fetch_plan(posts, now)
    assert plan == {"AAA/USDT:USDT": ("1h", 20 * 24 + 3), "BBB/USDT:USDT": ("15m", 12 + 3),
                    "CCC/USDT:USDT": ("4h", 100 * 6 + 3)}               # DDD старше 180 дней
    many = [_post(i, now - pd.Timedelta(hours=i), f"#C{i:02d}X/USDT LONG\nВход: 5\nСтоп: 4")
            for i in range(1, 36)]
    capped = ca.fetch_plan(many, now)
    assert len(capped) == ca.MAX_SYMBOLS and "C01X/USDT:USDT" in capped   # свежие в приоритете
    assert "C35X/USDT:USDT" not in capped


# --- итог на первой цели ---

LONG = sc.ParsedSignal("AAA", "LONG", 100.0, 100.0, 90.0, [110.0, 120.0])
SHORT = sc.ParsedSignal("BBB", "SHORT", 50.0, 50.0, 55.0, [45.0])


@pytest.mark.parametrize("sig,res,r", [
    (LONG, sc.CheckResult("open", fill_price=100.0, hit=1), 1.0),
    (LONG, sc.CheckResult("stop", fill_price=100.0, hit=1), 1.0),          # TP1, потом стоп
    (LONG, sc.CheckResult("all_tp", fill_price=100.0, hit=2), 1.0),
    (LONG, sc.CheckResult("open", fill_price=102.0, hit=1), 8 / 12),       # хуже вход — меньше R
    (LONG, sc.CheckResult("open", fill_price=111.0, hit=1), 0.0),          # цель уже позади
    (LONG, sc.CheckResult("stop", fill_price=100.0, hit=0), -1.0),
    (LONG, sc.CheckResult("open", fill_price=100.0, hit=0), None),
    (LONG, sc.CheckResult("no_fill"), None),
    (SHORT, sc.CheckResult("open", fill_price=50.0, hit=1), 1.0),
    (sc.ParsedSignal("C", "LONG", 1.0, 1.0, 0.9, []), sc.CheckResult("stop", fill_price=1.0), -1.0),
    (sc.ParsedSignal("C", "LONG", 1.0, 1.0, 0.9, []), sc.CheckResult("open", fill_price=1.0), None),
])
def test_tp1_r(sig, res, r):
    got = ca.tp1_r(sig, res)
    assert got == (None if r is None else pytest.approx(r))


def _scenario():
    """Канал из 12 постов на все ветки агрегата; свечи 15m с T0."""
    now = T0 + pd.Timedelta("2D")
    h = pd.Timedelta("1h")
    posts = [
        _post(1, T0, "#AAA/USDT LONG\nВход: 100\nСтоп: 90\nTP1: 110\nTP2: 120"),
        _post(2, T0 + h, "#BBB/USDT SHORT\nВход: 50\nСтоп: 55\nTP1: 45"),
        _post(3, T0 + 2 * h, "#CCC/USDT LONG\nВход: 10\nСтоп: 9\nTP1: 11"),
        _post(4, T0 + 3 * h, "#DDD/USDT LONG\nВход: 20\nСтоп: 18\nTP1: 25"),
        _post(5, T0 + 4 * h, "#EEE/USDT LONG\nВход: 30\nСтоп: 27"),
        _post(6, T0 + 5 * h, "#AAA/USDT LONG\nВход: 100\nСтоп: 90\nTP1: 110\nTP2: 120"),
        _post(7, T0 - pd.Timedelta("200D"), "#FFF/USDT LONG\nВход: 5\nСтоп: 4\nTP1: 6"),
        _post(8, T0 + 6 * h, "#GGG/USDT LONG\nВход: 7\nСтоп: 6\nTP1: 8"),
        _post(9, T0 + 7 * h, "#HHH $HHH LONG"),
        _post(10, T0 + 8 * h, "#HHH LONG ТП 1"),
        _post(11, T0 + 9 * h, "Биткоин растёт, всем хорошего дня"),
        _post(12, T0 + 10 * h, "#III/USDT SHORT\nВход 10\nстоп ставим позже"),
    ]
    flat = [(1, 1, 1, 1)] * 4                             # час до поста — мимо
    bars = {
        "AAA/USDT:USDT": _bars([(105, 106, 99, 101), (101, 111, 100, 110), (110, 111, 89, 90)]),
        "BBB/USDT:USDT": _bars([(49, 49.5, 48.5, 49)] * 4
                               + [(49, 50.5, 48.5, 50), (50, 56, 49, 55.5)]),
        "CCC/USDT:USDT": _bars(flat * 2 + [(12, 13, 11.5, 12.5)] * 6),
        "DDD/USDT:USDT": _bars(flat * 3 + [(20, 21, 19.5, 20.5)] * 6),
        "EEE/USDT:USDT": _bars(flat * 4 + [(30, 31, 29.5, 30.5), (30.5, 31, 26, 26.5)]),
    }
    return posts, bars, now


def test_audit_counts_every_branch():
    posts, bars, now = _scenario()
    agg = ca.audit(posts, bars, now)
    assert agg["posts"] == 12
    assert agg["first"] == posts[6].posted and agg["last"] == posts[11].posted
    assert (agg["signals"], agg["duplicates"], agg["too_old"], agg["no_bars"]) == (7, 1, 1, 1)
    assert (agg["checked"], agg["no_fill"], agg["filled"]) == (5, 1, 4)
    assert (agg["tp1"], agg["stop_first"], agg["open"]) == (1, 2, 1)
    assert (agg["no_stop"], agg["unparsed"], agg["no_targets"]) == (1, 1, 1)
    # TP1 (+1R) + стоп (−1R) + стоп без целей (−1R); открытая и не исполнившаяся — вне итога
    assert agg["r_total"] == -1.0 and agg["r_trades"] == 3 and agg["tp1_avg_r"] == 1.0
    assert [t["outcome"] for t in agg["trades"]] == ["tp1", "stop", "no_fill", "open", "stop"]
    assert agg["trades"][0]["url"] == "https://t.me/testsig/1" and agg["tfs"] == ["15m"]


def test_audit_empty_channel():
    agg = ca.audit([], {}, T0)
    assert agg["posts"] == 0 and agg["first"] is None and not ca.card_worthy(agg)
    assert "не нашёл сигналов" in ca.render_audit("quiet", None, agg)


def test_ambiguous_bar_counts_as_stop():
    post = _post(1, T0, "#AAA/USDT LONG\nВход: 100\nСтоп: 90\nTP1: 110")
    bars = {"AAA/USDT:USDT": _bars([(101, 101, 99, 100), (100, 115, 85, 100)])}
    agg = ca.audit([post], bars, T0 + pd.Timedelta("1D"))
    assert agg["stop_first"] == 1 and agg["ambiguous"] == 1 and agg["r_total"] == -1.0
    assert "считаем стоп (таких: 1)" in ca.render_audit("testsig", None, agg)


def test_comma_thousands_resolved_by_market_or_left_out():
    """«81,500»: прочтение выбирает цена рынка в момент поста; не решила и она — формат
    боту непонятен, сигнал в итог не входит (а не проверяется по чужим уровням)."""
    post = _post(1, T0, "#AAA/USDT LONG\nВход: 81,500\nСтоп: 80,900\nTP1: 83,000")
    now = T0 + pd.Timedelta("1D")
    hit = _bars([(81600, 81700, 81400, 81500), (81500, 83100, 81400, 83000)])
    agg = ca.audit([post], {"AAA/USDT:USDT": hit}, now)
    assert (agg["signals"], agg["checked"], agg["tp1"], agg["unparsed"]) == (1, 1, 1, 0)
    far = _bars([(5.0, 5.1, 4.9, 5.0)])
    agg = ca.audit([post], {"AAA/USDT:USDT": far}, now)
    assert (agg["signals"], agg["checked"], agg["unparsed"]) == (0, 0, 1)


# --- тексты и карточка ---

_LEAKS = ("6226", "LTF", "1:1", "SMC", "sub-score", "суб-скор")


def test_render_texts_and_card():
    posts, bars, now = _scenario()
    agg = ca.audit(posts, bars, now)
    title = "Сигналы <script>alert(1)</script>"
    txt = ca.render_audit("testsig", title, agg)
    assert "Проверка канала @testsig" in txt
    assert "До первой цели: <b>1 из 4 (25%)</b> · цель в среднем +1.0R" in txt
    assert "Сразу в стоп: <b>2 из 4 (50%)</b>" in txt
    assert "Без стопа: <b>1</b>" in txt and "-1.0R</b> за 3 сделки" in txt
    assert '<a href="https://t.me/testsig/1">AAA LONG</a> — TP1 ✅ +1.0R' in txt
    assert "Малая выборка — 4 сделки" in txt and "Не финансовый совет" in txt
    assert "задним числом" in txt and "<script>" not in txt and "&lt;script&gt;" in txt
    assert len(txt) < 4096
    cap = ca.audit_caption("testsig", agg)
    assert "до первой цели — 25%" in cap and "итог на TP1 — -1.0R" in cap and len(cap) < 1024
    card = ca.audit_card_html("testsig", title, agg)
    for s in ("До первой цели", "25%", "Сразу в стоп", "50%", "Без стопа", "Итог на TP1",
              "−1.0R", "Проверено по свечам <b>Bybit</b>", "@rsirange_bot", "Не финансовый совет",
              "Малая выборка", 'class="sq tp1"', 'class="sq no_fill"'):
        assert s in card, s
    assert "<script>" not in card
    for leak in _LEAKS:
        assert leak not in txt and leak not in cap and leak not in card and leak not in ca.HELP


def test_small_r_values_keep_precision():
    assert ca._r_fmt(0.0712) == "+0.07R" and ca._r_fmt(-0.026) == "-0.03R"
    assert ca._r_fmt(-0.001) == "0.00R" and ca._r_fmt(-1.0) == "-1.0R"
    assert ca._r_fmt(5.24) == "+5.2R"


def _boot_bot(monkeypatch):
    """Регистрация хэндлеров main() без запуска: asyncio.run подменён (polling и фоновые
    задачи не стартуют), .env не читается, сети нет. -> (dispatcher, настоящий asyncio.run)."""
    import asyncio
    import logging

    import aiogram

    from app.config import get_settings
    from app.delivery import bot

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST")
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    get_settings.cache_clear()
    real_run, got = asyncio.run, {}

    class _Dp(aiogram.Dispatcher):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            got["dp"] = self

    monkeypatch.setattr(aiogram, "Dispatcher", _Dp)
    monkeypatch.setattr(asyncio, "run", lambda coro: coro.close())
    try:
        bot.main()
    finally:
        logging.getLogger().removeHandler(bot._ERRBUF)
    return got["dp"], real_run


def test_bot_routes_channel_reference(fresh_db, monkeypatch):
    """main() регистрирует хэндлеры без ошибок, а голый «@канал» в личке уходит в аудит —
    не перехватывая шаги FSM, кнопки меню, пересланные посты и группы. Маршрут — как у
    aiogram: первый хэндлер по порядку регистрации, чьи фильтры прошли."""
    from datetime import datetime, timezone

    import aiogram
    from aiogram.fsm.state import State
    from aiogram.types import Chat, Message, MessageOriginChannel, User

    dp, real_run = _boot_bot(monkeypatch)
    handlers = dp.message.handlers
    tg = aiogram.Bot(token="123456:TEST")

    def state_of(name):                        # State в aiogram 3 — сам себе фильтр
        h = next(h for h in handlers if h.callback.__name__ == name)
        return next(f.callback.state for f in h.filters if isinstance(f.callback, State))

    def route(text, chat="private", state=None, forwarded=False):
        now = datetime.now(timezone.utc)
        msg = Message(message_id=1, date=now, chat=Chat(id=42, type=chat), text=text,
                      from_user=User(id=42, is_bot=False, first_name="U"),
                      forward_origin=MessageOriginChannel(
                          type="channel", date=now, chat=Chat(id=-1001, type="channel"),
                          message_id=5) if forwarded else None)

        async def first():
            for h in handlers:
                if (await h.check(msg, bot=tg, raw_state=state))[0]:
                    return h.callback.__name__
            return None
        return real_run(first())

    assert route("@oracle_easy") == "_audit_ref"
    assert route("https://t.me/s/oracle_easy") == "_audit_ref"
    assert route("/audit @oracle_easy") == "_audit_cmd"
    assert route("/audit") == "_audit_cmd"
    assert route("/audit @oracle_easy", chat="group") is None     # чужой разбор — не в группу
    assert route("@oracle_easy", state=state_of("_promo_step_code")) == "_promo_step_code"
    assert route("@oracle_easy", state=state_of("_calc_step_deposit")) == "_calc_step_deposit"
    assert route("@oracle_easy", forwarded=True) == "_check_forward"
    assert route("📊 Статистика") == "_stats"
    assert route("@oracle_easy", chat="group") is None
    assert route("привет, как дела?") is None
    # открыт опрос «как вам бот» — свободный текст становится отзывом, остальное как было
    from app.delivery import feedback
    feedback.record_send(42, "sent")
    assert route("привет, как дела?") == "_survey_reply"
    assert route("@oracle_easy") == "_audit_ref"
    assert route("📊 Статистика") == "_stats"


class _Sent:
    def __init__(self, log, text):
        self.log, self.text = log, text

    async def edit_text(self, text, **kw):
        self.log.append(("edit", text, kw))

    async def delete(self):
        self.log.append(("delete", self.text, {}))


class _Msg:
    """Входящее сообщение лички: ответы складываются в общий журнал."""

    def __init__(self, text, log, uid=42):
        from types import SimpleNamespace
        self.text, self.log = text, log
        self.from_user = SimpleNamespace(id=uid)
        self.chat = SimpleNamespace(id=uid, type="private")

    async def answer(self, text, **kw):
        self.log.append(("text", text, kw))
        return _Sent(self.log, text)

    async def answer_photo(self, photo, **kw):
        self.log.append(("photo", photo, kw))
        return _Sent(self.log, "")


def test_bot_audit_flow_end_to_end(fresh_db, monkeypatch):
    """Хэндлер целиком на подменах (t.me, Bybit, рендер): статус -> карточка с подписью ->
    подробный текст с кнопками; свечи монеты — один запрос, 1000-лоты Bybit делятся на 1000;
    лимит 3 в сутки, опечатка в username лимит не тратит."""
    from app.data import market_data
    from app.delivery import graphics

    dp, real_run = _boot_bot(monkeypatch)
    ref = next(h.callback for h in dp.message.handlers if h.callback.__name__ == "_audit_ref")
    now = pd.Timestamp.now(tz="UTC").floor("15min")
    posted = now - pd.Timedelta("3h")
    posts = [_post(1, posted, "#AAA/USDT LONG\nВход: 100\nСтоп: 90\nTP1: 110"),
             _post(2, posted, "#PEPE/USDT SHORT\nВход: 0.00002\nСтоп: 0.000021\nTP1: 0.000019"),
             _post(3, posted, "#UAI LONG")]
    monkeypatch.setattr(ca, "fetch_channel",
                        lambda name: ca.TmePage("Test <b>", posts, None))
    calls = []

    def fake_history(sym, tf, n, exchange_id=None):
        calls.append((sym, tf, n))
        if sym == "1000PEPE/USDT:USDT":                      # цены 1000-лота: ×1000
            return _bars([(0.0199, 0.0201, 0.0198, 0.0200), (0.0200, 0.0211, 0.0185, 0.0189)]
                          + [(0.0189, 0.0190, 0.0188, 0.0189)] * 10, start=posted)
        return _bars([(105, 106, 99, 101), (101, 111, 100, 110)] + [(110, 111, 109, 110)] * 10,
                     start=posted)

    class _Ex:
        markets = {"AAA/USDT:USDT": {}, "1000PEPE/USDT:USDT": {}}

    monkeypatch.setattr(market_data, "fetch_history", fake_history)
    monkeypatch.setattr(market_data, "_exchange", lambda exchange_id: _Ex())
    monkeypatch.setattr(graphics, "render_html_to_png", lambda html, *a: b"PNG")

    log: list = []
    real_run(ref(_Msg("@testsig", log)))
    kinds = [k for k, _, _ in log]
    assert kinds == ["text", "edit", "photo", "text", "delete"]
    assert log[0][1] == "🔎 Читаю канал @testsig…"
    assert log[1][1] == "🔎 Читаю канал… проверяю 2 сигнала по свечам биржи"
    photo_kw = log[2][2]
    assert "до первой цели — 50%" in photo_kw["caption"] and photo_kw["parse_mode"] == "HTML"
    assert log[2][1].filename == "audit_testsig.png"
    txt, kw = log[3][1], log[3][2]
    assert "AAA LONG</a> — TP1 ✅ +1.0R" in txt and "PEPE SHORT</a> — стоп ❌" in txt
    assert "Без стопа: <b>1</b>" in txt and "&lt;b&gt;" in txt and "Не финансовый совет" in txt
    rows = kw["reply_markup"].inline_keyboard
    assert rows[-1][0].callback_data == "audit_again" and kw["disable_web_page_preview"]
    assert sorted(calls) == [("1000PEPE/USDT:USDT", "15m", 15), ("AAA/USDT:USDT", "15m", 15)]

    log.clear()
    real_run(ref(_Msg("@testsig", log)))
    real_run(ref(_Msg("@testsig", log)))
    log.clear()
    real_run(ref(_Msg("@testsig", log)))                      # четвёртая за сутки
    assert log == [("text", "Лимит — 3 проверки канала в сутки. Завтра можно снова, а "
                    "отдельный сигнал — пересылкой сюда (/check).", {})]
    log.clear()

    def unavailable(name):
        raise ca.ChannelUnavailable(name)

    monkeypatch.setattr(ca, "fetch_channel", unavailable)
    real_run(ref(_Msg("@nobody_here", log, uid=7)))
    real_run(ref(_Msg("@nobody_here", log, uid=7)))
    real_run(ref(_Msg("@nobody_here", log, uid=7)))
    real_run(ref(_Msg("@nobody_here", log, uid=7)))           # опечатки лимит не съели
    assert [e[1] for e in log if e[0] == "edit"][-1].startswith("Не нашёл публичный канал")
    assert not any("Лимит" in e[1] for e in log)
    log.clear()
    real_run(ref(_Msg("@rsi_range", log, uid=8)))
    assert len(log) == 1 and log[0][1].startswith("Это наш канал")


def test_progress_and_long_names():
    assert ca.progress_text(1) == "🔎 Читаю канал… проверяю 1 сигнал по свечам биржи"
    assert "22 сигнала" in ca.progress_text(22) and "25 сигналов" in ca.progress_text(25)
    agg = ca.audit([], {}, T0)
    assert "font-size:58px" in ca.audit_card_html("short_name", None, agg)
    assert "font-size:40px" in ca.audit_card_html("a" * 32, None, agg)
