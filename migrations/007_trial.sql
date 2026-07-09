-- Триал: короткий полный доступ вместо бессрочного free-тира (задача 20 плана)
ALTER TABLE users ADD COLUMN trial_granted_at TEXT;
