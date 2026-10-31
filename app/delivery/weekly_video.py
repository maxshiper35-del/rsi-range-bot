"""
weekly_video.py — вертикальное видео-итог недели (1080×1920, MP4) для Reels/Shorts/TikTok.

Все закрытые сделки недели (тот же отбор, что у карточки недели — journal.trades_for_period)
появляются по одной: плитка монеты с R, кривая доходности дорисовывается, счётчик недели
растёт или падает. Затем лучшая/худшая/средняя и итог с призывом в канал. Без отбора —
минусовые недели выглядят так же, как плюсовые.

Чистые функции (week_stats, equity_svg, build_week_frame_html) — без браузера;
render_week_mp4 — через replay.frames_to_mp4 (только asyncio.to_thread).
"""
from __future__ import annotations

from app.delivery.graphics import _esc
from app.delivery.replay import (
    _AMBER,
    _CSS,
    _GREEN,
    _MUTED,
    _RED,
    H,
    W,
    _r_text,
    frames_to_mp4,
)

__all__ = ["H", "W", "build_week_frame_html", "equity_svg", "n_trades", "render_week_mp4",
           "week_stats"]

EQ_W, EQ_H = 960, 470
MAX_TILES = 40
# подписи по масштабу периода: (заголовок, герой, монеты, призыв)
_SCOPE = {"week": ("Итог недели", "Результат недели", "Монеты недели",
                   "Следующая неделя — в"),
          "day": ("Итог дня", "Результат дня", "Монеты дня", "Новые сделки — в")}

_WEEK_CSS = """
.hero{margin-top:46px}
.hl{font-size:22px;font-weight:700;letter-spacing:.14em;color:#7B8190;text-transform:uppercase}
.hv{font-size:170px;font-weight:800;letter-spacing:-5px;line-height:1.02;
  font-variant-numeric:tabular-nums}
.hs{font-size:28px;color:#8A909C;margin-top:4px}
.eq{margin-top:30px;border-radius:28px;padding:20px;
  background:linear-gradient(180deg,rgba(255,255,255,.04),rgba(255,255,255,.012));
  border:1px solid rgba(255,255,255,.08)}
.tiles{display:grid;gap:12px;margin-top:26px}
.tile{border-radius:16px;padding:14px 16px;background:rgba(255,255,255,.035);
  border:1px solid rgba(255,255,255,.07);display:flex;justify-content:space-between;
  align-items:baseline}
.tile.new{border-color:rgba(242,184,75,.75);box-shadow:0 0 0 2px rgba(242,184,75,.25)}
.ts{font-weight:800;color:#E3E6EC}.ta{font-weight:700;margin-left:6px}
.tr{font-weight:800;font-variant-numeric:tabular-nums}
.more{font-size:22px;color:#6B7280;margin-top:10px}
.bars{margin-top:26px;display:flex;flex-direction:column;gap:14px}
.bl{display:grid;grid-template-columns:150px 1fr 150px;align-items:center;gap:16px;
  font-size:28px;font-weight:800}
.bt{height:26px;border-radius:8px;background:rgba(255,255,255,.04);position:relative;overflow:hidden}
.bf{position:absolute;top:0;bottom:0;border-radius:8px}
.bv{text-align:right;font-variant-numeric:tabular-nums}
"""


def n_trades(n: int) -> str:
    """«1 сделка», «3 сделки», «5 сделок»."""
    m10, m100 = n % 10, n % 100
    word = ("сделка" if m10 == 1 and m100 != 11
            else "сделки" if 2 <= m10 <= 4 and not 12 <= m100 <= 14 else "сделок")
    return f"{n} {word}"


def week_stats(trades: list[dict]) -> dict:
    rs = [float(t["result_r"]) for t in trades]
    n = len(rs)
    best = max(trades, key=lambda t: t["result_r"]) if trades else None
    worst = min(trades, key=lambda t: t["result_r"]) if trades else None
    return {"n": n, "total": sum(rs), "wins": sum(1 for r in rs if r > 0),
            "losses": sum(1 for r in rs if r <= 0), "exp": sum(rs) / n if n else 0.0,
            "best": best, "worst": worst,
            "cum": [sum(rs[:i + 1]) for i in range(n)]}


def equity_svg(cum: list[float], k: int) -> str:
    """Кривая накопленного R по сделкам недели: видны первые k точек, масштаб — по всей
    неделе (ничего не прыгает), ноль — пунктиром."""
    n = len(cum)
    lo, hi = min(0.0, *cum), max(0.0, *cum)
    pad = (hi - lo) * 0.12 or 1.0
    lo, hi = lo - pad, hi + pad

    def x(i: int) -> float:                               # точка 0 = старт недели (0R)
        return round(30 + i * (EQ_W - 60) / max(n, 1), 1)

    def y(v: float) -> float:
        return round((hi - v) / (hi - lo) * (EQ_H - 40) + 20, 1)

    parts = [f'<line x1="20" y1="{y(0)}" x2="{EQ_W - 20}" y2="{y(0)}" stroke="{_MUTED}" '
             f'stroke-opacity=".6" stroke-dasharray="6 8" stroke-width="2"/>',
             f'<text x="{EQ_W - 24}" y="{y(0) - 10}" font-size="20" fill="{_MUTED}" '
             f'text-anchor="end">0R</text>']
    pts = [(x(0), y(0))] + [(x(i + 1), y(cum[i])) for i in range(min(k, n))]
    for i in range(1, len(pts)):
        up = cum[i - 1] - (cum[i - 2] if i >= 2 else 0.0) > 0
        parts.append(f'<line x1="{pts[i - 1][0]}" y1="{pts[i - 1][1]}" x2="{pts[i][0]}" '
                     f'y2="{pts[i][1]}" stroke="{_GREEN if up else _RED}" stroke-width="5" '
                     f'stroke-linecap="round"/>')
    for i in range(1, len(pts)):
        up = cum[i - 1] - (cum[i - 2] if i >= 2 else 0.0) > 0
        r = 11 if i == len(pts) - 1 else 7
        ring = f' stroke="{_AMBER}" stroke-width="4"' if i == len(pts) - 1 and k < n else ""
        parts.append(f'<circle cx="{pts[i][0]}" cy="{pts[i][1]}" r="{r}" '
                     f'fill="{_GREEN if up else _RED}"{ring}/>')
    return (f'<svg viewBox="0 0 {EQ_W} {EQ_H}" width="{EQ_W}" height="{EQ_H}" '
            f'xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>')


def _tiles(trades: list[dict], k: int) -> str:
    shown = trades[:k][-MAX_TILES:]
    cols = 3 if len(trades) <= 12 else 4 if len(trades) <= 24 else 5
    fs = {3: 30, 4: 26, 5: 22}[cols]
    cells = []
    for j, t in enumerate(shown):
        r = float(t["result_r"])
        txt, cls = _r_text(r)
        arrow = "▲" if t["side"] == "LONG" else "▼"
        acol = _GREEN if t["side"] == "LONG" else _RED
        new = " new" if j == len(shown) - 1 and k < len(trades) else ""
        pad = "14px 16px" if cols < 5 else "12px 12px"
        cells.append(f"<div class='tile{new}' style='font-size:{fs}px;padding:{pad}'><span>"
                     f"<span class='ts'>{_esc(t['symbol'].split('/')[0])}</span>"
                     f"<span class='ta' style='color:{acol}'>{arrow}</span></span>"
                     f"<span class='tr {cls}'>{txt.replace('.00R', 'R')}</span></div>")
    hidden = max(0, min(k, len(trades)) - MAX_TILES)
    more = f"<div class='more'>+ ещё {hidden} раньше</div>" if hidden else ""
    return (f"<div class='tiles' style='grid-template-columns:repeat({cols},minmax(0,1fr))'>"
            f"{''.join(cells)}</div>{more}")


def _by_symbol(trades: list[dict], top: int = 6, scope: str = "week") -> str:
    """Монеты периода по сумме R (до top штук: сильнейшие и слабейшие) — полосы от нуля."""
    agg: dict[str, float] = {}
    for t in trades:
        b = t["symbol"].split("/")[0]
        agg[b] = agg.get(b, 0.0) + float(t["result_r"])
    items = sorted(agg.items(), key=lambda kv: -kv[1])
    if len(items) > top:
        items = items[:top // 2] + items[-(top - top // 2):]
    m = max((abs(v) for _b, v in items), default=1.0) or 1.0
    rows = []
    for b, v in items:
        w = abs(v) / m * 50
        left = 50 if v >= 0 else 50 - w
        col = _GREEN if v > 0 else _RED
        tx, cls = _r_text(v)
        rows.append(f"<div class='bl'><div>{_esc(b)}</div><div class='bt'>"
                    f"<div class='bf' style='left:{left:.1f}%;width:{w:.1f}%;"
                    f"background:{col}'></div></div><div class='bv {cls}'>{tx}</div></div>")
    return (f"<div class='sl' style='margin-top:34px'>{_SCOPE[scope][2]}</div>"
            f"<div class='bars'>{''.join(rows)}</div>")


def build_week_frame_html(period: str, trades: list[dict], k: int, *, summary: bool = False,
                          outro: float | None = None, channel: str = "@rsi_range",
                          scope: str = "week") -> str:
    """Кадр: первые k сделок периода (scope: week|day); summary — блок
    лучшая/худшая/средняя вместо плиток; outro — доля счётчика итога (0..1) на заставке."""
    title, hero, _coins, cta = _SCOPE[scope]
    st = week_stats(trades)
    part = week_stats(trades[:k])
    val, cls = _r_text(part["total"]) if k else ("0R", "neu")
    sub = (f"{k} из {st['n']} сделок · {part['wins']} в плюс / {part['losses']} в минус"
           if not summary else f"{n_trades(st['n'])} · {st['wins']} в плюс / "
                               f"{st['losses']} в минус")
    body = _tiles(trades, k)
    if summary and st["n"]:
        def card(lbl: str, v: float, note: str) -> str:
            tx, c = _r_text(v)
            return (f"<div class='st'><div class='sl'>{lbl}</div><div class='sv {c}'>"
                    f"{tx}</div><div class='hs' style='font-size:22px'>{note}</div></div>")
        stops = sum(1 for t in trades if float(t["result_r"]) <= -0.99)
        worst = st["worst"]
        worst_note = (f"стоп −1R × {stops}" if stops > 1
                      else _esc(worst["symbol"].split("/")[0]))
        body = ("<div class='stats'>"
                + card("Лучшая", st["best"]["result_r"],
                       _esc(st["best"]["symbol"].split("/")[0]))
                + card("Худшая", worst["result_r"], worst_note)
                + card("В среднем", st["exp"], "на сделку") + "</div>"
                + _by_symbol(trades, scope=scope))
    over = ""
    if outro is not None:
        big, bcls = _r_text(st["total"] * outro)
        wr = round(st["wins"] / st["n"] * 100) if st["n"] else 0
        over = (f"<div class='outro'><div class='olbl'>{title} · {_esc(period)}</div>"
                f"<div class='big {bcls}'>{big}</div>"
                f"<div class='oline'>{n_trades(st['n'])} · винрейт {wr}%<br>"
                f"все закрытые сделки, без отбора</div>"
                f"<div class='cta'>{cta} {_esc(channel)}</div>"
                f"<div class='note'>R — в долях риска, по журналу, до комиссий · "
                f"не финансовый совет"
                f"</div></div>")
    return (
        f"<style>{_CSS}{_WEEK_CSS}</style><div class='page'>"
        f"<div class='top'><div class='brand'><div class='mark'>R</div><div>"
        f"<div class='name'>RSI Range</div><div class='handle'>{_esc(channel)}</div></div></div>"
        f"<div class='eyebrow'>{title}<br>{_esc(period)}</div></div>"
        f"<div class='hero'><div class='hl'>{hero}</div>"
        f"<div class='hv {cls}'>{val}</div><div class='hs'>{sub}</div></div>"
        f"<div class='eq'>{equity_svg(st['cum'], k)}</div>{body}"
        f"<div class='foot'><div>R — в долях риска, по журналу, до комиссий</div>"
        f"<div>не финансовый совет</div></div>{over}</div>")


def render_week_mp4(period: str, trades: list[dict], channel: str = "@rsi_range",
                    scope: str = "week") -> bytes:
    """Интро 1.2с · сделки по одной (всего ≈ 7с) · сводка 2.4с · счётчик итога 1.2с + 2.8с."""
    n = len(trades)
    if not n:
        raise ValueError("за период нет закрытых сделок")
    step = min(0.45, max(0.18, 7.0 / n))

    def fr(k: int, **kw) -> str:
        return build_week_frame_html(period, trades, k, channel=channel, scope=scope, **kw)

    frames = [(fr(0), 1.2)] + [(fr(k), step) for k in range(1, n + 1)]
    frames[-1] = (frames[-1][0], 1.0)
    frames.append((fr(n, summary=True), 2.4))
    frames += [(fr(n, summary=True, outro=(i + 1) / 12), 0.1) for i in range(12)]
    frames.append((fr(n, summary=True, outro=1.0), 2.8))
    # акценты: по звуку на каждую сделку (выше — плюс, ниже — минус) и аккорд итога недели
    cues, t = [], 1.2
    for tr in trades:
        cues.append((t, "tw" if float(tr["result_r"]) > 0 else "tl"))
        t += step
    t += 1.0 - step + 2.4
    cues.append((t, "win" if week_stats(trades)["total"] > 0 else "loss"))
    seed = sum(ord(c) for c in period)
    return frames_to_mp4(frames, seed=seed, cues=cues)
