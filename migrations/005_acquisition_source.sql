-- Атрибуция роста: откуда пришёл юзер (deep-link /start src_<label>, first-touch)
ALTER TABLE users ADD COLUMN acquisition_source TEXT;
