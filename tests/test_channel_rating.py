"""Рейтинг сигнальных каналов (channel_rating, миграция 030): хранение агрегатов, буквы,
анонимная выдача, карточка, блок обслуживания. Сеть не нужна."""
from __future__ import annotations

import pandas as pd

from app.delivery import channel_audit as ca
from app.delivery import channel_rating as cr

T0 = pd.Timestamp("2026-10-20 12:00", tz="UTC")


def _agg(filled=12, tp1=7, stop=5, r_total=2.4, r_trades=12, checked=None, no_stop=1,
         posts=40) -> dict:
    checked = checked if checked is not None else filled + 2
    return dict(posts=posts, first=T0 - pd.Timedelta("20D"), last=T0, signals=checked + 1,
                duplicates=0, no_stop=no_stop, unparsed=2, too_old=0, no_bars=0,
                checked=checked, no_fill=checked - filled, filled=filled, tp1=tp1,
                stop_first=stop, open=filled - tp1 - stop, ambiguous=0, no_targets=0,
                r_total=r_total, r_trades=r_trades,
                tp1_avg_r=round(r_total / tp1, 3) if tp1 else None, tfs=["1h"], trades=[])


# --- буквы и счётчик подписчиков ---

def test_letters_like_spreadsheet_columns():
    assert [cr.letter_for(i) for i in (0, 1, 25, 26, 27, 51, 52, 701, 702)] == \
        ["A", "B", "Z", "AA", "AB", "AZ", "BA", "ZZ", "AAA"]


def test_parse_subscribers_formats():
    html = ('<div class="tgme_channel_info_counter"><span class="counter_value">16.1K</span> '
            '<span class="counter_type">subscribers</span></div>')
    assert cr.parse_subscribers(html) == 16_100
    assert cr.parse_subscribers(html.replace("16.1K", "153K")) == 153_000
    assert cr.parse_subscribers(html.replace("16.1K", "864")) == 864
    assert cr.parse_subscribers(html.replace("16.1K", "1.2M")) == 1_200_000
    assert cr.parse_subscribers("<html>no counter</html>") is None
    assert cr.parse_subscribers(None) is None


# --- хранение ---

def test_seed_once_and_admin_status(fresh_db):
    assert cr.seed_channels() == 3
    assert cr.seed_channels() == 0                               # второй раз не сеем
    names = {c["username"] for c in cr.all_channels()}
    assert names == set(cr.SEED_CHANNELS)
    assert cr.set_status("piratesignal", "paused")
    assert not cr.set_status("nobody_here", "paused")
    # кандидат из /audit снятый админом канал не воскрешает, новый — становится candidate
    assert not cr.candidate_from_audit("piratesignal", _agg(checked=30), 42)
    assert cr.candidate_from_audit("@NewOne", _agg(checked=12), 42)
    assert not cr.candidate_from_audit("tiny", _agg(checked=11), 42)       # мало сигналов
    new = next(c for c in cr.all_channels() if c["username"] == "NewOne")
    assert new["status"] == "candidate" and new["added_by"] == "audit:42"
    # админский add активирует кандидата и не дублирует строку
    cr.add_channel("@newone", "admin:1")
    rows = [c for c in cr.all_channels() if c["username"].lower() == "newone"]
    assert len(rows) == 1 and rows[0]["status"] == "active"


def test_due_channels_respects_daily_limit_and_cap(fresh_db):
    cr.seed_channels()
    now = "2026-10-30T12:00:00+00:00"
    due = cr.due_channels(now, limit=2)
    assert [c["username"] for c in due] == list(cr.SEED_CHANNELS[:2])    # не больше лимита
    first = due[0]
    cr.record_run(first["id"], _agg(), "Title", 16_100, run_at=now)
    due2 = cr.due_channels("2026-10-30T13:00:00+00:00", limit=5)
    assert first["username"] not in {c["username"] for c in due2}      # проверен — не пора
    assert first["username"] in {c["username"] for c in
                                 cr.due_channels("2026-10-31T12:30:00+00:00", limit=5)}
    cr.record_error(due2[0]["id"], "t.me timeout")
    ch = next(c for c in cr.all_channels() if c["id"] == due2[0]["id"])
    assert ch["last_error"] == "t.me timeout" and ch["last_run_at"]   # ошибка = попытка


def test_record_run_assigns_stable_letters_and_keeps_history(fresh_db):
    cr.seed_channels()
    chans = {c["username"]: c for c in cr.all_channels()}
    a = cr.record_run(chans["CoinCoachSignals"]["id"], _agg(r_total=-1.1), "Coin", 16_100,
                      run_at="2026-10-30T12:00:00+00:00")
    b = cr.record_run(chans["WallstreetQueenOfficial"]["id"], _agg(r_total=0.5), "WSQ", 153_000,
                      run_at="2026-10-30T12:05:00+00:00")
    assert (a["letter"], b["letter"]) == ("A", "B")                 # по порядку первого обхода
    again = cr.record_run(chans["CoinCoachSignals"]["id"], _agg(r_total=0.9), "Coin", None,
                          run_at="2026-10-31T12:00:00+00:00")
    assert again["letter"] == "A"                                     # буква не меняется
    rows = {r["letter"]: r for r in cr.latest_rows()}
    assert rows["A"]["r_total"] == 0.9 and rows["A"]["prev_r_total"] == -1.1   # динамика
    assert rows["A"]["subscribers"] == 16_100                         # None не затирает
    assert len(fresh_db.query("SELECT 1 FROM rating_runs")) == 3      # история сохранена
    # снятый канал остаётся в таблице (его букву никто не займёт), удалённый — исчезает
    cr.set_status("CoinCoachSignals", "removed")
    assert {r["letter"] for r in cr.latest_rows()} == {"B"}
    c = cr.record_run(chans["piratesignal"]["id"], _agg(r_total=-7.4), None, 8_640)
    assert c["letter"] == "C"


# --- тексты: анонимность и вердикты ---

def test_render_rating_is_anonymous_and_sorted(fresh_db):
    cr.seed_channels()
    chans = {c["username"]: c for c in cr.all_channels()}
    cr.record_run(chans["CoinCoachSignals"]["id"], _agg(r_total=-1.1, filled=20, tp1=15, stop=4,
                                                         r_trades=19), "CoinCoach Signals", 16_100)
    cr.record_run(chans["WallstreetQueenOfficial"]["id"], _agg(r_total=0.5, filled=17, tp1=10,
                                                                stop=7, r_trades=17),
                  "Wallstreet Queen", 153_000)
    cr.record_run(chans["piratesignal"]["id"], _agg(filled=4, tp1=1, stop=3, r_total=-2.3,
                                                     r_trades=4), "Pirate", 8_640)
    rows = cr.latest_rows()
    txt = cr.render_rating(rows)
    for name in ("CoinCoach", "Wallstreet", "Queen", "Pirate", "piratesignal", "@"):
        assert name not in txt.replace("/audit @имя", "")            # имён и @ в выдаче нет
    assert txt.index("Канал B") < txt.index("Канал A") < txt.index("Канал C")   # плюс выше
    assert "~153 тыс. подписчиков" in txt and "~16 тыс. подписчиков" in txt
    assert "в плюсе" in txt and "в минусе" in txt and "мало данных" in txt
    assert "до первой цели 59%" in txt and "/audit @имя" in txt and "Не финансовый совет" in txt
    for word in ("демо", "6226", "LTF", "прибыль гарант"):
        assert word not in txt.lower()
    assert "Первая проверка ещё идёт" in cr.render_rating([])
    admin = cr.render_admin_names(cr.all_channels())
    assert "A · @CoinCoachSignals" in admin and "B · @WallstreetQueenOfficial" in admin


def test_rating_card_html_has_no_names(fresh_db):
    cr.seed_channels()
    ch = cr.all_channels()[0]
    cr.record_run(ch["id"], _agg(r_total=2.4), "Secret Title <b>", 16_100)
    html = cr.rating_card_html(cr.latest_rows(), "неделя 44", bot="rsirange_bot")
    assert "Secret" not in html and ch["username"] not in html
    assert 'class="card"' in html and "неделя 44" in html and "+2.4R" in html
    assert "в плюсе" in html and "@rsirange_bot" in html
    assert cr.week_label("2026-10-30T12:00:00+00:00") == "неделя 44"


def test_verdict_rules():
    assert cr.verdict(dict(small_sample=0, r_trades=10, r_total=0.3)).label == "в плюсе"
    assert cr.verdict(dict(small_sample=0, r_trades=10, r_total=-0.3)).label == "в минусе"
    assert cr.verdict(dict(small_sample=1, r_trades=5, r_total=3.0)).label == "мало данных"
    assert cr.verdict(dict(small_sample=0, r_trades=0, r_total=0.0)).label == "мало данных"


# --- бот: обход в обслуживании и команда ---

def _boot(monkeypatch, admin: int | None = None):
    """main() без запуска (как в test_channel_audit); admin — tg_id админа: cfg.admin_ids
    читается при старте, поэтому окружение выставляется ДО main()."""
    import aiogram

    from tests.test_channel_audit import _boot_bot
    dp, real_run = _boot_bot(monkeypatch)
    if admin is None:
        return dp, real_run
    # _boot_bot ставит TELEGRAM_ADMIN_IDS="" до main(): перезагружаем с админом
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", str(admin))
    from app.config import get_settings
    get_settings.cache_clear()
    import asyncio
    import logging

    from app.delivery import bot
    got = {}

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


def test_rating_cycle_survives_one_broken_channel(fresh_db, monkeypatch):
    """Сбой одного канала (нет превью / биржа) не прерывает обход остальных и пишется в
    канал; успешный обход даёт букву. Пауза между каналами подменена."""
    from app.data import market_data

    dp, real_run = _boot(monkeypatch, admin=42)
    run_cmd = next(h.callback for h in dp.message.handlers
                   if h.callback.__name__ == "_rating_cmd")
    now = pd.Timestamp.now(tz="UTC").floor("15min")
    posted = now - pd.Timedelta("3h")
    good = [ca.Post("CoinCoachSignals", i, posted, "#AAA/USDT LONG\nВход: 100\nСтоп: 90\nTP1: 110")
            for i in range(1, 13)]

    def fake_fetch_channel(name):
        if name == "WallstreetQueenOfficial":
            raise ca.ChannelUnavailable(name)
        return ca.TmePage("Coin", good, None)

    def fake_history(sym, tf, n, exchange_id=None):
        dt = pd.date_range(posted, periods=12, freq="15min")
        return pd.DataFrame([dict(dt=d, open=105, high=111, low=99, close=110) for d in dt])

    class _Ex:
        markets = {"AAA/USDT:USDT": {}}

    monkeypatch.setattr(ca, "fetch_tme_html", lambda name, before=None, timeout=1.0:
                        '<span class="counter_value">16.1K</span>')
    monkeypatch.setattr(ca, "fetch_channel", fake_fetch_channel)
    monkeypatch.setattr(market_data, "fetch_history", fake_history)
    monkeypatch.setattr(market_data, "_exchange", lambda exchange_id: _Ex())
    monkeypatch.setattr(cr, "parse_subscribers", lambda html: 16_100)

    log: list = []

    class _Msg:
        text = "/rating run"
        from_user = type("U", (), {"id": 42})()
        chat = type("C", (), {"id": 42, "type": "private"})()

        async def answer(self, text, **kw):
            log.append(text)

            class _S:
                async def edit_text(self_, t, **k):
                    log.append(t)
            return _S()

    cr.seed_channels()
    monkeypatch.setattr(cr, "PAUSE_MANUAL_S", 0.0)          # без 5-секундной паузы в тесте
    real_run(run_cmd(_Msg()))
    chans = {c["username"]: c for c in cr.all_channels()}
    assert chans["WallstreetQueenOfficial"]["last_error"] == "нет публичного канала"
    assert chans["CoinCoachSignals"]["letter"] == "A"
    assert chans["CoinCoachSignals"]["last_error"] is None
    assert any("WallstreetQueenOfficial: нет публичного канала" in t for t in log)
    rows = cr.latest_rows()
    assert len(rows) == 1 and rows[0]["checked"] == 1       # дубли одного сигнала — одна сделка


def test_migration_030_tables_and_constraints(fresh_db):
    """Таблицы миграции 030: уникальность username без учёта регистра и буквы, CHECK статуса."""
    import sqlite3

    import pytest
    fresh_db.execute("INSERT INTO rating_channels(username, status, added_by, added_at) "
                     "VALUES ('Abc','active','seed','2026-10-30T00:00:00+00:00')")
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.execute("INSERT INTO rating_channels(username, status, added_by, added_at) "
                         "VALUES ('abc','active','seed','2026-10-30T00:00:00+00:00')")
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.execute("INSERT INTO rating_channels(username, status, added_by, added_at) "
                         "VALUES ('xyz','weird','seed','2026-10-30T00:00:00+00:00')")
    fresh_db.execute("UPDATE rating_channels SET letter='A' WHERE username='Abc'")
    fresh_db.execute("INSERT INTO rating_channels(username, status, added_by, added_at) "
                     "VALUES ('xyz','active','seed','2026-10-30T00:00:00+00:00')")
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.execute("UPDATE rating_channels SET letter='A' WHERE username='xyz'")


def test_public_rating_command_and_admin_gate(fresh_db, monkeypatch):
    dp, real_run = _boot(monkeypatch)
    cmd = next(h.callback for h in dp.message.handlers if h.callback.__name__ == "_rating_cmd")
    log: list = []

    class _Msg:
        def __init__(self, text, uid=7):
            self.text = text
            self.from_user = type("U", (), {"id": uid})()
            self.chat = type("C", (), {"id": uid, "type": "private"})()

        async def answer(self, text, **kw):
            log.append((text, kw))

    real_run(cmd(_Msg("/rating")))
    assert "Рейтинг сигнальных каналов" in log[-1][0] and log[-1][1]["parse_mode"] == "HTML"
    real_run(cmd(_Msg("/rating names")))                     # не админ: обычная выдача, без имён
    assert "Рейтинг сигнальных каналов" in log[-1][0] and "@" not in log[-1][0].replace(
        "/audit @имя", "")
    # команда регистрируется только для лички и видна в обслуживании как отдельный блок
    from app.delivery import bot as botmod
    src = open(botmod.__file__, encoding="utf-8").read()
    assert '("рейтинг каналов", _mt_channel_rating)' in src
