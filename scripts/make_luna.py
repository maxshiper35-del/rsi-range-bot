"""Ролик 9:16 «Крах LUNA»: монета из топ-10 за 4 дня теряет 99,999%. График рисуется
вживую по часовым свечам, счётчик цены, события по дням; затем «купил на дне» и цена
стопа. Урок — зачем стоп, без названий сетапов и без обещаний доходности.

  .venv/bin/python -m scripts.make_luna   # -> data/content/10_krah_luna.mp4 + .txt

Данные: data.binance.vision, spot LUNAUSDT 1h за 2022-05 (старая LUNA до переименования
в LUNC), архив сверяется с опубликованным SHA-256. Кэш — data/cache/luna (gitignored).
"""
from __future__ import annotations

import csv
import hashlib
import io
import math
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from app.delivery import replay
from scripts.make_explainers import OUT

URL = ("https://data.binance.vision/data/spot/monthly/klines/LUNAUSDT/1h/"
       "LUNAUSDT-1h-2022-05.zip")
CACHE = Path("data/cache/luna")
START, END = "2022-05-09", "2022-05-14"       # 9 мая 00:00 → 13 мая 00:00 UTC: дальше Binance
HOURS = 97                                    # торги LUNA остановил (в архиве нет баров)
CW, CH = 860, 560

# события по дням: (UTC-час от начала окна, подпись) — факты, без оценок
EVENTS = [
    (6, "9 мая · стейблкоин UST теряет привязку к $1"),
    (36, "10 мая · «скидка {pct}%, беру на дне»"),
    (60, "11 мая · −94% за один день"),
    (84, "12 мая · блокчейн Terra остановлен"),
    (96, "13 мая · $0.00005 — торги на Binance остановлены"),
]

_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:1080px;height:1920px;overflow:hidden;color:#EDEFF3;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;
  background:radial-gradient(900px 700px at 50% 35%,rgba(241,101,101,.16),transparent 65%),
  linear-gradient(180deg,#0F0D10,#0A0A0D)}
.safe{position:absolute;left:64px;top:170px;width:860px;height:1340px}
.row{display:flex;justify-content:space-between;align-items:center}
.badge{font-size:26px;font-weight:800;letter-spacing:.08em;padding:10px 18px;border-radius:12px;
  background:rgba(241,101,101,.14);color:#F58787;border:2px solid rgba(241,101,101,.4)}
.brand{font-size:24px;font-weight:700;color:#6B7280}
.hook{font-size:62px;font-weight:850;letter-spacing:-1.8px;line-height:1.07;margin-top:26px;
  min-height:134px}
.hook em{font-style:normal;color:#F16565}
.price{font-size:150px;font-weight:900;letter-spacing:-5px;line-height:1;margin-top:18px;
  font-variant-numeric:tabular-nums}
.dd{font-size:56px;font-weight:850;color:#F16565;margin-top:6px;font-variant-numeric:tabular-nums}
.chart{margin-top:22px;border-radius:26px;padding:16px 0 8px;
  background:linear-gradient(180deg,rgba(255,255,255,.04),rgba(255,255,255,.01));
  border:1px solid rgba(255,255,255,.08)}
.ev{margin-top:22px;font-size:40px;font-weight:750;line-height:1.25;min-height:100px;
  color:#F1F2F5}
.ev b{color:#F2B84B}
.sc{position:absolute;inset:0;display:flex;flex-direction:column;justify-content:center}
.h{font-size:76px;font-weight:900;letter-spacing:-2px;line-height:1.05}
.h em{font-style:normal;color:#F16565}
.tb{margin-top:40px;display:flex;flex-direction:column;gap:18px}
.tr{display:flex;justify-content:space-between;align-items:baseline;border-radius:22px;
  padding:26px 30px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);
  font-size:44px;font-weight:700}
.tr .v{font-weight:900;color:#F16565;font-variant-numeric:tabular-nums}
.p{font-size:44px;line-height:1.3;color:#C7CBD4;margin-top:34px;font-weight:600}
.p b{color:#F1F2F5}
.vs{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-top:44px}
.card{border-radius:26px;padding:34px 30px;border:2px solid rgba(255,255,255,.1)}
.card .l{font-size:34px;font-weight:800;color:#8A909C}
.card .n{font-size:92px;font-weight:900;letter-spacing:-4px;margin-top:10px;
  font-variant-numeric:tabular-nums}
.card.g{border-color:rgba(74,222,156,.5);background:rgba(74,222,156,.07)}
.card.g .n{color:#4ADE9C}
.card.r{border-color:rgba(241,101,101,.5);background:rgba(241,101,101,.07)}
.card.r .n{color:#F16565}
.cta{margin-top:56px;font-size:46px;font-weight:850;padding:26px 40px;border-radius:24px;
  background:linear-gradient(155deg,#4C6EF5,#7C5CFC);align-self:flex-start}
.small{font-size:24px;color:#6B7280;margin-top:30px;line-height:1.4}
"""


def load_closes() -> list[float]:
    """Часовые close окна [START, END) — из кэша или архива Binance (SHA-256 сверяется)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    zpath = CACHE / "LUNAUSDT-1h-2022-05.zip"
    if not zpath.exists():
        with urllib.request.urlopen(URL, timeout=30) as r:
            data = r.read()
        with urllib.request.urlopen(URL + ".CHECKSUM", timeout=30) as r:
            want = r.read().decode().split()[0]
        if hashlib.sha256(data).hexdigest() != want:
            raise RuntimeError("SHA-256 архива Binance не совпал")
        zpath.write_bytes(data)
    with zipfile.ZipFile(zpath) as z:
        text = z.read(z.namelist()[0]).decode()
    out = []
    for row in csv.reader(io.StringIO(text)):
        day = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc).date().isoformat()
        if START <= day < END:
            out.append(float(row[4]))
    return out


def fmt_price(v: float) -> str:
    if v >= 10:
        return f"${v:.2f}"
    if v >= 1:
        return f"${v:.3f}"
    return f"${v:.5f}" if v >= 0.0001 else f"${v:.6f}"


def fmt_dd(v: float, base: float, digits: int | None = None) -> str:
    """Просадка с усечением, а не округлением: −99,99992% — это «−99,9999%», не «−100%»."""
    dd = (v / base - 1) * 100
    d = digits if digits is not None else 1 if dd > -99 else 2 if dd > -99.9 else 4
    s = f"{math.trunc(dd * 10 ** d) / 10 ** d:.{d}f}"
    return (s + "%").replace("-", "−").replace(".", ",")


def chart_svg(closes: list[float], k: int) -> str:
    n = len(closes)
    hi = max(closes) * 1.08

    def x(i: int) -> float:
        return round(20 + i * (CW - 40) / (n - 1), 1)

    def y(v: float) -> float:
        return round(20 + (1 - v / hi) * (CH - 70), 1)

    parts = []
    for d in range(5):                                   # полуночи — подписи дней
        xi = x(d * 24)
        parts.append(f'<line x1="{xi}" y1="20" x2="{xi}" y2="{CH - 50}" stroke="#2A2D35" '
                     f'stroke-width="2" stroke-dasharray="4 8"/>')
        if d < 4:
            parts.append(f'<text x="{xi + 8}" y="{CH - 18}" font-size="24" fill="#6B7280" '
                         f'font-weight="700">{9 + d} мая</text>')
    pts = [(x(i), y(closes[i])) for i in range(min(k, n))]
    if len(pts) >= 2:
        line = " ".join(f"{a},{b}" for a, b in pts)
        area = f"{pts[0][0]},{CH - 50} {line} {pts[-1][0]},{CH - 50}"
        parts.append(f'<polygon points="{area}" fill="#F16565" fill-opacity=".14"/>')
        parts.append(f'<polyline points="{line}" fill="none" stroke="#F16565" stroke-width="6" '
                     f'stroke-linejoin="round" stroke-linecap="round"/>')
        cx, cy = pts[-1]
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="16" fill="#F16565" fill-opacity=".3"/>'
                     f'<circle cx="{cx}" cy="{cy}" r="9" fill="#FFD0D0"/>')
    return (f'<svg viewBox="0 0 {CW} {CH}" width="{CW}" height="{CH}" '
            f'xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>')


def _head() -> str:
    return ("<div class='row'><div class='badge'>LUNA · май 2022</div>"
            "<div class='brand'>RSI Range</div></div>")


def chart_frame(closes: list[float], k: int, ev: str) -> str:
    v = closes[max(0, k - 1)]
    return (f"<style>{_CSS}</style><div class='safe'>{_head()}"
            "<div class='hook'>Монета из <em>топ-10</em>. Смотри, что будет за 4 дня</div>"
            f"<div class='price'>{fmt_price(v)}</div><div class='dd'>{fmt_dd(v, closes[0])}</div>"
            f"<div class='chart'>{chart_svg(closes, k)}</div><div class='ev'>{ev}</div></div>")


def scene(inner: str) -> str:
    return f"<style>{_CSS}</style><div class='safe'>{_head()}<div class='sc'>{inner}</div></div>"


def _loss(buy: float, end: float) -> str:
    return fmt_dd(end, buy)


def build(closes: list[float]) -> tuple[list[tuple[str, float]], list[tuple[float, str]]]:
    frames: list[tuple[str, float]] = []
    cues: list[tuple[float, str]] = []

    def add(html: str, dur: float, cue: str | None = None) -> None:
        if cue:
            cues.append((sum(d for _h, d in frames), cue))
        frames.append((html, dur))

    n = len(closes)
    pct = round((1 - closes[36] / closes[0]) * 100)
    ev_at = {h: t.format(pct=pct) for h, t in EVENTS}
    ev = ""
    add(chart_frame(closes, 1, ""), 0.5)
    for k in range(2, n + 1, 2):                         # 2 часа на кадр
        hit = [h for h in ev_at if k - 2 < h <= k]
        if hit:
            ev = ev_at[hit[0]]
        cue = ("loss" if hit and hit[0] == 60 else "tick") if hit else None
        add(chart_frame(closes, k, ev), 0.75 if hit else 0.075, cue)
    add(chart_frame(closes, n, ev), 1.6, "loss")

    by_day = {d: closes[(d - 9) * 24 + 23] for d in (9, 10, 11, 12)}   # close дня
    low = min(closes)
    add(scene("<div class='h'>«Купил на дне»?</div><div class='tb'>"
              f"<div class='tr'><span>9 мая за {fmt_price(by_day[9])}</span>"
              f"<span class='v'>{_loss(by_day[9], low)}</span></div>"
              f"<div class='tr'><span>10 мая за {fmt_price(by_day[10])}</span>"
              f"<span class='v'>{_loss(by_day[10], low)}</span></div>"
              f"<div class='tr'><span>11 мая за {fmt_price(by_day[11])}</span>"
              f"<span class='v'>{_loss(by_day[11], low)}</span></div></div>"
              "<div class='p'>Каждое «дно» оказывалось <b>серединой</b> падения.</div>"),
        3.6, "tick")
    add(scene("<div class='h'>Одна и та же покупка<br>за $64</div><div class='vs'>"
              "<div class='card g'><div class='l'>со стопом −10%</div><div class='n'>−10%</div>"
              "</div><div class='card r'><div class='l'>без стопа</div>"
              f"<div class='n'>{fmt_dd(low, closes[0], 2)}</div></div></div>"
              "<div class='p'>Стоп сработал бы ещё 9 мая. Всё, что было дальше, — уже "
              "не твоя проблема.</div>"), 3.6, "tick")
    add(scene("<div class='h'>Стоп не делает богатым.<br><em>Он оставляет в игре.</em></div>"
              "<div class='cta'>Подпишись — разбираю громкие провалы крипты</div>"
              "<div class='small'>Данные: Binance, LUNA/USDT, часовые свечи, 9–13 мая 2022. "
              "Не финансовый совет.</div>"), 3.0, "tick")
    return frames, cues


CAPTION = ("Монета из топ-10 за 4 дня: $64 → $0.00005. Каждое «дно» оказывалось серединой "
           "падения. Ставишь стоп в каждой сделке? 👇 #крипта #luna #трейдинг #стоплосс")


def main() -> None:
    closes = load_closes()
    assert len(closes) == HOURS, len(closes)
    OUT.mkdir(parents=True, exist_ok=True)
    frames, cues = build(closes)
    mp4 = replay.frames_to_mp4(frames, seed=10, cues=cues)
    (OUT / "10_krah_luna.mp4").write_bytes(mp4)
    (OUT / "10_krah_luna.txt").write_text(CAPTION + "\n", encoding="utf-8")
    print(f"10_krah_luna.mp4 — {len(mp4) // 1024} КБ, {sum(d for _h, d in frames):.1f} с",
          flush=True)


if __name__ == "__main__":
    main()
