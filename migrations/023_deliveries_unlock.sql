-- Разовая покупка сигнала (paid media) помечает доставку kind='unlock', а CHECK из 002
-- разрешал только full/teaser: UPDATE падал IntegrityError, и купивший не получал полный
-- текст (аудит 05.10.2026). SQLite не умеет ALTER CHECK — пересоздаём таблицу.
CREATE TABLE deliveries_new (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id      INTEGER NOT NULL,
    signal_id  INTEGER NOT NULL,
    kind       TEXT    NOT NULL CHECK (kind IN ('full','teaser','unlock')),
    created_at TEXT    NOT NULL,
    UNIQUE (tg_id, signal_id)
);
INSERT INTO deliveries_new(id, tg_id, signal_id, kind, created_at)
    SELECT id, tg_id, signal_id, kind, created_at FROM deliveries;
DROP TABLE deliveries;
ALTER TABLE deliveries_new RENAME TO deliveries;
CREATE INDEX IF NOT EXISTS idx_deliveries_tg ON deliveries(tg_id, created_at);
