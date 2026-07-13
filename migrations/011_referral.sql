-- Реферальная программа (задача 23): /start ref_<tg_id> -> invited_by (first-touch).
-- Награда рефереру НАЧИСЛЯЕТСЯ только за РЕАЛЬНЫЙ первый платёж приглашённого (не триал,
-- не промо) — анти-накрутка. referral_rewarded_at помечает, что за ЭТОГО юзера реферер
-- уже вознаграждён (одна награда на приглашённого, идемпотентно).
ALTER TABLE users ADD COLUMN invited_by INTEGER;          -- tg_id пригласившего (или NULL)
ALTER TABLE users ADD COLUMN referral_rewarded_at TEXT;   -- когда реферер награждён за юзера
