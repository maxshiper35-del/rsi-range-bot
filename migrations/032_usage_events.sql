-- Счётчик использования (10.11.2026): какой раздел бота и когда открывал человек. После
-- перевода бота на «проверку и обучение» главный вопрос — пользуются ли люди проверкой
-- сигналов и каналов; до этой таблицы ответить на него было нечем (в БД попадали только
-- оплаты, голоса игры и отметки «взял сделку»). Пишется только название действия: команда
-- без аргументов, текст кнопки меню, префикс инлайн-кнопки, «пересылка», «ввод текста»,
-- открытие мини-приложения. Содержимое сообщений не хранится.

CREATE TABLE IF NOT EXISTS usage_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id       INTEGER NOT NULL,
    action      TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_events_created ON usage_events(created_at);
CREATE INDEX IF NOT EXISTS idx_usage_events_user ON usage_events(tg_id, action, created_at);
