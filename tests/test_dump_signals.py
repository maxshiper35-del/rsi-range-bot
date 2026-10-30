"""Паритет снимка сигналов с продом: старший ТФ — только закрытые свечи, гейты движка
до выбора кандидата (scripts/dump_signals.py, 06.10.2026)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.brain.smc_setups import SMCConfig, SMCEngine
from scripts import dump_signals


def _bars(n: int, minutes: int, start: str = "2024-01-01") -> pd.DataFrame:
    dt = pd.date_range(start, periods=n, freq=f"{minutes}min", tz="UTC")
    px = 100 + np.sin(np.arange(n) / 5.0)
    return pd.DataFrame({"ts": (dt - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(milliseconds=1),
                         "open": px, "high": px + 0.5, "low": px - 0.5, "close": px,
                         "volume": 1.0, "dt": dt})


class _SpyEngine:
    """Запоминает, какой HTF-фрейм видел движок на каждом баре."""
    seen: list = []
    TF_MINUTES = SMCEngine.TF_MINUTES

    def __init__(self, cfg):
        self.cfg = cfg

    def analyze_single(self, df, symbol, tf, htf_df=None, partner_df=None):
        _SpyEngine.seen.append((df["ts"].iloc[-1], None if htf_df is None else htf_df.copy()))
        return None


def test_htf_bars_only_closed_candles(monkeypatch):
    h1, h4 = _bars(400, 60), _bars(100, 240)
    _SpyEngine.seen = []
    monkeypatch.setattr(dump_signals, "SMCEngine", _SpyEngine)
    dump_signals.scan_signals(h1, "X/USDT:USDT", "1h", SMCConfig(), 199, "4h", htf_bars=h4)
    checked = 0
    for last_ts, htf in _SpyEngine.seen:
        if htf is None:
            continue
        decision = last_ts + 3_600_000                         # закрытие текущего 1h-бара
        closes = htf["ts"] + 4 * 3_600_000
        assert (closes <= decision).all()                      # ни одной формирующейся свечи
        later = h4[(h4.ts + 4 * 3_600_000 <= decision) & (~h4.ts.isin(htf.ts))]
        assert later.empty or later.ts.max() < htf.ts.min()    # взяты ПОСЛЕДНИЕ закрытые
        assert len(htf) <= 199
        checked += 1
    assert checked > 300


def test_htf_bars_include_candle_closed_exactly_at_decision(monkeypatch):
    h1, h4 = _bars(400, 60), _bars(100, 240)
    _SpyEngine.seen = []
    monkeypatch.setattr(dump_signals, "SMCEngine", _SpyEngine)
    dump_signals.scan_signals(h1, "X/USDT:USDT", "1h", SMCConfig(), 199, "4h", htf_bars=h4)
    # 1h-бар 03:00–04:00 закрывается вместе с 4h-свечой 00:00–04:00 — она уже видна
    for last_ts, htf in _SpyEngine.seen:
        if htf is not None and (last_ts + 3_600_000) % (4 * 3_600_000) == 0:
            assert htf["ts"].iloc[-1] + 4 * 3_600_000 == last_ts + 3_600_000
            break
    else:
        raise AssertionError("не нашли бар на границе 4h")


def test_prod_config_mirrors_runtime(monkeypatch, fresh_db):
    # fresh_db: _prod_config читает и не подменённые здесь настройки runtime из БД — без
    # фикстуры тест зависел от data/signals.db в cwd (боевой БД в основном чекауте)
    from app import runtime
    monkeypatch.setattr(runtime, "enabled_setups", lambda: ("6226", "LTF"))
    monkeypatch.setattr(runtime, "min_stop_pct", lambda: 1.3)
    monkeypatch.setattr(runtime, "time_filter_enabled", lambda: True)
    monkeypatch.setattr(runtime, "htf_block_countertrend", lambda: True)
    args = dump_signals.parse_args(["--prod", "--symbols", "X", "--tfs", "4h,1h"])
    cfg, htf_map = dump_signals._prod_config(args)
    assert cfg.enabled_setups == ("6226", "LTF") and cfg.min_stop_pct == 1.3
    assert cfg.use_time_filter and cfg.htf_block_countertrend
    assert htf_map == {"1h": "4h", "4h": None}                # как analyze_multi
    assert args.window == 199                                 # limit=200 минус формирующийся


def test_prod_config_set_overrides(monkeypatch, fresh_db):
    from app import runtime
    monkeypatch.setattr(runtime, "enabled_setups", lambda: ("6226", "LTF"))
    monkeypatch.setattr(runtime, "min_stop_pct", lambda: 1.3)
    args = dump_signals.parse_args(["--prod", "--symbols", "X", "--set", "min_stop_pct=1.0",
                                    "--set", "enabled_setups=6226,1:1,LTF",
                                    "--set", "htf_block_countertrend=false"])
    cfg, _ = dump_signals._prod_config(args)
    assert cfg.min_stop_pct == 1.0 and cfg.enabled_setups == ("6226", "1:1", "LTF")
    assert cfg.htf_block_countertrend is False
