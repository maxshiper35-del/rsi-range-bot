"""my_account.py — «💼 Мой счёт»: виртуальный счёт юзера в ДЕНЬГАХ, а не в R.

ЗАЧЕМ (ретеншн 28.10.2026): 10 из 14 ушедших заблокировали бота ровно на 5–6-й день, когда
кончается триал, а депозит через /depo задал 1 из 20 активных — люди думают в долларах,
«+0.4R» им ничего не говорит. Счёт отвечает на вопрос «сколько было бы у меня, если брать
все сигналы с момента, как я пришёл» — честно, вместе с минусовыми днями.

МОДЕЛЬ. Старт — депозит из /depo, иначе $1 000; риск на сделку — % из /depo, иначе 1%.
Каждая ПУБЛИЧНАЯ закрытая сделка (tp1–tp3/stopped с result_r; теневые 'rejected' и
неисполненные лимитки 'expired' — нет, граница та же, что у journal.stats), СОЗДАННАЯ не
раньше прихода юзера, применяется в порядке закрытия со сложным процентом:
balance *= 1 + risk/100 × R. Сигналы, опубликованные до /start, не входят: юзер их не
видел и взять не мог — иначе счёт приписывал бы ему чужую историю.

Это расчёт по опубликованным сделкам, а не реальный счёт: без комиссий и проскальзывания
(как и R журнала) и без выбора «какие брать». Не финансовый совет.

Модуль чистый там, где это важно для тестов: compute_account и рендеры не трогают БД и часы
(now передаётся снаружи); БД читают только load_public_trades/account_for/push_*.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app import db

DEFAULT_START_USD = 1000.0
DEFAULT_RISK_PCT = 1.0
PUBLIC_STATUSES = ("tp1", "tp2", "tp3", "stopped")
# утреннее окно пуша «счёт за вчера», часы UTC [start, end): 06 UTC = 09 МСК / 07 CET.
# Позже полудня «утренний» отчёт про вчера уже не к месту — пропуск лучше вечернего пуша
PUSH_HOUR_START, PUSH_HOUR_END = 6, 12

_DISCLAIMER = "Это расчёт по опубликованным сделкам, не реальный счёт. Не финансовый совет."
_LINE = "━━━━━━━━━━━━━━"
_MINUS = "−"                                   # типографский минус, как в дашборде


@dataclass(frozen=True)
class Step:
    """Одна сделка на счёте: её R и что она сделала с деньгами."""
    id: int
    symbol: str
    side: str
    tf: str
    status: str                                # tp1–tp3 / stopped — чип «тейк/трейл/стоп»
    closed_at: datetime
    r: float
    usd: float                                 # изменение баланса этой сделкой
    balance: float                             # баланс после сделки


@dataclass(frozen=True)
class Day:
    """Итог календарного UTC-дня: сколько закрылось и сколько это в деньгах."""
    day: str                                   # YYYY-MM-DD
    trades: int = 0
    wins: int = 0
    losses: int = 0
    pnl_usd: float = 0.0


@dataclass(frozen=True)
class Account:
    start: float
    risk_pct: float
    is_default: bool                           # депозит не задан — $1 000 / 1% по умолчанию
    since: datetime                            # приход юзера (users.joined_at)
    balance: float
    trades: int
    wins: int
    losses: int
    max_dd_pct: float                          # худший откат от пика, % баланса
    yesterday: Day
    today: Day
    steps: list[Step] = field(default_factory=list)

    @property
    def pnl_usd(self) -> float:
        return self.balance - self.start

    @property
    def pnl_pct(self) -> float:
        return (self.balance / self.start - 1.0) * 100.0 if self.start else 0.0


def _dt(value) -> datetime:
    """ISO-строка/datetime -> aware UTC datetime (наивное время в БД трактуем как UTC)."""
    d = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def day_bounds(now: datetime, days_back: int = 0) -> tuple[datetime, datetime]:
    """[начало, конец) UTC-дня: 0 — сегодня, 1 — вчера."""
    n = _dt(now)
    start = datetime(n.year, n.month, n.day, tzinfo=timezone.utc) - timedelta(days=days_back)
    return start, start + timedelta(days=1)


def compute_account(rows: Iterable[Mapping], *, start_usd: float, risk_pct: float,
                    joined_at, now: datetime, is_default: bool = False) -> Account:
    """Счёт по списку сделок журнала (ключи как в signals). Отбор повторяет SQL
    load_public_trades — функция сама держит границу (тесты и любой вызывающий код):
    только публичные закрытые и только созданные не раньше прихода юзера."""
    since = _dt(joined_at)
    picked = []
    for row in rows:
        if row["status"] not in PUBLIC_STATUSES or row["result_r"] is None:
            continue
        if not row["closed_at"] or _dt(row["created_at"]) < since:
            continue
        picked.append(row)
    picked.sort(key=lambda r: (_dt(r["closed_at"]), r["id"]))

    bal = peak = float(start_usd)
    max_dd = 0.0
    steps: list[Step] = []
    for row in picked:
        r = float(row["result_r"])
        before = bal
        # ниже нуля деньги не уходят (на риске ≤10% и R ≥ −1 недостижимо, но счёт не врёт)
        bal = max(0.0, bal * (1.0 + risk_pct / 100.0 * r))
        peak = max(peak, bal)
        if peak > 0:
            max_dd = max(max_dd, (peak - bal) / peak * 100.0)
        steps.append(Step(id=int(row["id"]), symbol=str(row["symbol"]).split("/")[0],
                          side=str(row["side"]), tf=str(row["timeframe"]),
                          status=str(row["status"]), closed_at=_dt(row["closed_at"]), r=r,
                          usd=bal - before,
                          balance=bal))
    by_day = _by_day(steps)
    wins = sum(1 for s in steps if s.r > 0)

    def _day(days_back: int) -> Day:
        key = day_bounds(now, days_back)[0].date().isoformat()
        return by_day.get(key, Day(day=key))

    return Account(start=float(start_usd), risk_pct=float(risk_pct), is_default=is_default,
                   since=since, balance=bal, trades=len(steps), wins=wins,
                   losses=len(steps) - wins, max_dd_pct=max_dd, yesterday=_day(1),
                   today=_day(0), steps=steps)


def _by_day(steps: list[Step]) -> dict[str, Day]:
    """Сделки -> итог по UTC-дням закрытия. Деньги дня = сумма приращений его сделок
    (со сложным процентом это ровно «баланс на конец дня − на начало»)."""
    acc: dict[str, list[float]] = {}
    for s in steps:
        d = acc.setdefault(s.closed_at.date().isoformat(), [0, 0, 0.0])
        d[0] += 1
        d[1] += 1 if s.r > 0 else 0
        d[2] += s.usd
    return {k: Day(day=k, trades=int(v[0]), wins=int(v[1]), losses=int(v[0] - v[1]),
                   pnl_usd=v[2]) for k, v in acc.items()}


def daily(acc: Account) -> list[Day]:
    """Итоги по дням со сделками (хронологически) — столбики P&L в Mini App."""
    return [d for _, d in sorted(_by_day(acc.steps).items())]


# ---------------------------------------------------------------------------
# БД
# ---------------------------------------------------------------------------
def load_public_trades(since_iso: str) -> list:
    """Публичные закрытые сделки, созданные с since_iso, в порядке закрытия (вторичный
    ключ id: у закрытий одного бара одинаковый closed_at)."""
    return db.query(
        "SELECT id, symbol, side, timeframe, status, result_r, created_at, closed_at "
        "FROM signals WHERE status IN ('tp1','tp2','tp3','stopped') AND result_r IS NOT NULL "
        "AND closed_at IS NOT NULL AND created_at >= ? ORDER BY closed_at, id", (since_iso,))


def account_for(tg_id: int, now: datetime | None = None) -> Account | None:
    """Счёт юзера по БД или None (юзер не нажимал /start). Депозит/риск — те же поля,
    что /depo (payments.depo_prefs: риск NULL -> 1%)."""
    from app import clock
    from app.delivery import payments

    u = db.query_one("SELECT joined_at FROM users WHERE tg_id=?", (tg_id,))
    if u is None or not u["joined_at"]:
        return None
    dp = payments.depo_prefs(tg_id)
    if dp and dp[0] > 0:
        start, risk, default = dp[0], dp[1], False
    else:
        start, risk, default = DEFAULT_START_USD, DEFAULT_RISK_PCT, True
    return compute_account(load_public_trades(u["joined_at"]), start_usd=start,
                           risk_pct=risk, joined_at=u["joined_at"],
                           now=now or clock.utcnow(), is_default=default)


def push_enabled(tg_id: int) -> bool:
    """Утренний отчёт включён? Колонку добавляет миграция 027 — её накатывает бот на
    старте, а webapp (отдельный процесс) может подняться раньше: до миграции — «вкл»,
    как и дефолт колонки, а не 500 на вкладке «Счёт»."""
    import sqlite3
    try:
        row = db.query_one("SELECT account_push FROM users WHERE tg_id=?", (tg_id,))
    except sqlite3.OperationalError:
        return True
    return bool(row["account_push"]) if row is not None else True


def set_push(tg_id: int, on: bool) -> None:
    """/account off|on и тумблер в Mini App. Юзера не заводим: вызывают только уже
    зарегистрированные (бот регистрирует до, webapp требует запись в users)."""
    from app import clock
    db.execute("UPDATE users SET account_push=?, updated_at=? WHERE tg_id=?",
               (1 if on else 0, clock.utcnow_iso(), tg_id))


def push_recipients() -> list[int]:
    """Живые юзеры с включённым утренним отчётом (мьют/тихие часы проверяет рассыльщик —
    тот же payments.is_signal_suppressed, что у пуша сигналов)."""
    return [r["tg_id"] for r in db.query(
        "SELECT tg_id FROM users WHERE is_active=1 AND account_push=1 ORDER BY tg_id")]


def push_due(now: datetime, last_day: str | None) -> str | None:
    """Пора ли утренний пуш: -> ключ дня «вчера» (маркер runtime) или None.
    Раз в сутки и только в утреннем окне — поздний старт бота не шлёт «утро» вечером."""
    n = _dt(now)
    if not PUSH_HOUR_START <= n.hour < PUSH_HOUR_END:
        return None
    day = day_bounds(n, 1)[0].date().isoformat()
    return day if last_day != day else None


# ---------------------------------------------------------------------------
# Тексты (HTML для Telegram) и данные для Mini App / PNG
# ---------------------------------------------------------------------------
def fmt_usd(v: float, signed: bool = False) -> str:
    """$1 043 / $23 / $3.12; signed: +$23 / −$30. Тысячи — неразрывным пробелом, чтобы
    Telegram не переносил число посередине."""
    a = abs(v)
    body = f"{a:,.2f}" if 0 < a < 10 else f"{a:,.0f}"
    body = body.replace(",", " ")
    sign = ""
    if signed and round(v, 2) != 0:
        sign = "+" if v > 0 else _MINUS
    return f"{sign}${body}"


def fmt_pct(v: float) -> str:
    sign = "+" if v > 0.05 else _MINUS if v < -0.05 else ""
    return f"{sign}{abs(v):.1f}%"


def _ddmm(d: datetime) -> str:
    return d.strftime("%d.%m")


def _wl(day: Day) -> str:
    return f"{day.wins} ✅ / {day.losses} 🛑"


def render_account(acc: Account) -> str:
    """Ответ на /account и «💼 Мой счёт» (подпись к PNG — укладывается в 1024 символа)."""
    head = f"💼 <b>Мой счёт</b> · с {_ddmm(acc.since)}\n{_LINE}\n"
    setup = (f"Старт {fmt_usd(acc.start)}, риск {acc.risk_pct:g}% на сделку, сложный процент"
             f"{' (по умолчанию)' if acc.is_default else ''}.\n")
    hint = "💡 <code>/depo 500</code> — посчитать на свой депозит\n" if acc.is_default else ""
    foot = f"{_LINE}\n<i>{_DISCLAIMER}</i>"
    if not acc.trades:
        return (f"{head}Сделок с твоего прихода ещё не закрылось — счёт сдвинется с первой "
                f"закрытой.\n{setup}{hint}Утром пришлю, что изменилось за день "
                f"(выключить: <code>/account off</code>).\n{foot}")
    y = acc.yesterday
    y_line = (f"Вчера: <b>{fmt_usd(y.pnl_usd, True)}</b> ({_wl(y)})\n" if y.trades
              else "Вчера сделок не закрылось.\n")
    t = acc.today
    t_line = f"Сегодня: {fmt_usd(t.pnl_usd, True)} ({_wl(t)})\n" if t.trades else ""
    wr = acc.wins / acc.trades * 100
    return (f"{head}{fmt_usd(acc.start)} → <b>{fmt_usd(acc.balance)}</b>  "
            f"({fmt_usd(acc.pnl_usd, True)} · {fmt_pct(acc.pnl_pct)})\n"
            f"Сделок: {acc.trades} ({acc.wins} ✅ / {acc.losses} 🛑) · winrate {wr:.0f}%\n"
            f"Худший откат: {_MINUS}{acc.max_dd_pct:.1f}%\n"
            f"{y_line}{t_line}{setup}{hint}{foot}")


def render_push(acc: Account | None) -> str | None:
    """Утренний пуш «счёт за вчера» или None — вчера по его счёту ничего не закрылось
    (пустой пуш = спам, а спам тизерами уже стоил блоков: разбор воронки 27.10)."""
    if acc is None or not acc.yesterday.trades:
        return None
    y = acc.yesterday
    hint = "\n💡 <code>/depo 500</code> — на свой депозит" if acc.is_default else ""
    return (f"💼 Твой счёт за вчера: <b>{fmt_usd(y.pnl_usd, True)}</b> ({_wl(y)}) · "
            f"с {_ddmm(acc.since)}: {fmt_usd(acc.start)} → <b>{fmt_usd(acc.balance)}</b>"
            f"{hint}\n<i>Расчёт по опубликованным сделкам, не реальный счёт. "
            f"Не финансовый совет.</i>")


def card_data(acc: Account, bot_username: str | None = None) -> dict:
    """Данные PNG-карточки (graphics.render_account_card) — без уровней и сетапов."""
    return {"start": acc.start, "balance": acc.balance, "pnl_usd": acc.pnl_usd,
            "pnl_pct": acc.pnl_pct, "risk_pct": acc.risk_pct, "is_default": acc.is_default,
            "since_label": acc.since.strftime("%d.%m.%Y"), "trades": acc.trades,
            "wins": acc.wins, "losses": acc.losses, "max_dd_pct": acc.max_dd_pct,
            "series": [acc.start] + [s.balance for s in acc.steps],
            "yesterday": {"trades": acc.yesterday.trades, "wins": acc.yesterday.wins,
                          "losses": acc.yesterday.losses,
                          "pnl_usd": acc.yesterday.pnl_usd},
            "bot": bot_username or "rsirange_bot",
            "fmt": {"start": fmt_usd(acc.start), "balance": fmt_usd(acc.balance),
                    "pnl": fmt_usd(acc.pnl_usd, True), "pct": fmt_pct(acc.pnl_pct),
                    "y_pnl": fmt_usd(acc.yesterday.pnl_usd, True)}}


def to_payload(acc: Account, push: bool, items: int = 40) -> dict:
    """JSON вкладки «Счёт» Mini App. Только закрытые публичные сделки (граница та же, что
    /api/trades и CSV /trackrecord): символ, сторона, ТФ, R — без уровней и сетапов."""
    eq: list[list[float]] = [[int(acc.since.timestamp()), round(acc.start, 2)]]
    for s in acc.steps:
        ts = int(s.closed_at.timestamp())
        if ts <= eq[-1][0]:                    # закрытия одного бара — одна точка оси времени
            eq[-1][1] = round(s.balance, 2)
        else:
            eq.append([ts, round(s.balance, 2)])

    def _d(d: Day) -> dict:
        return {"day": d.day, "trades": d.trades, "wins": d.wins, "losses": d.losses,
                "pnl_usd": round(d.pnl_usd, 2)}

    return {
        "ok": True, "registered": True,
        "start": round(acc.start, 2), "balance": round(acc.balance, 2),
        "pnl_usd": round(acc.pnl_usd, 2), "pnl_pct": round(acc.pnl_pct, 2),
        "risk_pct": acc.risk_pct, "is_default": acc.is_default,
        "since": acc.since.date().isoformat(), "trades": acc.trades, "wins": acc.wins,
        "losses": acc.losses, "max_dd_pct": round(acc.max_dd_pct, 2),
        "yesterday": _d(acc.yesterday), "today": _d(acc.today),
        "eq": eq, "days": [_d(d) for d in daily(acc)],
        "items": [{"id": s.id, "s": s.symbol, "side": s.side, "tf": s.tf, "st": s.status,
                   "r": round(s.r, 2), "usd": round(s.usd, 2), "bal": round(s.balance, 2),
                   "d": s.closed_at.strftime("%Y-%m-%dT%H:%M")}
                  for s in reversed(acc.steps[-items:])],
        "push": push,
    }
