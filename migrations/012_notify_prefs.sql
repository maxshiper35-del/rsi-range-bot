-- Настройки уведомлений юзера (задача UX): мьют и тихие часы для DM-сигналов.
-- Гейтят ТОЛЬКО пуш сигналов в личку — доступ, статистика, результаты не трогаются.
-- quiet_start/quiet_end — часы UTC [0..23], полуинтервал [start, end); окно через полночь
-- допускается (start > end). NULL в любой границе = тихие часы выключены.
ALTER TABLE users ADD COLUMN notify_muted INTEGER NOT NULL DEFAULT 0;  -- 1 = мьют всех сигналов
ALTER TABLE users ADD COLUMN quiet_start INTEGER;                      -- час UTC начала тишины
ALTER TABLE users ADD COLUMN quiet_end INTEGER;                        -- час UTC конца тишины
