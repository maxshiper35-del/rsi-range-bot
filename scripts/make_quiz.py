"""Квизы 9:16 по формату лучшего ролика канала («Если подумал «50» — тебе в этот ролик»,
26.07: 2 311 просмотров на 479 зрителей — 11 секунд, досматривают и крутят по кругу).
Вопрос с подвохом с первого кадра, 3 секунды на подумать, «первая мысль» зачёркнута,
правильный ответ, наглядное доказательство, вывод. ~10–11 с — короче, чем терпение зрителя.

  .venv/bin/python -m scripts.make_quiz        # -> data/content/11..13_kviz_*.mp4 + .txt

Арифметика каждого квиза проверяется в _check() перед рендером: ролик не выйдет с ошибкой.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.delivery import replay
from scripts.make_explainers import OUT

_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:1080px;height:1920px;overflow:hidden;color:#EDEFF3;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;
  background:radial-gradient(900px 700px at 50% 26%,rgba(124,92,252,.20),transparent 65%),
  linear-gradient(180deg,#0E1016,#0A0C10)}
/* безопасная зона Shorts/TikTok: справа кнопки, снизу подпись */
.safe{position:absolute;left:64px;top:210px;width:860px;height:1300px;
  display:flex;flex-direction:column}
.kick{font-size:40px;font-weight:800;letter-spacing:.14em;color:#F2B84B}
.q{font-size:112px;font-weight:900;letter-spacing:-3.5px;line-height:1.03;margin-top:26px}
.q em{font-style:normal;color:#F16565}
.sub{font-size:54px;font-weight:600;color:#C7CBD4;margin-top:30px;line-height:1.25}
.sub b{color:#F1F2F5}
.timer{margin-top:80px;display:flex;align-items:center;gap:32px}
.cd{flex:0 0 160px;height:160px;border-radius:50%;display:flex;align-items:center;
  justify-content:center;font-size:96px;font-weight:900;background:#F16565;color:#fff}
.ask{font-size:50px;font-weight:800}
.bar{height:18px;border-radius:9px;background:rgba(255,255,255,.1);margin-top:20px;overflow:hidden}
.bar div{height:100%;background:#F16565;border-radius:9px}
.wrong{font-size:64px;font-weight:800;color:#8A909C;margin-top:70px}
.wrong span{color:#C7CBD4}
.wrong span.x{color:#F16565;text-decoration:line-through;text-decoration-thickness:8px}
.right{font-size:270px;font-weight:900;letter-spacing:-10px;color:#4ADE9C;line-height:1;
  margin-top:16px;font-variant-numeric:tabular-nums;transform-origin:left center}
.proof{margin-top:36px;font-size:52px;font-weight:600;line-height:1.3;color:#C7CBD4}
.proof b{color:#F1F2F5}
.row{display:grid;grid-template-columns:210px 1fr 160px;align-items:center;gap:22px;
  margin-top:22px}
.row .lb{font-size:44px;color:#8A909C}
.row .tr{height:80px;border-radius:18px;background:rgba(255,255,255,.05);overflow:hidden}
.row .fl{height:100%;border-radius:18px}
.row .v{font-size:56px;font-weight:900;text-align:right}
.chain{display:flex;align-items:baseline;gap:20px;margin-top:26px;font-size:80px;font-weight:900}
.chain i{font-style:normal;font-size:44px;color:#8A909C;font-weight:700}
.dots{display:grid;grid-template-columns:repeat(10,1fr);gap:10px;margin-top:30px}
.dots div{aspect-ratio:1;border-radius:50%;display:flex;align-items:center;justify-content:center;
  font-size:34px;font-weight:900;color:#0B0D11}
.moral{position:absolute;left:0;right:0;top:0;bottom:120px;display:flex;flex-direction:column;
  justify-content:center}
.m1{font-size:112px;font-weight:900;letter-spacing:-3.5px;line-height:1.04}
.m1 em{font-style:normal;color:#F2B84B}
.m2{font-size:56px;font-weight:600;color:#C7CBD4;line-height:1.3;margin-top:44px}
.tag{margin-top:80px;align-self:flex-start;font-size:46px;font-weight:800;padding:24px 34px;
  border-radius:22px;background:rgba(255,255,255,.06);border:2px solid rgba(255,255,255,.1)}
.tag span{color:#8A909C;font-weight:600}
"""

_G, _R, _A = "#4ADE9C", "#F16565", "#F2B84B"


@dataclass(frozen=True)
class Quiz:
    key: str                 # имя файла
    question: str            # HTML, крючок — на экране с первого кадра
    sub: str                 # условие одной строкой
    wrong: str               # «первая мысль» большинства
    right: str               # правильный ответ
    proof: str               # HTML наглядного доказательства
    moral: str               # HTML вывода (крупно)
    moral_sub: str
    title: str               # заголовок Shorts (≤100 символов)
    description: str


def _bars(rows: list[tuple[str, float, str, str]]) -> str:
    return "".join(f"<div class='row'><div class='lb'>{lb}</div><div class='tr'><div class='fl' "
                   f"style='width:{pct:.0f}%;background:{c}'></div></div>"
                   f"<div class='v' style='color:{c}'>{txt}</div></div>"
                   for lb, pct, c, txt in rows)


def _dots(wins: int, losses: int) -> str:
    cells = ([f"<div style='background:{_G}'>+1</div>"] * wins
             + [f"<div style='background:{_R}'>−3</div>"] * losses)
    return f"<div class='dots'>{''.join(cells)}</div>"


QUIZZES = (
    Quiz(
        key="11_kviz_minus90",
        question="Депозит <em>−90%</em>. Сколько нужно заработать, чтобы вернуться?",
        sub="Было <b>1000$</b> — осталось <b>100$</b>.",
        wrong="+90%", right="+900%",
        proof=(_bars([("было", 100, _G, "1000$"), ("осталось", 10, _R, "100$")])
               + "<div class='proof' style='margin-top:24px'>Из <b>100$</b> снова сделать "
               "<b>1000$</b> — это <b>×10</b>.</div>"),
        moral="Глубокую просадку <em>почти не отыграть</em>.",
        moral_sub=("Поэтому размер убытка решают до входа: стоп и риск "
                   "<span style='white-space:nowrap'>1–2%</span> на сделку."),
        title="Если подумал «+90%» — тебе в этот ролик #квиз #просадка #трейдинг",
        description=("Потерял 90% — чтобы вернуться, нужно +900%, а не +90%. Поэтому риск "
                     "считают до входа, а не после. Сигналы с публичным журналом всех сделок — "
                     "в Telegram: @rsi_range. Не финансовый совет. "
                     "#квиз #трейдинг #крипта #просадка #рискменеджмент"),
    ),
    Quiz(
        key="12_kviz_19_dollarov",
        question="Монета упала на <em>90%</em>, потом выросла на 90%. Сколько стало?",
        sub="Купил на <b>100$</b>.",
        wrong="100$", right="19$",
        proof=("<div class='chain'><span>100$</span><i>−90%</i><span style='color:#F16565'>10$"
               "</span><i>+90%</i><span style='color:#4ADE9C'>19$</span></div>"
               "<div class='proof'>+90% считаются уже от <b>10$</b> — это всего <b>+9$</b>.</div>"),
        moral="Проценты считаются <em>от того, что осталось</em>.",
        moral_sub="Падение на X% и рост на X% — не ноль. Минус всегда тяжелее плюса.",
        title="Если подумал «100$» — тебе в этот ролик #квиз #трейдинг #крипта",
        description=("100$ → −90% → +90% = 19$. Проценты считаются от того, что осталось, "
                     "поэтому просадку отыгрывать дольше, чем в неё падать. Сигналы с публичным "
                     "журналом всех сделок — в Telegram: @rsi_range. Не финансовый совет. "
                     "#квиз #трейдинг #крипта #математика"),
    ),
    Quiz(
        key="13_kviz_vinreit",
        question="<em>70%</em> сделок в плюс. Депозит растёт?",
        sub="Прибыльная сделка <b>+1$</b>, убыточная <b>−3$</b>.",
        wrong="Да", right="Нет",
        proof=(_dots(7, 3) + "<div class='proof'>10 сделок: <b>+7$</b> − <b>9$</b> = "
               "<b style='color:#F16565'>−2$</b>. Чем больше сделок — тем глубже минус.</div>"),
        moral="Винрейт <em>сам по себе</em> ничего не значит.",
        moral_sub="Важно, сколько приносит плюс и сколько забирает минус.",
        title="Если подумал «да» — тебе в этот ролик #квиз #винрейт #трейдинг",
        description=("70% сделок в плюс — и всё равно минус: +1$ на прибыльной и −3$ на убыточной "
                     "дают −2$ на каждые 10 сделок. Винрейт без размера прибыли и убытка ничего "
                     "не значит. Сигналы с публичным журналом всех сделок — в Telegram: "
                     "@rsi_range. Не финансовый совет. #квиз #трейдинг #крипта #винрейт"),
    ),
)


def _check() -> None:
    """Арифметика роликов — до рендера."""
    assert round(1000 * (1 - 0.9)) == 100 and round(100 * (1 + 9.0)) == 1000   # +900% = ×10
    assert round(100 * 0.1 * 1.9) == 19                                        # 100 → 10 → 19
    assert 7 * 1 - 3 * 3 == -2                                                 # винрейт 70%
    for q in QUIZZES:
        assert len(q.title) <= 100, q.key
        for leak in ("6226", "LTF", "1:1", "SMC"):                             # граница публикации
            assert leak not in q.title + q.description, (q.key, leak)


def _page(body: str, overlay: str = "") -> str:
    return f"<style>{_CSS}</style><div class='safe'><div>{body}</div>{overlay}</div>"


def _head(q: Quiz) -> str:
    return (f"<div class='kick'>КВИЗ · ДУМАЙ 3 СЕКУНДЫ</div><div class='q'>{q.question}</div>"
            f"<div class='sub'>{q.sub}</div>")


def build(q: Quiz) -> tuple[list[tuple[str, float]], list[tuple[float, str]]]:
    frames: list[tuple[str, float]] = []
    cues: list[tuple[float, str]] = []

    def add(html: str, dur: float, cue: str | None = None) -> None:
        if cue:
            cues.append((sum(d for _h, d in frames), cue))
        frames.append((html, dur))

    head = _head(q)
    for k in range(12):                                     # 3 с обратного отсчёта, бар тает
        n = 3 - k // 4
        frac = 1 - (k + 1) / 12
        timer = (f"<div class='timer'><div class='cd'>{n}</div><div style='flex:1'>"
                 f"<div class='ask'>Ответ — через {n} с</div><div class='bar'>"
                 f"<div style='width:{frac * 100:.0f}%'></div></div></div></div>")
        add(_page(head + timer), 0.25, "tick" if k % 4 == 0 else None)
    add(_page(head + f"<div class='wrong'>Первая мысль: <span>{q.wrong}</span></div>"), 0.55)
    wrong = f"<div class='wrong'>Первая мысль: <span class='x'>{q.wrong}</span> ✕</div>"
    add(_page(head + wrong), 0.5, "loss")
    for sc, op, dur in ((1.35, .4, 0.05), (1.1, .85, 0.05), (1.0, 1.0, 1.1)):
        right = (f"<div class='right' style='transform:scale({sc});opacity:{op}'>"
                 f"{q.right}</div>")
        add(_page(head + wrong + right), dur, "win" if sc > 1.3 else None)
    right = f"<div class='right' style='font-size:190px;margin-top:10px'>{q.right}</div>"
    add(_page(head + right + q.proof), 2.4, "tick")
    moral = (f"<div class='moral'><div class='m1'>{q.moral}</div><div class='m2'>{q.moral_sub}"
             f"</div><div class='tag'>Telegram: @rsi_range <span>· не финансовый совет</span>"
             f"</div></div>")
    add(_page("", overlay=moral), 2.3)
    return frames, cues


def main() -> None:
    _check()
    OUT.mkdir(parents=True, exist_ok=True)
    for i, q in enumerate(QUIZZES):
        frames, cues = build(q)
        mp4 = replay.frames_to_mp4(frames, seed=20 + i, cues=cues)
        (OUT / f"{q.key}.mp4").write_bytes(mp4)
        (OUT / f"{q.key}.txt").write_text(f"{q.title}\n\n{q.description}\n", encoding="utf-8")
        print(f"{q.key}.mp4 — {len(mp4) // 1024} КБ, {sum(d for _h, d in frames):.1f} с",
              flush=True)


if __name__ == "__main__":
    main()
