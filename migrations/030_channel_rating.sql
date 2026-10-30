-- Рейтинг сигнальных каналов (30.10.2026, поворот продукта к проверке: замеры показали,
-- что перевеса у стратегий сигналов при реальном исполнении нет — docs/research/
-- strategy-registry.md). Бот сам раз в сутки прогоняет публичные каналы через аудит
-- (channel_audit: веб-превью t.me/s -> сигналы -> свечи биржи) и хранит ТОЛЬКО агрегаты:
-- исходные посты каналов не копируем. Публичная выдача анонимна: каналы — буквы A, B, C…,
-- буква закреплена за каналом навсегда (столбец letter), имя видит только админ.

CREATE TABLE IF NOT EXISTS rating_channels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT    NOT NULL UNIQUE COLLATE NOCASE,   -- без @, как в t.me/s/<username>
    letter      TEXT    UNIQUE,                            -- A…Z, AA…: выдаётся при первом обходе
    status      TEXT    NOT NULL DEFAULT 'active'
                CHECK (status IN ('candidate','active','paused','removed')),
    -- candidate — попал из /audit (≥ 12 разобранных сигналов), ждёт решения админа или
    --             автовключения; active — в обходе; paused — админ снял; removed — удалён
    added_by    TEXT    NOT NULL,                          -- 'seed' | 'admin:<tg_id>' | 'audit:<tg_id>'
    added_at    TEXT    NOT NULL,
    title       TEXT,                                      -- название канала с превью (админу)
    subscribers INTEGER,                                   -- счётчик подписчиков с превью, если есть
    last_run_at TEXT,
    last_error  TEXT,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_rating_channels_status ON rating_channels(status);

-- Один обход канала — одна строка; история не затирается (динамика «было/стало»).
CREATE TABLE IF NOT EXISTS rating_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  INTEGER NOT NULL REFERENCES rating_channels(id),
    run_at      TEXT    NOT NULL,
    posts       INTEGER NOT NULL,                          -- прочитано постов
    signals     INTEGER NOT NULL,                          -- разобранных сигналов (с входом и стопом)
    checked     INTEGER NOT NULL,                          -- прогнано по свечам
    filled      INTEGER NOT NULL,                          -- вход исполнился
    tp1         INTEGER NOT NULL,                          -- дошли до первой цели
    stop_first  INTEGER NOT NULL,                          -- стоп раньше цели
    no_fill     INTEGER NOT NULL,                          -- вход не исполнился
    open        INTEGER NOT NULL,                          -- ещё открыты без TP1
    no_stop     INTEGER NOT NULL,                          -- посты-сигналы без стопа
    tp1_avg_r   REAL,                                      -- средняя сработавшая TP1 в R
    r_total     REAL    NOT NULL,                          -- итог «вся позиция на TP1», R
    r_trades    INTEGER NOT NULL,                          -- сделок в итоге
    small_sample INTEGER NOT NULL DEFAULT 0,               -- 1 = меньше 10 исполненных: выводы предварительные
    period_from TEXT,
    period_to   TEXT
);
CREATE INDEX IF NOT EXISTS idx_rating_runs_channel ON rating_runs(channel_id, run_at);
