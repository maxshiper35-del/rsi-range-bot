"""
replay.py — вертикальное видео-повтор закрытой сделки (1080×1920, MP4) для Reels/Shorts/TikTok.

Свечи открываются по одной от момента сигнала до выхода: вход, стоп, подтяжка трейлинга,
живой счётчик R и итог. Всё — из журнала и реальных баров Bybit; минусовые сделки рисуются
так же, как плюсовые (контент на честности, а не на подборке).

Чистые функции (replay_plan, frame_svg, build_frame_html) тестируются без браузера;
render_replay_mp4 — Playwright + ffmpeg, синхронно: вызывать ТОЛЬКО через asyncio.to_thread.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import pandas as pd

from app.delivery.graphics import _esc, _price_label

W, H = 1080, 1920
CHART_W, CHART_H, GUTTER = 960, 1100, 190
CONTEXT_BARS = 36            # история до сигнала — видно, откуда пришёл сетап
MAX_REPLAY_FRAMES = 72       # длинную сделку сжимаем: несколько баров на кадр
TAIL_BARS = 3                # пара свечей после выхода — видно, куда пошла цена
_GREEN, _RED, _INDIGO, _AMBER, _MUTED = "#4ADE9C", "#F16565", "#9BA8FA", "#F2B84B", "#767C88"
_TF_H = {"15m": 0.25, "1h": 1, "4h": 4}


def _ts(v) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def replay_plan(row: dict, bars: pd.DataFrame) -> dict:
    """План повтора из строки журнала и 1h-баров (колонки dt, open, high, low, close).

    -> {candles, start, fill_i, exit_i, end, levels, frames}: candles — все бары окна;
    кадр = {n (сколько свечей видно), stage, r (текущий R или None), stop (текущий стоп)}.
    Трейлинг — модель журнала: после хода +activate_r стоп = пик − distance_r (R)."""
    side = 1 if row["side"] == "LONG" else -1
    entry, stop0 = float(row["entry"]), float(row["stop"])
    risk = abs(entry - stop0)
    snap = json.loads(row.get("snapshot_json") or "{}")
    trail = snap.get("trail") or {}
    act, dist = float(trail.get("activate_r") or 1.5), float(trail.get("distance_r") or 0.2)
    decision = _ts(row["created_at"]) + pd.Timedelta(hours=_TF_H.get(row["timeframe"], 1))
    dts = pd.to_datetime(bars["dt"], utc=True)
    start = int(dts.searchsorted(decision))                 # первый бар после решения
    lo = max(0, start - CONTEXT_BARS)
    fill_i = int(dts.searchsorted(_ts(row["filled_at"]).floor("h"), side="right")) - 1
    exit_i = int(dts.searchsorted(_ts(row["closed_at"]).floor("h"), side="right")) - 1
    fill_i, exit_i = max(fill_i, start), max(exit_i, fill_i)
    end = min(len(bars), exit_i + 1 + TAIL_BARS)
    win = bars.iloc[lo:end].reset_index(drop=True)
    candles = [(float(o), float(h), float(lw), float(c)) for o, h, lw, c in
               win[["open", "high", "low", "close"]].itertuples(index=False)]
    start, fill_i, exit_i, end = start - lo, fill_i - lo, exit_i - lo, end - lo
    exit_px = float(row.get("exit_price") or entry + side * float(row["result_r"]) * risk)
    final_r = float(row["result_r"])

    # путь стопа по барам (действует со следующего бара — как у судьи)
    stops, cur, peak = [], stop0, entry
    for i, (_o, h, lw, _c) in enumerate(candles):
        stops.append(cur)
        if fill_i <= i < exit_i:
            peak = max(peak, h) if side > 0 else min(peak, lw)
            if side * (peak - entry) / risk >= act:
                tr = peak - side * dist * risk
                cur = max(cur, tr) if side > 0 else min(cur, tr)

    frames = []
    steps = list(range(start, exit_i + 1))
    k = max(1, math.ceil(len(steps) / MAX_REPLAY_FRAMES))
    shown = steps[::k]
    if shown[-1] != exit_i:
        shown.append(exit_i)
    for i in shown:
        if i < fill_i:
            stage, r = "wait", None
        elif i < exit_i:
            c = candles[i][3]
            stage = "trail" if stops[i] != stop0 else "open"
            r = side * (c - entry) / risk
        else:
            stage, r = "exit", final_r
        # бар входа не в счёт: порядок хай/лоу внутри него неизвестен (как у судьи)
        seen = candles[fill_i + 1:i + 1] if i > fill_i else []
        best = (max(side * ((h if side > 0 else lw) - entry) / risk
                    for _o, h, lw, _c in seen) if seen else None)
        frames.append({"n": i + 1, "stage": stage, "r": r, "stop": stops[i],
                       "hours": i - start + 1, "best": best})
    return {"candles": candles, "start": start, "fill_i": fill_i, "exit_i": exit_i,
            "end": len(candles), "entry": entry, "stop0": stop0, "exit_px": exit_px,
            "final_r": final_r, "side": side, "frames": frames, "bars_held": exit_i - fill_i + 1}


def frame_svg(plan: dict, frame: dict) -> str:
    """SVG графика для кадра: видны первые frame['n'] свечей, масштаб — по всему окну
    (ничего не прыгает), уровни: вход, исходный стоп, текущий стоп-трейл, точка выхода."""
    candles, entry, stop0 = plan["candles"], plan["entry"], plan["stop0"]
    plot_w, h = CHART_W - GUTTER, CHART_H
    lo = min(min(c[2] for c in candles), stop0, entry)
    hi = max(max(c[1] for c in candles), stop0, entry)
    pad = (hi - lo) * 0.06 or 1.0
    lo, hi = lo - pad, hi + pad

    def y(p: float) -> float:
        return round((hi - p) / (hi - lo) * (h - 40) + 20, 2)

    slot = plot_w / len(candles)
    bw = max(slot * 0.64, 2.0)
    n = frame["n"]
    parts = []
    # фон «до сигнала» — приглушённая зона контекста
    sx = plan["start"] * slot
    parts.append(f'<rect x="0" y="0" width="{sx:.1f}" height="{h}" fill="#FFFFFF" '
                 f'fill-opacity="0.025"/>')
    parts.append(f'<line x1="{sx:.1f}" y1="0" x2="{sx:.1f}" y2="{h}" stroke="{_MUTED}" '
                 f'stroke-opacity="0.5" stroke-dasharray="4 6"/>')
    # зона риска вход↔стоп
    top, bot = sorted((y(entry), y(stop0)))
    parts.append(f'<rect x="{sx:.1f}" y="{top}" width="{plot_w - sx:.1f}" '
                 f'height="{max(bot - top, 1):.2f}" fill="{_RED}" fill-opacity="0.08"/>')
    for i, (o, hh, lw, c) in enumerate(candles[:n]):
        color = _GREEN if c >= o else _RED
        cx = round(i * slot + slot / 2, 2)
        dim = ' opacity="0.55"' if i < plan["start"] else ""
        parts.append(f'<g{dim}><line x1="{cx}" y1="{y(hh)}" x2="{cx}" y2="{y(lw)}" '
                     f'stroke="{color}" stroke-width="2"/>')
        t, b = sorted((y(o), y(c)))
        parts.append(f'<rect x="{cx - bw / 2:.2f}" y="{t}" width="{bw:.2f}" '
                     f'height="{max(b - t, 2):.2f}" rx="2" fill="{color}"/></g>')
    levels = [("Вход", entry, _INDIGO, None), ("Стоп", stop0, _RED, "10 8")]
    if frame["stop"] != stop0:
        levels.append(("Трейл", frame["stop"], _AMBER, "4 6"))
    for name, price, color, dash in levels:
        yy = y(price)
        da = f' stroke-dasharray="{dash}"' if dash else ""
        parts.append(f'<line x1="{sx:.1f}" y1="{yy}" x2="{plot_w}" y2="{yy}" stroke="{color}" '
                     f'stroke-width="3" stroke-opacity="0.95"{da}/>')
        ly = min(max(yy - 20, 6), h - 46)
        parts.append(f'<rect x="{plot_w + 12}" y="{ly:.1f}" width="{GUTTER - 14}" height="40" '
                     f'rx="10" fill="{color}" fill-opacity="0.15" stroke="{color}" '
                     f'stroke-opacity="0.5"/><text x="{plot_w + 26}" y="{ly + 27:.1f}" '
                     f'font-size="21" font-weight="700" fill="{color}">{name} '
                     f'{_price_label(price)[:9]}</text>')
    if n > plan["fill_i"]:                                   # точка входа
        fx = plan["fill_i"] * slot + slot / 2
        parts.append(f'<circle cx="{fx:.1f}" cy="{y(entry)}" r="13" fill="{_INDIGO}" '
                     f'stroke="#0B0D11" stroke-width="4"/>')
    if frame["stage"] == "exit":                             # точка выхода
        ex = plan["exit_i"] * slot + slot / 2
        col = _GREEN if plan["final_r"] > 0 else _RED
        parts.append(f'<circle cx="{ex:.1f}" cy="{y(plan["exit_px"])}" r="15" fill="{col}" '
                     f'stroke="#0B0D11" stroke-width="4"/>')
    return (f'<svg viewBox="0 0 {CHART_W} {CHART_H}" width="{CHART_W}" height="{CHART_H}" '
            f'xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>')


_STAGE = {"wait": ("⏳", "Ждём вход по уровню"), "open": ("✅", "Вход исполнен"),
          "trail": ("🔁", "Трейлинг подтянул стоп"), "exit": ("🏁", "Сделка закрыта")}

_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:1080px;height:1920px;background:#0B0D11;overflow:hidden;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;color:#EDEFF3}
.page{position:relative;width:1080px;height:1920px;padding:72px 40px 56px;
  background:radial-gradient(900px 620px at 50% -6%,rgba(90,110,150,.20),transparent 62%),
  linear-gradient(180deg,#0E1016,#0B0D11 55%,#090B0F)}
.top{display:flex;justify-content:space-between;align-items:center}
.brand{display:flex;gap:16px;align-items:center}
.mark{width:64px;height:64px;border-radius:18px;background:linear-gradient(155deg,#4C6EF5,#7C5CFC);
  display:flex;align-items:center;justify-content:center;font-weight:800;font-size:30px}
.name{font-size:30px;font-weight:800;letter-spacing:-.3px}.handle{font-size:22px;color:#7B8190;margin-top:2px}
.eyebrow{font-size:20px;font-weight:700;letter-spacing:.12em;color:#7B8190;text-transform:uppercase;text-align:right}
.title{display:flex;align-items:center;gap:20px;margin-top:56px}
.pill{padding:10px 22px;border-radius:14px;font-size:28px;font-weight:800;letter-spacing:.04em}
.pill.long{background:rgba(74,222,156,.14);border:2px solid rgba(74,222,156,.5);color:#6FE3AC}
.pill.short{background:rgba(241,101,101,.13);border:2px solid rgba(241,101,101,.5);color:#F58787}
.sym{font-size:64px;font-weight:800;letter-spacing:-1.5px}.tf{font-size:32px;color:#7B8190;font-weight:700}
.sub{font-size:24px;color:#8A909C;margin-top:10px}
.chart{margin-top:34px;border-radius:28px;padding:20px 20px 16px;
  background:linear-gradient(180deg,rgba(255,255,255,.04),rgba(255,255,255,.012));
  border:1px solid rgba(255,255,255,.08)}
.status{display:flex;justify-content:space-between;align-items:center;margin-top:30px}
.wait{font-size:40px;font-weight:700;color:#5F6571}
.stats{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-top:26px}
.st{border-radius:20px;padding:20px 22px;background:rgba(255,255,255,.035);
  border:1px solid rgba(255,255,255,.07)}
.sl{font-size:18px;font-weight:700;letter-spacing:.1em;color:#6B7280;text-transform:uppercase}
.sv{font-size:38px;font-weight:800;margin-top:8px;font-variant-numeric:tabular-nums}
.stage{font-size:34px;font-weight:700;color:#D6D9E0}
.r{font-size:96px;font-weight:800;letter-spacing:-2px;font-variant-numeric:tabular-nums}
.pos{color:#4ADE9C}.neg{color:#F16565}.neu{color:#C7CBD4}
.foot{position:absolute;left:40px;right:40px;bottom:52px;display:flex;justify-content:space-between;
  font-size:20px;color:#5F6571}
.outro{position:absolute;inset:0;display:flex;flex-direction:column;justify-content:center;
  align-items:center;text-align:center;background:rgba(9,11,15,.88);padding:0 80px}
.big{font-size:230px;font-weight:800;letter-spacing:-6px;font-variant-numeric:tabular-nums;line-height:1}
.olbl{font-size:30px;font-weight:700;letter-spacing:.14em;color:#8A909C;text-transform:uppercase}
.oline{font-size:34px;color:#C7CBD4;margin-top:28px;line-height:1.4}
.cta{margin-top:70px;font-size:40px;font-weight:800;padding:26px 44px;border-radius:24px;
  background:linear-gradient(155deg,#4C6EF5,#7C5CFC)}
.note{margin-top:26px;font-size:22px;color:#6B7280}
"""


def _r_text(r: float | None) -> tuple[str, str]:
    if r is None:
        return "—", "neu"
    return f"{r:+.2f}R".replace("-", "−"), ("pos" if r > 0 else "neg" if r < 0 else "neu")


def build_frame_html(row: dict, plan: dict, frame: dict, outro: float | None = None,
                     channel: str = "@rsi_range") -> str:
    """Полный кадр 1080×1920. outro — доля счётчика итога (0..1) для финальных кадров."""
    base = row["symbol"].split("/")[0]
    side = row["side"]
    created = (_ts(row["created_at"]) + pd.Timedelta(hours=_TF_H.get(row["timeframe"], 1))
               ).strftime("%d.%m %H:%M UTC")                   # публикация = закрытие бара
    icon, text = _STAGE[frame["stage"]]
    r_txt, r_cls = _r_text(frame["r"])
    r_html = (f"<div class='r {r_cls}'>{r_txt}</div>" if frame["r"] is not None
              else "<div class='wait'>вход не исполнен</div>")
    best = frame.get("best")
    stop_now = frame["stop"]
    moved = stop_now != plan["stop0"]
    stats = (f"<div class='stats'><div class='st'><div class='sl'>С сигнала</div>"
             f"<div class='sv'>{frame.get('hours', 0)} ч</div></div>"
             f"<div class='st'><div class='sl'>Лучший ход</div><div class='sv "
             f"{'pos' if best and best > 0 else 'neu'}'>"
             f"{'—' if best is None else f'{best:+.2f}R'.replace('-', '−')}</div></div>"
             f"<div class='st'><div class='sl'>Стоп</div><div class='sv' style='color:"
             f"{_AMBER if moved else _RED}'>{_price_label(stop_now)[:9]}</div></div></div>")
    held = plan["bars_held"]
    over = ""
    if outro is not None:
        fr = plan["final_r"] * outro
        big, cls = _r_text(fr)
        over = (f'<div class="outro"><div class="olbl">Итог сделки #{int(row["id"])}</div>'
                f'<div class="big {cls}">{big}</div>'
                f'<div class="oline">{_esc(base)} {_esc(side)} · в позиции ~{held} ч<br>'
                f'выход по {"трейлингу" if plan["final_r"] > 0 else "стопу"}</div>'
                f'<div class="cta">Все сделки — в {_esc(channel)}</div>'
                f'<div class="note">Публикуем и плюсы, и минусы · R до комиссий · '
                f'не финансовый совет</div></div>')
    return (
        f"<style>{_CSS}</style><div class='page'>"
        f"<div class='top'><div class='brand'><div class='mark'>R</div><div>"
        f"<div class='name'>RSI Range</div><div class='handle'>{_esc(channel)}</div></div></div>"
        f"<div class='eyebrow'>Повтор сделки<br>#{int(row['id'])}</div></div>"
        f"<div class='title'><span class='pill {side.lower()}'>{_esc(side)}</span>"
        f"<span class='sym'>{_esc(base)}</span><span class='tf'>{_esc(row['timeframe'])}</span>"
        f"</div><div class='sub'>Сигнал {created} · риск 1R = до стопа</div>"
        f"<div class='chart'>{frame_svg(plan, frame)}</div>"
        f"<div class='status'><div class='stage'>{icon} {text}</div>"
        f"{r_html}</div>{stats}"
        f"<div class='foot'><div>R — в долях риска, до комиссий</div>"
        f"<div>не финансовый совет</div></div>{over}</div>")


def fetch_trade_bars(row: dict) -> pd.DataFrame:
    """1h-бары Bybit от (решение − CONTEXT_BARS) до (выход + TAIL_BARS)."""
    from app.data.market_data import _EXCHANGE_LOCK, _exchange
    ex = _exchange("bybit")
    since = _ts(row["created_at"]) - pd.Timedelta(hours=CONTEXT_BARS + 2)
    until = _ts(row["closed_at"]) + pd.Timedelta(hours=TAIL_BARS + 1)
    rows: list = []
    cur = int(since.timestamp() * 1000)
    while cur < until.timestamp() * 1000:
        with _EXCHANGE_LOCK:
            batch = ex.fetch_ohlcv(row["symbol"], timeframe="1h", since=cur, limit=1000)
        batch = [b for b in batch if not rows or b[0] > rows[-1][0]]
        if not batch:
            break
        rows += batch
        cur = rows[-1][0] + 3_600_000
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    now = datetime.now(timezone.utc)
    return df[df["dt"] + pd.Timedelta(hours=1) <= now].reset_index(drop=True)


def frames_to_mp4(frames: list[tuple[str, float]]) -> bytes:
    """[(HTML кадра 1080×1920, длительность с)] -> MP4 (H.264, 30 fps, faststart).
    Один браузер на всё видео; синхронно — только через asyncio.to_thread."""
    import subprocess
    import tempfile
    from pathlib import Path

    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as td, sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
            lines = []
            for k, (html, dur) in enumerate(frames):
                page.set_content(html)
                path = Path(td) / f"f{k:04d}.png"
                page.screenshot(path=str(path))
                lines += [f"file '{path.name}'", f"duration {dur}"]
            lines.append(f"file '{path.name}'")             # concat: последний кадр дважды
        finally:
            browser.close()
        (Path(td) / "list.txt").write_text("\n".join(lines))
        out = Path(td) / "video.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", "list.txt", "-vf", "fps=30,format=yuv420p", "-c:v", "libx264",
             "-preset", "medium", "-crf", "20", "-movflags", "+faststart", out.name],
            cwd=td, check=True)
        return out.read_bytes()


def render_replay_mp4(row: dict, bars: pd.DataFrame | None = None,
                      channel: str = "@rsi_range") -> bytes:
    """MP4 1080×1920, 30 fps: интро 1.6с, повтор ≤ ~8с, счётчик итога 1.2с + 2.6с."""
    if bars is None:
        bars = fetch_trade_bars(row)
    plan = replay_plan(row, bars)
    intro = {"n": plan["start"], "stage": "wait", "r": None, "stop": plan["stop0"]}
    seq: list[tuple[dict, float | None, float]] = [(intro, None, 1.6)]
    seq += [(f, None, 0.11) for f in plan["frames"]]
    seq[-1] = (seq[-1][0], None, 0.9)                        # задержка на выходе
    last = plan["frames"][-1]
    seq += [(last, (i + 1) / 12, 0.1) for i in range(12)]
    seq.append((last, 1.0, 2.6))
    return frames_to_mp4([(build_frame_html(row, plan, fr, outro, channel), dur)
                          for fr, outro, dur in seq])
