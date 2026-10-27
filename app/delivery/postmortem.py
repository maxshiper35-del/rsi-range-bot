"""Разбор каждой закрытой сделки для владельца: что сделал рынок и почему исход такой.

Запрос владельца 27.10.2026: «разбирать каждую сделку — минусовую и плюсовую, почему график
пошёл туда». Разовый разбор 189 живых сделок (09.08–24.10) показал, что минусы делятся на
устойчивые типы, а самая дорогая утечка — «был в плюсе ≥ +1R и развернулся в стоп» (49 из
112 стопов). Здесь — те же категории автоматически: ежедневный разбор админам и /why.

Метрики считаются так же, как живой судья (journal._outcome_from_bars): ход в пользу (MFE)
— по барам ПОСЛЕ бара филла до бара выхода включительно; бар филла не в счёт (выгодный
ход мог случиться до исполнения входа). Только факты баров — без догадок «почему».
"""
from __future__ import annotations

import pandas as pd

from app.delivery.templates import esc
from app.delivery.weekly_video import n_trades

CLUSTER_WINDOW_H = 3.0

CATS = {
    "win": ("✅", "трейлинг-выход в плюс"),
    "round_trip": ("↩️", "был в плюсе ≥ +1R и развернулся в стоп"),
    "market": ("🌊", "рынок целиком: ещё стопы той же стороны рядом"),
    "instant": ("⚡", "сразу против: дальше +0.3R не ушла"),
    "chop": ("〰️", "поболтало и выбило"),
}
_ORDER = ("win", "round_trip", "chop", "market", "instant")


def classify(result_r: float, mfe_r: float, cluster: int) -> str:
    """Категория исхода. Порядок важен: плюс → «был ≥+1R» → кластер рынка → «сразу» → прочее."""
    if result_r > 0:
        return "win"
    if mfe_r >= 1.0:
        return "round_trip"
    if cluster >= 2:
        return "market"
    if mfe_r < 0.3:
        return "instant"
    return "chop"


def _ts(v) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def excursions(row, bars: pd.DataFrame) -> tuple[float, float] | None:
    """(MFE, MAE) в R по 1h-барам строго после бара филла до бара выхода. None — нет баров."""
    if bars is None or bars.empty or not row["closed_at"]:
        return None
    entry, stop = float(row["entry"]), float(row["stop"])
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    dts = pd.to_datetime(bars["dt"], utc=True)
    fill = _ts(row["filled_at"] or row["created_at"]).floor("h")
    seg = bars[(dts > fill) & (dts <= _ts(row["closed_at"]))]
    if seg.empty:
        return 0.0, 0.0
    if row["side"] == "LONG":
        return ((float(seg["high"].max()) - entry) / risk,
                (entry - float(seg["low"].min())) / risk)
    return ((entry - float(seg["low"].min())) / risk,
            (float(seg["high"].max()) - entry) / risk)


def btc_move_pct(btc: pd.DataFrame, start, end) -> float | None:
    """Ход BTC за время сделки, % (от открытия первого часа до закрытия последнего)."""
    if btc is None or btc.empty:
        return None
    dts = pd.to_datetime(btc["dt"], utc=True)
    seg = btc[(dts >= _ts(start).floor("h")) & (dts <= _ts(end))]
    if seg.empty:
        return None
    return (float(seg["close"].iloc[-1]) / float(seg["open"].iloc[0]) - 1) * 100


def cluster_count(row, stops: list) -> int:
    """Сколько ДРУГИХ стопов той же стороны закрылось в ±CLUSTER_WINDOW_H от выхода.
    stops — [(id, side, closed_at)] минусовых стопов периода."""
    t = _ts(row["closed_at"])
    return sum(1 for sid, side, closed in stops
               if sid != row["id"] and side == row["side"]
               and abs((_ts(closed) - t).total_seconds()) <= CLUSTER_WINDOW_H * 3600)


def analyze(rows: list, bars: dict, btc: pd.DataFrame | None) -> list[dict]:
    """rows — закрытые опубликованные сделки; bars — {symbol: 1h df}. -> разборы."""
    stops = [(r["id"], r["side"], r["closed_at"]) for r in rows
             if r["status"] == "stopped" and (r["result_r"] or 0) <= 0]
    out = []
    for r in rows:
        ex = excursions(r, bars.get(r["symbol"]))
        mfe, mae = ex if ex else (None, None)
        cl = cluster_count(r, stops)
        res = float(r["result_r"] or 0.0)
        start = r["filled_at"] or r["created_at"]
        btc_mv = btc_move_pct(btc, start, r["closed_at"])
        held = max(0.0, (_ts(r["closed_at"]) - _ts(start)).total_seconds() / 3600)
        out.append({"id": r["id"], "symbol": r["symbol"], "side": r["side"], "r": res,
                    "mfe": mfe, "mae": mae, "cluster": cl, "btc": btc_mv, "hours": held,
                    "cat": classify(res, mfe if mfe is not None else 0.0, cl)})
    return out


def _btc_note(a: dict) -> str:
    if a["btc"] is None:
        return ""
    against = a["btc"] < 0 if a["side"] == "LONG" else a["btc"] > 0
    word = "против нас" if against else "в нашу сторону"
    return f"; BTC {a['btc']:+.1f}% {word}"


def trade_line(a: dict) -> str:
    emo, _ = CATS[a["cat"]]
    base = a["symbol"].split("/")[0]
    if a["cat"] == "win":
        why = f"трейлинг-выход, в позиции {a['hours']:.0f}ч"
    elif a["cat"] == "round_trip":
        why = f"был {a['mfe']:+.2f}R — развернулся в стоп"
    elif a["cat"] == "market":
        why = f"рынок целиком: ещё {a['cluster']} стоп(а) той же стороны за ±3ч"
    elif a["cat"] == "instant":
        why = f"сразу против: максимум {a['mfe'] or 0:+.2f}R, стоп за {a['hours']:.0f}ч"
    else:
        why = f"поболтало: максимум {a['mfe'] or 0:+.2f}R, стоп за {a['hours']:.0f}ч"
    return (f"{emo} #{a['id']} {esc(base)} {esc(a['side'])} <b>{a['r']:+.2f}R</b> — "
            f"{why}{_btc_note(a)}")


def summary_lines(items: list[dict]) -> list[str]:
    """Сводка по категориям + главная утечка (минусовая категория с наибольшим −R)."""
    if not items:
        return ["Сделок нет."]
    agg: dict = {}
    for a in items:
        n, s = agg.get(a["cat"], (0, 0.0))
        agg[a["cat"]] = (n + 1, s + a["r"])
    parts = [f"{CATS[c][0]} {agg[c][0]} ({agg[c][1]:+.1f}R)" for c in _ORDER if c in agg]
    lines = [" · ".join(parts)]
    losses = [(s, c) for c, (n, s) in agg.items() if c != "win"]
    if losses:
        worst_sum, worst = min(losses)
        lines.append(f"Главная утечка: {CATS[worst][0]} «{CATS[worst][1]}» ({worst_sum:+.1f}R).")
    return lines


def render_digest(title: str, day_items: list[dict], week_items: list[dict]) -> str:
    total = sum(a["r"] for a in day_items)
    lines = [f"🔍 <b>{esc(title)}</b> — {n_trades(len(day_items))}, {total:+.2f}R"]
    lines += [trade_line(a) for a in day_items] or ["Закрытых сделок не было."]
    lines.append("━━━━━━━━━━━━━━")
    lines.append("<b>За 7 дней:</b> " + "\n".join(summary_lines(week_items)))
    lines.append("MFE — лучший ход в пользу после входа; R до комиссий.")
    return "\n".join(lines)
