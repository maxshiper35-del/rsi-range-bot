"""Executor V2 (демо BingX): сайзинг и планировщик — чистые функции без сети."""
from __future__ import annotations

from app.v2.bingx_executor import Action, calc_qty, plan


def test_calc_qty_risk_based():
    # риск 1% от 10k VST = 100; стоп-дистанция 10 -> 10 монет номиналом $1000 —
    # под капом 1500 USDT, риск-формула работает как есть
    assert calc_qty(10_000, 1.0, 100, 90, 0.001, 0.001) == 10.0
    # шаг объёма режет ВНИЗ (никогда не превышать риск)
    assert calc_qty(10_000, 1.0, 100, 90, 0.001, 3.0) == 9.0
    # ниже минимального объёма биржи -> 0 (сделка пропускается)
    assert calc_qty(10, 1.0, 100, 90, 0.5, 0.001) == 0.0
    # вырожденные входы не роняют
    assert calc_qty(10_000, 1.0, 100, 100, 0.001, 0.001) == 0.0
    assert calc_qty(0, 1.0, 100, 90, 0.001, 0.001) == 0.0


def _row(sid, symbol="BTC/USDT:USDT", created="2026-08-13T10:00:00+00:00", filled=None):
    return {"id": sid, "symbol": symbol, "side": "LONG", "created_at": created,
            "filled_at": filled, "entry": 64_000.0, "stop": 62_000.0}


_MARKETS = {"BTC/USDT:USDT", "ETH/USDT:USDT"}
_SINCE = "2026-08-13T00:00:00+00:00"


def test_plan_places_only_new_untracked():
    acts = plan([_row(1)], tracked={}, markets=_MARKETS, since=_SINCE)
    assert [a.kind for a in acts] == ["place"]
    # уже ведётся -> действий нет
    assert plan([_row(1)], {1: "placed"}, _MARKETS, _SINCE) == []
    # созданный ДО включения — не догоняем (иначе чейзинг уехавших цен)
    assert plan([_row(2, created="2026-08-12T23:59:59+00:00")], {}, _MARKETS, _SINCE) == []


def test_plan_skips_missing_market_and_prefilled():
    acts = plan([_row(3, symbol="PUMPFUN/USDT:USDT")], {}, _MARKETS, _SINCE)
    assert acts[0].kind == "skip" and "нет на BingX" in acts[0].detail
    # филл зафиксирован журналом раньше постановки — честный пропуск, не догоняем рынок
    acts = plan([_row(4, filled="2026-08-13T10:05:00+00:00")], {}, _MARKETS, _SINCE)
    assert acts[0].kind == "skip"


def test_plan_closes_and_cancels_on_journal_resolution():
    # сигнал исчез из open: placing/placed -> снять лимитку (исполнившуюся между
    # тактами часть закроет ветка cancel по executedQty), filled -> закрыть позицию
    acts = plan([], {5: "placed", 6: "filled", 10: "placing"}, _MARKETS, _SINCE)
    kinds = {a.signal_id: a.kind for a in acts}
    assert kinds == {5: "cancel", 6: "close", 10: "cancel"}
    # терминальные состояния учёта не трогаются
    assert plan([], {7: "closed", 8: "cancelled", 9: "error"}, _MARKETS, _SINCE) == []


def test_plan_is_action_dataclass():
    a = Action("place", 1, "BTC/USDT:USDT")
    assert a.kind == "place" and a.signal_id == 1


def test_calc_qty_notional_cap():
    """BingX 101209: номинал позиции ограничен ~5000 USDT — риск-объём капится.
    1% от 100k при стопе 1% дал бы номинал 100k — режем до MAX_NOTIONAL_USDT."""
    from app.v2 import bingx_executor as ex
    qty = ex.calc_qty(100_000, 1.0, entry=100.0, stop=99.0, min_qty=0.0, qty_step=0.001)
    assert qty * 100.0 <= ex.MAX_NOTIONAL_USDT + 1e-6
    # без капа было бы 1000/1 = 1000 монет ($100k) — теперь 15 монет ($1500)
    assert qty == 15.0


def test_execution_report_slippage_and_gap(fresh_db):
    """Отчёт «исполнение vs журнал»: демо-R vs журнальный R, слип выхода, комиссии.
    LONG: вход 100 (филл 100), стоп 98 (риск 2), журнал вышел 106 (+3R),
    демо закрылось 105.8 -> демо +2.9R, слип выхода −0.1R."""
    from app import db
    from app.v2 import bingx_executor as ex
    db.execute(
        "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, created_at, closed_at, result_r, exit_price) "
        "VALUES (900,'AAA/USDT:USDT','LONG','1h',100,98,2.0,0.7,'{}','stopped',"
        "'2026-08-10T00:00:00+00:00','2026-08-12T00:00:00+00:00',3.0,106.0)")
    db.execute(
        "INSERT INTO executor_orders(signal_id, symbol, side, qty, entry_px, stop_px, "
        "client_id, state, created_at, updated_at, fill_px, fill_qty, fill_fee, "
        "close_px, close_fee, closed_at) VALUES (900,'AAA/USDT:USDT','LONG',10,100,98,"
        "'rsiE900','closed','2026-08-10T01:00:00+00:00','2026-08-12T00:01:00+00:00',"
        "100.0,10,0.4,105.8,0.42,'2026-08-12T00:01:00+00:00')")
    txt = ex.execution_report("2026-08-09T00:00:00+00:00", "2026-08-13T00:00:00+00:00")
    assert txt is not None
    assert "журнал +3.00R · демо +2.90R · разрыв -0.10R" in txt
    assert "в среднем -0.100R" in txt
    assert "0.82 VST" in txt
    # вне периода -> пусто
    assert ex.execution_report("2026-01-01T00:00:00+00:00",
                               "2026-01-02T00:00:00+00:00") is None


def test_calc_qty_symbol_budget(fresh_db):
    """Лимит BingX 5000 USDT — общий на символ/сторону: второй сигнал того же
    символа получает ОСТАТОК бюджета, а не полный кап (инцидент 101209 17.08)."""
    from app.v2 import bingx_executor as ex
    db_mod = __import__("app.db", fromlist=["db"])
    db_mod.execute(
        "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, created_at) VALUES "
        "(901,'BBB/USDT:USDT','SHORT','1h',0.9,1.0,2.0,0.7,'{}','open',"
        "'2026-08-17T00:00:00+00:00')")
    db_mod.execute(
        "INSERT INTO executor_orders(signal_id, symbol, side, qty, entry_px, stop_px, "
        "client_id, state, created_at) VALUES (901,'BBB/USDT:USDT','SHORT',5000,0.9,"
        "1.0,'rsiE901','placed','2026-08-17T00:00:00+00:00')")
    used = ex._active_notional("BBB/USDT:USDT", "SHORT")
    assert used == 4500.0
    # остаток бюджета 0 -> объём 0 (вторая лимитка не пробьёт биржевой лимит)
    qty = ex.calc_qty(100_000, 1.0, 0.9, 1.0, 0.0, 0.0,
                      max_notional=ex.MAX_NOTIONAL_USDT - used)
    assert qty == 0.0
    # закрытые/ошибочные бюджет не держат
    db_mod.execute("UPDATE executor_orders SET state='closed' WHERE signal_id=901")
    assert ex._active_notional("BBB/USDT:USDT", "SHORT") == 0.0


def test_cancel_deferred_stop_parses_note():
    """Снятие довешенного стопа: только note 'sl:<id>'; чужие/пустые note — no-op;
    «ордера нет» на бирже — не ошибка (цель достигнута)."""
    from app.v2 import bingx_executor as ex

    calls = []

    class _Ex:
        def cancel_order(self, oid, symbol):
            calls.append((oid, symbol))
            raise RuntimeError("order not exist")

    ex._cancel_deferred_stop(_Ex(), {"note": "sl:123", "symbol": "AAA/USDT:USDT"})
    assert calls == [("123", "AAA/USDT:USDT")]
    ex._cancel_deferred_stop(_Ex(), {"note": "стоп отложен до филла", "symbol": "A"})
    ex._cancel_deferred_stop(_Ex(), {"note": None, "symbol": "A"})
    ex._cancel_deferred_stop(_Ex(), {"note": "sl:", "symbol": "A"})
    assert len(calls) == 1                                   # прочие note не трогают биржу

    class _ExBad:
        def cancel_order(self, oid, symbol):
            raise RuntimeError("timeout")

    import pytest as _pytest
    with _pytest.raises(RuntimeError):                       # настоящий сбой не глотается
        ex._cancel_deferred_stop(_ExBad(), {"note": "sl:9", "symbol": "A"})


def test_effective_filled_trigger_semantics():
    """Сработавший TRIGGER_MARKET у BingX: status=closed, filled=0 (исполнение в
    отдельном ордере, смоук 02.09) — считается филлом на ПОЛНЫЙ учётный объём.
    canceled/open без филла — не филл; обычный частичный филл — как есть."""
    from app.v2 import bingx_executor as ex
    t = {"qty": 5.0}
    assert ex._effective_filled({"status": "closed", "filled": 0.0}, t) == 5.0
    assert ex._effective_filled({"status": "closed", "filled": 3.0}, t) == 3.0
    assert ex._effective_filled({"status": "canceled", "filled": 0.0}, t) == 0.0
    assert ex._effective_filled({"status": "open", "filled": 0.0}, t) == 0.0
    assert ex._effective_filled(None, t) == 0.0


def test_record_fill_idempotent(fresh_db):
    """_record_fill пишет цену/объём/комиссию входа и НЕ перетирает уже записанное
    (COALESCE): повторный вызов из другого пути не портит данные отчёта."""
    from app import db
    from app.v2 import bingx_executor as ex
    db.execute(
        "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, created_at) VALUES "
        "(902,'CCC/USDT:USDT','LONG','1h',100,98,2.0,0.7,'{}','open',"
        "'2026-08-31T00:00:00+00:00')")
    db.execute(
        "INSERT INTO executor_orders(signal_id, symbol, side, qty, entry_px, stop_px, "
        "client_id, state, created_at) VALUES (902,'CCC/USDT:USDT','LONG',10,100,98,"
        "'rsiE902','placed','2026-08-31T00:00:00+00:00')")
    ex._record_fill(902, {"filled": 10, "average": 100.05, "fee": {"cost": 0.5}})
    row = db.query_one("SELECT fill_px, fill_qty, fill_fee FROM executor_orders "
                       "WHERE signal_id=902")
    assert row["fill_px"] == 100.05 and row["fill_qty"] == 10 and row["fill_fee"] == 0.5
    ex._record_fill(902, {"filled": 10, "average": 999.0, "fee": {"cost": 9.9}})
    row = db.query_one("SELECT fill_px FROM executor_orders WHERE signal_id=902")
    assert row["fill_px"] == 100.05                          # не перетёрто
    # пустой/нефилленный ордер — no-op
    ex._record_fill(902, None)
    ex._record_fill(902, {"filled": 0})


def test_bump_retry_and_budget_requeue(fresh_db):
    """Ретраи cancel/close считаются в note ('retry#N'); скип по бюджету возвращается
    в очередь удалением строки, когда бюджет освободился и прошло 30 мин."""
    from app import db
    from app.v2 import bingx_executor as ex
    db.execute(
        "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, created_at) VALUES "
        "(903,'DDD/USDT:USDT','LONG','1h',10,9,2.0,0.7,'{}','open',"
        "'2026-08-31T00:00:00+00:00')")
    db.execute(
        "INSERT INTO executor_orders(signal_id, symbol, side, qty, entry_px, stop_px, "
        "client_id, state, created_at, updated_at, note) VALUES "
        "(903,'DDD/USDT:USDT','LONG',0,10,9,'','error','2026-08-31T00:00:00+00:00',"
        "'2026-08-31T00:00:00+00:00','бюджет номинала символа/стороны исчерпан')")
    assert ex._bump_retry(903, RuntimeError("сеть")) == 1
    assert ex._bump_retry(903, RuntimeError("сеть")) == 2
    # note перезаписан ретраем — вернём бюджетный текст для проверки requeue
    db.execute("UPDATE executor_orders SET note='бюджет номинала', "
               "updated_at='2026-08-31T00:00:00+00:00' WHERE signal_id=903")
    ex._retry_budget_skips({903})
    assert db.query_one("SELECT 1 FROM executor_orders WHERE signal_id=903") is None
    # сигнал уже не open -> строка (если была бы) не трогается: проверка ветки
    ex._retry_budget_skips(set())


def test_reconcile_self_disables_without_keys(fresh_db, monkeypatch):
    """Тумблер панели мог включить executor без ключей BingX в окружении (локальный
    режим) — reconcile самовыключается одним сообщением, не сыплет AuthenticationError."""
    from app import runtime
    from app.v2 import bingx_executor as ex
    monkeypatch.delenv("BINGX_API_KEY", raising=False)
    runtime.set_value("executor_enabled", "1")
    runtime.set_executor_since("2026-08-31T00:00:00+00:00")
    out = ex.reconcile()
    assert out and "выключен сам" in out[0]
    assert runtime.executor_enabled() is False


def test_match_orphans_pure():
    """Сироты: позиция биржи без учёта -> сирота; учтённая (placed/filled) — нет;
    два сигнала одного символа/стороны сливаются в одну позицию — сравниваем сумму;
    излишек в пределах 2% — не сирота (округление биржи)."""
    from app.v2 import bingx_executor as ex
    positions = [
        {"symbol": "HYPE/USDT:USDT", "side": "long", "contracts": 81.0643,
         "entryPrice": 55.545, "unrealizedPnl": 2163.1},
        {"symbol": "HYPE/USDT:USDT", "side": "short", "contracts": 18.1597,
         "entryPrice": 82.6, "unrealizedPnl": 6.7},
        {"symbol": "ZEC/USDT:USDT", "side": "short", "contracts": 3.64,
         "entryPrice": 824.0, "unrealizedPnl": -14.0},
        {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.0},
    ]
    tracked = [
        {"symbol": "HYPE/USDT:USDT", "side": "SHORT", "qty": 18.1597},
        {"symbol": "ZEC/USDT:USDT", "side": "SHORT", "qty": 1.82},
        {"symbol": "ZEC/USDT:USDT", "side": "SHORT", "qty": 1.8},   # второй сигнал (сумма 3.62)
    ]
    orph = ex._match_orphans(positions, tracked)
    assert [o["symbol"] for o in orph] == ["HYPE/USDT:USDT"]
    assert orph[0]["side"] == "LONG" and orph[0]["qty"] == 81.0643
    assert "uPnL +2163" in ex._orphan_line(orph[0])
    # без учёта вообще — все ненулевые позиции сироты
    assert len(ex._match_orphans(positions, [])) == 3


def test_daily_guard_pure():
    """Стоп-кран: якорь на первом такте суток; срабатывание на пороге; держится до конца
    суток; новый день — новый якорь и снятие; 0 = выключен; битый якорь пересоздаётся."""
    from app.v2.bingx_executor import daily_guard
    day = "2026-10-04"
    # нет якоря -> ставим от текущего эквити
    assert daily_guard(day, 100_000, None, None, 5.0) == \
        (f"{day}|100000.0000", False, False, 0.0)
    a = f"{day}|100000.0000"
    _, halted, tripped, dd = daily_guard(day, 96_000, a, None, 5.0)
    assert (halted, tripped) == (False, False) and round(dd, 1) == 4.0
    _, halted, tripped, dd = daily_guard(day, 95_000, a, None, 5.0)
    assert (halted, tripped) == (True, True) and round(dd, 1) == 5.0
    # уже сработал сегодня — держится и после отскока, повторно не «срабатывает»
    assert daily_guard(day, 99_000, a, day, 5.0)[1:3] == (True, False)
    # новые сутки: якорь заново, вчерашний кран не действует
    anchor, halted, tripped, _ = daily_guard("2026-10-05", 95_000, a, day, 5.0)
    assert anchor == "2026-10-05|95000.0000" and (halted, tripped) == (False, False)
    # выключен
    assert daily_guard(day, 50_000, a, None, 0.0)[1:3] == (False, False)
    # битый якорь
    assert daily_guard(day, 1000, "мусор", None, 5.0)[0] == f"{day}|1000.0000"


def test_equity_of_bingx_shape():
    from app.v2.bingx_executor import _equity_of
    bal = {"info": {"data": [{"asset": "VST", "balance": "100", "equity": "93.5"}]},
           "total": {"VST": 100.0}}
    assert _equity_of(bal) == 93.5
    assert _equity_of({"info": {}, "total": {"VST": 77.0}}) == 77.0     # фолбэк


def _seed_halt_case():
    from app import db
    db.execute(
        "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, created_at) VALUES "
        "(911,'EEE/USDT:USDT','LONG','1h',10,9,2.0,0.7,'{}','open',"
        "'2026-10-04T01:00:00+00:00'),"
        "(912,'FFF/USDT:USDT','LONG','1h',10,9,2.0,0.7,'{}','open',"
        "'2026-10-04T01:00:00+00:00')")
    # 911 — уже стоящий триггер; 912 — новый, ещё не поставлен
    db.execute(
        "INSERT INTO executor_orders(signal_id, symbol, side, qty, entry_px, stop_px, "
        "client_id, state, created_at) VALUES (911,'EEE/USDT:USDT','LONG',5,10,9,"
        "'rsiE911','placed','2026-10-04T01:00:00+00:00')")


def _fake_exchange(monkeypatch, ex_mod, balance):
    created = []

    class _Ex:
        markets = {"EEE/USDT:USDT": {}, "FFF/USDT:USDT": {}}

        def fetch_balance(self):
            if isinstance(balance, Exception):
                raise balance
            return {"info": {"data": [{"asset": "VST", "equity": str(balance)}]},
                    "total": {"VST": float(balance)}}

        def create_order(self, *a, **k):              # pragma: no cover — не должен вызываться
            created.append(a)
            raise AssertionError("стоп-кран пропустил постановку")

    monkeypatch.setenv("BINGX_API_KEY", "test")
    monkeypatch.setattr(ex_mod, "_exchange", lambda: _Ex())
    monkeypatch.setattr(ex_mod, "_cancel_entry", lambda ex, t: None)
    monkeypatch.setattr(ex_mod, "_entry_order",
                        lambda ex, t: {"status": "canceled", "filled": 0})
    monkeypatch.setattr(ex_mod, "_cancel_deferred_stop", lambda ex, t: None)
    monkeypatch.setattr(ex_mod, "_sync_fill_states", lambda ex, report: None)
    monkeypatch.setattr(ex_mod, "_orphan_lines", lambda ex: [])
    return created


def test_reconcile_daily_kill_switch(fresh_db, monkeypatch):
    """Эквити −6% от якоря суток при пороге 5%: новые входы не ставятся, стоящий триггер
    снимается с пометкой «стоп-кран», алерт один раз; второй такт молчит."""
    from app import clock, runtime
    from app.v2 import bingx_executor as ex
    _seed_halt_case()
    runtime.set_executor_since("2026-10-04T00:00:00+00:00")
    day = clock.utcnow_iso()[:10]
    runtime.set_value("executor_day_anchor", f"{day}|100000.0000")
    _fake_exchange(monkeypatch, ex, 94_000)

    out = ex.reconcile()
    assert any(line.startswith("⛔ Стоп-кран") for line in out), out
    assert any("#911" in line and "стоп-кран" in line for line in out)
    assert runtime.executor_halt_day() == day
    from app import db
    row = db.query_one("SELECT state, note FROM executor_orders WHERE signal_id=911")
    assert (row["state"], row["note"]) == ("cancelled", "стоп-кран")
    assert db.query_one("SELECT 1 FROM executor_orders WHERE signal_id=912") is None

    out2 = ex.reconcile()                                  # кран держится, алерт не повторяется
    assert not any(line.startswith("⛔") for line in out2)
    assert db.query_one("SELECT 1 FROM executor_orders WHERE signal_id=912") is None


def test_reconcile_balance_outage_defers_only_placement(fresh_db, monkeypatch):
    """Баланс не читается: такт не падает, постановка откладывается (алерт раз в час),
    а снятие/закрытие по журналу продолжаются."""
    from app import db, runtime
    from app.v2 import bingx_executor as ex
    _seed_halt_case()
    db.execute("UPDATE signals SET status='expired' WHERE id=911")   # журнал снял 911
    runtime.set_executor_since("2026-10-04T00:00:00+00:00")
    ex._BALANCE_ALERT["at"] = None
    _fake_exchange(monkeypatch, ex, RuntimeError("network down"))

    out = ex.reconcile()
    assert any("баланс не читается" in line for line in out)
    row = db.query_one("SELECT state FROM executor_orders WHERE signal_id=911")
    assert row["state"] == "cancelled"                      # отмена прошла без баланса
    assert db.query_one("SELECT 1 FROM executor_orders WHERE signal_id=912") is None
    out2 = ex.reconcile()
    assert not any("баланс не читается" in line for line in out2)   # троттлинг


def test_entry_slip_rule():
    """Слип входа в R и решающее правило: срабатывает только при n>=50 и среднем <= −0.10R."""
    from app.v2.bingx_executor import entry_slip_r, slip_rule_verdict
    # LONG: вошли выше entry на 0.1 при риске 1.0 -> −0.1R; SHORT зеркально
    assert round(entry_slip_r({"entry_px": 100, "stop_px": 99, "fill_px": 100.1,
                               "side": "LONG"}), 4) == -0.1
    assert round(entry_slip_r({"entry_px": 100, "stop_px": 101, "fill_px": 100.2,
                               "side": "SHORT"}), 4) == 0.2
    assert entry_slip_r({"entry_px": 100, "stop_px": 99, "fill_px": None,
                         "side": "LONG"}) is None
    assert slip_rule_verdict([-0.2] * 49)[2] is False          # мало данных — не судим
    assert slip_rule_verdict([-0.1] * 50) == (50, -0.1, True)
    assert slip_rule_verdict([-0.05] * 80)[2] is False
    assert slip_rule_verdict([]) == (0, 0.0, False)


class _PlaceEx:
    """Фейковая биржа для пути постановки: markets/market/price_to_precision + сценарий
    create_order. created — журнал вызовов."""
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.created = []
        self.markets = {"GGG/USDT:USDT": {}}

    def fetch_balance(self):
        return {"info": {"data": [{"asset": "VST", "equity": "100000"}]},
                "total": {"VST": 100000.0}}

    def market(self, symbol):
        return {"limits": {"amount": {"min": 0}}, "precision": {"amount": 0.001}}

    def price_to_precision(self, symbol, px):
        return str(px)

    def create_order(self, symbol, typ, side, qty, price, params=None):
        self.created.append(dict(params or {}))
        return self.behaviour(self, params or {})

    def fetch_open_orders(self, symbol):
        return []


def _seed_place(sid=941):
    from app import db, runtime
    db.execute(
        "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, created_at) VALUES "
        f"({sid},'GGG/USDT:USDT','LONG','1h',100,98,2.0,0.7,'{{}}','open',"
        "'2026-10-05T01:00:00+00:00')")
    runtime.set_executor_since("2026-10-05T00:00:00+00:00")


def _patch_net(monkeypatch, ex_mod, fake):
    monkeypatch.setenv("BINGX_API_KEY", "test")
    monkeypatch.setattr(ex_mod, "_exchange", lambda: fake)
    monkeypatch.setattr(ex_mod, "_exchange_fast", lambda: fake)
    monkeypatch.setattr(ex_mod, "_sync_fill_states", lambda ex, report: None)
    monkeypatch.setattr(ex_mod, "_orphan_lines", lambda ex: [])


def test_place_101400_keeps_deferred_stop_marker(fresh_db, monkeypatch):
    """Аудит 05.10: метку «стоп отложен до филла» затирал _set_state(…,'placed') —
    после филла стоп не довешивался, позиция жила без биржевого стопа."""
    from ccxt import ExchangeError

    from app import db
    from app.v2 import bingx_executor as ex

    def behaviour(fake, params):
        if "stopLoss" in params:
            raise ExchangeError('bingx {"code":101400,"msg":"SL Price must be lower"}')
        return {"id": "T1"}

    fake = _PlaceEx(behaviour)
    _seed_place()
    _patch_net(monkeypatch, ex, fake)
    out = ex.reconcile()
    row = db.query_one("SELECT state, note, order_id FROM executor_orders WHERE signal_id=941")
    assert (row["state"], row["note"], row["order_id"]) == ("placed", "стоп отложен до филла",
                                                             "T1")
    assert any("довешу при филле" in line for line in out)


def test_place_normal_path_stores_order_id(fresh_db, monkeypatch):
    from app import db
    from app.v2 import bingx_executor as ex
    fake = _PlaceEx(lambda f, p: {"id": "T2"})
    _seed_place(942)
    _patch_net(monkeypatch, ex, fake)
    ex.reconcile()
    row = db.query_one("SELECT state, note, order_id FROM executor_orders WHERE signal_id=942")
    assert (row["state"], row["note"], row["order_id"]) == ("placed", None, "T2")
    assert "stopLoss" in fake.created[0]                     # стоп прикреплён к входу


def test_place_lost_response_stays_placing(fresh_db, monkeypatch):
    """Аудит 05.10: таймаут после отправки -> раньше 'error' навсегда (живой триггер
    никто не снимал). Теперь строка остаётся 'placing' до сверки."""
    from ccxt import RequestTimeout

    from app import db
    from app.v2 import bingx_executor as ex

    def behaviour(fake, params):
        raise RequestTimeout("timed out")

    _seed_place(943)
    _patch_net(monkeypatch, ex, _PlaceEx(behaviour))
    out = ex.reconcile()
    assert db.query_one("SELECT state FROM executor_orders WHERE signal_id=943")["state"] \
        == "placing"
    assert any("ответ биржи потерян" in line for line in out)


def test_close_position_clamps_to_own_share(fresh_db):
    """Аудит 05.10: две позиции одного символа/стороны сливаются; стоп закрыл долю A —
    закрытие A не должно продать долю B."""
    from app import db
    from app.v2 import bingx_executor as ex
    for sid, qty in ((951, 10.0), (952, 5.0)):
        db.execute(
            "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
            "confidence, snapshot_json, status, created_at) VALUES "
            f"({sid},'HHH/USDT:USDT','LONG','1h',100,98,2.0,0.7,'{{}}','open',"
            f"'2026-10-05T0{sid - 950}:00:00+00:00')")
        db.execute(
            "INSERT INTO executor_orders(signal_id, symbol, side, qty, entry_px, stop_px, "
            "client_id, state, created_at) VALUES (?, 'HHH/USDT:USDT','LONG',?,100,98,"
            "?, 'filled','2026-10-05T01:00:00+00:00')", (sid, qty, f"rsiE{sid}"))
    sold = []

    class _Ex:
        def __init__(self, pos):
            self.pos = pos

        def fetch_positions(self, symbols):
            return [{"symbol": "HHH/USDT:USDT", "side": "long", "contracts": self.pos}]

        def create_order(self, symbol, typ, side, qty, price, params=None):
            sold.append(qty)
            return {"id": "C", "average": 97.0}

        def fetch_my_trades(self, symbol, limit=20):
            return []

    t = db.query_one("SELECT * FROM executor_orders WHERE signal_id=951")
    assert ex._close_position(_Ex(5.0), t, max_qty=10.0) == 0.0     # остаток — доля B
    assert sold == []
    assert ex._close_position(_Ex(15.0), t, max_qty=10.0) == 10.0    # обе доли живы
    assert sold == [10.0]


def test_symbol_budget_allows_concurrent_duplicates(fresh_db):
    """Аудит 05.10: кап 1500 работал как ОБЩИЙ бюджет символа/стороны — дубли мульти-ТФ
    не исполнялись. Теперь 1500 на ордер, 4500 на символ/сторону."""
    from app.v2 import bingx_executor as ex
    assert ex.SYMBOL_BUDGET_USDT >= 3 * ex.MAX_NOTIONAL_USDT
    assert ex.SYMBOL_BUDGET_USDT < 5000                      # лимит BingX 101209


def test_retry_keeps_deferred_stop_id(fresh_db):
    from app import db
    from app.v2 import bingx_executor as ex
    _seed_place(961)
    db.execute(
        "INSERT INTO executor_orders(signal_id, symbol, side, qty, entry_px, stop_px, "
        "client_id, state, created_at, note) VALUES (961,'GGG/USDT:USDT','LONG',1,100,98,"
        "'rsiE961','filled','2026-10-05T01:00:00+00:00','sl:777')")
    assert ex._bump_retry(961, RuntimeError("сеть")) == 1
    note = db.query_one("SELECT note FROM executor_orders WHERE signal_id=961")["note"]
    assert note.startswith("retry#1") and ex._sl_id(note) == "777"
    assert ex._bump_retry(961, RuntimeError("сеть")) == 2
    note = db.query_one("SELECT note FROM executor_orders WHERE signal_id=961")["note"]
    assert ex._sl_id(note) == "777"
    assert ex._sl_id("sl:12") == "12" and ex._sl_id("стоп НЕ встал: sl:x") == ""


def test_alert_due_first_alert_right_after_boot():
    """Аудит 05.10: старт с 0.0 глушил первый алерт час после загрузки Mac."""
    from app.v2.bingx_executor import _alert_due
    assert _alert_due({"at": None}, now_mono=120.0) is True
    assert _alert_due({"at": 100.0}, now_mono=200.0) is False
    assert _alert_due({"at": 100.0}, now_mono=3800.0) is True


def test_kill_switch_keeps_fresh_fill(fresh_db, monkeypatch):
    """Аудит 05.10: триггер сработал между тактами, строка ещё 'placed' — стоп-кран не
    должен «отменой» закрыть свежую позицию: сначала сверка филлов."""
    from app import clock, db, runtime
    from app.v2 import bingx_executor as ex
    _seed_halt_case()
    runtime.set_executor_since("2026-10-04T00:00:00+00:00")
    day = clock.utcnow_iso()[:10]
    runtime.set_value("executor_day_anchor", f"{day}|100000.0000")
    _fake_exchange(monkeypatch, ex, 94_000)
    closed = []
    monkeypatch.setattr(ex, "_close_position",
                        lambda e, t, max_qty: closed.append(t["signal_id"]) or max_qty)

    def sync(e, report):                                     # биржа: 911 уже исполнен
        db.execute("UPDATE executor_orders SET state='filled' WHERE signal_id=911 "
                   "AND state='placed'")
    monkeypatch.setattr(ex, "_sync_fill_states", sync)
    ex.reconcile()
    assert db.query_one("SELECT state FROM executor_orders WHERE signal_id=911")["state"] \
        == "filled"
    assert closed == []


class _TrailEx:
    """Биржа для трейла: прикреплённый STOP_MARKET входа, позиция, журнал вызовов."""

    def __init__(self, stop_px=98.0, qty=10.0, reject=False):
        self.opens = [{"id": "SL0", "info": {"type": "STOP_MARKET", "positionSide": "LONG",
                                             "stopPrice": str(stop_px), "origQty": str(qty)}}]
        self.pos, self.reject = qty, reject
        self.created, self.cancelled = [], []

    def price_to_precision(self, symbol, px):
        return str(round(px, 4))

    def fetch_open_orders(self, symbol):
        return list(self.opens)

    def fetch_positions(self, symbols):
        return [{"symbol": symbols[0], "side": "long", "contracts": self.pos}]

    def create_order(self, symbol, typ, side, qty, price, params=None):
        from ccxt import ExchangeError
        params = params or {}
        if "stopLossPrice" in params:
            # стоп ЗАКРЫВАЕТ позицию: без reduceOnly ccxt в hedge ставит сторону ордера
            # (sell -> SHORT) — это был бы ордер на открытие встречной позиции
            assert params.get("hedged") is True and params.get("reduceOnly") is True
            if self.reject:
                raise ExchangeError('bingx {"code":110411,"msg":"Stop Loss price should be lower '
                                    'than the current price"}')
            self.created.append(("stop", params["stopLossPrice"], qty))
            o = {"id": f"SL{len(self.created)}", "info": {
                "type": "STOP_MARKET", "positionSide": "LONG",
                "stopPrice": str(params["stopLossPrice"]), "origQty": str(qty)}}
            self.opens.append(o)
            return o
        self.created.append(("market", side, qty))
        self.pos = 0.0
        return {"id": "M1", "average": 103.0}

    def cancel_order(self, oid, symbol):
        self.cancelled.append(oid)
        self.opens = [o for o in self.opens if o["id"] != oid]

    def fetch_order(self, oid, symbol):
        return {"id": oid, "average": 103.0}

    def fetch_my_trades(self, symbol, limit=20):
        return []


def _seed_trail(trail_stop, sid=961):
    from app import db
    db.execute(
        "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, created_at, filled_at, trail_stop) VALUES "
        f"({sid},'TTT/USDT:USDT','LONG','1h',100,98,2.0,0.7,'{{}}','open',"
        "'2026-10-20T01:00:00+00:00','2026-10-20T02:00:00+00:00', ?)", (trail_stop,))
    db.execute(
        "INSERT INTO executor_orders(signal_id, symbol, side, qty, entry_px, stop_px, "
        "client_id, state, created_at) VALUES (?, 'TTT/USDT:USDT','LONG',10,100,98,"
        "?, 'filled','2026-10-20T01:00:00+00:00')", (sid, f"rsiE{sid}"))


def test_trail_moves_exchange_stop_new_before_old(fresh_db):
    """20.10: трейл журнала -> стоп BingX. Новый стоп ставится ДО снятия старого
    (прикреплённого ко входу), уровень и id помнятся; повтор того же уровня — тишина."""
    from app import db
    from app.v2 import bingx_executor as ex
    _seed_trail(103.6)
    fake, report = _TrailEx(), []
    ex._sync_trail_stops(fake, report)
    assert fake.created == [("stop", 103.6, 10.0)] and fake.cancelled == ["SL0"]
    row = db.query_one("SELECT trail_px, note, state FROM executor_orders WHERE signal_id=961")
    assert (row["trail_px"], row["note"], row["state"]) == (103.6, "sl:SL1", "filled")
    assert any("стоп подтянут 98 → 103.6" in line for line in report)
    ex._sync_trail_stops(fake, report)                       # уровень тот же — ничего
    assert len(fake.created) == 1
    db.execute("UPDATE signals SET trail_stop=104.2 WHERE id=961")
    ex._sync_trail_stops(fake, report)                       # следующий шаг: по id из note
    assert fake.created[-1] == ("stop", 104.2, 10.0) and fake.cancelled[-1] == "SL1"


def test_trail_price_already_beyond_closes_at_market(fresh_db):
    """Цена уже за новым уровнем (101400): журнал засчитает выход на закрытии бара —
    executor закрывается по рынку сразу и снимает старый стоп."""
    from app import db
    from app.v2 import bingx_executor as ex
    _seed_trail(103.6, sid=962)
    fake, report = _TrailEx(reject=True), []
    ex._sync_trail_stops(fake, report)
    assert ("market", "sell", 10.0) in fake.created and fake.cancelled == ["SL0"]
    row = db.query_one("SELECT state, close_px FROM executor_orders WHERE signal_id=962")
    assert row["state"] == "closed" and row["close_px"] == 103.0


def test_trail_no_stop_found_on_flat_position_is_silent(fresh_db):
    from app.v2 import bingx_executor as ex
    _seed_trail(103.6, sid=963)
    fake, report = _TrailEx(), []
    fake.opens, fake.pos = [], 0.0                           # стоп сработал, позиция плоская
    ex._sync_trail_stops(fake, report)
    assert fake.created == [] and report == []


def test_entry_and_stop_close_prices_from_own_trades(fresh_db):
    """24.10: без since «последний из 20 трейдов» был ЧУЖИМ (#655 WLD: вход 0.5003 вместо
    0.5871, фантомные −8R; отчёт исполнения −29R вместо +1.9R). Теперь — трейды после
    постановки, ближайшие к уровню входа/стопа."""
    from app import db
    from app.v2 import bingx_executor as ex
    db.execute(
        "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, confidence, "
        "snapshot_json, status, created_at) VALUES (971,'WLD/USDT:USDT','SHORT','1h',0.5884,"
        "0.5978,2.0,0.7,'{}','open','2026-10-22T00:00:00+00:00')")
    db.execute(
        "INSERT INTO executor_orders(signal_id, symbol, side, qty, entry_px, stop_px, client_id, "
        "state, created_at) VALUES (971,'WLD/USDT:USDT','SHORT',2549.39,0.5884,0.5978,'rsiE971',"
        "'filled','2026-10-22T00:00:30+00:00')")
    from datetime import datetime
    t0 = int(datetime.fromisoformat("2026-10-22T00:00:30+00:00").timestamp() * 1000)

    class _Ex:
        def fetch_my_trades(self, symbol, since=None, limit=20):
            trades = [
                {"side": "sell", "price": 0.5003, "timestamp": t0 - 86_400_000,
                 "info": {"positionSide": "SHORT"}},      # чужой, сутками раньше
                {"side": "sell", "price": 0.5871, "timestamp": t0 + 360_000,
                 "info": {"positionSide": "SHORT"}},      # наш вход
                {"side": "buy", "price": 0.5979, "timestamp": t0 + 7_200_000,
                 "info": {"positionSide": "SHORT"}},      # наш стоп
            ]
            return [tr for tr in trades if since is None or tr["timestamp"] >= since]

    row = db.query_one("SELECT * FROM executor_orders WHERE signal_id=971")
    ex._record_entry_from_trades(_Ex(), row)
    ex._record_stop_close(_Ex(), row)
    got = db.query_one("SELECT fill_px, close_px FROM executor_orders WHERE signal_id=971")
    assert (got["fill_px"], got["close_px"]) == (0.5871, 0.5979)


class _NativeEx(_TrailEx):
    """+ тикер для активации нативного трейла."""

    def __init__(self, last, **kw):
        super().__init__(**kw)
        self.last = last

    def fetch_ticker(self, symbol):
        return {"last": self.last}

    def create_order(self, symbol, typ, side, qty, price, params=None):
        params = params or {}
        if "trailingAmount" in params:
            assert params.get("hedged") is True and params.get("reduceOnly") is True
            self.created.append(("trail", params["trailingAmount"], side, qty))
            return {"id": "TR1"}
        return super().create_order(symbol, typ, side, qty, price, params)


def _seed_native(sid=981, trail_stop=None):
    import json

    from app import db
    snap = json.dumps({"trail": {"activate_r": 1.5, "distance_r": 0.2, "tp_exit": False,
                                 "max_hold_hours": 168}})
    db.execute(
        "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, created_at, filled_at, trail_stop) VALUES "
        f"({sid},'TTT/USDT:USDT','LONG','1h',100,98,2.0,0.7,?,'open',"
        "'2026-10-24T01:00:00+00:00','2026-10-24T02:00:00+00:00', ?)", (snap, trail_stop))
    db.execute(
        "INSERT INTO executor_orders(signal_id, symbol, side, qty, entry_px, stop_px, "
        "client_id, state, created_at) VALUES (?, 'TTT/USDT:USDT','LONG',10,100,98,"
        "?, 'filled','2026-10-24T01:00:00+00:00')", (sid, f"rsiE{sid}"))


def test_native_trail_waits_for_activation_then_places_exchange_trailing(fresh_db):
    """24.10: BingX игнорирует activationPrice — активирует executor. До +1.5R ничего;
    после — TRAILING_STOP_MARKET reduce-only с отступом 0.2R в цене, один раз."""
    from app import db
    from app.v2 import bingx_executor as ex
    _seed_native()
    fake, report = _NativeEx(last=102.5), []                   # +1.25R — рано
    assert ex._sync_native_trails(fake, report) == set() and fake.created == []
    fake.last = 103.1                                          # +1.55R — активация
    assert ex._sync_native_trails(fake, report) == {981}
    assert fake.created == [("trail", 0.4, "sell", 10.0)]
    assert db.query_one("SELECT trail_order_id FROM executor_orders WHERE signal_id=981")[
        "trail_order_id"] == "TR1"
    ex._sync_native_trails(fake, report)                       # повтор — без второго ордера
    assert len(fake.created) == 1 and any("трейлинг-стоп на бирже" in x for x in report)


def test_native_trail_activates_on_journal_trail_and_closes_if_price_beyond(fresh_db):
    from app import db
    from app.v2 import bingx_executor as ex
    _seed_native(982, trail_stop=103.6)                       # журнал подтянул трейл
    fake, report = _NativeEx(last=101.0), []                   # цена уже ниже 103.6
    assert ex._sync_native_trails(fake, report) == set()
    assert ("market", "sell", 10.0) in fake.created
    row = db.query_one("SELECT state FROM executor_orders WHERE signal_id=982")
    assert row["state"] == "closed"


def test_hourly_trail_skips_positions_with_native_trailing(fresh_db):
    from app.v2 import bingx_executor as ex
    _seed_trail(103.6, sid=983)
    fake, report = _TrailEx(), []
    ex._sync_trail_stops(fake, report, skip={983})
    assert fake.created == [] and fake.cancelled == []


# --- 30.10.2026: активация трейла по стриму Bybit, захват, быстрый вход, версии ------------
# (аудит docs/audits/2026-10-30-execution-parity.md)
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _exec_state_reset():
    """Модульные кеши executor (стрим, захваты, отчёт) не переживают границы тестов."""
    from app.v2 import bingx_executor as ex

    def _clear():
        ex.disarm_all()
        ex._STREAM_SEEN.clear()
        ex._ACT_TRIED.clear()
        ex._JLEVEL_SEEN.clear()
        ex._TRAIL_WARNED.clear()
        ex._FLAT_SEEN.clear()
        ex._TRAIL_STATUS.clear()
        ex._DIVERGENCE_WARNED["at"] = None
        ex._drain_report()
    _clear()
    yield
    _clear()


class _StreamEx(_NativeEx):
    """Нативный трейл + листинг открытых ордеров + сценарий create_order для трейла."""

    def __init__(self, last, trail_behaviour=None, **kw):
        super().__init__(last, **kw)
        self.trail_behaviour = trail_behaviour
        self.ticker_calls = 0

    def fetch_ticker(self, symbol):
        self.ticker_calls += 1
        return {"last": self.last}

    def create_order(self, symbol, typ, side, qty, price, params=None):
        params = params or {}
        if "trailingAmount" in params and self.trail_behaviour is not None:
            self.created.append(("trail", params["trailingAmount"], side, qty))
            return self.trail_behaviour(self)
        return super().create_order(symbol, typ, side, qty, price, params)


def _arm_env(monkeypatch, ex_mod, fake):
    from app import runtime
    monkeypatch.setenv("BINGX_API_KEY", "test")
    monkeypatch.setattr(ex_mod, "_exchange", lambda: fake)
    monkeypatch.setattr(ex_mod, "_exchange_fast", lambda: fake)
    runtime.set_value("executor_enabled", "1")


def _ms(iso):
    from datetime import datetime
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def test_stream_tick_uses_bar_extreme_only_after_fill_bar(fresh_db):
    """Бар филла: его high включает цену ДО входа — активация только по последней цене.
    Бар целиком после филла: high ловит проход между кадрами и во время реконнекта."""
    from app.v2 import bingx_executor as ex
    _seed_native(1001)                                      # LONG 100/98, act 103, филл 02:00
    assert ex.refresh_armed() == 1
    fill = _ms("2026-10-24T02:00:00+00:00")
    bar_fill, bar_next = fill - 30 * 60_000, fill + 60 * 60_000
    now = fill + 70 * 60_000
    # бар филла: high 104 (пик до входа), последняя цена 101 — не активация
    assert ex.stream_tick("TTTUSDT", 104.0, 99.0, 101.0, bar_fill, now) == []
    # следующий бар: high 103.2 прошёл +1.5R, хотя последняя цена уже 102.5
    assert ex.stream_tick("TTTUSDT", 103.2, 101.0, 102.5, bar_next, now) == [1001]
    # повтор раньше ACT_RETRY_S — тишина (активация идемпотентна, API бережём)
    assert ex.stream_tick("TTTUSDT", 103.5, 101.0, 103.4, bar_next, now) == []
    # до филла журнала ход сделки не считается
    ex._ACT_TRIED.clear()
    assert ex.stream_tick("TTTUSDT", 110.0, 99.0, 110.0, bar_fill, fill - 1) == []
    assert "TTTUSDT" in ex._STREAM_SEEN


def test_activate_trail_from_stream_places_once(fresh_db, monkeypatch):
    """Стрим увидел +1.5R: трейл встаёт сразу (не на такте), один раз; строка — в отчёт такта."""
    from app import db
    from app.v2 import bingx_executor as ex
    _seed_native(1002)
    fake = _StreamEx(last=103.1)
    _arm_env(monkeypatch, ex, fake)
    out = ex.activate_trail(1002)
    assert fake.created == [("trail", 0.4, "sell", 10.0)]
    assert db.query_one("SELECT trail_order_id FROM executor_orders WHERE signal_id=1002")[
        "trail_order_id"] == "TR1"
    assert any("трейлинг-стоп на бирже" in x and "стрим Bybit" in x for x in out)
    assert ex.activate_trail(1002) == [] and len(fake.created) == 1   # повтор — без ордера
    assert any("стрим Bybit" in x for x in ex._drain_report())


def test_activate_trail_skips_signal_held_by_reconcile(fresh_db, monkeypatch):
    """Такт сверки держит замок сигнала (закрывает позицию) — быстрый путь уступает."""
    import threading

    from app.v2 import bingx_executor as ex
    _seed_native(1003)
    fake = _StreamEx(last=103.1)
    _arm_env(monkeypatch, ex, fake)
    held, release = threading.Event(), threading.Event()

    def holder():
        with ex._sid_lock(1003):
            held.set()
            release.wait(5)
    th = threading.Thread(target=holder)
    th.start()
    held.wait(5)
    try:
        assert ex.activate_trail(1003) == [] and fake.created == []
    finally:
        release.set()
        th.join(5)


def test_activate_trail_lost_response_keeps_claim_then_recovers(fresh_db, monkeypatch):
    """Ответ create потерян: метка-захват остаётся (второго трейла не будет ни из потока, ни
    после рестарта), такт находит трейл в листинге и записывает его id."""
    from ccxt import RequestTimeout

    from app import db
    from app.v2 import bingx_executor as ex
    _seed_native(1004)

    def lost(fake):
        fake.opens.append({"id": "TR9", "info": {"type": "TRAILING_STOP_MARKET",
                                                 "positionSide": "LONG", "origQty": "10"}})
        raise RequestTimeout("timed out")
    fake = _StreamEx(last=103.1, trail_behaviour=lost)
    _arm_env(monkeypatch, ex, fake)
    ex.activate_trail(1004)
    oid = db.query_one("SELECT trail_order_id FROM executor_orders WHERE signal_id=1004")[
        "trail_order_id"]
    assert oid.startswith(ex._PENDING_TRAIL)
    ex._ACT_TRIED.clear()
    assert ex.activate_trail(1004) == [] and len(fake.created) == 1   # захват держит
    assert ex._sync_native_trails(fake, []) == {1004}                # такт: листинг -> id
    assert db.query_one("SELECT trail_order_id FROM executor_orders WHERE signal_id=1004")[
        "trail_order_id"] == "TR9"


def test_activate_trail_rejection_releases_claim(fresh_db, monkeypatch):
    """Биржа однозначно отвергла трейл — захват снимается, следующая попытка возможна."""
    from ccxt import ExchangeError

    from app import db
    from app.v2 import bingx_executor as ex
    _seed_native(1005)

    def reject(fake):
        raise ExchangeError('bingx {"code":109400,"msg":"bad"}')
    fake = _StreamEx(last=103.1, trail_behaviour=reject)
    _arm_env(monkeypatch, ex, fake)
    out = ex.activate_trail(1005)
    assert any("не встал" in x for x in out)
    assert db.query_one("SELECT trail_order_id FROM executor_orders WHERE signal_id=1005")[
        "trail_order_id"] is None


def test_tick_defers_activation_to_live_stream(fresh_db):
    """#695 SOL 27.10: цена демо-стакана BingX провалилась на +1.52R, рынок журнала — нет;
    опрос активировал трейл, выход +1.30R против +2.92R журнала. При живом стриме такт
    тикер не опрашивает и по цене BingX трейл не ставит; без стрима — прежнее правило."""
    import time

    from app.v2 import bingx_executor as ex
    _seed_native(1006)
    fake = _StreamEx(last=103.1)                               # BingX: +1.55R
    ex._STREAM_SEEN["TTTUSDT"] = time.monotonic()              # стрим жив, Bybit не прошёл
    assert ex._sync_native_trails(fake, []) == set()
    assert fake.created == [] and fake.ticker_calls == 0
    ex._STREAM_SEEN["TTTUSDT"] = time.monotonic() - ex.STREAM_STALE_S - 1   # стрим молчит
    assert ex._sync_native_trails(fake, []) == {1006}
    assert fake.created == [("trail", 0.4, "sell", 10.0)]


def test_journal_trail_beyond_closes_even_with_native_trail(fresh_db):
    """Трейл встал поздно (обрыв сети), журнал на закрытии бара подтянул свой выше, цена
    BingX уже за ним: журнал засчитает выход — снимаем трейл и выходим по рынку. Сверка —
    раз на уровень журнала."""
    from app import db
    from app.v2 import bingx_executor as ex
    _seed_native(1007, trail_stop=103.6)
    db.execute("UPDATE executor_orders SET trail_order_id='TR5' WHERE signal_id=1007")
    fake = _StreamEx(last=103.4)
    report: list = []
    assert ex._sync_native_trails(fake, report) == {1007}
    assert "TR5" in fake.cancelled and ("market", "sell", 10.0) in fake.created
    assert db.query_one("SELECT state FROM executor_orders WHERE signal_id=1007")["state"] \
        == "closed"
    assert any("трейлом журнала" in x for x in report)


def test_journal_level_checked_once_per_level(fresh_db):
    from app import db
    from app.v2 import bingx_executor as ex
    _seed_native(1008, trail_stop=103.6)
    db.execute("UPDATE executor_orders SET trail_order_id='TR6' WHERE signal_id=1008")
    fake = _StreamEx(last=104.5)                               # цена выше уровня — держим
    ex._sync_native_trails(fake, [])
    ex._sync_native_trails(fake, [])
    assert fake.ticker_calls == 1 and fake.created == []


def test_place_fresh_places_new_signal_before_tick_and_stamps_version(fresh_db, monkeypatch):
    """#698/#704: вход ставился на такте (до 60 с+), стрим засчитывал касание раньше.
    Быстрый путь ставит триггер сразу; такт после этого второго ордера не ставит."""
    from app import db, runtime
    from app.v2 import bingx_executor as ex
    fake = _PlaceEx(lambda f, p: {"id": "T7"})
    _seed_place(1009)
    _patch_net(monkeypatch, ex, fake)
    runtime.set_value("executor_enabled", "1")
    out = ex.place_fresh()
    assert any("триггер-маркет" in x for x in out) and len(fake.created) == 1
    row = db.query_one("SELECT state, order_id, execution_version FROM executor_orders "
                       "WHERE signal_id=1009")
    assert (row["state"], row["order_id"], row["execution_version"]) == \
        ("placed", "T7", ex.EXECUTION_VERSION)
    assert ex.place_fresh() == []                              # уже под учётом — без сети
    ex.reconcile()
    assert len(fake.created) == 1                              # такт не дублирует


def test_place_fresh_leaves_prefilled_signal_to_reconcile(fresh_db, monkeypatch):
    from app import db, runtime
    from app.v2 import bingx_executor as ex
    fake = _PlaceEx(lambda f, p: {"id": "T8"})
    _seed_place(1010)
    db.execute("UPDATE signals SET filled_at='2026-10-05T02:00:05+00:00' WHERE id=1010")
    _patch_net(monkeypatch, ex, fake)
    runtime.set_value("executor_enabled", "1")
    assert ex.place_fresh() == [] and fake.created == []
    ex.reconcile()                                             # пропуск оформляет такт
    row = db.query_one("SELECT state, note FROM executor_orders WHERE signal_id=1010")
    assert (row["state"], row["note"]) == ("error", ex.PREFILL_NOTE)


def test_fill_stamps_execution_version(fresh_db, monkeypatch):
    """Версия = код, при котором открылась позиция (он ведёт выход): пишется при филле."""
    from app import db
    from app.v2 import bingx_executor as ex
    _seed_place(1011)
    db.execute(
        "INSERT INTO executor_orders(signal_id, symbol, side, qty, entry_px, stop_px, "
        "client_id, state, created_at, order_id, execution_version) VALUES "
        "(1011,'GGG/USDT:USDT','LONG',5,100,98,'rsiE1011','placed',"
        "'2026-10-05T01:00:00+00:00','O1','e4')")
    monkeypatch.setattr(ex, "_entry_order", lambda e, t: {"id": "O1", "status": "closed",
                                                          "filled": 5, "average": 100.1})
    ex._sync_fill_states(object(), [])
    row = db.query_one("SELECT state, execution_version FROM executor_orders "
                       "WHERE signal_id=1011")
    assert (row["state"], row["execution_version"]) == ("filled", ex.EXECUTION_VERSION)


def test_execution_report_prefill_is_own_category(fresh_db):
    """«Филл в журнале раньше постановки» — не ошибка исполнения: отдельная строка с R
    журнала, в ошибках и парах не участвует."""
    from app import db
    from app.v2 import bingx_executor as ex
    db.execute(
        "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, created_at, result_r) VALUES "
        "(1012,'HHH/USDT:USDT','LONG','1h',10,9,2.0,0.7,'{}','stopped',"
        "'2026-10-24T19:00:00+00:00',-1.0)")
    db.execute(
        "INSERT INTO executor_orders(signal_id, symbol, side, qty, entry_px, stop_px, "
        "client_id, state, note, created_at, updated_at) VALUES (1012,'HHH/USDT:USDT','LONG',"
        "0,10,9,'','error',?,'2026-10-24T20:01:27+00:00','2026-10-24T20:01:27+00:00')",
        (ex.PREFILL_NOTE,))
    txt = ex.execution_report("2026-10-24T00:00:00+00:00", "2026-10-31T00:00:00+00:00")
    assert "Не исполнимо" in txt and "#1012" in txt and "−1.00R" not in txt
    assert "-1.00R" in txt and "ошибок: 0" in txt


def test_execution_report_vst_price_artifact(fresh_db, monkeypatch):
    """Сбой цены демо (#825 05.11): шорт SOL ликвидирован на VST по 101.264, а на Bybit цена
    за жизнь позиции не выходила из 89.14–97.0. Такая пара — отдельной строкой и не в парах;
    пара, где демо тоже намного хуже журнала, но цена закрытия на рынке была, — в парах."""
    import numpy as np

    from app import db
    from app.data import minute_bars
    from app.v2 import bingx_executor as ex
    bars = np.array([[0.0, 96.0, 97.0, 89.14, 92.0]])
    monkeypatch.setattr(minute_bars, "load_window", lambda *a, **k: bars)
    rows = ((1101, 2.4898, 101.264, "2026-11-04T18:00:00+00:00"),
            (1102, 1.5, 96.9, "2026-11-04T19:00:00+00:00"))
    for sid, j_r, close, created in rows:
        db.execute(
            "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
            "confidence, snapshot_json, status, created_at, filled_at, closed_at, result_r, "
            "exit_price) VALUES (?,'SOL/USDT:USDT','SHORT','1h',96.615,98.13,2.0,0.7,'{}',"
            "'stopped',?,'2026-11-05T03:15:33+00:00',"
            "'2026-11-05T15:00:00+00:00',?,92.843)", (sid, created, j_r))
        db.execute(
            "INSERT INTO executor_orders(signal_id, symbol, side, qty, entry_px, stop_px, "
            "client_id, state, created_at, updated_at, fill_px, fill_qty, close_px, closed_at) "
            "VALUES (?,'SOL/USDT:USDT','SHORT',15.52,96.615,98.13,?,'closed',"
            "'2026-11-04T19:00:11+00:00','2026-11-05T16:01:26+00:00',96.491,15.52,?,"
            "'2026-11-05T16:01:26+00:00')", (sid, f"rsiE{sid}", close))
    txt = ex.execution_report("2026-11-05T00:00:00+00:00", "2026-11-06T00:00:00+00:00")
    assert "Сбой цены демо (VST): 1" in txt and "#1101" in txt and "-3.15R" in txt
    assert "Закрытые зеркальные (1)" in txt
    # минуток нет — не доказано: пара остаётся в сравнении
    monkeypatch.setattr(minute_bars, "load_window", lambda *a, **k: None)
    txt = ex.execution_report("2026-11-05T00:00:00+00:00", "2026-11-06T00:00:00+00:00")
    assert "Сбой цены демо" not in txt and "Закрытые зеркальные (2)" in txt


def test_close_beyond_journal_trail_cancels_moved_hourly_stop(fresh_db):
    """Выход по рынку «цена за трейлом журнала» уводит строку сразу в closed — ветка close
    такта переставленный почасовой стоп ('sl:' в note) уже не снимет; снимаем здесь, иначе
    reduce-only сирота съела бы позицию следующего сигнала того же символа/стороны."""
    from app import db
    from app.v2 import bingx_executor as ex
    _seed_native(1013, trail_stop=103.6)
    db.execute("UPDATE executor_orders SET note='sl:SL7' WHERE signal_id=1013")
    fake = _StreamEx(last=103.0)
    ex._STREAM_SEEN["TTTUSDT"] = 0.0                           # стрим давно молчит
    assert ex._sync_native_trails(fake, []) == set()
    assert "SL7" in fake.cancelled and ("market", "sell", 10.0) in fake.created


# --- 10.11.2026: время сделок биржи в учёте (миграция 033), сверка цены демо с рынком в минуту
# закрытия (#776) и строка о поздних входах (docs/research/2026-11-10-demo-gap-e5.md) ---------
def _seed_ts_order(sid):
    """Сигнал GGG LONG 100/98 и его строка учёта (позиция открыта) без цен и времени сделок.
    -> строка учёта."""
    from app import db
    db.execute(
        "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, created_at) VALUES "
        "(?,'GGG/USDT:USDT','LONG','1h',100,98,2.0,0.7,'{}','open',?)",
        (sid, f"2026-11-10T00:{sid % 60:02d}:00+00:00"))
    db.execute(
        "INSERT INTO executor_orders(signal_id, symbol, side, qty, entry_px, stop_px, "
        "client_id, state, created_at) VALUES (?,'GGG/USDT:USDT','LONG',10,100,98,?,'filled',"
        "'2026-11-10T01:00:00+00:00')", (sid, f"rsiE{sid}"))
    return db.query_one("SELECT * FROM executor_orders WHERE signal_id=?", (sid,))


def _order_times(sid):
    from app import db
    return db.query_one("SELECT fill_px, fill_ts, close_px, close_ts, closed_at "
                        "FROM executor_orders WHERE signal_id=?", (sid,))


class _TradesEx:
    """Биржа с историей своих сделок GGG LONG: (сторона, цена, время ISO)."""

    def __init__(self, *trades):
        self.trades = [{"side": side, "price": px, "timestamp": _ms(at),
                        "info": {"positionSide": "LONG"}} for side, px, at in trades]

    def fetch_my_trades(self, symbol, since=None, limit=20):
        return [tr for tr in self.trades if since is None or tr["timestamp"] >= since]


def test_migration_033_trade_time_columns(fresh_db):
    """Миграция 033: в учёте есть время сделок биржи; бэкфилла нет — у строки без них NULL."""
    cols = {r["name"]: r["type"] for r in fresh_db.query("PRAGMA table_info(executor_orders)")}
    assert (cols.get("fill_ts"), cols.get("close_ts")) == ("TEXT", "TEXT")
    assert fresh_db.query_one("SELECT 1 FROM schema_migrations WHERE name=?",
                              ("033_executor_trade_times.sql",)) is not None
    _seed_ts_order(1201)
    row = _order_times(1201)
    assert (row["fill_ts"], row["close_ts"]) == (None, None)


def test_ms_iso_exchange_time():
    """Время биржи (мс) -> ISO UTC, как в колонках *_at; пусто, ноль и мусор — None, а не
    исключение (запись времени сделки не роняет такт)."""
    from app.v2 import bingx_executor as ex
    sec = _ms("2026-10-31T19:58:12+00:00")
    assert ex._ms_iso(sec) == "2026-10-31T19:58:12+00:00"
    assert ex._iso_ms(ex._ms_iso(sec)) == sec
    assert ex._ms_iso(sec + 345) == "2026-10-31T19:58:12.345000+00:00"   # миллисекунды целы
    assert ex._ms_iso(str(sec)) == ex._ms_iso(float(sec)) == ex._ms_iso(sec)
    for junk in (None, 0, -5, "", "мусор", float("nan"), float("inf"), 10 ** 30, {}):
        assert ex._ms_iso(junk) is None


def test_record_fill_writes_trade_time_once(fresh_db):
    """Время входа из ордера биржи: последняя сделка (lastTradeTimestamp), нет её — время
    самого ордера. Уже записанное не перетирается; ордер без времени оставляет NULL."""
    from app.v2 import bingx_executor as ex
    placed, traded = _ms("2026-11-10T02:00:05+00:00"), _ms("2026-11-10T02:31:07+00:00")
    _seed_ts_order(1202)
    ex._record_fill(1202, {"filled": 10, "average": 100.05, "timestamp": placed,
                           "lastTradeTimestamp": traded})
    assert _order_times(1202)["fill_ts"] == "2026-11-10T02:31:07+00:00"
    ex._record_fill(1202, {"filled": 10, "average": 100.05,
                           "lastTradeTimestamp": traded + 3_600_000})
    assert _order_times(1202)["fill_ts"] == "2026-11-10T02:31:07+00:00"   # не перетёрто
    _seed_ts_order(1203)
    ex._record_fill(1203, {"filled": 10, "average": 100.05, "timestamp": placed,
                           "lastTradeTimestamp": None})
    assert _order_times(1203)["fill_ts"] == "2026-11-10T02:00:05+00:00"
    _seed_ts_order(1204)
    ex._record_fill(1204, {"filled": 10, "average": 100.05, "timestamp": "мусор"})
    row = _order_times(1204)
    assert (row["fill_px"], row["fill_ts"]) == (100.05, None)   # цена записана и без времени


def test_entry_from_trades_writes_trade_time_once(fresh_db):
    """Сработавший триггер: время входа — время ВЫБРАННОГО трейда (ближайшего к уровню
    входа), а не первого попавшегося; повторный вызов записанное не перетирает."""
    from app.v2 import bingx_executor as ex
    t = _seed_ts_order(1205)
    fake = _TradesEx(("buy", 104.0, "2026-11-10T01:30:00+00:00"),      # сосед по позиции
                     ("buy", 100.1, "2026-11-10T02:31:07+00:00"))      # наш вход
    ex._record_entry_from_trades(fake, t)
    row = _order_times(1205)
    assert (row["fill_px"], row["fill_ts"]) == (100.1, "2026-11-10T02:31:07+00:00")
    ex._record_entry_from_trades(_TradesEx(("buy", 100.0, "2026-11-10T03:00:00+00:00")), t)
    row = _order_times(1205)
    assert (row["fill_px"], row["fill_ts"]) == (100.1, "2026-11-10T02:31:07+00:00")


def test_record_close_writes_order_time_once(fresh_db, monkeypatch):
    """Рыночное закрытие: время сделки — из ордера (последняя сделка, иначе время ордера,
    в том числе дочитанного fetch_order); биржа времени не дала — текущее: позицию только
    что закрыли сами. Уже записанное не перетирается."""
    from app import clock
    from app.v2 import bingx_executor as ex
    monkeypatch.setattr(clock, "utcnow_iso", lambda: "2026-11-10T05:00:00+00:00")
    sent, traded = _ms("2026-11-10T04:59:58+00:00"), _ms("2026-11-10T04:59:59+00:00")

    class _Ex:
        def fetch_order(self, oid, symbol):                    # ответ create был без цены
            return {"id": oid, "average": 103.0, "timestamp": sent}

    t = _seed_ts_order(1206)
    ex._record_close(_Ex(), t, {"average": 103.0, "timestamp": sent,
                                "lastTradeTimestamp": traded})
    row = _order_times(1206)
    assert (row["close_px"], row["close_ts"]) == (103.0, "2026-11-10T04:59:59+00:00")
    assert row["closed_at"] == "2026-11-10T05:00:00+00:00"     # когда заметил executor
    ex._record_close(_Ex(), t, {"average": 103.0, "lastTradeTimestamp": traded + 60_000})
    assert _order_times(1206)["close_ts"] == "2026-11-10T04:59:59+00:00"   # не перетёрто
    t = _seed_ts_order(1207)
    ex._record_close(_Ex(), t, {"id": "M7"})                   # время — из fetch_order
    assert _order_times(1207)["close_ts"] == "2026-11-10T04:59:58+00:00"
    t = _seed_ts_order(1208)
    ex._record_close(_Ex(), t, {"average": 103.0})             # времени нет — текущее
    assert _order_times(1208)["close_ts"] == "2026-11-10T05:00:00+00:00"


def test_stop_close_writes_trade_time_once(fresh_db, monkeypatch):
    """Позицию закрыла сама биржа (стоп, трейл, ликвидация): close_ts — время выбранного
    закрывающего трейда, closed_at — когда это заметил executor (#825: на 8 часов позже).
    Повторный вызов записанное не перетирает."""
    from app import clock
    from app.v2 import bingx_executor as ex
    monkeypatch.setattr(clock, "utcnow_iso", lambda: "2026-11-10T12:00:00+00:00")
    t = _seed_ts_order(1209)
    fake = _TradesEx(("buy", 100.1, "2026-11-10T02:31:07+00:00"),      # вход
                     ("sell", 97.9, "2026-11-10T03:47:47+00:00"))      # стоп биржи
    ex._record_stop_close(fake, t)
    row = _order_times(1209)
    assert (row["close_px"], row["close_ts"]) == (97.9, "2026-11-10T03:47:47+00:00")
    assert row["closed_at"] == "2026-11-10T12:00:00+00:00"
    later = _TradesEx(("buy", 100.1, "2026-11-10T02:31:07+00:00"),
                      ("sell", 98.0, "2026-11-10T04:10:00+00:00"))
    ex._record_stop_close(later, t)
    row = _order_times(1209)
    assert (row["close_px"], row["close_ts"]) == (97.9, "2026-11-10T03:47:47+00:00")


def test_close_position_price_and_time_from_same_trade(fresh_db, monkeypatch):
    """Ответ create без цены, fetch_order не дочитался: цену закрытия добирает
    _record_stop_close из трейдов — и время берётся от ТОГО ЖЕ трейда, а не «сейчас»:
    отчёт сверяет цену с рынком именно в минуту close_ts."""
    from app import clock
    from app.v2 import bingx_executor as ex
    monkeypatch.setattr(clock, "utcnow_iso", lambda: "2026-11-10T05:00:00+00:00")

    class _Ex(_TradesEx):
        def fetch_positions(self, symbols):
            return [{"symbol": symbols[0], "side": "long", "contracts": 10.0}]

        def create_order(self, symbol, typ, side, qty, price, params=None):
            return {"id": "M9"}

        def fetch_order(self, oid, symbol):
            raise RuntimeError("timeout")

    t = _seed_ts_order(1210)
    fake = _Ex(("buy", 100.1, "2026-11-10T02:31:07+00:00"),
               ("sell", 98.2, "2026-11-10T04:59:57+00:00"))
    assert ex._close_position(fake, t, max_qty=10.0) == 10.0
    row = _order_times(1210)
    assert (row["close_px"], row["close_ts"]) == (98.2, "2026-11-10T04:59:57+00:00")
    assert row["closed_at"] == "2026-11-10T05:00:00+00:00"


def _minute_feed(monkeypatch, bars):
    """Минутки Bybit для отчёта: load_window отдаёт из bars минуты запрошенного окна
    [start, end); bars=None — минуток нет. -> список запрошенных окон (symbol, start, end)."""
    from app.data import minute_bars
    calls = []

    def load_window(symbol, start_ms, end_ms, **kw):
        calls.append((symbol, start_ms, end_ms))
        if bars is None:
            return None
        return bars[(bars[:, 0] >= start_ms) & (bars[:, 0] < end_ms)]
    monkeypatch.setattr(minute_bars, "load_window", load_window)
    return calls


def _ena_minutes():
    """Bybit 31.10 по мотивам #776 (ENA шорт, вход 0.193315, 1R = 0.002755): до 19:30 цена
    у входа, в 19:30–19:43 проходит 0.1918, с 19:44 рынок уже у +1.6…+1.75R."""
    import numpy as np
    rows = []
    for ts in range(_ms("2026-10-31T18:27:00+00:00"), _ms("2026-10-31T20:03:00+00:00"), 60_000):
        if ts < _ms("2026-10-31T19:30:00+00:00"):
            hi, lo = 0.19345, 0.19250
        elif ts < _ms("2026-10-31T19:44:00+00:00"):
            hi, lo = 0.19200, 0.19150
        else:
            hi, lo = 0.18905, 0.18850
        rows.append([ts, hi, hi, lo, lo])
    return np.array(rows)


def _seed_ena_pair(sid, j_r, close_ts, close_px=0.1918):
    """Закрытая пара ENA шорт: вход демо 0.1929, закрытие 0.1918 (+0.40R от своего входа) —
    цена замершей ленты VST; executor заметил закрытие в 20:01:26."""
    from app import db
    db.execute(
        "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, created_at, filled_at, closed_at, result_r, "
        "exit_price) VALUES (?,'ENA/USDT:USDT','SHORT','1h',0.193315,0.19607,2.0,0.7,'{}',"
        "'stopped',?,'2026-10-31T18:27:00+00:00','2026-10-31T20:00:00+00:00',?,0.18899)",
        (sid, f"2026-10-31T17:00:{sid % 60:02d}+00:00", j_r))
    db.execute(
        "INSERT INTO executor_orders(signal_id, symbol, side, qty, entry_px, stop_px, "
        "client_id, state, created_at, updated_at, fill_px, fill_qty, close_px, closed_at, "
        "close_ts) VALUES (?,'ENA/USDT:USDT','SHORT',7799,0.193315,0.19607,?,'closed',"
        "'2026-10-31T17:01:00+00:00','2026-10-31T20:01:26+00:00',0.1929,7799,?,"
        "'2026-10-31T20:01:26+00:00',?)", (sid, f"rsiE{sid}", close_px, close_ts))


_ENA_WEEK = ("2026-10-31T00:00:00+00:00", "2026-11-01T00:00:00+00:00")


def test_execution_report_checks_close_price_at_close_minute(fresh_db, monkeypatch):
    """#776 ENA шорт 31.10: лента демо замерла, рынок ушёл дальше, позиция закрылась по
    замершей цене 0.1918. Она ВНУТРИ диапазона всей сделки, а демо хуже журнала на 1.17R —
    прежнее правило (1.5R, диапазон за жизнь позиции) молчит. С временем закрывающей сделки
    цена сверяется с минутами Bybit вокруг него (порог 0.5R, допуск 0.5%):
      1301 — в 19:58 рынок был у 0.1885–0.1890, цены 0.1918 не было -> «Сбой цены демо»;
      1302 — та же пара, но закрыта в 19:36, когда рынок шёл через 0.1918 -> в парах;
      1303 — закрыта в 19:58 по цене рынка 0.1889, журнал +2.5R: демо просто хуже -> в парах;
      1304 — как 1301, но без close_ts (строка до миграции 033) -> прежнее правило: в парах;
      1305 — без close_ts и хуже журнала на 1.8R: сверка с диапазоном всей сделки -> в парах."""
    from app.v2 import bingx_executor as ex
    _seed_ena_pair(1301, 1.57, "2026-10-31T19:58:12+00:00")
    _seed_ena_pair(1302, 1.57, "2026-10-31T19:36:40+00:00")
    _seed_ena_pair(1303, 2.5, "2026-10-31T19:58:40+00:00", close_px=0.1889)
    _seed_ena_pair(1304, 1.57, None)
    _seed_ena_pair(1305, 2.2, None)
    calls = _minute_feed(monkeypatch, _ena_minutes())
    txt = ex.execution_report(*_ENA_WEEK)
    assert ("Сбой цены демо (VST): 1 · журнал +1.57R · демо +0.40R — закрытие по цене, "
            "которой в тот момент не было на Bybit; в пары не входят: #1301") in txt.split("\n")
    assert "Закрытые зеркальные (4)" in txt
    assert not any(f"#{sid}" in txt for sid in (1302, 1303, 1304, 1305))
    # окна: с close_ts — минута до сделки, её минута и минута после; без него и хуже 1.5R —
    # от филла журнала до закрытия executor (+1 мин); без него и не хуже 1.5R — не сверяется
    sym = "ENA/USDT:USDT"
    close_minutes = (sym, _ms("2026-10-31T19:57:00+00:00"), _ms("2026-10-31T20:00:00+00:00"))
    assert sorted(calls) == sorted([
        close_minutes, close_minutes,
        (sym, _ms("2026-10-31T19:35:00+00:00"), _ms("2026-10-31T19:38:00+00:00")),
        (sym, _ms("2026-10-31T18:27:00+00:00"), _ms("2026-10-31T20:02:26+00:00"))])


def test_execution_report_close_minute_unproven_keeps_pair(fresh_db, monkeypatch):
    """Минуток вокруг закрывающей сделки нет (сеть, биржа) или среди них нет минуты самой
    сделки (ещё не закрылась) — сбой цены не доказан: пара остаётся в сравнении. Демо хуже
    журнала не больше чем на 0.5R — рынок не запрашивается вовсе."""
    from app import db
    from app.v2 import bingx_executor as ex
    _seed_ena_pair(1306, 1.57, "2026-10-31T19:58:12+00:00")
    bars = _ena_minutes()
    _minute_feed(monkeypatch, None)
    txt = ex.execution_report(*_ENA_WEEK)
    assert "Сбой цены демо" not in txt and "Закрытые зеркальные (1)" in txt
    # отчёт собран в 19:58: закрыта только минута 19:57 — по ней одной не судим
    _minute_feed(monkeypatch, bars[bars[:, 0] < _ms("2026-10-31T19:58:00+00:00")])
    txt = ex.execution_report(*_ENA_WEEK)
    assert "Сбой цены демо" not in txt and "Закрытые зеркальные (1)" in txt
    # с минутой сделки — доказано (минута «после» ещё не закрылась, и ждать её незачем)
    _minute_feed(monkeypatch, bars[bars[:, 0] < _ms("2026-10-31T19:59:00+00:00")])
    assert "Сбой цены демо (VST): 1" in ex.execution_report(*_ENA_WEEK)
    # журнал +0.85R, демо +0.40R: хуже на 0.45R — ниже порога, минутки не нужны
    db.execute("UPDATE signals SET result_r=0.85 WHERE id=1306")
    calls = _minute_feed(monkeypatch, bars)
    txt = ex.execution_report(*_ENA_WEEK)
    assert calls == [] and "Сбой цены демо" not in txt and "Закрытые зеркальные (1)" in txt


def _seed_late_entry(sid, judge, filled_at, fill_ts):
    """Открытая позиция демо: журнал засчитал вход в filled_at, биржа исполнила в fill_ts.
    judge — версия судьи в снапшоте сигнала ('1m'; None — часовой, как у прежних сигналов)."""
    import json

    from app import db
    trail = {"activate_r": 1.5, "distance_r": 0.2, "tp_exit": False, "max_hold_hours": 168}
    if judge:
        trail["judge"] = judge
    db.execute(
        "INSERT INTO signals(id, symbol, side, timeframe, entry, stop, risk_reward, "
        "confidence, snapshot_json, status, created_at, filled_at) VALUES "
        "(?,'TTT/USDT:USDT','LONG','1h',100,98,2.0,0.7,?,'open',?,?)",
        (sid, json.dumps({"trail": trail}), f"2026-11-10T08:{sid % 60:02d}:00+00:00",
         filled_at))
    db.execute(
        "INSERT INTO executor_orders(signal_id, symbol, side, qty, entry_px, stop_px, "
        "client_id, state, created_at, updated_at, fill_px, fill_qty, fill_ts) VALUES "
        "(?,'TTT/USDT:USDT','LONG',10,100,98,?,'filled','2026-11-10T09:00:00+00:00',"
        "'2026-11-10T13:00:00+00:00',100.1,10,?)", (sid, f"rsiE{sid}", fill_ts))


_LATE_WEEK = ("2026-11-10T00:00:00+00:00", "2026-11-11T00:00:00+00:00")
_TOUCH = "2026-11-10T10:00:00+00:00"
_LATE_LINE = "Вход на демо позже журнала на 30+ мин"


def test_execution_report_late_entries_line(fresh_db):
    """Вход на демо позже журнала (4 сделки e5 из 31: на 33–176 минут). Сравнимы только
    сигналы минутного судьи — у часового filled_at может быть началом бара; порог 30 минут
    включительно; id — самые поздние первыми. Сравнимых входов нет — строки нет."""
    from app import db
    from app.v2 import bingx_executor as ex
    _seed_late_entry(1311, "1m", _TOUCH, "2026-11-10T10:29:59+00:00")      # 29:59 — не поздний
    _seed_late_entry(1312, "1m", _TOUCH, "2026-11-10T10:30:00+00:00")      # ровно 30 минут
    _seed_late_entry(1313, "1m", _TOUCH, "2026-11-10T12:56:00+00:00")      # 176 минут (#803)
    _seed_late_entry(1314, None, _TOUCH, "2026-11-10T13:00:00+00:00")      # часовой судья
    _seed_late_entry(1315, "1m", _TOUCH, None)                             # времени сделки нет
    _seed_late_entry(1316, "1m", None, "2026-11-10T12:00:00+00:00")        # журнал вход не засчитал
    _seed_late_entry(1317, "1m", "не дата", "2026-11-10T12:00:00+00:00")   # битое время журнала
    _seed_late_entry(1318, "1m", _TOUCH, "2026-11-10T12:00:00+00:00")      # снапшот без трейла
    db.execute("UPDATE signals SET snapshot_json='{}' WHERE id=1318")
    txt = ex.execution_report(*_LATE_WEEK)
    assert f"{_LATE_LINE}: 2 из 3 — #1313, #1312" in txt.split("\n")
    assert ex._late_entries(db.query(
        "SELECT eo.*, s.filled_at AS j_filled_at, s.snapshot_json FROM executor_orders eo "
        "JOIN signals s ON s.id = eo.signal_id")) == ([1313, 1312], 3)
    # поздних нет — счётчик остаётся, id не печатаются
    db.execute("UPDATE executor_orders SET fill_ts='2026-11-10T10:05:00+00:00' "
               "WHERE signal_id IN (1312, 1313)")
    assert f"{_LATE_LINE}: 0 из 3" in ex.execution_report(*_LATE_WEEK).split("\n")
    # сравнимых входов нет (остались часовой судья и строки без времени) — строки нет
    db.execute("UPDATE executor_orders SET fill_ts=NULL WHERE signal_id IN (1311, 1312, 1313)")
    txt = ex.execution_report(*_LATE_WEEK)
    assert txt is not None and _LATE_LINE not in txt


def test_execution_report_late_entries_lists_six_latest(fresh_db):
    from app.v2 import bingx_executor as ex
    for i in range(7):                                         # задержки 31…37 минут
        _seed_late_entry(1321 + i, "1m", _TOUCH, f"2026-11-10T10:{31 + i}:00+00:00")
    txt = ex.execution_report(*_LATE_WEEK)
    assert f"{_LATE_LINE}: 7 из 7 — #1327, #1326, #1325, #1324, #1323, #1322" in txt.split("\n")


# --- 10.11.2026: алерты «журнал открыт — позиция плоская» и FAILED нативного трейла (#825) ---
class _DivEx:
    """Биржа для сверки расхождений: позиция TTT LONG и статус трейлинг-ордера."""

    def __init__(self, pos=10.0, trail_status="NEW", broken=False):
        self.pos, self.trail_status, self.broken = pos, trail_status, broken
        self.pos_calls, self.order_calls = 0, 0

    def fetch_positions(self, symbols):
        self.pos_calls += 1
        if self.broken:
            raise RuntimeError("timeout")
        if not self.pos:
            return []                                          # BingX отдаёт только ненулевые
        return [{"symbol": "TTT/USDT:USDT", "side": "long", "contracts": self.pos}]

    def fetch_order(self, oid, symbol):
        self.order_calls += 1
        # ccxt сводит FAILED в 'canceled' — различаем по сырому статусу биржи
        return {"id": oid, "status": "canceled", "info": {"status": self.trail_status}}


def _judge_1m(sid):
    from app import db
    db.execute("UPDATE signals SET snapshot_json=replace(snapshot_json, '\"tp_exit\"', "
               "'\"judge\": \"1m\", \"tp_exit\"') WHERE id=?", (sid,))


_T0 = 1_794_000_000.0                                          # unix-секунды: «сейчас» тестов
_FLAT_TEXT = ("журнал открыт, а позиция на бирже плоская — биржа закрыла её сама, журнал на "
              "своём такте выход не записал (ликвидация, сбой демо или стоп, которого нет "
              "в журнале)")


def _poller_tick(ts):
    """Поллер (он же такт журнала) проснулся в момент ts."""
    from datetime import datetime, timezone

    from app import runtime
    runtime.set_last_poller_tick_at(datetime.fromtimestamp(ts, tz=timezone.utc).isoformat())


@pytest.mark.parametrize("minute_judge", [False, True])
def test_flat_position_alerts_once_after_journal_tick(fresh_db, minute_judge):
    """#825: журнал держит сделку, позиции на бирже нет. Журнал пишет исходы на такте
    поллера (раз в час, минутный судья — тоже): тишина, пока после обнаружения не прошёл
    такт и не истёк запас на оценку исходов; затем одна строка. Повтора нет ни следующим
    тактом, ни после рестарта (отметка в settings). Учёт executor не меняется."""
    from app import db, runtime
    from app.v2 import bingx_executor as ex
    _seed_native(1101)
    if minute_judge:
        _judge_1m(1101)
    _poller_tick(_T0 - 1800)                                   # такт ДО обнаружения
    fake, report = _DivEx(pos=0.0), []
    ex._divergence_alerts(fake, report, now=_T0)               # обнаружили: таймер пошёл
    ex._divergence_alerts(fake, report, now=_T0 + 86400)       # такта не было — хоть сутки
    assert report == []
    _poller_tick(_T0 + 1800)                                   # такт после обнаружения
    ex._divergence_alerts(fake, report, now=_T0 + 1800 + ex.FLAT_GRACE_S - 1)
    assert report == []                                        # такт ещё оценивает исходы
    ex._divergence_alerts(fake, report, now=_T0 + 1800 + ex.FLAT_GRACE_S)
    assert report == ["⚠️ #1101 TTT LONG: " + _FLAT_TEXT]
    assert runtime._raw("executor_flat_alerted") == "1101"
    calls = fake.pos_calls
    ex._divergence_alerts(fake, report, now=_T0 + 3600)        # следующий такт
    ex._FLAT_SEEN.clear()                                      # «рестарт»: память пуста
    _poller_tick(_T0 + 5400)
    ex._divergence_alerts(fake, report, now=_T0 + 7200)
    assert len(report) == 1 and fake.pos_calls == calls        # и позиции больше не читаем
    row = db.query_one("SELECT state, note FROM executor_orders WHERE signal_id=1101")
    assert (row["state"], row["note"]) == ("filled", None)


@pytest.mark.parametrize("tick", [None, "не дата"])
def test_flat_position_waits_when_poller_tick_unknown(fresh_db, tick):
    """Время такта поллера пусто или не разбирается — ждём такт, не алертим."""
    from app import runtime
    from app.v2 import bingx_executor as ex
    _seed_native(1102)
    if tick is not None:
        runtime.set_last_poller_tick_at(tick)
    fake, report = _DivEx(pos=0.0), []
    ex._divergence_alerts(fake, report, now=_T0)
    ex._divergence_alerts(fake, report, now=_T0 + 86400)
    assert report == [] and 1102 in ex._FLAT_SEEN


def test_flat_timer_resets_when_position_seen(fresh_db):
    from app.v2 import bingx_executor as ex
    _seed_native(1103)
    fake, report = _DivEx(pos=0.0), []
    ex._divergence_alerts(fake, report, now=_T0)
    _poller_tick(_T0 + 600)
    fake.pos = 10.0                                            # пустой ответ был разовым
    ex._divergence_alerts(fake, report, now=_T0 + 1200)
    assert report == [] and 1103 not in ex._FLAT_SEEN
    fake.pos = 0.0                                             # снова плоско: отсчёт заново,
    ex._divergence_alerts(fake, report, now=_T0 + 1260)        # прежний такт уже не в счёт
    assert report == []


def test_flat_position_with_closed_journal_is_silent(fresh_db):
    """Журнал закрыл сделку — это ветка close такта, не расхождение; биржу не опрашиваем."""
    from app import db
    from app.v2 import bingx_executor as ex
    _seed_native(1104)
    db.execute("UPDATE signals SET status='tp1', result_r=1.2 WHERE id=1104")
    _poller_tick(_T0 + 600)
    fake, report = _DivEx(pos=0.0), []
    ex._divergence_alerts(fake, report, now=_T0 + 1200)
    assert report == [] and fake.pos_calls == 0 and ex._FLAT_SEEN == {}


def test_flat_position_alerts_each_signal_of_shared_position(fresh_db):
    """Hedge: два сигнала одной монеты и стороны делят позицию. Объём ниже минимального
    шага — плоско, алерт по каждому; живая позиция (пусть и меньше учёта) — тишина."""
    from app import db
    from app.v2 import bingx_executor as ex
    _seed_native(1105)
    db.execute("UPDATE signals SET timeframe='4h' WHERE id=1105")   # ключ уникальности
    _seed_native(1106)
    fake, report = _DivEx(pos=12.0), []                        # частичное расхождение: 12 из 20
    ex._divergence_alerts(fake, report, now=_T0)
    assert report == [] and ex._FLAT_SEEN == {}
    fake.pos = 0.0005                                          # пыль меньше шага
    fake.markets = {"TTT/USDT:USDT": {"limits": {"amount": {"min": 0.001}}}}
    ex._divergence_alerts(fake, report, now=_T0 + 60)
    _poller_tick(_T0 + 600)
    ex._divergence_alerts(fake, report, now=_T0 + 600 + ex.FLAT_GRACE_S)
    assert [x.split()[1] for x in report] == ["#1105", "#1106"] and fake.pos_calls == 3
    ex._divergence_alerts(fake, report, now=_T0 + 3600)
    assert len(report) == 2


def test_flat_after_filled_native_trail_is_planned_exit(fresh_db):
    """Позицию закрыл исполнившийся нативный трейл — плановый выход: даже если журнал на
    своём такте его не записал, тишина; конечный статус трейла второй раз не читается."""
    from app import db
    from app.v2 import bingx_executor as ex
    _seed_native(1107)
    db.execute("UPDATE executor_orders SET trail_order_id='TR7' WHERE signal_id=1107")
    fake, report = _DivEx(pos=0.0, trail_status="FILLED"), []
    ex._divergence_alerts(fake, report, now=_T0)
    _poller_tick(_T0 + 600)
    ex._divergence_alerts(fake, report, now=_T0 + 3600)
    assert report == [] and fake.order_calls == 1


def test_native_trail_failed_alerts_once(fresh_db):
    """#825, 14:39: трейл встал на несуществующую позицию, биржа ответила FAILED. Одна
    строка; повтора нет ни следующим тактом, ни после рестарта, статус больше не читаем.
    Ставить заново не на что: счёт отказов не идёт, id остаётся и держит штатный путь. Решение
    окончательное: позиция по этой монете и стороне, появившаяся позже, — доля соседнего
    сигнала (hedge), трейл на неё не ставится."""
    from app import db, runtime
    from app.v2 import bingx_executor as ex
    _seed_native(1108)
    db.execute("UPDATE executor_orders SET trail_order_id='TR8' WHERE signal_id=1108")
    fake, report = _DivEx(pos=0.0, trail_status="FAILED"), []
    ex._divergence_alerts(fake, report, now=_T0)               # плоская впервые — ждём такт
    assert report == [] and runtime._raw("executor_trail_failed_alerted") is None
    ex._divergence_alerts(fake, report, now=_T0 + 60)          # плоская второй такт подряд
    assert report == ["⚠️ #1108 TTT/USDT:USDT: нативный трейл отклонён биржей (FAILED) — "
                      "трейла на бирже нет"]
    assert runtime._raw("executor_trail_failed_alerted") == "1108"
    ex._divergence_alerts(fake, report, now=_T0 + 120)
    ex._TRAIL_STATUS.clear()                                   # «рестарт»
    fake.pos = 10.0                                            # вошёл соседний сигнал
    ex._divergence_alerts(fake, report, now=_T0 + 3600)
    assert len(report) == 1 and fake.order_calls == 1
    row = db.query_one("SELECT state FROM executor_orders WHERE signal_id=1108")
    assert row["state"] == "filled" and _trail_row(1108) == ("TR8", None, None)
    assert ex.refresh_armed() == 0


def test_rejected_trail_single_empty_positions_reply_is_not_final(fresh_db):
    """Трейл отклонён, а биржа один раз ответила «позиций нет». Решение «трейла на бирже нет»
    по сигналу окончательное, поэтому по одному пустому ответу оно не принимается: на
    следующем такте позиция видна — это обычный отказ при живой позиции, со счётом и паузой."""
    from app import db, runtime
    from app.v2 import bingx_executor as ex
    _seed_native(1119)
    db.execute("UPDATE executor_orders SET trail_order_id='TR9' WHERE signal_id=1119")
    fake, report = _DivEx(pos=0.0, trail_status="FAILED"), []
    ex._divergence_alerts(fake, report, now=_T0)
    assert report == [] and _trail_row(1119) == ("TR9", None, None)
    fake.pos = 10.0
    ex._divergence_alerts(fake, report, now=_T0 + 60)
    assert len(report) == 1 and "попытка 1 из 3" in report[0]
    assert _trail_row(1119) == ("TR9", 1, _iso(_T0 + 60))
    assert runtime._raw("executor_trail_failed_alerted") is None
    assert fake.order_calls == 1                               # статус FAILED читали один раз


# --- 10.11.2026: отклонённый биржей трейл ставится заново — до 3 отказов, пауза 5 минут ------
class _RejectEx(_StreamEx):
    """Биржа, которая принимает нативный трейл (выдаёт id), а потом отклоняет его: сырой
    статус FAILED, как у #825. Позиция и прикреплённый ко входу аварийный стоп SL0 — на месте."""

    markets = {"TTT/USDT:USDT": {}}

    def __init__(self, last=103.1, raw="FAILED", **kw):
        super().__init__(last, **kw)
        self.raw, self.statuses = raw, {}                      # id трейла -> сырой статус
        self.broken, self.order_calls = False, 0

    def fetch_balance(self):
        return {"info": {"data": [{"asset": "VST", "equity": "100000"}]},
                "total": {"VST": 100000.0}}

    def create_order(self, symbol, typ, side, qty, price, params=None):
        params = params or {}
        if "trailingAmount" in params:
            oid = f"TR{len(self.statuses) + 1}"
            self.statuses[oid] = self.raw
            self.created.append(("trail", params["trailingAmount"], side, qty))
            return {"id": oid}
        return super().create_order(symbol, typ, side, qty, price, params)

    def fetch_positions(self, symbols):
        if self.broken:
            raise RuntimeError("timeout")
        if not self.pos:
            return []
        return [{"symbol": "TTT/USDT:USDT", "side": "long", "contracts": self.pos}]

    def fetch_order(self, oid, symbol):
        self.order_calls += 1
        return {"id": oid, "status": "canceled",
                "info": {"status": self.statuses.get(oid, "NEW")}}


def _trail_row(sid):
    from app import db
    r = db.query_one("SELECT trail_order_id, trail_fails, trail_failed_at FROM executor_orders "
                     "WHERE signal_id=?", (sid,))
    return r["trail_order_id"], r["trail_fails"], r["trail_failed_at"]


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _only_trails(fake):
    """Вход и аварийный стоп не тронуты: биржа получала только трейлинг-ордера и ни одной
    отмены, прикреплённый стоп SL0 по-прежнему стоит."""
    return (fake.cancelled == [] and all(c[0] == "trail" for c in fake.created)
            and [o["id"] for o in fake.opens] == ["SL0"])


def _restart():
    """«Рестарт» бота: всё, что executor держал в памяти, пропало — осталась БД."""
    from app.v2 import bingx_executor as ex
    ex.disarm_all()
    for cache in (ex._STREAM_SEEN, ex._ACT_TRIED, ex._JLEVEL_SEEN, ex._TRAIL_WARNED,
                  ex._FLAT_SEEN, ex._TRAIL_STATUS):
        cache.clear()
    ex._drain_report()


def _reject_lines(report):
    return [x for x in report if "отклонён" in x]


_RETRY_TEXT = ("⚠️ #{} TTT/USDT:USDT: нативный трейл отклонён биржей ({}), попытка 1 из 3 — "
               "поставлю заново не раньше чем через 5 минут")
_GIVEUP_TEXT = ("⚠️ #{} TTT/USDT:USDT: нативный трейл отклонён 3 раза — больше не ставлю, "
                "веду по трейлу журнала раз в час")


@pytest.mark.parametrize("raw", ["FAILED", "REJECTED"])
def test_rejected_trail_with_live_position_is_released_after_pause(fresh_db, monkeypatch, raw):
    """Биржа отклонила трейл, позиция жива. Отказ учтён (счёт 1, время) и ушёл админам одной
    строкой; мёртвый id стоит 5 минут — при нём ни стрим, ни такт трейл не ставят, — затем
    снимается, и сигнал снова ждёт активации штатным путём. Биржу при этом не трогаем."""
    from app.v2 import bingx_executor as ex
    _seed_native(1111)
    fake, report = _RejectEx(raw=raw), []
    _arm_env(monkeypatch, ex, fake)
    assert ex.activate_trail(1111)                             # стрим: биржа приняла трейл TR1
    ex._divergence_alerts(fake, report, now=_T0)               # такт: его статус — отказ
    assert _trail_row(1111) == ("TR1", 1, _iso(_T0))
    assert report == [_RETRY_TEXT.format(1111, raw)]
    ex._divergence_alerts(fake, report, now=_T0 + ex.TRAIL_RETRY_PAUSE_S - 1)
    assert _trail_row(1111) == ("TR1", 1, _iso(_T0)) and ex.refresh_armed() == 0
    assert ex.activate_trail(1111) == [] and ex._sync_native_trails(fake, []) == {1111}
    assert len(fake.created) == 1                              # пауза: трейл никто не ставит
    ex._divergence_alerts(fake, report, now=_T0 + ex.TRAIL_RETRY_PAUSE_S)
    assert _trail_row(1111) == (None, 1, _iso(_T0)) and ex.refresh_armed() == 1
    assert len(report) == 1 and fake.order_calls == 1 and _only_trails(fake)


def test_rejected_trail_three_times_goes_to_hourly_fallback(fresh_db, monkeypatch):
    """Три отказа подряд. После второго — без строки, после третьего — одна итоговая, id снят
    сразу. Дальше нативный трейл не ставит никто (refresh_armed, стрим, такт), «с живым
    трейлом» сигнал не числится — его берёт почасовой фолбэк и сам доводит до выхода: стоп
    за трейлом журнала, а когда цена уже за уровнем — закрытие по рынку."""
    from app import db
    from app.v2 import bingx_executor as ex
    _seed_native(1112)
    fake, report = _RejectEx(), []
    _arm_env(monkeypatch, ex, fake)
    ex.activate_trail(1112)                                    # №1 ставит стрим
    ex._divergence_alerts(fake, report, now=_T0)
    ex._divergence_alerts(fake, report, now=_T0 + 300)         # пауза вышла — id снят
    assert ex.activate_trail(1112)                             # №2 — снова штатный путь
    assert _trail_row(1112) == ("TR2", 1, None)                # отказ нового трейла не учтён
    ex._divergence_alerts(fake, report, now=_T0 + 400)         # новый id читается сразу
    assert _trail_row(1112) == ("TR2", 2, _iso(_T0 + 400)) and fake.order_calls == 2
    ex._divergence_alerts(fake, report, now=_T0 + 699)
    assert _trail_row(1112)[0] == "TR2" and len(report) == 1   # после второго — без строки
    ex._divergence_alerts(fake, report, now=_T0 + 700)
    assert _trail_row(1112) == (None, 2, _iso(_T0 + 400))
    assert ex._sync_native_trails(fake, []) == {1112}          # №3 ставит такт (стрим молчит)
    ex._divergence_alerts(fake, report, now=_T0 + 800)
    assert _trail_row(1112) == (None, 3, _iso(_T0 + 800))      # третий: id снят сразу
    assert report == [_RETRY_TEXT.format(1112, "FAILED"), _GIVEUP_TEXT.format(1112)]

    _restart()                                                 # и после рестарта — из БД
    db.execute("UPDATE signals SET trail_stop=103.6 WHERE id=1112")   # журнал подтянул трейл
    fake.last, tickers = 104.5, fake.ticker_calls

    def no_exchange():
        raise AssertionError("стрим пошёл на биржу по сигналу без нативного трейла")
    monkeypatch.setattr(ex, "_exchange_fast", no_exchange)
    assert ex.refresh_armed() == 0 and ex.activate_trail(1112) == []
    assert ex._sync_native_trails(fake, report) == set()       # не ставит и не числит живым
    ex._divergence_alerts(fake, report, now=_T0 + 4000)
    assert len(fake.statuses) == 3 and fake.ticker_calls == tickers and len(report) == 2
    assert _only_trails(fake)                                  # вход и аварийный стоп целы

    ex._sync_trail_stops(fake, report, skip=ex._sync_native_trails(fake, report))
    assert fake.created[-1] == ("stop", 103.6, 10.0) and fake.cancelled == ["SL0"]
    assert any("стоп подтянут 98 → 103.6" in x for x in report)
    db.execute("UPDATE signals SET trail_stop=104.6 WHERE id=1112")   # цена уже за уровнем
    fake.reject = True
    ex._sync_trail_stops(fake, report, skip=ex._sync_native_trails(fake, report))
    assert fake.created[-1] == ("market", "sell", 10.0) and fake.cancelled == ["SL0", "SL4"]
    assert db.query_one("SELECT state FROM executor_orders WHERE signal_id=1112")[
        "state"] == "closed"


def test_rejected_trail_waits_while_positions_unknown(fresh_db, monkeypatch):
    """Позиции не читаются — неизвестно, есть ли на что ставить: такт не меняет ни счёт, ни
    id (и по уже учтённому отказу мёртвый id не снимает). Прочитались — решает; статус
    отклонённого трейла второй раз не запрашивает."""
    from app.v2 import bingx_executor as ex
    _seed_native(1113)
    fake, report = _RejectEx(), []
    _arm_env(monkeypatch, ex, fake)
    ex.activate_trail(1113)
    fake.broken = True
    ex._divergence_alerts(fake, report, now=_T0)
    assert _trail_row(1113) == ("TR1", None, None)
    assert len(report) == 1 and "позиции не читаются" in report[0]
    fake.broken = False
    ex._divergence_alerts(fake, report, now=_T0 + 60)
    assert _trail_row(1113) == ("TR1", 1, _iso(_T0 + 60)) and fake.order_calls == 1
    fake.broken = True                                         # пауза вышла, а позиций нет
    ex._divergence_alerts(fake, report, now=_T0 + 360)
    assert _trail_row(1113) == ("TR1", 1, _iso(_T0 + 60))
    fake.broken = False
    ex._divergence_alerts(fake, report, now=_T0 + 420)
    assert _trail_row(1113) == (None, 1, _iso(_T0 + 60))
    assert _reject_lines(report) == [_RETRY_TEXT.format(1113, "FAILED")] and len(report) == 2
    assert _only_trails(fake)


def test_rejected_trail_is_not_released_while_position_flat(fresh_db, monkeypatch):
    """Отказ учтён при живой позиции, а к концу паузы позиция плоская: ставить не на что —
    id остаётся, счёт не растёт, новых строк нет. Позиция снова видна (пустой ответ был
    разовым) — id снимается, как после обычной паузы."""
    from app.v2 import bingx_executor as ex
    _seed_native(1114)
    fake, report = _RejectEx(), []
    _arm_env(monkeypatch, ex, fake)
    ex.activate_trail(1114)
    ex._divergence_alerts(fake, report, now=_T0)
    fake.pos = 0.0
    ex._divergence_alerts(fake, report, now=_T0 + 300)
    ex._divergence_alerts(fake, report, now=_T0 + 360)
    assert _trail_row(1114) == ("TR1", 1, _iso(_T0)) and len(report) == 1
    assert ex.refresh_armed() == 0
    fake.pos = 10.0
    ex._divergence_alerts(fake, report, now=_T0 + 420)
    assert _trail_row(1114) == (None, 1, _iso(_T0)) and len(report) == 1
    assert _only_trails(fake)


def test_rejected_trail_after_flat_alert_is_not_retried(fresh_db):
    """Позицию уже признали пропавшей (алерт «журнал открыт, а позиция плоская»): что бы ни
    стояло на бирже по этой монете и стороне потом, это не наша доля. Отклонённый трейл —
    прежний алерт, без счёта и без снятия id; позиции ради него не читаем."""
    from app import db, runtime
    from app.v2 import bingx_executor as ex
    _seed_native(1115)
    db.execute("UPDATE executor_orders SET trail_order_id='TR5' WHERE signal_id=1115")
    runtime.set_value("executor_flat_alerted", "1115")
    fake, report = _DivEx(pos=10.0, trail_status="FAILED"), []
    ex._divergence_alerts(fake, report, now=_T0)
    ex._divergence_alerts(fake, report, now=_T0 + 600)
    assert report == ["⚠️ #1115 TTT/USDT:USDT: нативный трейл отклонён биржей (FAILED) — "
                      "трейла на бирже нет"]
    assert _trail_row(1115) == ("TR5", None, None) and fake.pos_calls == 0


def test_rejected_trail_count_and_pause_survive_restart(fresh_db, monkeypatch):
    """Рестарт посреди паузы: память пуста, счёт и время отказа — в БД. Отказ того же трейла
    второй раз не считается, его статус не перечитывается, строка не повторяется, штатный
    путь при мёртвом id трейл не ставит; пауза идёт от записанного времени."""
    from app.v2 import bingx_executor as ex
    _seed_native(1116)
    fake, report = _RejectEx(), []
    _arm_env(monkeypatch, ex, fake)
    ex.activate_trail(1116)
    ex._divergence_alerts(fake, report, now=_T0)
    _restart()
    ex._divergence_alerts(fake, report, now=_T0 + 120)
    assert _trail_row(1116) == ("TR1", 1, _iso(_T0)) and len(report) == 1
    assert fake.order_calls == 1 and ex.refresh_armed() == 0
    assert ex.activate_trail(1116) == [] and ex._sync_native_trails(fake, []) == {1116}
    _restart()
    ex._divergence_alerts(fake, report, now=_T0 + 299)
    assert _trail_row(1116)[0] == "TR1"
    ex._divergence_alerts(fake, report, now=_T0 + 300)
    assert _trail_row(1116) == (None, 1, _iso(_T0)) and ex.refresh_armed() == 1
    assert len(report) == 1 and fake.order_calls == 1 and _only_trails(fake)


def test_rejected_trail_with_broken_time_is_released(fresh_db):
    """Время отказа в БД не разбирается (ручная правка): паузу считаем выдержанной —
    мёртвый id не должен остаться навсегда."""
    from app import db
    from app.v2 import bingx_executor as ex
    _seed_native(1117)
    db.execute("UPDATE executor_orders SET trail_order_id='TR7', trail_fails=1, "
               "trail_failed_at='не дата' WHERE signal_id=1117")
    fake = _DivEx(pos=10.0, trail_status="FAILED")
    ex._divergence_alerts(fake, [], now=_T0)
    assert _trail_row(1117) == (None, 1, "не дата") and fake.order_calls == 0


def test_stream_cannot_place_trail_after_last_reject(fresh_db):
    """Гонка со стримом: поток прочитал строку, пока отказов было два и id пуст, а до захвата
    дошёл после третьего отказа. Предел стоит в самом условии захвата — четвёртый трейл на
    биржу не уходит."""
    from app import db
    from app.v2 import bingx_executor as ex
    _seed_native(1118)
    db.execute("UPDATE executor_orders SET trail_fails=2 WHERE signal_id=1118")
    stale = db.query_one(ex._TRAIL_ROWS_SQL + " AND eo.signal_id=1118")
    db.execute("UPDATE executor_orders SET trail_fails=3 WHERE signal_id=1118")
    fake = _RejectEx()
    ex._activate_native(fake, stale, [], "стрим Bybit")
    assert fake.created == [] and _trail_row(1118)[0] is None
    # а строка, прочитанная уже после третьего отказа, до биржи не доходит вовсе
    fresh, tickers = db.query_one(ex._TRAIL_ROWS_SQL + " AND eo.signal_id=1118"), fake.ticker_calls
    assert ex._activate_native(fake, fresh, [], "такт") is False
    assert fake.created == [] and fake.ticker_calls == tickers


def test_stream_right_after_each_tick_write_cannot_place_fourth_trail(fresh_db, monkeypatch):
    """Гонка со стримом в худшем чередовании: стрим пробует поставить трейл сразу после КАЖДОЙ
    записи такта в учёт. После снятия мёртвого id он ставит следующий трейл — так и задумано;
    между «id снят» и «отказов три» окна нет, поэтому трейлов ровно три."""
    from app import db
    from app.v2 import bingx_executor as ex
    _seed_native(1121)
    fake, report = _RejectEx(), []
    _arm_env(monkeypatch, ex, fake)
    write, in_tick, in_stream = db.execute, [], []

    def execute(sql, params=()):
        out = write(sql, params)
        if in_tick and not in_stream and "executor_orders" in sql:
            in_stream.append(1)
            try:
                ex.activate_trail(1121)                        # стрим вклинился после записи
            finally:
                in_stream.clear()
        return out
    monkeypatch.setattr(db, "execute", execute)
    ex.activate_trail(1121)
    for dt in (0, 300, 400, 700, 800, 1100, 4000):             # отказ, пауза, снятие — трижды
        in_tick.append(1)
        ex._divergence_alerts(fake, report, now=_T0 + dt)
        in_tick.clear()
    assert sorted(fake.statuses) == ["TR1", "TR2", "TR3"] and _trail_row(1121)[:2] == (None, 3)
    assert report == [_RETRY_TEXT.format(1121, "FAILED"), _GIVEUP_TEXT.format(1121)]
    assert _only_trails(fake)


def test_reject_bookkeeping_leaves_newer_trail_alone(fresh_db):
    """Счёт отказа и снятие мёртвого id идут с условием на этот самый id. Если его место уже
    заняла новая постановка, она остаётся как есть: её id не затирается, чужой отказ ей не
    приписывается. Отказ одного трейла дважды не считается."""
    from app import db
    from app.v2 import bingx_executor as ex
    _seed_native(1119)
    db.execute("UPDATE executor_orders SET trail_order_id='TR1', trail_fails=1, "
               "trail_failed_at=? WHERE signal_id=1119", (_iso(_T0),))
    waiting = db.query_one(ex._TRAIL_ROWS_SQL + " AND eo.signal_id=1119")
    db.execute("UPDATE executor_orders SET trail_order_id='TR2', trail_failed_at=NULL "
               "WHERE signal_id=1119")                         # штатный путь уже поставил новый
    ex._release_rejected_trail(waiting, "TR1", _T0 + 600)
    assert _trail_row(1119) == ("TR2", 1, None)
    fresh = db.query_one(ex._TRAIL_ROWS_SQL + " AND eo.signal_id=1119")
    db.execute("UPDATE executor_orders SET trail_order_id='TR3' WHERE signal_id=1119")
    assert ex._count_trail_reject(fresh, "TR2", "FAILED", _T0 + 700) is None
    assert _trail_row(1119) == ("TR3", 1, None)
    fresh = db.query_one(ex._TRAIL_ROWS_SQL + " AND eo.signal_id=1119")
    assert ex._count_trail_reject(fresh, "TR3", "FAILED", _T0 + 800) is None    # второй отказ
    assert ex._count_trail_reject(fresh, "TR3", "FAILED", _T0 + 900) is None    # тот же снимок
    assert _trail_row(1119) == ("TR3", 2, _iso(_T0 + 800))


def test_reconcile_third_reject_then_fallback_on_next_tick(fresh_db, monkeypatch):
    """Полный такт сверки. Отказов уже два, журнал ведёт трейл: такт ставит нативный, биржа
    отклоняет его в третий раз — итоговая строка в том же такте. Следующий такт нативный
    трейл не ставит, а почасовой фолбэк переставляет стоп за трейлом журнала."""
    from app import db, runtime
    from app.v2 import bingx_executor as ex
    _seed_native(1120, trail_stop=103.6)
    db.execute("UPDATE executor_orders SET trail_fails=2 WHERE signal_id=1120")
    runtime.set_executor_since("2026-10-01T00:00:00+00:00")
    fake = _RejectEx(last=104.5)
    _patch_net(monkeypatch, ex, fake)
    out = ex.reconcile()
    assert fake.created == [("trail", 0.4, "sell", 10.0)] and _only_trails(fake)
    assert _reject_lines(out) == [_GIVEUP_TEXT.format(1120)]
    assert _trail_row(1120)[:2] == (None, 3)
    out = ex.reconcile()
    assert fake.created == [("trail", 0.4, "sell", 10.0), ("stop", 103.6, 10.0)]
    assert fake.cancelled == ["SL0"] and any("стоп подтянут 98 → 103.6" in x for x in out)
    assert _reject_lines(out) == [] and _reject_lines(ex.reconcile()) == []


def test_divergence_silent_when_position_and_trail_alive(fresh_db):
    """Обычный случай: позиция есть, трейл жив — тишина, в settings ничего не пишется;
    статус живого трейла читается не чаще раза в TRAIL_STATUS_EVERY_S."""
    from app import db, runtime
    from app.v2 import bingx_executor as ex
    _seed_native(1109)
    db.execute("UPDATE executor_orders SET trail_order_id='TR9' WHERE signal_id=1109")
    fake, report = _DivEx(pos=10.0, trail_status="NEW"), []
    ex._divergence_alerts(fake, report)
    ex._divergence_alerts(fake, report)
    assert report == [] and (fake.pos_calls, fake.order_calls) == (2, 1)
    assert runtime._raw("executor_flat_alerted") is None
    assert runtime._raw("executor_trail_failed_alerted") is None
    oid, status, at = ex._TRAIL_STATUS[1109]
    ex._TRAIL_STATUS[1109] = (oid, status, at - ex.TRAIL_STATUS_EVERY_S - 1)
    ex._divergence_alerts(fake, report)
    assert report == [] and fake.order_calls == 2


def test_divergence_positions_outage_is_not_flat(fresh_db):
    """Позиции не читаются — это «неизвестно», а не «плоско»: алерта нет, таймер не идёт,
    предупреждение о сбое — не чаще раза в час."""
    from app.v2 import bingx_executor as ex
    _seed_native(1110)
    _judge_1m(1110)
    fake, report = _DivEx(broken=True), []
    ex._divergence_alerts(fake, report)
    ex._divergence_alerts(fake, report)
    assert len(report) == 1 and "позиции не читаются" in report[0]
    assert ex._FLAT_SEEN == {}


def test_reconcile_survives_divergence_check_crash(fresh_db, monkeypatch):
    """Сбой самой проверки не роняет такт: строки такта доходят, сбой — одной строкой."""
    from app import clock, runtime
    from app.v2 import bingx_executor as ex
    _seed_halt_case()
    _fake_exchange(monkeypatch, ex, balance=1000)
    runtime.set_value("executor_since", "2026-10-01T00:00:00+00:00")
    monkeypatch.setattr(clock, "utcnow_iso", lambda: "2026-10-04T12:00:00+00:00")
    monkeypatch.setattr(ex, "_place_entry",
                        lambda e, sid, r, bal, report, **k: report.append(f"place {sid}"))

    def boom(e, report):
        raise RuntimeError("сломалось")
    monkeypatch.setattr(ex, "_divergence_alerts", boom)
    report = ex.reconcile()
    assert "place 912" in report
    assert sum("сверка «журнал/позиция»" in x for x in report) == 1
    assert sum("сверка «журнал/позиция»" in x for x in ex.reconcile()) == 0
