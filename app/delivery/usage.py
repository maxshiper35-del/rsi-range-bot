"""Счётчик использования бота (10.11.2026).

После перевода бота на «проверку и обучение» главный вопрос — пользуются ли люди проверкой
сигналов и каналов. Раньше ответить было нечем: в БД попадали только оплаты, голоса игры и
отметки «взял сделку». Здесь — одна строка на действие: кто, что, когда (usage_events,
миграция 032).

Пишется ТОЛЬКО название действия: команда без аргументов, текст кнопки меню, префикс
инлайн-кнопки, «пересылка», «ввод текста», «картинка», открытие мини-приложения.
Содержимое сообщений не сохраняется.
"""
from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from datetime import datetime, timedelta, timezone

from app import clock, db

FORWARD = "пересылка (проверка сигнала)"
TEXT = "ввод текста"
OTHER_COMMAND = "/другая команда"
WEBAPP_OPEN = "мини-приложение"
IMAGE = "картинка (скриншот)"
WEBAPP_ONCE_MIN = 30.0            # страница опрашивает /api/me раз в минуту — это один заход
MAX_ACTION = 64
TOP = 12

_CMD_RE = re.compile(r"^/([a-z0-9_]{1,32})(?:@[a-z0-9_]{1,64})?$")
# метка источника в ссылке t.me/<бот>?start=src_<метка> — наш собственный идентификатор
# (src_check_yt, src_share…), не текст человека; реферальные ref_<id> сюда не попадают
_SRC_RE = re.compile(r"^src_([a-z0-9_]{1,32})$")
_CB_RE = re.compile(r"^[A-Za-z0-9_\-]{1,32}")


def message_action(text: str | None, is_forward: bool,
                   menu_texts: Collection[str], is_image: bool = False) -> str | None:
    """Название действия для сообщения в личке либо None (нечего считать: фото, оплата…).
    У команды берётся только имя: «/audit @канал» -> «/audit». Исключение — метка источника
    у /start: «/start src_check_yt» -> «/start (check_yt)». По ней видно, с какой площадки
    человек пришёл по ссылке, — в том числе тот, кто в боте уже был (users.acquisition_source
    хранит только первое касание)."""
    if is_forward:
        return FORWARD
    if is_image:
        return IMAGE
    t = (text or "").strip()
    if not t:
        return None
    if t in menu_texts:
        return t
    if t.startswith("/"):
        parts = t.lower().split()
        m = _CMD_RE.match(parts[0])
        if m is None:
            return OTHER_COMMAND
        src = _SRC_RE.match(parts[1]) if m.group(1) == "start" and len(parts) > 1 else None
        return f"/start ({src.group(1)})" if src else f"/{m.group(1)}"
    return TEXT


def callback_action(data: str | None) -> str | None:
    """Название действия для инлайн-кнопки: префикс до «:» («unlock:123» -> «кнопка: unlock»)."""
    m = _CB_RE.match(data or "")
    return f"кнопка: {m.group(0)}" if m else None


def record(tg_id: int, action: str, once_minutes: float = 0.0) -> bool:
    """Записать действие. once_minutes > 0 — не чаще раза за это окно на человека и действие
    (повторные опросы страницы — не новые заходы). -> True, если строка добавлена."""
    action = action[:MAX_ACTION]
    now = clock.utcnow_iso()
    if once_minutes > 0:
        since = (datetime.now(timezone.utc) - timedelta(minutes=once_minutes)).isoformat()
        seen = db.query_one(
            "SELECT 1 FROM usage_events WHERE tg_id=? AND action=? AND created_at >= ? LIMIT 1",
            (int(tg_id), action, since))
        if seen is not None:
            return False
    db.execute("INSERT INTO usage_events (tg_id, action, created_at) VALUES (?,?,?)",
               (int(tg_id), action, now))
    return True


def summary(since_iso: str, exclude: Iterable[int] = ()) -> dict:
    """Итог с момента since_iso без людей из exclude (админы): сколько людей и действий
    всего и по каждому действию — [(действие, людей, раз)], сначала самые массовые."""
    ex = tuple(int(x) for x in exclude)
    cond = f" AND tg_id NOT IN ({','.join('?' * len(ex))})" if ex else ""
    total = db.query_one(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT tg_id) AS people FROM usage_events "
        "WHERE created_at >= ?" + cond, (since_iso, *ex))
    rows = db.query(
        "SELECT action, COUNT(*) AS n, COUNT(DISTINCT tg_id) AS people FROM usage_events "
        "WHERE created_at >= ?" + cond + " GROUP BY action ORDER BY people DESC, n DESC, action",
        (since_iso, *ex))
    return {"people": int(total["people"]) if total else 0,
            "events": int(total["n"]) if total else 0,
            "actions": [(r["action"], int(r["people"]), int(r["n"])) for r in rows]}


def _block(title: str, s: dict) -> list[str]:
    lines = [f"{title}: людей {s['people']}, действий {s['events']}"]
    for action, people, n in s["actions"][:TOP]:
        lines.append(f"• {action} — {people} чел. ({n})")
    rest = len(s["actions"]) - TOP
    if rest > 0:
        lines.append(f"…и ещё действий: {rest}")
    return lines


def render(exclude: Iterable[int] = (), now: datetime | None = None) -> str:
    """Текст для /usage: сутки и неделя, без админов. Простой текст, без разметки."""
    now = now or datetime.now(timezone.utc)
    ex = tuple(exclude)
    first = db.query_one("SELECT MIN(created_at) AS t FROM usage_events")
    if first is None or not first["t"]:
        return ("📈 Кто чем пользуется\n\nПока пусто: счётчик только что включён. "
                "Пишется название действия (команда, кнопка), без текста сообщений.")
    lines = ["📈 Кто чем пользуется (без админов)", ""]
    lines += _block("За 24 часа", summary((now - timedelta(hours=24)).isoformat(), ex))
    lines.append("")
    lines += _block("За 7 дней", summary((now - timedelta(days=7)).isoformat(), ex))
    lines += ["", f"Счётчик ведётся с {first['t'][:16].replace('T', ' ')} UTC. В скобках — "
                  "сколько раз. Пишется только название действия, без текста сообщений."]
    return "\n".join(lines)
