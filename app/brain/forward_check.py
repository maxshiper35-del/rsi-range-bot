"""Проверка решений вперёд — прогресс по правилам docs/measurements/2026-10-24-forward-rules.md.

Чистые расчёты по журналу и executor_orders (без сети): еженедельный DM админу и, при
желании, дашборд. Пороги — константы ниже, записаны ДО данных; решение за владельцем.
"""
from __future__ import annotations

from datetime import datetime

from app import db

GATE_SINCE = "2026-10-06T22:00:00+00:00"        # late_entry_gate=entry включён
SHADOW_4H_SINCE = "2026-10-05T00:00:00+00:00"   # LTF-BB@4h в тени
EXEC_SINCE = "2026-10-19T23:30:00+00:00"        # трейл переставляется на бирже

GATE_MIN_N, GATE_MIN_WEEKS = 40, 6
SHADOW_MIN_N, SHADOW_MIN_DAYS = 60, 90
SHADOW_OFF, SHADOW_BACK = 0.0, 0.05
EXEC_MIN_N, EXEC_MIN_EXP, EXEC_MAX_DD, EXEC_MIN_GAP = 60, 0.05, 20.0, -0.05

_CLOSED = ("status IN ('tp1','tp2','tp3','stopped','expired') AND result_r IS NOT NULL")


def _agg(rs: list[float]) -> dict:
    n = len(rs)
    return {"n": n, "sum": round(sum(rs), 2), "exp": round(sum(rs) / n, 3) if n else None}


def _shadow_rs(reason: str, since: str) -> list[float]:
    return [r["result_r"] for r in db.query(
        "SELECT result_r FROM signals WHERE status='rejected' AND result_r IS NOT NULL "
        "AND created_at >= ? AND snapshot_json LIKE ?", (since, f'%"shadow": "{reason}"%'))]


def _max_dd(rs: list[float]) -> float:
    peak = cum = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return round(dd, 2)


def gate_check(now: datetime) -> dict:
    pub = [r["result_r"] for r in db.query(
        f"SELECT result_r FROM signals WHERE {_CLOSED} AND created_at >= ?", (GATE_SINCE,))]
    sh = _shadow_rs("late_entry", GATE_SINCE)
    weeks = (now - datetime.fromisoformat(GATE_SINCE)).days / 7
    a, b = _agg(pub), _agg(sh)
    ready = b["n"] >= GATE_MIN_N and weeks >= GATE_MIN_WEEKS
    verdict = None
    if ready and a["exp"] is not None and b["exp"] is not None:
        verdict = "keep" if b["exp"] < a["exp"] else "review"
    return {"published": a, "shadow": b, "weeks": round(weeks, 1), "ready": ready,
            "verdict": verdict}


def shadow_4h_check(now: datetime) -> dict:
    sh = _agg(_shadow_rs("setup_off", SHADOW_4H_SINCE))
    days = (now - datetime.fromisoformat(SHADOW_4H_SINCE)).days
    ready = sh["n"] >= SHADOW_MIN_N and days >= SHADOW_MIN_DAYS
    verdict = None
    if ready and sh["exp"] is not None:
        verdict = ("off" if sh["exp"] <= SHADOW_OFF else
                   "back" if sh["exp"] >= SHADOW_BACK else "continue")
    return {**sh, "days": days, "ready": ready, "verdict": verdict}


def exec_check() -> dict:
    """Реальный R executor по ценам исполнения против R журнала, сделки после трейла на бирже."""
    real, gaps = [], []
    for r in db.query(
            "SELECT eo.side, eo.qty, eo.fill_px, eo.close_px, eo.fill_fee, eo.close_fee, "
            "s.entry, s.stop, s.result_r FROM executor_orders eo "
            "JOIN signals s ON s.id = eo.signal_id WHERE eo.state='closed' "
            "AND eo.fill_px IS NOT NULL AND eo.close_px IS NOT NULL AND s.created_at >= ? "
            "ORDER BY eo.closed_at", (EXEC_SINCE,)):
        risk = abs(r["entry"] - r["stop"])
        if not risk or not r["qty"]:
            continue
        sg = 1 if r["side"] == "LONG" else -1
        fees = abs((r["fill_fee"] or 0) + (r["close_fee"] or 0)) / (risk * r["qty"])
        x = sg * (r["close_px"] - r["fill_px"]) / risk - fees
        real.append(x)
        gaps.append(x - (r["result_r"] or 0))
    a = _agg(real)
    gap = round(sum(gaps) / len(gaps), 3) if gaps else None
    dd = _max_dd(real)
    ready = a["n"] >= EXEC_MIN_N
    verdict = None
    if ready and a["exp"] is not None and gap is not None:
        ok = a["exp"] >= EXEC_MIN_EXP and dd <= EXEC_MAX_DD and gap >= EXEC_MIN_GAP
        verdict = "live_candidate" if ok else ("review" if a["exp"] <= 0 else "demo")
    return {**a, "dd": dd, "gap": gap, "ready": ready, "verdict": verdict}


def _fmt(v: float | None, d: int = 2) -> str:
    if v is None:
        return "—"
    return ("+" if v > 0 else "−" if v < 0 else "") + f"{abs(v):.{d}f}R"


def report(now: datetime) -> str:
    """Текст еженедельного DM «Проверка решений» (HTML)."""
    g, s, e = gate_check(now), shadow_4h_check(now), exec_check()
    gv = {"keep": "✅ гейт работает — оставить", "review": "⚠️ гейт не помогает — пересмотреть",
          None: f"копим данные: нужно ≥{GATE_MIN_N} в тени и ≥{GATE_MIN_WEEKS} нед."}[g["verdict"]]
    wait_sh = f"копим данные: нужно ≥{SHADOW_MIN_N} закрытых и ≥{SHADOW_MIN_DAYS} дн."
    sv = {"off": "⛔ выключить насовсем", "back": "✅ вернуть в публикацию",
          "continue": "продолжать тень", None: wait_sh}[s["verdict"]]
    ev = {"live_candidate": "✅ кандидат на живые деньги (маленький размер — ваше решение)",
          "review": "⚠️ реальный итог ≤ 0 — пересмотр стратегии",
          "demo": "продолжать демо — порог не пройден",
          None: f"копим данные: нужно ≥{EXEC_MIN_N} закрытых"}[e["verdict"]]
    return (
        "🧪 <b>Проверка решений вперёд</b> (правила от 24.10, пороги не меняются)\n\n"
        f"<b>1. Гейт «вход уже пройден»</b> · {g['weeks']} нед.\n"
        f"опубликованные: {g['published']['n']} сд. · {_fmt(g['published']['sum'], 1)} · "
        f"{_fmt(g['published']['exp'], 3)}/сд.\n"
        f"отсечённые (тень): {g['shadow']['n']} сд. · {_fmt(g['shadow']['sum'], 1)} · "
        f"{_fmt(g['shadow']['exp'], 3)}/сд.\n→ {gv}\n\n"
        f"<b>2. Тень LTF-BB@4h</b> · {s['days']} дн.\n"
        f"{s['n']} сд. · {_fmt(s['sum'], 1)} · {_fmt(s['exp'], 3)}/сд.\n→ {sv}\n\n"
        f"<b>3. Executor (демо), после трейла на бирже</b>\n"
        f"{e['n']} сд. · реально {_fmt(e['sum'], 1)} · {_fmt(e['exp'], 3)}/сд. · "
        f"просадка {e['dd']:.1f}R · отрыв от журнала {_fmt(e['gap'], 3)}/сд.\n→ {ev}")
