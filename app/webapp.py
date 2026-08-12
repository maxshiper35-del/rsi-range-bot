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
    """Открытые сигналы БЕЗ уровней (вход/стоп/цели — платный контент)."""
    now = clock.utcnow()
    out = []
    for r in db.query("SELECT symbol, side, timeframe, snapshot_json, created_at "
                      "FROM signals WHERE status='open' AND result_r IS NULL "
                      "ORDER BY created_at DESC"):
        try:
            setup = json.loads(r["snapshot_json"] or "{}").get("setup", "?")
        except (ValueError, TypeError):
            setup = "?"
        age_h = None
        if r["created_at"]:
            from datetime import datetime
            age_h = round((now - datetime.fromisoformat(r["created_at"]))
                          .total_seconds() / 3600, 1)
        out.append({"symbol": r["symbol"].split("/")[0], "side": r["side"],
                    "tf": r["timeframe"], "setup": setup, "age_h": age_h})
    return out


def _setup_rows(since: str | None) -> list:
    by = journal.stats_by_setup(since_created=since)
    rows = [{"setup": k, "trades": v.get("trades", 0),
             "total_r": round(v.get("total_r", 0.0), 2)}
            for k, v in by.items() if v.get("trades")]
    return sorted(rows, key=lambda x: -abs(x["total_r"]))


@app.get("/api/stats")
def api_stats() -> Response:
    epoch = runtime.strategy_epoch()
    payload = {
        "generated_at": clock.utcnow_iso(),
        "epoch_since": (epoch or "")[:10],
        "all": journal.stats(),
        "epoch": journal.stats_since_created(epoch) if epoch else None,
        "by_setup_epoch": _setup_rows(epoch),
        "by_setup_all": _setup_rows(None),
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
