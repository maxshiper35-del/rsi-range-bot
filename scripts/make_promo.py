"""Нейтральный ролик 9:16 под продвижение (TikTok Promote): математика серий на монетке и
цена риска. Без крипты, сигналов, доходностей, ссылок наружу и чужой музыки — то, что
модерация рекламы режет у финансового контента.

  .venv/bin/python -m scripts.make_promo      # -> data/content/08_monetka_promo.mp4 + .txt

Цифры — точный расчёт (динамика по длине текущей серии), сверен Монте-Карло 50 000 × 100
бросков: серия одной стороны ≥5 — 97.2%, ≥6 — 80.7%, ≥7 — 54.2%, медиана самой длинной — 7.
"""
from __future__ import annotations

import numpy as np

from app.delivery import replay
from scripts.make_explainers import _EX_CSS, OUT, _b, _kv

_G, _R, _I, _A = replay._GREEN, replay._RED, replay._INDIGO, replay._AMBER
SEED = 14                     # 100 бросков: самая длинная серия — 7 решек (броски 64–70)
TAG = "Проверка интуиции"

_PROMO_CSS = """
.grid{display:grid;grid-template-columns:repeat(10,1fr);gap:12px;margin-top:8px}
.grid i{display:flex;align-items:center;justify-content:center;aspect-ratio:1;border-radius:50%;
  font-style:normal;font-size:30px;font-weight:800;color:#0B0D11}
.grid i.e{background:rgba(255,255,255,.05);border:2px dashed rgba(255,255,255,.08)}
.grid i.o{background:#9BA8FA}.grid i.p{background:#F2B84B}
.grid.hl i{opacity:.22}.grid.hl i.run{opacity:1;box-shadow:0 0 0 6px #F1F2F5,0 0 34px #F2B84B}
.leg{display:flex;gap:34px;font-size:30px;color:#8A909C;margin-top:22px;align-items:center}
.leg span{display:inline-block;width:26px;height:26px;border-radius:50%;margin-right:10px;
  vertical-align:-4px}
.bar{margin-top:18px}.bar .lb{display:flex;justify-content:space-between;font-size:40px;
  font-weight:700}.bar .tr{height:44px;border-radius:12px;background:rgba(255,255,255,.05);
  margin-top:12px;overflow:hidden}.bar .fl{height:100%;border-radius:12px}
"""


def _flips() -> list[int]:
    return [int(v) for v in np.random.default_rng(SEED).integers(0, 2, 100)]   # 1 = решка


def _longest(xs: list[int]) -> tuple[int, int]:
    best, start, cur, s = 1, 0, 1, 0
    for i in range(1, len(xs)):
        cur, s = (cur + 1, s) if xs[i] == xs[i - 1] else (1, i)
        if cur > best:
            best, start = cur, s
    return start, best


def _page(inner: str) -> str:
    return (f"<style>{replay._CSS}{_EX_CSS}{_PROMO_CSS}</style><div class='page'>"
            f"<div class='top'><div class='brand'><div class='mark'>R</div><div>"
            f"<div class='name'>RSI Range</div><div class='handle'>математика риска</div>"
            f"</div></div><div class='eyebrow'>{TAG}</div></div><div class='body'>{inner}</div>"
            f"<div class='foot'><div>образовательный ролик</div><div>не финансовый совет</div>"
            f"</div></div>")


def _slide(items: list[str], holds: list[float], keep: int = 0) -> list[tuple[str, float]]:
    """Элементы проявляются по одному; первые keep — сразу, одним кадром (крючок ролика
    должен быть на экране с первой секунды)."""
    frames = []
    if keep:
        frames.append((_page("".join(f"<div class='it'>{x}</div>" for x in items[:keep])),
                       holds[keep - 1]))
    for k in range(keep, len(items)):
        for t in (0.35, 0.72):
            part = "".join(f"<div class='it'>{x}</div>" for x in items[:k])
            shift = f"opacity:{t};transform:translateY({(1 - t) * 26:.0f}px)"
            part += f"<div class='it' style='{shift}'>{items[k]}</div>"
            frames.append((_page(part), 0.05))
        frames.append((_page("".join(f"<div class='it'>{x}</div>" for x in items[:k + 1])),
                       holds[k]))
    return frames


def _grid(xs: list[int], shown: int, run: tuple[int, int] | None = None) -> str:
    cells = []
    for i in range(100):
        if i >= shown:
            cells.append("<i class='e'></i>")
            continue
        hit = run is not None and run[0] <= i < run[0] + run[1]
        cls = ("p" if xs[i] else "o") + (" run" if hit else "")
        cells.append(f"<i class='{cls}'>{'Р' if xs[i] else 'О'}</i>")
    return f"<div class='grid{' hl' if run else ''}'>{''.join(cells)}</div>"


def _toss_scene() -> tuple[list[tuple[str, float]], float]:
    xs = _flips()
    run = _longest(xs)
    leg = (f"<div class='leg'><div><span style='background:{_I}'></span>О — орёл</div>"
           f"<div><span style='background:{_A}'></span>Р — решка</div></div>")
    frames = []
    for shown in range(5, 101, 5):
        head = f"<div class='h2'>Бросаем… <span style='color:#7B8190'>{shown}/100</span></div>"
        frames.append((_page(f"<div class='it'>{head}</div><div class='it'>"
                             f"{_grid(xs, shown)}{leg}</div>"), 0.16))
    frames[-1] = (frames[-1][0], 0.5)
    t_hit = sum(d for _h, d in frames)
    side = "решек" if xs[run[0]] else "орлов"
    head = f"<div class='h2'>{_b(f'{run[1]} {side} подряд', _A)}</div>"
    frames.append((_page(f"<div class='it'>{head}</div><div class='it'>"
                         f"{_grid(xs, 100, run)}{leg}</div>"), 2.2))
    return frames, t_hit


def _bar(label: str, value: str, pct: float, color: str) -> str:
    return (f"<div class='bar'><div class='lb'><span>{label}</span>"
            f"<span style='color:{color}'>{value}</span></div><div class='tr'>"
            f"<div class='fl' style='width:{pct:.0f}%;background:{color}'></div></div></div>")


def build() -> tuple[list[tuple[str, float]], list[tuple[float, str]]]:
    frames = _slide(["<div class='kick'>Проверка интуиции</div>",
                     "<div class='h1'>Монетка.<br>100 бросков.</div>",
                     "<div class='h2'>Сколько раз подряд выпадет одна и та же сторона?</div>",
                     "<div class='p'>Обычно отвечают: <b>3–4 раза</b>.</div>"],
                    [0, 0, 2.2, 1.4], keep=3)
    toss, t_hit = _toss_scene()
    cues = [(sum(d for _h, d in frames) + t_hit, "tick")]
    frames += toss
    frames += _slide(["<div class='kick'>Если повторить опыт много раз</div>",
                      "<div class='h2'>Серия одной стороны за 100 бросков:</div>",
                      "<div class='card'>" + _kv("5 раз и больше", "97%", _A)
                      + _kv("6 раз и больше", "81%", _A)
                      + _kv("7 раз и больше", "54%", _A) + "</div>",
                      "<div class='p'>Длинная серия — <b>норма</b>, а не знак, что "
                      "«что-то сломалось».</div>"],
                     [0.3, 0.8, 2.2, 1.8])
    frames += _slide(["<div class='kick'>Где это важно</div>",
                      "<div class='h2'>Пусть решка — неудачная попытка. "
                      "7 неудач подряд стоят:</div>",
                      "<div class='card'>"
                      + _bar("Риск 1% на попытку", "−6,8%", 6.8, _G)
                      + _bar("Риск 10% на попытку", "−52%", 52.2, _R) + "</div>",
                      "<div class='p'>Серию не отменить. <b>Выбрать её цену</b> — можно.</div>"],
                     [0.3, 1.0, 2.4, 2.0])
    frames += _slide(["<div class='h1'>Подпишитесь</div>",
                      "<div class='p'>Разбираю математику риска простыми словами.</div>",
                      "<div class='small'>Расчёт: 100 бросков честной монеты, точная формула "
                      "и 50 000 повторов. Образовательный ролик, не финансовый совет.</div>"],
                     [0.6, 1.2, 2.4])
    return frames, cues


CAPTION = ("Подбросьте монетку 100 раз: 7 одинаковых подряд выпадает больше чем в половине "
           "случаев. Длинные серии — это норма. Вопрос только в цене каждой попытки. "
           "#математика #вероятность #финграмотность #риск")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    frames, cues = build()
    mp4 = replay.frames_to_mp4(frames, seed=8, cues=cues)
    (OUT / "08_monetka_promo.mp4").write_bytes(mp4)
    (OUT / "08_monetka_promo.txt").write_text(CAPTION + "\n", encoding="utf-8")
    print(f"08_monetka_promo.mp4 — {len(mp4) // 1024} КБ, "
          f"{sum(d for _h, d in frames):.1f} с", flush=True)


if __name__ == "__main__":
    main()
