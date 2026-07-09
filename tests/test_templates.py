"""Тесты единого HTML-рендерера постов (app/delivery/templates.py)."""
from app.core.signal_engine import Side, Signal
from app.delivery import templates


def _sig(**kw):
    base = dict(symbol="BTC/USDT:USDT", side=Side.SHORT, timeframe="15m", entry=62340.0,
                stop=62980.0, take_profits=[60893.0, 59000.0], risk_reward=2.26,
                confidence=0.65, reasons=["свип хая", "слом CHOCH"],
                created_at="2026-01-01T00:00:00+00:00", indicators_snapshot={"setup": "6226"})
    base.update(kw)
    return Signal(**base)


def _closed(**kw):
    base = {"id": 5, "symbol": "ETH/USDT:USDT", "side": "LONG", "timeframe": "1h",
            "status": "tp1", "result_r": 2.26, "shadow": False, "setup": "1:1",
            "entry": 100.0, "exit_price": 101.0, "price_move_pct": 1.0,
            "filled_at": "2026-01-01T01:00:00+00:00"}
    base.update(kw)
    return base


def test_price_full_precision():
    # {:g} режет BTC>100k до 6 значащих цифр и даёт экспоненту на мелких альтах —
    # _price обязан отдавать полную запись
    assert templates._price(104250.55) == "104250.55"
    assert templates._price(0.00012345) == "0.00012345"
    assert templates._price(67450.0) == "67450"


def test_render_signal_channel_html():
    txt = templates.render_signal_channel(_sig(), 7)
    assert "<b>RSI RANGE — Сигнал #7</b>" in txt and "🔴 SHORT" in txt
    assert "<code>62340</code>" in txt and "<code>60893</code> / <code>59000</code>" in txt
    assert "<blockquote expandable>" in txt and "свип хая" in txt
    assert "Не финсовет" in txt and "@rsirange_bot" in txt


def test_render_signal_escapes_injection():
    sig = _sig(reasons=["<b>инъекция</b> & прочее"])
    for txt in (templates.render_signal_channel(sig, 1), templates.render_signal_dm(sig, 1)):
        assert "&lt;b&gt;инъекция&lt;/b&gt; &amp; прочее" in txt
        assert "<b>инъекция" not in txt


def test_render_signal_dm():
    txt = templates.render_signal_dm(_sig(confidence=0.8), 3)
    assert "<b>СИГНАЛ #3</b>" in txt
    assert "▰▰▰▰▰▰▰▰▱▱ 80%" in txt
    assert "TP1: <code>60893</code>" in txt and "TP2: <code>59000</code>" in txt
    assert "<blockquote expandable>" in txt
    assert "Не финансовый совет" in txt


def test_render_teaser_hides_levels():
    txt = templates.render_teaser(_sig(), 9)
    assert "#9" in txt and "по подписке" in txt
    assert "62340" not in txt and "62980" not in txt   # ни входа, ни стопа


def test_render_fill():
    txt = templates.render_fill(_closed(status="filled"), show_price=True)
    assert "вход исполнен" in txt and "#5" in txt and "<code>100</code>" in txt
    hidden = templates.render_fill(_closed(status="filled"), show_price=False)
    assert "100" not in hidden                          # teaser-канал: цену не раскрываем


def test_render_expired():
    txt = templates.render_expired(_closed(status="expired", result_r=None))
    assert "отменён" in txt and "сделки не было" in txt and "#5" in txt


def test_render_result_with_display_leverage():
    txt = templates.render_result(_closed(), display_leverage=10.0)
    assert "+2.26R" in txt
    assert "При плече ×10: <b>+10.0%</b>" in txt       # 1.0% × 10
    assert "движение цены +1.00%" in txt
    assert "✅" in txt and "TP1" in txt and "/stats" in txt


def test_render_result_leverage_off_and_stop():
    txt = templates.render_result(
        _closed(status="stopped", result_r=-1.0, price_move_pct=-0.8), display_leverage=0.0)
    assert "🛑" in txt and "-1.00R" in txt and "📉" in txt
    assert "плече" not in txt
    assert "Движение цены: -0.80%" in txt


def test_render_result_without_price_move():
    # старые записи без расширенных полей не роняют рендер
    c = _closed()
    del c["price_move_pct"]
    txt = templates.render_result(c, display_leverage=10.0)
    assert "+2.26R" in txt and "плече" not in txt
