-- Версии исполнения и конфигурации (30.10.2026, docs/audits/2026-10-30-execution-parity.md).
-- Проверка вперёд (app/brain/forward_check.py) мерила демо одной когортой с 19.10, хотя механика
-- выхода за это время менялась трижды: почасовая перестановка стопа (19.10) -> нативный трейл с
-- активацией опросом BingX (24.10) -> активация по стриму Bybit (30.10). «Реальный R» смешивал
-- версии, и решение «демо -> живые деньги» принималось бы по чужой механике.
--
-- executor_orders.execution_version — версия executor, при которой ОТКРЫЛАСЬ позиция (она же
--   ведёт выход до закрытия; позиции, пережившие деплой, — редкое исключение):
--     e1 — вход лимиткой, выход по рынку после журнала;
--     e2 — вход TRIGGER_MARKET по касанию, выход по рынку после журнала;
--     e3 — + стоп на бирже переставляется вслед за трейлом журнала раз в такт;
--     e4 — + нативный трейлинг-стоп BingX, активация опросом цены BingX раз в такт;
--     e5 — активация по реалтайм-стриму Bybit (цена журнала), вход сразу после записи сигнала.
-- signals.config_version — короткий хеш стратегических настроек на момент создания сигнала
--   (journal._config_version): сравнение только внутри одной конфигурации. Для сигналов до
--   30.10 — NULL: настройки тогда не фиксировались, восстанавливать их задним числом нечем.
--
-- Бэкфилл execution_version — по моментам рестарта бота с новым кодом (bot.log, «poller
-- старт»: 02.09 11:05, 19.10 23:25:33, 24.10 20:15:33) и по времени открытия позиции
-- (филл журнала, без филла — постановка). Результаты журнала миграция не трогает.
ALTER TABLE executor_orders ADD COLUMN execution_version TEXT;
ALTER TABLE signals ADD COLUMN config_version TEXT;
UPDATE executor_orders SET execution_version = (
    SELECT CASE
        WHEN COALESCE(s.filled_at, executor_orders.created_at) < '2026-09-02T11:05:00' THEN 'e1'
        WHEN COALESCE(s.filled_at, executor_orders.created_at) < '2026-10-19T23:25:33' THEN 'e2'
        WHEN COALESCE(s.filled_at, executor_orders.created_at) < '2026-10-24T20:15:33' THEN 'e3'
        ELSE 'e4' END
    FROM signals s WHERE s.id = executor_orders.signal_id)
WHERE execution_version IS NULL;
