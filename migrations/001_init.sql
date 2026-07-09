-- ========================= signals =========================
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT    NOT NULL,
    side          TEXT    NOT NULL CHECK (side IN ('LONG','SHORT')),
    timeframe     TEXT    NOT NULL,
    entry         REAL    NOT NULL,
    stop          REAL    NOT NULL,
    tp1           REAL,
    tp2           REAL,
    tp3           REAL,
    risk_reward   REAL    NOT NULL,           -- +поле: R/R из Signal, чтобы не пересчитывать в /stats
    confidence    REAL    NOT NULL,
    reasons_json  TEXT,                        -- +поле: Signal.reasons для прозрачного журнала
    snapshot_json TEXT    NOT NULL,           -- indicators_snapshot из ядра
    exchange      TEXT    NOT NULL DEFAULT 'bybit',  -- +поле: мультибиржа (Bybit/Bitget)
    status        TEXT    NOT NULL DEFAULT 'open'
                  -- +'candidate'/'rejected': полуавтомат (кандидат ждёт подтверждения контекста человеком)
                  CHECK (status IN ('candidate','open','tp1','tp2','tp3','stopped','expired','rejected')),
    result_r      REAL,                        -- итог в R; NULL пока open
    created_at    TEXT    NOT NULL,            -- = время закрытия свечи (UTC)
    updated_at    TEXT,                        -- +поле: последняя смена статуса
    closed_at     TEXT,
    -- Идемпотентность цикла опроса: один сигнал на бар/сторону/биржу
    UNIQUE (exchange, symbol, timeframe, side, created_at)
);
CREATE INDEX IF NOT EXISTS idx_signals_status     ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals(created_at);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_tf  ON signals(symbol, timeframe);

-- ========================= users =========================
CREATE TABLE IF NOT EXISTS users (
    tg_id         INTEGER PRIMARY KEY,         -- Telegram user id
    username      TEXT,
    lang          TEXT    NOT NULL DEFAULT 'ru',
    tier          TEXT    NOT NULL DEFAULT 'free' CHECK (tier IN ('free','paid')),
    paid_until    TEXT,                        -- ISO UTC; NULL для free
    joined_at     TEXT    NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1,  -- +поле: юзер заблокировал бота -> не рассылать
    api_key_enc   BLOB,                        -- +поле (V2): Fernet-шифр API-ключа (только торговля)
    api_secret_enc BLOB,                       -- +поле (V2): Fernet-шифр секрета
    api_exchange  TEXT,                        -- +поле (V2): биржа для auto-execute
    updated_at    TEXT                         -- +поле: последнее изменение записи
);
CREATE INDEX IF NOT EXISTS idx_users_tier       ON users(tier);
CREATE INDEX IF NOT EXISTS idx_users_paid_until ON users(paid_until);  -- для sweep истёкших подписок

-- ========================= payments =========================
CREATE TABLE IF NOT EXISTS payments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id         INTEGER NOT NULL REFERENCES users(tg_id),
    method        TEXT    NOT NULL CHECK (method IN ('stars','cryptobot')),
    amount        REAL    NOT NULL,
    currency      TEXT    NOT NULL,            -- 'XTR' для Stars, 'USDT' для CryptoBot
    days          INTEGER NOT NULL,            -- сколько дней подписки куплено
    status        TEXT    NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','paid','expired','refunded')),  -- +поле: жизненный цикл инвойса
    invoice_id    TEXT,                        -- id инвойса у провайдера
    telegram_payment_charge_id TEXT,           -- +поле: для refundStarPayment
    created_at    TEXT    NOT NULL,
    paid_at       TEXT,                        -- +поле: момент фактической оплаты
    -- Идемпотентность вебхуков/повторных апдейтов
    UNIQUE (method, invoice_id)
);
CREATE INDEX IF NOT EXISTS idx_payments_tg_id      ON payments(tg_id);
CREATE INDEX IF NOT EXISTS idx_payments_status     ON payments(status);
CREATE INDEX IF NOT EXISTS idx_payments_created_at ON payments(created_at);

-- ========================= paper_trades =========================
CREATE TABLE IF NOT EXISTS paper_trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id         INTEGER NOT NULL REFERENCES users(tg_id),
    signal_id     INTEGER NOT NULL REFERENCES signals(id),
    side          TEXT,                        -- +поле: денормализация для быстрых отчётов
    qty           REAL    NOT NULL,
    entry         REAL    NOT NULL,
    exit          REAL,                        -- NULL пока открыт
    pnl           REAL,                        -- в USDT; NULL пока открыт
    result_r      REAL,                        -- +поле: результат в R для агрегатов
    status        TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),  -- +поле
    opened_at     TEXT    NOT NULL,            -- +поле: момент открытия
    closed_at     TEXT,
    UNIQUE (tg_id, signal_id)                  -- один бумажный трейд на юзера на сигнал
);
CREATE INDEX IF NOT EXISTS idx_paper_tg_id     ON paper_trades(tg_id);
CREATE INDEX IF NOT EXISTS idx_paper_signal_id ON paper_trades(signal_id);
CREATE INDEX IF NOT EXISTS idx_paper_status    ON paper_trades(status);

-- ============ signal_events (рекомендуемое расширение) ============
-- Append-only журнал исходов: реализует security-требование
-- «публичный журнал нельзя редактировать задним числом».
-- journal.py пишет сюда по данным биржи; UPDATE/DELETE запрещены на уровне репозитория.
CREATE TABLE IF NOT EXISTS signal_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id     INTEGER NOT NULL REFERENCES signals(id),
    -- +'shadow': теневой исход отклонённого кандидата (counterfactual для когортного отчёта)
    event         TEXT    NOT NULL CHECK (event IN ('candidate','confirmed','rejected','opened','tp1','tp2','tp3','stopped','expired','shadow')),
    price         REAL,                        -- цена биржи, зафиксировавшая исход
    r_at_event    REAL,                        -- сколько R закрыто на этом событии
    exchange_ts   TEXT,                        -- метка времени бара/сделки с биржи
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_signal_id ON signal_events(signal_id);
