"""Игра «Лонг или шорт?» (app/delivery/game.py): правила очков и серий, ничья, недельный
победитель и приз, поздний старт и позднее подведение итога, один голос, выход из игры,
гейты рассылки (мьют/тишина/блок) и сама рассылка фейковым ботом — без Telegram и биржи."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

H = 3_600_000
DAY0 = datetime(2026, 10, 26, tzinfo=timezone.utc)      # понедельник, ISO-неделя 2026-W44
LOG = logging.getLogger("test-game")


def at(day: int, h: int, m: int = 0, s: int = 0) -> datetime:
    return DAY0 + timedelta(days=day, hours=h, minutes=m, seconds=s)


def bar(day: int, h: int) -> int:
    """open-time (мс) часового бара, открывшегося в day/h."""
    return int(at(day, h).timestamp() * 1000)


class _Ex:
    """Фейковая биржа: close часового бара по его open-time; вызов отдаёт то же, что
    fetch_closed_ohlcv — limit-1 последних ЗАКРЫТЫХ к self.now баров (формирующийся отброшен).
    lag_bars — биржа «не дописала» хвост; missing — дыра в ряду."""

    def __init__(self, closes: dict | None = None, base: float = 100.0):
        self.now = DAY0
        self.closes = dict(closes or {})
        self.base = base
        self.calls: list[int] = []
        self.lag_bars = 0
        self.missing: set[int] = set()

    def close(self, ts: int) -> float:
        return float(self.closes.get(ts, self.base))

    def __call__(self, symbol: str, tf: str, limit: int) -> pd.DataFrame:
        assert symbol == "BTC/USDT:USDT" and tf == "1h"
        self.calls.append(limit)
        now_ms = int(self.now.timestamp() * 1000)
        last = (now_ms // H) * H - H - self.lag_bars * H
        rows = []
        for i in range(limit - 1):
            ts = last - (limit - 2 - i) * H
            if ts in self.missing:
                continue
            c, o = self.close(ts), self.close(ts - H)
            rows.append((ts, o, max(o, c) + 1.0, min(o, c) - 1.0, c, 1.0))
        return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])


def _tick(ex: _Ex, now: datetime):
    """Такт игры без Telegram: итоги созревших раундов, затем старт раунда дня."""
    from app.delivery import game

    ex.now = now
    resolved = game.resolve_due(now, ex)
    started = game.ensure_round(now, ex)
    return resolved, started


def _players(*uids, now=None):
    from app.delivery import game, payments

    for uid in uids:
        payments.register_user(uid, f"user{uid}")
        game.join(uid, now or at(0, 9))


# ---------------------------------------------------------------------------
# чистые правила
# ---------------------------------------------------------------------------
def test_classify_flat_threshold_inclusive():
    from app.delivery import game

    assert game.classify(100.0, 100.05) == game.FLAT        # ровно +0.05% — ничья
    assert game.classify(100.0, 99.95) == game.FLAT
    assert game.classify(100.0, 100.0) == game.FLAT
    assert game.classify(100.0, 100.06) == game.UP
    assert game.classify(100.0, 99.94) == game.DOWN


def test_vote_points_bonus_every_third_in_a_row():
    from app.delivery import game

    assert [game.vote_points(s) for s in range(1, 10)] == [1, 1, 2, 1, 1, 2, 1, 1, 2]


def test_mask_name_and_points_word():
    from app.delivery import game

    assert game.mask_name("maxshiper35", 1) == "max***35"
    assert game.mask_name("@vasya", 1) == "va***a"
    assert game.mask_name("abc", 1) == "a***"
    assert game.mask_name(None, 934414349) == "Игрок …4349"
    assert [game.points_word(n) for n in (0, 1, 2, 5, 11, 21, 22)] == \
        ["очков", "очко", "очка", "очков", "очков", "очко", "очка"]


def test_week_key_and_days():
    from app.delivery import game

    assert game.week_key(at(0, 12)) == "2026-W44"
    assert game.week_key(at(6, 23)) == "2026-W44"             # воскресенье — та же неделя
    assert game.week_key(at(7, 0)) == "2026-W45"
    mon, sun = game.week_days("2026-W44")
    assert (mon.isoformat(), sun.isoformat()) == ("2026-10-26", "2026-11-01")
    assert game.week_label("2026-W44") == "26.10–01.11"


def test_invite_and_vote_buttons_round_trip_through_parser():
    from app.delivery import game

    assert "/game" in game.INVITE_TEXT
    join_btn = game.invite_markup().inline_keyboard[0][0]
    assert game.parse_callback(join_btn.callback_data) == ("join", None, None)
    up, down = game.vote_markup(7).inline_keyboard[0]
    assert game.parse_callback(up.callback_data) == ("vote", 7, "up")
    assert game.parse_callback(down.callback_data) == ("vote", 7, "down")
    assert game.parse_callback(game.voted_markup("up").inline_keyboard[0][0].callback_data) \
        == ("noop", None, None)


def test_parse_callback_rejects_garbage():
    from app.delivery import game

    assert game.parse_callback("game:v:12:up") == ("vote", 12, "up")
    assert game.parse_callback("game:v:12:down") == ("vote", 12, "down")
    assert game.parse_callback("game:join") == ("join", None, None)
    assert game.parse_callback("game:noop") == ("noop", None, None)
    for bad in ("game:v:12:sideways", "game:v:x:up", "game:v:12", "game:drop", None, ""):
        assert game.parse_callback(bad) == ("", None, None)


# ---------------------------------------------------------------------------
# старт раунда
# ---------------------------------------------------------------------------
def test_round_starts_once_per_day_from_noon(fresh_db):
    from app.delivery import game

    ex = _Ex({bar(0, 11): 68123.4})
    _, r = _tick(ex, at(0, 11, 59))
    assert r is None and ex.calls == []                      # до 12:00 — биржу не трогаем
    _, r = _tick(ex, at(0, 12, 0, 30))
    assert r is not None
    assert r["day"] == "2026-10-26"
    assert r["ref_bar_ts"] == bar(0, 11)                     # последний ЗАКРЫТЫЙ к 12:00:30
    assert r["ref_price"] == pytest.approx(68123.4)
    assert r["target_bar_ts"] == bar(1, 11)                  # ровно +24 ч
    assert datetime.fromisoformat(r["closes_at"]) == at(0, 18, 0, 30)
    import json
    assert len(json.loads(r["chart_json"])) == game.CHART_BARS
    calls = len(ex.calls)
    _, again = _tick(ex, at(0, 12, 1))
    assert again is None and len(ex.calls) == calls          # раунд дня уже есть — без фетча
    assert fresh_db.query_one("SELECT COUNT(*) AS n FROM game_rounds")["n"] == 1


def test_round_starts_late_when_mac_wakes_but_not_at_night(fresh_db):
    ex = _Ex({bar(0, 14): 70000.0})
    _, r = _tick(ex, at(0, 15, 20))                          # Mac спал в 12:00, проснулся
    assert r is not None
    assert r["ref_bar_ts"] == bar(0, 14) and r["ref_price"] == 70000.0
    assert r["target_bar_ts"] == bar(1, 14)
    assert datetime.fromisoformat(r["closes_at"]) == at(0, 21, 20)
    # следующие сутки: проснулся в 19:00 — пуш ушёл бы аудитории в ночь, раунда нет
    _, r2 = _tick(ex, at(1, 19, 0))
    assert r2 is None
    assert fresh_db.query_one("SELECT COUNT(*) AS n FROM game_rounds")["n"] == 1


def test_round_waits_for_just_closed_bar(fresh_db):
    """Первые секунды часа биржа может ещё не отдать только что закрытый бар: «сейчас» на
    час старше было бы враньём — раунд ждёт следующего такта; протухший ряд — тоже."""
    ex = _Ex({bar(0, 11): 100.0})
    ex.lag_bars = 1
    _, r = _tick(ex, at(0, 12, 0, 5))
    assert r is None
    ex.lag_bars = 3
    _, r = _tick(ex, at(0, 12, 1, 5))
    assert r is None
    ex.lag_bars = 0
    _, r = _tick(ex, at(0, 12, 2, 5))
    assert r is not None and r["ref_bar_ts"] == bar(0, 11)


def test_noon_tick_resolves_yesterday_before_starting_today(fresh_db):
    """В 12:00 бар итога вчерашнего раунда — он же «сейчас» сегодняшнего: оба в одном такте."""
    from app.delivery import game

    ex = _Ex({bar(0, 11): 100, bar(1, 11): 101})
    _tick(ex, at(0, 12, 0, 30))
    resolved, started = _tick(ex, at(1, 12, 0, 30))
    assert [s["outcome"] for s in resolved] == ["up"]
    assert started is not None and started["ref_price"] == 101
    assert game.get_round(1)["result_price"] == started["ref_price"]


# ---------------------------------------------------------------------------
# голос
# ---------------------------------------------------------------------------
def test_one_vote_only_cannot_change_and_window_closes(fresh_db):
    from app.delivery import game

    ex = _Ex()
    _, r = _tick(ex, at(0, 12, 0, 30))
    rid = r["id"]
    assert game.cast_vote(rid, 7, "up", at(0, 12, 5)) == "ok"
    assert game.cast_vote(rid, 7, "down", at(0, 12, 6)) == "already"
    assert game.cast_vote(rid, 7, "up", at(0, 12, 7)) == "already"
    assert game.get_vote(rid, 7)["choice"] == "up"           # переголосовать нельзя
    assert game.cast_vote(rid, 8, "down", at(0, 18, 0, 30)) == "closed"   # ровно конец окна
    assert game.cast_vote(rid, 8, "sideways", at(0, 13)) == "bad_choice"
    assert game.cast_vote(rid + 99, 8, "up", at(0, 13)) == "no_round"
    assert fresh_db.query_one("SELECT COUNT(*) AS n FROM game_votes")["n"] == 1


def test_vote_without_joining_is_counted_but_not_subscribed(fresh_db):
    """Пересланный раунд: голос засчитан, но ежедневных раундов голосовавший не получает,
    пока сам не вступит (opt-in)."""
    from app.delivery import game, payments

    payments.register_user(5, "five")
    ex = _Ex()
    _, r = _tick(ex, at(0, 12, 0, 30))
    assert game.cast_vote(r["id"], 5, "up", at(0, 12, 10)) == "ok"
    assert game.is_player(5) is False
    _, r2 = _tick(ex, at(1, 12, 0, 30))
    assert game.round_recipients(r2["id"], at(1, 12, 1)) == []
    assert game.join(5, at(1, 12, 2)) == "new"               # вступление, а не «возврат»


# ---------------------------------------------------------------------------
# итог: очки, серии, ничья, пропуск
# ---------------------------------------------------------------------------
def _votes_of(db, uid):
    return [dict(r) for r in db.query(
        "SELECT points, correct, streak_after FROM game_votes WHERE tg_id=? ORDER BY round_id",
        (uid,))]


def test_scoring_streak_bonus_and_reset_on_miss(fresh_db):
    from app.delivery import game

    ex = _Ex({bar(0, 11): 100, bar(1, 11): 101, bar(2, 11): 102, bar(3, 11): 103,
              bar(4, 11): 102})                              # вверх, вверх, вверх, вниз
    for d in range(4):
        _tick(ex, at(d, 12, 0, 30))
        r = game.open_round(at(d, 12, 1))
        assert game.cast_vote(r["id"], 1, "up", at(d, 12, 5)) == "ok"
        assert game.cast_vote(r["id"], 2, "down", at(d, 12, 6)) == "ok"
    resolved, _ = _tick(ex, at(4, 12, 0, 30))
    assert [s["outcome"] for s in resolved] == ["down"]
    up = _votes_of(fresh_db, 1)
    assert [v["points"] for v in up] == [1, 1, 2, 0]         # 3-й подряд — +1 бонус
    assert [v["streak_after"] for v in up] == [1, 2, 3, 0]
    assert game.player(1)["streak"] == 0 and game.player(1)["best_streak"] == 3
    down = _votes_of(fresh_db, 2)
    assert [v["points"] for v in down] == [0, 0, 0, 1]
    assert game.player(2)["streak"] == 1


def test_flat_round_gives_no_points_and_keeps_streak(fresh_db):
    from app.delivery import game

    ex = _Ex({bar(0, 11): 100, bar(1, 11): 101, bar(2, 11): 101.04})   # вверх, затем ±0.04%
    for d in range(2):
        _tick(ex, at(d, 12, 0, 30))
        r = game.open_round(at(d, 12, 1))
        game.cast_vote(r["id"], 1, "up", at(d, 12, 5))
    resolved, _ = _tick(ex, at(2, 12, 0, 30))
    assert resolved[0]["outcome"] == "flat"
    last = _votes_of(fresh_db, 1)[-1]
    assert last["points"] == 0 and last["correct"] is None and last["streak_after"] == 1
    assert game.player(1)["streak"] == 1                     # ничья серию не трогает


def test_delivered_unanswered_round_breaks_streak_undelivered_does_not(fresh_db):
    from app.delivery import game

    _players(1, 2)
    ex = _Ex({bar(0, 11): 100, bar(1, 11): 101, bar(2, 11): 102})
    _tick(ex, at(0, 12, 0, 30))
    r0 = game.open_round(at(0, 12, 1))
    for uid in (1, 2):
        game.cast_vote(r0["id"], uid, "up", at(0, 12, 5))
    _tick(ex, at(1, 12, 0, 30))                              # итог раунда 0: оба угадали
    assert game.player(1)["streak"] == 1 and game.player(2)["streak"] == 1
    r1 = game.open_round(at(1, 12, 1))
    # юзеру 1 раунд доставлен, он не ответил; юзеру 2 — не доставлен (мьют/тишина)
    assert game.claim_send("round", str(r1["id"]), 1, at(1, 12, 1))
    game.mark_sent("round", str(r1["id"]), 1, at(1, 12, 1))
    # заявка без sent_at (краш между заявкой и отправкой) — не «доставлено»
    assert game.claim_send("round", str(r1["id"]), 2, at(1, 12, 1))
    resolved, _ = _tick(ex, at(2, 12, 0, 30))
    assert resolved[0]["missed"] == 1
    assert game.player(1)["streak"] == 0                     # пришёл — не ответил
    assert game.player(2)["streak"] == 1                     # не видел — серия цела


def test_resolve_round_is_idempotent(fresh_db):
    from app.delivery import game

    ex = _Ex({bar(0, 11): 100, bar(1, 11): 101})
    _tick(ex, at(0, 12, 0, 30))
    r = game.open_round(at(0, 12, 1))
    game.cast_vote(r["id"], 1, "up", at(0, 12, 5))
    assert game.resolve_round(r["id"], 101.0, at(1, 12, 1)) is not None
    assert game.resolve_round(r["id"], 90.0, at(1, 12, 2)) is None       # второй раз — нет
    assert game.get_round(r["id"])["outcome"] == "up"
    assert _votes_of(fresh_db, 1)[0]["points"] == 1 and game.player(1)["streak"] == 1


# ---------------------------------------------------------------------------
# простой Mac: поздний итог тем же баром
# ---------------------------------------------------------------------------
def test_late_resolution_uses_bar_at_t_plus_24h_not_now(fresh_db):
    from app.delivery import game

    # через 24 ч цена ВЫШЕ (101), а к пробуждению Mac через трое суток — уже НИЖЕ (95)
    ex = _Ex({bar(0, 11): 100, bar(1, 11): 101}, base=95.0)
    _tick(ex, at(0, 12, 0, 30))
    r = game.open_round(at(0, 12, 1))
    game.cast_vote(r["id"], 1, "up", at(0, 12, 5))
    game.cast_vote(r["id"], 2, "down", at(0, 12, 6))
    assert game.due_rounds(at(1, 11, 59)) == []              # бар итога ещё не закрыт
    ex.now = at(3, 16, 45)                                   # Mac проснулся через двое суток
    resolved = game.resolve_due(at(3, 16, 45), ex)
    assert [s["outcome"] for s in resolved] == ["up"]        # сейчас 95 < 100, но судит бар +24ч
    assert game.get_round(r["id"])["result_price"] == 101.0
    assert ex.calls[-1] == 53 + 3                            # глубина фетча — до бара итога
    assert _votes_of(fresh_db, 1)[0]["points"] == 1
    assert _votes_of(fresh_db, 2)[0]["points"] == 0


def test_resolution_waits_for_missing_bar_then_voids_after_48h(fresh_db):
    from app.delivery import game

    ex = _Ex({bar(0, 11): 100, bar(1, 11): 101})
    _tick(ex, at(0, 12, 0, 30))
    r = game.open_round(at(0, 12, 1))
    game.cast_vote(r["id"], 1, "up", at(0, 12, 5))
    ex.missing = {bar(1, 11)}                                # биржа не отдала бар итога
    ex.now = at(1, 13)
    assert game.resolve_due(at(1, 13), ex) == []             # ждём, не судим другим баром
    assert game.get_round(r["id"])["outcome"] is None
    ex.now = at(3, 12, 30)
    resolved = game.resolve_due(at(3, 12, 30), ex)           # >48 ч без бара — аннулировать
    assert [s["outcome"] for s in resolved] == ["void"]
    v = _votes_of(fresh_db, 1)[0]
    assert v["points"] == 0 and v["correct"] is None
    assert game.player(1)["streak"] == 0                     # серия не тронута (была 0)


def test_resolution_keeps_day_order_when_older_bar_missing(fresh_db):
    """Серии зависят от порядка раундов: младший раунд не решается раньше старшего."""
    from app.delivery import game

    ex = _Ex({bar(0, 11): 100, bar(1, 11): 101, bar(2, 11): 102})
    _tick(ex, at(0, 12, 0, 30))
    ex.missing = {bar(1, 11)}                                # дыра ровно в баре итога раунда 0
    _, r1 = _tick(ex, at(1, 13, 0, 30))                      # раунд 0 ждёт; раунд 1 стартовал
    assert r1 is not None and r1["target_bar_ts"] == bar(2, 12)
    ex.now = at(2, 13, 0, 30)
    resolved = game.resolve_due(at(2, 13, 0, 30), ex)        # бар раунда 1 есть, раунда 0 — нет
    assert resolved == []
    assert fresh_db.query_one(
        "SELECT COUNT(*) AS n FROM game_rounds WHERE outcome IS NULL")["n"] == 2


def test_round_beyond_exchange_window_is_voided(fresh_db):
    from app.delivery import game

    ex = _Ex()
    _tick(ex, at(0, 12, 0, 30))
    far = at(60, 12)                                         # ~2 месяца простоя: >1000 баров
    ex.now = far
    resolved = game.resolve_due(far, ex)
    assert [s["outcome"] for s in resolved] == ["void"]
    assert max(ex.calls) <= game.MAX_FETCH_BARS


# ---------------------------------------------------------------------------
# неделя: топ, ничьи, приз
# ---------------------------------------------------------------------------
def _ins_round(db, day: str, created: str, outcome: str | None = "up") -> int:
    return db.execute(
        "INSERT INTO game_rounds(day, symbol, ref_bar_ts, ref_price, target_bar_ts, chart_json, "
        "created_at, closes_at, outcome, result_price, resolved_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (day, "BTC/USDT:USDT", 0, 100.0, 0, "[]", created, created, outcome,
         101.0 if outcome else None, created if outcome else None))


def _ins_vote(db, rid: int, uid: int, voted_at: str, correct, points: int) -> None:
    db.execute("INSERT INTO game_votes(round_id, tg_id, choice, voted_at, correct, points, "
               "streak_after) VALUES(?,?,?,?,?,?,0)", (rid, uid, "up", voted_at, correct, points))


def _week_fixture(db):
    """W44: A (10) и B (20) — по 3 очка, B угадывал в среднем раньше; C (30) — 2; админ
    (99) — 5, вне зачёта."""
    from app.delivery import payments

    for uid in (10, 20, 30, 99):
        payments.register_user(uid, f"player_{uid}")
    rounds = [_ins_round(db, f"2026-10-{d}", f"2026-10-{d}T12:00:00+00:00") for d in (27, 28, 29)]
    for rid, d in zip(rounds, (27, 28, 29)):
        _ins_vote(db, rid, 10, f"2026-10-{d}T14:00:00+00:00", 1, 1)       # +2 ч
        _ins_vote(db, rid, 20, f"2026-10-{d}T13:00:00+00:00", 1, 1)       # +1 ч
        _ins_vote(db, rid, 99, f"2026-10-{d}T12:01:00+00:00", 1, 2)
    _ins_vote(db, rounds[0], 30, "2026-10-27T12:30:00+00:00", 1, 1)
    _ins_vote(db, rounds[1], 30, "2026-10-28T12:30:00+00:00", 1, 1)
    _ins_vote(db, rounds[2], 30, "2026-10-29T12:30:00+00:00", 0, 0)
    return rounds


def test_leaderboard_points_then_faster_correct_answers_admin_excluded(fresh_db):
    from app.delivery import game

    _week_fixture(fresh_db)
    board = game.leaderboard("2026-W44", exclude=(99,))
    assert [(a["tg_id"], a["points"], a["place"]) for a in board] == \
        [(20, 3, 1), (10, 3, 2), (30, 2, 3)]
    assert board[0]["avg_answer_sec"] == pytest.approx(3600.0)
    assert board[1]["avg_answer_sec"] == pytest.approx(7200.0)
    assert 99 in {a["tg_id"] for a in game.leaderboard("2026-W44")}     # без exclude — есть


def test_leaderboard_tie_falls_back_to_first_vote_then_id(fresh_db):
    from app.delivery import game

    early = _ins_round(fresh_db, "2026-10-26", "2026-10-26T12:00:00+00:00", outcome="down")
    rid = _ins_round(fresh_db, "2026-10-27", "2026-10-27T12:00:00+00:00")
    for uid in (1, 2, 3, 4):                                 # всем по очку, ответ через 1 ч
        _ins_vote(fresh_db, rid, uid, "2026-10-27T13:00:00+00:00", 1, 1)
    _ins_vote(fresh_db, early, 4, "2026-10-26T12:10:00+00:00", 0, 0)   # 4 играл раньше всех
    board = game.leaderboard("2026-W44")
    # очки и скорость верных равны -> раньше первый голос недели -> дальше tg_id
    assert [a["tg_id"] for a in board] == [4, 1, 2, 3]


def test_award_week_grants_prize_once_not_a_payment(fresh_db):
    from app.delivery import game, payments

    _week_fixture(fresh_db)
    now = at(7, 13)                                          # понедельник следующей недели
    assert game.weeks_to_award(now) == ["2026-W44"]
    assert game.weeks_to_award(at(6, 13)) == []              # неделя ещё идёт
    res = game.award_due_weeks(now, prize_days=3, exclude=(99,))
    assert len(res) == 1 and res[0]["winner"]["tg_id"] == 20 and res[0]["prize_days"] == 3
    u = payments.get_user(20)
    until = datetime.fromisoformat(u["paid_until"])
    left = until - datetime.now(timezone.utc)
    assert timedelta(days=2, hours=23) < left < timedelta(days=3, hours=1)
    assert u["tier"] == "paid"
    assert fresh_db.query_one("SELECT COUNT(*) AS n FROM payments")["n"] == 0   # не платёж
    # повтор (рестарт, гонка тактов) — ни второй строки недели, ни вторых дней
    assert game.award_due_weeks(now, prize_days=3, exclude=(99,)) == []
    assert game.award_week("2026-W44", 3, (99,), now) is None
    assert payments.get_user(20)["paid_until"] == u["paid_until"]
    assert payments.get_user(99)["paid_until"] is None       # админ вне зачёта


def test_award_week_prize_disabled_and_no_scorers(fresh_db):
    from app.delivery import game, payments

    _week_fixture(fresh_db)
    res = game.award_week("2026-W44", 0, (99,), at(7, 13))
    assert res["winner"]["tg_id"] == 20 and res["prize_days"] == 0
    assert payments.get_user(20)["paid_until"] is None       # приз выключен — дней нет
    # неделя, где никто не угадал: победителя нет
    rid = _ins_round(fresh_db, "2026-11-03", "2026-11-03T12:00:00+00:00")
    _ins_vote(fresh_db, rid, 10, "2026-11-03T12:10:00+00:00", 0, 0)
    res = game.award_week("2026-W45", 3, (99,), at(14, 13))
    assert res["winner"] is None and res["prize_days"] == 0
    row = fresh_db.query_one("SELECT * FROM game_weeks WHERE week_key='2026-W45'")
    assert row["winner_tg_id"] is None and row["players"] == 1


def test_weeks_to_award_waits_for_unresolved_round(fresh_db):
    from app.delivery import game

    _ins_round(fresh_db, "2026-10-27", "2026-10-27T12:00:00+00:00")
    pending = _ins_round(fresh_db, "2026-11-01", "2026-11-01T12:00:00+00:00", outcome=None)
    assert game.weeks_to_award(at(7, 11)) == []              # воскресный раунд ещё не решён
    fresh_db.execute("UPDATE game_rounds SET outcome='down' WHERE id=?", (pending,))
    assert game.weeks_to_award(at(7, 12, 5)) == ["2026-W44"]


# ---------------------------------------------------------------------------
# выход из игры и гейты рассылки
# ---------------------------------------------------------------------------
def test_opt_out_stops_rounds_resets_streak_keeps_week_points(fresh_db):
    from app.delivery import game

    _players(1, 2)
    ex = _Ex({bar(0, 11): 100, bar(1, 11): 101})
    _tick(ex, at(0, 12, 0, 30))
    r0 = game.open_round(at(0, 12, 1))
    game.cast_vote(r0["id"], 1, "up", at(0, 12, 5))
    _, r1 = _tick(ex, at(1, 12, 0, 30))
    assert game.player(1)["streak"] == 1
    assert game.leave(1, at(1, 12, 1)) is True
    assert game.leave(1, at(1, 12, 2)) is False               # уже вышел
    assert game.player(1)["streak"] == 0                     # «выйти и сберечь серию» нельзя
    assert game.round_recipients(r1["id"], at(1, 12, 3)) == [2]
    board = game.leaderboard("2026-W44")
    assert {a["tg_id"]: a["points"] for a in board}[1] == 1  # заработанное остаётся
    assert game.join(1, at(1, 13)) == "back"
    assert game.join(1, at(1, 13)) == "already"
    assert sorted(game.round_recipients(r1["id"], at(1, 13, 1))) == [1, 2]


def test_round_recipients_respect_mute_quiet_hours_and_blocked(fresh_db):
    from app.delivery import game, payments

    _players(1, 2, 3, 4, 5, 6)
    payments.set_muted(2, True)
    payments.set_quiet_hours(3, 10, 14)                      # 12:00 UTC — тишина
    payments.mark_blocked(4)
    _, r = _tick(_Ex(), at(0, 12, 0, 30))
    game.cast_vote(r["id"], 5, "up", at(0, 12, 1))           # уже ответил
    game.claim_send("round", str(r["id"]), 6, at(0, 12, 1))  # уже отправлено
    assert game.round_recipients(r["id"], at(0, 12, 2)) == [1]
    # тишина кончилась, голосование ещё идёт — раунд догоняет
    assert game.round_recipients(r["id"], at(0, 14, 5)) == [1, 3]


# ---------------------------------------------------------------------------
# рассылка фейковым ботом
# ---------------------------------------------------------------------------
class _Photo:
    def __init__(self, fid):
        self.file_id = fid


class _Msg:
    def __init__(self, fid=None):
        self.photo = [_Photo(fid)] if fid else None


class _Bot:
    def __init__(self, dead=(), flaky=None):
        self.photos: list = []
        self.texts: list = []
        self.log: list = []                                  # (вид, chat_id) в порядке отправки
        self.dead = set(dead)
        self.flaky = dict(flaky or {})                       # chat_id -> сколько раз упасть (сеть)

    def _maybe_fail(self, chat_id):
        if chat_id in self.dead:
            raise RuntimeError("Telegram server says - Forbidden: bot was blocked by the user")
        if self.flaky.get(chat_id, 0) > 0:
            self.flaky[chat_id] -= 1
            raise RuntimeError("Request timeout error")

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None, reply_markup=None):
        self._maybe_fail(chat_id)
        self.photos.append((chat_id, photo, caption, reply_markup))
        self.log.append(("photo", chat_id))
        return _Msg(f"fid-{len(self.photos)}")

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self._maybe_fail(chat_id)
        self.texts.append((chat_id, text, reply_markup))
        self.log.append(("text", chat_id))
        return _Msg()


def _send(bot, now, renders, exclude=(), prize_days=3):
    from app.delivery import game, payments

    async def _render(data):
        renders.append(data)
        return b"\x89PNG-fake"

    return asyncio.run(game.send_pending(bot, render=_render, prize_days=prize_days,
                                         on_dead=payments.mark_blocked, log=LOG,
                                         exclude=exclude, now=now, pause=0))


def test_send_round_one_message_per_player_png_once_then_file_id(fresh_db):
    from aiogram.types import BufferedInputFile

    from app.delivery import game

    _players(1, 2, 3)
    _, r = _tick(_Ex({bar(0, 11): 68123.4}), at(0, 12, 0, 30))
    bot, renders = _Bot(), []
    out = _send(bot, at(0, 12, 1), renders)
    assert out["round"] == 3 and len(renders) == 1           # карта рендерится один раз
    assert [p[0] for p in bot.photos] == [1, 2, 3]
    assert isinstance(bot.photos[0][1], BufferedInputFile)
    assert bot.photos[1][1] == "fid-1" and bot.photos[2][1] == "fid-1"   # дальше — file_id
    assert game.get_round(r["id"])["photo_file_id"] == "fid-1"
    caption, kb = bot.photos[0][2], bot.photos[0][3]
    assert "68 123.4" in caption
    assert [b.callback_data for b in kb.inline_keyboard[0]] == \
        [f"game:v:{r['id']}:up", f"game:v:{r['id']}:down"]
    # следующий такт — никому второй раз
    out2 = _send(bot, at(0, 12, 2), renders)
    assert out2["round"] == 0 and len(bot.photos) == 3


def test_send_round_dead_recipient_marked_flaky_retried(fresh_db):
    from app.delivery import game, payments

    _players(1, 2, 3, 4)
    _tick(_Ex(), at(0, 12, 0, 30))
    # 2 заблокировал бота; 3 — сеть упала на фото И на тексте; 4 — только на фото
    bot = _Bot(dead={2}, flaky={3: 2, 4: 1})
    out = _send(bot, at(0, 12, 1), [])
    assert out["round"] == 2
    assert [p[0] for p in bot.photos] == [1]
    assert [t[0] for t in bot.texts] == [4]                  # фото не ушло — раунд текстом
    assert payments.get_user(2)["is_active"] == 0            # мёртвый получатель — пометка
    out2 = _send(bot, at(0, 12, 2), [])
    assert out2["round"] == 1 and [p[0] for p in bot.photos] == [1, 3]   # сеть — повтор
    assert game.round_recipients(1, at(0, 12, 3)) == []


def test_send_round_skipped_when_voting_almost_over(fresh_db):
    _players(1)
    _tick(_Ex(), at(0, 12, 0, 30))
    bot = _Bot()
    out = _send(bot, at(0, 17, 45), [])                      # до конца окна 15 минут
    assert out["round"] == 0 and bot.photos == []


def test_send_round_falls_back_to_text_when_render_fails(fresh_db):
    from app.delivery import game, payments

    _players(1)
    _tick(_Ex(), at(0, 12, 0, 30))
    bot = _Bot()

    async def _boom(data):
        raise RuntimeError("chromium crashed")

    out = asyncio.run(game.send_pending(bot, render=_boom, prize_days=3,
                                        on_dead=payments.mark_blocked, log=LOG,
                                        now=at(0, 12, 1), pause=0))
    assert out["round"] == 1 and bot.photos == []
    assert "Лонг или шорт" in bot.texts[0][1] and bot.texts[0][2] is not None   # кнопки на месте


def test_results_sent_once_to_voters_respecting_mute(fresh_db):
    from app.delivery import payments

    _players(1, 2, 3)
    payments.set_muted(3, True)
    ex = _Ex({bar(0, 11): 100, bar(1, 11): 101})
    _, r = _tick(ex, at(0, 12, 0, 30))
    from app.delivery import game
    game.cast_vote(r["id"], 1, "up", at(0, 12, 5))
    game.cast_vote(r["id"], 2, "down", at(0, 12, 6))
    game.cast_vote(r["id"], 3, "up", at(0, 12, 7))
    _tick(ex, at(1, 12, 0, 30))
    bot = _Bot()
    out = _send(bot, at(1, 12, 1), [])
    assert out["result"] == 2                                # 3 в мьюте — итог не пушим
    by = {t[0]: t[1] for t in bot.texts}
    assert "✅ Угадал: +1 очко" in by[1] and "место <b>1</b> из 3" in by[1]
    assert "❌ Не угадал" in by[2]
    assert "+1.00%" in by[1]
    assert out["round"] == 2                                 # раунд дня — вступившим без мьюта
    assert [k for k, u in bot.log if u == 1] == ["text", "photo"]   # «вчера» раньше «сегодня»
    out2 = _send(bot, at(1, 12, 3), [])
    assert out2["result"] == 0                               # итог — один раз


def test_admin_result_is_out_of_competition(fresh_db):
    from app.delivery import game

    _players(1, 99)
    ex = _Ex({bar(0, 11): 100, bar(1, 11): 101})
    _, r = _tick(ex, at(0, 12, 0, 30))
    game.cast_vote(r["id"], 99, "up", at(0, 12, 5))
    _tick(ex, at(1, 12, 0, 30))
    bot = _Bot()
    _send(bot, at(1, 12, 1), [], exclude=(99,))
    text = next(t[1] for t in bot.texts if t[0] == 99)
    assert "вне зачёта" in text and "место" not in text


def test_week_notice_to_participants_and_winner(fresh_db):
    from app.delivery import game

    _week_fixture(fresh_db)
    for uid in (10, 20, 30):
        game.join(uid, at(0, 9))
    game.leave(30, at(3, 9))                                 # вышел — итоги недели не шлём
    game.award_due_weeks(at(7, 13), prize_days=3, exclude=(99,))
    bot = _Bot()
    out = _send(bot, at(7, 13, 1), [], exclude=(99,))
    by = {t[0]: t[1] for t in bot.texts}
    assert set(by) == {10, 20} and out["week"] == 2
    assert "Ты победил" in by[20] and "+3 дн." in by[20]
    assert "Победитель получает +3 дн." in by[10] and "место <b>2</b>" in by[10]
    assert "pla***20" in by[10]                              # ники в топе — маскированы
    assert _send(bot, at(7, 13, 2), [], exclude=(99,))["week"] == 0


# ---------------------------------------------------------------------------
# тексты
# ---------------------------------------------------------------------------
def _plain(html_text: str) -> str:
    return re.sub(r"<[^>]+>", "", html_text)


def test_round_caption_fits_and_is_honest(fresh_db):
    from app.delivery import game

    _, r = _tick(_Ex({bar(0, 11): 112345.6}), at(0, 12, 0, 30))
    cap = game.render_round_caption(r, 3)
    assert len(_plain(cap)) < 1024                           # лимит подписи к фото
    assert "112 345.6" in cap and "до 18:00 UTC" in cap and "27.10 в 12:00 UTC" in cap
    assert "не торговый сигнал" in cap and "+3 дн." in cap
    assert "+3 дн." not in game.render_round_caption(r, 0)   # приз выключен — не обещаем
    for word in ("6226", "LTF", "1:1", "SMC", "прибыл", "заработ"):
        assert word not in cap, word
    assert len(game.vote_ack(r, "up")) <= 200                # лимит всплывашки callback


def test_status_new_has_rules_and_top_masks_names(fresh_db):
    from app.delivery import game, payments

    _players(1)
    txt = game.render_status(1, "new", 3, at(0, 9))
    assert "Ты в игре" in txt and "+1 очко" in txt and "не торговый сигнал" in txt
    assert "сегодня в 12:00 UTC" in txt
    # 12 игроков: свой ряд за пределами топ-10 показан отдельно
    rid = _ins_round(fresh_db, "2026-10-27", "2026-10-27T12:00:00+00:00")
    for uid in range(100, 112):
        payments.register_user(uid, f"trader{uid}")
        _ins_vote(fresh_db, rid, uid, "2026-10-27T12:30:00+00:00", 1, 112 - uid)
    board = game.leaderboard("2026-W44")
    top = game.render_top("2026-W44", board, 111, 3)
    assert "tra***00" in top and "trader100" not in top
    assert top.count("\n1. ") == 1 and "\n11. " not in top
    assert "Ты: место 12 из 12" in top
    status = game.render_status(1, "already", 3, at(1, 13))
    assert "Серия: <b>0</b>" in status and "/game off" in status


def test_runtime_game_prize_days(fresh_db):
    from app import runtime

    assert runtime.game_prize_days() == 3
    runtime.set_value("game_prize_days", "0")
    assert runtime.game_prize_days() == 0
    runtime.set_value("game_prize_days", "-5")
    assert runtime.game_prize_days() == 0                    # отрицательный приз — 0
    runtime.set_value("game_prize_days", "abc")
    assert runtime.game_prize_days() == 3                    # битое — дефолт
    assert runtime.effective()["game_prize_days"] == 3


# ---------------------------------------------------------------------------
# карта раунда
# ---------------------------------------------------------------------------
def _chart_data():
    bars = [(bar(0, 0) + i * H, 68000 + i * 5, 68040 + i * 5, 67960 + i * 5, 68010 + i * 5)
            for i in range(48)]
    return {"day_label": "26.10.2026", "symbol": "BTC/USDT", "bars": bars,
            "ref_price": bars[-1][4], "result_label": "27.10 12:00 UTC"}


def test_build_game_chart_html_structure():
    from app.delivery import graphics

    html = graphics.build_game_chart_html(_chart_data())
    assert html.count('class="c-body"') == 48
    assert html.count('class="future"') == 2                 # «выше» зелёная / «ниже» красная
    assert "Сейчас 68 245" in html and "▲ выше" in html and "▼ ниже" in html
    assert "не торговый сигнал" in html and "Раунд 26.10.2026" in html
    data = dict(_chart_data(), day_label="<script>x</script>")
    assert "<script>x" not in graphics.build_game_chart_html(data)
    with pytest.raises(ValueError):
        graphics.build_game_chart_html(dict(_chart_data(), bars=[]))


def test_chart_data_roundtrip_from_db(fresh_db):
    from app.delivery import game, graphics

    _, r = _tick(_Ex({bar(0, 11): 68123.4}), at(0, 12, 0, 30))
    data = game.chart_data(r)
    assert len(data["bars"]) == 48 and data["ref_price"] == pytest.approx(68123.4)
    assert data["result_label"] == "27.10 12:00 UTC"
    assert "68 123.4" in graphics.build_game_chart_html(data)


def test_render_game_chart_produces_valid_png():
    pytest.importorskip("playwright")
    from app.delivery import graphics

    png = graphics.render_game_chart(_chart_data())
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10_000
