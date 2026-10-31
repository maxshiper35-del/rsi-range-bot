"""Опрос «как вам бот» и отзывы (миграция 031, app/delivery/feedback.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def test_open_survey_window_and_text_limit(fresh_db, monkeypatch):
    from app import clock
    from app.delivery import feedback

    assert feedback.open_survey(1) is None              # опрос не слали — текст не отзыв
    feedback.record_send(1, "sent")
    feedback.record_send(2, "blocked", "Forbidden: bot was blocked by the user")
    assert feedback.open_survey(1) == feedback.SURVEY
    assert feedback.open_survey(2) is None              # не доставлен — не открыт
    assert feedback.sent_ids() == {1}

    for i in range(feedback.TEXTS_PER_USER):
        feedback.save_text(1, f"отзыв {i}")
    assert feedback.open_survey(1) is None              # лимит текстов выбран

    feedback.record_send(3, "sent")
    later = datetime.now(timezone.utc) + timedelta(days=feedback.OPEN_DAYS, minutes=1)
    monkeypatch.setattr(clock, "utcnow", lambda: later)
    assert feedback.open_survey(3) is None              # неделя прошла — опрос закрыт


def test_stats_counts_last_rating_and_texts(fresh_db):
    from app.delivery import feedback

    feedback.record_send(1, "sent")
    feedback.record_send(2, "sent")
    feedback.record_send(3, "blocked")
    feedback.save_rating(1, "bad")
    feedback.save_rating(1, "love")                     # передумал — считается последняя
    feedback.save_rating(2, "ok")
    feedback.save_text(2, "  добавьте уведомление о закрытии  ")
    st = feedback.stats()
    assert st["sends"] == {"sent": 2, "blocked": 1}
    assert st["ratings"] == {"love": 1, "ok": 1, "bad": 0}
    assert st["texts"] == 1
    text = feedback.render_stats()
    assert "Доставлено: 2" in text and "заблокировали бота: 1" in text
    assert "добавьте уведомление о закрытии" in text


def test_rating_validation_text_trim_and_admin_note(fresh_db):
    import pytest

    from app.delivery import feedback

    with pytest.raises(ValueError):
        feedback.save_rating(1, "meh")
    feedback.save_text(1, "x" * (feedback.TEXT_MAX + 50))
    from app import db
    row = db.query_one("SELECT text FROM feedback WHERE tg_id=1")
    assert len(row["text"]) == feedback.TEXT_MAX
    assert feedback.admin_note(5, "neo", rating="love") == "📝 Отзыв от @neo (5): 🔥 Нравится"
    assert "«хочу тёмную тему»" in feedback.admin_note(5, None, text="хочу тёмную тему")


def test_survey_text_and_buttons():
    from app.delivery import feedback

    assert "Как вам бот?" in feedback.SURVEY_TEXT
    assert "демо" not in feedback.SURVEY_TEXT.lower()
    rows = feedback.keyboard_rows()
    datas = [d for row in rows for _, d in row]
    assert all(d.startswith(feedback.CALLBACK_PREFIX) and len(d.encode()) <= 64 for d in datas)
    assert {d.removeprefix(feedback.CALLBACK_PREFIX) for d in datas} == {
        *feedback.RATINGS, feedback.WRITE}
    assert set(feedback.ASK_TEXT) == {*feedback.RATINGS, feedback.WRITE}
