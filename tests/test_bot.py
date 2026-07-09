"""Тесты обвязки бота: scan_frames (движок -> журнал) и рендер команд."""
from __future__ import annotations

import pandas as pd
import pytest

from app.brain.smc_setups import SMCConfig, SMCEngine
from app.delivery import bot

_BARS_SHORT = [
    (100, 100.5, 99.5, 100), (100, 102.5, 99.8, 102), (102, 104.8, 101.5, 104),
    (104, 103.0, 101.8, 102), (102, 102.2, 100.5, 101), (101, 103.5, 100.8, 103),
    (103, 107.5, 102.8, 107), (107, 110.8, 106.5, 110), (110, 108.5, 106.8, 107),
    (107, 106.0, 104.5, 105), (105, 109.5, 104.8, 109), (109, 113.5, 108.5, 108),
    (108, 108.2, 103.5, 104), (104, 104.2, 99.5, 100), (100, 100.2, 95.5, 96),
    (96, 96.2, 93.5, 94),
]


def _df():
    o, h, low, c = zip(*_BARS_SHORT)
    return pd.DataFrame({"open": o, "high": h, "low": low, "close": c,
                         "volume": [1000.0] * len(_BARS_SHORT)})


def test_scan_frames_records_signal(fresh_db):
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    sig = bot.scan_frames("BTC/USDT:USDT", {"m15": _df()}, eng)
    assert sig is not None
    row = fresh_db.query_one("SELECT * FROM signals WHERE symbol=?", ("BTC/USDT:USDT",))
    assert row is not None and row["status"] == "open"
    # записанный сигнал виден в статистике как открытый
    from app.brain import journal
    assert journal.stats()["open"] == 1


def test_scan_frames_no_record(fresh_db):
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    sig = bot.scan_frames("BTC/USDT:USDT", {"m15": _df()}, eng, record=False)
    assert sig is not None
    assert fresh_db.query_one("SELECT COUNT(*) AS c FROM signals")["c"] == 0


def test_scan_auto_records_open_and_dedups(fresh_db):
    """Авто-режим: сигнал сразу open (в журнале/статистике), повтор — дедуп."""
    from app.brain import journal
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    res = bot.scan_auto("BTC/USDT:USDT", {"m15": _df()}, eng)
    assert res is not None
    sig, sid = res
    assert fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))["status"] == "open"
    assert journal.stats()["open"] == 1                 # сразу в статистике как открытый
    assert bot.scan_auto("BTC/USDT:USDT", {"m15": _df()}, eng) is None  # дедуп


def test_scan_semiauto_dedups_same_setup(fresh_db):
    """Тот же импульс на повторном скане не создаёт второго кандидата (и алерта)."""
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    first = bot.scan_semiauto("BTC/USDT:USDT", {"m15": _df()}, eng)
    assert first is not None
    again = bot.scan_semiauto("BTC/USDT:USDT", {"m15": _df()}, eng)
    assert again is None                                # дедуп по (symbol,tf,side,entry,stop)
    assert fresh_db.query_one("SELECT COUNT(*) AS c FROM signals")["c"] == 1
    # после решения оператора тот же сетап тоже не пере-регистрируется
    from app.brain import journal
    journal.reject_candidate(first[1])
    assert bot.scan_semiauto("BTC/USDT:USDT", {"m15": _df()}, eng) is None


def test_scan_semiauto_records_candidate(fresh_db):
    from app.brain import journal
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    res = bot.scan_semiauto("BTC/USDT:USDT", {"m15": _df()}, eng)
    assert res is not None
    sig, sid = res
    row = fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))
    assert row["status"] == "candidate"                 # не авто-open, а кандидат
    alert = bot.format_candidate_alert(sig, sid)
    assert f"#{sid}" in alert and "/confirm" in alert and "killzone" in alert
    # оператор подтверждает контекст -> активен
    assert journal.confirm_candidate(sid) is True
    assert fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))["status"] == "open"


def test_parse_id_arg():
    assert bot.parse_id_arg("/confirm 12") == 12
    assert bot.parse_id_arg("/reject 7") == 7
    assert bot.parse_id_arg("/confirm") is None
    assert bot.parse_id_arg("/confirm abc") is None
    assert bot.parse_id_arg("/confirm ²³") is None      # isdigit-ловушка: не должно падать
    assert bot.parse_id_arg("") is None
    assert bot.parse_id_arg(None) is None


def test_handle_confirm_reject_flow(fresh_db):
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    _, sid = bot.scan_semiauto("BTC/USDT:USDT", {"m15": _df()}, eng)

    listing = bot.handle_candidates()
    assert f"#{sid}" in listing                        # кандидат виден в списке

    assert "подтверждён" in bot.handle_confirm(sid)
    assert fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))["status"] == "open"
    assert "не найден" in bot.handle_confirm(sid)      # повторно — уже не кандидат
    assert "Использование" in bot.handle_confirm(None)
    assert bot.handle_candidates() == "Кандидатов нет — жду следующего закрытия свечи."


def test_handle_reject(fresh_db):
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    _, sid = bot.scan_semiauto("BTC/USDT:USDT", {"m15": _df()}, eng)
    assert "отклонён" in bot.handle_reject(sid)
    row = fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))
    assert row["status"] == "rejected"


def test_format_stats_and_calc():
    empty = bot.format_stats({"trades": 0, "open": 0})
    assert "пустая" in empty
    filled = bot.format_stats({"trades": 10, "open": 2, "wins": 6, "winrate": 0.6,
                               "expectancy_r": 0.4, "total_r": 4.0, "losses": 4})
    assert "Winrate: 60%" in filled
    calc = bot.format_calc(1000, 2.0, 100.0, 98.0)
    assert "Объём" in calc and "Плечо" in calc


def test_format_about_contains_disclaimer_parts():
    txt = bot.format_about()
    assert "НЕ инвестиционный совет" in txt
    assert "/backtest" in txt
    assert "18+" in txt
    assert "это не мы" in txt
    assert "Cornix" in txt


def test_format_backtest_summary():
    txt = bot.format_backtest_summary()
    assert "смоделированный" in txt          # CFTC 4.41-стиль дисклеймера
    assert "НЕ обнаружено" in txt
    assert "/stats" in txt
    assert "hindsight" in txt
    # /backtest — статический текст, не зависит от БД (не упадёт на пустом контексте)


def test_format_glossary():
    txt = bot.format_glossary()
    for term in ("BOS", "CHOCH", "FVG", "killzone", "Setup 1:1", "Свип"):
        assert term.lower() in txt.lower(), term


def test_format_cornix_block():
    from app.core.signal_engine import Side, Signal
    sig = Signal(symbol="BTC/USDT:USDT", side=Side.LONG, timeframe="15m", entry=61900.0,
                 stop=61600.0, take_profits=[62800.0, 63500.0], risk_reward=3.0,
                 confidence=0.68, reasons=[], created_at="2026-01-01T00:00:00+00:00",
                 indicators_snapshot={})
    txt = bot.format_cornix_block(sig)
    assert "Pair: BTC/USDT" in txt and ":USDT" not in txt.split("Pair: ")[1].split("\n")[0]
    assert "Type: Long" in txt
    assert "Entry: 61900" in txt
    assert "Take-Profit 1: 62800" in txt
    assert "Take-Profit 2: 63500" in txt
    assert "Stop-Loss: 61600" in txt          # полное слово, не S/L
    assert "Exchange: Bybit" in txt


def test_cornix_price_precision():
    # {:g} режет до 6 значащих цифр (BTC >100k) и даёт экспоненту на мелких альтах —
    # машиночитаемый блок обязан сохранять точность в фиксированной записи
    assert bot._cornix_price(109853.25) == "109853.25"
    assert bot._cornix_price(0.00001234) == "0.00001234"
    assert bot._cornix_price(61900.0) == "61900"
    assert "e" not in bot._cornix_price(0.00001234).lower()


def test_stats_small_sample_and_no_manual_edits_marks():
    small = bot.format_stats({"trades": 10, "open": 2, "wins": 6, "winrate": 0.6,
                              "expectancy_r": 0.4, "total_r": 4.0, "losses": 4})
    assert "Выборка пока мала" in small and "100+" in small
    assert "без ручных правок" in small

    big = bot.format_stats({"trades": 150, "open": 2, "wins": 90, "winrate": 0.6,
                            "expectancy_r": 0.4, "total_r": 60.0, "losses": 60})
    assert "Выборка пока мала" not in big
    assert "без ручных правок" in big

    ch_small = bot.format_channel_stats({"trades": 10, "open": 1, "winrate": 0.5,
                                         "expectancy_r": 0.1, "total_r": 1.0})
    assert "Выборка пока мала" in ch_small and "без ручных правок" in ch_small
    ch_empty = bot.format_channel_stats({"trades": 0, "open": 0})
    assert "без ручных правок" in ch_empty


def test_format_channel_signal():
    from app.core.signal_engine import Side, Signal
    sig = Signal(symbol="BTC/USDT:USDT", side=Side.SHORT, timeframe="15m", entry=62340.0,
                 stop=62980.0, take_profits=[60893.0, 59000.0], risk_reward=2.26,
                 confidence=0.65, reasons=["свип хая", "слом CHOCH"],
                 created_at="2026-01-01T00:00:00+00:00", indicators_snapshot={"setup": "6226"})
    txt = bot.format_channel_signal(sig, 7)
    assert "RSI RANGE" in txt and "Сигнал #7" in txt and "🔴 SHORT" in txt
    assert "62340" in txt and "60893 / 59000" in txt and "@rsirange_bot" in txt


def test_format_channel_result():
    tp = bot.format_channel_result({"id": 5, "symbol": "ETH/USDT:USDT", "side": "LONG",
                                    "timeframe": "15m", "status": "tp1", "result_r": 2.0})
    assert "✅" in tp and "#5" in tp and "+2.00R" in tp
    st = bot.format_channel_result({"id": 6, "symbol": "BTC/USDT:USDT", "side": "SHORT",
                                    "timeframe": "1h", "status": "stopped", "result_r": -1.0})
    assert "🛑" in st and "-1.00R" in st


def test_format_channel_stats():
    assert "копятся" in bot.format_channel_stats({"trades": 0, "open": 0})
    txt = bot.format_channel_stats({"trades": 20, "open": 3, "winrate": 0.55,
                                    "expectancy_r": 0.2, "total_r": 4.0})
    assert "Winrate: 55%" in txt and "+0.20R" in txt


def test_should_refresh_board():
    assert bot.should_refresh_board(None, "2026-01-02T00:00:00+00:00") is True
    assert bot.should_refresh_board("2026-01-01T00:00:00+00:00",
                                    "2026-01-01T12:00:00+00:00", hours=24) is False
    assert bot.should_refresh_board("2026-01-01T00:00:00+00:00",
                                    "2026-01-02T00:00:00+00:00", hours=24) is True
    assert bot.should_refresh_board("2026-01-01T00:00:00+00:00",
                                    "2026-01-02T00:00:00+00:00", hours=48) is False


def test_format_channel_live_board():
    empty = bot.format_channel_live_board({"trades": 0, "open": 0}, {})
    assert "живое табло" in empty and "копятся" in empty

    st = {"trades": 40, "open": 3, "wins": 24, "winrate": 0.6,
          "expectancy_r": 0.35, "total_r": 14.0}
    by_symbol = {"BTC/USDT:USDT": {"trades": 20, "wins": 12, "winrate": 0.6,
                                   "expectancy_r": 0.4, "total_r": 8.0}}
    txt = bot.format_channel_live_board(st, by_symbol)
    assert "40" in txt and "BTC/USDT:USDT" in txt and "включая убыточные" in txt
    assert "без ручных правок" in txt

    cohort = {"confirmed": {"trades": 10, "expectancy_r": 0.5},
             "rejected": {"trades": 8, "expectancy_r": -0.1}, "edge_r": 0.6}
    txt2 = bot.format_channel_live_board(st, {}, cohort=cohort)
    assert "Edge оператора: +0.60R" in txt2

    by_setup = {"LTF-BB": {"trades": 5, "wins": 1, "winrate": 0.2,
                           "expectancy_r": -0.2, "total_r": -1.0}}
    txt3 = bot.format_channel_live_board(st, {}, by_setup=by_setup)
    assert "По сетапам" in txt3 and "LTF-BB" in txt3


def test_format_report():
    cs = {
        "confirmed": {"trades": 40, "wins": 24, "winrate": 0.6,
                      "expectancy_r": 0.35, "total_r": 14.0},
        "rejected": {"trades": 35, "wins": 14, "winrate": 0.4,
                     "expectancy_r": -0.1, "total_r": -3.5},
        "all": {"trades": 75, "wins": 38, "winrate": 0.5067,
                "expectancy_r": 0.14, "total_r": 10.5},
        "pending_candidates": 2,
        "edge_r": 0.45,
    }
    txt = bot.format_report(cs)
    assert "Edge оператора: +0.45R" in txt
    assert "Подтверждённые: 40 сд" in txt
    assert "Ожидают решения: 2" in txt
    assert "Выборка мала" not in txt                    # обе когорты >= 30

    cs["rejected"]["trades"] = 5
    assert "Выборка мала" in bot.format_report(cs)

    cs["confirmed"] = {"trades": 0, "wins": 0, "winrate": 0.0, "expectancy_r": 0.0, "total_r": 0.0}
    assert "закрытых сделок нет" in bot.format_report(cs)


def test_build_trackrecord_csv_roundtrips_and_hashes():
    import csv
    import io

    rows = [
        {"id": 1, "symbol": "BTC/USDT:USDT", "side": "LONG", "timeframe": "15m", "entry": 100.0,
         "stop": 98.0, "tp1": 104.0, "tp2": None, "tp3": None, "status": "tp1", "result_r": 2.0,
         "created_at": "2026-01-01T00:00:00+00:00", "closed_at": "2026-01-01T01:00:00+00:00"},
    ]
    csv_bytes = bot.build_trackrecord_csv(rows)
    assert isinstance(csv_bytes, bytes)

    parsed = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))
    assert parsed[0] == list(bot._TRACKRECORD_COLS)              # заголовок
    assert parsed[1][0] == "1" and parsed[1][1] == "BTC/USDT:USDT"

    h1 = bot.trackrecord_checksum(csv_bytes)
    h2 = bot.trackrecord_checksum(csv_bytes)
    assert h1 == h2 and len(h1) == 64                            # sha256 hex — детерминирован
    assert bot.trackrecord_checksum(csv_bytes + b"x") != h1       # другие данные -> другой хэш


def test_should_post_weekly():
    assert bot.should_post_weekly(None, "2026-W01") is True       # ещё не постили
    assert bot.should_post_weekly("2026-W01", "2026-W01") is False  # та же неделя — не дублировать
    assert bot.should_post_weekly("2026-W01", "2026-W02") is True   # новая неделя


def test_should_alert_backup_stale():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    fresh = (now - timedelta(hours=1)).isoformat()
    stale = (now - timedelta(hours=30)).isoformat()

    assert bot.should_alert_backup_stale(None, None, now_iso) is True       # бэкапа не было
    assert bot.should_alert_backup_stale(fresh, None, now_iso) is False     # свежий — не пора
    assert bot.should_alert_backup_stale(stale, None, now_iso) is True      # устарел, не алертили

    recent_alert = (now - timedelta(hours=2)).isoformat()
    assert bot.should_alert_backup_stale(stale, recent_alert, now_iso) is False  # троттлинг

    old_alert = (now - timedelta(hours=25)).isoformat()
    assert bot.should_alert_backup_stale(stale, old_alert, now_iso) is True      # троттлинг истёк


def test_setup_codes_roundtrip():
    # каждый известный сетап имеет код, и код однозначно возвращается обратно в тег
    assert set(bot._KNOWN_SETUPS) == set(bot._SETUP_CODES)
    for tag in bot._KNOWN_SETUPS:
        code = bot._SETUP_CODES[tag]
        assert bot._SETUP_CODES_REV[code] == tag
    # коды короткие и без спецсимволов (безопасны для callback_data)
    for code in bot._SETUP_CODES.values():
        assert code.isalnum()


def test_parse_admin_field_value_int():
    assert bot._parse_admin_field_value("1000", "int") == 1000
    assert bot._parse_admin_field_value("  42 ", "int") == 42
    with pytest.raises(AssertionError):
        bot._parse_admin_field_value("1.5", "int")           # не целое
    with pytest.raises(AssertionError):
        bot._parse_admin_field_value("-1", "int")             # отрицательное


def test_parse_admin_field_value_unit():
    assert bot._parse_admin_field_value("0.6", "unit") == 0.6
    assert bot._parse_admin_field_value("0,6", "unit") == 0.6  # запятая как разделитель
    with pytest.raises(AssertionError):
        bot._parse_admin_field_value("1.1", "unit")           # вне 0..1
    with pytest.raises(AssertionError):
        bot._parse_admin_field_value("-0.1", "unit")


def test_parse_admin_field_value_float():
    assert bot._parse_admin_field_value("1.5", "float") == 1.5
    assert bot._parse_admin_field_value("0", "float") == 0.0
    with pytest.raises(AssertionError):
        bot._parse_admin_field_value("-0.5", "float")
    with pytest.raises(ValueError):
        bot._parse_admin_field_value("не число", "float")


def test_format_growth():
    breakdown = {
        "kol1": {"total": 10, "paid": 3},
        "—": {"total": 5, "paid": 0},
        "ads2": {"total": 2, "paid": 2},
    }
    txt = bot.format_growth(breakdown)
    assert "kol1: 10 приглашено, 3 оплатили (30%)" in txt
    assert "неизвестно/органика: 5 приглашено, 0 оплатили (0%)" in txt
    assert "ads2: 2 приглашено, 2 оплатили (100%)" in txt
    # сортировка по total, самый крупный источник первым
    assert txt.index("kol1") < txt.index("ads2")
    assert "Рефералы" in txt and "ещё не запущена" in txt

    empty = "📈 Атрибуция\n────────────────────\nЮзеров пока нет."
    assert bot.format_growth({}) == empty


def test_format_by_setup():
    by_setup = {
        "1:1": {"trades": 120, "wins": 66, "winrate": 0.55, "expectancy_r": 0.12, "total_r": 14.4},
        "LTF-BB": {"trades": 3, "wins": 0, "winrate": 0.0, "expectancy_r": -1.0, "total_r": -3.0},
        "6226": {"trades": 25, "wins": 10, "winrate": 0.4, "expectancy_r": -0.05, "total_r": -1.25},
    }
    txt = bot.format_by_setup(by_setup)
    lines = txt.split("\n")
    assert "1:1: 120 сд" in txt
    assert "n<20 — рано даже для направления" in txt      # LTF-BB, n=3
    assert "n≥20 — есть направление" in txt                # 6226, n=25
    idx = next(i for i, ln in enumerate(lines) if "1:1: 120 сд" in ln)
    assert "⚠️" not in lines[idx + 1]                       # у 1:1 (n=120) предупреждения нет

    empty_txt = "📐 По сетапам\n────────────────────\nЗакрытых сделок пока нет."
    assert bot.format_by_setup({}) == empty_txt


def test_fmt_days():
    assert bot._fmt_days(30) == "30 дней"
    assert bot._fmt_days(1) == "1 дней"
    assert bot._fmt_days(5 / 24) == "5 часов"
    assert bot._fmt_days(0.5) == "12 часов"


def test_fmt_age():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    assert bot._fmt_age(None, now) == "нет данных"
    assert bot._fmt_age((now - timedelta(minutes=3)).isoformat(), now) == "3м назад"
    assert bot._fmt_age((now - timedelta(hours=2, minutes=30)).isoformat(), now) == "2.5ч назад"
    assert bot._fmt_age((now - timedelta(days=2)).isoformat(), now) == "2.0д назад"


def test_format_health_fresh_vs_stale():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(minutes=5)).isoformat()
    stale = (now - timedelta(hours=30)).isoformat()

    txt_fresh = bot.format_health(3600.0, fresh, fresh, 102400, "wal")
    assert "✅ Последний тик poller" in txt_fresh
    assert "✅ Последний бэкап" in txt_fresh
    assert "Аптайм процесса: 1.0ч" in txt_fresh
    assert "journal_mode=wal" in txt_fresh

    txt_stale = bot.format_health(3600.0, stale, stale, 102400, "wal")
    assert "⚠️ Последний тик poller" in txt_stale
    assert "⚠️ Последний бэкап" in txt_stale

    txt_missing = bot.format_health(0.0, None, None, 0, "delete")
    assert "⚠️ Последний тик poller: нет данных" in txt_missing
    assert "⚠️ Последний бэкап: нет данных" in txt_missing


def test_format_crash_alert_contains_type_and_truncates():
    try:
        raise ValueError("boom")
    except ValueError as e:
        txt = bot.format_crash_alert(e)
    assert "RSI Range CRASHED" in txt
    assert "ValueError: boom" in txt

    try:
        raise RuntimeError("x" * 10000)                    # огромное сообщение исключения
    except RuntimeError as e:
        txt_long = bot.format_crash_alert(e)
    assert len(txt_long) < 4096                            # Telegram-лимит текста сообщения

    def _raise_deep(n):
        if n == 0:
            raise ValueError("deep")
        _raise_deep(n - 1)

    try:
        _raise_deep(500)                                    # огромный traceback
    except ValueError as e:
        txt_deep = bot.format_crash_alert(e)
    assert len(txt_deep) < 4096


def test_describe_update():
    from types import SimpleNamespace

    upd = SimpleNamespace(message=SimpleNamespace(text="/subscribe"), callback_query=None)
    assert bot._describe_update(upd) == "/subscribe"

    upd_cb = SimpleNamespace(message=None, callback_query=SimpleNamespace(data="pay_stars"))
    assert bot._describe_update(upd_cb) == "callback:pay_stars"

    assert bot._describe_update(None) == "?"

    class _Weird:
        message = None
        callback_query = None
    assert bot._describe_update(_Weird()) == "_Weird"


def test_format_handler_error_alert_not_crashed_wording():
    from types import SimpleNamespace

    upd = SimpleNamespace(message=SimpleNamespace(text="/subscribe"), callback_query=None)
    try:
        raise ValueError("bad sub_days")
    except ValueError as e:
        txt = bot.format_handler_error_alert(e, upd)
    assert "Ошибка в хендлере" in txt
    assert "RSI Range CRASHED" not in txt              # процесс жив — другая формулировка
    assert "/subscribe" in txt
    assert "ValueError: bad sub_days" in txt
    assert len(txt) < 4096


def test_send_raw_telegram_message_posts_to_bot_api(monkeypatch):
    calls = []

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def _fake_urlopen(req, timeout=10):
        calls.append((req.full_url, req.data))
        return _FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    bot._send_raw_telegram_message("TOK", 123, "привет")

    assert len(calls) == 1
    url, data = calls[0]
    assert url == "https://api.telegram.org/botTOK/sendMessage"
    assert b"123" in data
