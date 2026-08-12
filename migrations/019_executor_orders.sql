-- Учёт ордеров executor (V2, демо BingX): какой сигнал каким объёмом исполнен на бирже.
-- Нужен для ТОЧНОГО закрытия: в hedge-режиме позиции одного символа/стороны от разных
-- сигналов (разные ТФ) сливаются в одну — без учёта qty закрытие «своей» части невозможно.
-- state: placed (лимитка стоит) -> filled (позиция открыта) -> closed | cancelled | error.
CREATE TABLE IF NOT EXISTS executor_orders (
    signal_id  INTEGER PRIMARY KEY REFERENCES signals(id),
    symbol     TEXT    NOT NULL,
    side       TEXT    NOT NULL CHECK (side IN ('LONG','SHORT')),
    qty        REAL    NOT NULL,
    entry_px   REAL    NOT NULL,              -- лимит-цена входа (= сигнальная)
    stop_px    REAL    NOT NULL,              -- аварийный стоп на бирже (исходный, -1R)
    client_id  TEXT    NOT NULL,              -- clientOrderID входа: rsiE<signal_id>
    -- placing: строка пишется ДО create_order — неоднозначный сбой (таймаут после
    -- отправки) восстановим сверкой по clientOrderID, ордер-сирота невозможен
    state      TEXT    NOT NULL DEFAULT 'placing'
               CHECK (state IN ('placing','placed','filled','closed','cancelled','error')),
    note       TEXT,                          -- последняя ошибка/примечание
    created_at TEXT    NOT NULL,
    updated_at TEXT
);
