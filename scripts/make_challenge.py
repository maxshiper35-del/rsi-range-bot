"""Ролик-вызов 9:16 под продвижение: «Одну из последовательностей придумал человек —
угадай за 3 секунды». Крючок и движение с первого кадра, повод для комментария (A или B),
развязка про длинные серии. Нейтрально для модерации рекламы: без крипты, сигналов,
доходностей, ссылок наружу; музыка своя (оригинальный звук).

  .venv/bin/python -m scripts.make_challenge   # -> data/content/09_ugaday_monetka.mp4 + .txt

Цифры: 50 бросков честной монеты — серия одной стороны ≥5 в 82.1% случаев, ≥6 — 54.4%
(точная формула, та же, что в scripts/make_promo.py). «Человеческая» строка — модель людей,
выдумывающих случайность: частые смены стороны, не больше 3 подряд.
"""
from __future__ import annotations

import numpy as np

from app.delivery import replay
from scripts.make_explainers import OUT

_G, _R, _I, _A = replay._GREEN, replay._RED, replay._INDIGO, replay._AMBER
N = 50
REAL_SEED = 19                # 50 бросков: 6 решек подряд (броски 28–33), остальное ≤4
P5 = 82                       # % — серия ≥5 одинаковых за 50 честных бросков

_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:1080px;height:1920px;overflow:hidden;color:#EDEFF3;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;
  background:radial-gradient(900px 700px at 50% 30%,rgba(124,92,252,.20),transparent 65%),
  linear-gradient(180deg,#0E1016,#0A0C10)}
/* безопасная зона TikTok: справа кнопки, снизу подпись — всё важное в этом прямоугольнике */
.safe{position:absolute;left:64px;top:180px;width:860px;height:1330px}
.title{font-size:68px;font-weight:850;letter-spacing:-2px;line-height:1.06;min-height:216px}
.title em{font-style:normal;color:#F2B84B}
.panels{display:grid;grid-template-columns:1fr 1fr;gap:40px;margin-top:34px;position:relative}
.panel{position:relative;border-radius:30px;padding:20px 20px 22px;
  background:rgba(255,255,255,.045);border:2px solid rgba(255,255,255,.09)}
.panel.win{border-color:#4ADE9C;
  box-shadow:0 0 0 4px rgba(74,222,156,.25),0 0 60px rgba(74,222,156,.25)}
.lab{font-size:60px;font-weight:900;text-align:center;line-height:1;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(5,66px);gap:10px;justify-content:center}
.grid i{width:66px;height:66px;border-radius:50%;display:flex;align-items:center;
  justify-content:center;font-style:normal;font-size:30px;font-weight:900;color:#0B0D11}
.grid i.o{background:#9BA8FA}.grid i.p{background:#F2B84B}
.grid i.e{background:rgba(255,255,255,.04)}
.grid i.dim{opacity:.18}
.grid i.hot{box-shadow:0 0 0 5px #F1F2F5,0 0 30px #F2B84B}
.stamp{position:absolute;left:50%;top:46%;font-size:54px;font-weight:900;letter-spacing:.06em;
  padding:14px 26px;border:7px solid #F16565;color:#F16565;border-radius:18px;
  background:rgba(14,16,22,.82);white-space:nowrap}
.real{position:absolute;left:50%;top:46%;font-size:50px;font-weight:900;letter-spacing:.05em;
  padding:14px 24px;border-radius:18px;background:#4ADE9C;color:#0B0D11;white-space:nowrap;
  transform:translate(-50%,-50%) rotate(-6deg)}
.bottom{position:absolute;left:0;right:0;bottom:0;display:flex;align-items:center;gap:30px}
.cd{flex:0 0 120px;height:120px;border-radius:50%;display:flex;align-items:center;
  justify-content:center;font-size:72px;font-weight:900;background:#F16565;color:#fff}
.ask{font-size:44px;font-weight:800;line-height:1.2}
.bar{height:14px;border-radius:8px;background:rgba(255,255,255,.1);margin-top:16px;overflow:hidden}
.bar div{height:100%;background:#F16565;border-radius:8px}
.over{position:absolute;left:0;right:0;top:230px;bottom:150px;display:flex;flex-direction:column;
  justify-content:center;align-items:flex-start;padding:0 10px}
.big{font-size:230px;font-weight:900;letter-spacing:-8px;line-height:.95;color:#F2B84B;
  font-variant-numeric:tabular-nums}
.t2{font-size:64px;font-weight:850;letter-spacing:-1.5px;line-height:1.1}
.t3{font-size:44px;font-weight:600;line-height:1.3;color:#C7CBD4;margin-top:26px}
.t3 b{color:#F1F2F5}
.cta{margin-top:56px;font-size:46px;font-weight:850;padding:26px 40px;border-radius:24px;
  background:linear-gradient(155deg,#4C6EF5,#7C5CFC)}
.small{font-size:24px;color:#6B7280;position:absolute;left:0;bottom:-40px}
"""


def real_flips() -> list[int]:
    return [int(v) for v in np.random.default_rng(REAL_SEED).integers(0, 2, N)]  # 1 = решка


def human_flips() -> list[int]:
    """Как выдумывают люди: меняют сторону ~70% раз и не пишут больше 3 одинаковых."""
    rng = np.random.default_rng(7)
    while True:
        xs = [int(rng.integers(0, 2))]
        while len(xs) < N:
            run = 1
            while run < len(xs) and xs[-1 - run] == xs[-1]:
                run += 1
            flip = run >= 3 or rng.random() < 0.7
            xs.append(1 - xs[-1] if flip else xs[-1])
        if abs(sum(xs) - N // 2) <= 1 and longest(xs)[1] == 3:
            return xs


def longest(xs: list[int]) -> tuple[int, int]:
    best, start, cur, s = 1, 0, 1, 0
    for i in range(1, len(xs)):
        cur, s = (cur + 1, s) if xs[i] == xs[i - 1] else (1, i)
        if cur > best:
            best, start = cur, s
    return start, best


def runs_of(xs: list[int], k: int) -> set[int]:
    """Индексы всех серий длиной ≥ k."""
    out: set[int] = set()
    s = 0
    for i in range(1, len(xs) + 1):
        if i == len(xs) or xs[i] != xs[i - 1]:
            if i - s >= k:
                out.update(range(s, i))
            s = i
    return out


def _grid(xs: list[int], rows: int = 10, hot: set[int] | None = None,
          new_row: int | None = None) -> str:
    cells = []
    for i in range(N):
        r = i // 5
        if r >= rows:
            cells.append("<i class='e'></i>")
            continue
        cls = "p" if xs[i] else "o"
        if hot is not None:
            cls += " hot" if i in hot else " dim"
        st = " style='transform:scale(.6);opacity:.5'" if r == new_row else ""
        cells.append(f"<i class='{cls}'{st}>{'Р' if xs[i] else 'О'}</i>")
    return f"<div class='grid'>{''.join(cells)}</div>"


def _page(title: str, a: str, b: str, bottom: str = "", a_extra: str = "",
          b_extra: str = "", b_win: bool = False, overlay: str = "", dim: bool = False) -> str:
    op = " style='opacity:.12'" if dim else ""
    return (f"<style>{_CSS}</style><div class='safe'><div class='title'>{title}</div>"
            f"<div class='panels'{op}><div class='panel'><div class='lab'>A</div>{a}{a_extra}</div>"
            f"<div class='panel{' win' if b_win else ''}'><div class='lab'>B</div>{b}{b_extra}"
            f"</div></div><div class='bottom'>{bottom}</div>{overlay}</div>")


def _countdown(n: int, frac: float) -> str:
    return (f"<div class='cd'>{n}</div><div style='flex:1'><div class='ask'>Пиши в комментариях:"
            f" A или B?</div><div class='bar'><div style='width:{frac * 100:.0f}%'></div></div>"
            f"</div>")


def build() -> tuple[list[tuple[str, float]], list[tuple[float, str]]]:
    real, fake = real_flips(), human_flips()
    a, b = fake, real                          # A — придумал человек, B — настоящая
    hook = "Одну из них придумал <em>человек</em>. Угадаешь за 3 секунды?"
    frames: list[tuple[str, float]] = []
    cues: list[tuple[float, str]] = []

    def add(html: str, dur: float, cue: str | None = None) -> None:
        if cue:
            cues.append((sum(d for _h, d in frames), cue))
        frames.append((html, dur))

    for rows in range(1, 11):                  # монеты сыплются ряд за рядом — движение сразу
        add(_page(hook, _grid(a, rows, new_row=rows - 1), _grid(b, rows, new_row=rows - 1),
                  _countdown(3, 1.0)), 0.07)
    for k, n in enumerate((3, 3, 2, 2, 1, 1)):
        add(_page(hook, _grid(a), _grid(b), _countdown(n, 1 - (k + 1) / 6)), 0.5,
            "tick" if k % 2 == 0 else None)
    title = "Придумал человек — <em>A</em>"
    for sc, opct, dur in ((1.7, .35, 0.05), (1.25, .8, 0.05), (1.0, 1.0, 1.3)):
        stamp = (f"<div class='stamp' style='transform:translate(-50%,-50%) rotate(-12deg) "
                 f"scale({sc});opacity:{opct}'>ПОДДЕЛКА</div>")
        add(_page(title, _grid(a), _grid(b), a_extra=stamp,
                  b_extra="<div class='real'>НАСТОЯЩАЯ</div>" if dur > 1 else "",
                  b_win=dur > 1), dur, "win" if sc > 1.5 else None)
    s, k = longest(b)
    add(_page(f"В настоящей — <em>{k} решек подряд</em>", _grid(a, hot=set()),
              _grid(b, hot=set(range(s, s + k))), b_win=True), 2.2, "tick")
    add(_page("Человек больше <em>3 подряд</em> не пишет: «выглядит неслучайно»",
              _grid(a, hot=runs_of(a, 3)), _grid(b, hot=set())), 2.4, "tick")
    for v in (0, 15, 31, 46, 60, 71, 78, P5):
        over = (f"<div class='over'><div class='t2'>50 честных бросков:</div>"
                f"<div class='big'>{v}%</div><div class='t2'>серия из 5+ одинаковых</div>"
                f"<div class='t3'>В придуманных — <b>почти никогда</b>.</div></div>")
        add(_page("", _grid(a), _grid(b), overlay=over, dim=True), 2.2 if v == P5 else 0.06)
    add(_page("", _grid(a), _grid(b), dim=True, overlay=(
        "<div class='over'><div class='t2'>Длинные серии — это и есть случайность.</div>"
        "<div class='t3'>5 неудач подряд — ещё <b>не значит</b>, что ты всё делаешь не так."
        "</div></div>")), 2.6)
    add(_page("", _grid(a), _grid(b), dim=True, overlay=(
        "<div class='over'><div class='t2'>Угадал с первого раза?</div>"
        "<div class='t3'>Пиши A или B в комментариях.</div>"
        "<div class='cta'>Подпишись — ещё задачки на вероятность</div></div>"
        "<div class='small'>Расчёт: точная формула для 50 бросков честной монеты. "
        "Образовательный ролик.</div>")), 2.6, "tick")
    return frames, cues


CAPTION = ("Одну последовательность придумал человек, другая — честная монетка. "
           "Угадал за 3 секунды? Пиши A или B 👇 #вероятность #математика #задачка "
           "#психология")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    frames, cues = build()
    mp4 = replay.frames_to_mp4(frames, seed=9, cues=cues)
    (OUT / "09_ugaday_monetka.mp4").write_bytes(mp4)
    (OUT / "09_ugaday_monetka.txt").write_text(CAPTION + "\n", encoding="utf-8")
    print(f"09_ugaday_monetka.mp4 — {len(mp4) // 1024} КБ, "
          f"{sum(d for _h, d in frames):.1f} с", flush=True)


if __name__ == "__main__":
    main()
