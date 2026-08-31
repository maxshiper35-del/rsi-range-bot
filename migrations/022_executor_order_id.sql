-- id ордера на бирже: точечный fetch/cancel по clientOrderID BingX-демо отвергает
-- (109400, смоук 31.08) — детекция филлов и снятие теперь по order_id, полученному
-- из ответа create_order; clientOrderID остаётся резервом для восстановления через
-- СПИСОК открытых ордеров (там поле отдаётся).
ALTER TABLE executor_orders ADD COLUMN order_id TEXT;
