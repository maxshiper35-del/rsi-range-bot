-- Метка публикации сигнала и досылка после рестарта (30.10.2026,
-- docs/audits/2026-10-30-reliability.md, M5).
--
-- Рестарт в первые ~90 с часа: poll_once работает в потоке и успевает записать сигнал, а
-- корутина рассылки уже отменена — сделка есть в журнале и у executor, но её не видел никто
-- (ни канал, ни личка). Следующий такт её не повторит (дедуп, decision_is_fresh). Таблица
-- фиксирует, докуда дошла доставка; при старте бот досылает свежие недоставленные
-- (app/delivery/publication.py).
--
--   claimed_at     — доставку начал процесс: атомарный INSERT, второй претендент (досылка при
--                    старте против поллера) проигрывает и ничего не шлёт;
--   channel        — NULL: до поста в канал не дошли (пост точно не уходил);
--                    sending — пост отправляется: обрыв здесь = исход неизвестен, повторно НЕ
--                      шлём (лишний пост в публичном канале), админам — ERROR;
--                    sent — пост в канале; failed — Telegram вернул ошибку; off — канал выключен;
--                    unknown — обрыв на отправке, админам сообщено; skipped — досылка не нужна;
--   channel_msg_id — id поста в канале;
--   done_at        — доставка (канал + личка) завершена либо досылка признана ненужной;
--   resumed_at     — досылка при старте уже была: второй раз не досылаем;
--   note           — почему пропущено / текст ошибки.
CREATE TABLE IF NOT EXISTS signal_publication (
    signal_id      INTEGER PRIMARY KEY REFERENCES signals(id),
    claimed_at     TEXT NOT NULL,
    channel        TEXT CHECK (channel IN ('sending','sent','failed','off','unknown','skipped')),
    channel_msg_id INTEGER,
    done_at        TEXT,
    resumed_at     TEXT,
    note           TEXT
);

-- Всё записанное до миграции считаем опубликованным: досылка — только для новых сигналов.
INSERT OR IGNORE INTO signal_publication (signal_id, claimed_at, done_at, note)
SELECT id, created_at, created_at, 'до миграции 029' FROM signals;
