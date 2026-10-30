"""publication.py — метка «опубликован» у сигнала и досылка недоставленных после рестарта.

ЗАЧЕМ (аудит надёжности 30.10.2026, M5). poll_once работает в потоке: SIGTERM в первые ~90 с
часа отменяет корутину поллера, но не поток. Сигнал успевает записаться в журнал, executor его
исполнит, а в канал и в личку он не уходит, и следующий такт его не повторит (дедуп,
decision_is_fresh): сделка есть в публичной статистике, но её не видел никто. Таблица
signal_publication (миграция 029) фиксирует, докуда дошла доставка; при старте бот досылает
свежие недоставленные.

БЕЗ ДУБЛЕЙ:
- доставку начинает тот, кто захватил строку (INSERT OR IGNORE), — поллер и досылка при старте
  не пошлют один сигнал дважды;
- метка канала ставится ДО отправки поста ('sending') и ПОСЛЕ ('sent'). Обрыв на самой отправке
  — исход неизвестен: повторно не шлём, админам ERROR. Лишний пост в публичном канале хуже
  ручной проверки, а молчаливой потери нет — о ней сообщено;
- личка дедупится по deliveries (payments.plan_recipients): досылка дойдёт только до тех, кто
  ещё не получил;
- досылка при старте — одна на сигнал (resumed_at): прерванная досылка второй раз не идёт.

Досылаем, только пока сигнал актуален: open без исхода (вход не отменён и не истёк) и записан не
раньше REDELIVERY_WINDOW_MIN минут назад, то есть до следующего часового такта. Прочие
недоставленные закрываются меткой skipped, а админам уходит ERROR: в журнале сделка есть, а
подписчики её могли не видеть.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from app import clock, db

REDELIVERY_WINDOW_MIN = 50.0
# сигнал стал публичным: запись в auto-режиме ('opened') или /confirm кандидата ('confirmed');
# тени ('shadow') и неподтверждённые кандидаты не публикуются и сюда не попадают
_PUBLIC_EVENTS = "('opened','confirmed')"
_CHANNEL_HUMAN = {None: "поста не было", "sending": "пост мог уйти (обрыв на отправке)",
                  "sent": "пост есть", "failed": "пост не ушёл", "off": "канал выключен"}


def claim(signal_id: int) -> bool:
    """Захватить доставку сигнала. -> True, если захватили мы (строки ещё не было)."""
    with db.transaction() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO signal_publication(signal_id, claimed_at) "
                           "VALUES (?, ?)", (signal_id, clock.utcnow_iso()))
        return cur.rowcount == 1


def claim_resume(signal_id: int) -> bool:
    """Захватить досылку прерванной доставки — одна на сигнал. -> True, если она наша."""
    return db.execute("UPDATE signal_publication SET resumed_at=? WHERE signal_id=? "
                      "AND done_at IS NULL AND resumed_at IS NULL",
                      (clock.utcnow_iso(), signal_id)) == 1


def channel_state(signal_id: int) -> str | None:
    """Метка поста в канале (None — строки нет или до поста не дошли)."""
    row = db.query_one("SELECT channel FROM signal_publication WHERE signal_id=?", (signal_id,))
    return row["channel"] if row is not None else None


def set_channel(signal_id: int, state: str, msg_id: int | None = None,
                note: str | None = None) -> None:
    db.execute("UPDATE signal_publication SET channel=?, "
               "channel_msg_id=COALESCE(?, channel_msg_id), note=COALESCE(?, note) "
               "WHERE signal_id=?", (state, msg_id, note, signal_id))


def channel_step(state: str | None, channel_on: bool) -> str:
    """Что делать с постом в канал по метке: post — слать; off — канал выключен; unknown —
    прошлый процесс оборвался на самой отправке (пост мог уйти: не слать, сообщить админам);
    keep — канал уже решён (sent/failed/off/unknown), досылается только личка."""
    if state == "sending":
        return "unknown"
    if state is not None:
        return "keep"
    return "post" if channel_on else "off"


def mark_done(signal_id: int) -> None:
    """Доставка (канал + личка) завершена."""
    db.execute("UPDATE signal_publication SET done_at=? WHERE signal_id=? AND done_at IS NULL",
               (clock.utcnow_iso(), signal_id))


def close_skipped(signal_id: int, reason: str) -> None:
    """Досылка не нужна (устарел, закрыт, досылка уже прерывалась): закрыть метку, чтобы
    следующий старт не сообщал о том же сигнале снова. Обрыв на отправке поста -> unknown."""
    now = clock.utcnow_iso()
    with db.transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO signal_publication(signal_id, claimed_at, channel) "
                     "VALUES (?, ?, 'skipped')", (signal_id, now))
        conn.execute("UPDATE signal_publication SET done_at=?, note=?, "
                     "channel=CASE WHEN channel='sending' THEN 'unknown' ELSE channel END "
                     "WHERE signal_id=? AND done_at IS NULL", (now, reason, signal_id))


def _utc(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def startup_plan(now: datetime, window_min: float = REDELIVERY_WINDOW_MIN) -> list[dict]:
    """Недоставленные публичные сигналы -> [{id, symbol, action, channel, reason}] по id.

    action: deliver — доставка не начиналась (пост точно не уходил); resume — прервалась на
    середине (канал — по метке, личка — тем, кто не получил); skip — досылать поздно или нечего.
    Возраст — от записи сигнала (событие opened/confirmed), не от бара: created_at сигнала —
    открытие решающего бара, на час раньше."""
    rows = db.query(
        "SELECT s.id, s.symbol, s.status, s.result_r, p.signal_id AS claimed, p.channel, "
        "p.resumed_at, (SELECT MAX(e.created_at) FROM signal_events e "
        f"WHERE e.signal_id = s.id AND e.event IN {_PUBLIC_EVENTS}) AS born_at "
        "FROM signals s LEFT JOIN signal_publication p ON p.signal_id = s.id "
        "WHERE (p.signal_id IS NULL OR p.done_at IS NULL) AND EXISTS (SELECT 1 FROM "
        f"signal_events e WHERE e.signal_id = s.id AND e.event IN {_PUBLIC_EVENTS}) "
        "ORDER BY s.id")
    out = []
    for r in rows:
        born = _utc(r["born_at"])
        age_min = (now - born).total_seconds() / 60.0 if born is not None else float("inf")
        if r["resumed_at"] is not None:
            action, reason = "skip", "досылка уже была и прервалась"
        elif r["status"] != "open" or r["result_r"] is not None:
            action, reason = "skip", f"сигнал уже не актуален ({r['status']})"
        elif age_min > window_min:
            action, reason = "skip", f"записан {age_min:.0f} мин назад (окно {window_min:.0f})"
        else:
            action, reason = ("deliver" if r["claimed"] is None else "resume"), ""
        out.append(dict(id=r["id"], symbol=r["symbol"], action=action, channel=r["channel"],
                        reason=reason))
    return out


async def redeliver_on_start(deliver: Callable[[int, bool], Awaitable[None]], log,
                             now: datetime | None = None) -> list[dict]:
    """Досылка при старте процесса — до поллера и стрима, чтобы пост сигнала ушёл раньше
    «вход исполнен» по нему. deliver(signal_id, resume) — доставка бота. Сбой одного сигнала
    не мешает остальным. -> план (для лога и тестов)."""
    plan = await asyncio.to_thread(startup_plan, now or clock.utcnow())
    for item in plan:
        sid = item["id"]
        if item["action"] == "skip":
            await asyncio.to_thread(close_skipped, sid, item["reason"])
            log.error("сигнал #%s %s не доставлен до рестарта, досылать не буду: %s; канал: %s. "
                      "В журнале сделка есть, а подписчики её могли не видеть", sid,
                      item["symbol"], item["reason"],
                      _CHANNEL_HUMAN.get(item["channel"], item["channel"]))
            continue
        log.warning("сигнал #%s %s не доставлен до рестарта — досылаю (%s)", sid, item["symbol"],
                    "с начала" if item["action"] == "deliver" else "продолжение")
        try:
            await deliver(sid, item["action"] == "resume")
        except Exception:                               # noqa: BLE001 — один сигнал не роняет остальные
            log.exception("досылка #%s", sid)
    return plan
