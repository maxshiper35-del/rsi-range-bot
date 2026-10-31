"""Обучающие ролики 9:16 (Reels/Shorts/TikTok) — вечнозелёный контент воронки
канал -> бот -> /bybit. Тот же движок и стиль, что видео-повтор сделки (app/delivery/replay.py).

  .venv/bin/python -m scripts.make_explainers            # все ролики -> data/content/
  .venv/bin/python -m scripts.make_explainers r trail    # выборочно

Правила честности: никаких сумм R «за всё время» (журнал считает до комиссий — сумма
вводит в заблуждение), только проверяемые факты; таксономия сетапов не упоминается.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from app.delivery import replay
from app.delivery.graphics import _esc

OUT = Path("data/content")
_G, _R, _I, _A = replay._GREEN, replay._RED, replay._INDIGO, replay._AMBER

_EX_CSS = """
.body{position:absolute;top:190px;bottom:130px;left:40px;right:40px;display:flex;
  flex-direction:column;justify-content:center}
.it{margin-top:34px}.it:first-child{margin-top:0}
.kick{font-size:26px;font-weight:800;letter-spacing:.16em;color:#7C8CF8;text-transform:uppercase}
.h1{font-size:92px;font-weight:800;letter-spacing:-2.5px;line-height:1.04}
.h2{font-size:66px;font-weight:800;letter-spacing:-1.5px;line-height:1.08}
.p{font-size:42px;line-height:1.38;color:#C7CBD4;font-weight:500}
.p b{color:#F1F2F5}
.num{font-size:250px;font-weight:800;letter-spacing:-8px;line-height:1;font-variant-numeric:tabular-nums}
.card{border-radius:28px;padding:30px 34px;border:1px solid rgba(255,255,255,.09);
  background:linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,.015))}
.kv{display:flex;justify-content:space-between;align-items:baseline;font-size:44px;padding:14px 0;
  border-bottom:1px solid rgba(255,255,255,.07)}
.kv:last-child{border-bottom:none}.kv .k{color:#8A909C;font-weight:600}
.kv .v{font-weight:800;font-variant-numeric:tabular-nums}
.step{display:flex;gap:26px;align-items:flex-start;font-size:42px;line-height:1.3;color:#D6D9E0}
.step .n{flex:0 0 74px;height:74px;border-radius:20px;background:rgba(124,140,248,.16);
  border:2px solid rgba(124,140,248,.5);color:#9BA8FA;font-weight:800;display:flex;
  align-items:center;justify-content:center;font-size:38px}
.ex{display:inline-block;font-size:22px;font-weight:800;letter-spacing:.14em;color:#F2B84B;
  border:2px solid rgba(242,184,75,.5);border-radius:10px;padding:6px 14px;text-transform:uppercase}
.sq{display:grid;grid-template-columns:repeat(10,1fr);gap:12px}
.sq i{display:block;aspect-ratio:1;border-radius:12px}
.chartbox svg{width:1000px;height:auto;display:block}
.ctab{display:inline-block;margin-top:10px;font-size:46px;font-weight:800;padding:28px 46px;
  border-radius:26px;background:linear-gradient(155deg,#4C6EF5,#7C5CFC);align-self:flex-start}
.small{font-size:26px;color:#6B7280;line-height:1.4}
"""


def _kv(k: str, v: str, color: str = "") -> str:
    st = f" style='color:{color}'" if color else ""
    return f"<div class='kv'><span class='k'>{k}</span><span class='v'{st}>{v}</span></div>"


def _step(n: int, html: str) -> str:
    return f"<div class='step'><span class='n'>{n}</span><span>{html}</span></div>"


def _b(text: str, color: str) -> str:
    return f"<b style='color:{color}'>{text}</b>"


def _page(inner: str, tag: str) -> str:
    return (f"<style>{replay._CSS}{_EX_CSS}</style><div class='page'>"
            f"<div class='top'><div class='brand'><div class='mark'>R</div><div>"
            f"<div class='name'>RSI Range</div><div class='handle'>@rsi_range</div></div></div>"
            f"<div class='eyebrow'>{_esc(tag)}</div></div><div class='body'>{inner}</div>"
            f"<div class='foot'><div>R — в долях риска</div><div>не финансовый совет</div></div>"
            f"</div>")


def _slide(items: list[str], holds: list[float], tag: str,
           keep: int = 0) -> list[tuple[str, float]]:
    """Элементы появляются по одному (2 кадра проявления + удержание); первые keep —
    уже на экране с прошлого слайда, без повторного проявления."""
    frames = []
    for k in range(keep, len(items)):
        for t in (0.35, 0.72):
            part = "".join(f"<div class='it'>{x}</div>" for x in items[:k])
            shift = f"opacity:{t};transform:translateY({(1 - t) * 26:.0f}px)"
            part += f"<div class='it' style='{shift}'>{items[k]}</div>"
            frames.append((_page(part, tag), 0.05))
        frames.append((_page("".join(f"<div class='it'>{x}</div>" for x in items[:k + 1]),
                             tag), holds[k]))
    return frames


def _cta(tag: str, line: str) -> list[tuple[str, float]]:
    return _slide(["<div class='kick'>RSI Range</div>",
                   f"<div class='h1'>{line}</div>",
                   "<div class='p'>Публикуем <b>все</b> сделки — плюсы и минусы, без отбора</div>",
                   "<div class='ctab'>Telegram: @rsi_range</div>",
                   "<div class='small'>Не финансовый совет. "
                   "Торговля фьючерсами — высокий риск.</div>"],
                  [0.5, 1.2, 1.6, 1.4, 2.6], tag)


def _r_diagram() -> str:
    """Вход, стоп и скобка 1R на условном графике (пример); подписи — справа, не на цене."""
    w, h = 1000, 560
    path = ("M20,330 L120,300 L200,345 L290,260 L370,300 L450,210 L540,250 L630,150 "
            "L720,190 L820,110 L980,60")
    return (f"<svg viewBox='0 0 {w} {h}' width='{w}' height='{h}' "
            f"xmlns='http://www.w3.org/2000/svg'>"
            f"<rect x='300' y='300' width='560' height='120' fill='{_R}' fill-opacity='.10'/>"
            f"<line x1='300' y1='300' x2='860' y2='300' stroke='{_I}' stroke-width='5'/>"
            f"<line x1='300' y1='420' x2='860' y2='420' stroke='{_R}' stroke-width='5' "
            f"stroke-dasharray='14 10'/>"
            f"<path d='{path}' fill='none' stroke='{_G}' stroke-width='6' "
            f"stroke-linejoin='round'/>"
            f"<path d='M890,300 L910,300 L910,420 L890,420' fill='none' stroke='#F1F2F5' "
            f"stroke-width='4'/>"
            f"<text x='925' y='374' font-size='44' font-weight='800' fill='#F1F2F5'>1R</text>"
            f"<text x='850' y='340' font-size='34' font-weight='700' fill='{_I}' "
            f"text-anchor='end'>Вход</text>"
            f"<text x='850' y='405' font-size='34' font-weight='700' fill='{_R}' "
            f"text-anchor='end'>Стоп</text></svg>")


def video_r() -> list[tuple[str, float]]:
    tag = "Словарь трейдера"
    f = _slide(["<div class='kick'>Словарь трейдера</div>",
                "<div class='h1'>Что значит<br>«+3R»?</div>",
                "<div class='p'>И почему мы считаем результат в R, а не в процентах</div>"],
               [0.4, 1.4, 2.0], tag)
    f += _slide(["<div class='h2'>R — это риск<br>одной сделки</div>",
                 f"<div class='chartbox'>{_r_diagram()}</div>",
                 "<div class='p'>Расстояние от входа до стопа = <b>1R</b>. "
                 "Стоп ставится заранее — больше 1R сделка не теряет*</div>"],
                [1.0, 1.6, 2.6], tag)
    f += _slide(["<div class='h2'>Пример: риск 1% депозита</div>",
                 "<div class='card'>" + _kv("Депозит", "$1 000") + _kv("1R = 1%", "$10")
                 + _kv("+3R", "+$30", _G) + _kv("−1R", "−$10", _R) + "</div>",
                 "<div class='small'>* кроме проскальзывания и комиссий: на резком движении "
                 "стоп может исполниться хуже</div>"],
                [0.9, 2.8, 1.8], tag)
    f += _slide(["<div class='h2'>Почему в R</div>",
                 "<div class='p'>Результат <b>не зависит</b> от размера депозита и плеча</div>",
                 "<div class='p'>Сравнимо у всех: у новичка со $100 и у трейдера с $100 000</div>",
                 "<div class='p'>«+300%» без риска ничего не значит. «+3R» — значит.</div>"],
                [0.8, 1.8, 1.8, 2.2], tag)
    return f + _cta(tag, "Все сделки — в R")


def _trail_plan() -> tuple[dict, dict]:
    """Условная LONG-сделка (пример): ход до ~+2.6R и откат. Выход считается теми же
    правилами, что у судьи: трейл = пик − 0.2R после +1.5R, действует со следующего бара."""
    closes = ([100.6, 100.9, 101.1, 100.8, 101.0, 100.7, 100.9, 100.5, 100.3, 100.1]
              + [100.0, 100.4, 100.9, 101.6, 102.4, 103.1, 103.9, 104.6, 105.0, 105.2,
                 104.9, 104.7, 104.2, 104.0, 103.8, 103.9])
    rows, prev = [], closes[0]
    for c in closes:
        rows.append((prev, max(prev, c) + 0.12, min(prev, c) - 0.12, c))
        prev = c
    entry, stop, fill = 100.0, 98.0, 10
    risk = entry - stop
    peak, trail, exit_i, exit_px = entry, None, len(rows) - 1, rows[-1][3]
    for i in range(fill + 1, len(rows)):
        if trail is not None and rows[i][2] <= trail:
            exit_i, exit_px = i, trail
            break
        peak = max(peak, rows[i][1])
        if (peak - entry) / risk >= 1.5:
            trail = peak - 0.2 * risk
    dts = pd.date_range("2026-01-01", periods=len(rows), freq="1h", tz="UTC")
    o, h, lw, c = zip(*rows)
    bars = pd.DataFrame({"dt": dts, "open": o, "high": h, "low": lw, "close": c})
    row = {"id": 0, "symbol": "EXAMPLE/USDT:USDT", "side": "LONG", "timeframe": "1h",
           "entry": entry, "stop": stop, "created_at": str(dts[fill - 1]),
           "filled_at": str(dts[fill]), "closed_at": str(dts[exit_i]),
           "result_r": round((exit_px - entry) / risk, 2), "exit_price": exit_px,
           "snapshot_json": '{"trail": {"activate_r": 1.5, "distance_r": 0.2}}'}
    return row, replay.replay_plan(row, bars)


def video_trail() -> list[tuple[str, float]]:
    tag = "Трейлинг-стоп"
    # «≈0.27R на выходе» снят (тексты 31.10): замер 24.10 сравнивал ручной стоп с моделью
    # журнала, а она сама завышает выход по трейлу (минутная проверка 30.10)
    f = _slide(["<div class='kick'>Трейлинг-стоп</div>",
                "<div class='h1'>Двигаешь стоп руками раз в час?</div>",
                "<div class='p'>Биржа может вести трейлинг сама — "
                "<b>как поставить за 4 шага</b></div>"],
               [0.4, 1.4, 2.8], tag)
    row, plan = _trail_plan()
    for fr in plan["frames"]:
        stage = {"wait": "ждём вход", "open": "в позиции", "trail": "трейлинг подтягивает стоп",
                 "exit": "выход по трейлингу"}[fr["stage"]]
        inner = ("<div class='it'><span class='ex'>Пример</span></div>"
                 "<div class='it'><div class='h2' style='font-size:54px'>"
                 "Трейлинг ведёт биржа</div></div>"
                 f"<div class='it chartbox'>{replay.frame_svg(plan, fr)}</div>"
                 f"<div class='it p'>{stage}</div>")
        f.append((_page(inner, tag), 0.16 if fr["stage"] != "exit" else 2.2))
    f += _slide(["<div class='h2'>Как поставить на Bybit</div>",
                 _step(1, "Позиция → <b>TP/SL</b>"),
                 _step(2, "<b>Trailing Stop</b>"),
                 _step(3, "Расстояние = «отступ» из сигнала"),
                 _step(4, "Активация = цена «активация» из сигнала"),
                 "<div class='p'>Оба числа — в каждом нашем сигнале</div>"],
                [0.8, 1.3, 1.1, 1.5, 1.7, 2.0], tag)
    f += _slide(["<div class='h2'>Почему так лучше</div>",
                 "<div class='p'>Биржа подтягивает стоп <b>сразу</b>, а не по закрытию свечи</div>",
                 "<div class='p'>Пошаговый гайд: <b>/bybit</b> в @rsirange_bot</div>"],
                [0.7, 1.9, 2.2], tag)
    return f + _cta(tag, "Сигналы с готовым трейлингом")


def video_honest() -> list[tuple[str, float]]:
    from app import db
    from app.brain import journal
    st = journal.stats()
    first = db.query_one(
        "SELECT MIN(created_at) AS a FROM signals WHERE status IN "
        "('tp1','tp2','tp3','stopped','expired') AND result_r IS NOT NULL")
    since = pd.Timestamp(first["a"]).strftime("%d.%m.%Y") if first and first["a"] else ""
    last = [r["result_r"] for r in db.query(
        "SELECT result_r FROM signals WHERE status IN ('tp1','tp2','tp3','stopped') "
        "AND result_r IS NOT NULL ORDER BY closed_at DESC LIMIT 40")][::-1]
    tag = "Честная статистика"
    f = _slide(["<div class='kick'>Честная статистика</div>",
                "<div class='h1'>Каналы показывают только плюсы</div>",
                "<div class='p'>Мы публикуем <b>все сделки до одной</b> — и минусовые тоже</div>"],
               [0.4, 1.5, 2.2], tag)
    f += _slide([f"<div class='num'>{st['trades']}</div>",
                 f"<div class='p'>закрытых сделок с {since} — <b>все в журнале</b></div>",
                 f"<div class='p'>{round(st['winrate'] * 100)}% закрылись в плюс. Остальные — "
                 f"в минус, и это видно каждому</div>"],
                [1.0, 1.8, 2.6], tag)
    sq = "".join(f"<i style='background:{_G if r > 0 else _R}'></i>" for r in last)
    f += _slide([f"<div class='h2'>Последние {len(last)} сделок</div>",
                 f"<div class='sq'>{sq}</div>",
                 "<div class='p'>Минус — это <b>−1R</b>: стоп стоит заранее. Плюс растёт, "
                 "пока работает трейлинг</div>"],
                [0.8, 2.0, 2.6], tag)
    f += _slide(["<div class='h2'>Проверь сам</div>",
                 "<div class='p'>Исход пишет бот по ценам биржи — <b>без ручных правок</b></div>",
                 "<div class='p'>Полный журнал: CSV + контрольная сумма sha256 — "
                 "<b>/trackrecord</b> в @rsirange_bot</div>"],
                [0.7, 1.9, 2.6], tag)
    return f + _cta(tag, "Смотри сам — все сделки")


def video_signal() -> list[tuple[str, float]]:
    tag = "Как читать сигнал"
    card = ("<div class='card'>" + _kv("Сигнал", "LONG · ETH · 1h", _G)
            + _kv("Вход", "3120.5", _I) + _kv("Стоп", "3078.0", _R)
            + _kv("Активация трейла", "3184.3", _A) + _kv("Отступ", "8.5", _A) + "</div>")
    f = _slide(["<div class='kick'>15 секунд</div>",
                "<div class='h1'>Как читать сигнал RSI Range</div>"], [0.4, 1.8], tag)
    f += _slide(["<span class='ex'>Пример</span>", card], [0.5, 2.4], tag)
    f += _slide(["<span class='ex'>Пример</span>", card,
                 _step(1, _b("Вход", _I) + " — лимитный ордер, ждём цену"),
                 _step(2, _b("Стоп", _R) + " — ставим сразу. Это −1R"),
                 _step(3, _b("Активация", _A) + " — после хода +1.5R включается трейлинг"),
                 _step(4, _b("Отступ", _A) + " — на сколько стоп отстаёт от лучшей цены")],
                [0, 0, 1.8, 1.8, 2.0, 2.2], tag, keep=2)
    f += _slide(["<div class='h2'>Сколько ставить</div>",
                 "<div class='p'>Объём — чтобы стоп стоил <b>1–2% депозита</b></div>",
                 "<div class='p'>Кнопка «🧮 Риск» под сигналом посчитает за тебя</div>",
                 "<div class='p'>Тейк не ставим: выход по трейлингу или стопу</div>"],
                [0.7, 1.8, 1.8, 2.0], tag)
    return f + _cta(tag, "Сигналы — в Telegram")


VIDEOS = {
    "r": ("01_chto_takoe_R", video_r,
          "Что значит «+3R» и почему мы считаем результат в R, а не в процентах. "
          "Все сделки канала — в R, плюсы и минусы. #трейдинг #криптовалюта #риск #фьючерсы"),
    "trail": ("02_trailing_stop_bybit", video_trail,
              "Двигаешь стоп руками раз в час? Биржа может вести трейлинг сама. Как поставить "
              "трейлинг-стоп на Bybit за 4 шага. #bybit #трейлингстоп #трейдинг #крипта"),
    "honest": ("03_pochemu_minusy", video_honest,
               "Почему мы публикуем убыточные сделки. Все исходы — в журнале, проверяемый CSV. "
               "#трейдинг #сигналы #криптовалюта #честно"),
    "signal": ("04_kak_chitat_signal", video_signal,
               "Как читать сигнал за 15 секунд: вход, стоп, активация трейлинга, отступ. "
               "#трейдинг #сигналы #bybit #крипта"),
}


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv(".env")
    OUT.mkdir(parents=True, exist_ok=True)
    keys = sys.argv[1:] or list(VIDEOS)
    for k in keys:
        name, build, caption = VIDEOS[k]
        mp4 = replay.frames_to_mp4(build(), seed=list(VIDEOS).index(k))
        (OUT / f"{name}.mp4").write_bytes(mp4)
        (OUT / f"{name}.txt").write_text(caption + "\n\nНе финансовый совет.\n", encoding="utf-8")
        print(f"{name}.mp4 — {len(mp4) // 1024} КБ", flush=True)


if __name__ == "__main__":
    main()
