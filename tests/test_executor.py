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
    ex._BALANCE_ALERT["at"] = 0.0
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
