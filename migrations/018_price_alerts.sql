-- Бесплатные прайс-алерты (/alert BTC 65000) — воронка вширь: утилита для ВСЕХ
-- (включая неподписчиков), работает на уже подключённом WS-стриме klines.
-- One-shot: сработал -> triggered_at, повторно не стреляет (перевзвод = новый /alert).
-- Символы ограничены текущим вотчлистом (стрим подписан только на него); алерт на
-- выбывший из ротации символ дремлет до возвращения символа — это документировано в /alert.
CREATE TABLE IF NOT EXISTS price_alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id        INTEGER NOT NULL,
    symbol       TEXT    NOT NULL,              -- unified: 'BTC/USDT:USDT'
    price        REAL    NOT NULL,
    created_at   TEXT    NOT NULL,
    triggered_at TEXT                           -- NULL = армирован
);
CREATE INDEX IF NOT EXISTS idx_alerts_armed ON price_alerts(symbol, triggered_at);
CREATE INDEX IF NOT EXISTS idx_alerts_user ON price_alerts(tg_id, triggered_at);
