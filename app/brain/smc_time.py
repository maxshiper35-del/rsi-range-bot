"""
smc_time.py — временные фильтры SMC: килзоны Silver Bullet, азиатский ренж, Macros.

Все окна нормализованы в UTC. Времена и оффсеты — дословно из колод Silver Bullet
(NY, UTC-5 и −4) и Macros (UTC+3); оффсеты фиксированы (как в курсе, без DST).

Silver Bullet Classic (NY, UTC-5):  London 3–4 -> UTC 8–9; New York 10–11 -> UTC 15–16;
                                    PM New York 14–15 -> UTC 19–20.
Silver Bullet RU (NY, −4):          Asia range 20:00–0:00 -> UTC 00:00–04:00.
Macros (UTC+3):                     15:50–16:10, 16:50–17:10, 17:50–18:10,
                                    20:10–20:40, 22:15–22:45 -> см. ниже (UTC).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Window:
    name: str
    start_min: int      # минута дня в UTC (0..1439), включительно
    end_min: int        # исключительно


def _m(h: int, m: int) -> int:
    return h * 60 + m


# Килзоны Silver Bullet (окна входа, ≤60 мин, ТФ 1м–5м, RR 1:1–1:2)
SILVER_BULLET = [
    Window("SB London", _m(8, 0), _m(9, 0)),
    Window("SB New York", _m(15, 0), _m(16, 0)),
    Window("SB PM New York", _m(19, 0), _m(20, 0)),
]

# Азиатский диапазон (справочный — от его границ ждём съём ликвидности после закрытия)
ASIA_RANGE = [Window("Asia range", _m(0, 0), _m(4, 0))]

# Macros — окна повышенной волатильности (UTC+3 -> UTC)
MACROS = [
    Window("Macro 12:50", _m(12, 50), _m(13, 10)),
    Window("Macro 13:50", _m(13, 50), _m(14, 10)),
    Window("Macro 14:50", _m(14, 50), _m(15, 10)),
    Window("Macro 17:10", _m(17, 10), _m(17, 40)),
    Window("Macro 19:15", _m(19, 15), _m(19, 45)),
]

# Окна входа (Asia range — справочный диапазон, не окно входа)
ALL_WINDOWS = SILVER_BULLET + MACROS


def _utc_minute(dt: datetime) -> int:
    """Минута дня в UTC. Наивный datetime трактуется как UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    u = dt.astimezone(timezone.utc)
    return u.hour * 60 + u.minute


def active_windows(dt: datetime, windows: list[Window] = ALL_WINDOWS) -> list[str]:
    """Имена окон, активных в момент dt (UTC)."""
    m = _utc_minute(dt)
    return [w.name for w in windows if w.start_min <= m < w.end_min]


def in_killzone(dt: datetime) -> bool:
    """dt попадает в килзону Silver Bullet?"""
    return bool(active_windows(dt, SILVER_BULLET))


def in_asia_range(dt: datetime) -> bool:
    """dt внутри азиатского диапазона (UTC 00:00–04:00)?"""
    return bool(active_windows(dt, ASIA_RANGE))
