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
_INDEX = Path(__file__).with_name("webapp_index.html")

# ------------------- приватный режим (подписчики) -------------------
# Аутентификация — подпись initData Telegram Mini App (HMAC-SHA256 на токене бота):
# страница остаётся публичной, но /api/me отдаёт платный слой (уровни открытых,
# unrealized R) только валидному активному подписчику или админу. Сетапы не отдаются
# НИКОМУ и здесь — граница публикации стратегии единая для всех поверхностей.

_AUTH_MAX_AGE_S = 24 * 3600            # replay-защита: initData старше суток не принимаем


def _validate_init_data(raw: str, bot_token: str) -> dict | None:
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
    if clock.utcnow().timestamp() - auth_ts > _AUTH_MAX_AGE_S:
        return None
    try:
        user = json.loads(pairs.get("user", ""))
    except ValueError:
        return None
    return user if isinstance(user, dict) and "id" in user else None


_TICKERS: dict = {"at": 0.0, "ok_at": 0.0, "prices": {}}
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


def _fetch_tickers(symbols: list[str]) -> dict[str, float] | None:
    """Сетевой фетч БЕЗ лока кеша. None = попытка не удалась (кеш не трогаем).
    Неизвестные бирже символы (делистинг) отфильтровываются, а не роняют остальных."""
    try:
        ex = _exchange()
        known = [s for s in symbols if s in ex.markets]
        if not known:
            return {}
        data = ex.fetch_tickers(known)
        return {s: float(t["last"]) for s, t in data.items() if t.get("last")}
    except Exception:                                     # noqa: BLE001
        return None


def _last_prices(symbols: list[str]) -> dict[str, float]:
    """last-цены bybit (тот же источник, что у скоринга) с кешем 45с.

    Свежесть — ТОЛЬКО по времени попытки: пропавший из ответа символ не выключает
    кеш (ревью 13.08: иначе один делистнутый тикер превращал каждый /api/me в сетевой
    цикл). Попытку столбим ДО фетча — конкуренты не дублируют её; сам фетч вне лока —
    /api/stats и публичная страница не ждут биржу. Успех мержится в кеш (частичный
    ответ не стирает прежние цены); кеш старше 10 мин не отдаём — протухший
    unrealized R хуже честного прочерка."""
    with _TICKERS_LOCK:
        mono = time.monotonic()
        if mono - _TICKERS["at"] < _TICKERS_TTL_S:
            stale = mono - _TICKERS["ok_at"] > _PRICES_MAX_STALE_S
            return {} if stale else dict(_TICKERS["prices"])
        _TICKERS["at"] = mono
    got = _fetch_tickers(symbols)                         # сеть — вне лока
    with _TICKERS_LOCK:
        if got is not None:
            _TICKERS["prices"].update(got)
            _TICKERS["ok_at"] = time.monotonic()
        stale = time.monotonic() - _TICKERS["ok_at"] > _PRICES_MAX_STALE_S
        return {} if stale else dict(_TICKERS["prices"])


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
    """Среднее удержание закрытых сделок (филл -> закрытие), часов."""
    q = ("SELECT AVG((julianday(closed_at) - julianday(filled_at)) * 24.0) AS h "
         "FROM signals WHERE filled_at IS NOT NULL AND closed_at IS NOT NULL "
         "AND result_r IS NOT NULL ")
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
        "SELECT id, symbol, side, timeframe, entry, stop, created_at, filled_at "
        "FROM signals WHERE status='open' AND result_r IS NULL "
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
        out.append({"id": r["id"], "symbol": r["symbol"].split("/")[0],
                    "side": r["side"], "tf": r["timeframe"], "age_h": age_h,
                    "in_pos": in_pos, "entry": r["entry"], "stop": r["stop"],
                    "ur": ur})
    return out


def _no_store(payload: dict, status: int = 200) -> Response:
    """Персональные ответы не кешируются нигде — в том числе промежуточным Caddy."""
    resp = Response(json.dumps(payload, ensure_ascii=False), status=status,
                    mimetype="application/json")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _auth_user() -> dict | None:
    """user из Authorization: tma <initData> | None. initData едет в Authorization:
    Caddy в access-логах редактирует именно этот заголовок — PII и 24-часовой
    replay-токен не осядут в логах (ревью 13.08)."""
    auth = request.headers.get("Authorization", "")
    raw = auth[4:] if auth.startswith("tma ") else ""
    return _validate_init_data(raw, get_settings().tg_bot_token)


def _taken_ids(tg_id: int) -> list[int]:
    return [r["signal_id"] for r in db.query(
        "SELECT signal_id FROM user_taken WHERE tg_id=?", (tg_id,))]


_CLOSED = "status IN ('tp1','tp2','tp3','stopped','expired') AND result_r IS NOT NULL"


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


# ------------------- админ-панель (v5: пульт управления) -------------------
# Первый АДМИНСКИЙ пишущий контур на публичном вебе: гейт — та же подпись initData
# плюс uid в admin_ids; настройки меняются ТОЛЬКО по белому списку с валидацией
# (произвольный ключ в settings — это инъекция в конфиг бота, запрещено схемой).

_ADMIN_SETTINGS: dict[str, dict] = {
    "paused":            {"t": "bool", "label": "Пауза сигналов", "group": "Сигналы"},
    "min_confidence":    {"t": "float", "min": 0.0, "max": 1.0,
                          "label": "Мин. confidence", "group": "Сигналы"},
    "max_open_per_side": {"t": "int", "min": 0, "max": 50,
                          "label": "Лимит позиций на сторону", "group": "Сигналы"},
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
    "executor_enabled":  {"t": "bool", "label": "Executor (демо BingX)", "group": "Прочее"},
}


def _admin_uid() -> int | None:
    user = _auth_user()
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
        "channel_enabled": runtime.channel_enabled(), "channel_mode": runtime.channel_mode(),
        "sub_price_stars": runtime.price_stars(), "sub_days": runtime.sub_days(),
        "free_signals_per_week": runtime.free_per_week(),
        "usdt_bonus_days": runtime.usdt_bonus_days(),
        "affiliate_url": runtime.affiliate_url(),
        "executor_enabled": runtime.executor_enabled(),
    }
    return [{"key": k, "value": cur[k], "label": v["label"], "t": v["t"],
             "group": v["group"], **({"choices": v["choices"]} if "choices" in v else {}),
             **({"min": v["min"], "max": v["max"]} if "min" in v else {})}
            for k, v in _ADMIN_SETTINGS.items()]


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
    last_pays = [dict(r) for r in db.query(
        "SELECT tg_id, method, amount, currency, days, paid_at FROM payments "
        "WHERE status='paid' ORDER BY paid_at DESC LIMIT 10")]
    now_iso = clock.utcnow_iso()
    paid_now = db.query_one(
        "SELECT COUNT(*) AS n FROM users WHERE paid_until > ?", (now_iso,)) or {"n": 0}
    lapsed = db.query_one(
        "SELECT COUNT(*) AS n FROM users WHERE tier='paid' AND paid_until <= ?",
        (now_iso,)) or {"n": 0}
    recent_users = [dict(r) for r in db.query(
        "SELECT tg_id, username, tier, paid_until, joined_at FROM users "
        "ORDER BY joined_at DESC LIMIT 15")]
    open_full = [dict(r) for r in db.query(
        "SELECT id, symbol, side, timeframe, entry, stop, created_at, filled_at "
        "FROM signals WHERE status='open' AND result_r IS NULL ORDER BY created_at DESC")]
    payload = {
        "ok": True,
        "health": {
            "poller_tick_min": age_min(runtime.last_poller_tick_at()),
            "backup_min": age_min(runtime.last_backup_at()),
            "webapp_url": runtime.webapp_url(),
            "executor_enabled": runtime.executor_enabled(),
            "paused": runtime.paused(),
        },
        "users": {"counts": payments.user_counts(), "paid_now": paid_now["n"],
                  "lapsed": lapsed["n"], "acquisition": payments.acquisition_breakdown(),
                  "recent": recent_users},
        "money": {"payments_n": pays["n"], "payments_sum": round(float(pays["s"]), 2),
                  "last": last_pays},
        "settings": _admin_settings_payload(),
        "open_signals": open_full,
    }
    return _no_store(payload)


@app.post("/api/admin/set")
def api_admin_set() -> Response:
    """Изменение ОДНОЙ настройки по белому списку. 400 — ключ/значение вне схемы."""
    uid = _admin_uid()
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
    return _no_store({"ok": True, "settings": _admin_settings_payload()})


@app.post("/api/admin/user")
def api_admin_user() -> Response:
    """grant/revoke подписки из пульта (те же пути, что /grant и /revoke в боте)."""
    uid = _admin_uid()
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
            or not 0 < days <= 365):
        return _no_store({"ok": False}, 400)
    from app.delivery import payments
    if action == "grant":
        payments.admin_grant(tg_id, int(days))
    else:
        payments.admin_revoke(tg_id)
    return _no_store({"ok": True})


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
    return _no_store(payload)


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


@app.get("/api/stats")
def api_stats() -> Response:
    epoch = runtime.strategy_epoch()
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
    }
    resp = Response(json.dumps(payload, ensure_ascii=False),
                    mimetype="application/json")
    resp.headers["Cache-Control"] = "public, max-age=30"   # SQLite щадим, свежесть ок
    return resp


_OG_PNG = Path("data/og.png")            # рисует бот раз в сутки (cwd обоих — корень репо)


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
