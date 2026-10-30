"""Сериал 9:16 «Робот торгует сам — день N»: честный дневник демо-счёта.

Каждый выпуск — один закрытый UTC-день: все закрытые сделки журнала за день без отбора
(тот же отбор, что у публичной статистики: status tp1/tp2/tp3/stopped, result_r задан),
✅/🛑 по каждой, счётчик баланса, итог дня и все дни серии — минус-дни так же крупно.

Баланс — РАСЧЁТ ПО ЖУРНАЛУ, а не выписка биржи: на демо-счёте executor номинал позиций
урезан капом (1500 USDT) и там же живёт ручная позиция владельца, поэтому эквити демо
не описывает сигналы. Модель: старт 10 000 $ в день 1 серии, риск 1% от текущего
баланса на сделку, R журнала (до комиссий). Подпись «расчёт по журналу, демо» — на экране.

  .venv/bin/python -m scripts.make_bot_diary                       # вчерашний UTC-день
  .venv/bin/python -m scripts.make_bot_diary --day 2026-10-29 --preview
  .venv/bin/python -m scripts.make_bot_diary --db ../../../data/signals.db --out DIR

БД открывается ТОЛЬКО на чтение (sqlite URI mode=ro).
"""
from __future__ import annotations

import argparse
import random
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from scripts import shorts_kit as kit
from scripts.shorts_kit import G, R, esc, money, pct

SERIES_START = date(2026, 10, 27)       # день 1 серии (первый день контент-пака)
START_BALANCE = 10_000.0
RISK_PCT = 1.0
OUT = Path("data/content/new_formats")
_PUBLIC = ("tp1", "tp2", "tp3", "stopped")
_SIDE = {"LONG": "лонг", "SHORT": "шорт"}

_CSS = """
.hook{flex:1;display:flex;flex-direction:column;justify-content:center}
.h-k{font-size:44px;font-weight:850;letter-spacing:.12em;color:#F2B84B}
.h-day{font-size:250px;font-weight:900;letter-spacing:-10px;line-height:.95;margin-top:18px}
.h-q{font-size:104px;font-weight:900;letter-spacing:-3.5px;line-height:1.02;margin-top:24px}
.vault{margin-top:56px;border-radius:32px;padding:30px 36px;
  background:linear-gradient(180deg,rgba(255,255,255,.06),rgba(255,255,255,.02));
  border:2px solid rgba(255,255,255,.1)}
.v-l{font-size:30px;font-weight:800;letter-spacing:.12em;color:#8A909C}
.v-box{position:relative}
.v-n{font-size:150px;font-weight:900;letter-spacing:-5px;line-height:1.02;color:#6B7280;
  font-variant-numeric:tabular-nums;filter:blur(17px);opacity:.9}
.v-q{position:absolute;left:0;right:0;top:0;bottom:0;display:flex;align-items:center;
  justify-content:center;font-size:130px;font-weight:900;color:#F2B84B}
.sub{font-size:32px;color:#8A909C;font-weight:650;margin-top:16px}
.yest{flex:1;display:flex;flex-direction:column;justify-content:center}
.y-h{font-size:96px;font-weight:900;letter-spacing:-3px;line-height:1.05}
.y-v{font-size:200px;font-weight:900;letter-spacing:-7px;line-height:1;margin-top:20px;
  font-variant-numeric:tabular-nums}
.y-s{font-size:58px;font-weight:800;color:#EDEFF3;margin-top:22px;line-height:1.2}
.y-b{font-size:50px;font-weight:700;color:#8A909C;margin-top:10px;font-variant-numeric:tabular-nums}
.days{display:flex;gap:18px;margin-top:64px}
.dcell{flex:1;border-radius:26px;padding:26px 22px;border:2px solid rgba(255,255,255,.1);
  background:rgba(255,255,255,.03)}
.dcell .dl{font-size:34px;font-weight:800;color:#8A909C}
.dcell .dv{font-size:66px;font-weight:900;margin-top:8px;font-variant-numeric:tabular-nums}
.dcell.now{border-color:rgba(242,184,75,.7);background:rgba(242,184,75,.08)}
.bal{margin-top:30px}
.b-l{font-size:28px;font-weight:800;letter-spacing:.1em;color:#8A909C}
.b-l span{color:#F2B84B}
.b-n{font-size:150px;font-weight:900;letter-spacing:-5px;line-height:1.02;
  font-variant-numeric:tabular-nums}
.b-d{font-size:46px;font-weight:850;font-variant-numeric:tabular-nums;margin-top:2px}
.spark{margin-top:18px;border-radius:26px;background:rgba(255,255,255,.03);
  border:1px solid rgba(255,255,255,.08);padding:8px 0}
.note{margin-top:18px;min-height:76px;font-size:34px;font-weight:750;line-height:1.2;
  color:#F2B84B}
.chips{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:10px}
.chip{display:flex;align-items:center;gap:12px;border-radius:20px;padding:0 20px;height:92px;
  font-size:36px;font-weight:800;background:rgba(255,255,255,.04);
  border:2px solid rgba(255,255,255,.08)}
.chip .e{font-size:34px}.chip .c{flex:1;white-space:nowrap;overflow:hidden}
.chip .c i{font-style:normal;font-weight:700;color:#8A909C;font-size:30px;margin-left:6px}
.chip .v{font-variant-numeric:tabular-nums;white-space:nowrap}
.chip.w{border-color:rgba(74,222,156,.45);background:rgba(74,222,156,.07)}
.chip.l{border-color:rgba(241,101,101,.4);background:rgba(241,101,101,.06)}
.chip.new{box-shadow:0 0 0 4px rgba(242,184,75,.55)}
.sum{flex:1;display:flex;flex-direction:column;justify-content:center}
.s-l{font-size:40px;font-weight:850;letter-spacing:.12em;color:#8A909C}
.s-v{font-size:210px;font-weight:900;letter-spacing:-8px;line-height:1;margin-top:14px;
  font-variant-numeric:tabular-nums}
.s-p{font-size:72px;font-weight:900;margin-top:4px}
.s-row{display:flex;gap:18px;margin-top:44px}
.s-box{flex:1;border-radius:24px;padding:24px;background:rgba(255,255,255,.04);
  border:2px solid rgba(255,255,255,.08)}
.s-box .n{font-size:84px;font-weight:900;line-height:1}
.s-box .t{font-size:32px;font-weight:700;color:#C7CBD4;margin-top:8px}
.s-m{font-size:46px;font-weight:700;color:#C7CBD4;line-height:1.28;margin-top:40px}
.s-m b{color:#EDEFF3}
.ser{flex:1;display:flex;flex-direction:column;justify-content:center}
.ser-h{font-size:84px;font-weight:900;letter-spacing:-2.5px;line-height:1.05}
.ser-b{font-size:64px;font-weight:900;margin-top:20px;font-variant-numeric:tabular-nums}
.chart{position:relative;height:600px;margin-top:40px}
.zero{position:absolute;left:0;right:0;height:3px;background:rgba(255,255,255,.28)}
.zl{position:absolute;right:0;font-size:28px;font-weight:800;color:#8A909C}
.col{position:absolute;display:flex;flex-direction:column;align-items:center}
.col .bb{width:100%;border-radius:18px}
.col .bv{font-size:54px;font-weight:900;font-variant-numeric:tabular-nums;white-space:nowrap}
.col .bd{font-size:32px;font-weight:800;color:#8A909C;white-space:nowrap}
.ser-m{font-size:48px;font-weight:750;color:#C7CBD4;margin-top:40px;line-height:1.25}
"""


@dataclass(frozen=True)
class Step:
    trade: dict
    before: float
    pnl: float
    after: float


def day_number(day: date, start: date = SERIES_START) -> int:
    return (day - start).days + 1


def account_steps(trades: list[dict], start_balance: float = START_BALANCE,
                  risk_pct: float = RISK_PCT) -> list[Step]:
    """Счёт по сделкам: риск risk_pct% от ТЕКУЩЕГО баланса, PnL = риск × R журнала."""
    bal, out = start_balance, []
    for t in trades:
        pnl = bal * risk_pct / 100.0 * float(t["result_r"])
        out.append(Step(t, bal, pnl, bal + pnl))
        bal += pnl
    return out


def utc_day(ts: str) -> date:
    t = datetime.fromisoformat(ts)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).date()


def load_trades(db_path: Path, start: date, end: date) -> list[dict]:
    """Закрытые публичные сделки с closed_at в [start, end) UTC — БД только на чтение."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, symbol, side, timeframe, closed_at, result_r FROM signals "
            f"WHERE status IN ({','.join('?' * len(_PUBLIC))}) AND result_r IS NOT NULL "
            "AND closed_at >= ? AND closed_at < ? ORDER BY closed_at, id",
            (*_PUBLIC, f"{start.isoformat()}T00:00:00", f"{end.isoformat()}T00:00:00"),
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


@dataclass(frozen=True)
class Diary:
    day: date
    n: int                           # номер дня серии
    steps: list[Step]                # все шаги серии до конца дня включительно
    today: list[Step]
    days: list[tuple[date, float, float]]   # (день, баланс на начало, на конец)

    @property
    def open_balance(self) -> float:
        return self.today[0].before if self.today else self.days[-1][1]

    @property
    def close_balance(self) -> float:
        return self.days[-1][2]


def build_diary(trades: list[dict], day: date, start: date = SERIES_START) -> Diary:
    """Сделки серии [start, day] -> шаги счёта, дни с балансами на начало/конец."""
    steps = account_steps(trades)
    days = []
    bal = START_BALANCE
    d = start
    while d <= day:
        st = [s for s in steps if utc_day(s.trade["closed_at"]) == d]
        end = st[-1].after if st else bal
        days.append((d, bal, end))
        bal = end
        d += timedelta(days=1)
    today = [s for s in steps if utc_day(s.trade["closed_at"]) == day]
    return Diary(day, day_number(day, start), steps, today, days)


def _coin(t: dict) -> str:
    return t["symbol"].split("/")[0].replace("1000PEPE", "PEPE").replace("1000", "")


def _sparkline(values: list[float], k: int, w: int = 860, h: int = 230) -> str:
    """Баланс по сделкам дня: видны первые k точек, масштаб — по всему дню."""
    lo, hi = min(values), max(values)
    pad = (hi - lo) * 0.15 or 1.0
    lo, hi = lo - pad, hi + pad
    n = len(values) - 1

    def x(i: int) -> float:
        return round(30 + i * (w - 60) / max(n, 1), 1)

    def y(v: float) -> float:
        return round((hi - v) / (hi - lo) * (h - 30) + 15, 1)

    base = values[0]
    parts = [f'<line x1="20" y1="{y(base)}" x2="{w - 20}" y2="{y(base)}" stroke="#5F6571" '
             f'stroke-width="3" stroke-dasharray="8 10"/>']
    pts = [(x(i), y(values[i])) for i in range(min(k, n) + 1)]
    for i in range(1, len(pts)):
        col = G if values[i] >= values[i - 1] else R
        parts.append(f'<line x1="{pts[i - 1][0]}" y1="{pts[i - 1][1]}" x2="{pts[i][0]}" '
                     f'y2="{pts[i][1]}" stroke="{col}" stroke-width="7" stroke-linecap="round"/>')
    for i, (px, py) in enumerate(pts):
        col = "#C7CBD4" if i == 0 else (G if values[i] >= values[i - 1] else R)
        r = 13 if i == len(pts) - 1 else 8
        parts.append(f'<circle cx="{px}" cy="{py}" r="{r}" fill="{col}"/>')
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>')


def _chip(s: Step, new: bool) -> str:
    win = s.pnl > 0
    cls = "w" if win else "l"
    mark = "✅" if win else "🛑"
    col = G if win else R
    return (f"<div class='chip {cls}{' new' if new else ''}'><span class='e'>{mark}</span>"
            f"<span class='c'>{esc(_coin(s.trade))}<i>{_SIDE[s.trade['side']]}</i></span>"
            f"<span class='v' style='color:{col}'>{money(s.pnl, sign=True)}</span></div>")


def _crash_note(d: Diary, k: int, notes: dict[int, str]) -> str:
    last = ""
    for i in range(k):
        last = notes.get(i, last)
    return last


def frames(d: Diary, notes: dict[int, str] | None = None) -> kit.Timeline:
    notes = notes or {}
    tl = kit.Timeline()
    rng = random.Random(d.n)
    tag = f"День {d.n} · {d.day:%d.%m}"
    o, c = d.open_balance, d.close_balance

    # 1. крючок: «Робот торгует сам. День N. Сколько на счёте?» + «барабан» баланса
    for k in range(18):
        digits = "".join(str(rng.randint(0, 9)) for _ in range(5))
        scr = f"{digits[:2]} {digits[2:]} $"
        q = "?" * (1 + k // 6)                               # «?» → «??» → «???»
        body = (kit.brand_row(tag) +
                "<div class='hook'><div class='h-k'>РОБОТ ТОРГУЕТ САМ</div>"
                f"<div class='h-day'>День {d.n}</div>"
                "<div class='h-q'>Сколько на&nbsp;счёте?</div>"
                f"<div class='vault'><div class='v-l'>НА СЧЁТЕ</div><div class='v-box'>"
                f"<div class='v-n'>{scr}</div><div class='v-q'>{q}</div></div>"
                f"<div class='sub'>старт {d.days[0][0]:%d.%m} · {money(START_BALANCE)} · "
                "демо-расчёт по журналу</div></div></div>")
        tl.add(kit.page(body, _CSS), 0.1, "tick" if k == 0 else None)

    # 2. вчера: честно, даже если минус
    if len(d.days) >= 2:
        y_day, y_o, y_c = d.days[-2]
        y_pct = (y_c / y_o - 1) * 100
        y_cls = "pos" if y_pct > 0 else "neg"
        y_n = [s for s in d.steps if utc_day(s.trade["closed_at"]) == y_day]
        y_stops = sum(1 for s in y_n if s.pnl < 0)
        y_wins = len(y_n) - y_stops
        stops_txt = kit.plural(y_stops, "стоп", "стопа", "стопов")
        what = (f"{stops_txt} подряд" if y_stops >= 2 and not y_wins
                else f"{y_wins} в плюс · {stops_txt}")
        cells = "".join(
            f"<div class='dcell'><div class='dl'>День {i + 1}</div><div class='dv "
            f"{'pos' if e > b else 'neg'}'>{pct((e / b - 1) * 100)}</div></div>"
            for i, (_dd, b, e) in enumerate(d.days[:-1]))
        cells += (f"<div class='dcell now'><div class='dl'>День {d.n}</div>"
                  "<div class='dv amb'>?</div></div>")
        body = (kit.brand_row(tag) +
                f"<div class='yest'><div class='y-h'>Вчера, день {d.n - 1}:</div>"
                f"<div class='y-v {y_cls}'>{pct(y_pct)}</div>"
                f"<div class='y-s'>{what}</div>"
                f"<div class='y-b'>{money(y_o)} → {money(y_c)}</div>"
                f"<div class='days'>{cells}</div></div>")
        tl.add(kit.page(body, _CSS), 2.3, "tl" if y_pct < 0 else "tw")

    # 3. сделки дня по одной: чип ✅/🛑, счётчик баланса, линия дня
    vals = [o] + [s.after for s in d.today]

    max_chips = 12                                           # 2 колонки × 6 рядов в зоне
    chip_h = 92 if len(d.today) <= 10 else 78

    def today_frame(k: int, shown_bal: float, new: bool) -> str:
        delta = shown_bal - o
        dcls = "pos" if delta > 0 else "neg" if delta < 0 else "mut"
        shown = list(enumerate(d.today[:k]))[-max_chips:]
        chips = "".join(_chip(s, new and i == k - 1).replace(
            "class='chip", f"style='height:{chip_h}px' class='chip", 1) for i, s in shown)
        if k > max_chips:
            chips += f"<div class='fine'>+ ещё {k - max_chips} раньше</div>"
        body = (kit.brand_row(tag) +
                "<div class='bal'><div class='b-l'>НА СЧЁТЕ · <span>расчёт по журналу, демо"
                f"</span></div><div class='b-n'>{money(shown_bal)}</div>"
                f"<div class='b-d {dcls}'>{money(delta, sign=True)} за день "
                f"({pct(delta / o * 100)})</div></div>"
                f"<div class='spark'>{_sparkline(vals, k)}</div>"
                f"<div class='note'>{_crash_note(d, k, notes)}</div>"
                f"<div class='chips'>{chips}</div>")
        return kit.page(body, _CSS)

    tl.add(today_frame(0, o, False), 0.8, "tick")
    per = min(1.2, max(0.75, 11.0 / max(1, len(d.today))))
    for k, s in enumerate(d.today, start=1):
        cue = "tw" if s.pnl > 0 else "tl"
        for j in range(5):                                   # счётчик 5 кадров по 2/30 с
            v = s.before + s.pnl * kit.ease_out((j + 1) / 5)
            tl.add(today_frame(k, v, True), 2 / 30, cue if j == 0 else None)
        tl.add(today_frame(k, s.after, True), per - 10 / 30)
    tl.add(today_frame(len(d.today), c, False), 0.9)

    # 4. итог дня
    wins = sum(1 for s in d.today if s.pnl > 0)
    stops = len(d.today) - wins
    delta = c - o
    cls = "pos" if delta > 0 else "neg"
    best = max((s.pnl for s in d.today), default=0.0)
    worst = min((s.pnl for s in d.today), default=0.0)
    stops_word = kit.plural(stops, "стоп", "стопа", "стопов")
    moral = (f"Стоп забирает <b>~1%</b> ({money(worst)}), плюс — до "
             f"<b>{money(best, sign=True)}</b>. Поэтому {stops_word} не съели день."
             if delta > 0 and stops >= wins else
             "Минус-день: стопы сработали, убыток каждого — около 1% счёта." if delta < 0 else
             f"Стоп забирает ~1% счёта, плюс — до {money(best, sign=True)}.")
    body = (kit.brand_row(tag) +
            f"<div class='sum'><div class='s-l'>ИТОГ ДНЯ {d.n}</div>"
            f"<div class='s-v {cls}'>{money(delta, sign=True)}</div>"
            f"<div class='s-p {cls}'>{pct(delta / o * 100)} · {money(c)}</div>"
            f"<div class='s-row'><div class='s-box'><div class='n pos'>{wins}</div>"
            f"<div class='t'>в плюс</div></div><div class='s-box'><div class='n neg'>{stops}</div>"
            f"<div class='t'>{stops_word.split(' ', 1)[1]}</div></div></div>"
            f"<div class='s-m'>{moral}</div></div>")
    tl.add(kit.page(body, _CSS), 2.8, "win" if delta > 0 else "loss")

    # 5. вся серия: плюс-дни вверх от нуля, минус-дни вниз — так же крупно
    body = (kit.brand_row(tag) + series_chart(d))
    tl.add(kit.page(body, _CSS), 2.6, "tick")

    # 6. финал
    fine = (f"Демо-расчёт по журналу сигналов: счёт {money(START_BALANCE)} с {d.days[0][0]:%d.%m}, "
            "риск 1% на сделку, все закрытые сделки без отбора, до комиссий.")
    end = kit.end_card(f"Завтра — <em>день {d.n + 1}</em>",
                       "Робот сам находит сделки и публикует каждую — и плюс, и стоп.",
                       "Следить за роботом — в Telegram:", fine)
    tl.add(kit.page(end, _CSS), 3.2, "tick")
    return tl


def series_chart(d: Diary, show_last: int = 7) -> str:
    """Дни серии столбиками от нулевой линии (последние show_last дней)."""
    days = d.days[-show_last:]
    first = len(d.days) - len(days) + 1
    vals = [(e / b - 1) * 100 for _dd, b, e in days]
    up, dn = max(0.0, *vals), min(0.0, *vals)
    span = (up - dn) or 1.0
    ch, lab = 600, 130                         # высота области; место под подписи
    plot = ch - 2 * lab
    zero_y = lab + up / span * plot
    n = len(days)
    gap = 26
    cw = (860 - gap * (n - 1)) / n
    cols = []
    for i, v in enumerate(vals):
        col = G if v > 0 else R
        hpx = max(14.0, abs(v) / span * plot)
        x = i * (cw + gap)
        if v >= 0:
            top = zero_y - hpx
            cols.append(f"<div class='col' style='left:{x:.0f}px;width:{cw:.0f}px;"
                        f"top:{top - 118:.0f}px'><div class='bd'>День {first + i}</div>"
                        f"<div class='bv' style='color:{col}'>{pct(v)}</div>"
                        f"<div class='bb' style='height:{hpx:.0f}px;background:{col};"
                        f"margin-top:8px'></div></div>")
        else:
            cols.append(f"<div class='col' style='left:{x:.0f}px;width:{cw:.0f}px;"
                        f"top:{zero_y:.0f}px'><div class='bb' style='height:{hpx:.0f}px;"
                        f"background:{col};margin-bottom:8px'></div>"
                        f"<div class='bv' style='color:{col}'>{pct(v)}</div>"
                        f"<div class='bd'>День {first + i}</div></div>")
    c = d.close_balance
    total = (c / START_BALANCE - 1) * 100
    ndays = kit.plural(d.n, "день", "дня", "дней")
    return (f"<div class='ser'><div class='ser-h'>{ndays} робота</div>"
            f"<div class='ser-b'>{money(START_BALANCE)} → <span class="
            f"'{'pos' if total > 0 else 'neg'}'>{money(c)}</span></div>"
            f"<div class='chart'><div class='zero' style='top:{zero_y:.0f}px'></div>"
            f"{''.join(cols)}</div>"
            f"<div class='ser-m'>{pct(total)} за {ndays}. "
            "Минус-дни не прячем — показываем каждый.</div></div>")


def caption(d: Diary) -> kit.Caption:
    delta = d.close_balance - d.open_balance
    wins = sum(1 for s in d.today if s.pnl > 0)
    stops = len(d.today) - wins
    word = "плюсе" if delta > 0 else "минусе"
    return kit.Caption(
        title=(f"Робот торгует сам. День {d.n}: сколько на счёте? "
               "#трейдинг #крипта #робот"),
        hook=f"Робот торгует сам. День {d.n}. Сколько на счёте?",
        description=(
            f"День {d.n} дневника робота ({d.day:%d.%m.%Y}): {len(d.today)} закрытых сделок — "
            f"{wins} в плюс, {stops} стопов, день в {word}: {money(delta, sign=True)} "
            f"({pct(delta / d.open_balance * 100)}). На счёте {money(d.close_balance)} из "
            f"{money(START_BALANCE)} на старте {d.days[0][0]:%d.%m}. Баланс — расчёт по журналу "
            "сигналов (демо): риск 1% на сделку, все сделки без отбора, до комиссий. "
            f"Каждая сделка — в Telegram: {kit.BOT}. {kit.NOT_ADVICE}."),
        hashtags="#трейдинг #крипта #сигналы #робот #биткоин",
    )


def crash_notes(d: Diary, start_hour: int | None, text: str) -> dict[int, str]:
    """Подпись к сделкам часа обвала: с первой сделки, закрытой в этот час или позже."""
    if start_hour is None:
        return {}
    for i, s in enumerate(d.today):
        h = datetime.fromisoformat(s.trade["closed_at"]).astimezone(timezone.utc).hour
        if h >= start_hour:
            return {i: text}
    return {}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--day", help="UTC-день ГГГГ-ММ-ДД (по умолчанию вчера)")
    ap.add_argument("--db", default="data/signals.db")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--note-hour", type=int, default=None,
                    help="час UTC события дня (обвал) — подпись над сделками с этого часа")
    ap.add_argument("--note", default="")
    ap.add_argument("--preview", action="store_true", help="только PNG ключевых кадров")
    ap.add_argument("--allow-partial", action="store_true",
                    help="превью незакрытого дня (только с --preview)")
    a = ap.parse_args()
    today_utc = datetime.now(timezone.utc).date()
    day = date.fromisoformat(a.day) if a.day else today_utc - timedelta(days=1)
    if day >= today_utc and not (a.allow_partial and a.preview):
        raise SystemExit(f"{day} ещё не закончился (UTC) — дневник только за закрытый день")
    if day < SERIES_START:
        raise SystemExit(f"серия стартует {SERIES_START}")
    trades = load_trades(Path(a.db), SERIES_START, day + timedelta(days=1))
    d = build_diary(trades, day)
    if not d.today:
        raise SystemExit(f"{day}: закрытых сделок нет — выпуск «без сделок» не рендерю")
    tl = frames(d, crash_notes(d, a.note_hour, a.note))
    key = f"dnevnik_robota_den_{d.n}"
    out = Path(a.out)
    if a.preview:
        n = len(tl.frames)
        picks = [0, 18, 19 + 3 * 6, 19 + 6 * len(d.today), n - 3, n - 2, n - 1]
        for p in kit.snapshot([tl.frames[i][0] for i in picks], out / "preview", key):
            print(p)
        return
    path = kit.render(tl, out, key, seed=31 + d.n, caption=caption(d))
    print(f"{path} — {path.stat().st_size // 1024} КБ, {tl.t:.1f} с, день {d.n}: "
          f"{money(d.open_balance)} → {money(d.close_balance)}", flush=True)


if __name__ == "__main__":
    main()
