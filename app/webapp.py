"""Mini App «Живой дашборд» — Flask-страница + JSON API поверх журнала (read-only).

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
        "SELECT symbol, side, timeframe, entry, stop, created_at, filled_at "
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
        out.append({"symbol": r["symbol"].split("/")[0], "side": r["side"],
                    "tf": r["timeframe"], "age_h": age_h, "in_pos": in_pos,
                    "entry": r["entry"], "stop": r["stop"], "ur": ur})
    return out


@app.get("/api/me")
def api_me() -> Response:
    """Платный слой для валидного подписчика/админа. Без валидной подписи — 401;
    ответ всегда персональный -> no-store (никаких промежуточных кешей).
    initData едет в Authorization: Caddy в access-логах редактирует именно этот
    заголовок — PII и 24-часовой replay-токен не осядут в логах (ревью 13.08)."""
    cfg = get_settings()
    auth = request.headers.get("Authorization", "")
    raw = auth[4:] if auth.startswith("tma ") else ""
    user = _validate_init_data(raw, cfg.tg_bot_token)
    if user is None:
        resp = Response(json.dumps({"ok": False}), status=401,
                        mimetype="application/json")
        resp.headers["Cache-Control"] = "no-store"
        return resp
    uid = int(user["id"])
    from app.delivery import payments
    private = payments.is_paid(uid) or uid in cfg.admin_ids
    payload: dict = {"ok": True, "private": private}
    if private:
        since = _paid_since(uid)
        payload["positions"] = _open_signals_private()
        payload["since"] = (since or "")[:10]
        payload["since_stats"] = journal.stats_since_created(since) if since else None
    resp = Response(json.dumps(payload, ensure_ascii=False),
                    mimetype="application/json")
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
    }
    resp = Response(json.dumps(payload, ensure_ascii=False),
                    mimetype="application/json")
    resp.headers["Cache-Control"] = "public, max-age=30"   # SQLite щадим, свежесть ок
    return resp


@app.get("/")
def index() -> Response:
    resp = Response(_INDEX.read_text(encoding="utf-8"), mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp
