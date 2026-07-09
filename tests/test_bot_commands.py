"""Тесты меню команд Telegram: register_bot_commands вызывает set_my_commands корректно."""
from __future__ import annotations

import asyncio


class _FakeBot:
    def __init__(self, fail_admin_ids=frozenset()):
        self.calls = []
        self._fail = fail_admin_ids

    async def set_my_commands(self, commands, scope=None):
        chat_id = getattr(scope, "chat_id", None)
        if chat_id in self._fail:
            raise RuntimeError("chat not found")
        self.calls.append((chat_id, [c.command for c in commands]))


def test_register_bot_commands_default_and_admin_scopes():
    from app.delivery.bot import ADMIN_COMMANDS, USER_COMMANDS, register_bot_commands

    bot = _FakeBot()
    asyncio.run(register_bot_commands(bot, (111, 222)))

    default_calls = [c for c in bot.calls if c[0] is None]
    assert len(default_calls) == 1
    assert default_calls[0][1] == [c for c, _ in USER_COMMANDS]

    admin_calls = {chat_id: cmds for chat_id, cmds in bot.calls if chat_id is not None}
    assert set(admin_calls) == {111, 222}
    assert admin_calls[111] == [c for c, _ in ADMIN_COMMANDS]
    # админский список — надмножество пользовательского (включает всё то же самое)
    assert set(c for c, _ in USER_COMMANDS) <= set(c for c, _ in ADMIN_COMMANDS)
    assert "setups" in dict(ADMIN_COMMANDS)


def test_register_bot_commands_admin_failure_does_not_raise():
    from app.delivery.bot import register_bot_commands

    bot = _FakeBot(fail_admin_ids={222})
    asyncio.run(register_bot_commands(bot, (111, 222)))   # не должно бросить исключение
    admin_calls = {chat_id for chat_id, _ in bot.calls if chat_id is not None}
    assert admin_calls == {111}                            # 222 упал, 111 всё же зарегистрирован


def test_no_duplicate_commands():
    from app.delivery.bot import ADMIN_COMMANDS, USER_COMMANDS

    for name, cmds in (("USER_COMMANDS", USER_COMMANDS), ("ADMIN_COMMANDS", ADMIN_COMMANDS)):
        names = [c for c, _ in cmds]
        assert len(names) == len(set(names)), f"дубли в {name}: {names}"
