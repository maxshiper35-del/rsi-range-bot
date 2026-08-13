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

import json
from pathlib import Path

from flask import Flask, Response

from app import clock, db, runtime
from app.brain import journal

app = Flask(__name__)
_INDEX = Path(__file__).with_name("webapp_index.html")


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
