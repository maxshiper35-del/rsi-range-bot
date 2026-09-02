"""Executor V2 (v1: демо BingX) — зеркало журнала на бирже.

ПРИНЦИП. Executor не принимает ни одного торгового решения: источник истины — журнал
(signals). Он лишь сводит состояние биржи к состоянию журнала на каждом такте
обслуживания (reconcile):
  сигнал open без филла  -> TRIGGER_MARKET по касанию entry с ПРИКРЕПЛЁННЫМ
                            аварийным стопом (-1R);
  сигнал истёк без филла -> снять триггер;
  журнал закрыл сделку   -> закрыть позицию по рынку (reduceOnly) в ближайший такт.
МОДЕЛЬ ВХОДА — только маркетом по касанию (замер 31.08.2026 на паритетном OOS):
пассивная лимитка ЗАПРЕЩЕНА — through-филл −0.027R OOS / DD ×3.5 (adverse selection:
стакан исполняет худшие сделки и пропускает победителей); тейкер-по-касанию +0.149R.
Журнал считает филл касанием entry — TRIGGER_MARKET воспроизводит ту же семантику,
а реальная цена исполнения (fill_px) честно меряет слип входа в еженедельном отчёте.
Промежуточный трейлинг НЕ дублируется на бирже: текущий подтянутый стоп нигде не
персистится (событие 'trail' в signal_events — это уже ВЫХОД), а повторная реализация
той же математики — прямой риск расхождения с трек-рекордом. Аварийный стоп на бирже
покрывает случай «бот умер» тем же худшим исходом -1R, что и журнал; выход по трейлингу
исполняется рыночным закрытием после решения журнала (задержка <= такта обслуживания).
Ошибка слежения executor-против-журнала — измеряемая величина, не скрытая.

БЕЗОПАСНОСТЬ. Sandbox (VST) ЗАХАРДКОЖЕН — v1 физически не умеет торговать реальные
деньги. Права ключа: только чтение + фьючерсная торговля, вывода нет, IP-whitelist
сервера. Флаг executor_enabled (дефолт выкл) читается на каждом такте; исполняются
только сигналы, созданные ПОСЛЕ executor_since (включение не догоняет старые филлы).
Ключи в .env сервера (root/bot, 600); per-user ключей и Fernet в v1 нет — торгуется
ТОЛЬКО счёт владельца.

Hedge-режим BingX: у каждого ордера обязателен positionSide (LONG/SHORT); позиции
одного символа разных сторон независимы. Объёмы наших одновременных сигналов одного
символа/стороны (разные ТФ) на бирже сливаются — точное закрытие «своей» части ведётся
по qty из executor_orders (миграция 019).
"""
from __future__ import annotations

import os
import re as _re
from dataclasses import dataclass

from app import clock, db, runtime

# клиентские id: rsiE<sid> — вход; аварийный стоп прикреплён к входу (BingX создаёт
# его при филле сам, отдельного client_id у него нет)
_ENTRY_PREFIX = "rsiE"


# ---------------------------------------------------------------------------
# Чистые функции (тестируются без сети)
# ---------------------------------------------------------------------------
MAX_NOTIONAL_USDT = 1500.0   # BingX: лимит «5000 USDT на позицию символа» (101209) —
#                              общий бюджет символа/стороны. 1500 = влезают ТРИ
#                              одновременных сигнала одного символа (мульти-ТФ дубли —
#                              норма журнала): для демо-зеркала ПОКРЫТИЕ замера паритета
#                              важнее размера позиции (20.08: кап 4500 пропускал дубли).


def calc_qty(balance: float, risk_pct: float, entry: float, stop: float,
             min_qty: float, qty_step: float,
             max_notional: float = MAX_NOTIONAL_USDT) -> float:
    """Объём от риска: risk_pct% баланса / дистанцию стопа, с капом биржевого
    номинала, вниз до шага объёма. 0.0 = не проходит по минимальному объёму.
    max_notional — ОСТАТОК бюджета символа/стороны: лимит 5000 USDT у BingX общий
    на позицию символа, второй сигнал того же символа (другой ТФ) ловил 101209,
    пока кап был на-ордер (17.08: #325/#328/#329/#334/#338/#340)."""
    risk_amount = balance * risk_pct / 100.0
    dist = abs(entry - stop)
    if dist <= 0 or risk_amount <= 0 or entry <= 0 or max_notional <= 0:
        return 0.0
    qty = min(risk_amount / dist, max_notional / entry)
    if qty_step > 0:
        qty = int(qty / qty_step) * qty_step
    return 0.0 if qty < max(min_qty, 0) or qty <= 0 else round(qty, 10)


def _active_notional(symbol: str, side: str) -> float:
    """Суммарный номинал УЖЕ ведущихся ордеров/позиций символа/стороны (учёт наш,
    не биржи: стоящие лимитки биржевой fetch_positions не видит, а лимит BingX
    считает и их)."""
    r = db.query_one(
        "SELECT COALESCE(SUM(qty * entry_px), 0) AS s FROM executor_orders "
        "WHERE symbol=? AND side=? AND state IN ('placing','placed','filled')",
        (symbol, side))
    return float(r["s"]) if r and r["s"] else 0.0


@dataclass
class Action:
    kind: str          # 'place' | 'cancel' | 'close' | 'skip'
    signal_id: int
    detail: str


def plan(open_rows: list, tracked: dict, markets: set, since: str) -> list[Action]:
    """Свести журнал и учёт executor_orders к списку действий (без сети, тестируемо).

    open_rows — journal-строки со status='open'; tracked — {signal_id: state} из
    executor_orders; markets — множество символов, существующих на BingX.
    Закрытия планируются по РАЗНИЦЕ: tracked в состоянии placed/filled, чьих id больше
    нет среди open — журнал разрешил сделку (терминальный статус смотрит исполнитель)."""
    actions: list[Action] = []
    open_ids = set()
    for r in open_rows:
        sid = r["id"]
        open_ids.add(sid)
        if sid in tracked:
            continue                                    # уже ведётся
        if (r["created_at"] or "") < since:
            continue                                    # старше включения — не догоняем
        if r["symbol"] not in markets:
            actions.append(Action("skip", sid, f"{r['symbol']} нет на BingX"))
            continue
        if r["filled_at"] is not None:
            # журнал зафиксировал филл (реалтайм-стрим) раньше, чем executor успел
            # поставить вход — цена уже у уровня, честнее пропустить, чем догонять
            actions.append(Action("skip", sid, "филл в журнале раньше постановки"))
            continue
        actions.append(Action("place", sid, r["symbol"]))
    for sid, state in tracked.items():
        if sid in open_ids or state in ("closed", "cancelled", "error"):
            continue
        # placing/placed -> снять вход (ветка cancel сама закроет исполнившуюся
        # между тактами часть по executedQty); filled -> закрыть позицию
        actions.append(Action("cancel" if state in ("placed", "placing") else "close",
                              sid, ""))
    return actions


# ---------------------------------------------------------------------------
# Обвязка биржи
# ---------------------------------------------------------------------------
_ex_cache: dict = {"ex": None, "born": 0.0}
_EX_TTL_SEC = 6 * 3600            # пересоздать инстанс (и market-кеш ccxt) раз в 6ч


def _exchange():
    """Кешированный инстанс ccxt: без кеша каждый 60с-такт создавал бы клиента и качал
    полный список контрактов BingX (ревью 13.08) — лишний тяжёлый запрос и точка отказа."""
    import time as _time

    import ccxt

    now = _time.monotonic()
    if _ex_cache["ex"] is not None and now - _ex_cache["born"] < _EX_TTL_SEC:
        return _ex_cache["ex"]
    ex = ccxt.bingx({
        "apiKey": os.environ.get("BINGX_API_KEY", ""),
        "secret": os.environ.get("BINGX_API_SECRET", ""),
        "timeout": 20000,
        "options": {"defaultType": "swap"},
    })
    ex.set_sandbox_mode(True)      # v1: ТОЛЬКО демо (VST) — см. докстринг модуля
    ex.load_markets()
    _ex_cache["ex"], _ex_cache["born"] = ex, now
    return ex


def _tracked() -> dict:
    return {r["signal_id"]: r["state"] for r in db.query(
        "SELECT signal_id, state FROM executor_orders")}


def _track(sid: int, r, qty: float, client_id: str) -> None:
    db.execute(
        "INSERT OR REPLACE INTO executor_orders(signal_id, symbol, side, qty, entry_px, "
        "stop_px, client_id, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, r["symbol"], r["side"], qty, r["entry"], r["stop"], client_id,
         "placing", clock.utcnow_iso(), clock.utcnow_iso()))


def _set_state(sid: int, state: str, note: str | None = None) -> None:
    db.execute("UPDATE executor_orders SET state=?, note=?, updated_at=? WHERE signal_id=?",
               (state, note, clock.utcnow_iso(), sid))


def reconcile() -> list[str]:
    """Один такт сверки журнал -> биржа. -> человекочитаемые строки для алертов админу.

    Вызывается из maintenance-цикла бота через to_thread при executor_enabled.
    Любая ошибка по одному сигналу помечает его state='error' и НЕ роняет такт."""
    since = runtime.executor_since() or ""
    if not since:
        return []
    if not os.environ.get("BINGX_API_KEY"):
        # локальный режим без ключей: тумблер в панели мог включить executor, а ключи
        # живут в серверном .env (и их вайтлист — на IP сервера). Самовыключение с одним
        # сообщением вместо AuthenticationError каждые 60с (алерт 31.08).
        runtime.set_value("executor_enabled", "0")
        return ["⚠️ Executor выключен сам: в окружении нет ключей BingX "
                "(локальный режим; включать после возврата на сервер)"]
    report: list[str] = []
    ex = _exchange()
    open_rows = db.query("SELECT * FROM signals WHERE status='open' AND result_r IS NULL")
    _retry_budget_skips({r["id"] for r in open_rows})
    tracked = _tracked()
    todo = plan(open_rows, tracked, set(ex.markets), since)
    if not todo:
        return []
    rows_by_id = {r["id"]: r for r in open_rows}
    balance = float(ex.fetch_balance().get("total", {}).get("VST", 0.0))

    for a in todo:
        try:
            if a.kind == "skip":
                if a.signal_id not in tracked:
                    r = rows_by_id[a.signal_id]
                    _track(a.signal_id, r, 0.0, "")
                    _set_state(a.signal_id, "error", a.detail)
                    report.append(f"⏭ #{a.signal_id}: {a.detail}")
            elif a.kind == "place":
                r = rows_by_id[a.signal_id]
                m = ex.market(r["symbol"])
                lim = m.get("limits", {}) or {}
                budget = MAX_NOTIONAL_USDT - _active_notional(r["symbol"], r["side"])
                qty = calc_qty(balance, runtime.executor_risk_pct(), r["entry"], r["stop"],
                               float((lim.get("amount") or {}).get("min") or 0),
                               float(m.get("precision", {}).get("amount") or 0),
                               max_notional=budget)
                if qty <= 0:
                    why = ("бюджет номинала символа/стороны исчерпан "
                           f"(кап {MAX_NOTIONAL_USDT:g} USDT, занято "
                           f"{MAX_NOTIONAL_USDT - budget:g})"
                           if budget < MAX_NOTIONAL_USDT else "объём ниже минимума биржи")
                    _track(a.signal_id, r, 0.0, "")
                    _set_state(a.signal_id, "error", why)
                    report.append(f"⏭ #{a.signal_id}: {why}")
                    continue
                side = "buy" if r["side"] == "LONG" else "sell"
                client_id = f"{_ENTRY_PREFIX}{a.signal_id}"
                # учёт ДО create_order (state='placing'): таймаут ПОСЛЕ отправки не
                # оставит ордер-сироту — recovery в except сверит по clientOrderID
                _track(a.signal_id, r, qty, client_id)
                # цены — к тику инструмента: сырой стоп ловил 101400
                # «SL Price must be…» на монетах с крупным тиком (#343 PORTAL 20.08)
                entry_px = float(ex.price_to_precision(r["symbol"], r["entry"]))
                sl = {"type": "STOP_MARKET",
                      "stopPrice": float(ex.price_to_precision(r["symbol"], r["stop"]))}
                base = {"positionSide": r["side"], "clientOrderID": client_id,
                        "stopLoss": sl}
                # Направление триггера BingX ставит сам от текущей цены — это СОВПАДАЕТ
                # с журнальной семантикой «филл касанием entry с любой стороны»:
                # trigger сработает ровно тогда, когда журнал засчитает касание.
                from ccxt import ExchangeError as _ExErr
                try:
                    try:
                        created = ex.create_order(
                            r["symbol"], "market", side, qty, None,
                            params={**base, "triggerPrice": entry_px})
                    except _ExErr as e_sl:
                        if "101400" in str(e_sl) and "SL" in str(e_sl).upper():
                            # BingX валидирует прикреплённый стоп от ТЕКУЩЕЙ цены:
                            # если цена сейчас за стопом (#434 ENA 02.09), связка
                            # неставима. Ставим триггер БЕЗ стопа, аварийный стоп
                            # довесим при филле (_sync_fill_states по метке note)
                            created = ex.create_order(
                                r["symbol"], "market", side, qty, None,
                                params={"positionSide": r["side"],
                                        "clientOrderID": client_id,
                                        "triggerPrice": entry_px})
                            db.execute("UPDATE executor_orders SET note=? "
                                       "WHERE signal_id=?",
                                       ("стоп отложен до филла", a.signal_id))
                        else:
                            # триггер отвергнут (цена у уровня?). Немедленный маркет
                            # честен ТОЛЬКО если журнал УЖЕ засчитал касание (гонка
                            # внутри такта) — истина у журнала, не у тикера
                            fresh = db.query_one(
                                "SELECT filled_at FROM signals WHERE id=?",
                                (a.signal_id,))
                            if not (fresh and fresh["filled_at"]):
                                raise
                            created = ex.create_order(
                                r["symbol"], "market", side, qty, None,
                                params=dict(base))
                    _store_order_id(a.signal_id, (created or {}).get("id"))
                except Exception:                       # noqa: BLE001
                    # неоднозначный сбой (таймаут после отправки?): сверить по clientOrderID —
                    # ордер реально стоит -> успех; нет -> настоящая ошибка наверх
                    trow = db.query_one("SELECT * FROM executor_orders WHERE signal_id=?",
                                        (a.signal_id,))
                    if _entry_order(ex, trow) is None:
                        raise
                _set_state(a.signal_id, "placed")
                report.append(f"📥 #{a.signal_id} {r['symbol']} {r['side']}: "
                              f"триггер-маркет {qty:g} @ {r['entry']:g}, "
                              f"стоп {r['stop']:g} (VST)")
            elif a.kind == "cancel":
                t = db.query_one("SELECT * FROM executor_orders WHERE signal_id=?",
                                 (a.signal_id,))
                assert t is not None                    # tracked-строка есть по построению plan
                _cancel_entry(ex, t)
                # гонка «вход исполнился между тактами» (ревью 13.08): после снятия
                # проверить фактический филл — исполненную часть закрыть, не осиротить
                o = _entry_order(ex, t)
                if o is None and _row_order_id(t):
                    # известный order_id не читается (сеть?) — финализировать нельзя:
                    # «cancelled» при реальном филле осиротил бы позицию (аудит 02.09)
                    raise RuntimeError("вход не читается с биржи — повтор след. тактом")
                filled_qty = _effective_filled(o, t)
                _cancel_deferred_stop(ex, t)            # довешенный стоп не осиротить
                if filled_qty > 0:
                    # цена входа для отчёта: из ордера, у сработавшего триггера — из трейдов
                    if _order_filled(o) > 0:
                        _record_fill(a.signal_id, o)
                    else:
                        _record_entry_from_trades(ex, t)
                    closed = _close_position(ex, t, max_qty=filled_qty)
                    _set_state(a.signal_id, "closed",
                               f"филл {filled_qty:g} закрыт при отмене")
                    report.append(f"📤 #{a.signal_id} {t['symbol']}: вход снят, "
                                  f"исполненная часть {closed:g} закрыта по рынку")
                else:
                    _set_state(a.signal_id, "cancelled")
                    report.append(f"🚫 #{a.signal_id}: вход снят (журнал закрыл/истёк)")
            elif a.kind == "close":
                t = db.query_one("SELECT * FROM executor_orders WHERE signal_id=?",
                                 (a.signal_id,))
                assert t is not None                    # tracked-строка есть по построению plan
                _cancel_entry(ex, t)
                _cancel_deferred_stop(ex, t)            # довешенный стоп не осиротить
                if not t["fill_px"]:
                    # филл мог случиться и разрешиться стопом между тактами — дочитать
                    # цену входа, иначе пара выпадает из отчёта «исполнение vs журнал»
                    o2 = _entry_order(ex, t)
                    if _order_filled(o2) > 0:
                        _record_fill(a.signal_id, o2)
                    elif (o2 or {}).get("status") == "closed":
                        _record_entry_from_trades(ex, t)
                closed = _close_position(ex, t, max_qty=float(t["qty"]))
                if closed > 0:
                    _set_state(a.signal_id, "closed")
                    report.append(f"📤 #{a.signal_id} {t['symbol']} {t['side']}: "
                                  f"закрыто по рынку {closed:g} (VST)")
                else:
                    _record_stop_close(ex, t)           # цена аварийного стопа — best-effort
                    _set_state(a.signal_id, "closed", "позиция уже плоская (аварийный стоп)")
                    report.append(f"📤 #{a.signal_id} {t['symbol']}: уже закрыто "
                                  "аварийным стопом на бирже")
        except Exception as e:                          # noqa: BLE001 — один сигнал не роняет такт
            if a.kind in ("place", "skip"):
                _set_state(a.signal_id, "error", str(e)[:200])
                report.append(f"⚠️ #{a.signal_id}: {type(e).__name__}: {str(e)[:120]}")
            else:
                # cancel/close: терминальный error навсегда выбрасывал живой ордер/
                # позицию из сверки (аудит 02.09) — состояние НЕ трогаем, plan()
                # повторит следующим тактом; после 10 попыток сдаёмся с алертом
                n = _bump_retry(a.signal_id, e)
                if n >= 10:
                    _set_state(a.signal_id, "error",
                               f"после {n} попыток: {str(e)[:160]}")
                    report.append(f"⚠️ #{a.signal_id}: сдаюсь после {n} попыток — "
                                  f"{type(e).__name__}: {str(e)[:100]}")
                elif n == 1:
                    report.append(f"⚠️ #{a.signal_id}: {type(e).__name__}: "
                                  f"{str(e)[:100]} — повторю")

    # переход placed -> filled (информативно): вход исчез из открытых, позиция есть
    _sync_fill_states(ex, report)
    return report


_RETRY_RE = _re.compile(r"^retry#(\d+)")
_BUDGET_RETRY_MIN = 30


def _retry_budget_skips(open_ids: set) -> None:
    """Скип по бюджету символа/стороны НЕ терминален: бюджет освобождается закрытиями
    (аудит 02.09 — терминальный error терял покрытие замера у мульти-ТФ дублей).
    Пока сигнал ещё open и есть заметный остаток бюджета — вернуть его в очередь
    (удалить учётную строку: plan() поставит вход заново). Не чаще раза в 30 минут,
    чтобы не жужжать алертами, если остатка всё равно мало."""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=_BUDGET_RETRY_MIN)).isoformat()
    for r in db.query("SELECT signal_id, symbol, side, updated_at FROM executor_orders "
                      "WHERE state='error' AND note LIKE 'бюджет%'"):
        if r["signal_id"] not in open_ids or (r["updated_at"] or "") > cutoff:
            continue
        if MAX_NOTIONAL_USDT - _active_notional(r["symbol"], r["side"]) \
                > MAX_NOTIONAL_USDT * 0.1:
            db.execute("DELETE FROM executor_orders WHERE signal_id=?",
                       (r["signal_id"],))


def _bump_retry(sid: int, e: Exception) -> int:
    """Счётчик повторов cancel/close в note ('retry#N: …'). -> номер текущей попытки."""
    t = db.query_one("SELECT note FROM executor_orders WHERE signal_id=?", (sid,))
    m = _RETRY_RE.match((t["note"] or "") if t else "")
    n = (int(m.group(1)) if m else 0) + 1
    db.execute("UPDATE executor_orders SET note=?, updated_at=? WHERE signal_id=?",
               (f"retry#{n}: {str(e)[:170]}", clock.utcnow_iso(), sid))
    return n


def _store_order_id(sid: int, oid) -> None:
    if oid:
        db.execute("UPDATE executor_orders SET order_id=?, updated_at=? WHERE signal_id=?",
                   (str(oid), clock.utcnow_iso(), sid))


def _find_by_client_id(ex, t):
    """Ордер по clientOrderId через СПИСОК открытых: точечный fetch по clientOrderID
    BingX-демо отвергает 109400 (смоук 31.08) — а в листинге поле отдаётся. Видит
    только ОТКРЫТЫЕ ордера: исполненный/снятый так не восстановить (редкий случай
    потерянного ответа create — честно вернём None)."""
    try:
        for o in ex.fetch_open_orders(t["symbol"]):
            # BingX-демо в листинге приводит clientOrderId к нижнему регистру
            # ('rsismoke3' вместо 'rsiSMOKE3') — сравниваем без регистра
            if (o.get("clientOrderId") or "").lower() == t["client_id"].lower():
                return o
    except Exception:                                   # noqa: BLE001
        pass
    return None


def _row_order_id(t):
    try:
        return t["order_id"]
    except (KeyError, IndexError):
        return None


def _cancel_entry(ex, t) -> None:
    """Снять ордер входа (по order_id, резерв — поиск по clientOrderId в листинге);
    уже исполнена/отменена — не ошибка: цель «ордера нет» достигнута."""
    oid = _row_order_id(t)
    if not oid:
        found = _find_by_client_id(ex, t)
        if found is None:
            return                                       # открытого ордера нет — цель достигнута
        oid = found.get("id")
    try:
        ex.cancel_order(oid, t["symbol"])
    except Exception as e:                              # noqa: BLE001
        if "not exist" not in str(e).lower() and "80016" not in str(e):
            raise


def _entry_order(ex, t):
    """Ордер входа по order_id (резерв — листинг по clientOrderId) либо None."""
    oid = _row_order_id(t)
    if oid:
        try:
            return ex.fetch_order(oid, t["symbol"])
        except Exception:                               # noqa: BLE001
            return None
    o = _find_by_client_id(ex, t)
    if o is not None and o.get("id"):
        _store_order_id(t["signal_id"], o["id"])
    return o


def _place_deferred_stop(ex, t, report: list[str]) -> None:
    """Довесить аварийный стоп ПОСЛЕ филла (постановка со связкой была отвергнута:
    BingX валидирует SL от текущей цены — #434 ENA 02.09). reduce-only STOP_MARKET;
    id пишется в note ('sl:<id>') — ветки cancel/close снимут его, чтобы осиротевший
    стоп не съел позицию СЛЕДУЮЩЕГО сигнала того же символа/стороны."""
    close_side = "sell" if t["side"] == "LONG" else "buy"
    try:
        o = ex.create_order(
            t["symbol"], "market", close_side, float(t["qty"]), None,
            params={"stopLossPrice": float(ex.price_to_precision(
                t["symbol"], t["stop_px"])), "hedged": True})
        _set_state(t["signal_id"], "filled", f"sl:{(o or {}).get('id') or ''}")
        report.append(f"🛑 #{t['signal_id']} {t['symbol']}: аварийный стоп довешен")
    except Exception as e:                              # noqa: BLE001 — виден админу, журнал закроет сам
        _set_state(t["signal_id"], "filled", f"стоп НЕ встал: {str(e)[:120]}")
        report.append(f"⚠️ #{t['signal_id']} {t['symbol']}: аварийный стоп не встал "
                      f"({str(e)[:80]}) — закрытие останется за журналом")


def _cancel_deferred_stop(ex, t) -> None:
    """Снять довешенный аварийный стоп (note 'sl:<id>'), если он был. Уже исполнен/
    снят — не ошибка."""
    note = t["note"] or ""
    if not note.startswith("sl:"):
        return
    oid = note[3:].strip()
    if not oid:
        return
    try:
        ex.cancel_order(oid, t["symbol"])
    except Exception as e:                              # noqa: BLE001
        if "not exist" not in str(e).lower() and "80016" not in str(e):
            raise


def _order_filled(o) -> float:
    """Исполненный объём ордера входа (0.0 = филла не было / ордер не найден)."""
    try:
        return float(o.get("filled") or 0.0) if o else 0.0
    except (TypeError, ValueError):
        return 0.0


def _effective_filled(o, t) -> float:
    """Объём входа с поправкой на семантику триггера BingX (смоук 02.09):
    сработавший TRIGGER_MARKET закрывается со status=closed, но filled=0/average=None —
    исполнение живёт в ОТДЕЛЬНОМ рыночном ордере. closed без филла = АКТИВАЦИЯ,
    объём берём из учёта; иначе (canceled/open) филла не было."""
    if not o:
        return 0.0
    f = _order_filled(o)
    if f > 0:
        return f
    return float(t["qty"]) if o.get("status") == "closed" else 0.0


def _record_entry_from_trades(ex, t) -> None:
    """Цена входа сработавшего триггера (best-effort): сам триггер-ордер не несёт
    филла — берём последний трейд ВХОДНОЙ стороны по символу. Без этого слип входа
    триггерных сделок выпадал бы из отчёта «исполнение vs журнал»."""
    try:
        entry_side = "buy" if t["side"] == "LONG" else "sell"
        for tr in reversed(ex.fetch_my_trades(t["symbol"], limit=20) or []):
            info = tr.get("info") or {}
            pos_side = (info.get("positionSide") or "").upper()
            if tr.get("side") == entry_side and pos_side in ("", t["side"]):
                px = tr.get("price")
                db.execute(
                    "UPDATE executor_orders SET fill_px=COALESCE(fill_px, ?), "
                    "fill_qty=COALESCE(fill_qty, ?), fill_fee=COALESCE(fill_fee, ?) "
                    "WHERE signal_id=?",
                    (float(px) if px else None, float(t["qty"]), _fee_cost(tr),
                     t["signal_id"]))
                return
    except Exception:                                   # noqa: BLE001 — отчётные поля не критичны
        pass


def _record_fill(sid: int, o) -> None:
    """Фактическая цена/объём/комиссия входа из ордера биржи (best-effort, идемпотентно:
    не перетирает уже записанный филл). Без этого пары «вход снят с филлом» и
    «стоп между тактами» выпадали из отчёта исполнения — как раз худшие исходы."""
    if not o:
        return
    filled = _order_filled(o)
    if filled <= 0:
        return
    px = o.get("average") or o.get("price")
    db.execute(
        "UPDATE executor_orders SET fill_px=COALESCE(fill_px, ?), "
        "fill_qty=COALESCE(fill_qty, ?), fill_fee=COALESCE(fill_fee, ?) "
        "WHERE signal_id=?",
        (float(px) if px else None, filled, _fee_cost(o), sid))


def _record_stop_close(ex, t) -> None:
    """Цена закрытия аварийным стопом (best-effort): последний трейд символа
    закрывающей стороны. Без неё отчёт «исполнение vs журнал» исключал именно
    стоп-ауты — систематическое смещение замера в лучшую сторону."""
    try:
        close_side = "sell" if t["side"] == "LONG" else "buy"
        for tr in reversed(ex.fetch_my_trades(t["symbol"], limit=20) or []):
            info = tr.get("info") or {}
            pos_side = (info.get("positionSide") or "").upper()
            if tr.get("side") == close_side and pos_side in ("", t["side"]):
                px = tr.get("price")
                db.execute(
                    "UPDATE executor_orders SET close_px=COALESCE(close_px, ?), "
                    "close_fee=COALESCE(close_fee, ?), "
                    "closed_at=COALESCE(closed_at, ?) WHERE signal_id=?",
                    (float(px) if px else None, _fee_cost(tr), clock.utcnow_iso(),
                     t["signal_id"]))
                return
    except Exception:                                   # noqa: BLE001 — отчётные поля не критичны
        pass


def _position_qty(ex, symbol: str, side: str) -> float:
    """Текущий объём позиции символа нужной стороны (hedge-режим) на бирже.

    Сетевой сбой ПОДНИМАЕТСЯ наверх (аудит 02.09): раньше глотался и возвращал 0.0 —
    «позиция плоская» — и ветка close помечала сделку закрытой при ЖИВОЙ позиции.
    Теперь сбой уходит в retry-ветку reconcile и повторяется следующим тактом."""
    for p in ex.fetch_positions([symbol]):
        if (p.get("side") or "").upper() == side:
            return float(p.get("contracts") or 0.0)
    return 0.0


def _close_position(ex, t, max_qty: float) -> float:
    """Закрыть СВОЮ часть позиции по рынку. -> фактически закрытый объём.

    Ревью 13.08: (1) объём клампится по реальной позиции на бирже — аварийный стоп мог
    уже закрыть нашу часть, слепой ордер съел бы долю СОСЕДНЕГО сигнала того же
    символа/стороны; ноль -> закрывать нечего, это штатно. (2) params={'hedged': True,
    'reduceOnly': True} — путь самого ccxt для hedge-режима (bingx.py: при hedged ccxt
    сам выводит positionSide из side и УБИРАЕТ reduceOnly из запроса — сырой reduceOnly
    BingX в hedge отклоняет кодом 109414). Ошибки НЕ глотаются: после клампа «нет
    позиции» невозможен штатно, остальное должно быть видно как error.
    Побочно (best-effort) пишет фактическую цену/комиссию закрытия — сырьё
    еженедельного отчёта «исполнение vs журнал»."""
    closable = min(max_qty, _position_qty(ex, t["symbol"], t["side"]))
    if closable <= 0:
        return 0.0
    side = "sell" if t["side"] == "LONG" else "buy"
    o = ex.create_order(t["symbol"], "market", side, closable, None,
                        params={"hedged": True, "reduceOnly": True})
    _record_close(ex, t, o)
    row = db.query_one("SELECT close_px FROM executor_orders WHERE signal_id=?",
                       (t["signal_id"],))
    if row is not None and row["close_px"] is None:
        # ответ create без average и fetch не дочитался — добрать цену из трейдов,
        # иначе пара выпадает из отчёта исполнения навсегда (одноразовый захват)
        _record_stop_close(ex, t)
    return closable


def _fee_cost(o) -> float | None:
    try:
        fee = (o or {}).get("fee") or {}
        return float(fee["cost"]) if fee.get("cost") is not None else None
    except (TypeError, ValueError):
        return None


def _record_close(ex, t, created) -> None:
    """Фактическая цена/комиссия рыночного закрытия (best-effort: ответ create может
    не нести average — дочитываем fetch_order по id; сбой записи не роняет закрытие)."""
    try:
        o = created or {}
        if not o.get("average") and o.get("id"):
            try:
                o = ex.fetch_order(o["id"], t["symbol"]) or o
            except Exception:                           # noqa: BLE001
                pass
        px = o.get("average") or o.get("price")
        db.execute("UPDATE executor_orders SET close_px=?, close_fee=?, closed_at=? "
                   "WHERE signal_id=?",
                   (float(px) if px else None, _fee_cost(o), clock.utcnow_iso(),
                    t["signal_id"]))
    except Exception:                                   # noqa: BLE001 — отчётные поля не критичны
        pass


_PLACING_STALE_MIN = 10


def _sync_fill_states(ex, report: list[str]) -> None:
    """Позитивное определение филла: статус ордера через fetch_order, а не вывод из
    «пропал из открытых» (снятый руками в приложении вход — не филл; ревью 13.08)."""
    from datetime import datetime, timedelta, timezone

    placing_cutoff = (datetime.now(timezone.utc)
                      - timedelta(minutes=_PLACING_STALE_MIN)).isoformat()
    placed = db.query("SELECT * FROM executor_orders WHERE state IN ('placed','placing')")
    for t in placed:
        o = _entry_order(ex, t)
        if o is None:
            # placing без ордера: крэш между _track и create_order оставлял строку
            # навсегда (аудит 02.09) — по прошествии таймаута честный error
            if t["state"] == "placing" and (t["created_at"] or "") < placing_cutoff:
                _set_state(t["signal_id"], "error",
                           "застрял в placing — ордер на бирже не найден")
                report.append(f"⚠️ #{t['signal_id']} {t['symbol']}: постановка не "
                              "подтвердилась (крэш при create?) — снят с учёта")
            continue                                    # сеть/не найден — решим в след. такт
        if t["state"] == "placing":
            # ордер жив, а учёт отстал (крэш между create_order и set_state) — догнать
            _set_state(t["signal_id"], "placed")
            if o.get("id"):
                _store_order_id(t["signal_id"], o["id"])
        status, filled = o.get("status"), _order_filled(o)
        if status == "closed":
            # closed с филлом — обычный ордер; closed БЕЗ филла — сработавший триггер
            # BingX (исполнение в отдельном рыночном ордере, смоук 02.09)
            _set_state(t["signal_id"], "filled")
            if filled > 0:
                _record_fill(t["signal_id"], o)
            else:
                _record_entry_from_trades(ex, t)
            report.append(f"✅ #{t['signal_id']} {t['symbol']}: вход исполнен на BingX")
            if (t["note"] or "").startswith("стоп отложен"):
                _place_deferred_stop(ex, t, report)
        elif status == "canceled":
            if filled > 0:
                # частичный филл снятого извне ордера: не осиротить исполненную часть
                _record_fill(t["signal_id"], o)
                closed = _close_position(ex, t, max_qty=filled)
                _set_state(t["signal_id"], "closed",
                           f"снят вне executor, филл {filled:g} закрыт")
                report.append(f"📤 #{t['signal_id']} {t['symbol']}: вход снят вне "
                              f"executor, исполненная часть {closed:g} закрыта")
            else:
                _set_state(t["signal_id"], "cancelled", "снят вне executor")
                report.append(f"🚫 #{t['signal_id']} {t['symbol']}: вход снят вне executor")


def execution_report(start_iso: str, end_iso: str) -> str | None:
    """Еженедельная сводка «исполнение vs журнал» (только БД, без сети) | None = пусто.

    Живая проверка допущений бэктеста (maker-вход, слип 0.02%, комиссии) на демо:
    к гейту эпохи-3 (28.08) даёт второй ответ — «исполнение сходится?». Сравнения:
    вход fill_px vs entry_px журнала, выход close_px vs exit_price журнала (в R от
    дистанции стопа), демо-R vs журнальный result_r по закрытым зеркальным сделкам."""
    rows = list(db.query(
        "SELECT eo.*, s.result_r, s.exit_price, s.status AS j_status "
        "FROM executor_orders eo JOIN signals s ON s.id = eo.signal_id "
        "WHERE eo.updated_at >= ? AND eo.updated_at < ?", (start_iso, end_iso)))
    if not rows:
        return None
    placed = sum(1 for r in rows if r["state"] in ("placed", "filled", "closed"))
    filled = [r for r in rows if r["fill_px"]]
    closed = [r for r in rows if r["state"] == "closed"]
    errors = [r for r in rows if r["state"] == "error"]
    cancelled = sum(1 for r in rows if r["state"] == "cancelled")

    lines = ["🤖 <b>Executor: исполнение vs журнал (неделя)</b>",
             f"Ордеров: {placed} · входов: {len(filled)} · закрыто: {len(closed)} · "
             f"снято без филла: {cancelled} · ошибок: {len(errors)}"]
    j_sum = x_sum = 0.0
    n_pairs = 0
    slips_out = []
    for r in closed:
        risk = abs(r["entry_px"] - r["stop_px"])
        sign = 1.0 if r["side"] == "LONG" else -1.0
        if r["fill_px"] and r["close_px"] and risk:
            x_r = sign * (r["close_px"] - r["fill_px"]) / risk
            j_r = r["result_r"]
            if j_r is not None:
                j_sum += j_r
                x_sum += x_r
                n_pairs += 1
            if r["exit_price"]:
                # + = вышли ЛУЧШЕ журнала, − = хуже (в R от дистанции стопа)
                slips_out.append(sign * (r["close_px"] - r["exit_price"]) / risk)
    if n_pairs:
        lines.append(f"Закрытые зеркальные ({n_pairs}): журнал {j_sum:+.2f}R · "
                     f"демо {x_sum:+.2f}R · разрыв {x_sum - j_sum:+.2f}R")
    if slips_out:
        avg = sum(slips_out) / len(slips_out)
        worst = min(slips_out)
        lines.append(f"Слип выхода: в среднем {avg:+.3f}R, худший {worst:+.3f}R "
                     f"(n={len(slips_out)})")
    # слип ВХОДА (+ = вошли лучше журнального entry): с моделью «маркет по касанию»
    # это главный измеряемый параметр — бэктест закладывает 0.02% (замер 31.08)
    slips_in = []
    for r in filled:
        risk = abs(r["entry_px"] - r["stop_px"])
        if risk:
            sign = 1.0 if r["side"] == "LONG" else -1.0
            slips_in.append(sign * (r["entry_px"] - r["fill_px"]) / risk)
    if slips_in:
        avg_in = sum(slips_in) / len(slips_in)
        lines.append(f"Слип входа: в среднем {avg_in:+.3f}R, худший {min(slips_in):+.3f}R "
                     f"(n={len(slips_in)}; + = лучше журнала)")
    fees = [f for r in rows for f in (r["fill_fee"], r["close_fee"]) if f]
    if fees:
        lines.append(f"Комиссии (видимые): {sum(fees):.2f} VST")
    for r in errors[:3]:
        lines.append(f"⚠️ #{r['signal_id']}: {(r['note'] or '')[:80]}")
    lines.append("<i>Демо BingX (VST) — проверка исполнения перед гейтом 28.08.</i>")
    return "\n".join(lines)


def status() -> dict:
    """Сводка для /executor: учёт + баланс (сеть только при включённом флаге)."""
    counts: dict = {}
    for r in db.query("SELECT state, COUNT(*) AS n FROM executor_orders GROUP BY state"):
        counts[r["state"]] = r["n"]
    out = {"enabled": runtime.executor_enabled(), "since": runtime.executor_since(),
           "risk_pct": runtime.executor_risk_pct(), "orders": counts, "vst": None}
    if out["enabled"]:
        try:
            ex = _exchange()
            out["vst"] = round(float(ex.fetch_balance().get("total", {}).get("VST", 0)), 2)
        except Exception as e:                          # noqa: BLE001
            out["vst"] = f"ошибка: {str(e)[:80]}"
    return out
