import numpy as np
import pandas as pd
import pytest

from app.delivery import market_card as mc


def _bars(closes):
    c = pd.Series(closes, dtype=float)
    return pd.DataFrame({"open": c.shift().fillna(c.iloc[0]), "high": c * 1.01,
                         "low": c * 0.99, "close": c, "volume": 1.0})


def _zigzag(start, step, n):
    """Тренд «два шага по ходу, один против»: у монотонного ряда нет убыточных баров
    и RSI вырождается в 50 — здесь откаты есть, а направление однозначно."""
    out, x = [], start
    for i in range(n):
        x += step if i % 3 != 2 else -step / 2
        out.append(x)
    return out


@pytest.mark.parametrize("q,want", [
    ("btc", "BTC/USDT:USDT"), ("#BTC", "BTC/USDT:USDT"), ("$sol", "SOL/USDT:USDT"),
    ("btcusdt", "BTC/USDT:USDT"), ("BTC/USDT", "BTC/USDT:USDT"),
    ("BTC/USDT:USDT", "BTC/USDT:USDT"), ("eth-usdt", "ETH/USDT:USDT"),
    ("1000pepe", "1000PEPE/USDT:USDT"), ("1inch", "1INCH/USDT:USDT"),
])
def test_parse_coin_ok(q, want):
    assert mc.parse_coin(q) == want


@pytest.mark.parametrize("q", ["", "b", "!!!", "123", "привет", "x" * 20, "usdt"])
def test_parse_coin_rejects(q):
    assert mc.parse_coin(q) is None


def test_card_data_uptrend_and_render():
    h4 = _bars(_zigzag(100, 1.0, 260))         # рост: цена > EMA50 > EMA200
    h1 = _bars(_zigzag(190, 0.5, 60))
    d = mc.card_data("BTC/USDT:USDT", h1, h4)
    assert d["coin"] == "BTC"
    assert d["trend4h"] == "восходящий"
    assert d["chg24"] > 0
    assert d["rsi4h"] >= 70
    text = mc.render_card(d)
    assert "BTC/USDT" in text and "перекуплен" in text
    assert "Это не сигнал" in text and "@rsirange_bot" in text
    # граница публикации: в карточке нет входа/стопа/тейков
    for word in ("вход", "Стоп:", "тейк", "TP", "SL"):
        assert word not in text


def test_card_data_downtrend():
    h4 = _bars(_zigzag(400, -1.0, 260))
    h1 = _bars(_zigzag(300, -0.5, 60))
    d = mc.card_data("ETH/USDT:USDT", h1, h4)
    assert d["trend4h"] == "нисходящий"
    assert d["chg24"] < 0 and d["rsi1h"] <= 30


def test_card_data_needs_history():
    with pytest.raises(ValueError):
        mc.card_data("NEW/USDT:USDT", _bars([1.0] * 60), _bars([1.0] * 50))


def test_title_and_description_are_plain_text():
    h4 = _bars(np.linspace(100, 200, 260))
    h1 = _bars(np.linspace(190, 200, 60))
    d = mc.card_data("BTC/USDT:USDT", h1, h4)
    assert "<" not in mc.card_title(d) and "<" not in mc.card_description(d)
    assert mc.card_title(d).startswith("BTC · 200")
