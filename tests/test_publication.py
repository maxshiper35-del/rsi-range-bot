"""Метка публикации сигнала и досылка после рестарта (аудит надёжности 30.10, M5)."""
from __future__ import annotations

import asyncio
import shutil
from datetime import timedelta

from app.core.signal_engine import Side, Signal

_N = iter(range(1, 10_000))


def _sig(symbol: str = "BTC/USDT:USDT", setup: str = "6226") -> Signal:
    # свой бар на каждый сигнал: UNIQUE(symbol, tf, side, created_at, exchange)
    hour = next(_N)
    return Signal(symbol=symbol, side=Side.LONG, timeframe="1h", entry=100.0, stop=90.0,
                  take_profits=[120.0], risk_reward=2.0, confidence=0.6, reasons=["тест"],
                  created_at=f"2026-10-{1 + hour // 24:02d}T{hour % 24:02d}:00:00+00:00",
                  indicators_snapshot={"setup": setup})


def _age(db, sid: int, minutes: float) -> None:
    """Сдвинуть момент записи сигнала (событие opened/confirmed) в прошлое."""
    from app import clock
    db.execute("UPDATE signal_events SET created_at=? WHERE signal_id=? "
               "AND event IN ('opened','confirmed')",
               ((clock.utcnow() - timedelta(minutes=minutes)).isoformat(), sid))


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def _add(self, level: str, msg: str, *args) -> None:
        self.lines.append((level, msg % args if args else msg))

    def error(self, msg, *args):
        self._add("error", msg, *args)

    def warning(self, msg, *args):
        self._add("warning", msg, *args)

    def exception(self, msg, *args):
        self._add("exception", msg, *args)


def test_migration_marks_existing_signals_published(tmp_path, monkeypatch):
    """Сигналы, записанные до миграции 029, досылке не подлежат — иначе первый старт после
    выката разослал бы все открытые сделки заново."""
    import app.db as db
    from app.brain import journal
    from app.config import get_settings
    from app.delivery import publication
    from tests.conftest import MIGRATIONS, _reset_conn

    monkeypatch.setenv("DB_PATH", str(tmp_path / "m.db"))
    get_settings.cache_clear()
    _reset_conn(db)
    before = tmp_path / "migrations"
    before.mkdir()
    for p in sorted(MIGRATIONS.glob("*.sql")):
        if p.name < "029":
            shutil.copy(p, before / p.name)
    db.init_db(before)
    old = journal.record_signal(_sig())
    shutil.copy(MIGRATIONS / "029_signal_publication.sql", before)
    db.init_db(before)
    row = db.query_one("SELECT * FROM signal_publication WHERE signal_id=?", (old,))
    assert row["done_at"] is not None and row["note"] == "до миграции 029"
    from app import clock
    assert publication.startup_plan(clock.utcnow()) == []
    new = journal.record_signal(_sig("ETH/USDT:USDT"))
    assert [p["id"] for p in publication.startup_plan(clock.utcnow())] == [new]
    _reset_conn(db)
    get_settings.cache_clear()


def test_claim_is_exclusive(fresh_db):
    """Поллер и досылка при старте не доставят один сигнал дважды: захват — один."""
    from app.brain import journal
    from app.delivery import publication
    sid = journal.record_signal(_sig())
    assert publication.claim(sid) is True
    assert publication.claim(sid) is False


def test_claim_resume_only_once_and_only_unfinished(fresh_db):
    from app.brain import journal
    from app.delivery import publication
    a = journal.record_signal(_sig())
    b = journal.record_signal(_sig("ETH/USDT:USDT"))
    publication.claim(a)
    publication.claim(b)
    publication.mark_done(b)
    assert publication.claim_resume(a) is True
    assert publication.claim_resume(a) is False         # прерванная досылка второй раз не идёт
    assert publication.claim_resume(b) is False         # доставка завершена


def test_channel_step():
    from app.delivery.publication import channel_step
    assert channel_step(None, True) == "post"
    assert channel_step(None, False) == "off"
    assert channel_step("sending", True) == "unknown"   # обрыв на отправке: не повторять
    for done in ("sent", "failed", "off", "unknown"):
        assert channel_step(done, True) == "keep"


def test_startup_plan_classifies(fresh_db):
    from app import clock
    from app.brain import journal
    from app.delivery import publication

    fresh = journal.record_signal(_sig("A/USDT:USDT"))              # доставка не начиналась
    midway = journal.record_signal(_sig("B/USDT:USDT"))             # канал есть, личка прервана
    publication.claim(midway)
    publication.set_channel(midway, "sent", 77)
    done = journal.record_signal(_sig("C/USDT:USDT"))
    publication.claim(done)
    publication.mark_done(done)
    stale = journal.record_signal(_sig("D/USDT:USDT"))
    _age(fresh_db, stale, 120)
    closed = journal.record_signal(_sig("E/USDT:USDT"))
    fresh_db.execute("UPDATE signals SET status='stopped', result_r=-1 WHERE id=?", (closed,))
    journal.record_shadow(_sig("F/USDT:USDT"))                      # тень не публикуется
    journal.record_candidate(_sig("G/USDT:USDT"))                   # кандидат без /confirm
    confirmed = journal.record_candidate(_sig("H/USDT:USDT"))
    journal.confirm_candidate(confirmed)
    resumed = journal.record_signal(_sig("I/USDT:USDT"))
    publication.claim(resumed)
    publication.claim_resume(resumed)

    plan = {p["id"]: p for p in publication.startup_plan(clock.utcnow())}
    assert set(plan) == {fresh, midway, stale, closed, confirmed, resumed}
    assert plan[fresh]["action"] == "deliver"
    assert plan[confirmed]["action"] == "deliver"
    assert plan[midway]["action"] == "resume" and plan[midway]["channel"] == "sent"
    assert plan[stale]["action"] == "skip" and "120 мин" in plan[stale]["reason"]
    assert plan[closed]["action"] == "skip" and "stopped" in plan[closed]["reason"]
    assert plan[resumed]["action"] == "skip"


def test_close_skipped_is_final_and_marks_unknown_post(fresh_db):
    from app import clock
    from app.brain import journal
    from app.delivery import publication
    never = journal.record_signal(_sig())
    cut = journal.record_signal(_sig("ETH/USDT:USDT"))
    publication.claim(cut)
    publication.set_channel(cut, "sending")
    publication.close_skipped(never, "устарел")
    publication.close_skipped(cut, "устарел")
    rows = {r["signal_id"]: r for r in fresh_db.query("SELECT * FROM signal_publication")}
    assert rows[never]["channel"] == "skipped" and rows[never]["done_at"]
    assert rows[cut]["channel"] == "unknown" and rows[cut]["done_at"]
    assert publication.startup_plan(clock.utcnow()) == []


def test_redeliver_on_start_delivers_fresh_and_reports_stale(fresh_db):
    """Старт: свежие недоставленные — в доставку (resume — для прерванных), устаревшие —
    закрыть меткой и сообщить админам ERROR; сбой одной доставки не мешает остальным."""
    from app.brain import journal
    from app.delivery import publication

    a = journal.record_signal(_sig("A/USDT:USDT"))
    b = journal.record_signal(_sig("B/USDT:USDT"))
    publication.claim(b)
    publication.set_channel(b, "sending")                           # обрыв на отправке поста
    boom = journal.record_signal(_sig("C/USDT:USDT"))
    old = journal.record_signal(_sig("D/USDT:USDT"))
    _age(fresh_db, old, 90)
    calls: list[tuple[int, bool]] = []

    async def deliver(sid: int, resume: bool) -> None:
        calls.append((sid, resume))
        if sid == boom:
            raise RuntimeError("telegram недоступен")

    log = _Log()
    asyncio.run(publication.redeliver_on_start(deliver, log))
    assert calls == [(a, False), (b, True), (boom, False)]
    errors = [msg for level, msg in log.lines if level == "error"]
    assert len(errors) == 1 and f"#{old}" in errors[0] and "90 мин" in errors[0]
    assert any(level == "exception" and f"#{boom}" in msg for level, msg in log.lines)
    # устаревший закрыт: следующий старт о нём не напоминает
    row = fresh_db.query_one("SELECT * FROM signal_publication WHERE signal_id=?", (old,))
    assert row["done_at"] is not None and row["channel"] == "skipped"
    again = asyncio.run(publication.redeliver_on_start(deliver, _Log()))
    assert old not in {p["id"] for p in again}
