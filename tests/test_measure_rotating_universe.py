import pandas as pd

from scripts import measure_rotating_universe as mru


def _turnover(rows):
    return pd.DataFrame([{"symbol": s, "ts": int(pd.Timestamp(d, tz="UTC").timestamp() * 1000),
                          "turnover": t} for d, s, t in rows])


def test_daily_top_uses_previous_day_and_threshold(monkeypatch):
    monkeypatch.setattr(mru, "TOP_N", 2)
    t = _turnover([("2025-01-01", "A", 5e7), ("2025-01-01", "B", 3e7),
                   ("2025-01-01", "C", 4e7), ("2025-01-01", "D", 5e6),   # ниже 10 млн
                   ("2025-01-02", "D", 9e7)])
    top = mru.daily_top(t)
    # оборот 1 января решает состав на 2 января — без заглядывания в день торговли
    assert top[pd.Timestamp("2025-01-02", tz="UTC")] == ["A", "C"]
    assert top[pd.Timestamp("2025-01-03", tz="UTC")] == ["D"]


def test_daily_top_dedups_same_name(monkeypatch):
    monkeypatch.setattr(mru, "TOP_N", 3)
    t = _turnover([("2025-01-01", "A", 5e7), ("2025-01-01", "A", 2e7),
                   ("2025-01-01", "B", 3e7)])
    assert mru.daily_top(t)[pd.Timestamp("2025-01-02", tz="UTC")] == ["A", "B"]


def test_rotate_gate_and_removal():
    d1, d2, d3 = (pd.Timestamp(f"2025-01-0{i}", tz="UTC") for i in (1, 2, 3))
    top = {d1: ["A", "B"], d2: ["A", "C", "B"], d3: ["C", "A"]}
    calls = []

    def gate(sym, day):
        calls.append((sym, day))
        return sym != "B"

    mem = mru.rotate(top, gate)
    assert mem[d1] == ["A"]                 # B не прошёл гейт
    assert mem[d2] == ["A", "C"]            # C допущен, B снова отклонён
    assert mem[d3] == ["C", "A"]            # действующие члены гейт не проходят повторно
    assert ("A", d2) not in calls and ("C", d3) not in calls
    assert mru.rotate(top, None)[d2] == ["A", "C", "B"]


def test_rotate_drops_symbol_out_of_top():
    d1, d2 = pd.Timestamp("2025-01-01", tz="UTC"), pd.Timestamp("2025-01-02", tz="UTC")
    mem = mru.rotate({d1: ["A", "B"], d2: ["B", "X"]}, lambda s, d: s != "X")
    assert mem[d2] == ["B"]                 # A выпал из топа -> удалён


def test_rotate_seed_skips_gate_while_in_top():
    d1, d2 = pd.Timestamp("2025-01-01", tz="UTC"), pd.Timestamp("2025-01-02", tz="UTC")
    mem = mru.rotate({d1: ["BTC", "Z"], d2: ["Z"]}, lambda s, d: False, seed=("BTC", "OLD"))
    assert mem[d1] == ["BTC"]               # стартовый член в топе остаётся без гейта
    assert mem[d2] == ["BTC"]               # пустой итог -> прежний список (как в проде)
