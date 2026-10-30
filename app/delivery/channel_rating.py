"""Рейтинг сигнальных каналов: бот сам раз в сутки проверяет публичные каналы по свечам
биржи (тем же аудитом, что /audit @канал) и ведёт таблицу. Публично — АНОНИМНО.

Зачем (30.10.2026): замеры показали, что перевеса у стратегий сигналов при реальном
исполнении нет (docs/research/strategy-registry.md), а честную сводную статистику по чужим
сигналам не публикует никто (docs/research/2026-10-30-telegram-crypto-scan.md). Проверка —
то, что у нас есть и чего нет у других.

Анонимность — не стеснительность, а юридическая гигиена: в выдаче канал — буква (A, B, C…),
закреплённая за ним навсегда; username и название видит только админ (/rating names).
Хранятся только агрегаты обхода (миграция 030), посты каналов не копируются.

Чистые функции (буквы, строка рейтинга из агрегата аудита, тексты, HTML карточки) — для
тестов; сеть (t.me, свечи) — в боте через asyncio.to_thread, обход — блоком обслуживания
с лимитом каналов за такт: общий IP с прод-ботом, t.me и биржу не дёргаем залпом.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app import clock, db
from app.delivery import channel_audit
from app.delivery.templates import esc

# Первый список — каналы выпусков «Проверка VIP-канала» 30.10 (data/content/batch_20261030/
# MANIFEST.md); канал первого выпуска 29.10 нигде не записан — его нет.
SEED_CHANNELS = ("WallstreetQueenOfficial", "CoinCoachSignals", "piratesignal")
RUN_EVERY_H = 24.0              # канал проверяется не чаще раза в сутки
MAX_PER_TICK = 2                # каналов за такт обслуживания: t.me + до 30 запросов свечей каждый
PAUSE_BETWEEN_S = 90.0          # пауза между каналами внутри такта (вежливо к t.me и бирже)
PAUSE_MANUAL_S = 5.0            # то же при ручном /rating run (админ ждёт ответа)
CANDIDATE_MIN_SIGNALS = 12      # /audit: столько разобранных сигналов — канал в кандидаты
MIN_FILLED_FOR_VERDICT = 10     # меньше — «мало данных», как _SMALL_SAMPLE в аудите
RATING_MAX_ROWS = 15

_COUNTER_RE = re.compile(
    r'tgme_(?:channel_info|header)_counter[^>]*>\s*<span class="counter_value">([^<]+)</span>'
    r'\s*<span class="counter_type">(subscribers|members|подписчик\w*)</span>', re.I)


# ---------------------------------------------------------------------------
# Буквы
# ---------------------------------------------------------------------------

def letter_for(index: int) -> str:
    """0 -> A … 25 -> Z, 26 -> AA … — как столбцы таблицы; буква за каналом навсегда."""
    out = ""
    n = index
    while True:
        out = chr(ord("A") + n % 26) + out
        n = n // 26 - 1
        if n < 0:
            return out


def parse_subscribers(html: str | None) -> int | None:
    """Счётчик подписчиков с веб-превью («12.3K subscribers») -> число | None."""
    m = _COUNTER_RE.search(html or "")
    if not m:
        return None
    raw = m.group(1).strip().replace(" ", "").replace(",", ".")
    mult = 1.0
    if raw[-1:].upper() == "K":
        raw, mult = raw[:-1], 1_000.0
    elif raw[-1:].upper() == "M":
        raw, mult = raw[:-1], 1_000_000.0
    try:
        return int(float(raw) * mult)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Хранение
# ---------------------------------------------------------------------------

def _norm(username: str) -> str:
    return username.strip().lstrip("@")


def _channel(where: str, params: tuple) -> dict:
    row = db.query_one(f"SELECT * FROM rating_channels WHERE {where}", params)
    assert row is not None                                  # вызывается после INSERT/поиска
    return dict(row)


def add_channel(username: str, added_by: str, status: str = "active") -> dict:
    """Добавить канал (или вернуть существующий). Снятый/удалённый админом канал кандидатом
    из /audit заново не делаем — решение админа старше автоматики."""
    name = _norm(username)
    row = db.query_one("SELECT * FROM rating_channels WHERE username=?", (name,))
    now = clock.utcnow_iso()
    if row is None:
        db.execute("INSERT INTO rating_channels(username, status, added_by, added_at, updated_at) "
                   "VALUES (?,?,?,?,?)", (name, status, added_by, now, now))
        return _channel("username=?", (name,))
    if status == "active" and row["status"] in ("candidate", "paused", "removed"):
        db.execute("UPDATE rating_channels SET status='active', updated_at=? WHERE id=?",
                   (now, row["id"]))
    return _channel("id=?", (row["id"],))


def set_status(username: str, status: str) -> bool:
    n = db.execute("UPDATE rating_channels SET status=?, updated_at=? WHERE username=?",
                   (status, clock.utcnow_iso(), _norm(username)))
    return n > 0


def seed_channels() -> int:
    """Первый список — один раз: уже есть хоть одна строка -> ничего не трогаем."""
    if db.query_one("SELECT 1 FROM rating_channels LIMIT 1"):
        return 0
    for name in SEED_CHANNELS:
        add_channel(name, "seed")
    return len(SEED_CHANNELS)


def due_channels(now_iso: str, limit: int = MAX_PER_TICK) -> list[dict]:
    """Активные каналы, которые не проверялись дольше RUN_EVERY_H, самые давние — первыми;
    не больше limit за такт, остальное — следующим тактом/завтра."""
    rows = db.query("SELECT * FROM rating_channels WHERE status='active' "
                    "ORDER BY last_run_at IS NOT NULL, last_run_at ASC, id ASC")
    from datetime import datetime, timedelta
    now = datetime.fromisoformat(now_iso)
    out = []
    for r in rows:
        last = r["last_run_at"]
        if last is None or now - datetime.fromisoformat(last) >= timedelta(hours=RUN_EVERY_H):
            out.append(dict(r))
        if len(out) >= limit:
            break
    return out


def _assign_letter(channel_id: int) -> str:
    row = db.query_one("SELECT letter FROM rating_channels WHERE id=?", (channel_id,))
    if row and row["letter"]:
        return row["letter"]
    used = {r["letter"] for r in db.query("SELECT letter FROM rating_channels "
                                           "WHERE letter IS NOT NULL")}
    i = 0
    while letter_for(i) in used:
        i += 1
    letter = letter_for(i)
    db.execute("UPDATE rating_channels SET letter=? WHERE id=? AND letter IS NULL",
               (letter, channel_id))
    return letter


def record_run(channel_id: int, agg: dict, title: str | None, subscribers: int | None,
               run_at: str | None = None) -> dict:
    """Агрегат аудита -> строка обхода; буква выдаётся при первом успешном обходе."""
    run_at = run_at or clock.utcnow_iso()
    f = agg["filled"]
    # через соединение транзакции: db.execute сам берёт write-lock (не реентрантный)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO rating_runs(channel_id, run_at, posts, signals, checked, filled, tp1, "
            "stop_first, no_fill, open, no_stop, tp1_avg_r, r_total, r_trades, small_sample, "
            "period_from, period_to) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (channel_id, run_at, agg["posts"], agg["signals"], agg["checked"], f, agg["tp1"],
             agg["stop_first"], agg["no_fill"], agg["open"], agg["no_stop"], agg["tp1_avg_r"],
             agg["r_total"], agg["r_trades"], int(0 < f < MIN_FILLED_FOR_VERDICT),
             agg["first"].isoformat() if agg["first"] is not None else None,
             agg["last"].isoformat() if agg["last"] is not None else None))
        conn.execute("UPDATE rating_channels SET last_run_at=?, last_error=NULL, title=?, "
                     "subscribers=COALESCE(?, subscribers), updated_at=? WHERE id=?",
                     (run_at, title, subscribers, run_at, channel_id))
    letter = _assign_letter(channel_id)
    return {"letter": letter, "run_at": run_at}


def record_error(channel_id: int, error: str) -> None:
    """Сбой обхода: помечаем попытку (чтобы канал не забирал каждый такт) и причину."""
    now = clock.utcnow_iso()
    db.execute("UPDATE rating_channels SET last_run_at=?, last_error=?, updated_at=? WHERE id=?",
               (now, error[:200], now, channel_id))


def latest_rows() -> list[dict]:
    """Строки рейтинга: последний обход каждого канала с буквой + предыдущий (динамика).
    Сортировка — по итогу на TP1 за сделку (лучшие сверху), «мало данных» — ниже."""
    rows = db.query(
        "SELECT c.id, c.username, c.letter, c.title, c.subscribers, c.status, "
        "r.run_at, r.posts, r.signals, r.checked, r.filled, r.tp1, r.stop_first, r.no_fill, "
        "r.open, r.no_stop, r.tp1_avg_r, r.r_total, r.r_trades, r.small_sample, "
        "(SELECT p.r_total FROM rating_runs p WHERE p.channel_id=c.id AND p.id<r.id "
        " ORDER BY p.id DESC LIMIT 1) AS prev_r_total "
        "FROM rating_channels c JOIN rating_runs r ON r.id = "
        "(SELECT id FROM rating_runs WHERE channel_id=c.id ORDER BY id DESC LIMIT 1) "
        "WHERE c.letter IS NOT NULL AND c.status IN ('active','paused')")
    out = [dict(r) for r in rows]
    out.sort(key=lambda r: (r["small_sample"] or not r["r_trades"],
                            -(r["r_total"] / r["r_trades"]) if r["r_trades"] else 0.0,
                            r["letter"]))
    return out


def candidate_from_audit(username: str, agg: dict, tg_id: int) -> bool:
    """После /audit: канал с ≥ CANDIDATE_MIN_SIGNALS разобранными сигналами — в кандидаты
    (активируется админом /rating add). Уже известный канал не трогаем."""
    if agg.get("checked", 0) < CANDIDATE_MIN_SIGNALS:
        return False
    name = _norm(username)
    if db.query_one("SELECT 1 FROM rating_channels WHERE username=?", (name,)):
        return False
    add_channel(name, f"audit:{tg_id}", status="candidate")
    return True


def all_channels() -> list[dict]:
    return [dict(r) for r in db.query("SELECT * FROM rating_channels ORDER BY letter IS NULL, "
                                      "letter, id")]


# ---------------------------------------------------------------------------
# Тексты (публичные — без username и названий)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    label: str
    cls: str                    # pos | neg | mute — для карточки


def verdict(row: dict) -> Verdict:
    if row["small_sample"] or not row["r_trades"]:
        return Verdict("мало данных", "mute")
    return Verdict("в плюсе", "pos") if row["r_total"] > 0 else Verdict("в минусе", "neg")


def _subs(n: int | None) -> str:
    if not n:
        return "подписчиков: нет данных"
    if n >= 1_000_000:
        return f"~{n / 1_000_000:.1f} млн подписчиков"
    if n >= 1_000:
        return f"~{round(n / 1_000)} тыс. подписчиков"
    return f"~{n} подписчиков"


def _pct(a: int, b: int) -> str:
    return f"{round(100 * a / b)}%" if b else "—"


def _r(r: float) -> str:
    return channel_audit._r_fmt(r)


def _trend(row: dict) -> str:
    prev = row.get("prev_r_total")
    if prev is None:
        return ""
    d = row["r_total"] - prev
    if abs(d) < 0.05:
        return " · без изменений"
    return f" · {'↑' if d > 0 else '↓'} {d:+.1f}R с прошлой проверки"


def render_rating(rows: list[dict], max_rows: int = RATING_MAX_ROWS) -> str:
    """Публичный /rating (parse_mode=HTML). Каналы — буквы; имён нет по построению: в текст
    попадают только letter, числа и подписчики."""
    if not rows:
        return ("📋 <b>Рейтинг сигнальных каналов</b>\n\nПервая проверка ещё идёт — таблица "
                "появится после обхода. Проверить свой канал прямо сейчас — /audit @имя")
    lines = ["📋 <b>Рейтинг сигнальных каналов</b>",
             "Бот сам читает публичные каналы и прогоняет их сигналы по свечам Bybit. "
             "Вход, стоп и цель — как в постах; итог — если закрывать всю позицию на первой "
             "цели. Названия не публикуем — только цифры.", ""]
    for row in rows[:max_rows]:
        v = verdict(row)
        f = row["filled"]
        lines.append(
            f"<b>Канал {esc(row['letter'])}</b> · {_subs(row['subscribers'])}\n"
            f"сигналов {row['checked']} · до первой цели {_pct(row['tp1'], f)} · "
            f"итог на TP1 <b>{_r(row['r_total'])}</b> за {row['r_trades']} сд. · "
            f"{v.label}{_trend(row)}")
    if len(rows) > max_rows:
        lines.append(f"…и ещё {len(rows) - max_rows}.")
    lines += ["", "Проверить свой канал — /audit @имя. Не финансовый совет."]
    return "\n".join(lines)


def render_admin_names(channels: list[dict]) -> str:
    """Соответствие букв и имён — только админу."""
    if not channels:
        return "Каналов в рейтинге нет. /rating add @имя"
    out = ["🔐 Рейтинг: буквы → каналы"]
    for c in channels:
        letter = c["letter"] or "—"
        err = f" · ошибка: {c['last_error']}" if c.get("last_error") else ""
        last = (c["last_run_at"] or "ещё не проверялся")[:16].replace("T", " ")
        out.append(f"{letter} · @{c['username']} · {c['status']} · {_subs(c['subscribers'])} · "
                   f"проверен: {last}{err}")
    return "\n".join(out)


ADMIN_HELP = ("/rating add @имя — в обход; /rating rm @имя — снять; /rating run — проверить "
              "сейчас; /rating names — буквы и имена; /rating card — карточка PNG")


# ---------------------------------------------------------------------------
# Карточка для соцсетей (анонимная)
# ---------------------------------------------------------------------------

_RATING_STYLE = channel_audit._CARD_STYLE + """
.row { display:grid; grid-template-columns:120px 1fr 150px 190px 180px; gap:14px;
       align-items:baseline; padding:20px 0; border-bottom:1px solid #2a2927; }
.row.h { color:#8a877f; font-size:14px; letter-spacing:.12em; text-transform:uppercase;
         padding:10px 0 12px; }
.ltr { font-size:40px; font-weight:700; }
.cell { font-size:26px; font-variant-numeric:tabular-nums; }
.cell small { display:block; font-size:14px; color:#8a877f; margin-top:4px; }
.ttl { font-size:44px; font-weight:700; margin-top:38px; letter-spacing:-.01em; }
"""


def rating_card_html(rows: list[dict], week_label: str, bot: str = "rsirange_bot",
                     venue: str = "Bybit", max_rows: int = 8) -> str:
    """PNG через graphics.render_html_to_png(selector=".card"). Имён каналов нет."""
    body_rows = ['<div class="row h"><div>Канал</div><div>Подписчиков</div><div>До TP1</div>'
                 '<div>Итог на TP1</div><div>Статус</div></div>']
    for row in rows[:max_rows]:
        v = verdict(row)
        f = row["filled"]
        body_rows.append(
            f'<div class="row"><div class="ltr">{esc(row["letter"])}</div>'
            f'<div class="cell">{esc(_subs(row["subscribers"]).replace("подписчиков", "").strip())}'
            f'<small>{row["checked"]} сигналов</small></div>'
            f'<div class="cell">{_pct(row["tp1"], f)}<small>из {f} исполненных</small></div>'
            f'<div class="cell {v.cls}">{_r(row["r_total"]).replace("-", "−")}'
            f'<small>{row["r_trades"]} сд.</small></div>'
            f'<div class="cell {v.cls}">{v.label}</div></div>')
    fine = ("Вход, стоп и цели — как в постах каналов; исход — по свечам биржи после публикации. "
            "Итог — вся позиция на первой цели. Названия каналов не публикуем. "
            "Проверить любой канал — /audit в боте.")
    body = (
        '<div class="card">'
        '<div class="head"><div class="brand">RSI RANGE</div>'
        f'<div class="tag">рейтинг сигнальных каналов · {esc(week_label)}</div></div>'
        '<div class="ttl">Кто доходит до цели, а кто нет</div>'
        f'{"".join(body_rows)}'
        f'<div class="foot"><div>Проверено по свечам <b>{esc(venue)}</b> · <b>@{esc(bot)}</b>'
        '</div><div>Не финансовый совет</div></div>'
        f'<div class="fine">{fine}</div>'
        "</div>")
    return f"<style>{_RATING_STYLE}</style>{body}"


def week_label(now_iso: str) -> str:
    from datetime import datetime
    d = datetime.fromisoformat(now_iso)
    _, week, _ = d.isocalendar()
    return f"неделя {week}"
