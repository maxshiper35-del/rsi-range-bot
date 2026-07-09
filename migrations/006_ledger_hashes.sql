-- Локальный архив хэшей публичного трек-рекорда (CSV-экспорт /trackrecord + еженедельный
-- пост в канал). Живёт в уже забэкапленной БД — переживает удаление/блокировку канала.
CREATE TABLE IF NOT EXISTS ledger_hashes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    row_count  INTEGER NOT NULL,
    sha256     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
