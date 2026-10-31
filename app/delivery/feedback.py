"""Опрос «как вам бот» и отзывы пользователей (31.10.2026, миграция 031).

Рассылка — scripts/send_survey.py: опрос с тремя кнопками оценки и кнопкой «написать». Оценка
пишется по нажатию, текст — следующим сообщением (FSM в боте) или просто ответом на опрос, пока
он открыт (OPEN_DAYS). Каждый отзыв сразу уходит админам; /feedback — сводка и последние тексты.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app import clock, db

SURVEY = "bot-2026-10-31"
OPEN_DAYS = 7
TEXTS_PER_USER = 5          # текстов на опрос с одного юзера — дальше свободный текст не ловим
TEXT_MAX = 3000
CALLBACK_PREFIX = "fb:"

RATINGS = {"love": "🔥 Нравится", "ok": "🙂 Нормально", "bad": "👎 Не нравится"}
WRITE = "write"

SURVEY_TEXT = (
    "👋 Привет! Это команда RSI Range.\n\n"
    "Хотим сделать бота лучше — и без вас тут не обойтись.\n\n"
    "<b>Как вам бот?</b> Оцените одной кнопкой ниже — это пара секунд.\n\n"
    "Есть что сказать — что мешает, чего не хватает, что сделать по-другому? Просто ответьте "
    "на это сообщение. Каждый ответ читаем сами, лучшие идеи берём в работу.\n\n"
    "Спасибо, что вы с нами 🙏"
)

ASK_TEXT = {
    "love": "Спасибо! 🙌 А что улучшить в первую очередь? Напишите одним сообщением — "
            "передам команде. Не хочется писать — просто пропустите.",
    "ok": "Спасибо! Чего не хватает, чтобы стало «нравится»? Напишите одним сообщением — "
          "передам команде. Не хочется писать — просто пропустите.",
    "bad": "Спасибо за честность. Что именно не так? Напишите одним сообщением — "
           "разберёмся. Не хочется писать — просто пропустите.",
    WRITE: "Пишите одним сообщением — читаем каждое.",
}
THANKS = "Спасибо! Передал команде 🙏"


def keyboard_rows() -> list[list[tuple[str, str]]]:
    """Кнопки под опросом: (текст, callback_data). Сборка InlineKeyboardMarkup — в боте."""
    return [[(label, CALLBACK_PREFIX + key) for key, label in RATINGS.items()],
            [("✍️ Написать, что улучшить", CALLBACK_PREFIX + WRITE)]]


def record_send(tg_id: int, status: str, error: str | None = None,
                survey: str = SURVEY) -> None:
    db.execute("INSERT OR REPLACE INTO survey_sends(tg_id, survey, sent_at, status, error) "
               "VALUES(?,?,?,?,?)", (tg_id, survey, clock.utcnow_iso(), status, error))


def sent_ids(survey: str = SURVEY) -> set[int]:
    """Кому опрос уже доставлен — повторный запуск рассылки их пропускает."""
    return {r["tg_id"] for r in db.query(
        "SELECT tg_id FROM survey_sends WHERE survey=? AND status='sent'", (survey,))}


def open_survey(tg_id: int) -> str | None:
    """Метка опроса, если он доставлен юзеру меньше OPEN_DAYS назад и лимит текстов не выбран:
    тогда свободный текст юзера — отзыв. Иначе None."""
    since = (clock.utcnow() - timedelta(days=OPEN_DAYS)).isoformat()
    row = db.query_one("SELECT survey FROM survey_sends WHERE tg_id=? AND status='sent' "
                       "AND sent_at >= ? ORDER BY sent_at DESC LIMIT 1", (tg_id, since))
    if row is None:
        return None
    n = db.query("SELECT 1 FROM feedback WHERE tg_id=? AND survey=? AND text IS NOT NULL",
                 (tg_id, row["survey"]))
    return row["survey"] if len(n) < TEXTS_PER_USER else None


def save_rating(tg_id: int, rating: str, survey: str = SURVEY) -> None:
    if rating not in RATINGS:
        raise ValueError(f"оценка {rating!r}")
    db.execute("INSERT INTO feedback(tg_id, survey, rating, text, created_at) "
               "VALUES(?,?,?,NULL,?)", (tg_id, survey, rating, clock.utcnow_iso()))


def save_text(tg_id: int, text: str, survey: str = SURVEY) -> None:
    db.execute("INSERT INTO feedback(tg_id, survey, rating, text, created_at) "
               "VALUES(?,?,NULL,?,?)", (tg_id, survey, text.strip()[:TEXT_MAX],
                                         clock.utcnow_iso()))


def stats(survey: str = SURVEY) -> dict:
    sends = {r["status"]: r["n"] for r in db.query(
        "SELECT status, COUNT(*) AS n FROM survey_sends WHERE survey=? GROUP BY status",
        (survey,))}
    # оценка юзера — последняя нажатая (передумал — считаем новую)
    last = db.query(
        "SELECT f.rating FROM feedback f WHERE f.survey=? AND f.rating IS NOT NULL AND f.id = ("
        "SELECT MAX(id) FROM feedback g WHERE g.tg_id=f.tg_id AND g.survey=f.survey "
        "AND g.rating IS NOT NULL)", (survey,))
    ratings = {k: 0 for k in RATINGS}
    for r in last:
        ratings[r["rating"]] += 1
    texts = len(db.query("SELECT 1 FROM feedback WHERE survey=? AND text IS NOT NULL",
                         (survey,)))
    return {"sends": sends, "ratings": ratings, "texts": texts}


def recent_texts(survey: str = SURVEY, limit: int = 15) -> list:
    return db.query("SELECT tg_id, text, created_at FROM feedback WHERE survey=? "
                    "AND text IS NOT NULL ORDER BY id DESC LIMIT ?", (survey, limit))


def admin_note(tg_id: int, username: str | None, *, rating: str | None = None,
               text: str | None = None) -> str:
    """Уведомление админу о новом отзыве — без parse_mode (текст юзера не экранируем)."""
    who = f"@{username}" if username else f"id {tg_id}"
    head = f"📝 Отзыв от {who} ({tg_id})"
    if rating:
        return f"{head}: {RATINGS.get(rating, rating)}"
    return f"{head}:\n«{(text or '').strip()[:TEXT_MAX]}»"


def render_stats(survey: str = SURVEY) -> str:
    st = stats(survey)
    s = st["sends"]
    r = st["ratings"]
    lines = [f"📝 Опрос {survey}",
             f"Доставлено: {s.get('sent', 0)} · заблокировали бота: {s.get('blocked', 0)} · "
             f"ошибки: {s.get('failed', 0)}",
             " · ".join(f"{RATINGS[k]}: {r[k]}" for k in RATINGS),
             f"Текстовых отзывов: {st['texts']}"]
    rows = recent_texts(survey)
    if rows:
        lines.append("")
        lines.append("Последние:")
        for row in rows:
            day = datetime.fromisoformat(row["created_at"]).strftime("%d.%m %H:%M")
            lines.append(f"— {day} · {row['tg_id']}: {row['text'][:300]}")
    return "\n".join(lines)
