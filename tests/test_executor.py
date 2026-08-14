"""Executor V2 (демо BingX): сайзинг и планировщик — чистые функции без сети."""
from __future__ import annotations

from app.v2.bingx_executor import Action, calc_qty, plan


def test_calc_qty_risk_based():
    # риск 1% от 10k VST = 100; стоп-дистанция 200 -> 0.5 BTC номиналом $3200 —
    # под капом 4500 USDT, риск-формула работает как есть
    assert calc_qty(10_000, 1.0, 6_400, 6_200, 0.001, 0.001) == 0.5
    # шаг объёма режет ВНИЗ (никогда не превышать риск)
    assert calc_qty(10_000, 1.0, 6_400, 6_200, 0.001, 0.3) == 0.3
    # ниже минимального объёма биржи -> 0 (сделка пропускается)
    assert calc_qty(10, 1.0, 6_400, 6_200, 0.001, 0.001) == 0.0
    # вырожденные входы не роняют
    assert calc_qty(10_000, 1.0, 6_400, 6_400, 0.001, 0.001) == 0.0
    assert calc_qty(0, 1.0, 6_400, 6_200, 0.001, 0.001) == 0.0


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
    # без капа было бы 1000/1 = 1000 монет ($100k) — теперь 45 монет ($4500)
    assert qty == 45.0
