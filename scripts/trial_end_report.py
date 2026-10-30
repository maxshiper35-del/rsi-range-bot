"""Цифры для отчёта по концу пробных дней (задание docs/ops/2026-11-03-trial-end-report.md).

Только чтение: БД открывается mode=ro, лог бота читается как текст, .env — только ключ
TELEGRAM_ADMIN_IDS (админов и их тестовые оплаты из цифр исключаем, значения не печатаем).
Печатает markdown в stdout — основа для docs/reports/<дата>-trial-end.md.

    .venv/bin/python scripts/trial_end_report.py [--db data/signals.db] [--log logs/bot.log]
                                                 [--now 2026-11-03T09:00:00+00:00]
"""
from __future__ import annotations

import argparse
import collections
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOAL_PAYING = 10            # платящих к 30.11 (план ноября)
GOAL_RETAINED = 0.70        # доля оставшихся после триала (не заблокировали бота)

# «Платил» после начала триала — как payments.paid_ever_sql, но с отсечкой по времени:
# оплата/подтверждённая USDT-заявка не раньше trial_granted_at
_PAID_AFTER_TRIAL = (
    "(EXISTS(SELECT 1 FROM payments p WHERE p.tg_id=u.tg_id AND p.status='paid' "
    "AND p.paid_at >= u.trial_granted_at) "
    "OR EXISTS(SELECT 1 FROM usdt_claims c WHERE c.tg_id=u.tg_id "
    "AND c.resolved_at >= u.trial_granted_at))")


def _admin_ids(env: Path) -> set[int]:
    try:
        for line in env.read_text().splitlines():
            if line.startswith("TELEGRAM_ADMIN_IDS="):
                raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                return {int(x) for x in raw.split(",") if x.strip().isdigit()}
    except OSError:
        pass
    return set()


def _pct(a: int, b: int) -> str:
    return f"{a / b:.0%}" if b else "—"


def trials(conn: sqlite3.Connection, now: str, admins: set[int]) -> list[sqlite3.Row]:
    rows = conn.execute(
        f"SELECT u.tg_id, u.acquisition_source AS src, u.trial_granted_at, u.paid_until, "
        f"u.is_active, u.tier, {_PAID_AFTER_TRIAL} AS converted "
        "FROM users u WHERE u.trial_granted_at IS NOT NULL").fetchall()
    return [r for r in rows if r["tg_id"] not in admins]


def funnel_lines(rows: list[sqlite3.Row], now: str) -> list[str]:
    ended = [r for r in rows if r["paid_until"] and r["paid_until"] <= now]
    running = [r for r in rows if r["paid_until"] and r["paid_until"] > now
               and not r["converted"]]
    conv = [r for r in ended if r["converted"]]
    blocked = [r for r in ended if not r["converted"] and not r["is_active"]]
    stayed = [r for r in ended if not r["converted"] and r["is_active"]]
    out = [
        "| | людей | доля |", "|---|---:|---:|",
        f"| триал закончился | {len(ended)} | |",
        f"| оплатили после начала триала | {len(conv)} | {_pct(len(conv), len(ended))} |",
        f"| заблокировали бота | {len(blocked)} | {_pct(len(blocked), len(ended))} |",
        f"| остались в бесплатном | {len(stayed)} | {_pct(len(stayed), len(ended))} |",
        f"| триал ещё идёт | {len(running)} | |",
    ]
    return out


def cohorts(rows: list[sqlite3.Row], now: str) -> list[str]:
    by: dict[str, list[sqlite3.Row]] = collections.defaultdict(list)
    for r in rows:
        if r["paid_until"] and r["paid_until"] <= now:
            by[r["src"] or "—"].append(r)
            if (r["src"] or "").startswith("gramads"):
                by["gramads (все)"].append(r)
    out = ["| источник | закончили | оплатили | заблокировали | остались |",
           "|---|---:|---:|---:|---:|"]
    for src, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        conv = sum(1 for r in rs if r["converted"])
        blk = sum(1 for r in rs if not r["converted"] and not r["is_active"])
        out.append(f"| {src} | {len(rs)} | {conv} | {blk} | {len(rs) - conv - blk} |")
    return out


def payments_by_day(conn: sqlite3.Connection, now: datetime, admins: set[int]) -> list[str]:
    since = (now - timedelta(days=7)).isoformat()
    out = ["| день | Stars, оплат | Stars, ⭐ | USDT-заявок подтверждено |", "|---|---:|---:|---:|"]
    days: dict[str, list[float]] = collections.defaultdict(lambda: [0, 0.0, 0])
    for r in conn.execute("SELECT tg_id, amount, paid_at FROM payments WHERE status='paid' "
                          "AND method='stars' AND paid_at >= ?", (since,)):
        if r["tg_id"] in admins:
            continue
        d = days[r["paid_at"][:10]]
        d[0] += 1
        d[1] += r["amount"]
    for r in conn.execute("SELECT tg_id, resolved_at FROM usdt_claims "
                          "WHERE resolved_at >= ?", (since,)):
        if r["tg_id"] not in admins:
            days[r["resolved_at"][:10]][2] += 1
    for day in sorted(days):
        n, stars, usdt = days[day]
        out.append(f"| {day} | {int(n)} | {stars:g} | {int(usdt)} |")
    if len(out) == 2:
        out.append("| — | 0 | 0 | 0 |")
    return out


def paying_total(conn: sqlite3.Connection, admins: set[int]) -> int:
    ids = {r[0] for r in conn.execute("SELECT tg_id FROM payments WHERE status='paid'")}
    ids |= {r[0] for r in conn.execute(
        "SELECT tg_id FROM usdt_claims WHERE resolved_at IS NOT NULL")}
    return len(ids - admins)


_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2})T")


def delivery_errors(log: Path, since: str) -> list[str]:
    """Сбои сообщения о конце доступа (_mt_expire пишет «expire notify <id>: <ошибка>») и
    падения блока «истечение подписок» целиком (MaintenanceGuard)."""
    per_day: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        return [f"лог {log} не прочитан"]
    for line in lines:
        m = _LOG_TS.match(line)
        if not m or m.group(1) < since[:10]:
            continue
        if " expire notify " in line:
            kind = "заблокировал бота" if "blocked by the user" in line else "другая ошибка"
            per_day[m.group(1)][kind] += 1
        elif "блок «истечение подписок» упал" in line:
            per_day[m.group(1)]["блок упал целиком"] += 1
    if not per_day:
        return ["Ошибок доставки в логе нет."]
    return [f"- {d}: " + ", ".join(f"{k} — {v}" for k, v in c.items())
            for d, c in sorted(per_day.items())]


def rating(conn: sqlite3.Connection) -> list[str]:
    try:
        chans = conn.execute("SELECT id, letter, status, last_run_at, last_error "
                             "FROM rating_channels ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        return ["Таблиц рейтинга нет (миграция 030 не применена)."]
    out = ["| канал | статус | обход | сигналов | проверено | TP1 | стоп первым | итог R |",
           "|---|---|---|---:|---:|---:|---:|---:|"]
    for c in chans:
        run = conn.execute("SELECT * FROM rating_runs WHERE channel_id=? ORDER BY run_at DESC "
                           "LIMIT 1", (c["id"],)).fetchone()
        err = " (ошибка)" if c["last_error"] else ""
        if run is None:
            out.append(f"| {c['letter'] or '?'} | {c['status']}{err} | — | | | | | |")
            continue
        out.append(f"| {c['letter'] or '?'} | {c['status']}{err} | {run['run_at'][:16]} | "
                   f"{run['signals']} | {run['checked']} | {run['tp1']} | {run['stop_first']} | "
                   f"{run['r_total']:+.2f} |")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "signals.db"))
    ap.add_argument("--log", default=str(ROOT / "logs" / "bot.log"))
    ap.add_argument("--env", default=str(ROOT / ".env"))
    ap.add_argument("--now", help="ISO UTC; по умолчанию — сейчас")
    ap.add_argument("--since", default="2026-10-28", help="с какого дня смотреть лог")
    a = ap.parse_args()
    now_dt = (datetime.fromisoformat(a.now) if a.now else datetime.now(timezone.utc))
    now = now_dt.isoformat()
    admins = _admin_ids(Path(a.env))
    conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = trials(conn, now, admins)
    ended = [r for r in rows if r["paid_until"] and r["paid_until"] <= now]
    retained = sum(1 for r in ended if r["converted"] or r["is_active"])
    paying = paying_total(conn, admins)
    print(f"# Конец пробных дней — цифры на {now[:16]} UTC\n")
    print(f"Админы исключены: {len(admins)} id.\n")
    print("## Воронка триала\n")
    print("\n".join(funnel_lines(rows, now)))
    print("\n## Когорты по источнику (закончившие триал)\n")
    print("\n".join(cohorts(rows, now)))
    print("\n## Оплаты за 7 дней (без админов)\n")
    print("\n".join(payments_by_day(conn, now_dt, admins)))
    print("\n## Доставка сообщения о конце доступа\n")
    print("\n".join(delivery_errors(Path(a.log), a.since)))
    print("\n## /rating — последний обход\n")
    print("\n".join(rating(conn)))
    print("\n## Цели\n")
    print(f"- Платящих всего (без админов): {paying} из {GOAL_PAYING} к 30.11.")
    print(f"- Остались после триала (оплатили или не заблокировали): {retained} из "
          f"{len(ended)} — {_pct(retained, len(ended))} при цели {GOAL_RETAINED:.0%}.")


if __name__ == "__main__":
    main()
