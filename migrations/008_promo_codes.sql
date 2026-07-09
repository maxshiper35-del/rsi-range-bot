-- Промокоды на +N дней подписки (задача 21 плана). Готовые дни, не %-скидка — проще для
-- ручной выдачи одним админом, не требует пересчёта цены инвойса.
CREATE TABLE IF NOT EXISTS promo_codes (
    code        TEXT PRIMARY KEY,
    days        INTEGER NOT NULL,
    max_uses    INTEGER,                 -- NULL = без лимита
    used_count  INTEGER NOT NULL DEFAULT 0,
    expires_at  TEXT,                    -- NULL = бессрочный
    created_at  TEXT NOT NULL
);

-- дедуп: один код на юзера один раз (UNIQUE ловит повторное применение как IntegrityError)
CREATE TABLE IF NOT EXISTS promo_redemptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL REFERENCES promo_codes(code),
    tg_id       INTEGER NOT NULL REFERENCES users(tg_id),
    redeemed_at TEXT NOT NULL,
    UNIQUE(code, tg_id)
);
