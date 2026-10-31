-- Отзывы пользователей (опрос 31.10.2026 «как вам бот, что улучшить»). Оценка — кнопкой под
-- опросом, текст — следующим сообщением или просто ответом на опрос, пока он открыт. Каждый
-- отзыв сразу уходит админам. survey_sends — кому и когда ушёл опрос и чем кончилась отправка:
-- по ней текст без кнопки засчитывается как отзыв, а повторный запуск рассылки не шлёт дважды.

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id       INTEGER NOT NULL,
    survey      TEXT    NOT NULL,                          -- метка опроса, напр. 'bot-2026-10-31'
    rating      TEXT    CHECK (rating IN ('love', 'ok', 'bad')),  -- NULL — только текст
    text        TEXT,                                      -- NULL — только оценка
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_survey ON feedback(survey, created_at);
CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(tg_id, survey);

CREATE TABLE IF NOT EXISTS survey_sends (
    tg_id       INTEGER NOT NULL,
    survey      TEXT    NOT NULL,
    sent_at     TEXT    NOT NULL,
    status      TEXT    NOT NULL CHECK (status IN ('sent', 'blocked', 'failed')),
    error       TEXT,
    PRIMARY KEY (tg_id, survey)
);
