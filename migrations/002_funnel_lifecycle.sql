-- Воронка free-тира: учёт доставок сигналов (free получает N полных/нед, дальше тизер)
CREATE TABLE IF NOT EXISTS deliveries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id      INTEGER NOT NULL,
    signal_id  INTEGER NOT NULL,
    kind       TEXT    NOT NULL CHECK (kind IN ('full','teaser')),  -- полный сигнал | превью
    created_at TEXT    NOT NULL,
    UNIQUE (tg_id, signal_id)                    -- один сигнал юзеру доставляется один раз
);
CREATE INDEX IF NOT EXISTS idx_deliveries_tg ON deliveries(tg_id, created_at);

-- Напоминание о продлении: метка, чтобы не спамить (сбрасывается при новой оплате)
ALTER TABLE users ADD COLUMN renew_reminded_at TEXT;
