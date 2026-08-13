-- Персональный журнал подписчика в Mini App: «в какие сделки я зашёл».
-- Отметка = строка; снятие отметки = удаление строки. Итог юзера = сумма result_r
-- по отмеченным ЗАКРЫТЫМ сигналам (expired без R в итог не попадают сами).
-- Отметки переживают истечение подписки (вернулся — журнал на месте), но
-- видны/редактируются только при активной подписке (проверка в /api/me).
CREATE TABLE IF NOT EXISTS user_taken (
    tg_id      INTEGER NOT NULL,
    signal_id  INTEGER NOT NULL REFERENCES signals(id),
    created_at TEXT    NOT NULL,
    PRIMARY KEY (tg_id, signal_id)
);
CREATE INDEX IF NOT EXISTS idx_user_taken_signal ON user_taken(signal_id);
