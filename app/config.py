"""Единый конфиг. Все секреты — только из окружения (.env), не в git.

Совместимо с Python 3.9+ (прод целится в 3.12). Единственное отличие от плана —
у dataclass не включён slots=True (появился в 3.10), чтобы каркас запускался
и на системном 3.9. На 3.12 можно вернуть slots=True без иных правок.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _env(key: str, default: str | None = None, *, required: bool = False) -> str | None:
    val = os.getenv(key, default)
    if required and not val:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {key}")
    return val


def _csv(raw: str | None) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    env: str
    log_level: str
    tg_bot_token: str
    admin_ids: tuple[int, ...]
    cryptobot_token: str | None
    sub_price_usdt: float
    sub_price_stars: int
    sub_days: int
    exchange_id: str
    symbols: tuple[str, ...]
    timeframes: tuple[str, ...]
    poll_interval_sec: int
    default_risk_pct: float
    max_leverage: int
    free_signals_per_week: int
    db_path: Path
    fernet_key: str | None
    signal_mode: str  # auto: сигнал сразу в журнал+алерт | semiauto: кандидат на подтверждение
    signals_channel: str  # @username или -100... id канала для постинга сигналов (пусто = выкл)


@lru_cache
def get_settings() -> Settings:
    try:
        from dotenv import load_dotenv  # ленивый импорт тяжёлой зависимости

        load_dotenv()
    except ImportError:
        pass
    return Settings(
        env=_env("ENV", "dev"),
        log_level=_env("LOG_LEVEL", "INFO"),
        tg_bot_token=_env("TELEGRAM_BOT_TOKEN", required=True),
        admin_ids=tuple(int(x) for x in _csv(_env("TELEGRAM_ADMIN_IDS"))),
        cryptobot_token=_env("CRYPTOBOT_TOKEN"),
        sub_price_usdt=float(_env("SUB_PRICE_USDT", "20")),
        sub_price_stars=int(_env("SUB_PRICE_STARS", "1000")),
        sub_days=int(_env("SUB_DAYS", "30")),
        exchange_id=_env("EXCHANGE_ID", "bybit"),
        symbols=tuple(_csv(_env("SYMBOLS", "BTC/USDT:USDT,ETH/USDT:USDT"))),
        timeframes=tuple(_csv(_env("TIMEFRAMES", "1h,4h"))),
        poll_interval_sec=int(_env("POLL_INTERVAL_SEC", "60")),
        default_risk_pct=float(_env("DEFAULT_RISK_PCT", "1.0")),
        max_leverage=int(_env("MAX_LEVERAGE", "20")),
        free_signals_per_week=int(_env("FREE_SIGNALS_PER_WEEK", "3")),
        db_path=Path(_env("DB_PATH", "data/signals.db")),
        fernet_key=_env("FERNET_KEY"),
        signal_mode=_env("SIGNAL_MODE", "auto"),
        signals_channel=_env("SIGNALS_CHANNEL", ""),
    )
