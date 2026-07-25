-- Событие 'trail': выход по ПОДТЯНУТОМУ стопу (трейлинг).
-- Зачем отдельный тип, а не 'stopped': по трейлингу сделка может закрыться В ПЛЮС, и
-- смешивать её со срабатыванием исходного стопа значит терять аудит-след — по журналу
-- событий должно быть видно, какой именно уровень исполнился.
-- CHECK в SQLite не меняется через ALTER — таблица пересоздаётся с переносом строк
-- (append-only журнал: содержимое переносится один-в-один, ничего не переписывается).
CREATE TABLE signal_events_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id     INTEGER NOT NULL REFERENCES signals(id),
    -- +'shadow': теневой исход отклонённого кандидата (counterfactual для когортного отчёта)
    -- +'trail':  выход по подтянутому стопу (результат может быть положительным)
    event         TEXT    NOT NULL CHECK (event IN ('candidate','confirmed','rejected','opened','tp1','tp2','tp3','stopped','expired','shadow','trail')),
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
