-- Трейлинг на бирже (20.10.2026): журнал персистит текущий подтянутый стоп открытой
-- сделки (signals.trail_stop), executor переставляет стоп-ордер BingX вслед за ним и
-- помнит уровень, стоящий на бирже (executor_orders.trail_px). До этого трейл жил только
-- в журнале: на 24 выходах по трейлу executor отставал на −0.48R (закрытие по рынку
-- после бара, иногда уже исходным стопом).
ALTER TABLE signals ADD COLUMN trail_stop REAL;
ALTER TABLE executor_orders ADD COLUMN trail_px REAL;
