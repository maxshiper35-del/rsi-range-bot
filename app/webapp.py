"""Mini App «Живой дашборд» — Flask-страница + JSON API поверх журнала.
Почти read-only: единственная запись — отметки подписчика user_taken (/api/take).

Открывается КНОПКОЙ «📈 Дашборд» внутри Telegram (web_app), когда runtime.webapp_url
задан. Хостится на нашем VPS за Caddy (авто-HTTPS на rDNS-имени Hetzner — Telegram
требует валидный https). Запуск: waitress-serve app.webapp:app (systemd rsirange-webapp).

ЧЕСТНОСТЬ И ГРАНИЦЫ ПУБЛИКАЦИИ. Страница ПУБЛИЧНАЯ (initData не проверяем — в ней нет
ничего платного): закрытая история и статистика и так публикуются в /trackrecord CSV;
у ОТКРЫТЫХ сигналов отдаются только символ/сторона/ТФ/возраст — БЕЗ входа, стопа и
целей (это платный контент). Минусовые числа не прячутся, дисклеймер обязателен.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl

from flask import Flask, Response, request

from app import clock, db, runtime
from app.brain import journal
from app.config import get_settings

app = Flask(__name__)
# публичные POST не должны принимать гигантский JSON (аудит 02.09): самый большой
# легитимный запрос — /api/admin/set с url-настройкой, десятки байт
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
_INDEX = Path(__file__).with_name("webapp_index.html")

import logging  # noqa: E402

_wlog = logging.getLogger("webapp")

# ------------------- приватный режим (подписчики) -------------------
# Аутентификация — подпись initData Telegram Mini App (HMAC-SHA256 на токене бота):
# страница остаётся публичной, но /api/me отдаёт платный слой (уровни открытых,
# unrealized R) только валидному активному подписчику или админу. Сетапы не отдаются
# НИКОМУ и здесь — граница публикации стратегии единая для всех поверхностей.

_AUTH_MAX_AGE_S = 24 * 3600            # replay-защита: initData старше суток не принимаем
_ADMIN_WRITE_MAX_AGE_S = 6 * 3600      # пишущие админ-ручки: окно replay ужато (аудит 02.09)


def _validate_init_data(raw: str, bot_token: str,
                        max_age_s: int = _AUTH_MAX_AGE_S) -> dict | None:
    """initData -> словарь user | None. Официальная схема Telegram:
    secret = HMAC(\"WebAppData\", bot_token); hash = HMAC(secret, sorted k=v)."""
    try:
        pairs = dict(parse_qsl(raw, strict_parsing=True))
    except ValueError:
        return None
    their_hash = pairs.pop("hash", None)
    # формат до сравнения: compare_digest кидает TypeError на не-ASCII str —
    # иначе мусорный заголовок даёт неаутентифицированную 500 (ревью 13.08)
    if not their_hash or not re.fullmatch(r"[0-9a-f]{64}", their_hash):
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    mine = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mine, their_hash):
        return None
    try:
        auth_ts = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if clock.utcnow().timestamp() - auth_ts > max_age_s:
        return None
    try:
        user = json.loads(pairs.get("user", ""))
    except ValueError:
        return None
    return user if isinstance(user, dict) and "id" in user else None


# свежесть — ПО СИМВОЛУ (аудит 05.10): общий штамп отдавал цену символа вне вотчлиста
# сколь угодно старой, пока вотчлист обновлялся. tried — время последней попытки символа,
# ok — время последней успешной цены символа.
_TICKERS: dict = {"prices": {}, "pcts": {}, "tried": {}, "ok": {}}
_TICKERS_LOCK = threading.Lock()
_TICKERS_TTL_S = 45.0                  # unrealized R не тик-в-тик — биржу щадим
_PRICES_MAX_STALE_S = 600.0            # кеш старше 10 мин «живым» R не прикидывается
_EX: dict = {"ex": None, "at": 0.0}
_EX_LOCK = threading.Lock()
_EX_TTL_S = 6 * 3600.0                 # маркеты перечитываем редко: load_markets тяжёлый


def _exchange():
    """Кешированный bybit: load_markets один раз, не на каждый промах кеша цен."""
    with _EX_LOCK:
        if _EX["ex"] is None or time.monotonic() - _EX["at"] > _EX_TTL_S:
            import ccxt
            ex = ccxt.bybit({"timeout": 25000})
            ex.load_markets()
            _EX["ex"], _EX["at"] = ex, time.monotonic()
        return _EX["ex"]


def _fetch_tickers(symbols: list[str]) -> dict[str, dict] | None:
    """Сетевой фетч БЕЗ лока кеша. None = попытка не удалась (кеш не трогаем).
    Неизвестные бирже символы (делистинг) отфильтровываются, а не роняют остальных."""
    try:
        ex = _exchange()
        known = [s for s in symbols if s in ex.markets]
        if not known:
            return {}
        data = ex.fetch_tickers(known)
        out = {}
        for s, t in data.items():
            if t.get("last"):
                out[s] = {"px": float(t["last"]),
                          "pct": (float(t["percentage"])
                                  if t.get("percentage") is not None else None)}
        return out
    except Exception:                                     # noqa: BLE001
        return None


def _last_prices(symbols: list[str]) -> dict[str, float]:
    """last-цены bybit (тот же источник, что у скоринга) с кешем 45с ПО СИМВОЛУ.

    Символ перезапрашивается, если его последняя попытка старше 45с: пропавший из ответа
    символ не делает каждый вызов сетевым (ревью 13.08), а символ вне вотчлиста больше не
    живёт на чужой свежести (аудит 05.10 — общий штамп морозил unrealized R платных).
    Попытку столбим ДО фетча — конкуренты не дублируют её; сам фетч вне лока. Отдаются
    ТОЛЬКО запрошенные символы с ценой не старше 10 мин — протухший R хуже прочерка."""
    with _TICKERS_LOCK:
        mono = time.monotonic()
        need = [s for s in symbols
                if mono - _TICKERS["tried"].get(s, -1e18) >= _TICKERS_TTL_S]
        for s in need:
            _TICKERS["tried"][s] = mono
    got = _fetch_tickers(need) if need else {}            # сеть — вне лока
    with _TICKERS_LOCK:
        now = time.monotonic()
        if got:
            for s, v in got.items():
                _TICKERS["prices"][s] = v["px"]
                _TICKERS["ok"][s] = now
                if v["pct"] is not None:
                    _TICKERS["pcts"][s] = v["pct"]
        return {s: _TICKERS["prices"][s] for s in symbols
                if s in _TICKERS["prices"]
                and now - _TICKERS["ok"].get(s, -1e18) <= _PRICES_MAX_STALE_S}


def _equity(since_created: str | None = None) -> list:
    """[{t: closed_at, r: кумулятивный R}] в порядке закрытий — тот же фильтр, что
    journal.stats(): последняя точка совпадает с total_r до копейки."""
    q = ("SELECT closed_at, result_r FROM signals WHERE status IN "
         "('tp1','tp2','tp3','stopped','expired') AND result_r IS NOT NULL ")
    args: tuple = ()
    if since_created:
        q += "AND created_at >= ? "
        args = (since_created,)
    q += "ORDER BY closed_at"
    out, cum = [], 0.0
    for r in db.query(q, args):
        cum += r["result_r"]
        out.append({"t": (r["closed_at"] or "")[:10], "r": round(cum, 2)})
    return out


def _open_signals_public() -> list:
    """Открытые сигналы БЕЗ уровней и БЕЗ тегов сетапов: уровни — платный контент,
    таксономия сетапов — стратегическая собственность (публичный CSV /trackrecord
    сознательно не содержит setup — дашборд держит ту же границу публикации).
    in_pos отличает исполненный вход от ожидающей лимитки (факт филла и так публичен —
    канал постит исполнение); возраст позиции — от филла, ожидания — от публикации,
    иначе лимитка выглядит «сделкой, висящей 50 часов» (вопрос владельца 13.08.2026)."""
    now = clock.utcnow()
    out = []
    for r in db.query("SELECT symbol, side, timeframe, created_at, filled_at "
                      "FROM signals WHERE status='open' AND result_r IS NULL "
                      "ORDER BY created_at DESC"):
        in_pos = bool(r["filled_at"])
        anchor = r["filled_at"] if in_pos else r["created_at"]
        age_h = None
        if anchor:
            from datetime import datetime
            age_h = round((now - datetime.fromisoformat(anchor))
                          .total_seconds() / 3600, 1)
        out.append({"symbol": r["symbol"].split("/")[0], "side": r["side"],
                    "tf": r["timeframe"], "age_h": age_h, "in_pos": in_pos})
    return out


def _avg_hold_hours(since_created: str | None = None) -> float | None:
    """Среднее удержание закрытых сделок (филл -> закрытие), часов. Только публичные
    исходы: теневые (rejected) сюда не входят, как и во всей прочей стате (аудит 05.10)."""
    q = ("SELECT AVG(MAX(0.0, (julianday(closed_at) - julianday(filled_at)) * 24.0)) AS h "
         "FROM signals WHERE filled_at IS NOT NULL AND closed_at IS NOT NULL "
         "AND result_r IS NOT NULL AND status IN ('tp1','tp2','tp3','stopped') ")
    args: tuple = ()
    if since_created:
        q += "AND created_at >= ?"
        args = (since_created,)
    row = db.query_one(q, args)
    return round(row["h"], 1) if row and row["h"] is not None else None


def _paid_since(tg_id: int) -> str | None:
    """Начало «твоей подписки»: первый реальный платёж, иначе дата регистрации
    (триал/промо/админ-грант — там paid_at нет)."""
    r = db.query_one("SELECT MIN(paid_at) AS t FROM payments "
                     "WHERE tg_id=? AND status='paid'", (tg_id,))
    if r and r["t"]:
        return r["t"]
    u = db.query_one("SELECT joined_at FROM users WHERE tg_id=?", (tg_id,))
    return u["joined_at"] if u else None


def _open_signals_private() -> list:
    """Открытые С УРОВНЯМИ (вход/стоп — платный слой) и живым unrealized R.
    Тегов сетапов нет и здесь: таксономия — собственность стратегии, её не видит никто."""
    rows = list(db.query(
        "SELECT id, symbol, side, timeframe, entry, stop, tp1, created_at, filled_at, "
        "snapshot_json FROM signals WHERE status='open' AND result_r IS NULL "
        "ORDER BY created_at DESC"))
    prices = _last_prices(sorted({r["symbol"] for r in rows})) if rows else {}
    now = clock.utcnow()
    out = []
    for r in rows:
        in_pos = bool(r["filled_at"])
        anchor = r["filled_at"] if in_pos else r["created_at"]
        age_h = None
        if anchor:
            from datetime import datetime
            age_h = round((now - datetime.fromisoformat(anchor))
                          .total_seconds() / 3600, 1)
        ur = None
        price = prices.get(r["symbol"])
        risk = abs(r["entry"] - r["stop"])
        if in_pos and price and risk:
            move = price - r["entry"] if r["side"] == "LONG" else r["entry"] - price
            ur = round(move / risk, 2)
        trail = None
        rule = journal._trail_of(r)
        if rule and risk:
            sg = 1 if r["side"] == "LONG" else -1
            act = r["entry"] + sg * rule["activate_r"] * risk
            gap = rule["distance_r"] * risk
            trail = {"act": round(act, 8), "gap": round(gap, 8),
                     "pct": round(gap / act * 100, 2) if act else None}
        out.append({"id": r["id"], "symbol": r["symbol"].split("/")[0],
                    "side": r["side"], "tf": r["timeframe"], "age_h": age_h,
                    "in_pos": in_pos, "entry": r["entry"], "stop": r["stop"],
                    "tp1": r["tp1"], "ur": ur, "trail": trail})
    return out


def _no_store(payload: dict, status: int = 200) -> Response:
    """Персональные ответы не кешируются нигде — в том числе промежуточным Caddy."""
    resp = Response(json.dumps(payload, ensure_ascii=False), status=status,
                    mimetype="application/json")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _auth_user(max_age_s: int = _AUTH_MAX_AGE_S) -> dict | None:
    """user из Authorization: tma <initData> | None. initData едет в Authorization:
    Caddy в access-логах редактирует именно этот заголовок — PII и 24-часовой
    replay-токен не осядут в логах (ревью 13.08)."""
    auth = request.headers.get("Authorization", "")
    raw = auth[4:] if auth.startswith("tma ") else ""
    return _validate_init_data(raw, get_settings().tg_bot_token, max_age_s)


def _taken_ids(tg_id: int) -> list[int]:
    return [r["signal_id"] for r in db.query(
        "SELECT signal_id FROM user_taken WHERE tg_id=?", (tg_id,))]


_CLOSED = "status IN ('tp1','tp2','tp3','stopped','expired') AND result_r IS NOT NULL"


def _r_series(since_created: str | None = None) -> tuple[list, list, list]:
    """(result_r, дистанция стопа в % от входа, часы в позиции) в порядке закрытий —
    сырьё симулятора депозита: сложный процент, требуемое плечо (риск% ÷ ход до
    стопа%) и ИЗДЕРЖКИ (комиссии+слип в R = стоимость%/ход до стопа%, фандинг по
    часам удержания) считаются на клиенте по той же модели, что в бэктесте."""
    from datetime import datetime

    q = f"SELECT result_r, entry, stop, filled_at, closed_at FROM signals WHERE {_CLOSED} "
    args: tuple = ()
    if since_created:
        q += "AND created_at >= ? "
        args = (since_created,)
    q += "ORDER BY closed_at, id"
    rs, sps, hold = [], [], []
    for r in db.query(q, args):
        rs.append(round(r["result_r"], 3))
        sps.append(round(abs(r["entry"] - r["stop"]) / r["entry"] * 100, 3)
                   if r["entry"] else 0.0)
        h = 0.0
        if r["filled_at"] and r["closed_at"]:
            try:
                h = (datetime.fromisoformat(r["closed_at"])
                     - datetime.fromisoformat(r["filled_at"])).total_seconds() / 3600
            except ValueError:
                h = 0.0
        hold.append(round(max(h, 0.0), 1))
    return rs, sps, hold


def _market_overview() -> list:
    """Вотчлист с последней ценой и суточным ходом — секция «Монеты» дашборда.
    Пусто при протухшем кеше цен (секция честно прячется, а не показывает старьё)."""
    syms = runtime.symbols()
    prices = _last_prices(syms)
    if not prices:
        return []
    with _TICKERS_LOCK:
        pcts = {k: v for k, v in _TICKERS["pcts"].items() if k in prices}
    out = []
    for sym in syms:
        px = prices.get(sym)
        if px:
            out.append({"s": sym.split("/")[0], "px": px, "pct": pcts.get(sym)})
    return out


def _last_closed_public(limit: int = 30) -> list:
    """Последние закрытые с id — публичная таблица «Закрытые» (тап -> график сделки).
    Исходы закрытых и так публичны (/trackrecord CSV, где есть и status) — граница
    публикации та же; st — статус исхода для чипа «тейк/трейл/стоп» (v7, 29.10)."""
    return [{"id": r["id"], "s": r["symbol"].split("/")[0], "side": r["side"],
             "tf": r["timeframe"], "r": r["result_r"], "d": (r["closed_at"] or "")[5:10],
             "st": r["status"]}
            for r in db.query(
                f"SELECT id, symbol, side, timeframe, status, result_r, closed_at FROM signals "
                f"WHERE {_CLOSED} ORDER BY closed_at DESC LIMIT ?", (limit,))]


def _my_stats(tg_id: int) -> dict:
    """Личный итог по отмеченным — базовый расчёт общий с ботом (карточка/коуч),
    поверх — личная эквити-кривая и «ты vs стратегия» за тот же период (v4-max)."""
    from app.delivery import payments
    out = payments.taken_stats(tg_id)
    rows = list(db.query(
        "SELECT s.result_r, s.closed_at FROM user_taken ut "
        "JOIN signals s ON s.id = ut.signal_id "
        "WHERE ut.tg_id=? AND s.result_r IS NOT NULL AND s.status IN "
        "('tp1','tp2','tp3','stopped','expired') ORDER BY s.closed_at", (tg_id,)))
    if rows:
        cum, eq = 0.0, []
        for r in rows:
            cum += r["result_r"]
            eq.append({"t": (r["closed_at"] or "")[:10], "r": round(cum, 2)})
        out["eq"] = eq
        strat = db.query_one(
            f"SELECT COALESCE(SUM(result_r), 0) AS s, COUNT(*) AS n FROM signals "
            f"WHERE {_CLOSED} AND closed_at >= ?", (rows[0]["closed_at"],))
        if strat:
            out["strat_r"] = round(float(strat["s"]), 2)
            out["strat_n"] = int(strat["n"])
    return out


def _symbols_best_worst(limit: int = 4) -> dict:
    """Лучшие/худшие инструменты по итоговому R закрытых — обе стороны, не витрина
    из одних плюсов (честность бренда). Символы закрытых и так публичны в CSV."""
    rows = [{"s": r["symbol"].split("/")[0], "n": r["n"], "r": round(float(r["sr"]), 2)}
            for r in db.query(
                f"SELECT symbol, COUNT(*) AS n, SUM(result_r) AS sr FROM signals "
                f"WHERE {_CLOSED} GROUP BY symbol")]
    rows.sort(key=lambda x: x["r"], reverse=True)
    n = len(rows)
    # без пересечения колонок и на малом числе символов: голова — лучшим,
    # хвост (worst-first) — худшим
    k = limit if n >= 2 * limit else max(1, n // 2) if n else 0
    best = rows[:k]
    tail = min(limit, n - k)
    worst = rows[-tail:][::-1] if tail > 0 else []
    return {"best": best, "worst": worst}


def _recent_closed(tg_id: int, limit: int = 15) -> list:
    """Недавно закрытые для отметки задним числом + ВСЕ отмеченные юзером закрытые:
    иначе отметка старше топ-15 исчезала бы из UI навсегда, оставаясь в итоге
    (ревью 13.08 — «невидимая и неснимаемая»). Результаты закрытых и так публичны
    (/trackrecord CSV) — уровни и сетапы сюда не кладём всё равно."""
    rows = {r["id"]: r for r in db.query(
        f"SELECT id, symbol, side, timeframe, result_r, closed_at FROM signals "
        f"WHERE {_CLOSED} ORDER BY closed_at DESC LIMIT ?", (limit,))}
    for r in db.query(
            f"SELECT s.id, s.symbol, s.side, s.timeframe, s.result_r, s.closed_at "
            f"FROM user_taken ut JOIN signals s ON s.id = ut.signal_id "
            f"WHERE ut.tg_id=? AND {_CLOSED}", (tg_id,)):
        rows.setdefault(r["id"], r)
    ordered = sorted(rows.values(), key=lambda r: r["closed_at"] or "", reverse=True)
    return [{"id": r["id"], "symbol": r["symbol"].split("/")[0], "side": r["side"],
             "tf": r["timeframe"], "r": r["result_r"], "d": (r["closed_at"] or "")[5:10]}
            for r in ordered]


def _splits(since_created: str | None = None) -> dict:
    """Разбивка закрытых по стороне и ТФ ({n, r, w}). Сторона и ТФ публичны (CSV
    /trackrecord), таксономии сетапов здесь нет."""
    q = f"SELECT side, timeframe, result_r FROM signals WHERE {_CLOSED} "
    args: tuple = ()
    if since_created:
        q += "AND created_at >= ?"
        args = (since_created,)
    side: dict = {}
    tf: dict = {}
    for r in db.query(q, args):
        for key, bucket in ((r["side"], side), (r["timeframe"], tf)):
            d = bucket.setdefault(key, {"n": 0, "r": 0.0, "w": 0})
            d["n"] += 1
            d["r"] += r["result_r"]
            d["w"] += 1 if r["result_r"] > 0 else 0
    for bucket in (side, tf):
        for d in bucket.values():
            d["r"] = round(d["r"], 2)
    return {"side": side, "tf": tf}


def _daily(since_created: str | None = None, days: int = 371) -> list:
    """Итог закрытых по дням (календарь-тепловая карта) за последний год."""
    from datetime import timedelta
    cutoff = (clock.utcnow() - timedelta(days=days)).isoformat()
    q = (f"SELECT substr(closed_at, 1, 10) AS d, COUNT(*) AS n, SUM(result_r) AS r "
         f"FROM signals WHERE {_CLOSED} AND closed_at >= ? ")
    args: tuple = (cutoff,)
    if since_created:
        q += "AND created_at >= ? "
        args = (cutoff, since_created)
    q += "GROUP BY d ORDER BY d"
    return [{"d": r["d"], "n": r["n"], "r": round(float(r["r"]), 2)} for r in db.query(q, args)]


_TRADE_FILTERS = {"side": {"long": "side='LONG'", "short": "side='SHORT'"},
                  "res": {"win": "result_r > 0", "loss": "result_r <= 0"}}


def _trades_page(offset: int, limit: int, side: str, tf: str, res: str, q: str) -> dict:
    """Публичная история закрытых с фильтрами — граница та же, что у CSV /trackrecord:
    исход, сторона, ТФ, даты; без сетапов. Итог и winrate — по ВСЕЙ выборке фильтра."""
    where, args = [_CLOSED], []
    if side in _TRADE_FILTERS["side"]:
        where.append(_TRADE_FILTERS["side"][side])
    if res in _TRADE_FILTERS["res"]:
        where.append(_TRADE_FILTERS["res"][res])
    if tf:
        where.append("timeframe=?")
        args.append(tf)
    if q:
        where.append("symbol LIKE ?")
        args.append(q.upper() + "%")
    w = " AND ".join(where)
    agg = db.query_one(f"SELECT COUNT(*) AS n, COALESCE(SUM(result_r), 0) AS s, "
                       f"COALESCE(SUM(result_r > 0), 0) AS w FROM signals WHERE {w}", tuple(args))
    rows = db.query(
        f"SELECT id, symbol, side, timeframe, status, result_r, created_at, filled_at, "
        f"closed_at FROM signals WHERE {w} ORDER BY closed_at DESC, id DESC LIMIT ? OFFSET ?",
        (*args, limit, offset))
    from datetime import datetime
    items = []
    for r in rows:
        hold = None
        if r["filled_at"] and r["closed_at"]:
            try:
                hold = round(max(0.0, (datetime.fromisoformat(r["closed_at"])
                                       - datetime.fromisoformat(r["filled_at"]))
                                 .total_seconds() / 3600), 1)
            except ValueError:
                hold = None
        items.append({"id": r["id"], "s": r["symbol"].split("/")[0], "side": r["side"],
                      "tf": r["timeframe"], "r": round(r["result_r"], 2),
                      "d": (r["closed_at"] or "")[:10], "c": (r["created_at"] or "")[:16],
                      "h": hold, "st": r["status"]})
    n = int(agg["n"]) if agg else 0
    return {"ok": True, "total": n, "sum_r": round(float(agg["s"]), 2) if agg else 0.0,
            "wins": int(agg["w"]) if agg else 0, "offset": offset, "items": items}


# ------------------- админ-панель (v5: пульт управления) -------------------
# Первый АДМИНСКИЙ пишущий контур на публичном вебе: гейт — та же подпись initData
# плюс uid в admin_ids; настройки меняются ТОЛЬКО по белому списку с валидацией
# (произвольный ключ в settings — это инъекция в конфиг бота, запрещено схемой).

_ADMIN_SETTINGS: dict[str, dict] = {
    "paused":            {"t": "bool", "label": "Пауза сигналов", "group": "Сигналы"},
    "min_confidence":    {"t": "float", "min": 0.0, "max": 1.0,
                          "label": "Мин. confidence", "group": "Сигналы"},
    "max_open_per_side": {"t": "int", "min": 0, "max": 50,
                          "label": "Лимит на сторону (0 = БЕЗ лимита!)", "group": "Сигналы"},
    "watchlist_auto_enabled": {"t": "bool", "group": "Сигналы",
                               "label": "Авто-вотчлист (подбирает и экзотику)"},
    "late_entry_gate":   {"t": "choice", "choices": ("off", "stop", "entry"),
                          "label": "Гейт «вход уже пройден» (entry — замер 06.10)",
                          "group": "Сигналы"},
    "channel_enabled":   {"t": "bool", "label": "Постить в канал", "group": "Канал"},
    "channel_mode":      {"t": "choice", "choices": ("full", "teaser"),
                          "label": "Режим канала", "group": "Канал"},
    "sub_price_stars":   {"t": "int", "min": 1, "max": 100000,
                          "label": "Цена подписки, Stars", "group": "Подписка"},
    "sub_days":          {"t": "float", "min": 0.01, "max": 365,
                          "label": "Срок подписки, дней", "group": "Подписка"},
    "free_signals_per_week": {"t": "int", "min": 0, "max": 100,
                              "label": "Бесплатных сигналов/нед", "group": "Подписка"},
    "usdt_bonus_days":   {"t": "int", "min": 0, "max": 30,
                          "label": "Бонус за USDT, дней", "group": "Подписка"},
    "affiliate_url":     {"t": "url", "label": "Реф-ссылка биржи", "group": "Прочее"},
    "backtest_public":   {"t": "bool", "label": "Секция «Бэктест 5 лет» в дашборде",
                          "group": "Прочее"},
    "executor_enabled":  {"t": "bool", "label": "Executor (нужны ключи BingX)", "group": "Прочее"},
    # v6 (24.10.2026): всё, что админ раньше менял только командами бота
    "min_stop_pct":      {"t": "float", "min": 0.0, "max": 10.0, "group": "Стратегия",
                          "label": "Мин. стоп, % (1.3 — замер 24.10; 0 = выкл)"},
    "side_cooldown_stops": {"t": "int", "min": 0, "max": 20, "group": "Стратегия",
                            "label": "Тормоз стороны: стопов за окно (0 = выкл)"},
    "side_cooldown_hours": {"t": "float", "min": 0.5, "max": 72, "group": "Стратегия",
                            "label": "Тормоз стороны: окно, ч"},
    "time_filter_enabled": {"t": "bool", "group": "Стратегия",
                            "label": "Временное окно NY для 6226"},
    "timeframes":        {"t": "choice", "choices": ("1h,4h", "1h", "4h"), "group": "Стратегия",
                          "label": "Таймфреймы сканирования"},
    "watchlist_size":    {"t": "int", "min": 3, "max": 40, "group": "Вотчлист",
                          "label": "Размер авто-вотчлиста"},
    "symbols":           {"t": "symbols", "group": "Вотчлист",
                          "label": "Вотчлист (BTC/USDT:USDT, …) — авто перезапишет"},
    "watchlist_blacklist": {"t": "symbols", "group": "Вотчлист",
                            "label": "Чёрный список монет"},
    "trial_days":        {"t": "int", "min": 0, "max": 60, "group": "Подписка",
                          "label": "Триал новым юзерам, дней"},
    "sub_price_usdt":    {"t": "float", "min": 0.0, "max": 10000, "group": "Подписка",
                          "label": "Цена подписки, USDT"},
    "intro_price_stars": {"t": "int", "min": 0, "max": 100000, "group": "Подписка",
                          "label": "Пробный тариф, Stars (0 = скрыть)"},
    "intro_hours":       {"t": "float", "min": 1, "max": 240, "group": "Подписка",
                          "label": "Пробный тариф, часов"},
    "quarter_price_stars": {"t": "int", "min": 0, "max": 1000000, "group": "Подписка",
                            "label": "Квартал, Stars (0 = скрыть)"},
    "year_price_stars":  {"t": "int", "min": 0, "max": 1000000, "group": "Подписка",
                          "label": "Год, Stars (0 = скрыть)"},
    "lifetime_price_stars": {"t": "int", "min": 0, "max": 1000000, "group": "Подписка",
                             "label": "Lifetime, Stars (0 = скрыть)"},
    "paid_signal_stars": {"t": "int", "min": 0, "max": 100000, "group": "Подписка",
                          "label": "Разовая покупка сигнала, Stars (0 = выкл)"},
    "referral_reward_days": {"t": "int", "min": 0, "max": 90, "group": "Подписка",
                             "label": "Награда рефереру, дней"},
    "display_leverage":  {"t": "float", "min": 0, "max": 125, "group": "Канал",
                          "label": "Плечо в постах, ×N (0 = скрыть строку)"},
    "signal_chart_enabled": {"t": "bool", "group": "Канал", "label": "График к посту сигнала"},
    "lifecycle_chart_enabled": {"t": "bool", "group": "Канал",
                                "label": "График к посту результата"},
    "channel_cta_enabled": {"t": "bool", "group": "Канал", "label": "CTA-кнопки под постами"},
    "cornix_block_enabled": {"t": "bool", "group": "Канал", "label": "Блок для Cornix в посте"},
    "executor_risk_pct": {"t": "float", "min": 0.1, "max": 5.0, "group": "Executor (демо)",
                          "label": "Риск на сделку, % эквити"},
    "executor_daily_loss_pct": {"t": "float", "min": 0.0, "max": 50.0,
                                "group": "Executor (демо)",
                                "label": "Дневной стоп-кран, % эквити (0 = выкл)"},
}

_SYMBOL_RE = re.compile(r"[A-Z0-9]{1,20}/USDT:USDT")


def _admin_uid(max_age_s: int = _AUTH_MAX_AGE_S) -> int | None:
    user = _auth_user(max_age_s)
    if user is None:
        return None
    uid = int(user["id"])
    return uid if uid in get_settings().admin_ids else None


def _validate_setting(key: str, value) -> str | None:
    """-> строка для settings | None если значение не проходит схему."""
    spec = _ADMIN_SETTINGS.get(key)
    if spec is None:
        return None
    t = spec["t"]
    if t == "bool":
        if isinstance(value, bool):
            return "1" if value else "0"
        return None
    if t == "choice":
        return value if isinstance(value, str) and value in spec["choices"] else None
    if t == "url":
        if value in ("", None):
            return ""
        ok = isinstance(value, str) and value.startswith("https://") and len(value) < 500
        return value if ok else None
    if t == "symbols":
        if not isinstance(value, str) or len(value) > 2000:
            return None
        items = [x.strip().upper() for x in value.replace("\n", ",").split(",") if x.strip()]
        if len(items) > 60 or not all(_SYMBOL_RE.fullmatch(x) for x in items):
            return None
        return ",".join(dict.fromkeys(items))            # порядок сохраняем, дубли — прочь
    if t in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not spec["min"] <= value <= spec["max"]:
            return None
        return str(int(value)) if t == "int" else str(float(value))
    return None


def _admin_settings_payload() -> list:
    cur = {
        "paused": runtime.paused(), "min_confidence": runtime.min_confidence(),
        "max_open_per_side": runtime.max_open_per_side(),
        "watchlist_auto_enabled": runtime.watchlist_auto_enabled(),
        "channel_enabled": runtime.channel_enabled(), "channel_mode": runtime.channel_mode(),
        "sub_price_stars": runtime.price_stars(), "sub_days": runtime.sub_days(),
        "free_signals_per_week": runtime.free_per_week(),
        "usdt_bonus_days": runtime.usdt_bonus_days(),
        "affiliate_url": runtime.affiliate_url(),
        "executor_enabled": runtime.executor_enabled(),
        "backtest_public": runtime.backtest_public(),
        "late_entry_gate": runtime.late_entry_gate(),
        "min_stop_pct": runtime.min_stop_pct(),
        "side_cooldown_stops": runtime.side_cooldown_stops(),
        "side_cooldown_hours": runtime.side_cooldown_hours(),
        "time_filter_enabled": runtime.time_filter_enabled(),
        "timeframes": ",".join(runtime.timeframes()),
        "watchlist_size": runtime.watchlist_size(),
        "symbols": ",".join(runtime.symbols()),
        "watchlist_blacklist": ",".join(runtime.watchlist_blacklist()),
        "trial_days": runtime.trial_days(), "sub_price_usdt": runtime.price_usdt(),
        "intro_price_stars": runtime.intro_price_stars(), "intro_hours": runtime.intro_hours(),
        "quarter_price_stars": runtime.quarter_price_stars(),
        "year_price_stars": runtime.year_price_stars(),
        "lifetime_price_stars": runtime.lifetime_price_stars(),
        "paid_signal_stars": runtime.paid_signal_stars(),
        "referral_reward_days": runtime.referral_reward_days(),
        "display_leverage": runtime.display_leverage(),
        "signal_chart_enabled": runtime.signal_chart_enabled(),
        "lifecycle_chart_enabled": runtime.lifecycle_chart_enabled(),
        "channel_cta_enabled": runtime.channel_cta_enabled(),
        "cornix_block_enabled": runtime.cornix_block_enabled(),
        "executor_risk_pct": runtime.executor_risk_pct(),
        "executor_daily_loss_pct": runtime.executor_daily_loss_pct(),
    }
    return [{"key": k, "value": cur[k], "label": v["label"], "t": v["t"],
             "group": v["group"], **({"choices": v["choices"]} if "choices" in v else {}),
             **({"min": v["min"], "max": v["max"]} if "min" in v else {})}
            for k, v in _ADMIN_SETTINGS.items()]


_LOG = Path("logs/bot.log")
_SECRET_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}|[A-Za-z0-9]{40,}")


def _log_tail(n: int = 30) -> list:
    """Последние WARNING/ERROR бота (хвост файла ~256 КБ). Токеноподобные строки
    вымарываются — лог могут читать с телефона, а секретам в UI не место."""
    try:
        with _LOG.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 262144))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = [ln for ln in lines if " WARNING " in ln or " ERROR " in ln]
    return [_SECRET_RE.sub("•••", ln)[:300] for ln in out[-n:]][::-1]


@app.get("/api/admin")
def api_admin() -> Response:
    """Всё для пульта: здоровье, юзеры/деньги, настройки, журнал. Только админам."""
    if _admin_uid() is None:
        return _no_store({"ok": False}, 403)
    from datetime import datetime

    from app.delivery import payments
    now = clock.utcnow()

    def age_min(iso: str | None) -> float | None:
        if not iso:
            return None
        try:
            return round((now - datetime.fromisoformat(iso)).total_seconds() / 60, 1)
        except ValueError:
            return None

    pays = db.query_one("SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS s "
                        "FROM payments WHERE status='paid'") or {"n": 0, "s": 0.0}
    # Stars и USDT — разные деньги: сумма «всего» без валюты врала (аудит 05.10)
    by_cur = {r["currency"]: round(float(r["s"]), 2) for r in db.query(
        "SELECT currency, SUM(amount) AS s FROM payments WHERE status='paid' "
        "GROUP BY currency")}
    last_pays = [dict(r) for r in db.query(
        "SELECT tg_id, method, amount, currency, days, paid_at FROM payments "
        "WHERE status='paid' ORDER BY paid_at DESC LIMIT 10")]
    now_iso = clock.utcnow_iso()
    # «платящих» = есть оплаченный платёж: триал ставит тот же tier='paid', и
    # фильтр по tier всё равно показывал 7 «платных» при нуле платежей (07.09)
    bd = payments.subscriber_breakdown()
    paid_now = {"n": bd["paying"]}
    lapsed = db.query_one(
        "SELECT COUNT(*) AS n FROM users WHERE tier='paid' AND paid_until <= ?",
        (now_iso,)) or {"n": 0}
    recent_users = [dict(r) for r in db.query(
        "SELECT tg_id, username, tier, paid_until, joined_at FROM users "
        "ORDER BY joined_at DESC LIMIT 15")]
    open_full = [dict(r) for r in db.query(
        "SELECT id, symbol, side, timeframe, entry, stop, created_at, filled_at "
        "FROM signals WHERE status='open' AND result_r IS NULL ORDER BY created_at DESC")]
    from datetime import timedelta

    from app.power import pmset_batt
    since90 = (now - timedelta(days=90)).isoformat()
    daily = [dict(r) for r in db.query(
        "SELECT substr(paid_at, 1, 10) AS d, currency AS cur, ROUND(SUM(amount), 2) AS s, "
        "COUNT(*) AS n FROM payments WHERE status='paid' AND paid_at >= ? "
        "GROUP BY d, cur ORDER BY d", (since90,))]
    by_method = [dict(r) for r in db.query(
        "SELECT method, currency AS cur, COUNT(*) AS n, ROUND(SUM(amount), 2) AS s "
        "FROM payments WHERE status='paid' GROUP BY method, currency ORDER BY n DESC")]
    funnel_row = db.query_one(
        "SELECT COUNT(*) AS joined, "
        "SUM(trial_granted_at IS NOT NULL) AS trial_ever, "
        f"SUM({payments.paid_ever_sql('users')}) AS paid_ever, "
        "SUM((SELECT COUNT(*) FROM payments p WHERE p.tg_id=users.tg_id "
        "     AND p.status='paid') >= 2) AS renewed "
        "FROM users")
    funnel = {k: int((funnel_row[k] if funnel_row else 0) or 0)
              for k in ("joined", "trial_ever", "paid_ever", "renewed")}
    funnel["paying_now"] = bd["paying"]
    refunds = db.query_one("SELECT COUNT(*) AS n FROM payments WHERE status='refunded'")
    shadow = {}
    for reason, label in _SHADOW_LABEL.items():
        st = journal.shadow_stats(reason=reason)
        n = sum(v["n"] for v in st.values())
        sm = round(sum(v["sum"] for v in st.values()), 2)
        opn = db.query_one(
            "SELECT COUNT(*) AS n FROM signals WHERE status='rejected' AND result_r IS NULL "
            "AND closed_at IS NULL AND snapshot_json LIKE ?", (f'%"shadow": "{reason}"%',))
        shadow[reason] = {"label": label, "n": n, "sum": sm,
                          "exp": round(sm / n, 3) if n else None,
                          "wins": sum(v["wins"] for v in st.values()),
                          "open": opn["n"] if opn else 0}
    today = now.isoformat()[:10]
    today_row = db.query_one(
        "SELECT SUM(status != 'rejected') AS pub, SUM(status = 'rejected') AS sh "
        "FROM signals WHERE substr(created_at, 1, 10) = ?", (today,))
    payload = {
        "ok": True,
        "health": {
            "poller_tick_min": age_min(runtime.last_poller_tick_at()),
            "backup_min": age_min(runtime.last_backup_at()),
            "offsite_min": age_min(runtime.last_offsite_at()),
            "webapp_url": runtime.webapp_url(),
            "executor_enabled": runtime.executor_enabled(),
            "paused": runtime.paused(),
            "power": pmset_batt(),
            "overdue": len(journal.overdue_open_signals(now_iso)),
            "today_pub": int((today_row["pub"] if today_row else 0) or 0),
            "today_shadow": int((today_row["sh"] if today_row else 0) or 0),
            "log": _log_tail(),
        },
        "shadow": shadow,
        "users": {"counts": payments.user_counts(), "paid_now": paid_now["n"],
                  "trial_now": bd["trial"], "granted_now": bd["granted"],
                  "lapsed": lapsed["n"], "acquisition": payments.acquisition_breakdown(),
                  "recent": recent_users},
        "money": {"payments_n": pays["n"], "payments_sum": round(float(pays["s"]), 2),
                  "payments_by_cur": by_cur, "daily": daily, "by_method": by_method,
                  "funnel": funnel, "refunds": refunds["n"] if refunds else 0,
                  "last": last_pays},
        "settings": _admin_settings_payload(),
        "open_signals": open_full,
    }
    return _no_store(payload)


@app.post("/api/admin/set")
def api_admin_set() -> Response:
    """Изменение ОДНОЙ настройки по белому списку. 400 — ключ/значение вне схемы."""
    uid = _admin_uid(_ADMIN_WRITE_MAX_AGE_S)
    if uid is None:
        return _no_store({"ok": False}, 403)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _no_store({"ok": False}, 400)
    key = body.get("key")
    if not isinstance(key, str) or key not in _ADMIN_SETTINGS:
        return _no_store({"ok": False, "err": "unknown key"}, 400)
    stored = _validate_setting(key, body.get("value"))
    if stored is None:
        return _no_store({"ok": False, "err": "bad value"}, 400)
    if key == "executor_enabled" and stored == "1":
        runtime.set_executor_since(clock.utcnow_iso())   # как /executor on: не догонять старое
    runtime.set_value(key, stored)
    _wlog.info("admin %s: настройка %s -> %r", uid, key, stored)  # аудит-след записи
    return _no_store({"ok": True, "settings": _admin_settings_payload()})


@app.post("/api/admin/user")
def api_admin_user() -> Response:
    """grant/revoke подписки из пульта (те же пути, что /grant и /revoke в боте)."""
    uid = _admin_uid(_ADMIN_WRITE_MAX_AGE_S)
    if uid is None:
        return _no_store({"ok": False}, 403)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _no_store({"ok": False}, 400)
    tg_id, action = body.get("tg_id"), body.get("action")
    days = body.get("days", 30)
    if (not isinstance(tg_id, int) or isinstance(tg_id, bool)
            or not 0 < tg_id < 2 ** 63 or action not in ("grant", "revoke")
            or isinstance(days, bool) or not isinstance(days, (int, float))
            or not float(days).is_integer() or not 1 <= days <= 365):
        return _no_store({"ok": False}, 400)   # дробные дни: int(0.5)=0 давал ложный успех
    from app.delivery import payments
    if action == "grant":
        payments.admin_grant(tg_id, int(days))
        # те же последствия, что у /grant в боте: подтверждённая оплата награждает
        # реферера (идемпотентно, порог квалифицирующих дней внутри; аудит 05.10)
        from app import runtime as _rt
        payments.reward_referrer_on_payment(tg_id, _rt.referral_reward_days(), int(days))
    else:
        payments.admin_revoke(tg_id)
    _wlog.info("admin %s: %s подписки юзеру %s (%s дн)",
               uid, action, tg_id, int(days))            # аудит-след записи
    return _no_store({"ok": True})


_USER_STATUS_SQL = """
SELECT u.tg_id, u.username, u.tier, u.paid_until, u.joined_at, u.acquisition_source AS src,
       u.invited_by, u.trial_granted_at, u.is_active, u.notify_muted, u.depo_usd,
       (SELECT COUNT(*) FROM payments p WHERE p.tg_id=u.tg_id AND p.status='paid') AS pays_n,
       (SELECT COUNT(*) FROM user_taken t WHERE t.tg_id=u.tg_id) AS taken_n,
       (SELECT COUNT(*) FROM users r WHERE r.invited_by=u.tg_id) AS invited_n
FROM users u
"""


def _user_status(r, now_iso: str) -> str:
    active = bool(r["paid_until"]) and r["paid_until"] > now_iso
    if active:
        return "paying" if r["pays_n"] else ("trial" if r["trial_granted_at"] else "grant")
    return "lapsed" if r["paid_until"] else "free"


def _user_row(r, now_iso: str) -> dict:
    """Строка юзера для админки — БЕЗ api-ключей и прочих секретов из таблицы users."""
    return {"tg_id": r["tg_id"], "username": r["username"], "status": _user_status(r, now_iso),
            "paid_until": (r["paid_until"] or "")[:10] or None,
            "joined": (r["joined_at"] or "")[:10], "src": r["src"],
            "invited_by": r["invited_by"], "pays_n": r["pays_n"], "taken_n": r["taken_n"],
            "invited_n": r["invited_n"], "blocked": not r["is_active"],
            "muted": bool(r["notify_muted"]), "depo": bool(r["depo_usd"])}


@app.get("/api/admin/users")
def api_admin_users() -> Response:
    """Поиск/фильтр юзеров: q — tg_id или @username (подстрока), st — статус."""
    if _admin_uid() is None:
        return _no_store({"ok": False}, 403)
    q = request.args.get("q", "").strip().lstrip("@")
    st = request.args.get("st", "")
    if len(q) > 40 or st not in ("", "paying", "trial", "grant", "lapsed", "free"):
        return _no_store({"ok": False}, 400)
    where, args = "", ()  # type: tuple[str, tuple]
    if q:
        where = "WHERE CAST(u.tg_id AS TEXT) LIKE ? OR u.username LIKE ?"
        args = (q + "%", "%" + q + "%")
    now_iso = clock.utcnow_iso()
    rows = [_user_row(r, now_iso)
            for r in db.query(_USER_STATUS_SQL + where + " ORDER BY u.joined_at DESC", args)]
    if st:
        rows = [r for r in rows if r["status"] == st]
    return _no_store({"ok": True, "items": rows[:200], "total": len(rows)})


@app.get("/api/admin/user")
def api_admin_user_detail() -> Response:
    """Карточка юзера: статус, платежи, рефералы, личный итог по отмеченным."""
    if _admin_uid() is None:
        return _no_store({"ok": False}, 403)
    raw = request.args.get("id", "")
    if not re.fullmatch(r"[0-9]{1,19}", raw):
        return _no_store({"ok": False}, 400)
    uid = int(raw)
    r = db.query_one(_USER_STATUS_SQL + "WHERE u.tg_id=?", (uid,))
    if r is None:
        return _no_store({"ok": False, "err": "нет такого юзера"}, 404)
    from app.delivery import payments
    now_iso = clock.utcnow_iso()
    pays = [dict(p) for p in db.query(
        "SELECT id, method, amount, currency, days, status, created_at, paid_at FROM payments "
        "WHERE tg_id=? ORDER BY COALESCE(paid_at, created_at) DESC LIMIT 50", (uid,))]
    invited = [{"tg_id": x["tg_id"], "username": x["username"],
                "rewarded": bool(x["referral_rewarded_at"]), "joined": (x["joined_at"] or "")[:10]}
               for x in db.query("SELECT tg_id, username, referral_rewarded_at, joined_at "
                                 "FROM users WHERE invited_by=? ORDER BY joined_at DESC", (uid,))]
    deliv = {d["kind"]: d["n"] for d in db.query(
        "SELECT kind, COUNT(*) AS n FROM deliveries WHERE tg_id=? GROUP BY kind", (uid,))}
    return _no_store({"ok": True, "user": _user_row(r, now_iso), "payments": pays,
                      "invited": invited, "deliveries": deliv,
                      "taken": payments.taken_stats(uid),
                      "notify": payments.notify_prefs(uid)})


_SHADOW_LABEL = {"setup_off": "сетап в тени", "late_entry": "вход пройден"}


@app.get("/api/admin/signals")
def api_admin_signals() -> Response:
    """Журнал для админа С УРОВНЯМИ: открытые, закрытые, тени. Тегов сетапов нет и тут —
    граница стратегии единая для всех поверхностей дашборда (см. шапку модуля)."""
    if _admin_uid() is None:
        return _no_store({"ok": False}, 403)
    view = request.args.get("view", "open")
    q = request.args.get("q", "")
    off_s = request.args.get("offset", "0")
    if (view not in ("open", "closed", "shadow", "all") or not re.fullmatch(r"[A-Za-z0-9]{0,15}", q)
            or not re.fullmatch(r"[0-9]{1,6}", off_s)):
        return _no_store({"ok": False}, 400)
    cond = {"open": "status='open' AND result_r IS NULL",
            "closed": _CLOSED,
            "shadow": "status='rejected' AND snapshot_json LIKE '%\"shadow\":%'",
            "all": "1=1"}[view]
    args: tuple = ()
    if q:
        cond += " AND symbol LIKE ?"
        args = (q.upper() + "%",)
    cnt = db.query_one(f"SELECT COUNT(*) AS n FROM signals WHERE {cond}", args)
    total = cnt["n"] if cnt else 0
    rows = db.query(
        f"SELECT id, symbol, side, timeframe, entry, stop, tp1, status, result_r, created_at, "
        f"filled_at, closed_at, trail_stop, snapshot_json FROM signals WHERE {cond} "
        f"ORDER BY id DESC LIMIT 50 OFFSET ?", (*args, int(off_s)))
    items = []
    for r in rows:
        try:
            shadow = json.loads(r["snapshot_json"] or "{}").get("shadow")
        except ValueError:
            shadow = None
        items.append({"id": r["id"], "s": r["symbol"].split("/")[0], "side": r["side"],
                      "tf": r["timeframe"], "entry": r["entry"], "stop": r["stop"],
                      "tp1": r["tp1"], "status": r["status"], "r": r["result_r"],
                      "created": (r["created_at"] or "")[:16],
                      "filled": (r["filled_at"] or "")[:16],
                      "closed": (r["closed_at"] or "")[:16], "trail": r["trail_stop"],
                      "shadow": _SHADOW_LABEL.get(shadow) if shadow else None})
    return _no_store({"ok": True, "total": total, "items": items})


_EXEC_CACHE: dict = {"at": 0.0, "status": None}


@app.get("/api/admin/executor")
def api_admin_executor() -> Response:
    """Демо-executor: статус и эквити (сеть — кеш 60с), позиции, реальный R против журнала
    по исправленным ценам исполнения (24.10), ошибки постановки."""
    if _admin_uid() is None:
        return _no_store({"ok": False}, 403)
    from app.v2 import bingx_executor as bx
    with _CHART_CACHE_LOCK:
        cached = _EXEC_CACHE["status"] if time.monotonic() - _EXEC_CACHE["at"] < 60 else None
    if cached is None:
        try:
            cached = bx.status()
        except Exception as e:                            # noqa: BLE001 — сеть/ключи
            cached = {"enabled": runtime.executor_enabled(), "vst": f"ошибка: {str(e)[:60]}"}
        with _CHART_CACHE_LOCK:
            _EXEC_CACHE.update(at=time.monotonic(), status=cached)
    live = [dict(r) for r in db.query(
        "SELECT eo.signal_id, eo.symbol, eo.side, eo.qty, eo.entry_px, eo.stop_px, eo.trail_px, "
        "eo.state, eo.fill_px, eo.created_at, s.trail_stop FROM executor_orders eo "
        "JOIN signals s ON s.id=eo.signal_id WHERE eo.state IN ('placing','placed','filled') "
        "ORDER BY eo.created_at DESC")]
    closed = []
    for r in db.query(
            "SELECT eo.signal_id, eo.symbol, eo.side, eo.qty, eo.fill_px, eo.close_px, "
            "eo.fill_fee, eo.close_fee, eo.closed_at, s.entry, s.stop, s.result_r "
            "FROM executor_orders eo JOIN signals s ON s.id=eo.signal_id "
            "WHERE eo.state='closed' AND eo.fill_px IS NOT NULL AND eo.close_px IS NOT NULL "
            "ORDER BY eo.closed_at DESC"):
        risk = abs(r["entry"] - r["stop"])
        if not risk or not r["qty"]:
            continue
        sg = 1 if r["side"] == "LONG" else -1
        fees = ((r["fill_fee"] or 0) + (r["close_fee"] or 0)) / (risk * r["qty"])
        closed.append({"id": r["signal_id"], "s": r["symbol"].split("/")[0], "side": r["side"],
                       "real": round(sg * (r["close_px"] - r["fill_px"]) / risk - abs(fees), 3),
                       "journal": r["result_r"],
                       "slip": round(sg * (r["entry"] - r["fill_px"]) / risk, 3),
                       "d": (r["closed_at"] or "")[:10]})
    errors = [dict(r) for r in db.query(
        "SELECT signal_id, symbol, note, created_at FROM executor_orders WHERE state='error' "
        "ORDER BY created_at DESC LIMIT 15")]
    n = len(closed)
    summary = {"n": n, "real": round(sum(c["real"] for c in closed), 2),
               "journal": round(sum(c["journal"] or 0 for c in closed), 2),
               "slip": round(sum(c["slip"] for c in closed) / n, 3) if n else None}
    return _no_store({"ok": True, "status": cached, "live": live, "closed": closed[:60],
                      "summary": summary, "errors": errors})


@app.get("/api/me")
def api_me() -> Response:
    """Платный слой для валидного подписчика/админа. Без валидной подписи — 401."""
    user = _auth_user()
    if user is None:
        return _no_store({"ok": False}, 401)
    uid = int(user["id"])
    from app.delivery import payments
    private = payments.is_paid(uid) or uid in get_settings().admin_ids
    payload: dict = {"ok": True, "private": private,
                     "admin": uid in get_settings().admin_ids}
    if private:
        since = _paid_since(uid)
        payload["positions"] = _open_signals_private()
        payload["since"] = (since or "")[:10]
        payload["since_stats"] = journal.stats_since_created(since) if since else None
        payload["taken"] = _taken_ids(uid)
        payload["my_stats"] = _my_stats(uid)
        payload["recent_closed"] = _recent_closed(uid)
        payload["bot"] = runtime.bot_username()   # deep-link «поделиться карточкой»
        payload["sub"] = _sub_info(uid)
        payload["referral"] = _referral_info(uid)
        dp = payments.depo_prefs(uid)
        payload["depo"] = {"usd": dp[0], "risk": dp[1]} if dp else None
        payload["notify"] = payments.notify_prefs(uid)
        aff = runtime.affiliate_url()
        payload["affiliate"] = aff if aff.startswith("https://") else None
    return _no_store(payload)


def _sub_info(uid: int) -> dict:
    """Карточка подписки «Моё»: до какого числа, сколько осталось, вид (оплата/триал/грант)."""
    from datetime import datetime

    from app.delivery import payments
    u = payments.get_user(uid)
    until = u["paid_until"] if u else None
    left = None
    if until:
        try:
            left = round(max(0.0, (datetime.fromisoformat(until) - clock.utcnow())
                             .total_seconds() / 86400), 2)
        except ValueError:
            left = None
    kind = ("paid" if payments.has_paid_ever(uid) else
            "trial" if u is not None and u["trial_granted_at"] else
            "grant" if left else "free")
    return {"until": (until or "")[:10] or None, "days_left": left, "kind": kind,
            "price_stars": runtime.price_stars(), "sub_days": runtime.sub_days()}


def _referral_info(uid: int) -> dict:
    from app.delivery import payments
    st = payments.referral_stats(uid)
    bot = runtime.bot_username()
    return {"link": f"https://t.me/{bot}?start=ref_{uid}" if bot else None,
            "invited": st["invited"], "rewarded": st["rewarded"],
            "reward_days": runtime.referral_reward_days()}


@app.post("/api/prefs")
def api_prefs() -> Response:
    """Личные настройки из «Моё» — те же поля, что /depo, /mute, /quiet в боте:
    депозит и риск для расчёта объёма, мьют и тихие часы DM-сигналов. Только платник/админ."""
    user = _auth_user()
    if user is None:
        return _no_store({"ok": False}, 401)
    uid = int(user["id"])
    from app.delivery import payments
    if not (payments.is_paid(uid) or uid in get_settings().admin_ids):
        return _no_store({"ok": False}, 403)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _no_store({"ok": False}, 400)

    def num(v, lo, hi):
        return (not isinstance(v, bool) and isinstance(v, (int, float))
                and lo <= v <= hi)

    if "depo_usd" in body or "risk_pct" in body:
        usd, risk = body.get("depo_usd"), body.get("risk_pct")
        if usd is None and risk is None:
            payments.set_depo(uid, None, None)
        elif num(usd, 10, 10_000_000) and num(risk, 0.1, 10):
            payments.set_depo(uid, float(usd or 0), float(risk or 0))
        else:
            return _no_store({"ok": False, "err": "депозит 10…10 000 000, риск 0.1…10%"}, 400)
    if "muted" in body:
        if not isinstance(body["muted"], bool):
            return _no_store({"ok": False}, 400)
        payments.set_muted(uid, body["muted"])
    if "quiet_start" in body or "quiet_end" in body:
        qs, qe = body.get("quiet_start"), body.get("quiet_end")
        if qs is None and qe is None:
            payments.set_quiet_hours(uid, None, None)
        elif (isinstance(qs, int) and isinstance(qe, int) and not isinstance(qs, bool)
              and not isinstance(qe, bool) and 0 <= qs <= 23 and 0 <= qe <= 23):
            payments.set_quiet_hours(uid, qs, qe)
        else:
            return _no_store({"ok": False, "err": "часы 0…23 (UTC)"}, 400)
    dp = payments.depo_prefs(uid)
    return _no_store({"ok": True, "depo": {"usd": dp[0], "risk": dp[1]} if dp else None,
                      "notify": payments.notify_prefs(uid)})


def _account_payload(uid: int) -> dict | None:
    """«Мой счёт» юзера для вкладки «Счёт» или None (не нажимал /start в боте)."""
    from app.delivery import my_account
    acc = my_account.account_for(uid)
    if acc is None:
        return None
    payload = my_account.to_payload(acc, my_account.push_enabled(uid))
    payload["bot"] = runtime.bot_username()
    return payload


@app.get("/api/account")
def api_account() -> Response:
    """Виртуальный счёт в $ (app/delivery/my_account.py) — ЛЮБОМУ зарегистрированному юзеру
    с валидной подписью, не только платнику: внутри только уже закрытые публичные сделки
    (граница та же, что /api/trades и CSV /trackrecord), ничего платного. Ретеншн 28.10:
    уходят на конце триала — им-то и нужно видеть, что сигналы сделали бы в деньгах."""
    user = _auth_user()
    if user is None:
        return _no_store({"ok": False}, 401)
    payload = _account_payload(int(user["id"]))
    if payload is None:
        return _no_store({"ok": True, "registered": False, "bot": runtime.bot_username()})
    return _no_store(payload)


@app.post("/api/account")
def api_account_set() -> Response:
    """Настройки счёта из Mini App: депозит/риск (те же поля, что /depo — личные сигналы
    тоже посчитают объём от них) и утренний отчёт. Границы — как у /api/prefs. Юзера
    здесь не заводим: без /start в боте ему некуда слать отчёт — 404."""
    user = _auth_user()
    if user is None:
        return _no_store({"ok": False}, 401)
    uid = int(user["id"])
    from app.delivery import my_account, payments
    if payments.get_user(uid) is None:
        return _no_store({"ok": False, "err": "сначала /start в боте"}, 404)
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not ({"depo_usd", "risk_pct", "push"} & set(body)):
        return _no_store({"ok": False}, 400)

    def num(v, lo, hi):
        return not isinstance(v, bool) and isinstance(v, (int, float)) and lo <= v <= hi

    depo: tuple[float | None, float | None] | None = None
    if "depo_usd" in body or "risk_pct" in body:
        usd, risk = body.get("depo_usd"), body.get("risk_pct")
        if usd is None and risk is None:
            depo = (None, None)
        elif num(usd, 10, 10_000_000) and num(risk, 0.1, 10):
            depo = (float(usd or 0), float(risk or 0))
        else:
            return _no_store({"ok": False, "err": "депозит 10…10 000 000, риск 0.1…10%"}, 400)
    push = body.get("push")
    if "push" in body and not isinstance(push, bool):
        return _no_store({"ok": False}, 400)
    if depo is not None:                      # запись — только когда ВСЁ тело прошло проверку
        payments.set_depo(uid, *depo)
    if isinstance(push, bool):
        my_account.set_push(uid, push)
    return _no_store(_account_payload(uid) or {"ok": False})


@app.get("/api/trades")
def api_trades() -> Response:
    """Публичная история закрытых с фильтрами и пагинацией (вкладка «Сделки»)."""
    a = request.args

    def int_arg(name: str, default: int, lo: int, hi: int) -> int | None:
        raw = a.get(name, str(default))
        if not re.fullmatch(r"[0-9]{1,6}", raw):
            return None
        v = int(raw)
        return v if lo <= v <= hi else None

    offset, limit = int_arg("offset", 0, 0, 100000), int_arg("limit", 40, 1, 100)
    side, tf, res = a.get("side", ""), a.get("tf", ""), a.get("res", "")
    q = a.get("q", "")
    if (offset is None or limit is None or side not in ("", "long", "short")
            or res not in ("", "win", "loss") or not re.fullmatch(r"(|15m|30m|1h|4h|1d)", tf)
            or not re.fullmatch(r"[A-Za-z0-9]{0,15}", q)):
        return _no_store({"ok": False}, 400)
    body = json.dumps(_trades_page(offset, limit, side, tf, res, q), ensure_ascii=False)
    resp = Response(body, mimetype="application/json")
    resp.headers["Cache-Control"] = "public, max-age=30"
    return resp


@app.post("/api/take")
def api_take() -> Response:
    """Отметка «я зашёл в эту сделку» (или снятие). Только активный платник/админ:
    фри-юзеру и истёкшему — 403, даже с валидной подписью."""
    user = _auth_user()
    if user is None:
        return _no_store({"ok": False}, 401)
    uid = int(user["id"])
    from app.delivery import payments
    if not (payments.is_paid(uid) or uid in get_settings().admin_ids):
        return _no_store({"ok": False}, 403)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):                # [1,2]/"abc"/5 — валидный JSON, но не объект
        return _no_store({"ok": False}, 400)
    sid, take = body.get("signal_id"), body.get("taken")
    if (not isinstance(sid, int) or isinstance(sid, bool) or not isinstance(take, bool)
            or not 0 < sid < 2 ** 63):            # за 64 бита SQLite не биндится -> был бы 500
        return _no_store({"ok": False}, 400)
    if not db.query_one("SELECT 1 FROM signals WHERE id=?", (sid,)):
        return _no_store({"ok": False}, 404)
    if take:
        db.execute("INSERT OR IGNORE INTO user_taken(tg_id, signal_id, created_at) "
                   "VALUES (?,?,?)", (uid, sid, clock.utcnow_iso()))
    else:
        db.execute("DELETE FROM user_taken WHERE tg_id=? AND signal_id=?", (uid, sid))
    # свежие отметки и итог сразу в ответе — фронт перерисовывается без второго GET
    return _no_store({"ok": True, "taken": _taken_ids(uid), "my_stats": _my_stats(uid)})


_STATS_CACHE: dict = {"at": 0.0, "body": None}
_STATS_CACHE_LOCK = threading.Lock()
_STATS_TTL_S = 20.0


@app.get("/api/stats")
def api_stats() -> Response:
    # публичный неаутентифицированный эндпоинт с ~8 агрегатами (2 скана эквити):
    # 20с серверного кеша срезают DoS-поверхность, Cache-Control клиентский и так 30с
    with _STATS_CACHE_LOCK:
        if _STATS_CACHE["body"] is not None \
                and time.monotonic() - _STATS_CACHE["at"] < _STATS_TTL_S:
            resp = Response(_STATS_CACHE["body"], mimetype="application/json")
            resp.headers["Cache-Control"] = "public, max-age=30"
            return resp
    epoch = runtime.strategy_epoch()
    r_all, sp_all, h_all = _r_series()
    r_ep, sp_ep, h_ep = _r_series(epoch) if epoch else ([], [], [])
    payload = {
        "generated_at": clock.utcnow_iso(),
        "epoch_since": (epoch or "")[:10],
        "all": journal.stats(),
        "epoch": journal.stats_since_created(epoch) if epoch else None,
        "hold_all": _avg_hold_hours(),
        "hold_epoch": _avg_hold_hours(epoch) if epoch else None,
        "equity_all": _equity(),
        "equity_epoch": _equity(epoch) if epoch else [],
        "open_signals": _open_signals_public(),
        "symbols": _symbols_best_worst(),
        "market": _market_overview(),
        "last_closed": _last_closed_public(),
        "r_all": r_all, "sp_all": sp_all, "h_all": h_all,
        "r_epoch": r_ep, "sp_epoch": sp_ep, "h_epoch": h_ep,
        "kelly_all": journal.kelly_stats(),
        "kelly_epoch": journal.kelly_stats(epoch) if epoch else None,
        "split_all": _splits(),
        "split_epoch": _splits(epoch) if epoch else None,
        "daily_all": _daily(),
        "daily_epoch": _daily(epoch) if epoch else [],
        # флаг секции «Бэктест 5 лет»: фронт не дёргает /api/backtest вслепую (404 в
        # консоли на каждом открытии, пока секция снята с витрины)
        "bt": runtime.backtest_public(),
    }
    body = json.dumps(payload, ensure_ascii=False)
    with _STATS_CACHE_LOCK:
        _STATS_CACHE["body"], _STATS_CACHE["at"] = body, time.monotonic()
    resp = Response(body, mimetype="application/json")
    resp.headers["Cache-Control"] = "public, max-age=30"   # SQLite щадим, свежесть ок
    return resp


_OG_PNG = Path("data/og.png")            # рисует бот раз в сутки (cwd обоих — корень репо)


_LWC_JS = Path(__file__).with_name("webapp_lwc.js")
_CHART_CACHE: dict = {}
_CHART_CACHE_LOCK = threading.Lock()
_CHART_TTL_S = 600.0
_CHART_MAX_BARS = 420
_TF_SEC = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def _chart_candles(symbol: str, tf: str, since_ms: int, limit: int) -> list:
    """OHLCV bybit для графика сделки (отдельная функция — мокается в тестах)."""
    return _exchange().fetch_ohlcv(symbol, tf, since=since_ms, limit=limit)


@app.get("/lwc.js")
def lwc_js() -> Response:
    """Вендоренный lightweight-charts (Apache-2.0) — график сделки без внешних CDN."""
    resp = Response(_LWC_JS.read_bytes(), mimetype="application/javascript")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.get("/api/trade_chart")
def api_trade_chart() -> Response:
    """Свечи ЗАКРЫТОЙ сделки с уровнями и метками входа/выхода («сделка на графике»).

    Только закрытые: их уровни и так публичны (/trackrecord CSV) — открытые остаются
    платным контентом, та же граница публикации, что везде. Кеш 10 мин на сделку:
    эндпоинт публичный, а за ним сетевой fetch_ohlcv."""
    sid_s = request.args.get("id", "")
    if not re.fullmatch(r"[0-9]{1,12}", sid_s):      # isdigit() пускал '²' -> 500
        return _no_store({"ok": False}, 400)
    sid = int(sid_s)
    # приватный слой: валидный подписчик/админ видит и ОТКРЫТЫЕ (уровни он и так
    # видит в «Моём»); публике — только закрытые. Без подписи открытая = 404, как раньше
    user = _auth_user()
    private = False
    if user is not None:
        from app.delivery import payments
        private = payments.is_paid(int(user["id"])) \
            or int(user["id"]) in get_settings().admin_ids
    with _CHART_CACHE_LOCK:
        hit = _CHART_CACHE.get(sid)
        if hit and time.monotonic() - hit[0] < _CHART_TTL_S:
            return _no_store(hit[1])
    row = db.query_one(f"SELECT * FROM signals WHERE id=? AND {_CLOSED}", (sid,))
    is_open = False
    if row is None and private:
        row = db.query_one("SELECT * FROM signals WHERE id=? AND status='open' "
                           "AND result_r IS NULL", (sid,))
        is_open = row is not None
    if row is None:
        return _no_store({"ok": False, "err": "сделка не найдена или ещё открыта"}, 404)
    from datetime import datetime, timedelta

    tf = row["timeframe"] if row["timeframe"] in _TF_SEC else "1h"
    t0 = datetime.fromisoformat(row["created_at"])
    t1 = clock.utcnow() if is_open else datetime.fromisoformat(
        row["closed_at"] or row["created_at"])
    # эскалация ТФ: долгая сделка не влезает в лимит баров — укрупняем свечи,
    # чтобы вход и выход всегда были видны на одном экране
    span = _CHART_MAX_BARS
    for cand in [tf] + [x for x in ("4h", "1d") if _TF_SEC[x] > _TF_SEC[tf]]:
        tf = cand
        span = int((t1 - t0).total_seconds() / _TF_SEC[tf]) + 40
        if span <= _CHART_MAX_BARS:
            break
    since = t0 - timedelta(seconds=30 * _TF_SEC[tf])
    try:
        raw = _chart_candles(row["symbol"], tf, int(since.timestamp() * 1000),
                             min(_CHART_MAX_BARS + 60, span + 40))
    except Exception:                                     # noqa: BLE001 — сеть/делистинг
        raw = None
    if not raw:
        return _no_store({"ok": False, "err": "биржа не отдала свечи"}, 502)
    payload = {
        "ok": True, "id": sid, "symbol": row["symbol"].split("/")[0],
        "side": row["side"], "tf_signal": row["timeframe"], "tf": tf,
        "entry": row["entry"], "stop": row["stop"],
        "tps": [row[k] for k in ("tp1", "tp2", "tp3") if row[k]],
        "exit_price": row["exit_price"], "result_r": row["result_r"],
        "status": row["status"], "filled_at": row["filled_at"],
        "closed_at": row["closed_at"], "created_at": row["created_at"],
        "open": is_open, "in_pos": bool(row["filled_at"]),
        "candles": [[int(c[0] / 1000), c[1], c[2], c[3], c[4]] for c in raw],
    }
    if is_open:
        return _no_store(payload)                         # живая — не кешируем (uR по свечам)
    with _CHART_CACHE_LOCK:
        if len(_CHART_CACHE) > 64:                        # публичный эндпоинт: кеш не пухнет
            _CHART_CACHE.clear()
        _CHART_CACHE[sid] = (time.monotonic(), payload)
    return _no_store(payload)


_BT_JSON = Path(__file__).with_name("backtest_monthly.json")
_BT_CACHE: dict = {"mtime": 0.0, "body": None}
# публичные поля секции; всё прочее (в т.ч. разбивка по сетапам — закрытая таксономия)
# наружу не уходит, даже если окажется в файле (утечка by_setup 05.09–05.10.2026)
_BT_PUBLIC_KEYS = ("generated_at", "fee_pct", "symbols", "tfs", "period", "totals",
                   "months", "max_dd_window")


@app.get("/api/backtest")
def api_backtest() -> Response:
    """Помесячный бэктест текущей конфигурации (scripts/backtest_monthly.py) —
    статический JSON из репо, НЕ живой журнал: секция дашборда подписана как модель."""
    if not runtime.backtest_public():
        # снят с витрины до решения владельца: старая модель завышала итог ~8× (05.10)
        return _no_store({"ok": False, "err": "бэктест на пересчёте"}, 404)
    try:
        mtime = _BT_JSON.stat().st_mtime
    except OSError:
        return _no_store({"ok": False, "err": "бэктест не собран"}, 404)
    with _STATS_CACHE_LOCK:
        if _BT_CACHE["body"] is None or _BT_CACHE["mtime"] != mtime:
            raw = json.loads(_BT_JSON.read_text(encoding="utf-8"))
            public = {k: raw[k] for k in _BT_PUBLIC_KEYS if k in raw}
            _BT_CACHE["body"] = json.dumps(public, ensure_ascii=False).encode("utf-8")
            _BT_CACHE["mtime"] = mtime
        body = _BT_CACHE["body"]
    resp = Response(body, mimetype="application/json")
    # меняется только релизом; клиент ревалидирует (cache: no-cache в index)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.get("/healthz")
def healthz() -> dict:
    """Лёгкий пульс для сторожа туннеля (deploy/tunnel_webapp.sh) и мониторинга:
    без БД и рендера — отвечает, пока жив процесс waitress."""
    return {"ok": True}


@app.get("/og.png")
def og_png() -> Response:
    if not _OG_PNG.exists():
        return Response(status=404)
    resp = Response(_OG_PNG.read_bytes(), mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.get("/")
def index() -> Response:
    html = _INDEX.read_text(encoding="utf-8")
    base = runtime.webapp_url()
    if base.startswith("https://") and _OG_PNG.exists():
        # превью ссылки с эквити: og:image требует АБСОЛЮТНЫЙ url — подставляем из
        # настроек на лету, статический файл не привязан к хосту деплоя
        html = html.replace('<meta name="twitter:card" content="summary">',
                            '<meta name="twitter:card" content="summary_large_image">')
        html = html.replace("</title>", f'</title>\n<meta property="og:image" '
                                        f'content="{base.rstrip("/")}/og.png">')
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp
