"""Тесты временных фильтров SMC (килзоны/сессии/Macros) + MT и корреляций."""
from __future__ import annotations

from datetime import datetime, timezone

from app.brain.smc_detectors import CORRELATIONS, Bias, Zone, mean_threshold
from app.brain.smc_time import active_windows, in_asia_range, in_killzone


def _utc(h, m):
    return datetime(2026, 1, 5, h, m, tzinfo=timezone.utc)


def test_silver_bullet_killzones():
    assert in_killzone(_utc(8, 30)) is True        # SB London (UTC 8–9)
    assert in_killzone(_utc(15, 30)) is True        # SB New York (UTC 15–16)
    assert in_killzone(_utc(19, 30)) is True        # SB PM NY (UTC 19–20)
    assert in_killzone(_utc(12, 0)) is False        # вне килзон
    assert active_windows(_utc(15, 30), )  # непустой список
    assert "SB New York" in active_windows(_utc(15, 30))


def test_macros_windows():
    assert "Macro 12:50" in active_windows(_utc(13, 0))
    assert "Macro 19:15" in active_windows(_utc(19, 30))
    assert active_windows(_utc(16, 30)) == []       # между окнами


def test_asia_range():
    assert in_asia_range(_utc(2, 0)) is True         # UTC 00:00–04:00
    assert in_asia_range(_utc(5, 0)) is False


def test_naive_datetime_treated_as_utc():
    assert in_killzone(datetime(2026, 1, 5, 8, 30)) is True


def test_mean_threshold():
    z = Zone("OB", top=110.0, bottom=100.0, index_start=0, index_end=3, bias=Bias.BULL)
    assert mean_threshold(z) == 105.0
    assert mean_threshold(z, level=0.7) == 107.0


def test_correlations_table():
    assert CORRELATIONS[("EURUSD", "DXY")] == (0.92, "inverse")
    assert CORRELATIONS[("BTCUSDT", "ETHUSDT")][1] == "direct"
    assert CORRELATIONS[("US500", "US100")][0] == 0.83
