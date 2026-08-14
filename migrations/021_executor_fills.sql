-- Отчёт «исполнение vs журнал»: фактические цены/комиссии демо-исполнения BingX.
-- Заполняются executor'ом в моменты событий (fetch_order по clientOrderID/id):
--   fill_px/fill_qty/fee — при позитивном определении филла входа;
--   close_px/close_fee/closed_at — при рыночном закрытии по решению журнала.
-- NULL = событие не случилось или биржа не отдала поле (best-effort, VST).
-- На этих полях строится еженедельная сводка execution_report: проскальзывание
-- входа/выхода против журнала, комиссии, демо-R vs журнальный R — живая проверка
-- допущений бэктеста (maker-вход, слип 0.02%) перед гейтом эпохи-3 (28.08).
ALTER TABLE executor_orders ADD COLUMN fill_px   REAL;
ALTER TABLE executor_orders ADD COLUMN fill_qty  REAL;
ALTER TABLE executor_orders ADD COLUMN fill_fee  REAL;
ALTER TABLE executor_orders ADD COLUMN close_px  REAL;
ALTER TABLE executor_orders ADD COLUMN close_fee REAL;
ALTER TABLE executor_orders ADD COLUMN closed_at TEXT;
