import json

import pandas as pd

from app.delivery import replay


def _bars(prices):
    """1h-бары от 2026-01-01 00:00: (open, high, low, close) по списку."""
    dts = pd.date_range("2026-01-01", periods=len(prices), freq="1h", tz="UTC")
    o, h, lw, c = zip(*prices)
    return pd.DataFrame({"dt": dts, "open": o, "high": h, "low": lw, "close": c})


def _row(**kw):
    row = {"id": 7, "symbol": "ETH/USDT:USDT", "side": "LONG", "timeframe": "1h",
           "entry": 100.0, "stop": 99.0, "created_at": "2026-01-01T00:00:00+00:00",
           "filled_at": None, "closed_at": None, "result_r": 0.0, "exit_price": None,
           "snapshot_json": json.dumps({"setup": "6226", "scores": {"bias": 1},
                                        "trail": {"activate_r": 1.5, "distance_r": 0.2}})}
    row.update(kw)
    return row


def _long_win():
    flat = [(101.0, 101.5, 100.5, 101.0)] * 40              # контекст выше входа
    trade = [(100.8, 101.0, 99.8, 100.2),                   # 40: бар решения+1 — касание входа
             (100.2, 101.0, 100.1, 100.9),                  # 41
             (100.9, 102.2, 100.8, 102.0),                  # 42: +2.2R — трейл включается
             (102.0, 102.1, 101.9, 102.0),                  # 43: стоп = 102.2-0.2 = 102.0
             (101.9, 101.95, 101.5, 101.6)]                 # 44: выход по трейлу
    tail = [(101.6, 101.8, 101.4, 101.5)] * 5
    bars = _bars(flat + trade + tail)
    row = _row(created_at=str(bars["dt"].iloc[39]))   # сигнал на баре 39 -> решение 40:00
    row["filled_at"] = str(bars["dt"].iloc[40] + pd.Timedelta(minutes=20))
    row["closed_at"] = str(bars["dt"].iloc[44])
    row["result_r"], row["exit_price"] = 2.0, 102.0
    return row, bars


def test_plan_indices_stages_and_trail():
    row, bars = _long_win()
    plan = replay.replay_plan(row, bars)
    assert plan["start"] == replay.CONTEXT_BARS            # 36 баров контекста до решения
    assert plan["fill_i"] == plan["start"] and plan["exit_i"] == plan["start"] + 4
    stages = [f["stage"] for f in plan["frames"]]
    assert stages[0] == "open" and stages[-1] == "exit" and "trail" in stages
    # трейл действует со следующего бара: на баре выхода стоп = пик − 0.2R = 102.0
    assert plan["frames"][-2]["stop"] == 102.0
    assert plan["frames"][-1]["r"] == 2.0
    assert plan["end"] == plan["exit_i"] + 1 + replay.TAIL_BARS


def test_best_move_ignores_fill_bar():
    row, bars = _long_win()
    plan = replay.replay_plan(row, bars)
    assert plan["frames"][0]["best"] is None               # бар входа: порядок хай/лоу неизвестен
    assert round(plan["frames"][2]["best"], 2) == 2.2


def test_long_trade_compressed_to_frame_cap():
    flat = [(101.0, 101.5, 100.5, 101.0)] * 40
    walk = [(100.0 + i * 0.01, 100.5 + i * 0.01, 99.6 + i * 0.01, 100.0 + i * 0.01)
            for i in range(200)]
    bars = _bars(flat + walk)
    row = _row(created_at=str(bars["dt"].iloc[39]),
               filled_at=str(bars["dt"].iloc[40]), closed_at=str(bars["dt"].iloc[235]),
               result_r=1.0)
    plan = replay.replay_plan(row, bars)
    assert len(plan["frames"]) <= replay.MAX_REPLAY_FRAMES + 1
    assert plan["frames"][-1]["stage"] == "exit"


def test_frame_html_no_setup_taxonomy_and_honest_labels():
    row, bars = _long_win()
    plan = replay.replay_plan(row, bars)
    html = replay.build_frame_html(row, plan, plan["frames"][-1], outro=1.0)
    for leak in ("6226", "LTF", "1:1", "SMC", "bias", "scores"):
        assert leak not in html                            # таксономия сетапов закрыта
    assert "до комиссий" in html and "не финансовый совет" in html
    assert "+2.00R" in html and "Итог сделки #7" in html
    loss = dict(row, result_r=-1.0)
    lplan = replay.replay_plan(loss, bars)
    assert "−1.00R" in replay.build_frame_html(loss, lplan, lplan["frames"][-1], outro=1.0)


def test_ffmpeg_found_without_homebrew_in_path(monkeypatch):
    # launchd даёт PATH=/usr/bin:/bin — which() не видит Homebrew, путь ищем сами
    import shutil
    from pathlib import Path

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(Path, "exists", lambda self: str(self) == "/opt/homebrew/bin/ffmpeg")
    assert replay._ffmpeg() == "/opt/homebrew/bin/ffmpeg"
