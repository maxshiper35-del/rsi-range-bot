"""
journal.py — источник правды публичной статистики.

Пишет сигналы, автоматически фиксирует исход (TP/SL) по цене биржи, считает
winrate / PnL в R. Журнал неизменяем: исходы пишутся только в append-таблицу
signal_events; entry/stop/take_profits/created_at не редактируются.
"""
from __future__ import annotations

import json
from datetime import timedelta

from app import clock, db
from app.core.signal_engine import Side, Signal

_TP_COLS = ("tp1", "tp2", "tp3")

# Окно ожидания исполнения лимитки — В ЧАСАХ, не барах: смена ТФ скоринга не должна менять
# окно (инцидент 2026-07-13). ЕДИНЫЙ источник для живого судьи и бэктеста (backtest.py
# конвертирует в бары своего ТФ) — расхождение живого и исследовательского окна = drift.
MAX_WAIT_HOURS = 50.0


def unfilled_open_signals(symbol: str) -> list:
    """Открытые сигналы символа без зафиксированного филла (для реалтайм-стрима)."""
    return db.query("SELECT * FROM signals WHERE symbol=? AND status='open' "
                    "AND result_r IS NULL AND filled_at IS NULL", (symbol,))


def symbols_with_open_positions() -> list:
    """Символы с неразрешёнными сигналами (open без исхода либо rejected-тень).

    Нужно, чтобы доскорить исход даже когда символ выпал из вотчлиста (авто-ротация
    или ручное удаление): иначе open-сигнал висит вечно, исход теряется навсегда,
    а публичная статистика недосчитывает закрытых сделок (винрейт/матожидание врут)."""
    rows = db.query("SELECT DISTINCT symbol FROM signals "
                    "WHERE (status='open' AND result_r IS NULL) "
                    "OR (status='rejected' AND result_r IS NULL AND closed_at IS NULL)")
    return [r["symbol"] for r in rows]


# запас сверх контрактного потолка жизни сигнала, прежде чем считать судью застрявшим
OVERDUE_SLACK_HOURS = 12.0
# потолок для сигналов без правила удержания (старые снапшоты / max_hold=0)
OVERDUE_NO_CAP_HOURS = 30 * 24.0


def overdue_open_signals(now_iso: str) -> list[dict]:
    """Открытые сигналы, которые по контракту ОБЯЗАНЫ были разрешиться: старше
    закрытия решающего бара + окна ожидания филла + потолка удержания + запаса.

    Такой сигнал значит, что судья его не видит (пауза без скоринга 13.09–04.10, символ
    пропал с биржи данных, сбой фетча): дашборд копит «плавающий» R по давно закрытой
    сделке, executor держит учёт. -> [{id, symbol, side, timeframe, over_h}]"""
    from datetime import datetime

    from app.brain.smc_setups import SMCEngine

    now = datetime.fromisoformat(now_iso)
    out = []
    for row in db.query("SELECT * FROM signals WHERE status='open' AND result_r IS NULL"):
        try:
            created = datetime.fromisoformat(row["created_at"])
        except (TypeError, ValueError):
            continue
        trail = _trail_of(row)
        hold_h = float(trail["max_hold_hours"]) if trail else 0.0
        tf_h = SMCEngine.TF_MINUTES.get(row["timeframe"], 60) / 60.0
        life_h = (tf_h + MAX_WAIT_HOURS + hold_h if hold_h > 0 else OVERDUE_NO_CAP_HOURS)
        age_h = (now - created).total_seconds() / 3600.0
        over = age_h - life_h - OVERDUE_SLACK_HOURS
        if over > 0:
            out.append({"id": row["id"], "symbol": row["symbol"], "side": row["side"],
                        "timeframe": row["timeframe"], "over_h": round(over, 1)})
    return out


def try_mark_filled(signal_id: int, iso_ts: str) -> bool:
    """Атомарно зафиксировать филл (guard filled_at IS NULL) — гонку стрим vs
    bar-скоринг выигрывает ровно один, второй молчит. -> True если выиграли."""
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE signals SET filled_at=?, updated_at=? WHERE id=? AND filled_at IS NULL",
            (iso_ts, clock.utcnow_iso(), signal_id))
        return cur.rowcount == 1


def signal_row(signal_id: int):
    """Строка signals по id (или None) — для разовой покупки/переформатирования."""
    return db.query_one("SELECT * FROM signals WHERE id=?", (signal_id,))


def row_to_signal(row) -> Signal:
    """Восстановить Signal из строки signals (для алертов/переформатирования)."""
    return Signal(
        symbol=row["symbol"],
        side=Side(row["side"]),
        timeframe=row["timeframe"],
        entry=row["entry"],
        stop=row["stop"],
        take_profits=[row[c] for c in _TP_COLS if row[c] is not None],
        risk_reward=row["risk_reward"],
        confidence=row["confidence"],
        reasons=json.loads(row["reasons_json"] or "[]"),
        created_at=row["created_at"],
        indicators_snapshot=json.loads(row["snapshot_json"] or "{}"),
    )


def _trail_rule() -> dict | None:
    """Правило трейлинга на МОМЕНТ СОЗДАНИЯ сигнала -> {'activate_r','distance_r'} | None.

    Пишется в snapshot сигнала и оттуда же читается судьёй. Так смена настройки НИКОГДА
    не переоценивает уже опубликованные сделки: старые сигналы (без ключа 'trail')
    судятся ровно так, как их видел подписчик — фиксированный тейк и неподвижный стоп.
    Это прямое требование контракта честности: журнал неизменяем."""
    from app import runtime

    act = runtime.trail_activate_r()
    if act <= 0:
        return None
    return {"activate_r": act, "distance_r": runtime.trail_distance_r(),
            "tp_exit": not runtime.trail_only_exit(),
            "max_hold_hours": runtime.trail_max_hold_hours()}


def _insert(signal: Signal, status: str, event: str) -> int:
    """Общая вставка сигнала в заданном статусе + начальное событие. -> signal_id."""
    tps = list(signal.take_profits) + [None, None, None]
    now = clock.utcnow_iso()
    snapshot = dict(signal.indicators_snapshot) \
        if isinstance(signal.indicators_snapshot, dict) else {}
    trail = _trail_rule()
    if trail is not None:
        snapshot["trail"] = trail
    exchange = snapshot.get("exchange", "bybit")
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO signals(symbol, side, timeframe, entry, stop, tp1, tp2, tp3, "
            "risk_reward, confidence, reasons_json, snapshot_json, exchange, status, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (signal.symbol, signal.side.value, signal.timeframe, signal.entry, signal.stop,
             tps[0], tps[1], tps[2], signal.risk_reward, signal.confidence,
             json.dumps(signal.reasons, ensure_ascii=False),
             json.dumps(snapshot, ensure_ascii=False),
             exchange, status, signal.created_at, now),
        )
        sid = cur.lastrowid
        conn.execute(
            "INSERT INTO signal_events(signal_id, event, price, r_at_event, created_at) "
            "VALUES (?,?,?,0,?)",
            (sid, event, signal.entry, now),
        )
    return sid


def record_signal(signal: Signal) -> int:
    """Авто-режим: записать сигнал сразу активным (status=open) + событие 'opened'."""
    return _insert(signal, "open", "opened")


def find_duplicate_setup(signal: Signal) -> int | None:
    """id уже записанного НЕ-истёкшего сигнала того же сетапа, если есть.

    Один импульс детектится на нескольких последовательных барах. Стоп (якорь S —
    снятый экстремум) при этом СТАБИЛЕН, а вход (фибо от БЕГУЩЕГО экстремума E)
    ДРЕЙФУЕТ каждый бар. Дедуп по точному (entry,stop) промахивался на тренде и плодил
    дубли открытых сигналов -> раздувал число сделок и искажал винрейт (аудит [2]).
    Дедупим по (symbol, timeframe, side, stop, setup) среди не-expired — стоп устойчив
    к дрейфу входа. ПЛЮС: пока АКТИВЕН сигнал того же (symbol, tf, side, setup), новый не
    создаётся ДАЖЕ с другим стопом — якорь S сам дрейфует к новым подтверждённым свингам
    того же движения, и точный матч по стопу пропускал каскады дублей (аудит-2 стратегии
    [3]: 4×6226 на WLD одним движением; 5×LTF-BB на BTC в день −3R). Закрытый/истёкший
    сигнал не блокирует честный повторный сетап позже.
    Тег сетапа сверяем в Python (не json_extract) ради переносимости.
    """
    setup = signal.indicators_snapshot.get("setup", "") \
        if isinstance(signal.indicators_snapshot, dict) else ""
    rows = db.query(
        "SELECT id, snapshot_json FROM signals WHERE symbol=? AND timeframe=? AND side=? "
        "AND ((stop=? AND status != 'expired') "
        "OR (status IN ('open','candidate') AND result_r IS NULL)) ORDER BY id DESC",
        (signal.symbol, signal.timeframe, signal.side.value, signal.stop))
    for r in rows:
        if _setup_of(r) == setup:
            return r["id"]
    return None


def record_candidate(signal: Signal) -> int:
    """Полуавтомат: записать КАНДИДАТА (status=candidate) — ждёт подтверждения контекста."""
    return _insert(signal, "candidate", "candidate")


def _resolve_candidate(signal_id: int, status: str, event: str) -> bool:
    """Атомарно: условный UPDATE (WHERE status='candidate') + событие только при rowcount==1.

    Проверка и запись — в одной транзакции, поэтому конкурирующие /confirm и /reject
    не могут оба «выиграть» (TOCTOU закрыт): второй получит rowcount=0 -> False.
    """
    now = clock.utcnow_iso()
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE signals SET status=?, updated_at=? WHERE id=? AND status='candidate'",
            (status, now, signal_id))
        if cur.rowcount != 1:
            return False
        conn.execute("INSERT INTO signal_events(signal_id, event, created_at) VALUES (?,?,?)",
                     (signal_id, event, now))
    return True


def confirm_candidate(signal_id: int) -> bool:
    """Оператор подтвердил контекст: candidate -> open (идёт в рассылку/мониторинг)."""
    return _resolve_candidate(signal_id, "open", "confirmed")


def reject_candidate(signal_id: int) -> bool:
    """Оператор отклонил (контекст против): candidate -> rejected."""
    return _resolve_candidate(signal_id, "rejected", "rejected")


def open_candidates() -> list:
    """Список кандидатов, ожидающих решения оператора."""
    return db.query("SELECT * FROM signals WHERE status='candidate' ORDER BY id")


def expire_stale_candidates(max_age_minutes: int = 60) -> list:
    """TTL для semiauto-кандидатов: без /confirm или /reject дольше max_age_minutes ->
    candidate -> expired. Готовится ВПРОК (semiauto сейчас выключен, signal_mode=auto) —
    без TTL включение semiauto копило бы неразрешённых кандидатов вечно, искажая /report
    (cohort_stats). result_r остаётся NULL -> в публичную статистику (stats/stats_by_setup,
    фильтр result_r IS NOT NULL) не попадают. Событие 'expired' переиспользуем из уже
    разрешённого CHECK(event IN (...)) — тот же смысл (сделки не случилось), не заводим
    новое значение ради одной лишней миграции.
    -> список id просроченных кандидатов."""
    cutoff = (clock.utcnow() - timedelta(minutes=max_age_minutes)).isoformat()
    rows = db.query("SELECT id FROM signals WHERE status='candidate' AND created_at < ?", (cutoff,))
    ids = [r["id"] for r in rows]
    if not ids:
        return ids
    now = clock.utcnow_iso()
    with db.transaction() as conn:
        for sid in ids:
            conn.execute("UPDATE signals SET status='expired', updated_at=? WHERE id=?", (now, sid))
            conn.execute("INSERT INTO signal_events(signal_id, event, created_at) VALUES (?,?,?)",
                         (sid, "expired", now))
    return ids


def record_ledger_hash(row_count: int, sha256: str) -> None:
    """Локальный архив хэша опубликованного трек-рекорда — переживает удаление/блокировку
    канала (лежит в уже забэкапленной БД, не только в Telegram)."""
    db.execute("INSERT INTO ledger_hashes(row_count, sha256, created_at) VALUES (?,?,?)",
              (row_count, sha256, clock.utcnow_iso()))


def export_public_trades() -> list:
    """Полный публичный журнал для /trackrecord и еженедельного экспорта: и открытые, и
    закрытые сигналы, БЕЗ candidate/rejected (это отдельный когортный эксперимент /report,
    не публичный трек-рекорд — сознательно другая выборка, чтобы не путать читателя CSV).

    Исключаются и TTL-истёкшие кандидаты (status='expired', но никогда не подтверждались
    и не публиковались — аудит-2 математики [8]). ORDER BY created_at, id: у created_at
    массовые совпадения (один бар), без вторичного ключа порядок строк при равных ключах
    не гарантирован SQLite — два экспорта одинаковых данных могли бы дать разные sha256.

    Уровни ОТКРЫТЫХ сигналов (вход/стоп/тейки) маскируются (аудит 02.09): это платный
    контент — CSV доступен любому фри-юзеру и каналу, открытая строка была обходом
    подписки. Строка присутствует сразу (факт сигнала фиксируется), уровни появляются
    после закрытия — проверяемость исходов не страдает."""
    return db.query(
        "SELECT id, symbol, side, timeframe, "
        "CASE WHEN status='open' THEN NULL ELSE entry END AS entry, "
        "CASE WHEN status='open' THEN NULL ELSE stop END AS stop, "
        "CASE WHEN status='open' THEN NULL ELSE tp1 END AS tp1, "
        "CASE WHEN status='open' THEN NULL ELSE tp2 END AS tp2, "
        "CASE WHEN status='open' THEN NULL ELSE tp3 END AS tp3, "
        "status, result_r, "
        "created_at, closed_at FROM signals WHERE status NOT IN ('candidate','rejected') "
        "AND NOT (status='expired' "
        "AND EXISTS (SELECT 1 FROM signal_events e WHERE e.signal_id = signals.id "
        "AND e.event = 'candidate') "
        "AND NOT EXISTS (SELECT 1 FROM signal_events c WHERE c.signal_id = signals.id "
        "AND c.event = 'confirmed')) "
        "ORDER BY created_at, id")


def open_signals_brief() -> list:
    """Открытые для ПУБЛИЧНЫХ поверхностей (табло канала): символ/сторона/ТФ/время —
    БЕЗ уровней и сетапов, та же граница публикации, что у дашборда."""
    return [dict(r) for r in db.query(
        "SELECT symbol, side, timeframe, created_at, filled_at FROM signals "
        "WHERE status='open' AND result_r IS NULL ORDER BY created_at DESC")]


def open_side_counts() -> dict:
    """Активные сигналы по направлениям (open, включая ждущие лимитки — обе несут риск):
    -> {"LONG": n, "SHORT": m}. Для портфельного лимита направления и /status."""
    out = {"LONG": 0, "SHORT": 0}
    for r in db.query("SELECT side, COUNT(*) AS n FROM signals "
                      "WHERE status='open' AND result_r IS NULL GROUP BY side"):
        out[r["side"]] = r["n"]
    return out


def recent_side_stops(side: str, hours: float) -> int:
    """Стопы направления за скользящее окно N часов (по closed_at, все символы) — для
    анти-кластерного тормоза. Серия стопов одной стороны = рынок гонит тренд против
    ретрейсмент-входов (17.07: 8 стопов LONG за 6ч на сползании BTC −3.5%).
    Теневые исходы semiauto-rejected не считаются: их статус остаётся 'rejected'.

    ЗНАК result_r НЕ УЧИТЫВАЕТСЯ СОЗНАТЕЛЬНО. При схеме «выход только по трейлингу»
    (trail_only_exit, с 25.07.2026) status='stopped' стоит и у ПРИБЫЛЬНОЙ фиксации по
    подтянутому стопу — вживую это треть таких выходов (8 из 25 с 25.07.2026). Логика
    подсказывает их не считать: прибыль ≠ «рынок против входов». Портфельный гейт
    09.08.2026 говорит обратное (9 символов, 1h+4h, 2021-07..2026-07, конфиг в паритете
    с продом: 6226+LTF, min_stop_pct 1.3, трейлинг 1.5/0.2 без фикс. тейка, cap 6,
    тормоз 3/6ч, мейкер-вход/тейкер-выход, фандинг 0.125 bps/бар):

      правило                  5 лет: exp / Σ / DD        OOS 1г: exp / Σ / DD
      любой стоп (осталось)    +0.196R / +1048R / 32.2R   +0.222R / +283R / 21.8R
      только result_r<0        +0.189R / +1025R / 41.0R   +0.215R / +278R / 25.4R
      тормоз выключен          +0.193R / +1072R / 35.7R   +0.222R / +293R / 29.6R

    Хуже на ОБЕИХ выборках при БОЕВОМ окне 6ч — и при пороге 2, и при 3, и при 4 (6
    сравнений из 6); сильнее всего страдает просадка (+27% на 5 годах). Более того, на
    5 годах фильтр по знаку хуже ПОЛНОСТЬЮ ВЫКЛЮЧЕННОГО тормоза и по матожиданию, и по
    просадке: полумера ломает ограничитель, не давая ничего взамен. При окне 12ч знак
    сравнения переворачивается (там фильтр лучше на обеих выборках) — то есть дело не в
    «правильности» знака, а в силе торможения. Механизм, судя по замеру: кластер выходов
    ОДНОЙ стороны — признак выдохшегося движения независимо от знака, а тормоз работает
    ограничителем темпа перезахода, а не детектором убытков. Перепроверить:
    simulate_portfolio(cooldown_losses_only=True) / run_portfolio --cooldown-losses-only."""
    cutoff = (clock.utcnow() - timedelta(hours=hours)).isoformat()
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM signals WHERE side=? AND status='stopped' "
        "AND closed_at >= ?", (side, cutoff))
    return int(row["n"]) if row else 0


def oldest_open_anchor(symbol: str) -> str | None:
    """Самая старая точка (филл или создание) нескорённых сигналов символа — поллер по ней
    выбирает ГЛУБИНУ фетча баров: обрезанное окно даёт слепые зоны, где пропущенный стоп
    флипает исход (аудит-2 математики [5]). -> ISO | None (нет нескорённых)."""
    row = db.query_one(
        "SELECT MIN(COALESCE(filled_at, created_at)) AS a FROM signals WHERE symbol=? "
        "AND ((status='open' AND result_r IS NULL) "
        "OR (status='rejected' AND result_r IS NULL AND closed_at IS NULL))", (symbol,))
    return row["a"] if row else None


def _outcome_for(row, price: float):
    """Определить исход открытого сигнала по текущей цене. -> (status, event, result_r) | None."""
    entry, stop, side = row["entry"], row["stop"], row["side"]
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    if side == "LONG":
        if price <= stop:
            return "stopped", "stopped", -1.0
        reached = [(c, row[c]) for c in _TP_COLS if row[c] is not None and price >= row[c]]
        if reached:
            col, tp = reached[-1]
            return col, col, (tp - entry) / risk
    else:  # SHORT
        if price >= stop:
            return "stopped", "stopped", -1.0
        reached = [(c, row[c]) for c in _TP_COLS if row[c] is not None and price <= row[c]]
        if reached:
            col, tp = reached[-1]
            return col, col, (entry - tp) / risk
    return None


def update_outcomes(latest_prices: dict[str, float]) -> int:
    """УПРОЩЁННЫЙ скоринг по снапшоту последней цены (юнит-тесты/ручная проверка).

    НЕ учитывает исполнение лимитного входа и путь цены между прогонами —
    в проде использовать update_outcomes_from_bars (честный скоринг по барам).

    Активные (open): статус -> tp*/stopped, идут в публичную статистику.
    Отклонённые (rejected): считаем ТЕНЕВОЙ исход (counterfactual) — пишем result_r/closed_at
    и событие 'shadow', но статус ОСТАВЛЯЕМ 'rejected' (в публичную стату не попадают).
    Это данные для когортного отчёта confirmed vs rejected.
    """
    closed = 0
    rows = db.query("SELECT * FROM signals WHERE status='open' "
                    "OR (status='rejected' AND result_r IS NULL)")
    for row in rows:
        price = latest_prices.get(row["symbol"])
        if price is None:
            continue
        res = _outcome_for(row, float(price))
        if res is None:
            continue
        status, event, result_r = res
        now = clock.utcnow_iso()
        shadow = row["status"] == "rejected"
        with db.transaction() as conn:
            conn.execute(
                "UPDATE signals SET status=?, result_r=?, closed_at=?, updated_at=? WHERE id=?",
                (row["status"] if shadow else status, round(result_r, 4), now, now, row["id"]),
            )
            conn.execute(
                "INSERT INTO signal_events(signal_id, event, price, r_at_event, created_at) "
                "VALUES (?,?,?,?,?)",
                (row["id"], "shadow" if shadow else event,
                 float(price), round(result_r, 4), now),
            )
        closed += 1
    return closed


def _outcome_from_bars(row, df) -> tuple | None:
    """Исход по ХРОНОЛОГИИ закрытых баров ПОСЛЕ ЗАКРЫТИЯ решающего бара (честный скоринг).

    Фазы: (1) ждём касания entry (лимитный вход, low<=entry<=high) — без fill сделки НЕТ;
    просрочка окна MAX_WAIT_HOURS проверяется ДО касания (тач после окна — ордер уже снят).
    (2) после fill: стоп и тейк по high/low каждого бара; оба в одном баре — консервативно
    СТОП; на САМОМ баре филла тейк НЕ присуждается (high/low не говорят, была ли цена у
    тейка ДО или ПОСЛЕ исполнения лимитки — ретрейсный лимит почти всегда касается
    тейк-стороны до филла; аудит-2 математики [2]), стоп на баре филла — присуждается.
    -> (status, event, result_r|None, exit_price|None, fill_dt_iso|None, exit_dt_iso|None)
       | None (ждём fill / баров мало / окно не покрывает филл).
       status='open' — вход исполнен, развязки ещё нет.
    exit_dt — время БАРА выхода (для expired — момент снятия ордера), а не момент такта
    поллера: закрытые сделки скоупятся по closed_at (тормоз стороны, эквити, карточка
    недели), и после простоя бота пачка старых исходов иначе схлопывалась в одну точку
    «сейчас» — 09.08.2026 это дало призрачный кластер из 5 стопов LONG «за 6 часов»
    и заблокировало сторону на полдня на основании сделок девятидневной давности.
    """
    import pandas as pd

    from app.brain.smc_setups import SMCEngine

    entry, stop, side = row["entry"], row["stop"], row["side"]
    risk = abs(entry - stop)
    if risk <= 0 or "dt" not in df.columns:
        return None
    tps = [(c, row[c]) for c in _TP_COLS if row[c] is not None]
    # правило трейлинга — из СНАПШОТА сигнала (что видел подписчик), не из текущих настроек
    trail = _trail_of(row)
    # tp_exit=False -> тейк опубликован как ОРИЕНТИР и позицию не закрывает: выход только
    # по стопу/трейлингу/потолку удержания. Нет правила трейлинга -> прежняя семантика.
    tp_exits = trail is None or trail["tp_exit"]
    max_hold_h = trail["max_hold_hours"] if trail else 0.0
    cur_stop = stop
    peak = entry
    dts = pd.to_datetime(df["dt"], utc=True)
    created = pd.to_datetime(row["created_at"], utc=True)
    # created_at хранит ОТКРЫТИЕ решающего бара (метка ccxt); сигнал реально существует с
    # его ЗАКРЫТИЯ. Скоринг идёт барами МЛАДШЕГО ТФ конфигурации — старый фильтр по created
    # впускал бары ИЗНУТРИ решающего бара старшего ТФ: филл/исход из цены, которую детектор
    # уже видел (ЛУКАХЕД; живой пример #126 — 4h-сигнал «исполнился» за 1ч45м до своего
    # рождения и ушёл в −1R в публичный журнал). Старт — строго с закрытия решающего бара.
    decision = created + pd.Timedelta(minutes=SMCEngine.TF_MINUTES.get(row["timeframe"], 60))

    tp_block_dt = None                               # dt бара филла: тейк на нём не присуждаем
    # если филл УЖЕ зафиксирован ранее (bar-скоринг или реалтайм-стрим) — НЕ перевыводим
    # его и НИКОГДА не истекаем (аудит 2026-07-13 [1]). Доскориваем с бара, СОДЕРЖАЩЕГО филл.
    persisted_fill = row["filled_at"] if "filled_at" in row.keys() else None
    if persisted_fill is not None:
        filled = True
        fill_dt = persisted_fill
        fill_ts = pd.to_datetime(persisted_fill, utc=True)
        at_or_before = dts[dts <= fill_ts]
        if at_or_before.empty:
            # окно баров НЕ покрывает филл: скоринг по обрезку пропустил бы стоп в дыре и
            # мог флипнуть исход (аудит-2 [5]) — ждём, поллер добирает глубину фетча
            return None
        bars = df.loc[dts >= at_or_before.iloc[-1]]
        if not bars.empty:
            tp_block_dt = pd.to_datetime(bars.iloc[0]["dt"], utc=True)
    else:
        filled = False
        fill_dt = None
        bars = df.loc[dts >= decision]
    if bars.empty:
        return None

    for _, b in bars.iterrows():
        hi, lo = float(b["high"]), float(b["low"])
        bar_dt = pd.to_datetime(b["dt"], utc=True)
        if not filled:
            if (bar_dt - decision).total_seconds() > MAX_WAIT_HOURS * 3600:
                # лимитка не исполнилась за окно — сделки не было (тач ПОСЛЕ окна не в счёт:
                # подписчик, следующий контракту, уже снял ордер — аудит-2 математики [7]).
                # Время снятия — КОНЕЦ окна, а не бар, на котором мы это заметили: момент
                # определён контрактом и не зависит от ТФ скоринга и глубины фетча.
                expire_dt = decision + pd.Timedelta(hours=MAX_WAIT_HOURS)
                return ("expired", "expired", None, None, None, expire_dt.isoformat())
            if not (lo <= entry <= hi):
                continue
            filled = True                            # вход исполнен на ЭТОМ баре
            fill_dt = bar_dt.isoformat()
            tp_block_dt = bar_dt
        if side == "LONG":
            if lo <= cur_stop:
                r_stop = (cur_stop - entry) / risk           # −1.0 либо плюс, если подтянут
                event = "stopped" if cur_stop == stop else "trail"
                return ("stopped", event, round(r_stop, 4), cur_stop, fill_dt,
                        bar_dt.isoformat())
            hit = ([] if bar_dt == tp_block_dt or not tp_exits
                   else [(c, tp) for c, tp in tps if hi >= tp])
        else:
            if hi >= cur_stop:
                r_stop = (entry - cur_stop) / risk
                event = "stopped" if cur_stop == stop else "trail"
                return ("stopped", event, round(r_stop, 4), cur_stop, fill_dt,
                        bar_dt.isoformat())
            hit = ([] if bar_dt == tp_block_dt or not tp_exits
                   else [(c, tp) for c, tp in tps if lo <= tp])
        if trail is not None and bar_dt != tp_block_dt:
            # подтяжка ПОСЛЕ проверки стопа этого бара: внутри бара порядок хай/лоу
            # неизвестен, поэтому новый уровень действует со СЛЕДУЮЩЕГО бара (тот же
            # принцип без-лукахеда, что и во всём скоринге).
            # На баре ФИЛЛА трейлинг не активируем по той же причине, по которой не
            # присуждаем на нём тейк: выгодный ход мог случиться ДО исполнения лимитки,
            # и подписчик его не получил бы.
            if side == "LONG":
                mfe_r = (hi - entry) / risk
                if mfe_r >= trail["activate_r"]:
                    peak = max(peak, hi)
                    cur_stop = max(cur_stop, peak - trail["distance_r"] * risk)
            else:
                mfe_r = (entry - lo) / risk
                if mfe_r >= trail["activate_r"]:
                    peak = min(peak, lo)
                    cur_stop = min(cur_stop, peak + trail["distance_r"] * risk)
        if hit:
            col, tp = hit[-1]                        # лучший достигнутый в этом баре тейк
            r = (tp - entry) / risk if side == "LONG" else (entry - tp) / risk
            return (col, col, r, tp, fill_dt, bar_dt.isoformat())
        if filled and max_hold_h > 0 and fill_dt is not None:
            held_h = (bar_dt - pd.to_datetime(fill_dt, utc=True)).total_seconds() / 3600.0
            if held_h >= max_hold_h:
                # потолок удержания: без фиксированного тейка позиция иначе висит неделями,
                # занимая слот направления и накапливая фандинг. Выход по ЗАКРЫТИЮ бара —
                # цена, которую подписчик реально мог получить рыночным ордером
                close_p = float(b["close"])
                r_now = ((close_p - entry) / risk if side == "LONG"
                         else (entry - close_p) / risk)
                return ("stopped", "timeout", round(r_now, 4), close_p, fill_dt,
                        bar_dt.isoformat())
    if filled:
        # вход есть, развязки нет — сообщить о филле
        return ("open", None, None, None, fill_dt, None)
    return None                                      # всё ещё ждём fill


def _trail_of(row) -> dict | None:
    """Правило трейлинга из snapshot_json сигнала (как он был опубликован) | None.
    Битый/старый снапшот -> None = прежняя семантика неподвижного стопа."""
    try:
        t = json.loads(row["snapshot_json"] or "{}").get("trail")
    except (ValueError, TypeError):
        return None
    if not isinstance(t, dict):
        return None
    try:
        act, dist = float(t["activate_r"]), float(t["distance_r"])
    except (KeyError, TypeError, ValueError):
        return None
    if act <= 0:
        return None
    try:
        max_hold = float(t.get("max_hold_hours") or 0.0)
    except (TypeError, ValueError):
        max_hold = 0.0
    # tp_exit отсутствует в снапшотах первой версии трейлинга -> True (тейк закрывал сделку)
    return {"activate_r": act, "distance_r": dist,
            "tp_exit": bool(t.get("tp_exit", True)), "max_hold_hours": max_hold}


def _setup_of(row) -> str:
    """Тег сетапа из snapshot_json ('6226'/'1:1'/'LTF-BB'/... либо '?')."""
    try:
        return json.loads(row["snapshot_json"] or "{}").get("setup", "?")
    except (ValueError, TypeError):
        return "?"


def _price_move_pct(side: str, entry: float, exit_price) -> float | None:
    """Движение цены в % в пользу сделки (без плеча): для SHORT знак инвертирован."""
    if exit_price is None or not entry:
        return None
    move = (exit_price - entry) / entry * 100.0
    return round(move if side == "LONG" else -move, 4)


def update_outcomes_from_bars(bars_by_symbol: dict) -> list:
    """Продовый скоринг исходов по OHLCV-барам (см. _outcome_from_bars).

    -> список событий {id, symbol, side, timeframe, status, result_r, shadow, setup,
       entry, exit_price, price_move_pct, filled_at}:
       status='filled' — лимитка исполнилась (не тень; сигнал остаётся open),
       остальные — терминальные исходы. Caller фильтрует shadow/expired/filled сам.
    Идемпотентен: исход скорится один раз (фильтр по result_r/closed_at),
    filled — один раз (фильтр по filled_at IS NULL).
    Для rejected пишется теневой исход (статус не меняется, событие 'shadow').
    """
    closed = []
    rows = db.query("SELECT * FROM signals WHERE (status='open' AND result_r IS NULL) "
                    "OR (status='rejected' AND result_r IS NULL AND closed_at IS NULL)")
    for row in rows:
        df = bars_by_symbol.get(row["symbol"])
        if df is None:
            continue
        res = _outcome_from_bars(row, df)
        if res is None:
            continue
        status, event, result_r, exit_price, fill_dt, exit_dt = res
        now = clock.utcnow_iso()
        shadow = row["status"] == "rejected"
        base = {"id": row["id"], "symbol": row["symbol"], "side": row["side"],
                "timeframe": row["timeframe"], "shadow": shadow,
                "setup": _setup_of(row), "entry": row["entry"], "filled_at": fill_dt}
        # первый раз увидели исполнение лимитки — персистим; уведомление — не для теней.
        # Гейт по rowcount: если реалтайм-стрим успел проставить filled_at между нашим
        # SELECT и этим UPDATE, событие «вход исполнен» уже ушло из стрима — второй пост
        # в канал был бы дублем (аудит-2 математики [6] / UX [9])
        if fill_dt is not None and row["filled_at"] is None:
            with db.transaction() as conn:
                cur = conn.execute("UPDATE signals SET filled_at=?, updated_at=? "
                                   "WHERE id=? AND filled_at IS NULL",
                                   (fill_dt, now, row["id"]))
                fill_won = cur.rowcount == 1
            if fill_won and not shadow:
                closed.append({**base, "status": "filled", "result_r": None,
                               "exit_price": None, "price_move_pct": None})
        if status == "open":
            continue                                 # вход есть, развязки ещё нет
        r_val = round(result_r, 4) if result_r is not None else None
        # closed_at — время БАРА выхода (домен), updated_at — момент такта (служебное).
        # Тот же принцип, что у filled_at. Фолбэк на now недостижим для терминальных
        # исходов (exit_dt отсутствует только у status='open', отсеянного выше) и стоит
        # лишь затем, чтобы колонка не могла остаться NULL.
        with db.transaction() as conn:
            conn.execute(
                "UPDATE signals SET status=?, result_r=?, closed_at=?, updated_at=?, "
                "exit_price=? WHERE id=?",
                (row["status"] if shadow else status, r_val, exit_dt or now, now,
                 exit_price, row["id"]),
            )
            conn.execute(
                "INSERT INTO signal_events(signal_id, event, price, r_at_event, created_at) "
                "VALUES (?,?,?,?,?)",
                (row["id"], ("shadow" if event != "expired" else "expired") if shadow else event,
                 exit_price, r_val, now),                # цена выхода — полный аудит-след
            )
        closed.append({**base, "status": status, "result_r": r_val, "exit_price": exit_price,
                       "closed_at": exit_dt or now,
                       "price_move_pct": _price_move_pct(row["side"], row["entry"], exit_price)})
    return closed


def cohort_stats() -> dict:
    """Сравнение когорт для форвард-эксперимента: подтверждённые vs отклонённые.

    confirmed — сигналы, прошедшие подтверждение (активные и закрытые);
    rejected — отклонённые с ТЕНЕВЫМ исходом. Метрики только по закрытым
    (result_r IS NOT NULL). edge_r = разница экспектанси confirmed − rejected —
    это и есть измеряемый вклад человека.
    """
    def agg(rows) -> dict:
        rs = [r["result_r"] for r in rows]
        n = len(rs)
        wins = sum(1 for r in rs if r > 0)
        return {"trades": n, "wins": wins,
                "winrate": round(wins / n, 4) if n else 0.0,
                "expectancy_r": round(sum(rs) / n, 4) if n else 0.0,
                "total_r": round(sum(rs), 4)}

    # членство в когорте — по факту РЕШЕНИЯ ОПЕРАТОРА (событию), а не по статусу:
    # сигналы record_signal (авто-режим) в эксперимент не входят.
    confirmed = db.query(
        "SELECT s.result_r FROM signals s "
        "WHERE s.result_r IS NOT NULL AND EXISTS (SELECT 1 FROM signal_events e "
        "WHERE e.signal_id = s.id AND e.event = 'confirmed')")
    rejected = db.query(
        "SELECT s.result_r FROM signals s "
        "WHERE s.result_r IS NOT NULL AND EXISTS (SELECT 1 FROM signal_events e "
        "WHERE e.signal_id = s.id AND e.event = 'rejected')")
    pending = db.query_one(
        "SELECT COUNT(*) AS c FROM signals WHERE status='candidate'")["c"]  # type: ignore[index]
    out = {"confirmed": agg(confirmed), "rejected": agg(rejected),
           "all": agg(list(confirmed) + list(rejected)), "pending_candidates": pending}
    out["edge_r"] = round(out["confirmed"]["expectancy_r"]
                          - out["rejected"]["expectancy_r"], 4)
    return out


def _split_moves(rows) -> tuple[list[float], list[float]]:
    """Ходы цены «в пользу сделки» по закрытым: (плюсовые, минусовые). Общий расчёт
    для stats()/stats_for_period()/stats_since_created() — «сколько чистого движения
    забрали» (фидбэк владельца 30.08). exit_price NULL (до миграции 014) -> уровень."""
    tp_moves: list[float] = []
    stop_moves: list[float] = []
    for r in rows:
        if r["status"] == "expired":
            continue                                     # сделки не было — хода нет
        exit_price = r["exit_price"] if r["exit_price"] is not None else (
            r["stop"] if r["status"] == "stopped" else r[r["status"]])
        mv = _price_move_pct(r["side"], r["entry"], exit_price)
        if mv is None:
            continue
        (tp_moves if (r["result_r"] or 0) > 0 else stop_moves).append(mv)
    return tp_moves, stop_moves


def stats() -> dict:
    """Публичные агрегаты для /stats: winrate, expectancy(R), суммарный PnL(R), счётчики,
    плюс ЧИСТОЕ движение цены (без плеча): средний ход в тейках и в стопах и суммарный % —
    чтобы R-результат читался и в процентах рынка («+1R ≈ +N% хода»)."""
    # только реально закрытые активные сделки; candidate/rejected (теневые) — не публичная стата
    rows = db.query(
        "SELECT side, entry, stop, tp1, tp2, tp3, status, result_r, exit_price FROM signals "
        "WHERE status IN ('tp1','tp2','tp3','stopped') AND result_r IS NOT NULL")
    rs = [r["result_r"] for r in rows]
    n = len(rs)
    wins = [r for r in rs if r > 0]
    total_r = sum(rs)
    tp_moves, stop_moves = _split_moves(rows)
    open_n = db.query_one(
        "SELECT COUNT(*) AS c FROM signals WHERE status='open'")["c"]  # type: ignore[index]
    return {
        "trades": n,
        "wins": len(wins),
        "losses": n - len(wins),
        "winrate": round(len(wins) / n, 4) if n else 0.0,
        "expectancy_r": round(total_r / n, 4) if n else 0.0,
        "total_r": round(total_r, 4),
        "open": open_n,
        "avg_tp_move_pct": round(sum(tp_moves) / len(tp_moves), 3) if tp_moves else 0.0,
        "avg_stop_move_pct": round(sum(stop_moves) / len(stop_moves), 3) if stop_moves else 0.0,
        "total_move_pct": round(sum(tp_moves) + sum(stop_moves), 2),
    }


def stats_since_created(start_iso: str) -> dict:
    """Агрегат ТОЛЬКО по сигналам, СОЗДАННЫМ с start_iso (эпоха обновления стратегии):
    легаси-бэклог доносится по старым правилам и не должен смешиваться с оценкой нового
    кода. Скоуп по created_at, не closed_at — важно, КОГДА сигнал родился."""
    rows = list(db.query(
        "SELECT side, entry, stop, tp1, tp2, tp3, status, result_r, exit_price "
        "FROM signals WHERE status IN ('tp1','tp2','tp3','stopped') "
        "AND result_r IS NOT NULL AND created_at >= ?", (start_iso,)))
    rs = [r["result_r"] for r in rows]
    n = len(rs)
    wins = sum(1 for r in rs if r > 0)
    total_r = sum(rs)
    open_n = db.query_one(
        "SELECT COUNT(*) AS c FROM signals WHERE status='open' AND created_at >= ?",
        (start_iso,))["c"]  # type: ignore[index]
    tp_moves, stop_moves = _split_moves(rows)
    return {"trades": n, "wins": wins, "losses": n - wins,
            "winrate": round(wins / n, 4) if n else 0.0,
            "expectancy_r": round(total_r / n, 4) if n else 0.0,
            "total_r": round(total_r, 4), "open": open_n,
            "total_move_pct": round(sum(tp_moves) + sum(stop_moves), 2)}


def recent_streak(n: int = 10) -> dict:
    """Серия из последних n закрытых сделок с исходом (по closed_at DESC) — для футера
    соц-пруфа под постами результата. Только реальные R-исходы (expired без R не в счёт).
    -> {n, sum_r, wins} (n — фактическое число, может быть меньше запрошенного)."""
    rows = db.query(
        "SELECT result_r FROM signals WHERE status IN ('tp1','tp2','tp3','stopped') "
        "AND result_r IS NOT NULL ORDER BY closed_at DESC LIMIT ?", (n,))
    rs = [r["result_r"] for r in rows]
    return {"n": len(rs), "sum_r": round(sum(rs), 2), "wins": sum(1 for r in rs if r > 0)}


def stats_for_period(start_iso: str, end_iso: str) -> dict:
    """Та же агрегация, что stats(), но СКОУПЛЕННАЯ по closed_at в [start_iso, end_iso) —
    для карточки недели (реальные цифры ЭТОЙ недели, не всё время жизни бота). Дополнительно
    отдаёт outcomes — хронологический список True/False (win/loss) для юнит-чарта.
    Даты/окно определяет вызывающий код (bot.py), не эта функция — она не читает часы."""
    rows = list(db.query(
        "SELECT side, entry, stop, tp1, tp2, tp3, status, result_r, exit_price "
        "FROM signals WHERE status IN ('tp1','tp2','tp3','stopped','expired') "
        "AND result_r IS NOT NULL AND closed_at >= ? AND closed_at < ? ORDER BY closed_at",
        (start_iso, end_iso)))
    rs = [r["result_r"] for r in rows]
    n = len(rs)
    wins = [r for r in rs if r > 0]
    total_r = sum(rs)
    tp_moves, stop_moves = _split_moves(rows)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": n - len(wins),
        "winrate": round(len(wins) / n, 4) if n else 0.0,
        "expectancy_r": round(total_r / n, 4) if n else 0.0,
        "total_r": round(total_r, 4),
        "outcomes": [r > 0 for r in rs],
        "total_move_pct": round(sum(tp_moves) + sum(stop_moves), 2),
    }


def equity_series() -> list:
    """Хронологический список result_r закрытых сделок (по closed_at) — для эквити-кривой
    дашборда. Тот же фильтр, что stats(): суммы совпадут с total_r до копейки."""
    rows = db.query(
        "SELECT result_r FROM signals WHERE status IN ('tp1','tp2','tp3','stopped','expired') "
        "AND result_r IS NOT NULL ORDER BY closed_at")
    return [r["result_r"] for r in rows]


def stats_by_symbol() -> dict:
    """Разбивка публичной статистики по символам — для живого табло в канале."""
    rows = db.query(
        "SELECT symbol, result_r FROM signals "
        "WHERE status IN ('tp1','tp2','tp3','stopped','expired') AND result_r IS NOT NULL")
    by_symbol: dict = {}
    for r in rows:
        rs = by_symbol.setdefault(r["symbol"], [])
        rs.append(r["result_r"])
    out = {}
    for symbol, rs in by_symbol.items():
        n = len(rs)
        wins = sum(1 for x in rs if x > 0)
        out[symbol] = {
            "trades": n,
            "wins": wins,
            "winrate": round(wins / n, 4) if n else 0.0,
            "expectancy_r": round(sum(rs) / n, 4) if n else 0.0,
            "total_r": round(sum(rs), 4),
        }
    return out


def stats_by_setup(since_created: str | None = None) -> dict:
    """Разбивка публичной статистики по тегу сетапа (6226/1:1/open-SMC/LTF-BB/LTF-OB) —
    для /bysetup и живого табло. Тег живёт в snapshot_json (JSON-поле, не колонка) —
    парсим в Python, не через json_extract в SQL (переносимость важнее при этом объёме).
    LTF-BB и LTF-OB НЕ схлопываются в один бакет — именно LTF-BB показал проблему
    в бэктесте/живой эксплуатации, LTF-OB отдельно не проверялся.
    since_created — только сигналы, созданные с этой даты (эпоха нового кода).
    """
    rows = db.query(
        "SELECT snapshot_json, result_r FROM signals WHERE status IN "
        "('tp1','tp2','tp3','stopped','expired') AND result_r IS NOT NULL "
        "AND created_at >= ?", (since_created or "",))
    by_setup: dict = {}
    for r in rows:
        try:
            setup = json.loads(r["snapshot_json"] or "{}").get("setup", "?")
        except (ValueError, TypeError):
            setup = "?"
        by_setup.setdefault(setup, []).append(r["result_r"])
    out = {}
    for setup, rs in by_setup.items():
        n = len(rs)
        wins = sum(1 for x in rs if x > 0)
        out[setup] = {
            "trades": n,
            "wins": wins,
            "winrate": round(wins / n, 4) if n else 0.0,
            "expectancy_r": round(sum(rs) / n, 4) if n else 0.0,
            "total_r": round(sum(rs), 4),
        }
    return out


def consecutive_losses(setup: str, n: int = 5) -> int:
    """Сколько последних ЗАКРЫТЫХ сделок этого сетапа подряд убыточны (result_r<=0),
    считая от самой свежей. Читает JSON-тег в Python по тем же причинам, что и
    stats_by_setup(); n ограничивает окно (не нужно сканировать всю историю)."""
    rows = db.query(
        "SELECT snapshot_json, result_r FROM signals WHERE status IN "
        "('tp1','tp2','tp3','stopped','expired') AND result_r IS NOT NULL "
        "ORDER BY closed_at DESC LIMIT ?", (max(n * 20, 100),))
    streak = 0
    for r in rows:
        try:
            tag = json.loads(r["snapshot_json"] or "{}").get("setup", "?")
        except (ValueError, TypeError):
            tag = "?"
        if tag != setup:
            continue
        if r["result_r"] <= 0:
            streak += 1
            if streak >= n:
                return streak
        else:
            break
    return streak


def signal_xray(sid: int) -> dict | None:
    """«Рентген сделки» — анатомия ЗАКРЫТОГО сигнала для deep-link /start xray_<sid>.

    Всё из БД, без походов на биржу (мгновенный ответ по клику из канала):
    строка сигнала + суб-скоры confidence из снапшота (пишутся с 11.08.2026, у старых
    сигналов их нет — рендер обязан это переживать) + путь трейлинга из signal_events
    (счётчик подтяжек и максимум зафиксированного R). Открытые/кандидаты -> None:
    рентген публикует уровни и механику, до закрытия это не для публики.
    """
    row = db.query_one("SELECT * FROM signals WHERE id=?", (sid,))
    if row is None or row["status"] not in ("tp1", "tp2", "tp3", "stopped", "expired"):
        return None
    if row["status"] == "expired":
        # тот же фильтр, что у export_public_trades: TTL-истёкший КАНДИДАТ (semiauto,
        # видел только оператор) никогда не публиковался — deep-link перебором id не
        # должен доставать приватную когорту, которой нет в публичном трек-рекорде
        was_candidate = db.query_one(
            "SELECT 1 AS x FROM signal_events WHERE signal_id=? AND event='candidate'", (sid,))
        confirmed = db.query_one(
            "SELECT 1 AS x FROM signal_events WHERE signal_id=? AND event='confirmed'", (sid,))
        if was_candidate and not confirmed:
            return None
    try:
        snapshot = json.loads(row["snapshot_json"] or "{}")
    except (ValueError, TypeError):
        snapshot = {}
    try:
        reasons = json.loads(row["reasons_json"] or "[]")
    except (ValueError, TypeError):
        reasons = []
    trail = db.query("SELECT price, r_at_event FROM signal_events "
                     "WHERE signal_id=? AND event='trail' ORDER BY id", (sid,))

    def _hours(a: str | None, b: str | None) -> float | None:
        if not a or not b:
            return None
        from datetime import datetime
        return round((datetime.fromisoformat(b) - datetime.fromisoformat(a))
                     .total_seconds() / 3600, 1)

    entry, stop = float(row["entry"]), float(row["stop"])
    return {
        "id": row["id"], "symbol": row["symbol"], "side": row["side"],
        "timeframe": row["timeframe"], "status": row["status"],
        "setup": snapshot.get("setup", "?"),
        "entry": entry, "stop": stop, "exit_price": row["exit_price"],
        "result_r": row["result_r"], "confidence": row["confidence"],
        "stop_pct": round(abs(entry - stop) / entry * 100, 2) if entry else None,
        "scores": snapshot.get("scores"),           # None у сигналов до 11.08.2026
        "swept": snapshot.get("swept"),
        "clean_impulse": snapshot.get("clean_impulse"),
        "reasons": reasons,
        "wait_hours": _hours(row["created_at"], row["filled_at"]),
        "hold_hours": _hours(row["filled_at"], row["closed_at"]),
        "trail_moves": len(trail),
        "trail_peak_r": max((float(t["r_at_event"]) for t in trail
                             if t["r_at_event"] is not None), default=None),
    }


def kelly_stats(since_created: str | None = None) -> dict | None:
    """Критерий Келли по закрытым сделкам журнала: f* = p − (1−p)/b, где b — средний
    плюс к среднему минусу (в R). Приближение (Келли писан под бинарную ставку,
    у нас распределение R) — потому наружу идёт ПОЛУ-Келли и только как справка.
    None: мало сделок (<20), нет плюсов/минусов или f*<=0 — Келли на шуме
    опаснее его отсутствия."""
    q = ("SELECT result_r FROM signals WHERE status IN ('tp1','tp2','tp3','stopped') "
         "AND result_r IS NOT NULL ")
    args: tuple = ()
    if since_created:
        q += "AND created_at >= ? "
        args = (since_created,)
    rs = [r["result_r"] for r in db.query(q, args)]
    n = len(rs)
    if n < 20:
        return None
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r < 0]
    if not wins or not losses:
        return None
    p = len(wins) / n
    b = (sum(wins) / len(wins)) / (sum(losses) / len(losses))
    f = p - (1 - p) / b
    if f <= 0:
        return None
    return {"full": round(f * 100, 1), "half": round(f * 50, 1),
            "wr": round(p * 100), "b": round(b, 2), "n": n}


def best_worst_closed(start_iso: str, end_iso: str) -> tuple[dict | None, dict | None]:
    """Лучшая и худшая закрытые сделки периода (по result_r) — «разбор недели».
    (None, None) при пустом периоде; одна сделка -> она же лучшая, худшей нет."""
    rows = [dict(r) for r in db.query(
        "SELECT * FROM signals WHERE status IN ('tp1','tp2','tp3','stopped') "
        "AND result_r IS NOT NULL AND closed_at >= ? AND closed_at < ? "
        "ORDER BY result_r DESC, id", (start_iso, end_iso))]
    if not rows:
        return None, None
    best = rows[0]
    worst = rows[-1] if len(rows) > 1 else None
    return best, worst
