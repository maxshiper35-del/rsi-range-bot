"""«💼 Мой счёт»: математика виртуального счёта (сложный процент, дефолтный депозит, отбор
сделок по приходу юзера и публичности, окно «вчера»), утренний пуш и его опт-аут."""
from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest

from app.delivery import my_account as M

_NOW = datetime(2026, 10, 30, 6, 0, tzinfo=timezone.utc)
_JOIN = "2026-10-28T09:30:00.123456+00:00"
_ID = itertools.count(1)


def _row(r: float, closed: str, created: str = "2026-10-28T10:00:00+00:00",
         status: str | None = None, sym: str = "BTC/USDT:USDT") -> dict:
    return {"id": next(_ID), "symbol": sym, "side": "LONG", "timeframe": "1h",
            "status": status or ("tp1" if r > 0 else "stopped"), "result_r": r,
            "created_at": created, "closed_at": closed}


def _acc(rows, start=1000.0, risk=1.0, joined=_JOIN, now=_NOW):
    return M.compute_account(rows, start_usd=start, risk_pct=risk, joined_at=joined, now=now)


def test_compounding_applies_trades_in_closing_order():
    rows = [_row(0.5, "2026-10-29T12:00:00+00:00"),       # закрылась последней
            _row(2.0, "2026-10-28T12:00:00+00:00"),
            _row(-1.0, "2026-10-29T08:00:00+00:00")]
    acc = _acc(rows)
    assert acc.balance == pytest.approx(1000 * 1.02 * 0.99 * 1.005)
    assert [s.r for s in acc.steps] == [2.0, -1.0, 0.5]            # порядок закрытий
    assert acc.steps[0].usd == pytest.approx(20.0)                 # +2R от $1 000 = +$20
    assert acc.steps[1].usd == pytest.approx(-10.2)                # −1R уже от $1 020
    assert acc.pnl_usd == pytest.approx(acc.balance - 1000)
    assert acc.pnl_pct == pytest.approx((acc.balance / 1000 - 1) * 100)
    assert (acc.trades, acc.wins, acc.losses) == (3, 2, 1)


def test_positive_stop_counts_as_win_and_risk_scales():
    """Трейлинг-стоп в плюсе (stopped, R>0) — это плюс, как и в journal.stats."""
    acc = _acc([_row(1.5, "2026-10-29T01:00:00+00:00", status="stopped")], start=500, risk=2)
    assert acc.wins == 1 and acc.balance == pytest.approx(500 * 1.03)


def test_trades_created_before_join_are_excluded():
    """Сигнал, опубликованный ДО /start, юзер не видел — даже если закрылся после."""
    before = _row(3.0, "2026-10-29T10:00:00+00:00", created="2026-10-28T09:00:00+00:00")
    after = _row(-1.0, "2026-10-29T11:00:00+00:00", created="2026-10-28T09:31:00+00:00")
    acc = _acc([before, after])
    assert acc.trades == 1 and acc.balance == pytest.approx(990.0)


def test_shadow_open_and_expired_excluded():
    rows = [_row(2.0, "2026-10-29T01:00:00+00:00", status="rejected"),   # тень
            _row(1.0, "2026-10-29T02:00:00+00:00", status="expired"),    # лимитка не взята
            {**_row(1.0, "2026-10-29T03:00:00+00:00"), "result_r": None},
            {**_row(1.0, "2026-10-29T04:00:00+00:00", status="open"), "closed_at": None},
            _row(1.0, "2026-10-29T05:00:00+00:00")]
    acc = _acc(rows)
    assert acc.trades == 1 and acc.balance == pytest.approx(1010.0)


def test_yesterday_and_today_windows_are_utc_days():
    rows = [_row(1.0, "2026-10-28T23:59:59+00:00"),       # позавчера
            _row(2.0, "2026-10-29T00:00:00+00:00"),       # вчера — граница включительно
            _row(-1.0, "2026-10-29T23:59:59+00:00"),      # вчера
            _row(1.0, "2026-10-30T00:00:00+00:00")]       # сегодня
    acc = _acc(rows)
    y = acc.yesterday
    assert y.day == "2026-10-29" and (y.trades, y.wins, y.losses) == (2, 1, 1)
    before_y = 1000 * 1.01
    assert y.pnl_usd == pytest.approx(before_y * 1.02 * 0.99 - before_y)   # со сложным %
    assert acc.today.trades == 1 and acc.today.day == "2026-10-30"
    assert [d.day for d in M.daily(acc)] == ["2026-10-28", "2026-10-29", "2026-10-30"]


def test_max_drawdown_from_peak():
    rows = [_row(1.0, "2026-10-28T11:00:00+00:00"), _row(-1.0, "2026-10-28T12:00:00+00:00"),
            _row(-1.0, "2026-10-28T13:00:00+00:00"), _row(3.0, "2026-10-28T14:00:00+00:00")]
    acc = _acc(rows, risk=10)
    # 1000 -> 1100 (пик) -> 990 -> 891 (дно) -> 1158.3: откат (1100 − 891) / 1100
    assert acc.max_dd_pct == pytest.approx((1100 - 891) / 1100 * 100)
    assert acc.balance == pytest.approx(1158.3)


def test_fmt_usd_and_pct():
    assert M.fmt_usd(1000) == "$1 000"
    assert M.fmt_usd(1043.4) == "$1 043"
    assert M.fmt_usd(23.4, signed=True) == "+$23"
    assert M.fmt_usd(-30, signed=True) == "−$30"
    assert M.fmt_usd(3.123, signed=True) == "+$3.12"
    assert M.fmt_usd(0, signed=True) == "$0"
    assert M.fmt_pct(4.32) == "+4.3%" and M.fmt_pct(-3.9) == "−3.9%" and M.fmt_pct(0) == "0.0%"


def test_render_push_text_and_silence_on_empty_day():
    rows = [_row(2.0, "2026-10-29T03:00:00+00:00"), _row(1.0, "2026-10-29T05:00:00+00:00"),
            _row(-1.0, "2026-10-29T07:00:00+00:00")]
    txt = M.render_push(_acc(rows))
    assert txt is not None
    assert "Твой счёт за вчера: <b>+$20</b> (2 ✅ / 1 🛑)" in txt
    assert "с 28.10: $1 000 → <b>$1 020</b>" in txt
    assert "не реальный счёт" in txt and "Не финансовый совет" in txt
    # вчера ничего не закрылось -> не шлём вовсе; юзер неизвестен -> тоже
    assert M.render_push(_acc([_row(2.0, "2026-10-28T20:00:00+00:00")])) is None
    assert M.render_push(None) is None


def test_render_push_minus_day_is_shown_honestly():
    rows = [_row(-1.0, "2026-10-29T03:00:00+00:00"), _row(-1.0, "2026-10-29T05:00:00+00:00")]
    txt = M.render_push(_acc(rows, start=500, risk=2))
    assert "<b>−$20</b> (0 ✅ / 2 🛑)" in txt


def test_render_account_texts():
    acc = _acc([_row(2.0, "2026-10-29T03:00:00+00:00")])
    txt = M.render_account(acc)
    assert "$1 000 → <b>$1 020</b>" in txt and "+2.0%" in txt
    assert M._DISCLAIMER in txt and "Вчера: <b>+$20</b>" in txt
    default = M.compute_account([], start_usd=1000, risk_pct=1, joined_at=_JOIN, now=_NOW,
                                is_default=True)
    empty = M.render_account(default)
    assert "ещё не закрылось" in empty and "/depo 500" in empty and M._DISCLAIMER in empty
    for leak in ("6226", "LTF", "1:1", "SMC", "setup"):
        assert leak not in txt + empty
    assert len(txt) < 1024                    # подпись к PNG: лимит Telegram на caption


def test_push_due_window_and_once_per_day():
    day = "2026-10-29"
    assert M.push_due(datetime(2026, 10, 30, 5, 59, tzinfo=timezone.utc), None) is None
    assert M.push_due(_NOW, None) == day
    assert M.push_due(_NOW, "2026-10-28") == day
    assert M.push_due(_NOW, day) is None                       # уже разослано сегодня
    assert M.push_due(datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc), None) is None


def test_to_payload_axis_strictly_ascending_and_no_levels():
    rows = [_row(1.0, "2026-10-29T21:00:00+00:00"), _row(-1.0, "2026-10-29T21:00:00+00:00"),
            _row(2.0, "2026-10-29T22:00:00+00:00")]
    p = M.to_payload(_acc(rows), push=True)
    ts = [t for t, _ in p["eq"]]
    assert ts == sorted(set(ts)) and len(p["eq"]) == 3          # старт + 2 уникальных момента
    assert p["eq"][-1][1] == p["balance"] and p["eq"][0][1] == 1000
    assert p["items"][0]["r"] == 2.0                           # свежие сверху
    assert {"entry", "stop", "tp1", "setup"}.isdisjoint(p["items"][0])
    assert p["since"] == "2026-10-28" and p["push"] is True


# ------------------------------ БД ------------------------------
_SEQ = itertools.count(1)


def _db_signal(status: str, r: float | None, created: str, closed: str | None) -> None:
    from app import db
    n = next(_SEQ)
    db.execute(
        "INSERT INTO signals(symbol, side, timeframe, entry, stop, risk_reward, confidence, "
        "snapshot_json, status, created_at, closed_at, result_r) VALUES "
        "(?, 'LONG', '1h', 1, 0.9, 2.0, 0.7, '{}', ?, ?, ?, ?)",
        (f"C{n}/USDT:USDT", status, created, closed, r))


def test_account_for_reads_db_and_respects_join_and_publicity(fresh_db):
    from app import db
    from app.delivery import payments
    payments.register_user(4242, "u")
    db.execute("UPDATE users SET joined_at=? WHERE tg_id=4242", (_JOIN,))
    _db_signal("tp1", 2.0, "2026-10-28T08:00:00+00:00", "2026-10-29T01:00:00+00:00")  # до прихода
    _db_signal("tp1", 1.0, "2026-10-28T10:00:00+00:00", "2026-10-29T02:00:00+00:00")
    _db_signal("rejected", 3.0, "2026-10-28T11:00:00+00:00", "2026-10-29T03:00:00+00:00")
    _db_signal("expired", None, "2026-10-28T12:00:00+00:00", "2026-10-29T04:00:00+00:00")
    _db_signal("open", None, "2026-10-28T13:00:00+00:00", None)
    _db_signal("stopped", -1.0, "2026-10-29T10:00:00+00:00", "2026-10-29T15:00:00+00:00")
    acc = M.account_for(4242, now=_NOW)
    assert acc is not None and acc.is_default and acc.start == 1000 and acc.risk_pct == 1
    assert acc.trades == 2 and acc.balance == pytest.approx(1000 * 1.01 * 0.99)
    assert acc.yesterday.trades == 2
    payments.set_depo(4242, 500.0, 2.0)                       # /depo 500 2
    acc2 = M.account_for(4242, now=_NOW)
    assert (acc2.start, acc2.risk_pct, acc2.is_default) == (500.0, 2.0, False)
    assert acc2.balance == pytest.approx(500 * 1.02 * 0.98)
    assert M.account_for(999999) is None                       # не нажимал /start


def test_push_recipients_opt_out_and_blocked(fresh_db):
    from app.delivery import payments
    for uid in (1, 2, 3):
        payments.register_user(uid)
    assert M.push_recipients() == [1, 2, 3] and M.push_enabled(1)  # по умолчанию включён
    M.set_push(2, False)                                       # /account off
    payments.mark_blocked(3)                                   # заблокировал бота
    assert M.push_recipients() == [1]
    assert M.push_enabled(2) is False
    M.set_push(2, True)
    assert M.push_recipients() == [1, 2]


def test_account_push_master_switch_default_off(fresh_db):
    """30.10: утренний пуш всем — решение владельца; по умолчанию выкл, включает /account push on.
    Mini App получает push_on и прячет переключатель, пока рубильник выкл."""
    from datetime import datetime, timezone

    from app import runtime
    from app.delivery import my_account, payments
    assert runtime.account_push_enabled() is False
    payments.register_user(1)
    acc = my_account.account_for(1, now=datetime(2026, 10, 30, 7, tzinfo=timezone.utc))
    assert acc is not None
    assert my_account.to_payload(acc, True)["push_on"] is False
    runtime.set_value("account_push_enabled", 1)
    assert runtime.account_push_enabled() is True
    assert my_account.to_payload(acc, True)["push_on"] is True
