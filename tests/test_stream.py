"""Тесты реалтайм-стрима (app/data/stream.py): чистые функции + атомарный fill-маркер,
состав подписки (вотчлист + монеты открытых сигналов, #881) и обработчик кадров
bot.handle_stream_frame."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.data import stream

BTC, ETH, ICNT = "BTC/USDT:USDT", "ETH/USDT:USDT", "ICNT/USDT:USDT"


def test_ws_symbol_mapping():
    assert stream.ws_symbol("BTC/USDT:USDT") == "BTCUSDT"
    assert stream.ws_symbol("1000PEPE/USDT:USDT") == "1000PEPEUSDT"
    assert stream.ws_symbol("XAU/USDT:USDT") == "XAUUSDT"


def test_bybit_interval_mapping():
    assert stream.bybit_interval("1h") == "60"
    assert stream.bybit_interval("4h") == "240"
    assert stream.bybit_interval("15m") == "15"
    with pytest.raises(ValueError):
        stream.bybit_interval("3m")


def test_parse_kline():
    raw = json.dumps({"topic": "kline.60.BTCUSDT", "data": [
        {"start": 1, "open": "100", "high": "105.5", "low": "99.1",
         "close": "104", "confirm": False}]})
    evts = stream.parse_kline(raw)
    assert len(evts) == 1
    e = evts[0]
    assert e.symbol_ws == "BTCUSDT" and e.high == 105.5 and e.low == 99.1
    assert e.confirm is False
    # не-kline и мусор -> пусто, без исключений
    assert stream.parse_kline(json.dumps({"op": "pong"})) == []
    assert stream.parse_kline("не json вовсе") == []
    assert stream.parse_kline(json.dumps({"topic": "tickers.BTCUSDT", "data": []})) == []


def test_entry_touched():
    assert stream.entry_touched(100.0, 105.0, 99.0) is True
    assert stream.entry_touched(100.0, 100.0, 100.0) is True    # ровно касание
    assert stream.entry_touched(100.0, 99.9, 99.0) is False
    assert stream.entry_touched(100.0, 105.0, 100.1) is False


def test_try_mark_filled_atomic(fresh_db):
    """Гонка стрим vs bar-скоринг: выигрывает ровно один."""
    from app.brain import journal
    from tests.test_journal import _sig

    sid = journal.record_signal(_sig())
    rows = journal.unfilled_open_signals("BTC/USDT:USDT")
    assert [r["id"] for r in rows] == [sid]
    assert journal.try_mark_filled(sid, "2026-07-11T00:00:00+00:00") is True
    assert journal.try_mark_filled(sid, "2026-07-11T00:01:00+00:00") is False  # второй молчит
    assert journal.unfilled_open_signals("BTC/USDT:USDT") == []
    row = fresh_db.query_one("SELECT filled_at FROM signals WHERE id=?", (sid,))
    assert row["filled_at"] == "2026-07-11T00:00:00+00:00"      # выигравший, не второй


# --- #881: топики = вотчлист + монеты открытых публичных сигналов ---------------------------
# (docs/research/2026-11-10-trail-activation-881.md)
def test_extra_symbols_adds_only_symbols_outside_watchlist():
    """Вотчлист в подписке целиком и без дублей: монета вотчлиста с открытым сигналом в
    добавку не идёт. Порядок добавки — тот, в котором монеты отдал журнал."""
    watchlist = [BTC, ETH]
    extra, skipped = stream.extra_symbols(watchlist, ["SOL/USDT:USDT", ETH, ICNT, BTC])
    assert extra == ["SOL/USDT:USDT", ICNT] and skipped == []
    assert watchlist == [BTC, ETH]                              # вход не мутируется
    # сравнение по топику Bybit: та же монета в другой записи — не вторая подписка
    assert stream.extra_symbols([BTC], ["BTC/USDT", ICNT, ICNT]) == ([ICNT], [])
    assert stream.extra_symbols([BTC, ETH], []) == ([], [])     # открытых сигналов нет
    assert stream.extra_symbols([], [ICNT]) == ([ICNT], [])     # пустой вотчлист — не помеха


def test_extra_symbols_cap_keeps_freshest_in_stable_order():
    """Потолок: сверх вотчлиста не больше EXTRA_SYMBOLS_MAX монет — первые по порядку журнала,
    порядок детерминирован; остаток возвращается отдельно (он остаётся без стрима)."""
    assert stream.EXTRA_SYMBOLS_MAX == 10
    opened = [f"C{i:02d}/USDT:USDT" for i in range(13)]         # порядок журнала
    extra, skipped = stream.extra_symbols([BTC], opened[:5] + [BTC] + opened[5:])
    assert extra == opened[:10] and skipped == opened[10:]
    assert stream.extra_symbols([BTC], opened) == (extra, skipped)      # повтор — тот же ответ
    assert stream.extra_symbols([BTC], opened, cap=2) == (opened[:2], opened[2:])


def test_open_signal_symbols_only_public_open(fresh_db):
    """В добавку идут только ПУБЛИЧНЫЕ открытые сигналы — и ждущие входа, и в позиции.
    Тени, кандидаты, отклонённые, закрытые и истёкшие монету в топики не приводят."""
    from app.brain import journal
    from tests.test_journal import _sig

    def rec(fn, symbol, hour):
        return fn(_sig(symbol=symbol, created_at=f"2026-11-09T{hour:02d}:00:00+00:00"))

    waiting = rec(journal.record_signal, "WAIT/USDT:USDT", 1)           # ждёт входа
    in_pos = rec(journal.record_signal, "POS/USDT:USDT", 2)             # в позиции
    assert journal.try_mark_filled(in_pos, "2026-11-09T02:30:00+00:00")
    rec(journal.record_shadow, "SHADOW/USDT:USDT", 3)                   # тень (rejected)
    rec(journal.record_candidate, "CAND/USDT:USDT", 4)                  # кандидат semiauto
    assert journal.reject_candidate(rec(journal.record_candidate, "REJ/USDT:USDT", 5))
    closed = rec(journal.record_signal, "CLOSED/USDT:USDT", 6)
    fresh_db.execute("UPDATE signals SET status='stopped', result_r=-1.0 WHERE id=?", (closed,))
    expired = rec(journal.record_signal, "EXPIRED/USDT:USDT", 7)
    fresh_db.execute("UPDATE signals SET status='expired' WHERE id=?", (expired,))
    assert waiting < in_pos
    assert journal.open_signal_symbols() == ["POS/USDT:USDT", "WAIT/USDT:USDT"]
    # новый сигнал по старой монете: она в списке одна. Монета с позицией всё равно первая —
    # при потолке подписку теряют ждущие входа, а не те, кому нужна активация трейла;
    # среди ждущих — от свежего сигнала к старому
    rec(journal.record_signal, "WAIT/USDT:USDT", 8)
    rec(journal.record_signal, "NEW/USDT:USDT", 9)
    assert journal.open_signal_symbols() == ["POS/USDT:USDT", "NEW/USDT:USDT",
                                             "WAIT/USDT:USDT"]
    fresh_db.execute("UPDATE signals SET status='stopped', result_r=1.0 WHERE symbol=?",
                     ("NEW/USDT:USDT",))
    assert stream.extra_symbols(["POS/USDT:USDT"], journal.open_signal_symbols()) == (
        ["WAIT/USDT:USDT"], [])


def test_composition_line():
    assert stream.composition_line(12, [], []) == (
        "стрим: состав подписки — 12 топиков, сверх вотчлиста нет")
    assert stream.composition_line(14, [ICNT, "XPL/USDT:USDT"], []) == (
        "стрим: состав подписки — 14 топиков, сверх вотчлиста 2: "
        "ICNT/USDT:USDT, XPL/USDT:USDT")
    opened = [f"C{i:02d}/USDT:USDT" for i in range(12)]
    extra, skipped = stream.extra_symbols([BTC], opened)
    assert stream.composition_line(11, extra, skipped).endswith(
        "; без подписки (потолок 10): C10/USDT:USDT, C11/USDT:USDT")


class _FakeWS:
    """Соединение без сети: копит отправленное и отдаёт заготовленные кадры. Кадры
    кончились — отмена задачи, как при остановке бота."""

    def __init__(self, sent: list, frames: list):
        self._sent, self._frames = sent, frames

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send_json(self, payload):
        self._sent.append(payload)

    async def receive(self, timeout=None):
        import aiohttp
        if not self._frames:
            raise asyncio.CancelledError
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=self._frames.pop(0))


def _fake_bybit(monkeypatch, frames=()) -> list[list]:
    """Подменить соединение с Bybit. -> список подключений: запросы каждого по порядку."""
    import aiohttp

    connects: list[list] = []
    queue = list(frames)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def ws_connect(self, url, heartbeat=None):
            connects.append([])
            return _FakeWS(connects[-1], queue)

    monkeypatch.setattr(aiohttp, "ClientSession", _Session)
    return connects


def _reconnects(monkeypatch, caplog, steps, get_symbols, get_open_symbols):
    """Прогнать run_stream через len(steps) подключений подряд, без сети и ожиданий: перед
    каждым подключением выполняется свой шаг сценария. -> (запросы каждого подключения,
    строки лога стрима)."""
    connects = _fake_bybit(monkeypatch)
    monkeypatch.setattr(stream, "_RECONNECT_MAX_AGE", -1.0)     # соединение сразу «отжило»
    todo = iter(steps)

    def symbols():
        step = next(todo, None)
        if step is None:
            raise asyncio.CancelledError                        # сценарий кончился — стоп
        step()
        return get_symbols()

    log = logging.getLogger("stream-test")
    with caplog.at_level(logging.INFO, logger="stream-test"):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(stream.run_stream(symbols, lambda: "1h", None, lambda: True, log,
                                          get_open_symbols))
    return connects, [(r.levelname, r.getMessage()) for r in caplog.records
                      if r.name == "stream-test"]


def _topics(*symbols_ws):
    return {"op": "subscribe", "args": [f"kline.60.{s}" for s in symbols_ws]}


def test_symbol_with_open_signal_keeps_subscription_after_leaving_watchlist(
        fresh_db, monkeypatch, caplog):
    """#881: монета вышла из вотчлиста при открытом сигнале — подписка на неё не теряется
    ни на одном переподключении, пока сигнал открыт (ждёт входа или в позиции). Состав
    считается из БД при каждом подключении; строка о составе — только при его смене."""
    from app import runtime
    from app.brain import journal
    from tests.test_journal import _sig

    runtime.set_value("symbols", f"{BTC},{ICNT}")
    sid = journal.record_signal(_sig(symbol=ICNT))
    steps = [
        lambda: None,                                           # ICNT ещё в вотчлисте
        lambda: runtime.set_value("symbols", BTC),              # суточный пересмотр: вышла
        lambda: None,                                           # плановый reconnect, 15 минут
        lambda: journal.try_mark_filled(sid, "2026-01-01T03:00:00+00:00"),   # вход: в позиции
        lambda: fresh_db.execute(                               # сделка закрыта
            "UPDATE signals SET status='stopped', result_r=-1.0 WHERE id=?", (sid,)),
        lambda: None,
    ]
    connects, logs = _reconnects(monkeypatch, caplog, steps, runtime.symbols,
                                 journal.open_signal_symbols)
    kept = [_topics("BTCUSDT"), _topics("ICNTUSDT")]            # добавка — своим запросом
    assert connects == [[_topics("BTCUSDT", "ICNTUSDT")], kept, kept, kept,
                        [_topics("BTCUSDT")], [_topics("BTCUSDT")]]
    # прежняя строка подключения цела и считает все топики, вместе с добавкой
    assert [m for _, m in logs if m.startswith("стрим подключён")] == (
        ["стрим подключён: 2 топиков (60)"] * 4 + ["стрим подключён: 1 топиков (60)"] * 2)
    # строка о составе: старт, выход ICNT из вотчлиста (топиков столько же!), закрытие сделки
    assert [(lvl, m) for lvl, m in logs if "состав подписки" in m] == [
        ("INFO", "стрим: состав подписки — 2 топиков, сверх вотчлиста нет"),
        ("INFO", "стрим: состав подписки — 2 топиков, сверх вотчлиста 1: ICNT/USDT:USDT"),
        ("INFO", "стрим: состав подписки — 1 топиков, сверх вотчлиста нет"),
    ]


def test_stream_subscription_cap_and_watchlist_only_mode(monkeypatch, caplog):
    """Сверх вотчлиста — не больше 10 монет, остаток назван в логе предупреждением.
    Без источника открытых сигналов подписка прежняя: один запрос, только вотчлист."""
    opened = [f"C{i:02d}/USDT:USDT" for i in range(12)]
    connects, logs = _reconnects(monkeypatch, caplog, [lambda: None, lambda: None],
                                 lambda: [BTC, ETH], lambda: opened)
    first = connects[0]
    assert first[0] == _topics("BTCUSDT", "ETHUSDT")            # вотчлист целиком, как раньше
    assert first[1:] == [_topics(f"C{i:02d}USDT") for i in range(10)]
    assert connects[1] == first                                 # порядок стабилен
    assert logs.count(("INFO", "стрим подключён: 12 топиков (60)")) == 2
    assert [(lvl, m) for lvl, m in logs if "состав подписки" in m] == [
        ("WARNING", "стрим: состав подписки — 12 топиков, сверх вотчлиста 10: "
                    + ", ".join(opened[:10])
                    + "; без подписки (потолок 10): C10/USDT:USDT, C11/USDT:USDT")]

    caplog.clear()
    connects, logs = _reconnects(monkeypatch, caplog, [lambda: None], lambda: [BTC, ETH], None)
    assert connects == [[_topics("BTCUSDT", "ETHUSDT")]]
    assert logs == [("INFO", "стрим подключён: 2 топиков (60)"),
                    ("INFO", "стрим: состав подписки — 2 топиков, сверх вотчлиста нет")]


# --- #881: кадр монеты сверх вотчлиста — executor и реалтайм-вход да, прайс-алерты нет -------
@pytest.fixture()
def executor_state():
    """Кеши executor, которые трогает stream_tick, не переживают границы теста."""
    from app.v2 import bingx_executor as ex

    def _clear():
        ex.disarm_all()
        ex._STREAM_SEEN.clear()
        ex._ACT_TRIED.clear()
    _clear()
    yield ex
    _clear()


class _Frames:
    """bot.handle_stream_frame с записью всего, что он отдаёт наружу."""

    def __init__(self):
        self.last_price: dict[str, float] = {}
        self.trails: list[int] = []
        self.alerts: list[list] = []
        self.fills: list[tuple] = []

    async def handle(self, symbol_ws, high, low, close, start_ms=0):
        from app.delivery import bot

        async def on_fill(row, now_iso):
            self.fills.append((row["id"], now_iso))

        await bot.handle_stream_frame(
            symbol_ws, high, low, close, start_ms, last_price=self.last_price,
            on_trail=self.trails.append, on_alerts=self.alerts.append, on_fill=on_fill)

    def send(self, *frame):
        asyncio.run(self.handle(*frame))


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def _icnt_waiting_entry():
    """Сигнал #881 до входа (LONG 0.31835, стоп 0.3134) и алерт юзера на 0.3190. -> sid."""
    from app.brain import journal
    from app.core.signal_engine import Side
    from app.delivery import alerts
    from tests.test_journal import _sig

    assert alerts.create_alert(501, ICNT, 0.3190)["ok"]
    return journal.record_signal(_sig(symbol=ICNT, side=Side.LONG, entry=0.31835, stop=0.3134,
                                      tp=0.34, created_at=_ago(hours=2)))


def test_frame_outside_watchlist_fills_entry_but_skips_price_alerts(fresh_db, executor_state):
    """Монета вышла из вотчлиста, сигнал по ней ждёт входа: кадр доходит до executor и до
    реалтайм-входа (правило касания то же), а прайс-алерт дремлет — как до правки."""
    from app import runtime
    from app.delivery import alerts

    runtime.set_value("symbols", BTC)
    sid = _icnt_waiting_entry()
    f = _Frames()
    f.send("ICNTUSDT", 0.3210, 0.3195, 0.3200)                  # цена над входом
    assert f.fills == []
    assert fresh_db.query_one("SELECT filled_at FROM signals WHERE id=?", (sid,))[0] is None
    f.send("ICNTUSDT", 0.3210, 0.3180, 0.3185)                  # коснулась 0.31835, прошла 0.3190
    filled_at = fresh_db.query_one("SELECT filled_at FROM signals WHERE id=?", (sid,))[0]
    assert filled_at is not None and f.fills == [(sid, filled_at)]
    f.send("ICNTUSDT", 0.3210, 0.3180, 0.3183)                  # вход уже засчитан — без дубля
    assert f.fills == [(sid, filled_at)]
    assert "ICNTUSDT" in executor_state._STREAM_SEEN            # executor кадры получил
    # прайс-алерты не тронуты: ни выстрела, ни цены в кроссинг-окне
    assert f.alerts == [] and f.last_price == {}
    assert [a["price"] for a in alerts.active_alerts(501)] == [0.3190]


def test_frame_of_watchlist_symbol_still_triggers_price_alert_and_fill(fresh_db, executor_state):
    """Контроль к тесту выше: те же кадры по монете вотчлиста — алерт срабатывает по
    кроссингу, вход засчитан. Поведение для вотчлиста не изменилось."""
    from app import runtime
    from app.delivery import alerts

    runtime.set_value("symbols", f"{BTC},{ICNT}")
    sid = _icnt_waiting_entry()
    f = _Frames()
    f.send("ICNTUSDT", 0.3210, 0.3195, 0.3200)                  # первая цена — только сид
    assert f.alerts == [] and f.fills == [] and f.last_price == {"ICNTUSDT": 0.3200}
    f.send("ICNTUSDT", 0.3210, 0.3180, 0.3185)
    assert [[a["tg_id"], a["symbol"], a["price"]] for a in f.alerts[0]] == [[501, ICNT, 0.3190]]
    assert len(f.alerts) == 1 and alerts.active_alerts(501) == []
    assert [s for s, _ in f.fills] == [sid] and f.last_price == {"ICNTUSDT": 0.3185}


def test_frame_outside_watchlist_activates_executor_trail(fresh_db, executor_state):
    """#881 как он был: ICNT вне вотчлиста, позиция открыта, цена меньше минуты выше
    +1.5R (0.325775). Кадр стрима доходит до stream_tick, и трейл уходит на активацию;
    вотчлист executor не нужен."""
    from app import runtime

    runtime.set_value("symbols", BTC)
    fill = datetime.now(timezone.utc) - timedelta(hours=2)
    snap = json.dumps({"trail": {"activate_r": 1.5, "distance_r": 0.2, "tp_exit": False,
                                 "max_hold_hours": 168}})
    fresh_db.execute(
        "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, created_at, filled_at) VALUES "
        "(881, ?, 'LONG', '1h', 0.31835, 0.3134, 2.0, 0.7, ?, 'open', ?, ?)",
        (ICNT, snap, _ago(hours=11), fill.isoformat()))
    fresh_db.execute(
        "INSERT INTO executor_orders(signal_id, symbol, side, qty, entry_px, stop_px, "
        "client_id, state, created_at) VALUES (881, ?, 'LONG', 100, 0.31835, 0.3134, "
        "'rsiE881', 'filled', ?)", (ICNT, _ago(hours=11)))
    assert executor_state.refresh_armed() == 1
    bar = int(fill.timestamp() * 1000) + 20 * 60_000            # бар открылся после входа
    f = _Frames()
    f.send("ICNTUSDT", 0.3250, 0.3210, 0.3233, bar)             # до активации не дошла
    assert f.trails == []
    f.send("ICNTUSDT", 0.3258, 0.3210, 0.3233, bar)             # high прошёл +1.5R, цена ниже
    assert f.trails == [881]
    assert f.fills == [] and f.alerts == [] and f.last_price == {}


def test_frame_outside_watchlist_without_open_public_signal_goes_nowhere(
        fresh_db, executor_state):
    """Вне вотчлиста реалтайм-вход получают только публичные открытые сигналы, и окно входа
    для них то же: тень, кандидат, закрытый сигнал и сигнал с истёкшим окном кадр не
    исполняет (топик закрытой монеты доживает до переподключения)."""
    from app import runtime
    from app.brain import journal
    from tests.test_journal import _sig

    runtime.set_value("symbols", BTC)

    def rec(fn, name, **ago):
        return fn(_sig(symbol=f"{name}/USDT:USDT", created_at=_ago(**(ago or {"hours": 2}))))

    rec(journal.record_shadow, "SHADOW")
    rec(journal.record_candidate, "CAND")
    closed = rec(journal.record_signal, "CLOSED")
    fresh_db.execute("UPDATE signals SET status='stopped', result_r=-1.0 WHERE id=?", (closed,))
    rec(journal.record_signal, "STALE", hours=60)               # окно входа 50 ч вышло
    f = _Frames()
    for name in ("SHADOW", "CAND", "CLOSED", "STALE", "NOSUCH"):
        f.send(f"{name}USDT", 101.0, 99.0, 100.0)               # диапазон накрывает вход 100
        f.send(f"{name}USDT", 101.0, 99.0, 100.5)
    assert f.fills == [] and f.alerts == [] and f.trails == [] and f.last_price == {}
    assert fresh_db.query_one(
        "SELECT COUNT(*) FROM signals WHERE filled_at IS NOT NULL")[0] == 0
    assert len(executor_state._STREAM_SEEN) == 5                # executor видел каждый кадр


def test_stream_end_to_end_symbol_outside_watchlist(fresh_db, monkeypatch, executor_state):
    """Вся цепочка без сети: монета вне вотчлиста с сигналом, ждущим входа, попадает в
    подписку, её кадр доходит из run_stream до обработчика, журнал засчитывает вход."""
    from app import runtime
    from app.brain import journal

    runtime.set_value("symbols", BTC)
    sid = _icnt_waiting_entry()
    kline = {"topic": "kline.60.ICNTUSDT", "data": [
        {"start": 1, "open": "0.3200", "high": "0.3210", "low": "0.3180",
         "close": "0.3185", "confirm": False}]}
    connects = _fake_bybit(monkeypatch, frames=[
        json.dumps({"success": True, "ret_msg": "", "op": "subscribe"}), json.dumps(kline)])
    f = _Frames()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(stream.run_stream(
            runtime.symbols, lambda: "1h", f.handle, lambda: True,
            logging.getLogger("stream-test"), journal.open_signal_symbols))
    assert connects == [[_topics("BTCUSDT"), _topics("ICNTUSDT")]]
    filled_at = fresh_db.query_one("SELECT filled_at FROM signals WHERE id=?", (sid,))[0]
    assert filled_at is not None and f.fills == [(sid, filled_at)]
    assert f.alerts == [] and f.last_price == {}
