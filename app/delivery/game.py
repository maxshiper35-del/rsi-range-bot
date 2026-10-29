"""
game.py — ежедневная игра «⬆️ Лонг или шорт? ⬇️» (ретеншн, 29.10.2026).

Зачем: юзеры уходят на конце 5-дневного триала — бесплатному остатку нечем звать в бота
каждый день. Игра — такой повод: раз в сутки один вопрос «BTC через 24 часа будет выше
или ниже, чем сейчас?», очки, серии, недельный топ и приз (дни полного доступа). Дёшево:
два запроса к бирже в сутки (старт и итог) и один PNG-рендер на раунд (дальше — file_id).

Правила (они же — в /game rules):
- Opt-in: раунды приходят только вступившим (/game или кнопка-приглашение INVITE_TEXT);
  выход — /game off. Мьют и тихие часы /notify уважаются: раунд придёт, когда тишина
  кончится (если голосование ещё открыто), итог — так же.
- Раунд — один на UTC-сутки, старт в 12:00 UTC. Mac спал — раунд стартует при пробуждении,
  но не позже 18:00 UTC (позже пуш уходил бы основной аудитории, CET…MSK, в ночь);
  пропущенные сутки задним числом не догоняются — голосовать в них было некому.
- Цена «сейчас» — close последнего ЗАКРЫТОГО 1h-бара BTC/USDT (Bybit) на старте. Итог —
  close бара ровно через 24 ч от него (target_bar_ts = ref_bar_ts + 24ч), а НЕ цена в момент
  проверки: раунд, проверенный с опозданием (Mac спал), судится тем же баром, что и вовремя.
- Голос — один, изменить нельзя (PRIMARY KEY, UPDATE choice в коде нет); голосование 6 ч.
- Ход меньше ±0.05% (включительно) — ничья: очков нет, серия проголосовавших не меняется.
- Очки: +1 за верный ответ; каждый 3-й верный подряд (серия 3, 6, 9…) — ещё +1.
- Серию обнуляют промах, пропуск ДОСТАВЛЕННОГО раунда (пришёл — не ответил) и выход из
  игры. Недоставленный раунд (мьют, тишина до конца голосования, бот спал) серию не трогает —
  нельзя наказывать за то, чего юзер не видел.
- Неделя — ISO (пн–вс, UTC) по дню старта раунда. Победитель — больше всех очков. Ничья по
  очкам -> выше тот, кто в среднем РАНЬШЕ отвечал на свои угаданные раунды (секунды от
  старта раунда): цена отсчёта общая, и кто ждал полдня, видел больше движения — скорость
  уравновешивает это преимущество. Дальше — кто раньше проголосовал впервые за неделю, tg_id.
  Делёж приза отвергнут: на 7 бинарных раундах ничьи у вершины — норма, и «поровну на всех»
  размывал бы приз до часов. Приз — runtime.game_prize_days() дней доступа (0 — без приза),
  не платёж. Админы вне зачёта: приз владельца самому себе выглядел бы подставой.

Честность: игра — не сигнал и не совет, цена BTC через сутки близка к монетке — текст
говорит это прямо. Ник в топе маскируется: список участников крипто-бота не должен
становиться базой для скам-рассылок.

Слои: чистые правила и SQL — синхронные (в боте — через asyncio.to_thread); рассылка
(send_pending) — async, бот передаётся параметром: тестируется фейковым ботом без
aiogram-диспетчера.
"""
from __future__ import annotations

import asyncio
import html
import json
import sqlite3
from collections.abc import Awaitable, Callable, Collection
from datetime import date, datetime, timedelta, timezone
from functools import partial
from typing import Any

from app import clock, db
from app.delivery import payments
from app.delivery.graphics import spaced_price

SYMBOL = "BTC/USDT:USDT"
EXCHANGE_ID = "bybit"            # текст раунда обещает цену Bybit — не зависим от EXCHANGE_ID
TF = "1h"
BAR_MS = 3_600_000
ROUND_HOUR_UTC = 12
LAST_START_HOUR_UTC = 18
VOTE_HOURS = 6
HORIZON_BARS = 24                # итог — через 24 часовых бара от бара отсчёта
FLAT_PCT = 0.05
STREAK_EVERY = 3
CHART_BARS = 48
MAX_FETCH_BARS = 1000            # потолок одного fetch_ohlcv у Bybit (~41 сутки часовых баров)
VOID_AFTER_HOURS = 48            # бар итога так и не появился — раунд аннулируется
MIN_VOTE_LEFT_MIN = 30           # раунд, на который осталось меньше получаса, не присылаем
RESULT_NOTIFY_HOURS = 24         # итог, не доставленный за сутки (тишина/сбой), уже не шлём
WEEK_NOTIFY_HOURS = 72
TOP_N = 10

UP, DOWN, FLAT, VOID = "up", "down", "flat", "void"
CHOICE_LABEL = {UP: "⬆️ Выше", DOWN: "⬇️ Ниже"}

# (symbol, tf, limit) -> DataFrame с колонками ts, open, high, low, close (форма ccxt)
FetchFn = Callable[[str, str, int], Any]

INVITE_TEXT = (
    "🎯 <b>Лонг или шорт?</b> Каждый день в 12:00 UTC — один вопрос: BTC через сутки "
    "будет выше или ниже? Очки, серии и приз недели — дни полного доступа. "
    "Вступить: /game"
)

LEAVE_TEXT = (
    "Ок, раунды больше не присылаю. Очки этой недели остаются за тобой, серия обнулена.\n"
    "Вернуться — /game"
)


# ---------------------------------------------------------------------------
# Чистые правила
# ---------------------------------------------------------------------------
def _aware(now: datetime | None) -> datetime:
    now = now or clock.utcnow()
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def esc(v: object) -> str:
    return html.escape(str(v), quote=False)


def change_pct(ref_price: float, result_price: float) -> float:
    return (result_price - ref_price) / ref_price * 100.0 if ref_price else 0.0


def classify(ref_price: float, result_price: float, flat_pct: float = FLAT_PCT) -> str:
    """Исход раунда: 'up' | 'down' | 'flat' (|ход| ≤ flat_pct% — ничья, очков нет)."""
    ch = change_pct(ref_price, result_price)
    if abs(ch) <= flat_pct:
        return FLAT
    return UP if ch > 0 else DOWN


def vote_points(streak_after: int) -> int:
    """Очки за ВЕРНЫЙ ответ; streak_after — серия с учётом этого ответа (3, 6, 9… — бонус)."""
    return 1 + (1 if streak_after > 0 and streak_after % STREAK_EVERY == 0 else 0)


def week_key(d: date | datetime) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_days(key: str) -> tuple[date, date]:
    """'YYYY-Www' -> (понедельник, воскресенье)."""
    y, w = key.split("-W")
    monday = date.fromisocalendar(int(y), int(w), 1)
    return monday, monday + timedelta(days=6)


def week_label(key: str) -> str:
    mon, sun = week_days(key)
    return f"{mon:%d.%m}–{sun:%d.%m}"


def mask_name(username: str | None, tg_id: int) -> str:
    """Имя в топе: ник маскируется (vas***ya), без ника — хвост id. Полные ники участников
    крипто-бота в открытом списке — готовая база для скам-рассылок."""
    u = (username or "").lstrip("@")
    if not u:
        return f"Игрок …{str(tg_id)[-4:]}"
    if len(u) <= 4:
        return u[:1] + "***"
    if len(u) <= 6:
        return u[:2] + "***" + u[-1:]
    return u[:3] + "***" + u[-2:]


def points_word(n: int) -> str:
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return "очков"
    if n % 10 == 1:
        return "очко"
    if 2 <= n % 10 <= 4:
        return "очка"
    return "очков"


def _bars(df) -> list[tuple[int, float, float, float, float]]:
    """DataFrame формы ccxt -> [(ts_ms, o, h, l, c)] по возрастанию времени."""
    if df is None or len(df) == 0:
        return []
    rows = zip(df["ts"], df["open"], df["high"], df["low"], df["close"])
    return sorted((int(t), float(o), float(h), float(lo), float(c)) for t, o, h, lo, c in rows)


def _default_fetch(symbol: str, tf: str, limit: int):
    from app.data.market_data import fetch_closed_ohlcv  # общий ccxt-инстанс + rate-limit

    return fetch_closed_ohlcv(symbol, tf, limit=limit, exchange_id=EXCHANGE_ID)


# ---------------------------------------------------------------------------
# Участники
# ---------------------------------------------------------------------------
def player(tg_id: int) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM game_players WHERE tg_id=?", (tg_id,))


def is_player(tg_id: int) -> bool:
    p = player(tg_id)
    return bool(p and p["active"])


def join(tg_id: int, now: datetime | None = None) -> str:
    """Вступить в игру. -> 'new' | 'back' (возврат после /game off) | 'already'."""
    now_iso = _aware(now).isoformat()
    with db.transaction() as conn:
        row = conn.execute("SELECT active, left_at FROM game_players WHERE tg_id=?",
                           (tg_id,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO game_players(tg_id, active, joined_at, updated_at) "
                         "VALUES(?,1,?,?)", (tg_id, now_iso, now_iso))
            return "new"
        if row["active"]:
            return "already"
        # строка без left_at — голосовал по пересланному раунду, но не вступал: это первое
        # вступление, а не «возврат»
        conn.execute("UPDATE game_players SET active=1, left_at=NULL, joined_at=?, updated_at=? "
                     "WHERE tg_id=?", (now_iso, now_iso, tg_id))
        return "back" if row["left_at"] else "new"


def leave(tg_id: int, now: datetime | None = None) -> bool:
    """Выйти: раунды больше не приходят, серия обнуляется (иначе «выйти на день — сберечь
    серию»); очки недели остаются — они заработаны. -> был ли в игре."""
    now_iso = _aware(now).isoformat()
    return db.execute("UPDATE game_players SET active=0, left_at=?, streak=0, updated_at=? "
                      "WHERE tg_id=? AND active=1", (now_iso, now_iso, tg_id)) > 0


# ---------------------------------------------------------------------------
# Раунды и голоса
# ---------------------------------------------------------------------------
def get_round(round_id: int) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM game_rounds WHERE id=?", (round_id,))


def round_due(now: datetime | None = None) -> bool:
    """Пора ли стартовать раунд дня: окно [12:00, 18:00) UTC и раунда за эти сутки нет."""
    now = _aware(now)
    if not ROUND_HOUR_UTC <= now.hour < LAST_START_HOUR_UTC:
        return False
    return db.query_one("SELECT 1 FROM game_rounds WHERE day=?",
                        (now.date().isoformat(),)) is None


def ensure_round(now: datetime | None = None, fetch: FetchFn | None = None) -> sqlite3.Row | None:
    """Стартовать раунд дня, если пора (round_due). -> строка НОВОГО раунда или None.

    «Сейчас» — close часового бара, закрывшегося в последний полный час: тот же ряд, что
    судит итог, сравниваются два close одной серии (не тикер против свечи). Биржа его ещё
    не отдала (первые секунды часа) или ряд протух — раунд не стартует, повтор следующим
    тактом: «сейчас» на час старше было бы враньём в вопросе. UNIQUE(day) режет гонку двух
    тактов — второй получает None."""
    now = _aware(now)
    if not round_due(now):
        return None
    df = (fetch or _default_fetch)(SYMBOL, TF, CHART_BARS + 2)
    now_ms = _ms(now)
    bars = [b for b in _bars(df) if b[0] + BAR_MS <= now_ms]
    if not bars or bars[-1][0] != (now_ms // BAR_MS) * BAR_MS - BAR_MS:
        return None
    ref = bars[-1]
    try:
        rid = db.execute(
            "INSERT INTO game_rounds(day, symbol, ref_bar_ts, ref_price, target_bar_ts, "
            "chart_json, created_at, closes_at) VALUES(?,?,?,?,?,?,?,?)",
            (now.date().isoformat(), SYMBOL, ref[0], ref[4], ref[0] + HORIZON_BARS * BAR_MS,
             json.dumps(bars[-CHART_BARS:]), now.isoformat(),
             (now + timedelta(hours=VOTE_HOURS)).isoformat()))
    except sqlite3.IntegrityError:
        return None
    return get_round(rid)


def open_round(now: datetime | None = None) -> sqlite3.Row | None:
    """Раунд, по которому сейчас идёт голосование (старт раз в сутки, окно 6 ч — не больше
    одного)."""
    return db.query_one("SELECT * FROM game_rounds WHERE outcome IS NULL AND closes_at > ? "
                        "ORDER BY id DESC LIMIT 1", (_aware(now).isoformat(),))


def get_vote(round_id: int, tg_id: int) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM game_votes WHERE round_id=? AND tg_id=?",
                        (round_id, tg_id))


def cast_vote(round_id: int, tg_id: int, choice: str, now: datetime | None = None) -> str:
    """Голос. -> 'ok' | 'already' | 'closed' | 'no_round' | 'bad_choice'.

    Один на раунд и без переголосования: проверка и вставка — в одной транзакции, а
    PRIMARY KEY(round_id, tg_id) режет дабл-тап. Голосующий без строки игрока (пересланный
    раунд) получает строку active=0: итог этого раунда придёт, ежедневные — нет (opt-in)."""
    if choice not in (UP, DOWN):
        return "bad_choice"
    now = _aware(now)
    now_iso = now.isoformat()
    with db.transaction() as conn:
        r = conn.execute("SELECT closes_at, outcome FROM game_rounds WHERE id=?",
                         (round_id,)).fetchone()
        if r is None:
            return "no_round"
        if conn.execute("SELECT 1 FROM game_votes WHERE round_id=? AND tg_id=?",
                        (round_id, tg_id)).fetchone():
            return "already"
        if r["outcome"] is not None or datetime.fromisoformat(r["closes_at"]) <= now:
            return "closed"
        try:
            conn.execute("INSERT INTO game_votes(round_id, tg_id, choice, voted_at) "
                         "VALUES(?,?,?,?)", (round_id, tg_id, choice, now_iso))
        except sqlite3.IntegrityError:
            return "already"
        conn.execute("INSERT OR IGNORE INTO game_players(tg_id, active, joined_at, updated_at) "
                     "VALUES(?,0,?,?)", (tg_id, now_iso, now_iso))
    return "ok"


def set_photo_file_id(round_id: int, file_id: str) -> None:
    db.execute("UPDATE game_rounds SET photo_file_id=? WHERE id=? AND photo_file_id IS NULL",
               (file_id, round_id))


def pending_votes(tg_id: int) -> list[sqlite3.Row]:
    """Голоса юзера в ещё не решённых раундах (для /game)."""
    return db.query("SELECT r.*, v.choice FROM game_votes v JOIN game_rounds r "
                    "ON r.id = v.round_id WHERE v.tg_id=? AND r.outcome IS NULL "
                    "ORDER BY r.id", (tg_id,))


# ---------------------------------------------------------------------------
# Итог раунда: очки и серии
# ---------------------------------------------------------------------------
def due_rounds(now: datetime | None = None) -> list[sqlite3.Row]:
    """Нерешённые раунды, чей бар итога уже закрылся по часам. Отдала ли его биржа — решает
    resolve_due по данным (закрытый бар есть в ряду), а не запас по времени: в 12:00 итог
    вчерашнего раунда и старт сегодняшнего попадают в ОДИН такт (итог — первым)."""
    cutoff = _ms(_aware(now)) - BAR_MS
    return db.query("SELECT * FROM game_rounds WHERE outcome IS NULL AND target_bar_ts <= ? "
                    "ORDER BY day, id", (cutoff,))


def resolve_round(round_id: int, result_price: float,
                  now: datetime | None = None) -> dict | None:
    """Итог раунда + очки + серии — ОДНОЙ транзакцией с гейтом outcome IS NULL: повторный
    вызов или гонка тактов не начислят дважды. -> сводка или None (уже решён/нет раунда)."""
    now_iso = _aware(now).isoformat()
    with db.transaction() as conn:
        r = conn.execute("SELECT * FROM game_rounds WHERE id=?", (round_id,)).fetchone()
        if r is None or r["outcome"] is not None:
            return None
        outcome = classify(r["ref_price"], result_price)
        cur = conn.execute("UPDATE game_rounds SET outcome=?, result_price=?, resolved_at=? "
                           "WHERE id=? AND outcome IS NULL",
                           (outcome, result_price, now_iso, round_id))
        if cur.rowcount != 1:
            return None
        votes = conn.execute("SELECT tg_id, choice FROM game_votes WHERE round_id=? "
                             "ORDER BY voted_at", (round_id,)).fetchall()
        correct_n = 0
        for v in votes:
            p = conn.execute("SELECT streak, best_streak FROM game_players WHERE tg_id=?",
                             (v["tg_id"],)).fetchone()
            streak = int(p["streak"]) if p else 0
            best = int(p["best_streak"]) if p else 0
            correct: int | None
            if outcome == FLAT:
                correct, points = None, 0                # ничья: серия не меняется
            elif v["choice"] == outcome:
                streak += 1
                correct, points = 1, vote_points(streak)
                correct_n += 1
            else:
                streak, correct, points = 0, 0, 0
            conn.execute("UPDATE game_votes SET correct=?, points=?, streak_after=? "
                         "WHERE round_id=? AND tg_id=?",
                         (correct, points, streak, round_id, v["tg_id"]))
            conn.execute("UPDATE game_players SET streak=?, best_streak=?, updated_at=? "
                         "WHERE tg_id=?", (streak, max(best, streak), now_iso, v["tg_id"]))
        # раунд пришёл (sent_at — реально доставлен), а ответа нет -> серия обнуляется
        missed = [row["tg_id"] for row in conn.execute(
            "SELECT s.tg_id FROM game_sends s WHERE s.kind='round' AND s.ref=? "
            "AND s.sent_at IS NOT NULL AND NOT EXISTS (SELECT 1 FROM game_votes v "
            "WHERE v.round_id=? AND v.tg_id=s.tg_id)", (str(round_id), round_id))]
        for uid in missed:
            conn.execute("UPDATE game_players SET streak=0, updated_at=? "
                         "WHERE tg_id=? AND streak > 0", (now_iso, uid))
    return {"round_id": round_id, "day": r["day"], "outcome": outcome,
            "ref_price": float(r["ref_price"]), "result_price": float(result_price),
            "voters": len(votes), "correct": correct_n, "missed": len(missed)}


def void_round(round_id: int, now: datetime | None = None) -> dict | None:
    """Аннулировать раунд (бар итога биржа так и не отдала): очков нет, серии не меняются."""
    now_iso = _aware(now).isoformat()
    with db.transaction() as conn:
        r = conn.execute("SELECT day FROM game_rounds WHERE id=?", (round_id,)).fetchone()
        cur = conn.execute("UPDATE game_rounds SET outcome='void', resolved_at=? "
                           "WHERE id=? AND outcome IS NULL", (now_iso, round_id))
        if r is None or cur.rowcount != 1:
            return None
        conn.execute("UPDATE game_votes SET correct=NULL, points=0, streak_after=COALESCE("
                     "(SELECT p.streak FROM game_players p WHERE p.tg_id=game_votes.tg_id), 0) "
                     "WHERE round_id=?", (round_id,))
    return {"round_id": round_id, "day": r["day"], "outcome": VOID}


def resolve_due(now: datetime | None = None, fetch: FetchFn | None = None) -> list[dict]:
    """Решить раунды, чей бар итога (ref + 24 ч) закрыт. Судит close ИМЕННО этого бара, а не
    текущую цену: раунд, проверенный через трое суток (Mac спал), получает тот же итог, что
    получил бы вовремя. Один фетч на все просроченные раунды — глубина от самого старого.

    Порядок — по дням: серии зависят от предыдущих раундов, поэтому раунд, чей бар биржа ещё
    не отдала, задерживает следующие, а не перепрыгивается. Аннулирование — когда ждать
    бессмысленно: бар левее окна, которое отдаёт биржа (>~41 суток), или так и не появился
    за VOID_AFTER_HOURS."""
    now = _aware(now)
    rows = due_rounds(now)
    if not rows:
        return []
    now_ms = _ms(now)
    need = (now_ms - int(rows[0]["target_bar_ts"])) // BAR_MS + 3
    df = (fetch or _default_fetch)(SYMBOL, TF, int(min(max(need, 3), MAX_FETCH_BARS)))
    closes = {b[0]: b[4] for b in _bars(df) if b[0] + BAR_MS <= now_ms}
    first = min(closes) if closes else None
    out = []
    for r in rows:
        tgt = int(r["target_bar_ts"])
        if tgt in closes:
            res = resolve_round(r["id"], closes[tgt], now)
        elif (first is not None and tgt < first) or \
                now_ms - (tgt + BAR_MS) >= VOID_AFTER_HOURS * 3_600_000:
            res = void_round(r["id"], now)
        else:
            break                                       # бар ещё не отдан — ждём по порядку
        if res:
            out.append(res)
    return out


# ---------------------------------------------------------------------------
# Неделя: топ и приз
# ---------------------------------------------------------------------------
def leaderboard(key: str, exclude: Collection[int] = ()) -> list[dict]:
    """Топ ISO-недели (по дню старта раунда). Порядок: очки ↓, средние секунды от старта
    раунда до ВЕРНЫХ ответов ↑ (кто раньше угадывал), первый голос недели ↑, tg_id.
    exclude (админы) — вне зачёта: в топ и места не попадают. Нерешённые голоса — 0 очков
    (участие видно сразу). -> [{tg_id, username, points, votes, wins, avg_answer_sec, place}]."""
    mon, sun = week_days(key)
    rows = db.query(
        "SELECT v.tg_id, v.voted_at, v.correct, v.points, r.created_at, u.username "
        "FROM game_votes v JOIN game_rounds r ON r.id = v.round_id "
        "LEFT JOIN users u ON u.tg_id = v.tg_id WHERE r.day >= ? AND r.day <= ?",
        (mon.isoformat(), sun.isoformat()))
    agg: dict[int, dict] = {}
    secs: dict[int, list[float]] = {}
    skip = set(exclude)
    for r in rows:
        uid = int(r["tg_id"])
        if uid in skip:
            continue
        a = agg.setdefault(uid, {"tg_id": uid, "username": r["username"], "points": 0,
                                 "votes": 0, "wins": 0, "first_vote": r["voted_at"]})
        a["votes"] += 1
        a["points"] += int(r["points"] or 0)
        a["first_vote"] = min(a["first_vote"], r["voted_at"])
        if r["correct"] == 1:
            a["wins"] += 1
            secs.setdefault(uid, []).append(
                (datetime.fromisoformat(r["voted_at"])
                 - datetime.fromisoformat(r["created_at"])).total_seconds())
    board = []
    for uid, a in agg.items():
        s = secs.get(uid)
        a["avg_answer_sec"] = sum(s) / len(s) if s else None
        board.append(a)
    board.sort(key=lambda a: (-a["points"],
                              a["avg_answer_sec"] if a["avg_answer_sec"] is not None
                              else float("inf"),
                              a["first_vote"], a["tg_id"]))
    for i, a in enumerate(board, 1):
        a["place"] = i
    return board


def week_points(key: str, tg_id: int) -> int:
    mon, sun = week_days(key)
    row = db.query_one("SELECT COALESCE(SUM(v.points), 0) AS p FROM game_votes v "
                       "JOIN game_rounds r ON r.id = v.round_id "
                       "WHERE v.tg_id=? AND r.day >= ? AND r.day <= ?",
                       (tg_id, mon.isoformat(), sun.isoformat()))
    return int(row["p"]) if row else 0


def weeks_to_award(now: datetime | None = None) -> list[str]:
    """Закончившиеся ISO-недели с раундами, где все раунды решены, а итог не подведён.
    Не только прошлая неделя: Mac, проспавший две недели, подводит обе по порядку."""
    today = _aware(now).date()
    done = {r["week_key"] for r in db.query("SELECT week_key FROM game_weeks")}
    by_week: dict[str, bool] = {}
    for r in db.query("SELECT day, outcome FROM game_rounds"):
        k = week_key(date.fromisoformat(r["day"]))
        by_week[k] = by_week.get(k, True) and r["outcome"] is not None
    return sorted(k for k, resolved in by_week.items()
                  if resolved and k not in done and week_days(k)[1] < today)


def award_week(key: str, prize_days: float, exclude: Collection[int] = (),
               now: datetime | None = None) -> dict | None:
    """Подвести неделю: победитель — первый в leaderboard с очками > 0. Приз — prize_days
    дней доступа через payments.grant_gift_days (не платёж). Строка недели и продление —
    одной транзакцией, PRIMARY KEY недели — идемпотентность (повтор/гонка -> None)."""
    now_iso = _aware(now).isoformat()
    board = leaderboard(key, exclude)
    top = board[0] if board and board[0]["points"] > 0 else None
    days = float(prize_days) if (top is not None and prize_days > 0) else 0.0
    if top is not None and days > 0:
        payments.register_user(top["tg_id"])            # ДО транзакции: lock не реентерабелен
    with db.transaction() as conn:
        try:
            conn.execute("INSERT INTO game_weeks(week_key, winner_tg_id, points, prize_days, "
                         "players, awarded_at) VALUES(?,?,?,?,?,?)",
                         (key, top["tg_id"] if top else None, top["points"] if top else 0,
                          days, len(board), now_iso))
        except sqlite3.IntegrityError:
            return None
        if top is not None and days > 0:
            payments.grant_gift_days(conn, top["tg_id"], days)
    return {"week_key": key, "winner": top, "prize_days": days, "players": len(board)}


def award_due_weeks(now: datetime | None = None, prize_days: float = 0,
                    exclude: Collection[int] = ()) -> list[dict]:
    out = []
    for k in weeks_to_award(now):
        res = award_week(k, prize_days, exclude, now)
        if res:
            out.append(res)
    return out


# ---------------------------------------------------------------------------
# Исходящие: одна отправка на (вид, раунд/неделя, юзер)
# ---------------------------------------------------------------------------
def claim_send(kind: str, ref: str, tg_id: int, now: datetime | None = None) -> bool:
    """Застолбить отправку ДО неё (at-most-once). -> True, если застолбил этот вызов."""
    with db.transaction() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO game_sends(kind, ref, tg_id, claimed_at) "
                           "VALUES(?,?,?,?)", (kind, ref, tg_id, _aware(now).isoformat()))
        return cur.rowcount == 1


def mark_sent(kind: str, ref: str, tg_id: int, now: datetime | None = None) -> None:
    db.execute("UPDATE game_sends SET sent_at=? WHERE kind=? AND ref=? AND tg_id=?",
               (_aware(now).isoformat(), kind, ref, tg_id))


def release_send(kind: str, ref: str, tg_id: int) -> None:
    """Транзиентный сбой: снять заявку — следующий такт попробует снова."""
    db.execute("DELETE FROM game_sends WHERE kind=? AND ref=? AND tg_id=? AND sent_at IS NULL",
               (kind, ref, tg_id))


def _pushable(tg_id: int, hour: int) -> bool:
    """Мьют и тихие часы /notify — тот же гейт, что у пуша сигналов."""
    return not payments.is_signal_suppressed(tg_id, hour)


def round_recipients(round_id: int, now: datetime | None = None) -> list[int]:
    """Кому сейчас отправить раунд: вступившие, не заблокировавшие бота, ещё не получавшие и
    не голосовавшие, не в мьюте и не в тихих часах (эти получат раунд позже, если голосование
    к концу тишины ещё открыто)."""
    hour = _aware(now).hour
    rows = db.query(
        "SELECT p.tg_id FROM game_players p JOIN users u ON u.tg_id = p.tg_id "
        "WHERE p.active=1 AND u.is_active=1 "
        "AND NOT EXISTS (SELECT 1 FROM game_sends s WHERE s.kind='round' AND s.ref=? "
        "AND s.tg_id=p.tg_id) "
        "AND NOT EXISTS (SELECT 1 FROM game_votes v WHERE v.round_id=? AND v.tg_id=p.tg_id) "
        "ORDER BY p.joined_at, p.tg_id", (str(round_id), round_id))
    return [int(r["tg_id"]) for r in rows if _pushable(int(r["tg_id"]), hour)]


def result_recipients(now: datetime | None = None) -> list[tuple[int, int]]:
    """(round_id, tg_id) итогов к отправке: раунды, решённые за последние сутки, — каждому
    проголосовавшему (и вышедшему из игры: голос по этому раунду он отдал), кто итог ещё не
    получил и сейчас не в мьюте/тишине."""
    now = _aware(now)
    since = (now - timedelta(hours=RESULT_NOTIFY_HOURS)).isoformat()
    rows = db.query(
        "SELECT v.round_id, v.tg_id FROM game_votes v JOIN game_rounds r ON r.id = v.round_id "
        "JOIN users u ON u.tg_id = v.tg_id "
        "WHERE r.outcome IS NOT NULL AND r.resolved_at >= ? AND u.is_active=1 "
        "AND NOT EXISTS (SELECT 1 FROM game_sends s WHERE s.kind='result' "
        "AND s.ref=CAST(v.round_id AS TEXT) AND s.tg_id=v.tg_id) "
        "ORDER BY r.day, v.voted_at", (since,))
    return [(int(r["round_id"]), int(r["tg_id"])) for r in rows
            if _pushable(int(r["tg_id"]), now.hour)]


def week_recipients(now: datetime | None = None) -> list[tuple[str, int]]:
    """(week_key, tg_id) итогов недели к отправке: участники недели, оставшиеся в игре и не в
    мьюте/тишине, + победитель всегда (начисленные дни — транзакционное уведомление, как
    реф-награда)."""
    now = _aware(now)
    since = (now - timedelta(hours=WEEK_NOTIFY_HOURS)).isoformat()
    out = []
    for w in db.query("SELECT * FROM game_weeks WHERE awarded_at >= ? AND players > 0 "
                      "ORDER BY week_key", (since,)):
        mon, sun = week_days(w["week_key"])
        winner = w["winner_tg_id"]
        rows = db.query(
            "SELECT DISTINCT v.tg_id, p.active FROM game_votes v "
            "JOIN game_rounds r ON r.id = v.round_id JOIN users u ON u.tg_id = v.tg_id "
            "LEFT JOIN game_players p ON p.tg_id = v.tg_id "
            "WHERE r.day >= ? AND r.day <= ? AND u.is_active=1 "
            "AND NOT EXISTS (SELECT 1 FROM game_sends s WHERE s.kind='week' AND s.ref=? "
            "AND s.tg_id=v.tg_id)", (mon.isoformat(), sun.isoformat(), w["week_key"]))
        for r in rows:
            uid = int(r["tg_id"])
            if uid == winner or (r["active"] and _pushable(uid, now.hour)):
                out.append((w["week_key"], uid))
    return out


def admin_summary(now: datetime | None = None) -> dict:
    act = db.query_one("SELECT COUNT(*) AS n FROM game_players WHERE active=1")
    gone = db.query_one("SELECT COUNT(*) AS n FROM game_players "
                        "WHERE active=0 AND left_at IS NOT NULL")
    last = db.query_one("SELECT id, day FROM game_rounds ORDER BY id DESC LIMIT 1")
    votes = sent = 0
    if last is not None:
        v = db.query_one("SELECT COUNT(*) AS n FROM game_votes WHERE round_id=?", (last["id"],))
        s = db.query_one("SELECT COUNT(*) AS n FROM game_sends WHERE kind='round' AND ref=? "
                         "AND sent_at IS NOT NULL", (str(last["id"]),))
        votes, sent = (int(v["n"]) if v else 0), (int(s["n"]) if s else 0)
    return {"players": int(act["n"]) if act else 0, "left": int(gone["n"]) if gone else 0,
            "last_day": last["day"] if last else None, "votes": votes, "sent": sent}


# ---------------------------------------------------------------------------
# Тексты (parse_mode=HTML; динамика — через esc)
# ---------------------------------------------------------------------------
def _hhmm(iso_or_dt: str | datetime) -> str:
    d = datetime.fromisoformat(iso_or_dt) if isinstance(iso_or_dt, str) else iso_or_dt
    return f"{d:%H:%M}"


def result_close(r) -> datetime:
    """Момент, когда закрывается бар итога (= когда раунд решается)."""
    return _dt(int(r["target_bar_ts"]) + BAR_MS)


def _prize_line(prize_days: float) -> str:
    return (f"🎁 Приз недели — <b>+{prize_days:g} дн.</b> полного доступа.\n"
            if prize_days > 0 else "")


def render_rules(prize_days: float) -> str:
    return (
        "🎯 <b>Лонг или шорт?</b> — правила\n"
        "Каждый день в 12:00 UTC — один вопрос: BTC через 24 часа будет выше или ниже, "
        "чем сейчас? Ответ — одной кнопкой.\n\n"
        f"• +1 очко за верный ответ, каждый {STREAK_EVERY}-й верный подряд — ещё +1\n"
        f"• ход меньше ±{FLAT_PCT:g}% — ничья, очков нет, серия сохраняется\n"
        f"• один ответ на раунд, изменить нельзя; голосование — {VOTE_HOURS} ч\n"
        "• итог — по закрытию часовой свечи Bybit ровно через сутки\n"
        "• серию обнуляют промах и пропуск пришедшего раунда\n"
        "• неделя пн–вс: побеждает больше очков; при равенстве — кто в среднем раньше "
        "отвечал на угаданные раунды\n"
        f"{_prize_line(prize_days)}\n"
        "<i>Это игра на интуицию, не торговый сигнал: цена BTC через сутки — почти монетка.</i>\n"
        "/game top — топ недели · /game off — выйти"
    )


def render_round_caption(r, prize_days: float) -> str:
    """Подпись к карте раунда (лимит подписи Telegram — 1024 символа)."""
    tgt = result_close(r)
    return (
        f"⬆️ <b>Лонг или шорт?</b> ⬇️  ·  раунд {date.fromisoformat(r['day']):%d.%m}\n"
        "BTC через 24 часа будет выше или ниже, чем сейчас — "
        f"<b>{spaced_price(r['ref_price'])}</b>?\n\n"
        f"⏳ Ответ — до {_hhmm(r['closes_at'])} UTC, один раз, без переголосования.\n"
        f"🏁 Итог — {tgt:%d.%m} в {tgt:%H:%M} UTC по закрытию часовой свечи Bybit.\n"
        f"+1 очко за верный ответ, каждый {STREAK_EVERY}-й верный подряд — ещё +1. "
        f"Ход меньше ±{FLAT_PCT:g}% — ничья.\n"
        f"{_prize_line(prize_days)}"
        "<i>Игра на интуицию, не торговый сигнал.</i>"
    )


def vote_ack(r, choice: str) -> str:
    """Ответ на нажатие (всплывашка callback — до 200 символов)."""
    tgt = result_close(r)
    return (f"Принято: {CHOICE_LABEL[choice]} {spaced_price(r['ref_price'])}. "
            f"Итог {tgt:%d.%m} в {tgt:%H:%M} UTC пришлю сюда.")


def _week_summary(key: str, tg_id: int, board: list[dict], excluded: bool) -> str:
    """'<b>5</b> очков · место <b>2</b> из 14' — очки недели и место юзера."""
    if excluded:
        pts = week_points(key, tg_id)
        return f"<b>{pts}</b> {points_word(pts)} (админ — вне зачёта)"
    me = next((a for a in board if a["tg_id"] == tg_id), None)
    if me is None:
        return "пока 0 очков"
    return (f"<b>{me['points']}</b> {points_word(me['points'])} · "
            f"место <b>{me['place']}</b> из {len(board)}")


def _rank_line(key: str, tg_id: int, board: list[dict], excluded: bool) -> str:
    return "За неделю: " + _week_summary(key, tg_id, board, excluded)


def render_result(r, v, board: list[dict], excluded: bool = False) -> str:
    """Итог раунда в личку голосовавшему: исход, его очки, серия, место недели."""
    day = date.fromisoformat(r["day"])
    key = week_key(day)
    if r["outcome"] == VOID:
        return (f"🎯 <b>Раунд {day:%d.%m} аннулирован</b>\n"
                "Биржа не отдала цену итога — очки не начислены, серия не изменилась.\n"
                "Топ недели: /game top")
    ch = change_pct(r["ref_price"], r["result_price"])
    move = {UP: "⬆️ выше", DOWN: "⬇️ ниже", FLAT: "↔️ почти на месте"}[r["outcome"]]
    streak = int(v["streak_after"] or 0)
    if r["outcome"] == FLAT:
        verdict = (f"↔️ Ход меньше ±{FLAT_PCT:g}% — ничья: очков нет, серия сохраняется "
                   f"(<b>{streak}</b>).")
    elif v["correct"]:
        pts = int(v["points"] or 0)
        bonus = f" (бонус за серию {streak} 🔥)" if pts > 1 else ""
        verdict = f"✅ Угадал: +{pts} {points_word(pts)}{bonus}. Серия: <b>{streak}</b>."
    else:
        verdict = "❌ Не угадал — серия обнулилась."
    return (
        f"🎯 <b>Итог раунда {day:%d.%m}</b>\n"
        f"BTC через 24 ч: <b>{spaced_price(r['result_price'])}</b> против "
        f"{spaced_price(r['ref_price'])} на старте — {move} ({ch:+.2f}%)\n"
        f"Твой ответ: {CHOICE_LABEL[v['choice']]}\n\n"
        f"{verdict}\n"
        f"{_rank_line(key, int(v['tg_id']), board, excluded)}\n\n"
        "Топ недели: /game top · раунды — каждый день в 12:00 UTC"
    )


def render_top(key: str, board: list[dict], tg_id: int, prize_days: float,
               excluded: bool = False) -> str:
    lines = [f"🏆 <b>Топ недели {week_label(key)}</b> · Лонг или шорт?"]
    if not board:
        lines.append("Пока никто не играл — раунд каждый день в 12:00 UTC.")
    for a in board[:TOP_N]:
        me = " ← ты" if a["tg_id"] == tg_id else ""
        lines.append(f"{a['place']}. {esc(mask_name(a['username'], a['tg_id']))} — "
                     f"{a['points']} {points_word(a['points'])}{me}")
    me_row = next((a for a in board if a["tg_id"] == tg_id), None)
    lines.append("")
    if excluded:
        pts = week_points(key, tg_id)
        lines.append(f"Ты: {pts} {points_word(pts)} — админ, вне зачёта.")
    elif me_row is None:
        lines.append("Ты на этой неделе ещё не отвечал — /game")
    elif me_row["place"] > TOP_N:
        lines.append(f"Ты: место {me_row['place']} из {len(board)} · "
                     f"{me_row['points']} {points_word(me_row['points'])}")
    if prize_days > 0:
        lines.append(f"🎁 Победителю недели — +{prize_days:g} дн. полного доступа.")
    lines.append("При равенстве очков выше тот, кто в среднем раньше отвечал на угаданные "
                 "раунды. Ники скрыты — чтобы список не стал базой для спама.")
    return "\n".join(lines)


def _round_lines(tg_id: int, now: datetime) -> list[str]:
    """Состояние раундов для юзера: его ждущие итога ответы, открытое голосование или
    когда следующий раунд."""
    lines = []
    pend = {int(r["id"]): r for r in pending_votes(tg_id)}
    for r in pend.values():
        tgt = result_close(r)
        lines.append(f"✅ Раунд {date.fromisoformat(r['day']):%d.%m}: твой ответ "
                     f"{CHOICE_LABEL[r['choice']]} от {spaced_price(r['ref_price'])} — итог "
                     f"{tgt:%d.%m} в {tgt:%H:%M} UTC")
    op = open_round(now)
    if op is not None:
        if int(op["id"]) not in pend:
            lines.append(f"⏳ Сегодняшний раунд открыт до {_hhmm(op['closes_at'])} UTC — "
                         "он ниже 👇")
        return lines
    started = db.query_one("SELECT 1 FROM game_rounds WHERE day=?",
                           (now.date().isoformat(),)) is not None
    if not started and now.hour < ROUND_HOUR_UTC:
        lines.append(f"Следующий раунд — сегодня в {ROUND_HOUR_UTC}:00 UTC.")
    elif not started and now.hour < LAST_START_HOUR_UTC:
        lines.append("Сегодняшний раунд вот-вот стартует — пришлю сюда.")
    else:
        lines.append(f"Следующий раунд — завтра в {ROUND_HOUR_UTC}:00 UTC.")
    return lines


def render_status(tg_id: int, joined: str, prize_days: float, now: datetime | None = None,
                  exclude: Collection[int] = ()) -> str:
    """Экран /game. joined — результат join(): 'new' — приветствие и правила, иначе статус:
    серия, очки и место недели, ответы, ждущие итога, и что с раундом дня."""
    now = _aware(now)
    rounds = _round_lines(tg_id, now)
    if joined == "new":
        return ("✅ <b>Ты в игре!</b> Раунды буду присылать каждый день в 12:00 UTC "
                "(мьют и тихие часы /notify действуют и здесь).\n"
                + "".join(f"{x}\n" for x in rounds) + "\n" + render_rules(prize_days))
    p = player(tg_id)
    key = week_key(now.date())
    board = leaderboard(key, exclude)
    streak = int(p["streak"]) if p else 0
    best = int(p["best_streak"]) if p else 0
    lines = ["🎯 <b>Лонг или шорт?</b>" + (" — с возвращением!" if joined == "back" else ""),
             f"Серия: <b>{streak}</b> (лучшая {best}) · неделя: "
             + _week_summary(key, tg_id, board, tg_id in set(exclude)),
             *rounds, "",
             "/game top — топ недели · /game rules — правила · /game off — выйти"]
    return "\n".join(lines)


def render_week_notice(w, board: list[dict], tg_id: int, excluded: bool = False) -> str:
    """Итог недели участнику (и отдельная строка победителю)."""
    key = w["week_key"]
    lines = [f"🏆 <b>Итоги недели {week_label(key)}</b> · Лонг или шорт?"]
    for a in board[:3]:
        lines.append(f"{a['place']}. {esc(mask_name(a['username'], a['tg_id']))} — "
                     f"{a['points']} {points_word(a['points'])}")
    winner = w["winner_tg_id"]
    days = float(w["prize_days"] or 0)
    lines.append("")
    if winner is not None and int(winner) == tg_id:
        lines.append(f"🎉 <b>Ты победил!</b> {w['points']} {points_word(w['points'])} за неделю."
                     + (f" +{days:g} дн. полного доступа уже начислены — /subscribe"
                        if days > 0 else ""))
    else:
        if winner is not None and days > 0:
            lines.append(f"Победитель получает +{days:g} дн. полного доступа.")
        lines.append(_rank_line(key, tg_id, board, excluded))
    lines.append("Новая неделя — новые очки. Раунд — каждый день в 12:00 UTC.")
    return "\n".join(lines)


def chart_data(r) -> dict:
    """Данные карты раунда для graphics.render_game_chart — из chart_json, без биржи."""
    tgt = result_close(r)
    return {"day_label": f"{date.fromisoformat(r['day']):%d.%m.%Y}", "symbol": "BTC/USDT",
            "bars": [tuple(b) for b in json.loads(r["chart_json"])],
            "ref_price": float(r["ref_price"]), "result_label": f"{tgt:%d.%m %H:%M} UTC"}


# ---------------------------------------------------------------------------
# Клавиатуры (aiogram — ленивый импорт, как в bot.py)
# ---------------------------------------------------------------------------
def vote_markup(round_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=CHOICE_LABEL[UP], callback_data=f"game:v:{round_id}:{UP}"),
        InlineKeyboardButton(text=CHOICE_LABEL[DOWN], callback_data=f"game:v:{round_id}:{DOWN}"),
    ]])


def voted_markup(choice: str):
    """После голоса кнопки сменяются плашкой с ответом — видно, что выбрал, и не тянет жать."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=f"✅ Твой ответ: {CHOICE_LABEL[choice]}", callback_data="game:noop")]])


def invite_markup():
    """Кнопка к INVITE_TEXT — для онбординга/дрипа (в канале нужна ссылка ?start=game)."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🎯 Играть", callback_data="game:join")]])


def parse_callback(data: str | None) -> tuple[str, int | None, str | None]:
    """'game:v:<id>:<up|down>' -> ('vote', id, choice); 'game:join'/'game:noop' -> (вид, None,
    None); мусор -> ('', None, None). Поддельный callback_data безопасен: голос проверяется
    по БД, раунд — по id."""
    parts = (data or "").split(":")
    if len(parts) == 4 and parts[:2] == ["game", "v"] and parts[2].isdecimal() \
            and parts[3] in (UP, DOWN):
        return "vote", int(parts[2]), parts[3]
    if len(parts) == 2 and parts[0] == "game" and parts[1] in ("join", "noop"):
        return parts[1], None, None
    return "", None, None


# ---------------------------------------------------------------------------
# Рассылка (async; бот — параметром)
# ---------------------------------------------------------------------------
def is_dead_recipient(e: BaseException) -> bool:
    """Заблокировал бота / удалил чат — как bot._dead_recipient: пометить, не ретраить."""
    low = str(e).lower()
    return "blocked" in low or "chat not found" in low or "deactivated" in low


async def _call(fn: Callable[..., Awaitable[Any]], *args, **kwargs):
    """Один повтор после 429 (flood-wait) — паттерн bot._send_retry."""
    from aiogram.exceptions import TelegramRetryAfter

    try:
        return await fn(*args, **kwargs)
    except TelegramRetryAfter as e:
        await asyncio.sleep(min(float(e.retry_after), 30.0) + 1.0)
        return await fn(*args, **kwargs)


async def _deliver(kind: str, ref: str, uid: int, send: Callable[[], Awaitable[Any]],
                   on_dead: Callable[[int], Any], log, now: datetime,
                   force: bool = False) -> bool:
    """claim -> send -> sent_at. Мёртвый получатель: mark_blocked, заявка остаётся (не
    долбить); транзиентный сбой: заявка снимается — повтор следующим тактом.
    force — юзер сам попросил (/game): шлём и при уже стоящей заявке."""
    claimed = await asyncio.to_thread(claim_send, kind, ref, uid, now)
    if not claimed and not force:
        return False
    try:
        await send()
    except Exception as e:                              # noqa: BLE001 — один юзер не роняет рассылку
        log.warning("игра: %s %s -> %s: %s", kind, ref, uid, e)
        if is_dead_recipient(e):
            await asyncio.to_thread(on_dead, uid)
        elif claimed:
            await asyncio.to_thread(release_send, kind, ref, uid)
        return False
    await asyncio.to_thread(mark_sent, kind, ref, uid, now)
    return True


def _file_id(msg) -> str | None:
    photos = getattr(msg, "photo", None) or []
    return getattr(photos[-1], "file_id", None) if photos else None


async def send_round(bot, r, uids: list[int], *, render: Callable[[dict], Awaitable[bytes]],
                     prize_days: float, on_dead: Callable[[int], Any], log,
                     now: datetime | None = None, force: bool = False,
                     pause: float = 0.05) -> int:
    """Карта раунда + вопрос + кнопки. PNG рендерится один раз на раунд, дальше — file_id из
    Telegram (и после рестарта: он в game_rounds). Сбой рендера или отправки фото — откат на
    текст с теми же кнопками: раунд важнее картинки.

    force=True — юзер сам попросил раунд (/game): показать, даже если уже приходил; заявка
    отправки всё равно ставится, чтобы рассылка не прислала его второй раз."""
    from aiogram.types import BufferedInputFile

    now = _aware(now)
    caption = render_round_caption(r, prize_days)
    kb = vote_markup(int(r["id"]))
    ref = str(r["id"])
    st: dict[str, Any] = {"fid": r["photo_file_id"], "png": None, "tried": False}

    async def _photo():
        if st["fid"]:
            return st["fid"]
        if not st["tried"]:
            st["tried"] = True
            try:
                st["png"] = await render(chart_data(r))
            except Exception as e:                      # noqa: BLE001 — без картинки, не без раунда
                log.warning("игра: карта раунда %s: %s", ref, e)
        return (BufferedInputFile(st["png"], filename=f"game_{r['day']}.png")
                if st["png"] else None)

    async def _send_to(uid: int) -> None:
        photo = await _photo()
        if photo is not None:
            try:
                msg = await _call(bot.send_photo, uid, photo, caption=caption,
                                  parse_mode="HTML", reply_markup=kb)
            except Exception as e:                      # noqa: BLE001
                if is_dead_recipient(e):
                    raise
                log.warning("игра: фото раунда %s -> %s: %s — шлю текстом", ref, uid, e)
            else:
                fid = _file_id(msg)
                if fid and not st["fid"]:
                    st["fid"] = fid
                    await asyncio.to_thread(set_photo_file_id, int(r["id"]), fid)
                return
        await _call(bot.send_message, uid, caption, parse_mode="HTML", reply_markup=kb)

    sent = 0
    for uid in uids:
        if await _deliver("round", ref, uid, partial(_send_to, uid), on_dead, log, now,
                          force=force):
            sent += 1
        await asyncio.sleep(pause)
    return sent


async def send_pending(bot, *, render: Callable[[dict], Awaitable[bytes]], prize_days: float,
                       on_dead: Callable[[int], Any], log, exclude: Collection[int] = (),
                       now: datetime | None = None, pause: float = 0.05) -> dict:
    """Разослать всё, что ждёт: итоги раундов -> итоги недели -> новый раунд (в 12:00 юзер
    видит «вчера» раньше «сегодня»). Каждая отправка — одна на (вид, раунд/неделя, юзер)."""
    now = _aware(now)
    skip = set(exclude)
    out = {"result": 0, "week": 0, "round": 0}

    items = await asyncio.to_thread(result_recipients, now)
    boards: dict[str, list[dict]] = {}
    for rid, uid in items:
        r = await asyncio.to_thread(get_round, rid)
        v = await asyncio.to_thread(get_vote, rid, uid)
        if r is None or v is None:
            continue
        key = week_key(date.fromisoformat(r["day"]))
        if key not in boards:
            boards[key] = await asyncio.to_thread(leaderboard, key, skip)
        text = await asyncio.to_thread(render_result, r, v, boards[key], uid in skip)
        if await _deliver("result", str(rid), uid,
                          partial(_call, bot.send_message, uid, text, parse_mode="HTML"),
                          on_dead, log, now):
            out["result"] += 1
        await asyncio.sleep(pause)

    for wk, uid in await asyncio.to_thread(week_recipients, now):
        w = await asyncio.to_thread(db.query_one, "SELECT * FROM game_weeks WHERE week_key=?",
                                    (wk,))
        if w is None:
            continue
        if wk not in boards:
            boards[wk] = await asyncio.to_thread(leaderboard, wk, skip)
        text = await asyncio.to_thread(render_week_notice, w, boards[wk], uid, uid in skip)
        if await _deliver("week", wk, uid,
                          partial(_call, bot.send_message, uid, text, parse_mode="HTML"),
                          on_dead, log, now):
            out["week"] += 1
        await asyncio.sleep(pause)

    r = await asyncio.to_thread(open_round, now)
    if r is not None and datetime.fromisoformat(r["closes_at"]) - now >= \
            timedelta(minutes=MIN_VOTE_LEFT_MIN):
        uids = await asyncio.to_thread(round_recipients, int(r["id"]), now)
        if uids:
            out["round"] = await send_round(bot, r, uids, render=render, prize_days=prize_days,
                                            on_dead=on_dead, log=log, now=now, pause=pause)
    return out
