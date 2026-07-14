-- Владелец персонального промокода (win-back wb<tg_id>): погасить может ТОЛЬКО владелец.
-- Telegram-id публичны — без привязки «первый погасивший забирает» позволял угонять чужой
-- win-back-код (аудит-2 [4]). NULL = общий код без владельца (обычные /addpromo).
ALTER TABLE promo_codes ADD COLUMN owner_tg_id INTEGER;
