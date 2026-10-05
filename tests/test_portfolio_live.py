"""Режим живого судьи в портфельном симуляторе (аудит 05.10): исход = journal._outcome_from_bars
на 1h-барах. Стоп на баре филла засчитывается (−1R), старая модель его пропускала."""
from __future__ import annotations

import pandas as pd

from app.sim.portfolio import simulate_portfolio


def _bars():
    t0 = pd.Timestamp("2026-01-01T00:00:00Z")
    rows = [(101, 101.5, 100.5, 101)] * 6            # 00:00..05:00 — до сигнала
    rows += [(101, 101.2, 97.5, 98.5)]               # 06:00: касание входа 100 И стопа 98
    rows += [(99, 107, 98.6, 106.5)] * 3             # дальше ралли выше тейка 106
    rows += [(106, 106.5, 105.5, 106)] * 300
    dt = [t0 + pd.Timedelta(hours=i) for i in range(len(rows))]
    o, h, low, c = zip(*rows)
    return pd.DataFrame({"dt": dt, "open": o, "high": h, "low": low, "close": c,
                         "volume": [1.0] * len(rows)})


def _sig():
    # решающий бар 05:00 (open) -> закрыт в 06:00; первый бар после решения — 06:00
    return {"symbol": "AAA/USDT:USDT", "tf": "1h", "bar_index": 5,
            "dt": "2026-01-01 05:00:00+00:00", "side": "LONG", "setup": "X",
            "entry": 100.0, "stop": 98.0, "tp": 106.0, "rr": 3.0, "confidence": 0.7}


def test_live_judge_counts_fill_bar_stop():
    df = _bars()
    bars = {("AAA/USDT:USDT", "1h"): df}
    kw = dict(fee_pct=0.0, slippage_pct=0.0, funding_bps_per_bar=0.0,
              trail_activate_r=1.5, trail_distance_r=0.2)
    legacy = simulate_portfolio(bars, [_sig()], **kw)
    live = simulate_portfolio(bars, [_sig()], live_judge_bars={"AAA/USDT:USDT": df},
                              tp_exit=False, max_hold_hours=168.0, **kw)
    assert legacy.trades == 1 and legacy.total_r > 0          # старая модель: бар филла пропущен
    assert live.trades == 1 and live.total_r == -1.0          # живой судья: стоп на баре филла
    assert live.detail[0].outcome.endswith("stop")            # тормоз стороны его считает
