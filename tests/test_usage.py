"""Счётчик использования (10.11.2026): кто какой раздел бота открывал. Нужен, чтобы после
перевода бота на «проверку и обучение» видеть, пользуются ли люди проверкой. Пишется только
название действия; счётчик не должен мешать хэндлерам."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.delivery import bot, usage

MENU = frozenset({"🔎 Проверить сигнал", "📊 Статистика"})


def test_message_action_names_only():
    """Команда — без аргументов и без @бота, кнопка меню — своим текстом, пересылка и
    свободный текст — общими названиями: содержимое сообщений в счётчик не попадает."""
    assert usage.message_action("/audit @some_channel", False, MENU) == "/audit"
    assert usage.message_action("/Start ref123", False, MENU) == "/start"
    assert usage.message_action("/check@rsirange_bot", False, MENU) == "/check"
    assert usage.message_action("🔎 Проверить сигнал", False, MENU) == "🔎 Проверить сигнал"
    assert usage.message_action("BTC лонг 65000 стоп 64000", False, MENU) == usage.TEXT
    assert usage.message_action("что угодно", True, MENU) == usage.FORWARD
    assert usage.message_action(None, True, MENU) == usage.FORWARD      # фото с подписью
    assert usage.message_action("/что-то <b>странное</b>", False, MENU) == usage.OTHER_COMMAND
    assert usage.message_action(None, False, MENU) is None              # оплата, стикер
    assert usage.message_action("   ", False, MENU) is None


def test_callback_action_takes_prefix():
    assert usage.callback_action("unlock:123") == "кнопка: unlock"
    assert usage.callback_action("game:vote:up") == "кнопка: game"
    assert usage.callback_action("tariffs") == "кнопка: tariffs"
    assert usage.callback_action("") is None and usage.callback_action(None) is None
    assert usage.callback_action(":123") is None


def test_summary_counts_people_and_excludes_admins(fresh_db):
    """Итог: людей и действий всего и по каждому действию; админы не в счёт; старое — за
    границей окна."""
    assert "Пока пусто" in usage.render()
    for uid, action in ((1, "/check"), (1, "/check"), (2, "/check"), (2, "/rating"),
                        (99, "/usage"), (99, "/check")):
        assert usage.record(uid, action) is True
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    fresh_db.execute("INSERT INTO usage_events (tg_id, action, created_at) VALUES (3,'/stats',?)",
                     (old,))
    day = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    s = usage.summary(day, exclude=(99,))
    assert s == {"people": 2, "events": 4, "actions": [("/check", 2, 3), ("/rating", 1, 1)]}
    assert usage.summary(day)["people"] == 3                    # без исключений — с админом
    text = usage.render(exclude=(99,))
    assert "За 24 часа: людей 2, действий 4" in text
    assert "• /check — 2 чел. (3)" in text
    assert "За 7 дней: людей 3, действий 5" in text and "• /stats — 1 чел. (1)" in text
    assert "/usage" not in text                                 # действие админа не показано
    assert "<" not in text                                      # уходит без разметки


def test_record_once_per_window_and_truncates(fresh_db):
    """Мини-приложение опрашивает /api/me раз в минуту — это один заход, а не десятки."""
    assert usage.record(5, usage.WEBAPP_OPEN, once_minutes=30) is True
    assert usage.record(5, usage.WEBAPP_OPEN, once_minutes=30) is False
    assert usage.record(6, usage.WEBAPP_OPEN, once_minutes=30) is True      # другой человек
    fresh_db.execute("UPDATE usage_events SET created_at=? WHERE tg_id=5",
                     ((datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat(),))
    assert usage.record(5, usage.WEBAPP_OPEN, once_minutes=30) is True      # окно прошло
    usage.record(7, "x" * 200)
    row = fresh_db.query_one("SELECT action FROM usage_events WHERE tg_id=7")
    assert len(row["action"]) == usage.MAX_ACTION


def _msg(text, uid=42, chat_type="private", forward=None, is_bot=False):
    return SimpleNamespace(text=text, forward_origin=forward,
                           from_user=SimpleNamespace(id=uid, is_bot=is_bot),
                           chat=SimpleNamespace(type=chat_type))


def _run(mw, event):
    seen = []

    async def handler(ev, data):
        seen.append(ev)
        return "ok"

    assert asyncio.run(mw(handler, event, {})) == "ok"
    assert seen == [event]                                      # хэндлер вызван всегда


def test_middleware_records_private_messages_and_callbacks(fresh_db):
    mw = bot.usage_recorder(MENU, "message")
    _run(mw, _msg("🔎 Проверить сигнал"))
    _run(mw, _msg("/audit @chan"))
    _run(mw, _msg("пост из канала", forward=object()))
    _run(mw, _msg("/stats", chat_type="supergroup"))            # не личка — не считаем
    _run(mw, _msg("/stats", is_bot=True))                       # бот — не человек
    _run(mw, SimpleNamespace(text="/stats", forward_origin=None, from_user=None,
                             chat=SimpleNamespace(type="private")))
    cb = bot.usage_recorder(MENU, "callback")
    _run(cb, SimpleNamespace(data="check_demo:1", from_user=SimpleNamespace(id=42, is_bot=False)))
    rows = fresh_db.query("SELECT tg_id, action FROM usage_events ORDER BY id")
    assert [(r["tg_id"], r["action"]) for r in rows] == [
        (42, "🔎 Проверить сигнал"), (42, "/audit"), (42, usage.FORWARD),
        (42, "кнопка: check_demo")]


def test_middleware_never_breaks_handler(fresh_db, monkeypatch, caplog):
    """Сбой записи (нет таблицы, БД занята) не мешает ответу бота; предупреждение — одно."""
    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(usage, "record", boom)
    mw = bot.usage_recorder(MENU, "message")
    with caplog.at_level("WARNING", logger="bot"):
        _run(mw, _msg("/stats"))
        _run(mw, _msg("/stats"))
    assert [r.getMessage() for r in caplog.records
            if "usage" in r.getMessage()] == ["usage: запись не удалась: database is locked"]


def test_usage_is_documented_admin_command():
    names = [c for c, _ in bot.ADMIN_COMMANDS]
    assert "usage" in names and "usage" not in [c for c, _ in bot.USER_COMMANDS]
    assert "/usage" in bot.format_help(True, free_for_all=True)
    assert "/usage" not in bot.format_help(False, free_for_all=True)


class _ProfileBot:
    def __init__(self):
        self.descriptions = []

    async def set_my_description(self, description):
        self.descriptions.append(description)


def test_profile_description_not_overwritten_when_signals_free():
    """В режиме «сигналы бесплатны всем» бот не трогает описание профиля: в коде стоит
    «Триал при старте», а новый текст владелец задаёт в BotFather — запуск его не затирает."""
    b = _ProfileBot()
    asyncio.run(bot.register_bot_profile(b, free_for_all=True))
    assert b.descriptions == []
    asyncio.run(bot.register_bot_profile(b))
    assert b.descriptions == [bot.BOT_DESCRIPTION]
