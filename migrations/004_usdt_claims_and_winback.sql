-- Заявки «Я оплатил USDT»: SLA-отслеживание и эскалация зависших (топ-жалоба ниши —
-- «оплатил, доступ не выдали вовремя»), плюс win-back напоминание после истечения подписки.
CREATE TABLE IF NOT EXISTS usdt_claims (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id          INTEGER NOT NULL REFERENCES users(tg_id),
    created_at     TEXT    NOT NULL,
    resolved_at    TEXT,                       -- закрыта /grant'ом
    escalated_at   TEXT                        -- админ пингован о зависшей заявке (один раз)
);
CREATE INDEX IF NOT EXISTS idx_usdt_claims_open ON usdt_claims(tg_id, resolved_at);

-- win-back: одно напоминание после истечения подписки (сбрасывается при новой оплате)
ALTER TABLE users ADD COLUMN winback_reminded_at TEXT;
