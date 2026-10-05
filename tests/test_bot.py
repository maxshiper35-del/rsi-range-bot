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
    eng = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0))
    sig = bot.scan_frames("BTC/USDT:USDT", {"m15": _df()}, eng)
    assert sig is not None
    row = fresh_db.query_one("SELECT * FROM signals WHERE symbol=?", ("BTC/USDT:USDT",))
    assert row is not None and row["status"] == "open"
    # записанный сигнал виден в статистике как открытый
    from app.brain import journal
    assert journal.stats()["open"] == 1


def test_scan_frames_no_record(fresh_db):
    eng = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0))
    sig = bot.scan_frames("BTC/USDT:USDT", {"m15": _df()}, eng, record=False)
    assert sig is not None
    assert fresh_db.query_one("SELECT COUNT(*) AS c FROM signals")["c"] == 0


def test_scan_auto_records_open_and_dedups(fresh_db):
    """Авто-режим: сигнал сразу open (в журнале/статистике), повтор — дедуп.
    scan_auto возвращает СПИСОК (по сигналу на входной ТФ); один ТФ -> один сигнал."""
    from app.brain import journal
    eng = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0))
    res = bot.scan_auto("BTC/USDT:USDT", {"m15": _df()}, eng)
    assert len(res) == 1
    sig, sid = res[0]
    assert fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))["status"] == "open"
    assert journal.stats()["open"] == 1                 # сразу в статистике как открытый
    assert bot.scan_auto("BTC/USDT:USDT", {"m15": _df()}, eng) == []  # дедуп -> пусто


def test_scan_semiauto_dedups_same_setup(fresh_db):
    """Тот же импульс на повторном скане не создаёт второго кандидата (и алерта)."""
    eng = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0))
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
    eng = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0))
    df = _df()
    res = bot.scan_auto("BTC/USDT:USDT", {"15m": df, "1h": df, "4h": df}, eng)
    tfs = {sig.timeframe for sig, _ in res}
    assert tfs == {"15m", "1h", "4h"}                   # по сигналу на каждый ТФ
    assert len(res) == 3


def test_scan_semiauto_records_candidate(fresh_db):
    from app.brain import journal
    eng = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0))
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
    eng = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0))
    (_, sid), = bot.scan_semiauto("BTC/USDT:USDT", {"m15": _df()}, eng)

    listing = bot.handle_candidates()
    assert f"#{sid}" in listing                        # кандидат виден в списке

    assert "подтверждён" in bot.handle_confirm(sid)
    assert fresh_db.query_one("SELECT status FROM signals WHERE id=?", (sid,))["status"] == "open"
    assert "не найден" in bot.handle_confirm(sid)      # повторно — уже не кандидат
    assert "Использование" in bot.handle_confirm(None)
    assert bot.handle_candidates() == "Кандидатов нет — жду следующего закрытия свечи."


def test_handle_reject(fresh_db):
    eng = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0))
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
    for term in ("BOS", "CHOCH", "FVG", "killzone", "Свип"):
        assert term.lower() in txt.lower(), term
    # граница публикации стратегии: таксономия сетапов НЕ публикуется никому
    # (мандат владельца; утечка «Setup 1:1»/«LTF-POI» найдена аудитом 02.09)
    for secret in ("1:1", "ltf", "6226"):
        assert secret not in txt.lower(), secret
    assert "6226" not in bot.format_backtest_summary()
    assert "1:1" not in bot.format_backtest_summary()


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

    # граница публикации: сетапов в публичном табло НЕТ (фидбэк владельца 13.08),
    # открытые — символ/сторона/ТФ/возраст, без уровней и uR
    open_rows = [
        {"symbol": "SOL/USDT:USDT", "side": "LONG", "timeframe": "4h",
         "created_at": "2026-08-12T12:00:00+00:00",
         "filled_at": "2026-08-12T16:00:00+00:00"},
        {"symbol": "CYS/USDT:USDT", "side": "SHORT", "timeframe": "1h",
         "created_at": "2026-08-13T11:00:00+00:00", "filled_at": None},
    ]
    txt3 = bot.format_channel_live_board(st, {}, open_rows=open_rows,
                                         now_iso="2026-08-13T20:00:00+00:00")
    assert "сетап" not in txt3.lower() and "6226" not in txt3
    assert "Открыты сейчас (2)" in txt3
    assert "▲ SOL 4h — в позиции · 28ч" in txt3
    assert "▼ CYS 1h — ждёт входа · 9ч" in txt3
    assert "обновлено 20:00 UTC" in txt3


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


def test_channel_cta_markup():
    """CTA под постом канала: deep-link с атрибуцией источника; без username — None
    (пост не должен блокироваться, если get_me не отработал)."""
    kb = bot.channel_cta_markup("rsirange_bot", "chsig")
    btn = kb.inline_keyboard[0][0]
    assert btn.url == "https://t.me/rsirange_bot?start=src_chsig"
    assert "личку" in btn.text
    # у каждой поверхности свой src — конверсия поверхностей сравнима по /sources
    assert bot.channel_cta_markup("rsirange_bot", "chres").inline_keyboard[0][0].url.endswith(
        "src_chres")
    assert bot.channel_cta_markup(None, "chsig") is None


def test_channel_cta_runtime_toggle(fresh_db):
    from app import runtime
    assert runtime.channel_cta_enabled() is True       # дефолт вкл (аудит воронки 12.08)
    runtime.set_value("channel_cta_enabled", "0")
    assert runtime.channel_cta_enabled() is False


def test_healthcheck_ping_deadman_switch(fresh_db, monkeypatch):
    """Dead-man's switch: пульс уходит при свежем поллере, молчит при зависшем.

    Контр-урок простоя 30.07-09.08: внутренние watchdog'и умирают вместе с процессом,
    внешний сервис ловит молчание. Гейт по свежести тика делает пульс сигналом
    «жив ВЕСЬ конвейер», а не «жив maintenance»."""
    from datetime import timedelta

    from app import clock, runtime

    calls: list[str] = []
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda url, timeout=10: calls.append(url))
    url = "https://hc-ping.example/test"

    assert bot.healthcheck_ping(None) is False           # выключен (dev) — нет запроса
    assert calls == []
    runtime.set_last_poller_tick_at(clock.utcnow_iso())  # поллер свежий -> пульс
    assert bot.healthcheck_ping(url) is True
    assert calls == [url]
    stale = (clock.utcnow() - timedelta(hours=3)).isoformat()
    runtime.set_last_poller_tick_at(stale)               # поллер завис -> молчание
    assert bot.healthcheck_ping(url) is False
    assert calls == [url]

    def _boom(u, timeout=10):
        raise OSError("network down")
    runtime.set_last_poller_tick_at(clock.utcnow_iso())
    monkeypatch.setattr("urllib.request.urlopen", _boom)
    assert bot.healthcheck_ping(url) is False            # сбой сети глотается, не роняет


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

    empty_txt = "📐 Статистика по сетапам\n────────────────────\nЗакрытых сделок пока нет."
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
    assert bot.watchlist_gate_decision(0.53, 25) is True         # явное преимущество, n хватает
    assert bot.watchlist_gate_decision(0.10, 20) is True         # ровно на обоих порогах
    assert bot.watchlist_gate_decision(0.09, 22) is False        # XAU — чуть ниже, мимо
    assert bot.watchlist_gate_decision(0.02, 28) is False        # около нуля — уже не «сильный»
    assert bot.watchlist_gate_decision(-0.06, 20) is False       # хуже нейтрали — тем более мимо
    # n поднят 5 -> 20 (25.07.2026): на 5 сделках SE ~0.63R — в 6 раз больше самого
    # порога, решение неотличимо от монетки; так в вотчлист попадала непроверенная экзотика
    assert bot.watchlist_gate_decision(0.53, 17) is False        # раньше проходило
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
    d = bot.dashboard_data(st, [1.0, -1.0], {"BTC": {}}, "now")
    assert "by_setup" not in d                       # сетапы не отдаются никому
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


def test_scan_auto_respects_max_open_per_side(fresh_db):
    """Портфельный лимит направления: при N активных в сторону новый сигнал не создаётся
    (урок 14.07: 15 одновременных SHORT = одна ставка с риском 15R)."""
    from app import runtime
    from app.brain import journal
    runtime.set_value("max_open_per_side", "2")
    eng = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0))
    df = _df()
    assert len(bot.scan_auto("AAA/USDT:USDT", {"1h": df}, eng)) == 1   # SHORT #1
    assert len(bot.scan_auto("BBB/USDT:USDT", {"1h": df}, eng)) == 1   # SHORT #2 (лимит достигнут)
    assert bot.scan_auto("CCC/USDT:USDT", {"1h": df}, eng) == []       # SHORT #3 -> отсечён
    assert journal.open_side_counts()["SHORT"] == 2
    # закрыли одну -> место освободилось
    from app import db
    db.execute("UPDATE signals SET status='stopped', result_r=-1.0 WHERE id=1")
    assert len(bot.scan_auto("CCC/USDT:USDT", {"1h": df}, eng)) == 1
    # 0 = лимит выключен
    runtime.set_value("max_open_per_side", "0")
    assert len(bot.scan_auto("DDD/USDT:USDT", {"1h": df}, eng)) == 1


def test_format_stats_move_and_epoch_blocks():
    st = {"trades": 10, "open": 3, "wins": 6, "losses": 4, "winrate": 0.6,
          "expectancy_r": 0.2, "total_r": 2.0,
          "avg_tp_move_pct": 4.5, "avg_stop_move_pct": -2.1, "total_move_pct": 18.6}
    ep = {"trades": 2, "wins": 1, "losses": 1, "winrate": 0.5,
          "expectancy_r": 0.5, "total_r": 1.0, "open": 4}
    txt = bot.format_stats(st, ep, "14.07")
    assert "тейк в среднем <b>+4.50%</b>" in txt and "стоп <b>-2.10%</b>" in txt
    assert "Чистое движение суммарно: <b>+18.6%</b>" in txt
    assert "🆕 Новый код (сигналы с 14.07): 2 закрыто" in txt and "открыто 4" in txt
    # без эпохи и движений — старый вид, ничего лишнего
    st0 = {**st, "avg_tp_move_pct": 0.0, "avg_stop_move_pct": 0.0, "total_move_pct": 0.0}
    txt0 = bot.format_stats(st0)
    assert "Ход цены" not in txt0 and "Новый код" not in txt0


def test_scan_auto_respects_side_cooldown(fresh_db):
    """Анти-кластерный тормоз: серия стопов стороны за окно -> новые сигналы этой стороны
    не создаются (урок 17.07: 8 последовательных стопов LONG за 6ч — лимит/сторону
    не мешал перезаходу, стопы освобождали слоты)."""
    from app import clock, db, runtime
    from app.brain import journal
    runtime.set_value("side_cooldown_stops", "2")
    runtime.set_value("side_cooldown_hours", "6")
    runtime.set_value("max_open_per_side", "0")       # изолируем тормоз от лимита
    eng = SMCEngine(SMCConfig(use_time_filter=False, min_confidence=0.0))
    df = _df()
    assert len(bot.scan_auto("AAA/USDT:USDT", {"1h": df}, eng)) == 1     # SHORT #1
    assert len(bot.scan_auto("BBB/USDT:USDT", {"1h": df}, eng)) == 1     # SHORT #2
    now = clock.utcnow_iso()
    db.execute("UPDATE signals SET status='stopped', result_r=-1.0, closed_at=?", (now,))
    # 2 стопа SHORT в окне -> сторона на тормозе
    assert bot.scan_auto("CCC/USDT:USDT", {"1h": df}, eng) == []
    assert bot._active_cooldowns() == {"SHORT": 2}
    assert journal.recent_side_stops("SHORT", 6.0) == 2
    # стопы старше окна -> тормоз снят
    db.execute("UPDATE signals SET closed_at='2026-01-01T00:00:00+00:00'")
    assert bot._active_cooldowns() == {}
    assert len(bot.scan_auto("CCC/USDT:USDT", {"1h": df}, eng)) == 1
    # 0 = тормоз выключен даже при свежих стопах
    db.execute("UPDATE signals SET status='stopped', result_r=-1.0, closed_at=?", (now,))
    runtime.set_value("side_cooldown_stops", "0")
    assert bot._active_cooldowns() == {}
    assert len(bot.scan_auto("DDD/USDT:USDT", {"1h": df}, eng)) == 1


def test_format_admin_status_cooldown_line(fresh_db):
    """Строка тормоза в /admin и /status: видна только когда сторона на паузе."""
    from app import runtime
    eff = runtime.effective()
    users = {"total": 1, "paid": 0}
    assert "Тормоз стороны" not in bot.format_admin_status(eff, users, {})
    txt = bot.format_admin_status(eff, users, {"LONG": 2, "SHORT": 1},
                                  cooldowns={"LONG": 4})
    assert "⛔ Тормоз стороны: LONG (4 стопов)" in txt


def test_parse_depo_args():
    assert bot.parse_depo_args("/depo 500 1.5") == (500.0, 1.5)
    assert bot.parse_depo_args("/depo 500") == (500.0, 1.0)      # риск по умолчанию 1%
    assert bot.parse_depo_args("/depo 500,5 2") == (500.5, 2.0)  # запятая как в РФ-локали
    assert bot.parse_depo_args("/depo off") == ("off",)
    assert bot.parse_depo_args("/depo") == ("show",)
    assert bot.parse_depo_args("/depo -5 1") is None             # депозит > 0
    assert bot.parse_depo_args("/depo 500 9") is None            # риск капится 5%
    assert bot.parse_depo_args("/depo abc") is None
    assert bot.parse_depo_args("/depo inf") is None          # isfinite: «плечо ×nan»
    assert bot.parse_depo_args("/depo 500 nan") is None


def test_personal_size_line_in_dm():
    """Строка объёма: количество/нотионал/риск от calculate_position; тизеру не положена
    (проверяется в _broadcast только для kind=full — здесь контракт шаблона)."""
    from types import SimpleNamespace

    from app.delivery import templates
    s = SimpleNamespace(symbol="ETH/USDT:USDT", entry=2000.0, stop=1900.0)
    line = templates.personal_size_line(s, 1000.0, 1.0)
    assert "0.1 ETH" in line and "риск $10" in line and "/depo" in line
    # мелкий альт: количество полной записью, не экспонентой ({:g} давал бы 2.5e+07)
    pepe = SimpleNamespace(symbol="1000PEPE/USDT:USDT", entry=0.0000125, stop=0.0000121)
    small = templates.personal_size_line(pepe, 1000.0, 1.0)
    assert "e" not in small.split("1000PEPE")[0].split("объём:</b>")[1].lower()
    # плечо выше лимита -> предупреждение вместо объёма (не подталкивать к пересиживанию)
    t2 = SimpleNamespace(symbol="ETH/USDT:USDT", entry=2000.0, stop=1999.0)
    warn = templates.personal_size_line(t2, 100.0, 5.0, max_leverage=20)
    assert "⚠️" in warn and ("плечо" in warn.lower())


def test_weekly_digest_missed_block():
    from app.delivery import templates
    wk = {"trades": 5, "wins": 2, "losses": 3, "winrate": 0.4, "total_r": 1.2}
    st = {"open": 3, "trades": 100, "expectancy_r": 0.2, "total_r": 20.0}
    plain = templates.render_weekly_digest("04.08–10.08", wk, st)
    assert "упустил" not in plain and "тизерами" not in plain     # подписчику блока нет
    with_missed = templates.render_weekly_digest("04.08–10.08", wk, st,
                                                 {"n": 3, "total_r": 4.2})
    assert "3" in with_missed and "+4.2R" in with_missed and "/subscribe" in with_missed
    # минусовая неделя не прячется (честность > конверсия)
    neg = templates.render_weekly_digest("04.08–10.08", wk, st, {"n": 2, "total_r": -1.5})
    assert "-1.5R" in neg
    # нет тизерных закрытий -> блока нет
    empty = templates.render_weekly_digest("04.08–10.08", wk, st, {"n": 0, "total_r": 0.0})
    assert "тизерами" not in empty


def test_xray_channel_markup():
    kb = bot.xray_channel_markup("rsirange_bot", 315)
    urls = [b.url for row in kb.inline_keyboard for b in row]
    assert "https://t.me/rsirange_bot?start=xray_315" in urls[0]
    assert urls[1].endswith("src_chres")
    assert bot.xray_channel_markup(None, 315) is None


def test_render_xray_without_scores_survives():
    """Сигналы до 11.08.2026 не имеют суб-скоров — рендер обязан работать и без них."""
    from app.delivery import templates
    x = {"id": 7, "symbol": "ETH/USDT:USDT", "side": "LONG", "timeframe": "4h",
         "status": "expired", "setup": "LTF-BB", "entry": 2000.0, "stop": 1900.0,
         "exit_price": None, "result_r": None, "confidence": 0.61, "stop_pct": 5.0,
         "scores": None, "swept": False, "clean_impulse": False, "reasons": [],
         "wait_hours": None, "hold_hours": None, "trail_moves": 0, "trail_peak_r": None}
    txt = templates.render_xray(x)
    assert "не исполнился" in txt and "до 11.08.2026" in txt


def test_parse_alert_args():
    assert bot.parse_alert_args("/alert BTC 65000") == ("BTC", 65000.0)
    assert bot.parse_alert_args("/alert btc 64999,5") == ("btc", 64999.5)
    assert bot.parse_alert_args("/alert") == ("list",)
    assert bot.parse_alert_args("/alert BTC") is None
    assert bot.parse_alert_args("/alert BTC ноль") is None
    assert bot.parse_alert_args("/alert BTC -5") is None
    assert bot.parse_alert_args("/alert BTC inf") is None    # isfinite-гейт
    assert bot.parse_alert_args("/alert BTC nan") is None


def test_render_result_trailing_exit_header():
    """Прибыльный «стоп» = выход по подтянутому трейлингу — заголовок не должен пугать
    (живой вопрос владельца по #315: «как может быть стоп и +2.60R?»)."""
    from app.delivery import templates
    base = {"id": 315, "symbol": "BEAT/USDT:USDT", "side": "SHORT", "timeframe": "1h",
            "entry": 1.2665, "price_move_pct": 27.38}
    win = templates.render_result({**base, "status": "stopped", "result_r": 2.6})
    assert "трейлинг-выход" in win and "✅" in win and "🛑" not in win
    loss = templates.render_result({**base, "status": "stopped", "result_r": -1.0})
    assert "стоп" in loss and "🛑" in loss and "трейлинг-выход" not in loss


def test_render_xray_hides_setup_tag():
    """Граница публикации: тег сетапа не светится и в рентгене (фидбэк 13.08)."""
    from app.delivery import templates
    x = {"id": 9, "symbol": "BTC/USDT:USDT", "side": "SHORT", "timeframe": "1h",
         "status": "stopped", "setup": "6226", "entry": 64000.0, "stop": 65000.0,
         "exit_price": 62000.0, "result_r": 2.0, "confidence": 0.8, "stop_pct": 1.6,
         "scores": None, "swept": True, "clean_impulse": True, "reasons": [],
         "wait_hours": 1.0, "hold_hours": 5.0, "trail_moves": 1, "trail_peak_r": 2.1}
    txt = templates.render_xray(x)
    assert "6226" not in txt and "сетап" not in txt.lower()


def test_affiliate_block_in_about_and_board(fresh_db):
    """Реф-блок появляется только с заданной ссылкой, с честной пометкой «реферальная»."""
    from app import runtime
    assert "реферальная" not in bot.format_about()
    runtime.set_value("affiliate_url", "https://bingx.com/ref/XYZ")
    about = bot.format_about()
    assert "https://bingx.com/ref/XYZ" in about and "реферальная" in about
    st = {"trades": 2, "open": 1, "winrate": 0.5, "expectancy_r": 0.1, "total_r": 0.2,
          "wins": 1, "losses": 1}
    board = bot.format_channel_live_board(st, {}, None, [], "2026-08-31T00:00:00+00:00")
    assert "bingx.com/ref/XYZ" in board
    runtime.set_value("affiliate_url", "")
    assert "bingx.com" not in bot.format_about()


def test_stars_payload_contract():
    """Контракт payload Stars-платежа: отправка пишет 'sub:{days}', приём парсит
    float(split(':')[1]) — рассинхрон форматов дал бы фолбэк на sub_days для ВСЕХ
    тарифов (квартал/год/lifetime получили бы 30 дней). Закрепляем формат."""
    days = 36500.0
    payload = f"sub:{days}"                     # как в _send_stars_invoice
    assert float(payload.split(":")[1]) == days  # как в _paid
    # дробные дни (тестовый тариф 0.208333) тоже проходят
    assert float(f"sub:{0.208333}".split(":")[1]) == 0.208333


def test_scrub_reasons_hides_construction():
    """Граница публикации: конструкция сетапов (фибо-рецепт, параметры входа от блока)
    вычищается на рендере; generic-SMC формулировки не трогаются (аудит 02.09)."""
    from app.delivery import templates as t
    out = t.scrub_reasons([
        "фибо 1/0.62/0/-0.26 по импульсу",
        "вход 0.5, стоп 1, тейк 0 (RR 1:1)",
        "вход от OB (MT 50%), стоп за дальнюю границу блока",
        "свип лоя (снятие ликвидности)",
        "слом структуры BOS (закреп телом)",
    ])
    joined = " ".join(out)
    assert "0.62" not in joined and "0.26" not in joined and "MT 50%" not in joined
    assert "RR 1:1" not in joined
    assert "свип лоя (снятие ликвидности)" in out
    assert "слом структуры BOS (закреп телом)" in out
    # рендеры канала/DM реально применяют скраб
    from app.core.signal_engine import Side, Signal
    sig = Signal(symbol="BTC/USDT:USDT", side=Side.LONG, timeframe="1h", entry=100.0,
                 stop=98.0, take_profits=[104.0], risk_reward=2.0, confidence=0.7,
                 reasons=["фибо 1/0.62/0/-0.26 по импульсу"],
                 created_at="2026-01-01T00:00:00+00:00", indicators_snapshot={})
    assert "0.62" not in t.render_signal_channel(sig, 1)
    assert "0.62" not in t.render_signal_dm(sig, 1)


def test_result_is_fresh():
    """Посты результатов в канал — только по свежим исходам: запоздалое закрытие
    (пауза/простой/рестарт) учитывается в журнале, но пачкой в канал не уходит."""
    from datetime import datetime, timezone
    now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    def fresh(closed_at):
        return bot.result_is_fresh({"closed_at": closed_at}, now)

    assert fresh("2026-10-04T11:00:00+00:00") is True
    assert fresh("2026-10-04T00:30:00+00:00") is True      # 11.5ч
    assert fresh("2026-10-03T23:00:00+00:00") is False     # 13ч
    assert fresh("2026-09-13T08:00:00+00:00") is False     # 3 недели
    assert fresh("2026-10-04T11:00:00") is True            # naive = UTC
    assert bot.result_is_fresh({}, now) is True            # нет метки — как раньше
    assert fresh("мусор") is True                          # битая метка не молчит


def test_should_remind_pause():
    now = "2026-10-04T12:00:00+00:00"
    assert bot.should_remind_pause(None, None, now) is False                       # не на паузе
    assert bot.should_remind_pause("2026-10-04T08:00:00+00:00", None, now) is False  # 4ч — рано
    assert bot.should_remind_pause("2026-09-13T18:18:31+00:00", None, now) is True   # 3 недели
    # напоминали 2ч назад — не спамим; 25ч назад — пора снова
    assert bot.should_remind_pause("2026-09-13T18:18:31+00:00",
                                   "2026-10-04T10:00:00+00:00", now) is False
    assert bot.should_remind_pause("2026-09-13T18:18:31+00:00",
                                   "2026-10-03T11:00:00+00:00", now) is True


def test_precheckout_ok():
    """Аудит 05.10: pre_checkout пропускал всё — старые инвойсы выключенных/подорожавших
    тарифов оплачивались вечно. Теперь сверка дней и текущей цены в XTR."""
    tiers = [(30.0, 1500), (90.0, 15000), (365.0, 40000), (36500.0, 0), (0.208333, 10)]
    assert bot.precheckout_ok("sub:30.0", 1500, "XTR", tiers) is True
    assert bot.precheckout_ok("sub:30", 1500, "XTR", tiers) is True
    assert bot.precheckout_ok("sub:0.208333", 10, "XTR", tiers) is True
    assert bot.precheckout_ok("sub:30.0", 1000, "XTR", tiers) is False     # старая цена
    assert bot.precheckout_ok("sub:36500", 15000, "XTR", tiers) is False   # тариф выключен
    assert bot.precheckout_ok("sub:7", 1500, "XTR", tiers) is False        # нет такого тира
    assert bot.precheckout_ok("мусор", 1500, "XTR", tiers) is False
    assert bot.precheckout_ok("sub:x", 1500, "XTR", tiers) is False
    assert bot.precheckout_ok("sub:30.0", 1500, "USD", tiers) is False
    assert bot.precheckout_ok("unlock:5:50", 50, "XTR", tiers) is True     # paid media
    assert bot.precheckout_ok(None, 0, "XTR", tiers) is False


def test_pause_age_text():
    from datetime import datetime, timezone
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    assert bot.pause_age_text("2026-10-04T23:00:00+00:00", now) == "13 ч"   # не «0 дн.»
    assert bot.pause_age_text("2026-09-13T18:18:31+00:00", now) == "22 дн."
