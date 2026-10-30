"""Переборки такта обслуживания и фоновые задачи (аудит кода 30.10, §3.6 п.2, 4, 8)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_failing_early_block_does_not_stop_expiry(fresh_db):
    """Устойчиво падающий ранний блок (битая метка, баг в предикате) больше не выключает
    истечение подписок — конец триала 02–03.11 уходит при любом сбое алертов."""
    from app.delivery import bot, payments
    payments.grant_trial(2001, 5)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    fresh_db.execute("UPDATE users SET paid_until=? WHERE tg_id=2001", (past,))
    expired: list[int] = []

    async def broken():
        raise RuntimeError("битая метка в settings")

    async def expire():
        expired.extend(await asyncio.to_thread(payments.expire_overdue))

    alerts: list[str] = []

    async def alert(text):
        alerts.append(text)

    clock = _Clock()
    guard = bot.MaintenanceGuard(alert, alert_every_s=3600, clock=clock)
    blocks = [("бэкап", broken), ("истечение подписок", expire)]
    for _ in range(3):                                   # три такта подряд, сбой каждый раз
        assert asyncio.run(guard.run(blocks)) == ["бэкап"]
        clock.t += 60
    assert expired == [2001]                             # истекло с первого же такта
    assert payments.get_user(2001)["tier"] == "free"
    assert len(alerts) == 1 and "«бэкап»" in alerts[0]   # дроссель: один алерт на окно
    clock.t += 3600
    asyncio.run(guard.run(blocks))
    assert len(alerts) == 2                              # окно прошло — напомнили снова


def test_guard_survives_failing_alert():
    from app.delivery import bot

    async def broken():
        raise ValueError("сбой блока")

    ran: list[str] = []

    async def ok():
        ran.append("ok")

    async def alert_down(_text):
        raise RuntimeError("Telegram недоступен")

    guard = bot.MaintenanceGuard(alert_down)
    assert asyncio.run(guard.run([("a", broken), ("b", ok)])) == ["a"]
    assert ran == ["ok"]


def test_spawn_keeps_reference_and_logs_failure(caplog):
    from app.delivery import bot

    async def boom():
        await asyncio.sleep(0)
        raise RuntimeError("рассылка упала")

    async def main():
        task = bot.spawn(boom(), name="digest-test")
        assert task in bot._BG_TASKS                     # сильная ссылка, пока работает
        await asyncio.sleep(0.01)
        return task

    with caplog.at_level(logging.ERROR, logger="bot"):
        task = asyncio.run(main())
    assert task not in bot._BG_TASKS
    assert "digest-test" in caplog.text and "рассылка упала" in caplog.text


class _AdminBot:
    def __init__(self, dead: set[int]):
        self.sent: list[tuple] = []
        self.dead = dead

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.dead:
            raise RuntimeError("chat not found")
        self.sent.append((chat_id, text, kw))


def test_notify_admins_isolates_and_logs(caplog):
    from app.delivery import bot
    b = _AdminBot(dead={2})
    with caplog.at_level(logging.WARNING, logger="bot"):
        n = asyncio.run(bot.notify_admins(b, (1, 2, 3), "отчёт", what="отчёт executor",
                                          parse_mode="HTML"))
    assert n == 2 and [s[0] for s in b.sent] == [1, 3]
    assert b.sent[0][2] == {"parse_mode": "HTML"}
    assert "отчёт executor" in caplog.text               # не молчаливый pass
    asyncio.run(bot.notify_admins(b, (1,), "без разметки", what="x"))
    assert b.sent[-1][2] == {}                           # parse_mode не передаём без нужды


def test_send_raw_alerts_isolates_admins(monkeypatch):
    from app.delivery import bot
    sent: list[int] = []

    def fake_send(token, chat_id, text):
        if chat_id == 2:
            raise OSError("timeout")
        sent.append(chat_id)

    monkeypatch.setattr(bot, "_send_raw_telegram_message", fake_send)
    bot._send_raw_alerts("tok", (1, 2, 3), "таска умерла")
    assert sent == [1, 3]
