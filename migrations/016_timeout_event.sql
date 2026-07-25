-- Событие 'timeout': выход по времени удержания.
-- Нужно вместе с режимом «выход только по трейлингу»: без фиксированного тейка позиция
-- в принципе может висеть неделями (в симуляции встречались 36 дней), занимая слот
-- направления и накапливая фандинг. Потолок удержания закрывает сделку по цене закрытия
-- бара — и это отдельный тип исхода, а не стоп и не тейк.
-- CHECK в SQLite не меняется через ALTER — таблица пересоздаётся с переносом строк.
CREATE TABLE signal_events_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id     INTEGER NOT NULL REFERENCES signals(id),
    -- 'shadow': теневой исход отклонённого кандидата; 'trail': выход по подтянутому стопу;
    -- 'timeout': выход по потолку времени удержания
    event         TEXT    NOT NULL CHECK (event IN ('candidate','confirmed','rejected','opened','tp1','tp2','tp3','stopped','expired','shadow','trail','timeout')),
    price         REAL,
    r_at_event    REAL,
    exchange_ts   TEXT,
    created_at    TEXT    NOT NULL
);

INSERT INTO signal_events_new (id, signal_id, event, price, r_at_event, exchange_ts, created_at)
SELECT id, signal_id, event, price, r_at_event, exchange_ts, created_at FROM signal_events;

DROP TABLE signal_events;
ALTER TABLE signal_events_new RENAME TO signal_events;
CREATE INDEX idx_events_signal_id ON signal_events(signal_id);
