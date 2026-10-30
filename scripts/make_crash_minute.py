"""Ролик 9:16 «Обвал за 60 секунд»: самая большая часовая свеча BTC за последние 72 часа
рисуется поминутно, потом — что было со сделками бота в этот час и сразу после (журнал,
без отбора), и что было бы с теми же лонгами без стопа. Урок — без обещаний доходности.

  .venv/bin/python -m scripts.make_crash_minute                  # последние 72 ч
  .venv/bin/python -m scripts.make_crash_minute --preview --db ../../../data/signals.db

Данные: публичные свечи Bybit (1h — поиск свечи, 1m — анимация и минуты выходов), сделки —
журнал сигналов (status tp1/tp2/tp3/stopped, result_r задан), БД только на чтение.
Свечи кэшируются в data/cache/crash/ — повторный рендер не ходит в биржу.
"""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from scripts import shorts_kit as kit
from scripts.shorts_kit import A, G, R, esc, money, num, pct, rr

OUT = Path("data/content/new_formats")
CACHE = Path("data/cache/crash")
_PUBLIC = ("tp1", "tp2", "tp3", "stopped")
_SIDE = {"LONG": "лонг", "SHORT": "шорт"}
CW, CHH = 860, 600                    # график
CONTEXT = 23                          # часов спокойного рынка до свечи

_CSS = """
.hk{flex:1;display:flex;flex-direction:column;justify-content:center}
.hk1{font-size:96px;font-weight:900;letter-spacing:-2.5px}
.hk2{font-size:196px;font-weight:900;letter-spacing:-8px;line-height:.95;color:#F16565;
  font-variant-numeric:tabular-nums;white-space:nowrap;margin-top:8px}
.hk3{font-size:96px;font-weight:900;letter-spacing:-3px;margin-top:6px}
.hk4{font-size:92px;font-weight:900;letter-spacing:-3px;color:#F2B84B;margin-top:56px}
.hk5{margin-top:28px}
.px{display:flex;justify-content:space-between;align-items:flex-end;margin-top:22px}
.px .p{font-size:128px;font-weight:900;letter-spacing:-4px;line-height:1;
  font-variant-numeric:tabular-nums}
.px .t{font-size:46px;font-weight:850;color:#F2B84B;font-variant-numeric:tabular-nums;
  padding-bottom:10px}
.chg{font-size:50px;font-weight:850;margin-top:8px;font-variant-numeric:tabular-nums}
.chart{margin-top:22px;border-radius:26px;background:rgba(255,255,255,.03);
  border:1px solid rgba(255,255,255,.08)}
.ev{display:flex;flex-direction:column;gap:12px;margin-top:20px}
.evc{display:flex;align-items:center;gap:16px;height:84px;border-radius:20px;padding:0 22px;
  font-size:38px;font-weight:800;background:rgba(255,255,255,.04);
  border:2px solid rgba(255,255,255,.08);white-space:nowrap;overflow:hidden}
.evc .tm{color:#8A909C;font-variant-numeric:tabular-nums;font-size:34px}
.evc.l{border-color:rgba(241,101,101,.45);background:rgba(241,101,101,.07)}
.evc.a{border-color:rgba(242,184,75,.5);background:rgba(242,184,75,.08)}
.evc.w{border-color:rgba(74,222,156,.45);background:rgba(74,222,156,.07)}
.sc{flex:1;display:flex;flex-direction:column;justify-content:center}
.sc.top{justify-content:flex-start;padding-top:80px}
.sh{font-size:84px;font-weight:900;letter-spacing:-2.5px;line-height:1.05}
.ss{font-size:38px;font-weight:700;color:#8A909C;margin-top:12px}
.rows{display:flex;flex-direction:column;gap:14px;margin-top:40px}
.row{display:grid;grid-template-columns:118px 1fr auto;align-items:center;gap:18px;height:104px;
  border-radius:22px;padding:0 24px;background:rgba(255,255,255,.04);
  border:2px solid rgba(255,255,255,.08);font-size:42px;font-weight:850}
.row .tm{font-size:34px;color:#8A909C;font-variant-numeric:tabular-nums}
.row .c{white-space:nowrap}.row .c i{font-style:normal;color:#8A909C;font-weight:700;
  font-size:36px;margin-left:8px}.row .c small{font-size:30px;color:#8A909C;margin-left:8px}
.row .v{font-variant-numeric:tabular-nums;white-space:nowrap}
.row.w{border-color:rgba(74,222,156,.45);background:rgba(74,222,156,.07)}
.row.l{border-color:rgba(241,101,101,.4);background:rgba(241,101,101,.06)}
.tot{margin-top:28px;font-size:60px;font-weight:900;font-variant-numeric:tabular-nums}
.tot small{display:block;font-size:36px;font-weight:700;color:#8A909C;margin-top:6px}
.cmp{display:flex;flex-direction:column;gap:30px;margin-top:44px}
.cm .nm{font-size:44px;font-weight:850}
.cm .nm i{font-style:normal;color:#8A909C;font-weight:700;font-size:38px}
.cm .ln{display:grid;grid-template-columns:200px 1fr 150px;align-items:center;gap:16px;
  margin-top:10px;font-size:32px;font-weight:750;color:#C7CBD4}
.cm .tr{height:40px;border-radius:12px;background:rgba(255,255,255,.05);overflow:hidden}
.cm .fl{height:100%;border-radius:12px}
.cm .rv{text-align:right;font-size:40px;font-weight:900;font-variant-numeric:tabular-nums}
.big2{display:flex;gap:18px;margin-top:40px}
.b2{flex:1;border-radius:26px;padding:24px 26px;border:2px solid rgba(255,255,255,.1)}
.b2 .l{font-size:32px;font-weight:800;color:#8A909C}
.b2 .n{font-size:96px;font-weight:900;letter-spacing:-3px;font-variant-numeric:tabular-nums}
.b2.g{border-color:rgba(242,184,75,.5);background:rgba(242,184,75,.07)}
.b2.r{border-color:rgba(241,101,101,.5);background:rgba(241,101,101,.07)}
.ls1{font-size:96px;font-weight:900;letter-spacing:-3px;line-height:1.04}
.ls1 em{font-style:normal;color:#F2B84B}
.ls2{font-size:50px;font-weight:700;color:#C7CBD4;line-height:1.3;margin-top:44px}
.ls2 b{color:#EDEFF3}
"""


@dataclass(frozen=True)
class Exit:
    trade: dict
    minute: pd.Timestamp | None      # минута выхода по 1m-свечам (None — не нашли)
    close_r: float | None            # для убыточных: R без стопа на закрытии дня (UTC)
    extreme: float | None = None     # для плюсовых: лучшая цена до выхода («дно» шорта)


def _ts(v) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def fetch(symbol: str, tf: str, since: pd.Timestamp, until: pd.Timestamp) -> pd.DataFrame:
    """Публичные свечи Bybit [since, until) с кэшем в CSV (общий ccxt-инстанс бота)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    key = f"{symbol.split(':')[0].replace('/', '')}_{tf}_{since:%Y%m%d%H%M}_{until:%Y%m%d%H%M}"
    path = CACHE / f"{key}.csv"
    if path.exists():
        df = pd.read_csv(path)
        df["dt"] = pd.to_datetime(df["dt"], utc=True)
        return df
    from app.data.market_data import _EXCHANGE_LOCK, _exchange
    ex = _exchange("bybit")
    step = {"1m": 60_000, "1h": 3_600_000}[tf]
    rows: list = []
    cur = int(since.timestamp() * 1000)
    end = int(until.timestamp() * 1000)
    while cur < end:
        with _EXCHANGE_LOCK:
            batch = ex.fetch_ohlcv(symbol, timeframe=tf, since=cur, limit=1000)
        batch = [b for b in batch if b[0] < end and (not rows or b[0] > rows[-1][0])]
        if not batch:
            break
        rows += batch
        cur = rows[-1][0] + step
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    now = pd.Timestamp.now(tz="UTC")
    df = df[df["dt"] + pd.Timedelta(milliseconds=step) <= now].reset_index(drop=True)
    if until <= now:                                       # кэшируем только прошлое
        df.to_csv(path, index=False)
    return df


def biggest_move(bars: pd.DataFrame) -> int:
    """Индекс часовой свечи с самым большим |close/open − 1|."""
    chg = (bars["close"] / bars["open"] - 1).abs()
    return int(chg.idxmax())


def load_trades(db_path: Path, start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
    """Публичные закрытые сделки, чей выход пришёлся на [start, end) — БД только на чтение."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, symbol, side, timeframe, entry, stop, exit_price, result_r, filled_at, "
            f"closed_at FROM signals WHERE status IN ({','.join('?' * len(_PUBLIC))}) "
            "AND result_r IS NOT NULL AND closed_at >= ? AND closed_at < ? "
            "ORDER BY closed_at, id",
            (*_PUBLIC, start.isoformat(), end.isoformat())).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def exit_minute(m1: pd.DataFrame, side: str, level: float,
                hour: pd.Timestamp) -> pd.Timestamp | None:
    """Первая минута часа выхода, где цена дошла до уровня выхода (как у судьи по барам)."""
    h = m1[(m1["dt"] >= hour) & (m1["dt"] < hour + pd.Timedelta(hours=1))]
    hit = h[h["low"] <= level] if side == "LONG" else h[h["high"] >= level]
    return None if hit.empty else hit["dt"].iloc[0]


def close_r(m1: pd.DataFrame, t: dict, until: pd.Timestamp) -> float:
    """R той же позиции без стопа на последней минуте до until: один момент для всех
    сделок (сумма худших точек разных минут завысила бы «без стопа»)."""
    risk = abs(float(t["entry"]) - float(t["stop"]))
    last = float(m1[m1["dt"] < until]["close"].iloc[-1])
    side = 1.0 if t["side"] == "LONG" else -1.0
    return side * (last - float(t["entry"])) / risk


def coin(symbol: str) -> str:
    return symbol.split("/")[0].replace("1000PEPE", "PEPE")


@dataclass(frozen=True)
class Crash:
    bar: pd.Series                   # часовая свеча обвала
    context: pd.DataFrame            # часовые свечи до неё
    m1: pd.DataFrame                 # минутки часа свечи
    exits: list[Exit]
    sharpest: pd.Series              # самая резкая минута против движения часа
    bottom: pd.Series                # минута экстремума часа

    @property
    def down(self) -> bool:
        return float(self.bar["close"]) < float(self.bar["open"])

    @property
    def extreme(self) -> float:
        return float(self.bar["low"] if self.down else self.bar["high"])

    @property
    def move_usd(self) -> float:
        return self.extreme - float(self.bar["open"])

    @property
    def move_pct(self) -> float:
        return (self.extreme / float(self.bar["open"]) - 1) * 100


def _candles_svg(c: Crash, minute: int | None) -> str:
    """Контекстные часовые свечи + формирующаяся свеча обвала по минутам 0..minute."""
    ctx = c.context
    lo = min(float(ctx["low"].min()), float(c.bar["low"]))
    hi = max(float(ctx["high"].max()), float(c.bar["high"]))
    pad = (hi - lo) * 0.06
    lo, hi = lo - pad, hi + pad
    gut = 150
    plot_w = CW - gut
    n = len(ctx) + 1

    def y(p: float) -> float:
        return round((hi - p) / (hi - lo) * (CHH - 40) + 20, 1)

    slot = plot_w / n
    bw = slot * 0.62
    parts = []
    step = 500 if hi - lo > 2000 else 250
    live = None if minute is None else float(c.m1["close"].iloc[minute])
    g = (int(lo // step) + 1) * step
    while g < hi:
        parts.append(f'<line x1="0" y1="{y(g)}" x2="{plot_w}" y2="{y(g)}" stroke="#262A33" '
                     f'stroke-width="2"/>')
        if live is None or abs(y(g) - y(live)) > 40:         # не под плашкой живой цены
            parts.append(f'<text x="{plot_w + 14}" y="{y(g) + 10}" font-size="28" '
                         f'font-weight="700" fill="#6B7280">{num(g)}</text>')
        g += step
    for i, r in enumerate(ctx.itertuples(index=False)):
        col = G if r.close >= r.open else R
        cx = i * slot + slot / 2
        t, b = sorted((y(r.open), y(r.close)))
        parts.append(f'<line x1="{cx:.1f}" y1="{y(r.high)}" x2="{cx:.1f}" y2="{y(r.low)}" '
                     f'stroke="{col}" stroke-width="3" opacity=".75"/>'
                     f'<rect x="{cx - bw / 2:.1f}" y="{t}" width="{bw:.1f}" '
                     f'height="{max(b - t, 3):.1f}" rx="3" fill="{col}" opacity=".75"/>')
    if minute is not None:
        m = c.m1.iloc[:minute + 1]
        o, h, lw, cl = (float(c.bar["open"]), float(m["high"].max()), float(m["low"].min()),
                        float(m["close"].iloc[-1]))
        col = G if cl >= o else R
        cx = (n - 1) * slot + slot / 2
        t, b = sorted((y(o), y(cl)))
        parts.append(f'<rect x="{cx - slot * 0.9:.1f}" y="10" width="{slot * 1.8:.1f}" '
                     f'height="{CHH - 20}" rx="10" fill="#F2B84B" opacity=".06"/>')
        parts.append(f'<line x1="{cx:.1f}" y1="{y(h)}" x2="{cx:.1f}" y2="{y(lw)}" stroke="{col}" '
                     f'stroke-width="5"/><rect x="{cx - bw * 0.75:.1f}" y="{t}" '
                     f'width="{bw * 1.5:.1f}" height="{max(b - t, 4):.1f}" rx="4" fill="{col}"/>')
        parts.append(f'<line x1="0" y1="{y(cl)}" x2="{plot_w}" y2="{y(cl)}" stroke="{col}" '
                     f'stroke-width="2" stroke-dasharray="6 8" opacity=".8"/>'
                     f'<rect x="{plot_w + 4}" y="{y(cl) - 22}" width="{gut - 8}" height="44" '
                     f'rx="10" fill="{col}"/><text x="{plot_w + 16}" y="{y(cl) + 11}" '
                     f'font-size="30" font-weight="900" fill="#0B0D11">{num(cl)}</text>')
    return (f'<svg width="{CW}" height="{CHH}" viewBox="0 0 {CW} {CHH}" '
            f'xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>')


def _hm(t: pd.Timestamp | None) -> str:
    return "—" if t is None else f"{t:%H:%M}"


def _merge(exits: list[Exit]) -> list[tuple[Exit, int]]:
    """Одинаковые сделки (та же монета/сторона/уровни/итог) — одной строкой «×2»."""
    out: list[tuple[Exit, int]] = []
    for e in exits:
        k = (e.trade["symbol"], e.trade["side"], e.trade["entry"], e.trade["stop"],
             e.trade["exit_price"], round(float(e.trade["result_r"]), 4))
        for i, (o, n) in enumerate(out):
            ok = (o.trade["symbol"], o.trade["side"], o.trade["entry"], o.trade["stop"],
                  o.trade["exit_price"], round(float(o.trade["result_r"]), 4))
            if ok == k:
                out[i] = (o, n + 1)
                break
        else:
            out.append((e, 1))
    return out


def frames(c: Crash) -> kit.Timeline:
    tl = kit.Timeline()
    tag = "Обвал за 60 секунд"
    o = float(c.bar["open"])
    t0: pd.Timestamp = c.bar["dt"]
    day = f"{t0:%d.%m.%Y}"
    sign_usd = money(c.move_usd, sign=True)

    # 1. крючок — без графика, одна мысль: сумма, время, вопрос
    hook = (kit.brand_row(tag) +
            f"<div class='hk'><div class='hk1'>Биткоин</div><div class='hk2'>{sign_usd}</div>"
            "<div class='hk3'>за 1 час</div><div class='hk4'>Что сделал бот?</div>"
            f"<div class='fine hk5'>{day} · {t0:%H:%M}–{t0 + pd.Timedelta(hours=1):%H:%M} UTC · "
            "Bybit, BTC/USDT</div></div>")
    tl.add(kit.page(hook, _CSS, glow="rgba(241,101,101,.18)"), 1.9, "loss")

    # 2. поминутно: свеча растёт, цена и время тикают, события — чипами
    events: list[tuple[pd.Timestamp, str, str]] = []
    for e in c.exits:
        if e.minute is not None and t0 <= e.minute < t0 + pd.Timedelta(hours=1):
            win = float(e.trade["result_r"]) > 0
            what = "трейлинг" if win else "стоп"
            events.append((e.minute, "w" if win else "l",
                           f"{'✅' if win else '🛑'} {esc(coin(e.trade['symbol']))} "
                           f"{_SIDE[e.trade['side']]} бота — {what}"))
    sh = c.sharpest
    events.append((sh["dt"], "a", f"⚡ {money(float(sh['close']) - float(sh['open']), sign=True)} "
                                  "за 60 секунд"))
    events.append((c.bottom["dt"], "a", f"{'⬇' if c.down else '⬆'} {'дно' if c.down else 'пик'} "
                                        f"{money(c.extreme)}"))
    events.sort(key=lambda x: x[0])

    def chart_frame(minute: int | None) -> str:
        if minute is None:
            p, t = o, t0
        else:
            p, t = float(c.m1["close"].iloc[minute]), c.m1["dt"].iloc[minute]
        ch = (p / o - 1) * 100
        cls = "neg" if ch <= -0.05 else "pos" if ch >= 0.05 else "mut"
        shown = [ev for ev in events if minute is not None and ev[0] <= t][-3:]
        chips = "".join(f"<div class='evc {k}'><span class='tm'>{ev:%H:%M}</span>{txt}</div>"
                        for ev, k, txt in shown)
        body = (kit.brand_row(tag) +
                f"<div class='px'><div class='p'>{money(p)}</div><div class='t'>{t:%H:%M} UTC"
                f"</div></div><div class='chg {cls}'>{pct(ch)} с {t0:%H:%M} · BTC/USDT, 1 час</div>"
                f"<div class='chart'>{_candles_svg(c, minute)}</div>"
                f"<div class='ev'>{chips}</div>")
        return kit.page(body, _CSS, glow="rgba(241,101,101,.10)")

    tl.add(chart_frame(None), 0.7, "tick")
    ev_minutes = {ev.floor("min") for ev, _k, _t in events}
    for m in range(len(c.m1)):
        t = c.m1["dt"].iloc[m]
        move = abs(float(c.m1["close"].iloc[m]) - float(c.m1["open"].iloc[m]))
        hit = t in ev_minutes
        cue = None
        if hit:
            kinds = [k for ev, k, _t in events if ev == t]
            cue = "tl" if "l" in kinds else "tw" if "w" in kinds else "tick"
        dur = 0.55 if hit else (0.1 if move > 60 else 2 / 30)
        tl.add(chart_frame(m), dur, cue)
    tl.add(chart_frame(len(c.m1) - 1), 0.9)

    # 3. сделки бота в обвал и сразу после — все, без отбора
    merged = _merge(c.exits)
    total = sum(float(e.trade["result_r"]) for e in c.exits)
    n_loss = sum(1 for e in c.exits if float(e.trade["result_r"]) <= 0)
    n_win = len(c.exits) - n_loss
    r_loss = sum(float(e.trade["result_r"]) for e in c.exits if float(e.trade["result_r"]) <= 0)
    r_win = total - r_loss
    win_end = max((e.minute for e in c.exits if e.minute is not None), default=t0)
    head = (f"<div class='sh'>Сделки бота</div><div class='ss'>{t0:%H:%M}–"
            f"{(win_end + pd.Timedelta(minutes=1)).ceil('h'):%H:%M} UTC · все закрытые, без отбора"
            "</div>")

    def rows_html(k: int) -> str:
        out = []
        for e, n in merged[:k]:
            r = float(e.trade["result_r"])
            win = r > 0
            what = "трейлинг" if win else "стоп"
            mult = f"<small>×{n}</small>" if n > 1 else ""
            val = rr(r) + (" каждая" if n > 1 else "")
            out.append(f"<div class='row {'w' if win else 'l'}'><span class='tm'>{_hm(e.minute)}"
                       f"</span><span class='c'>{'✅' if win else '🛑'} "
                       f"{esc(coin(e.trade['symbol']))}<i>{_SIDE[e.trade['side']]} · {what}</i>"
                       f"{mult}</span><span class='v' style='color:{G if win else R}'>{val}"
                       f"</span></div>")
        return "".join(out)

    for k in range(1, len(merged) + 1):
        e, _n = merged[k - 1]
        cue = "tw" if float(e.trade["result_r"]) > 0 else "tl"
        tl.add(kit.page(kit.brand_row(tag) + f"<div class='sc top'>{head}<div class='rows'>"
                        f"{rows_html(k)}</div></div>", _CSS), 0.6, cue)
    tot = (f"<div class='tot {'pos' if total > 0 else 'neg'}'>Итог: {rr(total)}"
           f"<small>{kit.plural(n_loss, 'стоп', 'стопа', 'стопов')} {rr(r_loss)} · "
           f"{kit.plural(n_win, 'плюс', 'плюса', 'плюсов')} {rr(r_win)}</small></div>")
    tl.add(kit.page(kit.brand_row(tag) + f"<div class='sc top'>{head}<div class='rows'>"
                    f"{rows_html(len(merged))}</div>{tot}</div>", _CSS), 2.4,
           "win" if total > 0 else "loss")

    # 4. «а без стопа?» — те же убыточные сделки, держи их до конца дня
    losers = [(e, n) for e, n in merged
              if float(e.trade["result_r"]) <= 0 and e.close_r is not None]
    if losers:
        worst_abs = max(1.0, *(abs(e.close_r or 0.0) for e, _n in losers))
        cmp_rows = []
        for e, n in losers:
            wr = e.close_r or 0.0
            mult = f" ×{n}" if n > 1 else ""
            cmp_rows.append(
                f"<div class='cm'><div class='nm'>{esc(coin(e.trade['symbol']))} "
                f"<i>{_SIDE[e.trade['side']]}{mult}</i></div>"
                f"<div class='ln'><span>со стопом</span><div class='tr'><div class='fl' "
                f"style='width:{100 / worst_abs:.0f}%;background:{A}'></div></div>"
                f"<span class='rv amb'>−1R</span></div>"
                f"<div class='ln'><span>без стопа</span><div class='tr'><div class='fl' "
                f"style='width:{abs(wr) / worst_abs * 100:.0f}%;background:{R}'></div></div>"
                f"<span class='rv neg'>{rr(wr, 1)}</span></div></div>")
        with_stop = sum(float(e.trade["result_r"]) * n for e, n in losers)
        without = sum((e.close_r or 0.0) * n for e, n in losers)
        day_end = t0.floor("D") + pd.Timedelta(days=1)
        body = (kit.brand_row(tag) +
                "<div class='sc'><div class='sh'>А если без стопа?</div>"
                "<div class='ss'>те же сделки без стопа — по цене на конец дня "
                f"({day_end - pd.Timedelta(minutes=1):%H:%M} UTC)</div>"
                f"<div class='cmp'>{''.join(cmp_rows)}</div>"
                f"<div class='big2'><div class='b2 g'><div class='l'>со стопом</div>"
                f"<div class='n amb'>{rr(with_stop, 1)}</div></div><div class='b2 r'>"
                f"<div class='l'>без стопа</div><div class='n neg'>{rr(without, 1)}</div></div>"
                "</div></div>")
        tl.add(kit.page(body, _CSS, glow="rgba(241,101,101,.12)"), 4.4, "loss")

    # 5. урок
    gaps = [abs(float(e.trade["exit_price"]) / e.extreme - 1) * 100 for e in c.exits
            if float(e.trade["result_r"]) > 0 and e.extreme]
    tail = ""
    if gaps and c.down:
        tail = ("А трейлинг держал шорты, пока цена падала: выход не дальше "
                f"<b>{num(max(gaps), 1)}%</b> от дна.")
    body = (kit.brand_row(tag) +
            "<div class='sc'><div class='ls1'>Стоп не спасает от минуса.<br>"
            "<em>Он делает минус маленьким.</em></div>"
            f"<div class='ls2'>{tail}</div></div>")
    tl.add(kit.page(body, _CSS), 3.4, "tick")

    # 6. финал
    end = kit.end_card("Каждая сделка — <em>в открытую</em>",
                       "Плюсы, стопы, обвалы: бот публикует всё, без отбора.",
                       "Смотри сам — в Telegram:",
                       "Свечи Bybit 1m/1h. Сделки — журнал сигналов бота, R до комиссий. "
                       "1R — риск сделки до стопа.")
    tl.add(kit.page(end, _CSS), 3.3, "tick")
    return tl


def caption(c: Crash) -> kit.Caption:
    total = sum(float(e.trade["result_r"]) for e in c.exits)
    n_loss = sum(1 for e in c.exits if float(e.trade["result_r"]) <= 0)
    t0: pd.Timestamp = c.bar["dt"]
    losers = [e for e in c.exits if float(e.trade["result_r"]) <= 0 and e.close_r is not None]
    no_stop = (f"Без стопа те же сделки к концу дня стояли бы на "
               f"{rr(sum(e.close_r or 0.0 for e in losers), 1)} вместо "
               f"{rr(sum(float(e.trade['result_r']) for e in losers), 1)}. " if losers else "")
    return kit.Caption(
        title=f"Биткоин {money(c.move_usd, sign=True)} за час. Что сделал бот? #биткоин #крипта",
        hook=f"Биткоин {money(c.move_usd, sign=True)} за 1 час. Что сделал бот?",
        description=(
            f"{t0:%d.%m.%Y} в {t0:%H:%M} UTC биткоин за час упал с {money(float(c.bar['open']))} "
            f"до {money(c.extreme)} ({pct(c.move_pct)}). Сделки бота в этот час и сразу после — "
            f"все, без отбора: {kit.plural(n_loss, 'стоп', 'стопа', 'стопов')} по −1R, шорты "
            f"вышли по трейлингу, итог {rr(total)}. {no_stop}R — в долях риска, до комиссий. "
            f"Все сделки — в Telegram: {kit.BOT}. {kit.NOT_ADVICE}."),
        hashtags="#биткоин #крипта #трейдинг #стоплосс #обвал",
    )


def build(db: Path, hours: int, window_h: int) -> Crash:
    now = pd.Timestamp.now(tz="UTC").floor("h")
    h1 = fetch("BTC/USDT:USDT", "1h", now - pd.Timedelta(hours=hours), now)
    i = biggest_move(h1)
    bar = h1.iloc[i]
    t0: pd.Timestamp = bar["dt"]
    context = h1.iloc[max(0, i - CONTEXT):i].reset_index(drop=True)
    m1 = fetch("BTC/USDT:USDT", "1m", t0, t0 + pd.Timedelta(hours=1))
    down = float(bar["close"]) < float(bar["open"])
    d = m1["close"] - m1["open"]
    sharpest = m1.iloc[int(d.idxmin() if down else d.idxmax())]
    bottom = m1.iloc[int(m1["low"].idxmin() if down else m1["high"].idxmax())]
    trades = load_trades(db, t0, t0 + pd.Timedelta(hours=window_h))
    day_end = t0.floor("D") + pd.Timedelta(days=1)
    exits = []
    for t in trades:
        hour = _ts(t["closed_at"]).floor("h")
        sm = fetch(t["symbol"], "1m", t0 - pd.Timedelta(minutes=30),
                   min(day_end, pd.Timestamp.now(tz="UTC").floor("min")))
        minute = exit_minute(sm, t["side"], float(t["exit_price"]), hour)
        cr, extreme = None, None
        if float(t["result_r"]) <= 0:
            cr = close_r(sm, t, day_end)
        else:                                                # экстремум до выхода — «дно»
            upto = sm[sm["dt"] <= (minute if minute is not None else hour + pd.Timedelta(hours=1))]
            extreme = float(upto["low"].min() if t["side"] == "SHORT" else upto["high"].max())
        exits.append(Exit(t, minute, cr, extreme))
    exits.sort(key=lambda e: (e.minute or _ts(e.trade["closed_at"]), e.trade["id"]))
    return Crash(bar, context, m1, exits, sharpest, bottom)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default="data/signals.db")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--hours", type=int, default=72, help="где искать свечу (часов назад)")
    ap.add_argument("--window", type=int, default=2,
                    help="сделки с выходом в [свеча, свеча + N часов)")
    ap.add_argument("--preview", action="store_true")
    a = ap.parse_args()
    c = build(Path(a.db), a.hours, a.window)
    tl = frames(c)
    t0: pd.Timestamp = c.bar["dt"]
    key = f"obval_{t0:%Y%m%d_%H}"
    print(f"свеча {t0} {money(float(c.bar['open']))} → {money(c.extreme)} ({pct(c.move_pct)}); "
          f"сделок: {len(c.exits)}; {tl.t:.1f} с", flush=True)
    for e in c.exits:
        print("  ", e.trade["id"], coin(e.trade["symbol"]), e.trade["side"], e.trade["result_r"],
              _hm(e.minute), None if e.close_r is None else round(e.close_r, 2), e.extreme)
    out = Path(a.out)
    if a.preview:
        n = len(tl.frames)
        picks = [0, 1, 30, 50, n - 9, n - 4, n - 3, n - 2, n - 1]
        for p in kit.snapshot([tl.frames[i][0] for i in picks], out / "preview", key):
            print(p)
        return
    path = kit.render(tl, out, key, seed=32, caption=caption(c))
    print(f"{path} — {path.stat().st_size // 1024} КБ, {tl.t:.1f} с", flush=True)


if __name__ == "__main__":
    main()
