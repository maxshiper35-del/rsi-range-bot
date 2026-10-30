-- Ежедневная игра «⬆️ Лонг или шорт? ⬇️» (ретеншн, 29.10.2026): повод открывать бота
-- каждый день, когда кончился 5-дневный триал. Opt-in через /game; раз в сутки один вопрос
-- «BTC через 24 часа будет выше или ниже, чем сейчас?». Правила и их обоснование — в
-- app/delivery/game.py. Время — ISO UTC; бары — open-time 1h-свечи в мс (как ts у ccxt).

-- Участники. active=0 — вышел (/game off) или проголосовал, не вступая (пересланный раунд):
-- итог своего раунда такой получит, ежедневные раунды — нет (opt-in без исключений).
CREATE TABLE IF NOT EXISTS game_players (
    tg_id       INTEGER PRIMARY KEY,
    active      INTEGER NOT NULL DEFAULT 1,
    joined_at   TEXT    NOT NULL,
    left_at     TEXT,                             -- NULL у вступавшего = ни разу не выходил
    streak      INTEGER NOT NULL DEFAULT 0,       -- верных подряд на сейчас (пересчёт на итоге)
    best_streak INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_game_players_active ON game_players(active);

-- Раунды: один на UTC-сутки — UNIQUE(day) он же маркер «раунд дня уже стартовал».
CREATE TABLE IF NOT EXISTS game_rounds (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    day           TEXT    NOT NULL UNIQUE,        -- YYYY-MM-DD (UTC) дня старта
    symbol        TEXT    NOT NULL,
    ref_bar_ts    INTEGER NOT NULL,               -- open-time последнего ЗАКРЫТОГО 1h-бара на старте
    ref_price     REAL    NOT NULL,               -- его close — «сейчас» в вопросе
    target_bar_ts INTEGER NOT NULL,               -- ref_bar_ts + 24ч: бар, чей close решает раунд
    chart_json    TEXT    NOT NULL,               -- свечи карты [[ts,o,h,l,c],…] — перерисовка без биржи
    created_at    TEXT    NOT NULL,               -- фактический старт (позже 12:00, если Mac спал)
    closes_at     TEXT    NOT NULL,               -- конец голосования
    photo_file_id TEXT,                           -- file_id карты в Telegram после первой отправки
    result_price  REAL,
    outcome       TEXT CHECK (outcome IN ('up','down','flat','void')),  -- NULL = ещё не решён
    resolved_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_game_rounds_pending ON game_rounds(outcome, target_bar_ts);

-- Голоса: один на юзера на раунд (PRIMARY KEY); изменить нельзя — UPDATE choice в коде нет.
CREATE TABLE IF NOT EXISTS game_votes (
    round_id     INTEGER NOT NULL REFERENCES game_rounds(id),
    tg_id        INTEGER NOT NULL,
    choice       TEXT    NOT NULL CHECK (choice IN ('up','down')),
    voted_at     TEXT    NOT NULL,
    correct      INTEGER,                         -- 1/0; NULL — не решён, ничья или аннулирован
    points       INTEGER,                         -- NULL — раунд ещё не решён
    streak_after INTEGER,                         -- серия после этого раунда (для итога в личку)
    PRIMARY KEY (round_id, tg_id)
);
CREATE INDEX IF NOT EXISTS idx_game_votes_user ON game_votes(tg_id);

-- Исходящие сообщения игры: ОДНО на (вид, раунд/неделя, юзер). Строка ставится ДО отправки
-- (at-most-once: краш посреди рассылки не задублирует), sent_at — после успеха; транзиентный
-- сбой строку снимает (повтор следующим тактом). kind: round | result | week.
CREATE TABLE IF NOT EXISTS game_sends (
    kind       TEXT    NOT NULL CHECK (kind IN ('round','result','week')),
    ref        TEXT    NOT NULL,                  -- id раунда или ISO-неделя 'YYYY-Www'
    tg_id      INTEGER NOT NULL,
    claimed_at TEXT    NOT NULL,
    sent_at    TEXT,                              -- NULL — не доставлено (сбой/мёртвый получатель)
    PRIMARY KEY (kind, ref, tg_id)
);

-- Итоги недели: одна строка на ISO-неделю — PRIMARY KEY делает приз идемпотентным.
CREATE TABLE IF NOT EXISTS game_weeks (
    week_key     TEXT    PRIMARY KEY,             -- 'YYYY-Www'
    winner_tg_id INTEGER,                         -- NULL — никто не набрал очков
    points       INTEGER NOT NULL DEFAULT 0,
    prize_days   REAL    NOT NULL DEFAULT 0,      -- выдано дней (0 — приз выключен/нет победителя)
    players      INTEGER NOT NULL DEFAULT 0,      -- участников в зачёте
    awarded_at   TEXT    NOT NULL
);
