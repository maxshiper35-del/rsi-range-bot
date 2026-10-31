"""Рассылка опроса «как вам бот» всем пользователям (31.10.2026, app/delivery/feedback.py).

Ответы принимает работающий бот (кнопки fb:* и текст, миграция 031) — сначала выкатить бота,
потом слать. Шлёт и тем, у кого is_active=0: заблокировавшему бота Telegram не доставит
(403 — пишем 'blocked'), а разблокировавший получит и снова станет активным. Админам опрос
не шлётся (кроме --test). Повторный запуск пропускает тех, кому уже доставлено.

    .venv/bin/python scripts/send_survey.py --dry        # сколько получателей, текст опроса
    .venv/bin/python scripts/send_survey.py --test       # только админам — проверить вид
    .venv/bin/python scripts/send_survey.py --send       # всем

Токен и админы — из .env (значения не печатаются). Темп — не больше 20 сообщений в секунду,
429 пережидается.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dotenv  # noqa: E402

dotenv.load_dotenv(ROOT / ".env")

from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.delivery import feedback  # noqa: E402

PAUSE_S = 0.05


def recipients(admins: set[int], *, test: bool) -> list[int]:
    if test:
        return sorted(admins)
    done = feedback.sent_ids()
    rows = db.query("SELECT tg_id FROM users ORDER BY joined_at")
    return [r["tg_id"] for r in rows if r["tg_id"] not in admins and r["tg_id"] not in done]


async def send(ids: list[int], *, test: bool) -> dict:
    from aiogram import Bot
    from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in feedback.keyboard_rows()])
    bot = Bot(get_settings().tg_bot_token)
    out = {"sent": 0, "blocked": 0, "failed": 0, "reactivated": 0}
    try:
        for i, uid in enumerate(ids, 1):
            status, err = "failed", "429 трижды подряд"
            for attempt in range(3):
                try:
                    await bot.send_message(uid, feedback.SURVEY_TEXT, parse_mode="HTML",
                                           reply_markup=kb)
                    status, err = "sent", None
                    break
                except TelegramRetryAfter as e:
                    await asyncio.sleep(min(float(e.retry_after), 60.0) + 1.0)
                except TelegramForbiddenError as e:
                    status, err = "blocked", str(e)[:200]
                    break
                except Exception as e:                  # noqa: BLE001 — один юзер не роняет рассылку
                    status, err = "failed", f"{type(e).__name__}: {str(e)[:180]}"
                    if attempt == 2 or "chat not found" in str(e).lower():
                        break
                    await asyncio.sleep(2.0)
            out[status] += 1
            if not test:
                feedback.record_send(uid, status, err)
                if status == "blocked" or (status == "failed" and "chat not found" in (err or "")):
                    db.execute("UPDATE users SET is_active=0 WHERE tg_id=?", (uid,))
                elif status == "sent" and db.execute(
                        "UPDATE users SET is_active=1 WHERE tg_id=? AND is_active=0", (uid,)):
                    out["reactivated"] += 1
            if i % 20 == 0:
                print(f"… {i}/{len(ids)}: {out}", flush=True)
            await asyncio.sleep(PAUSE_S)
    finally:
        await bot.session.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry", action="store_true")
    mode.add_argument("--test", action="store_true")
    mode.add_argument("--send", action="store_true")
    a = ap.parse_args()
    admins = set(get_settings().admin_ids)
    ids = recipients(admins, test=a.test)
    if a.dry:
        inactive = sum(1 for r in db.query("SELECT tg_id FROM users WHERE is_active=0")
                       if r["tg_id"] in set(ids))
        print(f"получателей: {len(ids)} (из них is_active=0 сейчас: {inactive}); "
              f"админов: {len(admins)}; уже доставлено: {len(feedback.sent_ids())}")
        print("---\n" + feedback.SURVEY_TEXT + "\n---")
        print("кнопки:", feedback.keyboard_rows())
        return
    print(f"шлю {'админам' if a.test else 'всем'}: {len(ids)}", flush=True)
    print("итог:", asyncio.run(send(ids, test=a.test)))


if __name__ == "__main__":
    main()
