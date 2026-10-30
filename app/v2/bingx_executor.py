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
Трейлинг ДУБЛИРУЕТСЯ на бирже (20.10.2026): журнал персистит подтянутый стоп открытой
сделки (signals.trail_stop, та же математика судьи — без второй реализации), executor
переставляет стоп-ордер BingX на этот уровень (_sync_trail_stops). Раньше трейл жил только
в журнале, выход исполнялся рыночным закрытием после бара — замер 20.10: на 24 выходах
по трейлу executor отставал на −0.48R (при развороте позицию забирал исходный стоп).
С 24.10 — нативный TRAILING_STOP_MARKET (биржа ведёт трейл в реальном времени); с 30.10
(версия e5) его АКТИВАЦИЮ решает реалтайм-стрим Bybit — та же цена, что у журнала, — а не
опрос цены BingX раз в такт (аудит docs/audits/2026-10-30-execution-parity.md: опрос
пропускал короткие проходы +1.5R и активировал трейл по артефактам цены демо-стакана).
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
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from app import clock, db, runtime

# клиентские id: rsiE<sid> — вход; аварийный стоп прикреплён к входу (BingX создаёт
# его при филле сам, отдельного client_id у него нет)
_ENTRY_PREFIX = "rsiE"

# Версия исполнения (миграция 028): пишется в executor_orders при постановке и при филле —
# проверка вперёд сравнивает сделки ОДНОЙ механики. История — в миграции 028:
# e1 лимитка · e2 TRIGGER_MARKET · e3 почасовой стоп за трейлом · e4 нативный трейл, опрос
# BingX · e5 активация трейла по стриму Bybit + постановка входа сразу после записи сигнала.
EXECUTION_VERSION = "e5"
EXECUTION_VERSIONS = {
    "e1": "лимитный вход, выход по рынку после журнала",
    "e2": "вход по касанию (TRIGGER_MARKET), выход по рынку после журнала",
    "e3": "стоп на бирже за трейлом журнала раз в такт",
    "e4": "нативный трейл, активация опросом BingX",
    "e5": "нативный трейл, активация по стриму Bybit",
}

# Замки сигналов (30.10.2026): стрим-активация трейла и быстрая постановка входа идут из
# потоков fast-цикла параллельно такту сверки. Без замка трейлинг-ордер мог встать на
# позицию, которую такт в этот же момент закрывает по журналу, — reduce-only сирота съела
# бы долю СЛЕДУЮЩЕГО сигнала того же символа/стороны. RLock: повторный вход того же потока
# безопасен; быстрые пути берут замок без ожидания и уступают такту.
_SID_LOCKS: dict[int, threading.RLock] = {}
_SID_LOCKS_GUARD = threading.Lock()


def _sid_lock(sid: int) -> threading.RLock:
    with _SID_LOCKS_GUARD:
        lk = _SID_LOCKS.get(sid)
        if lk is None:
            lk = _SID_LOCKS[sid] = threading.RLock()
        return lk


# Строки отчёта быстрых путей (поток fast-цикла): уходят админу с ближайшим тактом
# сверки ОДНИМ сообщением — действие мгновенное, уведомление в прежнем ритме и троттлинге.
_PENDING_REPORT: list[str] = []
_PENDING_GUARD = threading.Lock()


def _push_report(lines: list[str]) -> None:
    if lines:
        with _PENDING_GUARD:
            _PENDING_REPORT.extend(lines)


def _drain_report() -> list[str]:
    with _PENDING_GUARD:
        out = list(_PENDING_REPORT)
        _PENDING_REPORT.clear()
    return out


def _iso_ms(iso: str) -> int:
    d = datetime.fromisoformat(iso)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Чистые функции (тестируются без сети)
# ---------------------------------------------------------------------------
MAX_NOTIONAL_USDT = 1500.0   # кап номинала ОДНОГО ордера. 1500 = влезают ТРИ
#                              одновременных сигнала одного символа (мульти-ТФ дубли —
#                              норма журнала): для демо-зеркала ПОКРЫТИЕ замера паритета
#                              важнее размера позиции (20.08: кап 4500 пропускал дубли).
SYMBOL_BUDGET_USDT = 4500.0  # общий бюджет символа/стороны под лимитом BingX 5000 (101209).
#                              До 05.10 кап 1500 работал как ОБЩИЙ бюджет: первый сигнал
#                              занимал всё, дубли пропускались (24 скипа) — замысел 20.08
#                              не исполнялся (аудит 05.10).


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


def daily_guard(day: str, equity: float, anchor_raw: str | None, halt_day: str | None,
                limit_pct: float) -> tuple[str, bool, bool, float]:
    """Стоп-кран по дневной просадке эквити (чистая функция).

    -> (якорь для записи, halted, сработал_сейчас, просадка_%). Якорь — 'YYYY-MM-DD|эквити'
    на первом такте суток; другой день или битый якорь -> новый якорь от текущего эквити.
    Сработавший кран действует до конца UTC-суток (halt_day == day)."""
    a_day, a_eq = "", 0.0
    try:
        a_day, raw_eq = (anchor_raw or "").split("|", 1)
        a_eq = float(raw_eq)
    except ValueError:
        a_day = ""
    halted = halt_day == day
    if a_day != day or a_eq <= 0:
        return f"{day}|{equity:.4f}", halted, False, 0.0
    dd = (a_eq - equity) / a_eq * 100.0
    if not halted and limit_pct > 0 and dd >= limit_pct:
        return anchor_raw or "", True, True, dd
    return anchor_raw or "", halted, False, dd


def _equity_of(bal: dict) -> float:
    """Эквити VST (баланс + нереализованный PnL) из ответа BingX; фолбэк — total VST."""
    data = (bal.get("info") or {}).get("data")
    rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    for row in rows:
        if isinstance(row, dict) and row.get("asset", "VST") == "VST" \
                and row.get("equity") is not None:
            return float(row["equity"])
    return float((bal.get("total") or {}).get("VST", 0.0))


@dataclass
class Action:
    kind: str          # 'place' | 'cancel' | 'close' | 'skip'
    signal_id: int
    detail: str


# пометка пропуска: журнал засчитал касание entry раньше, чем executor поставил вход
PREFILL_NOTE = "филл в журнале раньше постановки"


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
            # поставить вход — цена уже у уровня, честнее пропустить, чем догонять.
            # С 30.10 вход ставит быстрый путь place_fresh через секунды после записи
            # сигнала; сюда попадают касания в первые секунды после публикации
            actions.append(Action("skip", sid, PREFILL_NOTE))
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
_ex_fast_cache: dict = {"ex": None, "born": 0.0}
_EX_TTL_SEC = 6 * 3600            # пересоздать инстанс (и market-кеш ccxt) раз в 6ч


def _exchange(cache: dict | None = None):
    """Кешированный инстанс ccxt: без кеша каждый 60с-такт создавал бы клиента и качал
    полный список контрактов BingX (ревью 13.08) — лишний тяжёлый запрос и точка отказа."""
    import time as _time

    import ccxt

    c = _ex_cache if cache is None else cache
    now = _time.monotonic()
    if c["ex"] is not None and now - c["born"] < _EX_TTL_SEC:
        return c["ex"]
    ex = ccxt.bingx({
        "apiKey": os.environ.get("BINGX_API_KEY", ""),
        "secret": os.environ.get("BINGX_API_SECRET", ""),
        "timeout": 20000,
        "options": {"defaultType": "swap"},
    })
    ex.set_sandbox_mode(True)      # v1: ТОЛЬКО демо (VST) — см. докстринг модуля
    ex.load_markets()
    c["ex"], c["born"] = ex, now
    return ex


def _exchange_fast():
    """Свой инстанс ccxt для потока fast-цикла (вход сразу после сигнала, трейл по стриму):
    такт сверки работает в другом потоке параллельно, а ccxt и requests.Session
    потокобезопасности не обещают. Цена — лишний load_markets раз в 6 ч."""
    return _exchange(_ex_fast_cache)


def _tracked() -> dict:
    return {r["signal_id"]: r["state"] for r in db.query(
        "SELECT signal_id, state FROM executor_orders")}


def _track(sid: int, r, qty: float, client_id: str) -> None:
    db.execute(
        "INSERT OR REPLACE INTO executor_orders(signal_id, symbol, side, qty, entry_px, "
        "stop_px, client_id, state, created_at, updated_at, execution_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (sid, r["symbol"], r["side"], qty, r["entry"], r["stop"], client_id,
         "placing", clock.utcnow_iso(), clock.utcnow_iso(), EXECUTION_VERSION))


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
    # строки быстрых путей (стрим-трейл, постановка входа) — в общее сообщение такта
    report: list[str] = _drain_report()
    ex = _exchange()
    open_rows = db.query("SELECT * FROM signals WHERE status='open' AND result_r IS NULL")
    _retry_budget_skips({r["id"] for r in open_rows})
    tracked = _tracked()
    todo = plan(open_rows, tracked, set(ex.markets), since)
    rows_by_id = {r["id"]: r for r in open_rows}
    # баланс читаем КАЖДЫЙ такт (стоп-кран смотрит эквити), но его сбой не роняет такт:
    # закрытия важнее постановок — без баланса откладываем только постановку. Раньше
    # ранний return/исключение пропускали _sync_fill_states и закрытия (найдено 03.09)
    balance = 0.0
    try:
        bal = ex.fetch_balance()
        balance = float((bal.get("total") or {}).get("VST", 0.0))
        halted = _apply_daily_guard(_equity_of(bal), report)
    except Exception as e:                              # noqa: BLE001 — сеть/биржа
        halted = runtime.executor_halt_day() == clock.utcnow_iso()[:10]
        if any(a.kind == "place" for a in todo):
            todo = [a for a in todo if a.kind != "place"]
            import time as _t
            if _alert_due(_BALANCE_ALERT, _t.monotonic()):
                _BALANCE_ALERT["at"] = _t.monotonic()
                report.append(f"⚠️ баланс не читается — постановка входов отложена: "
                              f"{str(e)[:80]}")
    if halted:
        # стоп-кран: новых входов нет, ждущие триггеры снимаем (иначе сработают и добавят
        # риска), открытые позиции ведём до выхода по журналу — выключение executor целиком
        # оставило бы их без трейлинг-выхода. Сначала сверка филлов: триггер, сработавший
        # между тактами, ещё 'placed' — без сверки «отмена» закрыла бы СВЕЖУЮ позицию по
        # рынку вопреки обещанию «веду до выхода» (аудит 05.10)
        _sync_fill_states(ex, report)
        tracked = _tracked()
        open_ids = set(rows_by_id)
        todo = [a for a in todo if a.kind != "place"]
        todo += [Action("cancel", sid, "стоп-кран") for sid, st in tracked.items()
                 if st in ("placing", "placed") and sid in open_ids]

    for a in todo:
        # замок сигнала: быстрые пути (стрим-трейл, постановка входа) не пересекаются с
        # действием такта по тому же сигналу
        lock = _sid_lock(a.signal_id)
        lock.acquire()
        try:
            if a.kind == "skip":
                if a.signal_id not in tracked:
                    r = rows_by_id[a.signal_id]
                    _track(a.signal_id, r, 0.0, "")
                    _set_state(a.signal_id, "error", a.detail)
                    report.append(f"⏭ #{a.signal_id}: {a.detail}")
            elif a.kind == "place":
                _place_entry(ex, a.signal_id, rows_by_id[a.signal_id], balance, report)
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
                _cancel_native_trail(ex, t)
                if filled_qty > 0:
                    # цена входа для отчёта: из ордера, у сработавшего триггера — из трейдов
                    if _order_filled(o) > 0:
                        _record_fill(a.signal_id, o)
                    else:
                        _record_entry_from_trades(ex, t)
                    closed = _close_position(ex, t, max_qty=filled_qty)
                    if closed <= 0:
                        # вход и аварийный стоп отработали внутри одного такта — цена
                        # стопа, иначе быстрейшие −1R выпадали из отчёта (аудит 05.10)
                        _record_stop_close(ex, t)
                    _set_state(a.signal_id, "closed",
                               f"филл {filled_qty:g} закрыт при отмене")
                    report.append(f"📤 #{a.signal_id} {t['symbol']}: вход снят, "
                                  f"исполненная часть {closed:g} закрыта по рынку")
                else:
                    _set_state(a.signal_id, "cancelled", a.detail or None)
                    why = "стоп-кран" if a.detail == "стоп-кран" else "журнал закрыл/истёк"
                    report.append(f"🚫 #{a.signal_id}: вход снят ({why})")
            elif a.kind == "close":
                t = db.query_one("SELECT * FROM executor_orders WHERE signal_id=?",
                                 (a.signal_id,))
                assert t is not None                    # tracked-строка есть по построению plan
                _cancel_entry(ex, t)
                _cancel_deferred_stop(ex, t)            # довешенный стоп не осиротить
                _cancel_native_trail(ex, t)
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
                    _set_state(a.signal_id, "closed", "позиция уже плоская (стоп/трейл биржи)")
                    report.append(f"📤 #{a.signal_id} {t['symbol']}: уже закрыто на бирже "
                                  "(стоп или трейлинг)")
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
        finally:
            lock.release()

    # переход placed -> filled (информативно): вход исчез из открытых, позиция есть
    _sync_fill_states(ex, report)
    # трейл: нативный трейлинг-ордер биржи; почасовая перестановка стопа — фолбэк
    _sync_trail_stops(ex, report, skip=_sync_native_trails(ex, report))
    # сироты: позиции биржи без учёта (наследие сломанного детектора филлов) —
    # алерт не чаще раза в час, закрытие только по /executor sweep
    import time as _time
    if _alert_due(_ORPHAN_ALERT, _time.monotonic()):
        try:
            orph = _orphan_lines(ex)
        except Exception as e:                          # noqa: BLE001 — сироты не роняют такт
            orph = [f"⚠️ проверка сирот: {str(e)[:80]}"]
        if orph:
            _ORPHAN_ALERT["at"] = _time.monotonic()
            report.extend(orph)
    return report


def _place_entry(ex, sid: int, r, balance: float, report: list[str],
                 require_unfilled: bool = False) -> None:
    """Поставить вход сигнала: TRIGGER_MARKET по касанию entry с прикреплённым аварийным
    стопом (−1R). Зовётся тактом сверки и быстрым путём place_fresh — ПОД замком сигнала.
    Сигнал уже под учётом (быстрый путь или прошлый такт успели) — ничего не делаем.
    require_unfilled (быстрый путь): журнал уже засчитал касание — вход не ставим, пропуск
    «филл в журнале раньше постановки» оформит такт сверки по прежнему правилу."""
    if db.query_one("SELECT 1 FROM executor_orders WHERE signal_id=?", (sid,)):
        return
    if require_unfilled:
        cur = db.query_one("SELECT filled_at FROM signals WHERE id=?", (sid,))
        if cur is None or cur["filled_at"]:
            return
    m = ex.market(r["symbol"])
    lim = m.get("limits", {}) or {}
    budget = min(MAX_NOTIONAL_USDT,
                 SYMBOL_BUDGET_USDT - _active_notional(r["symbol"], r["side"]))
    qty = calc_qty(balance, runtime.executor_risk_pct(), r["entry"], r["stop"],
                   float((lim.get("amount") or {}).get("min") or 0),
                   float(m.get("precision", {}).get("amount") or 0),
                   max_notional=budget)
    if qty <= 0:
        used = _active_notional(r["symbol"], r["side"])
        why = ("бюджет номинала символа/стороны исчерпан "
               f"(кап {SYMBOL_BUDGET_USDT:g} USDT, занято {used:g})"
               if budget < MAX_NOTIONAL_USDT else "объём ниже минимума биржи")
        _track(sid, r, 0.0, "")
        _set_state(sid, "error", why)
        report.append(f"⏭ #{sid}: {why}")
        return
    side = "buy" if r["side"] == "LONG" else "sell"
    client_id = f"{_ENTRY_PREFIX}{sid}"
    # учёт ДО create_order (state='placing'): таймаут ПОСЛЕ отправки не
    # оставит ордер-сироту — recovery в except сверит по clientOrderID
    _track(sid, r, qty, client_id)
    # цены — к тику инструмента: сырой стоп ловил 101400
    # «SL Price must be…» на монетах с крупным тиком (#343 PORTAL 20.08)
    entry_px = float(ex.price_to_precision(r["symbol"], r["entry"]))
    sl = {"type": "STOP_MARKET",
          "stopPrice": float(ex.price_to_precision(r["symbol"], r["stop"]))}
    base = {"positionSide": r["side"], "clientOrderID": client_id, "stopLoss": sl}
    # Направление триггера BingX ставит сам от текущей цены — это СОВПАДАЕТ
    # с журнальной семантикой «филл касанием entry с любой стороны»:
    # trigger сработает ровно тогда, когда журнал засчитает касание.
    from ccxt import ExchangeError as _ExErr
    from ccxt import NetworkError as _NetErr
    deferred_sl = False
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
                    params={"positionSide": r["side"], "clientOrderID": client_id,
                            "triggerPrice": entry_px})
                # метка ставится ВМЕСТЕ с 'placed' ниже: отдельный UPDATE
                # затирался _set_state(…, 'placed') — стоп после филла не
                # довешивался, позиция жила без биржевого стопа (аудит 05.10)
                deferred_sl = True
            else:
                # триггер отвергнут (цена у уровня?). Немедленный маркет
                # честен ТОЛЬКО если журнал УЖЕ засчитал касание (гонка
                # внутри такта) — истина у журнала, не у тикера
                fresh = db.query_one("SELECT filled_at FROM signals WHERE id=?", (sid,))
                if not (fresh and fresh["filled_at"]):
                    raise
                created = ex.create_order(r["symbol"], "market", side, qty, None,
                                          params=dict(base))
        _store_order_id(sid, (created or {}).get("id"))
    except Exception as e_create:                       # noqa: BLE001
        # неоднозначный сбой (таймаут после отправки?): сверить по clientOrderID —
        # ордер реально стоит -> успех; нет -> настоящая ошибка наверх
        trow = db.query_one("SELECT * FROM executor_orders WHERE signal_id=?", (sid,))
        if _entry_order(ex, trow) is None:
            if isinstance(e_create, _NetErr):
                # ответ потерян в сети: ордер мог встать, но ещё не виден в
                # листинге. 'error' навсегда выбрасывал его из сверки — живой
                # триггер никто не снимал (аудит 05.10). Остаёмся в 'placing':
                # _sync_fill_states подхватит ордер или через 10 мин честно
                # закроет строку
                report.append(f"⚠️ #{sid} {r['symbol']}: ответ биржи "
                              f"потерян ({type(e_create).__name__}) — сверю "
                              "следующими тактами")
                return
            raise
    _set_state(sid, "placed", "стоп отложен до филла" if deferred_sl else None)
    report.append(f"📥 #{sid} {r['symbol']} {r['side']}: "
                  f"триггер-маркет {qty:g} @ {r['entry']:g}, "
                  + (f"стоп {r['stop']:g} довешу при филле (VST)" if deferred_sl
                     else f"стоп {r['stop']:g} (VST)"))


def place_fresh() -> list[str]:
    """Быстрая постановка входов ТОЛЬКО ЧТО записанных сигналов (fast-цикл бота, ~2 с).

    Аудит 30.10 (#698/#704, раньше #691/#562/#487…): вход ставился на ближайшем такте
    сверки — до 60 с + время обслуживания после записи сигнала, — а стрим засчитывал
    касание entry через 3–42 с после записи (уровень у текущей цены). Executor честно
    пропускал такие сигналы («филл в журнале раньше постановки»): ордера ещё не было.
    Здесь триггер встаёт через секунды после записи. Сеть — только когда есть новый
    сигнал; любой сбой молча оставляет постановку такту сверки (там и алерты)."""
    since = runtime.executor_since() or ""
    if not since or not runtime.executor_enabled() or not os.environ.get("BINGX_API_KEY"):
        return []
    rows = db.query(
        "SELECT * FROM signals WHERE status='open' AND result_r IS NULL AND filled_at IS NULL "
        "AND created_at >= ? AND id NOT IN (SELECT signal_id FROM executor_orders)", (since,))
    if not rows or runtime.executor_halt_day() == clock.utcnow_iso()[:10]:
        return []
    report: list[str] = []
    try:
        ex = _exchange_fast()
        rows = [r for r in rows if r["symbol"] in ex.markets]
        if not rows:
            return []
        bal = ex.fetch_balance()
        balance = float((bal.get("total") or {}).get("VST", 0.0))
        if _apply_daily_guard(_equity_of(bal), report):
            _push_report(report)
            return report
    except Exception:                                   # noqa: BLE001 — сеть: решит такт
        return []
    for r in rows:
        lock = _sid_lock(r["id"])
        if not lock.acquire(blocking=False):
            continue                                    # такт сверки ведёт этот сигнал
        try:
            _place_entry(ex, r["id"], r, balance, report, require_unfilled=True)
        except Exception as e:                          # noqa: BLE001 — как ветка place такта
            _set_state(r["id"], "error", str(e)[:200])
            report.append(f"⚠️ #{r['id']}: {type(e).__name__}: {str(e)[:120]}")
        finally:
            lock.release()
    _push_report(report)
    return report


_BALANCE_ALERT: dict = {"at": None}


def _alert_due(stamp: dict, now_mono: float, every_s: float = 3600.0) -> bool:
    """Троттлинг алертов «раз в час». None = ещё не алертили. Старт с 0.0 глушил ПЕРВЫЙ
    алерт целый час после загрузки Mac: monotonic() считает секунды с загрузки (аудит 05.10,
    реальная перезагрузка 05.10 06:59)."""
    return stamp["at"] is None or now_mono - stamp["at"] > every_s


def _apply_daily_guard(equity: float, report: list[str]) -> bool:
    """Обновить якорь суток, проверить стоп-кран; при срабатывании — метка и алерт."""
    day = clock.utcnow_iso()[:10]
    prev = runtime.executor_day_anchor()
    anchor, halted, tripped, dd = daily_guard(day, equity, prev, runtime.executor_halt_day(),
                                              runtime.executor_daily_loss_pct())
    if anchor != prev:
        runtime.set_value("executor_day_anchor", anchor)
    if tripped:
        runtime.set_value("executor_halt_day", day)
        report.append(f"⛔ Стоп-кран executor: эквити {equity:,.0f} VST, −{dd:.1f}% за сутки "
                      f"(порог {runtime.executor_daily_loss_pct():g}%). До 00:00 UTC новые "
                      "входы не ставлю, ждущие триггеры снимаю, открытые позиции веду до "
                      "выхода. Порог: /executor limit N".replace(",", " "))
    return halted


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
    note = (t["note"] or "") if t else ""
    m = _RETRY_RE.match(note)
    n = (int(m.group(1)) if m else 0) + 1
    # id довешенного стопа переживает ретраи: иначе после сбоя закрытия стоп-ордер
    # оставался на бирже без учёта (аудит 05.10)
    sl = _sl_id(note)
    tail = f" | sl:{sl}" if sl else ""
    db.execute("UPDATE executor_orders SET note=?, updated_at=? WHERE signal_id=?",
               (f"retry#{n}: {str(e)[:150]}{tail}", clock.utcnow_iso(), sid))
    return n


_SL_RE = _re.compile(r"(?:^|\| )sl:(\S+)")


def _sl_id(note: str | None) -> str:
    """id довешенного стопа из note: 'sl:<id>' в начале или хвост ' | sl:<id>'."""
    m = _SL_RE.search(note or "")
    return m.group(1) if m else ""


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


# Стоп, ЗАКРЫВАЮЩИЙ позицию в hedge-режиме. ccxt для stopLossPrice и сам ставит reduceOnly
# (positionSide = сторона закрываемой позиции) — здесь явно, чтобы не зависеть от версии.
# 110411/110412 («стоп должен быть ниже/выше цены») — НЕ сторона: цена к моменту
# перестановки уже за новым уровнем трейла (бар закрылся ниже «пик − 0.2R»).
_CLOSE_STOP = {"hedged": True, "reduceOnly": True}


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
                t["symbol"], t["stop_px"])), **_CLOSE_STOP})
        _set_state(t["signal_id"], "filled", f"sl:{(o or {}).get('id') or ''}")
        report.append(f"🛑 #{t['signal_id']} {t['symbol']}: аварийный стоп довешен")
    except Exception as e:                              # noqa: BLE001 — виден админу, журнал закроет сам
        _set_state(t["signal_id"], "filled", f"стоп НЕ встал: {str(e)[:120]}")
        report.append(f"⚠️ #{t['signal_id']} {t['symbol']}: аварийный стоп не встал "
                      f"({str(e)[:80]}) — закрытие останется за журналом")


def _cancel_deferred_stop(ex, t) -> None:
    """Снять довешенный аварийный стоп (note 'sl:<id>'), если он был. Уже исполнен/
    снят — не ошибка."""
    oid = _sl_id(t["note"])
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


def _trades_since(ex, t) -> list:
    """Трейды символа нашей стороны позиции С МОМЕНТА постановки входа, по времени.
    Без since BingX отдавал не последние трейды — «последний из 20» оказывался ЧУЖИМ
    (#655 WLD: записан вход 0.5003 при реальном 0.5871 → фантомные −8R; 14 из 99 сделок
    с неправдоподобной ценой, отчёт исполнения врал: −29R вместо +1.9R, замер 24.10)."""
    from datetime import datetime
    since = int(datetime.fromisoformat(t["created_at"]).timestamp() * 1000) - 60_000
    out = []
    for tr in ex.fetch_my_trades(t["symbol"], since=since, limit=100) or []:
        pos_side = ((tr.get("info") or {}).get("positionSide") or "").upper()
        if pos_side in ("", t["side"]) and (tr.get("timestamp") or 0) >= since:
            out.append(tr)
    return sorted(out, key=lambda tr: tr.get("timestamp") or 0)


def _nearest(trades: list, side: str, level: float | None, after: int = 0):
    """Трейд нужной стороны после `after`, ближайший по цене к ожидаемому уровню (вход —
    триггер на entry, стоп — уровень стопа): при слитой позиции символа отделяет наш
    трейд от соседского. level=None — первый по времени."""
    cand = [tr for tr in trades if tr.get("side") == side and (tr.get("timestamp") or 0) > after]
    if not cand:
        return None
    if level is None:
        return cand[0]
    return min(cand, key=lambda tr: abs(float(tr.get("price") or 0.0) - level))


def _record_entry_from_trades(ex, t) -> None:
    """Цена входа сработавшего триггера (best-effort): сам триггер-ордер не несёт
    филла — берём трейд ВХОДНОЙ стороны после постановки, ближайший к уровню входа.
    Без этого слип входа триггерных сделок выпадал бы из отчёта «исполнение vs журнал»."""
    try:
        entry_side = "buy" if t["side"] == "LONG" else "sell"
        tr = _nearest(_trades_since(ex, t), entry_side, float(t["entry_px"] or 0) or None)
        if tr is not None:
            px = tr.get("price")
            db.execute(
                "UPDATE executor_orders SET fill_px=COALESCE(fill_px, ?), "
                "fill_qty=COALESCE(fill_qty, ?), fill_fee=COALESCE(fill_fee, ?) "
                "WHERE signal_id=?",
                (float(px) if px else None, float(t["qty"]), _fee_cost(tr), t["signal_id"]))
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
        entry_side = "buy" if close_side == "sell" else "sell"
        trades = _trades_since(ex, t)
        ent = _nearest(trades, entry_side, float(t["entry_px"] or 0) or None)
        jx = db.query_one("SELECT exit_price FROM signals WHERE id=?", (t["signal_id"],))
        level = ((jx["exit_price"] if jx else None)
                 or (t["trail_px"] if "trail_px" in t.keys() and t["trail_px"] else None)
                 or t["stop_px"])
        tr = _nearest(trades, close_side, float(level) if level else None,
                      after=(ent.get("timestamp") or 0) if ent else 0)
        if tr is not None:
            px = tr.get("price")
            db.execute(
                "UPDATE executor_orders SET close_px=COALESCE(close_px, ?), "
                "close_fee=COALESCE(close_fee, ?), "
                "closed_at=COALESCE(closed_at, ?) WHERE signal_id=?",
                (float(px) if px else None, _fee_cost(tr), clock.utcnow_iso(),
                 t["signal_id"]))
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
    # своя доля = позиция минус доли ДРУГИХ ведущихся филлов того же символа/стороны:
    # если аварийный стоп уже закрыл нашу часть, остаток — соседский, его не трогаем
    # (раньше кламп по всей позиции продавал долю соседа, аудит 05.10)
    others = db.query_one(
        "SELECT COALESCE(SUM(qty), 0) AS q FROM executor_orders WHERE symbol=? AND side=? "
        "AND state='filled' AND signal_id != ?", (t["symbol"], t["side"], t["signal_id"]))
    own = _position_qty(ex, t["symbol"], t["side"]) - float(others["q"] if others else 0.0)
    closable = min(max_qty, max(0.0, own))
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


def _is_tighter(side: str, new: float, cur: float) -> bool:
    return new > cur if side == "LONG" else new < cur


def _find_stop_order(ex, t, level: float):
    """Наш стоп на бирже: по id из note ('sl:<id>' — довешенный/переставленный) либо
    прикреплённый ко входу — STOP_MARKET той же стороны позиции на уровне level с нашим
    объёмом (смоук 20.10: BingX отдаёт его в fetch_open_orders, info.type/positionSide/
    stopPrice/origQty). None — не найден (исполнен/снят)."""
    oid = _sl_id(t["note"])
    opens = ex.fetch_open_orders(t["symbol"]) or []
    if oid:
        return next((o for o in opens if str(o.get("id")) == oid), None)
    want = float(ex.price_to_precision(t["symbol"], level))
    qty = float(t["qty"])
    for o in opens:
        info = o.get("info") or {}
        if (str(info.get("type", "")).upper() not in ("STOP_MARKET", "STOP")
                or str(info.get("positionSide", "")).upper() != t["side"]):
            continue
        try:
            px = float(info.get("stopPrice") or o.get("stopPrice") or 0.0)
            q = float(info.get("origQty") or o.get("amount") or 0.0)
        except (TypeError, ValueError):
            continue
        if abs(px - want) <= abs(want) * 1e-6 and abs(q - qty) <= qty * 1e-6:
            return o
    return None


_TRAIL_WARNED: dict = {}


def _sync_trail_stops(ex, report: list[str], skip: set | frozenset = frozenset()) -> None:
    """Стоп на бирже вслед за трейлом журнала: новый reduce-only стоп ставится ДО снятия
    старого (позиция ни на миг без стопа). Цена уже за новым уровнем (101400) — журнал
    засчитает выход по трейлу на закрытии бара; закрываемся по рынку сразу."""
    import time as _t

    rows = db.query(
        "SELECT eo.*, s.trail_stop FROM executor_orders eo JOIN signals s "
        "ON s.id = eo.signal_id WHERE eo.state='filled' AND s.status='open' "
        "AND s.result_r IS NULL AND s.trail_stop IS NOT NULL")
    for t in rows:
        sid, sym = t["signal_id"], t["symbol"]
        if sid in skip or t["trail_order_id"]:
            continue                                    # нативный трейл ведёт биржа
        lock = _sid_lock(sid)
        if not lock.acquire(blocking=False):
            continue                                    # стрим ставит нативный трейл сейчас
        try:
            _move_trail_stop(ex, t, report)
        except Exception as e:                          # noqa: BLE001 — один сигнал не роняет такт
            stamp = _TRAIL_WARNED.setdefault(sid, {"at": None})
            if _alert_due(stamp, _t.monotonic()):       # повтор каждый такт — не спамить
                stamp["at"] = _t.monotonic()
                report.append(f"⚠️ #{sid} {sym}: трейл не переставлен — "
                              f"{type(e).__name__}: {str(e)[:100]}")
        finally:
            lock.release()


def _move_trail_stop(ex, t, report: list[str]) -> None:
    """Один шаг почасового трейла (фолбэк к нативному): стоп BingX на уровень журнала."""
    import time as _t

    from ccxt import ExchangeError as _ExErr
    sid, sym = t["signal_id"], t["symbol"]
    cur = t["trail_px"] if t["trail_px"] is not None else t["stop_px"]
    new = float(t["trail_stop"])
    if cur is None or not _is_tighter(t["side"], new, float(cur)):
        return
    new_px = float(ex.price_to_precision(sym, new))
    if not _is_tighter(t["side"], new_px, float(cur)):
        return                                          # сдвиг меньше тика
    old = _find_stop_order(ex, t, float(cur))
    if old is None:
        # стоп уже сработал (позиция закрыта) — закрытие оформит ветка close по
        # журналу; живая позиция без найденного стопа — алерт раз в час
        if _position_qty(ex, sym, t["side"]) > 0 and _alert_due(
                _TRAIL_WARNED.setdefault(sid, {"at": None}), _t.monotonic()):
            _TRAIL_WARNED[sid]["at"] = _t.monotonic()
            report.append(f"⚠️ #{sid} {sym}: стоп на бирже не найден — трейл "
                          f"{new_px:g} не переставлен")
        return
    close_side = "sell" if t["side"] == "LONG" else "buy"
    try:
        o = ex.create_order(sym, "market", close_side, float(t["qty"]), None,
                            params={"stopLossPrice": new_px, **_CLOSE_STOP})
    except _ExErr as e:
        # цена уже за новым уровнем: 101400 (вход со связкой) / 110411 (LONG: стоп
        # должен быть НИЖЕ цены) / 110412 (SHORT: ВЫШЕ) — журнал засчитает выход
        if not any(c in str(e) for c in ("101400", "110411", "110412")):
            raise
        closed = _close_position(ex, t, max_qty=float(t["qty"]))
        _cancel_stop(ex, sym, old.get("id"))
        _set_state(sid, "closed", f"трейл {new_px:g}: цена уже за стопом — по рынку")
        report.append(f"📤 #{sid} {sym}: цена уже за трейлом {new_px:g} — "
                      f"закрыто по рынку {closed:g} (VST)")
        return
    db.execute("UPDATE executor_orders SET trail_px=?, note=?, updated_at=? "
               "WHERE signal_id=?",
               (new_px, f"sl:{(o or {}).get('id') or ''}", clock.utcnow_iso(), sid))
    _cancel_stop(ex, sym, old.get("id"))
    report.append(f"🪜 #{sid} {sym} {t['side']}: стоп подтянут "
                  f"{float(cur):g} → {new_px:g} (VST)")


# --- нативный трейл (TRAILING_STOP_MARKET) -------------------------------------------------
# Метка-захват в trail_order_id, пока create_order в полёте или его ответ потерян: второй
# трейл на ту же позицию не встанет ни из другого потока, ни после рестарта (метка в БД).
_PENDING_TRAIL = "pending:"
_PENDING_TRAIL_STALE_S = 120.0
STREAM_STALE_S = 120.0       # кадров стрима по символу нет дольше — активация опросом BingX
ACT_RETRY_S = 30.0           # не чаще одной попытки активации сигнала за 30 с (стрим ~1 кадр/с)

_TRAIL_ROWS_SQL = (
    "SELECT eo.*, s.entry AS j_entry, s.stop AS j_stop, s.trail_stop, s.snapshot_json, "
    "s.filled_at AS j_filled_at FROM executor_orders eo JOIN signals s ON s.id = eo.signal_id "
    "WHERE eo.state='filled' AND s.status='open' AND s.result_r IS NULL")

# ws-символ ('BTCUSDT') -> позиции, ждущие активации: [{sid, side, act, fill_ms}]. Кеш
# пересобирает fast-цикл (refresh_armed, ~10 с) и ЗАМЕНЯЕТ целиком (не мутирует) — горячий
# обработчик стрима читает его из event loop без SQL, сети и гонок.
_ARMED: dict[str, list[dict]] = {}
_STREAM_SEEN: dict[str, float] = {}     # ws-символ -> monotonic последнего кадра стрима
_ACT_TRIED: dict[int, float] = {}       # sid -> monotonic последней попытки (только event loop)
_JLEVEL_SEEN: dict[int, float] = {}     # sid -> уровень трейла журнала, уже сверенный с ценой


def refresh_armed() -> int:
    """Пересобрать из БД позиции, ждущие активации нативного трейла. Только SQL, без сети.
    Условия: позиция открыта на BingX (state='filled'), журнал засчитал вход (filled_at —
    ход сделки считается только после него), у сигнала есть правило трейла, трейла ещё нет."""
    from app.brain import journal
    from app.data.stream import ws_symbol

    global _ARMED
    armed: dict[str, list[dict]] = {}
    for t in db.query(_TRAIL_ROWS_SQL
                      + " AND (eo.trail_order_id IS NULL OR eo.trail_order_id='')"):
        rule = journal._trail_of(t)
        risk = abs(t["j_entry"] - t["j_stop"])
        if rule is None or not risk or not t["j_filled_at"]:
            continue
        sg = 1 if t["side"] == "LONG" else -1
        armed.setdefault(ws_symbol(t["symbol"]), []).append({
            "sid": t["signal_id"], "side": t["side"],
            "act": t["j_entry"] + sg * rule["activate_r"] * risk,
            "fill_ms": _iso_ms(t["j_filled_at"])})
    _ARMED = armed
    return sum(len(v) for v in armed.values())


def disarm_all() -> None:
    global _ARMED
    _ARMED = {}


def _disarm(sid: int) -> None:
    global _ARMED
    _ARMED = {k: [it for it in v if it["sid"] != sid] for k, v in _ARMED.items()}


def stream_tick(symbol_ws: str, high: float, low: float, close: float,
                start_ms: int = 0, now_ms: int | None = None) -> list[int]:
    """Кадр реалтайм-стрима Bybit (горячий путь event loop: только память). -> sid позиций,
    чей ход по цене ЖУРНАЛА прошёл уровень активации трейла — их активировать сейчас.

    Экстремум бара (high/low) берётся, лишь если бар начался ПОСЛЕ филла журнала: high/low
    бара филла включают цену ДО входа (пик до касания entry — не ход сделки), там — только
    последняя цена. Экстремум ловит и проход между кадрами, и проход во время переподключения
    стрима (кадр после реконнекта несёт high/low всего бара). Повтор по сигналу — не чаще
    ACT_RETRY_S: активация идемпотентна (захват в БД), троттлинг лишь бережёт API BingX."""
    import time as _t

    mono = _t.monotonic()
    _STREAM_SEEN[symbol_ws] = mono
    items = _ARMED.get(symbol_ws)
    if not items:
        return []
    now_ms = int(_t.time() * 1000) if now_ms is None else now_ms
    out = []
    for it in items:
        if now_ms <= it["fill_ms"]:
            continue
        whole_bar = bool(start_ms) and start_ms >= it["fill_ms"]
        if it["side"] == "LONG":
            hit = (high if whole_bar else close) >= it["act"]
        else:
            hit = (low if whole_bar else close) <= it["act"]
        if not hit:
            continue
        last = _ACT_TRIED.get(it["sid"])
        if last is not None and mono - last < ACT_RETRY_S:
            continue
        _ACT_TRIED[it["sid"]] = mono
        out.append(it["sid"])
    return out


def activate_trail(sid: int) -> list[str]:
    """Стрим Bybit увидел проход уровня активации — поставить нативный трейл СЕЙЧАС, не ждать
    такта сверки (поток fast-цикла). Замок сигнала берётся без ожидания: занят тактом — тот
    и решит. Цена BingX (last) — подтверждение и якорь: биржевой трейл ведёт её; если она уже
    за трейлом журнала — выход по рынку. -> строки отчёта (они же уходят в такт)."""
    import time as _t

    lock = _sid_lock(sid)
    if not lock.acquire(blocking=False):
        return []
    report: list[str] = []
    try:
        if not runtime.executor_enabled() or not os.environ.get("BINGX_API_KEY"):
            return []
        t = db.query_one(_TRAIL_ROWS_SQL + " AND eo.signal_id=?", (sid,))
        if t is None or t["trail_order_id"]:
            return []
        _activate_native(_exchange_fast(), t, report, "стрим Bybit")
        _disarm(sid)              # трейл стоит / позиция закрыта; иначе вернёт refresh_armed
    except Exception as e:                              # noqa: BLE001 — такт сверки повторит
        stamp = _TRAIL_WARNED.setdefault(sid, {"at": None})
        if _alert_due(stamp, _t.monotonic()):
            stamp["at"] = _t.monotonic()
            report.append(f"⚠️ #{sid}: нативный трейл (стрим) не встал — "
                          f"{type(e).__name__}: {str(e)[:100]}")
    finally:
        lock.release()
    _push_report(report)
    return report


def _activate_native(ex, t, report: list[str], source: str, px: float | None = None) -> bool:
    """Поставить нативный трейл позиции (вызывающий держит замок сигнала). -> True, если
    позиция теперь под трейлом (или его ставит другой поток); закрыта по рынку — False.

    Цену АКТИВАЦИИ BingX через API игнорирует (смоук 24.10: activationPrice молча отброшен,
    трейл начинался сразу) — поэтому ордер ставится в момент активации и тянется от текущей
    цены BingX. Захват в БД ДО create_order: потерянный ответ не даст второго трейла."""
    from ccxt import NetworkError as _NetErr

    from app.brain import journal
    sid, sym = t["signal_id"], t["symbol"]
    rule = journal._trail_of(t)
    risk = abs(t["j_entry"] - t["j_stop"])
    if rule is None or not risk:
        return False
    if px is None:
        px = float(ex.fetch_ticker(sym)["last"])
    sg = 1 if t["side"] == "LONG" else -1
    move_r = sg * (px - t["j_entry"]) / risk
    if t["trail_stop"] is not None and sg * (px - float(t["trail_stop"])) <= 0:
        # журнал подтянул трейл на закрытии бара, цена BingX уже за ним: журнал засчитает
        # выход на следующем баре — выходим сейчас. Переставленный почасовой стоп ('sl:'
        # в note) снять: строка уходит в closed, ветка close такта его уже не увидит
        _cancel_deferred_stop(ex, t)
        closed = _close_position(ex, t, max_qty=float(t["qty"]))
        if closed <= 0:
            _record_stop_close(ex, t)
        _set_state(sid, "closed", f"цена {px:g} уже за трейлом журнала — по рынку")
        report.append(f"📤 #{sid} {sym}: цена уже за трейлом {float(t['trail_stop']):g} — "
                      f"закрыто по рынку {closed:g} (VST)")
        return False
    now = clock.utcnow_iso()
    claimed = db.execute(
        "UPDATE executor_orders SET trail_order_id=?, updated_at=? WHERE signal_id=? "
        "AND state='filled' AND (trail_order_id IS NULL OR trail_order_id='')",
        (_PENDING_TRAIL + now, now, sid))
    if claimed != 1:
        return True                                     # трейл уже стоит / ставится
    dist = float(ex.price_to_precision(sym, rule["distance_r"] * risk))
    close_side = "sell" if t["side"] == "LONG" else "buy"
    try:
        o = ex.create_order(sym, "market", close_side, float(t["qty"]), None,
                            params={"trailingAmount": dist, **_CLOSE_STOP})
    except _NetErr:
        raise                                           # ответ мог потеряться: метка остаётся
    except Exception:
        db.execute("UPDATE executor_orders SET trail_order_id=NULL WHERE signal_id=? "
                   "AND trail_order_id=?", (sid, _PENDING_TRAIL + now))
        raise
    oid = str((o or {}).get("id") or "")
    if oid:                                             # без id — метку разрешит листинг
        db.execute("UPDATE executor_orders SET trail_order_id=?, updated_at=? "
                   "WHERE signal_id=?", (oid, clock.utcnow_iso(), sid))
    report.append(f"🎯 #{sid} {sym} {t['side']}: трейлинг-стоп на бирже, отступ {dist:g} "
                  f"(ход BingX {move_r:+.2f}R; {source})")
    return True


def _find_trail_order(ex, t):
    """Наш TRAILING_STOP_MARKET в открытых ордерах: та же сторона позиции и наш объём.
    clientOrderID трейлу не задаём — параметр для трейлинг-ордера не проверен смоуком."""
    qty = float(t["qty"])
    for o in ex.fetch_open_orders(t["symbol"]) or []:
        info = o.get("info") or {}
        typ = str(info.get("type") or o.get("type") or "").upper()
        if "TRAILING" not in typ or str(info.get("positionSide", "")).upper() != t["side"]:
            continue
        try:
            q = float(info.get("origQty") or o.get("amount") or 0.0)
        except (TypeError, ValueError):
            continue
        if abs(q - qty) <= qty * 1e-6:
            return o
    return None


def _resolve_pending_trail(ex, t) -> None:
    """Метка-захват без id (ответ create потерян): найти трейл в листинге и записать id.
    Нет его дольше 2 мин: позиция жива — снять метку (активация повторится), позиция
    плоская — метку оставить (трейл, видимо, уже исполнился; закрытие оформит журнал)."""
    marker = t["trail_order_id"] or ""
    sid = t["signal_id"]
    o = _find_trail_order(ex, t)
    if o is not None and o.get("id"):
        db.execute("UPDATE executor_orders SET trail_order_id=?, updated_at=? "
                   "WHERE signal_id=? AND trail_order_id=?",
                   (str(o["id"]), clock.utcnow_iso(), sid, marker))
        return
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(marker[len(_PENDING_TRAIL):])).total_seconds()
    except ValueError:
        age = _PENDING_TRAIL_STALE_S + 1
    if age > _PENDING_TRAIL_STALE_S and _position_qty(ex, t["symbol"], t["side"]) > 0:
        db.execute("UPDATE executor_orders SET trail_order_id=NULL, updated_at=? "
                   "WHERE signal_id=? AND trail_order_id=?", (clock.utcnow_iso(), sid, marker))


def _journal_level_check(ex, t, report: list[str]) -> None:
    """Нативный трейл стоит, но журнал на закрытии бара подтянул СВОЙ трейл выше биржевого
    (трейл встал поздно: обрыв сети Mac, опоздавшая активация) и цена BingX уже за ним —
    журнал гарантированно засчитает выход на следующем баре. Снимаем трейл и выходим по
    рынку сейчас. Сверка — раз на новый уровень журнала (≤ 1 тикер в час на позицию)."""
    lvl = t["trail_stop"]
    sid = t["signal_id"]
    if lvl is None or _JLEVEL_SEEN.get(sid) == float(lvl):
        return
    px = float(ex.fetch_ticker(t["symbol"])["last"])
    _JLEVEL_SEEN[sid] = float(lvl)
    sg = 1 if t["side"] == "LONG" else -1
    if sg * (px - float(lvl)) > 0:
        return
    _cancel_native_trail(ex, t)
    # учёт отражает биржу: при сбое закрытия следующий такт увидит позицию без трейла и
    # повторит выход по ветке «цена за трейлом журнала»
    db.execute("UPDATE executor_orders SET trail_order_id=NULL, updated_at=? WHERE signal_id=?",
               (clock.utcnow_iso(), sid))
    _cancel_deferred_stop(ex, t)
    closed = _close_position(ex, t, max_qty=float(t["qty"]))
    if closed <= 0:
        _record_stop_close(ex, t)
    _set_state(sid, "closed", f"цена {px:g} уже за трейлом журнала {float(lvl):g} — по рынку")
    report.append(f"📤 #{sid} {t['symbol']}: цена уже за трейлом журнала {float(lvl):g} "
                  f"(нативный трейл ниже) — закрыто по рынку {closed:g} (VST)")


def _sync_native_trails(ex, report: list[str]) -> set:
    """Нативный трейл на такте сверки (фолбэк и обслуживание). -> sid с живым трейлом.

    Активацию ведёт стрим Bybit (stream_tick -> activate_trail, цена журнала, секунды). Такт:
      * трейла нет, журнал подтянул трейл на закрытии бара -> поставить трейл (или выйти по
        рынку, если цена BingX уже за уровнем журнала);
      * трейла нет, стрим по символу молчит дольше STREAM_STALE_S (выключен, обрыв, символ
        вне вотчлиста) -> прежнее правило: цена BingX прошла +activate_r;
      * трейл стоит -> разрешить метку-захват / сверить с новым уровнем журнала.
    При живом стриме тикер BingX раз в такт больше не опрашивается: аудит 30.10 — опрос
    пропускал короткие проходы +1.5R (#715) и активировал трейл по артефакту цены
    демо-стакана, которого не было на рынке журнала (#695 SOL, 27.10)."""
    import time as _t

    from app.brain import journal
    from app.data.stream import ws_symbol
    active: set = set()
    for t in db.query(_TRAIL_ROWS_SQL):
        sid, sym = t["signal_id"], t["symbol"]
        lock = _sid_lock(sid)
        if not lock.acquire(blocking=False):
            active.add(sid)                             # быстрый путь ставит трейл сейчас
            continue
        try:
            oid = t["trail_order_id"] or ""
            if oid:
                active.add(sid)
                if oid.startswith(_PENDING_TRAIL):
                    _resolve_pending_trail(ex, t)
                else:
                    _journal_level_check(ex, t, report)
                continue
            journal_active = t["trail_stop"] is not None
            if not journal_active:
                seen = _STREAM_SEEN.get(ws_symbol(sym))
                if seen is not None and _t.monotonic() - seen < STREAM_STALE_S:
                    continue                            # активацию ведёт стрим Bybit
            rule = journal._trail_of(t)
            risk = abs(t["j_entry"] - t["j_stop"])
            if rule is None or not risk:
                continue
            try:
                px = float(ex.fetch_ticker(sym)["last"])
            except Exception:                           # noqa: BLE001 — сеть: следующим тактом
                continue
            sg = 1 if t["side"] == "LONG" else -1
            if not journal_active and (not t["j_filled_at"]
                                       or sg * (px - t["j_entry"]) / risk < rule["activate_r"]):
                continue
            if _activate_native(ex, t, report, "такт", px=px):
                active.add(sid)
        except Exception as e:                          # noqa: BLE001 — фолбэк: почасовой трейл
            stamp = _TRAIL_WARNED.setdefault(sid, {"at": None})
            if _alert_due(stamp, _t.monotonic()):
                stamp["at"] = _t.monotonic()
                report.append(f"⚠️ #{sid} {sym}: нативный трейл не встал — "
                              f"{type(e).__name__}: {str(e)[:100]}")
        finally:
            lock.release()
    return active


def _cancel_native_trail(ex, t) -> None:
    """Снять нативный трейл, если позиция закрывается иначе (журнал/аварийный стоп):
    reduce-only сирота съел бы долю СЛЕДУЮЩЕГО сигнала того же символа/стороны. Метка-
    захват без id (ответ create потерян) — трейл ищем в листинге открытых ордеров."""
    oid = t["trail_order_id"] if "trail_order_id" in t.keys() else None
    if not oid:
        return
    if str(oid).startswith(_PENDING_TRAIL):
        o = _find_trail_order(ex, t)
        oid = o.get("id") if o else None
    _cancel_stop(ex, t["symbol"], oid)


def _cancel_stop(ex, symbol: str, oid) -> None:
    """Снять старый стоп после постановки нового; уже исполнен/снят — не ошибка."""
    if not oid:
        return
    try:
        ex.cancel_order(oid, symbol)
    except Exception as e:                              # noqa: BLE001
        if "not exist" not in str(e).lower() and "80016" not in str(e):
            raise


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
            # версия = код, при котором ОТКРЫЛАСЬ позиция: он же ведёт её выход
            db.execute("UPDATE executor_orders SET execution_version=? WHERE signal_id=?",
                       (EXECUTION_VERSION, t["signal_id"]))
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


_ORPHAN_ALERT: dict = {"at": None}


def _match_orphans(positions: list, tracked: list, tol: float = 0.02) -> list:
    """Позиции биржи, не покрытые учётом executor (placing/placed/filled того же
    символа/стороны). Несколько сигналов одного символа/стороны сливаются в одну
    позицию — сравниваем биржевой объём с СУММОЙ учётного; излишек сверх допуска —
    сирота. -> [{symbol, side, qty, entry, upnl}] (чистая функция, тестируется)."""
    acct: dict = {}
    for t in tracked:
        key = (t["symbol"], t["side"])
        acct[key] = acct.get(key, 0.0) + float(t["qty"] or 0.0)
    out = []
    for p in positions:
        qty = float(p.get("contracts") or 0.0)
        if qty <= 0:
            continue
        side = (p.get("side") or "").upper()
        excess = qty - acct.get((p["symbol"], side), 0.0)
        if excess > qty * tol:
            out.append({"symbol": p["symbol"], "side": side, "qty": round(excess, 8),
                        "entry": p.get("entryPrice"), "upnl": p.get("unrealizedPnl")})
    return out


def orphan_positions(ex) -> list:
    tracked = db.query("SELECT symbol, side, qty FROM executor_orders "
                       "WHERE state IN ('placing','placed','filled')")
    return _match_orphans(ex.fetch_positions(), tracked)


def _orphan_line(o: dict) -> str:
    try:
        upnl = float(o.get("upnl") or 0.0)
    except (TypeError, ValueError):
        upnl = 0.0
    return f"{o['symbol']} {o['side']} {o['qty']:g} @ {o.get('entry')} (uPnL {upnl:+.0f} VST)"


def _orphan_lines(ex) -> list[str]:
    orphans = orphan_positions(ex)
    if not orphans:
        return []
    return ["👻 сирота (позиция без учёта): " + _orphan_line(o) for o in orphans] + \
        ["→ /executor sweep закроет их по рынку"]


def sweep_orphans(close: bool = False) -> list[str]:
    """Сироты: позиции биржи без учёта executor. Наследие детектора филлов до 31.08:
    журнал закрывал сделку, executor «снимал» невидимый ему филл, позиция оставалась
    без хозяина (HYPE #327 и ACE #330 висели 20 дней). close=False — только список;
    True — закрыть по рынку (reduceOnly, hedged); зовётся ТОЛЬКО админ-командой."""
    if not os.environ.get("BINGX_API_KEY"):
        return ["нет ключей BingX"]
    ex = _exchange()
    orphans = orphan_positions(ex)
    if not orphans:
        return ["сирот нет — все позиции биржи под учётом"]
    out = []
    for o in orphans:
        line = _orphan_line(o)
        if not close:
            out.append("👻 " + line)
            continue
        side = "sell" if o["side"] == "LONG" else "buy"
        try:
            ex.create_order(o["symbol"], "market", side, o["qty"], None,
                            params={"hedged": True, "reduceOnly": True})
            out.append("📤 закрыта сирота: " + line)
        except Exception as e:                          # noqa: BLE001 — одна не роняет остальные
            out.append(f"⚠️ не закрылась: {line} — {str(e)[:100]}")
    if not close:
        out.append("→ /executor sweep закроет их по рынку")
    return out


# начало эпохи входа TRIGGER_MARKET (коммит 6eac140, первый ордер #434 02.09 11:10 UTC);
# до этого вход шёл лимиткой и филл совпадал с уровнем — в замер слипа не смешиваем
ENTRY_MODEL_SINCE = "2026-09-02T11:05:00+00:00"
# правило зарегистрировано 04.10.2026 ДО накопления выборки
# (docs/measurements/2026-10-04-entry-slip-rule.md)
SLIP_RULE_MIN_N = 50
SLIP_RULE_MEAN_R = -0.10


def entry_slip_r(r) -> float | None:
    """Слип входа в R от дистанции стопа (+ = вошли лучше журнального entry)."""
    risk = abs(r["entry_px"] - r["stop_px"]) if r["entry_px"] and r["stop_px"] else 0.0
    if not r["fill_px"] or not risk:
        return None
    sign = 1.0 if r["side"] == "LONG" else -1.0
    return sign * (r["entry_px"] - r["fill_px"]) / risk


def slip_rule_verdict(slips: list[float]) -> tuple[int, float, bool]:
    """-> (n, средний слип R, правило сработало). Срабатывает только на полной выборке."""
    n = len(slips)
    mean = round(sum(slips) / n, 6) if n else 0.0     # округление: порог включительно
    return n, mean, n >= SLIP_RULE_MIN_N and mean <= SLIP_RULE_MEAN_R


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
    # «филл в журнале раньше постановки» — не ошибка исполнения, а сделка журнала, которую
    # executor не мог повторить (ордера ещё не было). Раньше шла строкой ⚠️ среди ошибок
    # и молча выпадала из пар (аудит 30.10, #698/#704) — теперь отдельной категорией с R
    prefill = [r for r in rows if r["state"] == "error"
               and (r["note"] or "").startswith(PREFILL_NOTE)]
    prefill_ids = {r["signal_id"] for r in prefill}
    errors = [r for r in rows if r["state"] == "error" and r["signal_id"] not in prefill_ids]
    cancelled = sum(1 for r in rows if r["state"] == "cancelled")

    lines = ["🤖 <b>Executor: исполнение vs журнал (неделя)</b>",
             f"Ордеров: {placed} · входов: {len(filled)} · закрыто: {len(closed)} · "
             f"снято без филла: {cancelled} · ошибок: {len(errors)}"]
    j_sum = x_sum = 0.0
    n_pairs = 0
    slips_out = []
    by_ver: dict = {}
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
                v = by_ver.setdefault(r["execution_version"] or "?", [0, 0.0, 0.0])
                v[0] += 1
                v[1] += j_r
                v[2] += x_r
            if r["exit_price"]:
                # + = вышли ЛУЧШЕ журнала, − = хуже (в R от дистанции стопа)
                slips_out.append(sign * (r["close_px"] - r["exit_price"]) / risk)
    if n_pairs:
        lines.append(f"Закрытые зеркальные ({n_pairs}): журнал {j_sum:+.2f}R · "
                     f"демо {x_sum:+.2f}R · разрыв {x_sum - j_sum:+.2f}R")
        if len(by_ver) > 1:
            # механика выхода менялась внутри недели — разрыв по версиям, не вперемешку
            lines.append("по версиям исполнения: " + "; ".join(
                f"{k} n={n}: разрыв {x - j:+.2f}R" for k, (n, j, x) in sorted(by_ver.items())))
    if prefill:
        pj = sum(r["result_r"] for r in prefill if r["result_r"] is not None)
        lines.append(f"Не исполнимо (журнал засчитал вход раньше, чем ордер успел встать): "
                     f"{len(prefill)} · журнал {pj:+.2f}R — в пары не входят: "
                     + ", ".join(f"#{r['signal_id']}" for r in prefill[:6]))
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
    # накопительно с перехода на триггер-маркет — решающее правило по модели входа
    cum = [x for r in db.query("SELECT * FROM executor_orders WHERE fill_px IS NOT NULL "
                               "AND created_at >= ?", (ENTRY_MODEL_SINCE,))
           if (x := entry_slip_r(r)) is not None]
    if cum:
        n, mean, hit = slip_rule_verdict(cum)
        lines.append(f"Слип входа накопительно (триггер-маркет с 02.09): {mean:+.3f}R, "
                     f"n={n}/{SLIP_RULE_MIN_N}")
        if hit:
            lines.append(f"⚠️ ПРАВИЛО СРАБОТАЛО: средний слип ≤ {SLIP_RULE_MEAN_R:+.2f}R на "
                         f"{n} входах — модель входа съедает эдж, пересмотреть (замер по "
                         "docs/measurements/2026-10-04-entry-slip-rule.md)")
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
           "risk_pct": runtime.executor_risk_pct(), "orders": counts, "vst": None,
           "loss_limit_pct": runtime.executor_daily_loss_pct(),
           "halted_today": runtime.executor_halt_day() == clock.utcnow_iso()[:10]}
    if out["enabled"]:
        try:
            ex = _exchange()
            out["vst"] = round(float(ex.fetch_balance().get("total", {}).get("VST", 0)), 2)
        except Exception as e:                          # noqa: BLE001
            out["vst"] = f"ошибка: {str(e)[:80]}"
    return out
