"""Тесты обвязки бота: scan_frames (движок -> журнал) и рендер команд."""
from __future__ import annotations

from datetime import datetime, timezone

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
    """Авто-режим: сигнал сразу open (в журнале/статистике), повтор — дедуп.
    scan_auto возвращает СПИСОК (по сигналу на входной ТФ); один ТФ -> один сигнал."""
    from app.brain import journal
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    res = bot.scan_auto("BTC/USDT:USDT", {"m15": _df()}, eng)
    assert len(res) == 1
    sig, sid = res[0]
    assert fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))["status"] == "open"
    assert journal.stats()["open"] == 1                 # сразу в статистике как открытый
    assert bot.scan_auto("BTC/USDT:USDT", {"m15": _df()}, eng) == []  # дедуп -> пусто


def test_scan_semiauto_dedups_same_setup(fresh_db):
    """Тот же импульс на повторном скане не создаёт второго кандидата (и алерта)."""
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    first = bot.scan_semiauto("BTC/USDT:USDT", {"m15": _df()}, eng)
    assert len(first) == 1
    again = bot.scan_semiauto("BTC/USDT:USDT", {"m15": _df()}, eng)
    assert again == []                                  # дедуп по (symbol,tf,side,entry,stop)
    assert fresh_db.query_one("SELECT COUNT(*) AS c FROM signals")["c"] == 1
    # после решения оператора тот же сетап тоже не пере-регистрируется
    from app.brain import journal
    journal.reject_candidate(first[0][1])
    assert bot.scan_semiauto("BTC/USDT:USDT", {"m15": _df()}, eng) == []


def test_scan_multi_timeframe_one_signal_per_tf(fresh_db):
    """15m,1h,4h в одном наборе -> движок даёт сигнал на КАЖДОМ входном ТФ (не один)."""
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    df = _df()
    res = bot.scan_auto("BTC/USDT:USDT", {"15m": df, "1h": df, "4h": df}, eng)
    tfs = {sig.timeframe for sig, _ in res}
    assert tfs == {"15m", "1h", "4h"}                   # по сигналу на каждый ТФ
    assert len(res) == 3


def test_scan_semiauto_records_candidate(fresh_db):
    from app.brain import journal
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    res = bot.scan_semiauto("BTC/USDT:USDT", {"m15": _df()}, eng)
    assert len(res) == 1
    sig, sid = res[0]
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
    (_, sid), = bot.scan_semiauto("BTC/USDT:USDT", {"m15": _df()}, eng)

    listing = bot.handle_candidates()
    assert f"#{sid}" in listing                        # кандидат виден в списке

    assert "подтверждён" in bot.handle_confirm(sid)
    assert fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))["status"] == "open"
    assert "не найден" in bot.handle_confirm(sid)      # повторно — уже не кандидат
    assert "Использование" in bot.handle_confirm(None)
    assert bot.handle_candidates() == "Кандидатов нет — жду следующего закрытия свечи."


def test_handle_reject(fresh_db):
    eng = SMCEngine(SMCConfig(use_time_filter=False))
    (_, sid), = bot.scan_semiauto("BTC/USDT:USDT", {"m15": _df()}, eng)
    assert "отклонён" in bot.handle_reject(sid)
    row = fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))
    assert row["status"] == "rejected"


def test_format_stats_and_calc():
    empty = bot.format_stats({"trades": 0, "open": 0})
    assert "пустая" in empty
    filled = bot.format_stats({"trades": 10, "open": 2, "wins": 6, "winrate": 0.6,
                               "expectancy_r": 0.4, "total_r": 4.0, "losses": 4})
    assert "Winrate: <b>60%</b>" in filled     # /stats теперь HTML
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


def test_fill_is_fresh():
    # шаблоны постов сигнального цикла — в tests/test_templates.py; здесь — гейт свежести
    from datetime import datetime, timezone
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    fresh = {"timeframe": "1h", "filled_at": "2026-01-01T11:00:00+00:00"}   # час назад, 1h ТФ
    assert bot.fill_is_fresh(fresh, now) is True
    stale = {"timeframe": "1h", "filled_at": "2026-01-01T02:00:00+00:00"}   # 10 часов назад
    assert bot.fill_is_fresh(stale, now) is False
    # неизвестный ТФ -> дефолт 1h; битый/отсутствующий filled_at -> не свежий (не постим)
    assert bot.fill_is_fresh({"timeframe": "?", "filled_at": "2026-01-01T11:30:00+00:00"},
                             now) is True
    assert bot.fill_is_fresh({"timeframe": "1h", "filled_at": None}, now) is False
    assert bot.fill_is_fresh({"timeframe": "1h"}, now) is False


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


def test_weekly_report_period_is_previous_full_week():
    # живой инцидент 2026-07-13: гейт сработал в 00:06 понедельника, окно "с начала этой
    # недели до сейчас" дало 6 минут и 0 сделок. Период обязан быть ПРЕДЫДУЩЕЙ неделей.
    just_after_midnight_monday = datetime(2026, 7, 13, 0, 6, tzinfo=timezone.utc)
    start, end = bot.weekly_report_period(just_after_midnight_monday)
    assert start == datetime(2026, 7, 6, tzinfo=timezone.utc)   # прошлый понедельник
    assert end == datetime(2026, 7, 13, tzinfo=timezone.utc)    # текущий понедельник (искл.)
    assert (end - start).days == 7

    # тот же вызов в среду той же недели (например, бот был выключен на выходных) даёт
    # тот же результат — период не "плавает" в течение недели, только по факту смены недели
    wednesday = datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc)
    assert bot.weekly_report_period(wednesday) == (start, end)


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


def test_should_alert_poller_stale():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    slack = 4140.0                                          # ~69 мин (1h youngest tf)
    up_long = slack + 100                                   # процесс поднят достаточно давно
    fresh = (now - timedelta(minutes=10)).isoformat()
    stale = (now - timedelta(hours=3)).isoformat()

    # процесс только поднялся — грейс, молчим даже при устаревшем персистентном тике
    assert bot.should_alert_poller_stale(stale, None, now_iso, slack, slack - 10) is False
    # аптайм есть, тика не было вовсе после грейса -> алерт
    assert bot.should_alert_poller_stale(None, None, now_iso, slack, up_long) is True
    # свежий тик -> не пора
    assert bot.should_alert_poller_stale(fresh, None, now_iso, slack, up_long) is False
    # устарел, не алертили -> алерт
    assert bot.should_alert_poller_stale(stale, None, now_iso, slack, up_long) is True
    # троттлинг: недавно алертили
    recent = (now - timedelta(hours=1)).isoformat()
    assert bot.should_alert_poller_stale(stale, recent, now_iso, slack, up_long) is False
    # троттлинг истёк (>6ч)
    old = (now - timedelta(hours=7)).isoformat()
    assert bot.should_alert_poller_stale(stale, old, now_iso, slack, up_long) is True


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


def test_poller_tick_slack_seconds_scales_with_cycle():
    # найдено при аудите: захардкоженный порог 3700с был размерен под 15m-цикл (900с,
    # запас x4); после перехода на 1h,4h (гейт F) 1h-цикл (3600с) почти не имел запаса —
    # порог обязан расти вместе с youngest ТФ скана, иначе ложные тревоги на обычный ретрай
    assert bot.poller_tick_slack_seconds(["15m", "1h"]) == max(900 * 1.15, 900 + 300)
    assert bot.poller_tick_slack_seconds(["1h", "4h"]) == max(3600 * 1.15, 3600 + 300)
    assert bot.poller_tick_slack_seconds(["4h"]) == 14400 * 1.15
    assert bot.poller_tick_slack_seconds([]) == max(3600 * 1.15, 3600 + 300)  # дефолт 1h
    assert bot.poller_tick_slack_seconds(["unknown_tf"]) == max(3600 * 1.15, 3600 + 300)


def test_format_health_uses_dynamic_slack():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    age_65min = (now - timedelta(minutes=65)).isoformat()
    # старый жёсткий порог (3700с=61.7м) считал бы 65 минут просроченными; новый порог
    # для цикла 1h,4h (~69.5м) — ещё в норме, ложной тревоги быть не должно
    slack = bot.poller_tick_slack_seconds(["1h", "4h"])
    txt = bot.format_health(3600.0, age_65min, age_65min, 1024, "wal", slack)
    assert "✅ Последний тик poller" in txt
    txt_default = bot.format_health(3600.0, age_65min, age_65min, 1024, "wal")
    assert "⚠️ Последний тик poller" in txt_default   # старый дефолт (3700с) — без изменений


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


def test_caption_fits():
    assert bot.caption_fits("<b>короткая подпись</b>") is True
    # теги не считаются: 1000 видимых символов + жирные теги -> влезает
    assert bot.caption_fits("<b>" + "x" * 1000 + "</b>") is True
    assert bot.caption_fits("y" * 1025) is False


def test_build_signal_chart_data():
    import pandas as pd

    from app.core.signal_engine import Side, Signal
    sig = Signal(symbol="BTC/USDT:USDT", side=Side.LONG, timeframe="1h", entry=104250.5,
                 stop=103180.0, take_profits=[106670.0], risk_reward=2.26, confidence=0.82,
                 reasons=["r"], created_at="2026-07-10T00:00:00+00:00",
                 indicators_snapshot={"setup": "1:1"})
    df = pd.DataFrame({"open": range(100), "high": range(1, 101), "low": range(100),
                       "close": range(100), "volume": [1.0] * 100})
    data = bot.build_signal_chart_data(sig, 7, df, "10.07.2026", max_bars=56)
    assert data["signal_id"] == 7 and data["side"] == "LONG"
    assert len(data["candles"]) == 56                   # хвост, не все 100
    assert data["candles"][-1][0] == 99                 # последние бары, не первые
    assert data["tps"] == [106670.0] and data["confidence_pct"] == 82.0
    # пустой df -> явная ошибка (caller ловит и падает на текстовый пост)
    import pytest
    with pytest.raises(ValueError):
        bot.build_signal_chart_data(sig, 7, df.iloc[0:0], "10.07.2026")


def test_main_menu_layout_user():
    user = bot.main_menu_layout(is_admin=False)
    flat = [t for row in user for t in row]
    for label in ("📊 Статистика", "📄 Трек-рекорд", "📈 Дашборд", "🧮 Калькулятор",
                  "💳 Подписка", "🤝 Пригласить", "ℹ️ О боте", "🧪 Бэктест", "📖 Глоссарий",
                  "🎟 Промокод", "❓ Помощь"):
        assert label in flat, label
    assert "🛠 Админ-панель" not in flat                # админ-кнопки не текут юзерам
    assert all(len(row) <= 2 for row in user)           # компактные ряды по 1-2 кнопки


def test_main_menu_layout_admin():
    # админу — операционные отчёты, БЕЗ подписческого контента (он и так получает всё)
    admin = [t for row in bot.main_menu_layout(is_admin=True) for t in row]
    for label in ("📊 Статистика", "📄 Трек-рекорд", "📈 Дашборд", "🛠 Админ-панель",
                  "📟 Статус", "🩺 Health", "👥 Юзеры", "📈 Growth", "🎯 По сетапам",
                  "❓ Помощь"):
        assert label in admin, label
    for label in ("ℹ️ О боте", "🧪 Бэктест", "📖 Глоссарий", "🎟 Промокод",
                  "🧮 Калькулятор", "💳 Подписка"):
        assert label not in admin, label


def test_format_help_user_has_all_user_commands_no_admin_leak():
    txt = bot.format_help(is_admin=False)
    for cmd, _ in bot.USER_COMMANDS:
        if cmd not in ("start", "help"):
            assert f"/{cmd} —" in txt, cmd
    assert "Админ-команды" not in txt
    admin_only = bot.ADMIN_COMMANDS[len(bot.USER_COMMANDS):]
    assert f"/{admin_only[0][0]}" not in txt            # напр. /admin не течёт юзеру


def test_format_help_admin_includes_admin_commands():
    txt = bot.format_help(is_admin=True)
    assert "Админ-команды" in txt
    for cmd, _ in bot.ADMIN_COMMANDS:
        if cmd not in ("start", "help"):                # исключены из тела намеренно
            assert f"/{cmd} —" in txt, cmd


def test_unlock_verdict():
    """Разовая покупка сигнала: продаём только ОТКРЫТЫЕ опубликованные сигналы."""
    open_row = {"status": "open", "result_r": None}
    assert bot.unlock_verdict(open_row, 50) == "ok"
    assert bot.unlock_verdict(open_row, 0) == "disabled"         # цена 0 = фича выключена
    assert bot.unlock_verdict(None, 50) == "not_found"
    assert bot.unlock_verdict({"status": "candidate", "result_r": None}, 50) == "not_found"
    assert bot.unlock_verdict({"status": "rejected", "result_r": None}, 50) == "not_found"
    # закрытые не продаём — исход уже бесплатно в канале (честность)
    for st, r in (("tp1", 2.26), ("stopped", -1.0), ("expired", None)):
        assert bot.unlock_verdict({"status": st, "result_r": r}, 50) == "closed"
    for verdict in ("not_found", "closed", "disabled"):
        assert verdict in bot._UNLOCK_DENIALS


def test_milestone_reached():
    """Вехи: одна (старшая) за раз, только что пересечённая."""
    assert bot.milestone_reached(0, 5) is None
    assert bot.milestone_reached(0, 10) == 10
    assert bot.milestone_reached(9, 12) == 10
    assert bot.milestone_reached(10, 12) is None                 # уже опубликована
    assert bot.milestone_reached(20, 60) == 50                   # скачок через 25 -> старшая
    assert bot.milestone_reached(999, 1000) == 1000
    assert bot.milestone_reached(1000, 5000) is None             # выше вех нет


def test_watchlist_gate_decision():
    """Гейт кандидата: порог ужесточён 2026-07-11 до +0.10R («меньше, но сильнее»),
    было −0.05R «не хуже нейтрали» — владелец предпочёл качество скорости набора статистики."""
    assert bot.watchlist_gate_decision(0.53, 17) is True         # ARB — явное преимущество
    assert bot.watchlist_gate_decision(0.10, 12) is True         # ровно на пороге
    assert bot.watchlist_gate_decision(0.09, 22) is False        # XAU — чуть ниже, мимо
    assert bot.watchlist_gate_decision(0.02, 28) is False        # около нуля — уже не «сильный»
    assert bot.watchlist_gate_decision(-0.06, 20) is False       # хуже нейтрали — тем более мимо
    assert bot.watchlist_gate_decision(0.50, 4) is False         # мало сделок
    assert bot.watchlist_gate_decision(0.0, 0) is False


def test_plan_watchlist_rotation():
    cur = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
    top = ["BTC/USDT:USDT", "XRP/USDT:USDT", "ETH/USDT:USDT", "DOGE/USDT:USDT"]
    gate = {"XRP/USDT:USDT": True, "DOGE/USDT:USDT": False}
    new, added, removed = bot.plan_watchlist_rotation(cur, top, gate)
    assert new == ["BTC/USDT:USDT", "XRP/USDT:USDT", "ETH/USDT:USDT"]  # порядок топа
    assert added == ["XRP/USDT:USDT"]                # DOGE не прошёл гейт
    assert removed == ["SOL/USDT:USDT"]              # выпал из топа по ликвидности
    # защита: пустой итог не оставляет бот без рынка
    new2, a2, r2 = bot.plan_watchlist_rotation(cur, [], {})
    assert new2 == cur and a2 == [] and r2 == []


def test_verifier_html_self_contained():
    """verifier.html: существует, WebCrypto на месте, НИКАКИХ внешних запросов
    (self-contained — иначе «офлайн-проверка» была бы враньём)."""
    html = bot.load_verifier_html().decode("utf-8")
    assert "crypto.subtle.digest" in html and "SHA-256" in html
    for marker in ("http://", "https://", "src=", "@import", "fetch(", "XMLHttpRequest"):
        assert marker not in html, f"внешняя зависимость: {marker}"
    assert "Не финансовый совет" in html          # честный дисклеймер и здесь


def test_bot_description_within_telegram_limit():
    """Описание профиля: лимит Telegram 512 символов, честные формулировки на месте."""
    assert len(bot.BOT_DESCRIPTION) <= 512, len(bot.BOT_DESCRIPTION)
    assert "100% исходов" in bot.BOT_DESCRIPTION
    assert "Не финансовый совет" in bot.BOT_DESCRIPTION
    assert "не обещание прибыли" in bot.BOT_DESCRIPTION


def test_next_milestone_and_dashboard_data():
    assert bot.next_milestone(0) == 10
    assert bot.next_milestone(24) == 25
    assert bot.next_milestone(1000) is None
    st = {"trades": 24, "open": 3, "winrate": 0.5, "expectancy_r": 0.1, "total_r": 2.4,
          "wins": 12, "losses": 12}
    d = bot.dashboard_data(st, [1.0, -1.0], {"BTC": {}}, {"1:1": {}}, "now")
    assert d["milestone"] == {"current": 24, "next": 25}
    assert d["stats"] is st and d["equity"] == [1.0, -1.0]


def test_parse_vipchannel_args():
    """Формат: /vipchannel <@канал|id> <цена 1-10000>."""
    assert bot.parse_vipchannel_args("/vipchannel @rsi_vip 300") == ("@rsi_vip", 300)
    assert bot.parse_vipchannel_args("/vipchannel -1001234567890 50") == \
        ("-1001234567890", 50)
    assert bot.parse_vipchannel_args("/vipchannel @rsi_vip 1") == ("@rsi_vip", 1)
    assert bot.parse_vipchannel_args("/vipchannel @rsi_vip 10000") == ("@rsi_vip", 10000)
    assert bot.parse_vipchannel_args("/vipchannel @rsi_vip 0") is None       # ниже диапазона
    assert bot.parse_vipchannel_args("/vipchannel @rsi_vip 10001") is None   # выше диапазона
    assert bot.parse_vipchannel_args("/vipchannel @rsi_vip") is None        # нет цены
    assert bot.parse_vipchannel_args("/vipchannel") is None
    assert bot.parse_vipchannel_args("/vipchannel @rsi_vip abc") is None    # не число
    assert bot.parse_vipchannel_args("") is None


def test_format_terms_covers_key_sections():
    txt = bot.format_terms()
    assert "Условия использования" in txt
    assert "не инвестиционный совет" in txt.lower()
    assert "Money-back 7 дней" in txt
    assert "18+" in txt
    assert "никогда не пишем первыми в личку" in txt
    assert "храним минимум" in txt.lower()
    assert "не юридическая" in txt.lower()          # честная оговорка про черновик


def test_lifecycle_chart_wanted():
    # карта на посте цикла сделки — только когда флаг ВКЛ И канал в full (teaser прячет уровни)
    assert bot.lifecycle_chart_wanted(True, "full") is True
    assert bot.lifecycle_chart_wanted(True, "teaser") is False   # teaser не раскрывает уровни
    assert bot.lifecycle_chart_wanted(False, "full") is False    # флаг выключен
    assert bot.lifecycle_chart_wanted(False, "teaser") is False
