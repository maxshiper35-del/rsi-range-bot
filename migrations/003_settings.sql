-- Рантайм-настройки: правятся из Telegram без рестарта (override поверх .env)
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL
);
