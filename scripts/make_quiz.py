"""Квизы 9:16 по формату лучшего ролика канала («Если подумал «50» — тебе в этот ролик»,
26.07: 2 311 просмотров на 479 зрителей — 11 секунд, досматривают и крутят по кругу).
Вопрос с подвохом с первого кадра, 3 секунды на подумать, «первая мысль» зачёркнута,
правильный ответ, наглядное доказательство, вывод. ~10–11 с — короче, чем терпение зрителя.

  .venv/bin/python -m scripts.make_quiz [14 15 …]  # -> data/content/NN_kviz_*.mp4 + .txt

Арифметика каждого квиза проверяется в _check() перед рендером: ролик не выйдет с ошибкой.
"""
from __future__ import annotations

import html as _html
import math
import re
from dataclasses import dataclass
from fractions import Fraction as F
from pathlib import Path

from app.delivery import replay
from scripts import shorts_kit as kit
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
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:28px}
.tiles div{border-radius:18px;padding:14px 0;text-align:center;font-size:50px;font-weight:900;
  color:#0B0D11}
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


def _dots(wins: int, losses: int, win: str = "+1", loss: str = "−3", cols: int = 10) -> str:
    fs = ";font-size:64px" if cols != 10 else ""          # крупные кружки — крупные подписи
    cells = ([f"<div style='background:{_G}{fs}'>{win}</div>"] * wins
             + [f"<div style='background:{_R}{fs}'>{loss}</div>"] * losses)
    style = f" style='grid-template-columns:repeat({cols},1fr);gap:18px'" if cols != 10 else ""
    return f"<div class='dots'{style}>{''.join(cells)}</div>"


def _tiles(items: list[tuple[str, str]]) -> str:
    return ("<div class='tiles'>" + "".join(f"<div style='background:{c}'>{t}</div>"
                                            for t, c in items) + "</div>")


def p_streak(n: int, k: int, q: float) -> float:
    """Точная вероятность хотя бы одной серии из ≥k неудач подряд за n попыток
    (вероятность неудачи q) — динамика по длине текущей серии."""
    state = [1.0] + [0.0] * (k - 1)
    hit = 0.0
    for _ in range(n):
        nxt = [0.0] * k
        for run, pr in enumerate(state):
            nxt[0] += pr * (1 - q)
            if run + 1 >= k:
                hit += pr * q
            else:
                nxt[run + 1] += pr * q
        state = nxt
    return hit


_GRAY = "#8A909C"
_TAIL = ("Сигналы с публичным журналом всех сделок — в Telegram: @rsi_range. "
         "Не финансовый совет.")


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
    Quiz(
        key="14_kviz_5_stopov",
        question="Винрейт <em>60%</em>. Шанс поймать 5 стопов подряд?",
        sub="За <b>100 сделок</b>.",
        wrong="1%", right="46%",
        proof=(_bars([("кажется", 2, _GRAY, "1%"), ("на деле", 46, _R, "46%")])
               + "<div class='proof'>1% — это шанс для пяти <b>конкретных</b> сделок. "
               "А мест для серии в 100 сделках — почти сто.</div>"),
        moral="Серия стопов — <em>не поломка</em>, а норма.",
        moral_sub="Её не избежать. Можно только выбрать её цену — риск на сделку.",
        title="Если подумал «1%» — тебе в этот ролик #квиз #вероятность #трейдинг",
        description=("Винрейт 60%, 100 сделок: шанс хотя бы раз получить 5 стопов подряд — 46%, "
                     "а не 1%. Серии убытков — норма, поэтому риск на сделку держат маленьким. "
                     "Расчёт точный. " + _TAIL + " #квиз #трейдинг #крипта #вероятность"),
    ),
    Quiz(
        key="15_kviz_1_v_den",
        question="<em>+1%</em> в день. Сколько это за год?",
        sub="Каждый день +1% к тому, что уже есть. <b>365 дней</b>.",
        wrong="+365%", right="×37,8",
        proof=(_bars([("100 дн.", 7, _A, "×2,7"), ("200 дн.", 19, _A, "×7,3"),
                      ("365 дн.", 100, _G, "×37,8")])
               + "<div class='proof'>Из <b>1000$</b> вышло бы почти <b>38&nbsp;000$</b>.</div>"),
        moral="Кто обещает <em>+1% в день</em>, обещает ×37 за год.",
        moral_sub="Так не торгует никто. Это признак развода, а не стратегии.",
        title="Если подумал «+365%» — тебе в этот ролик #квиз #трейдинг #крипта",
        description=("+1% в день со сложным процентом — это ×37,8 за год: из 1000$ вышло бы почти "
                     "38 000$. Поэтому обещания «+1% в день» — признак развода. "
                     + _TAIL + " #квиз #трейдинг #крипта #скам"),
    ),
    Quiz(
        key="16_kviz_usrednenie",
        question="Купил по <em>100$</em>, докупил по 50$. Средняя цена?",
        sub="Оба раза — на одну и ту же сумму: <b>100$</b>.",
        wrong="75$", right="66,7$",
        proof=("<div class='proof'>100$ по цене 100$ = <b>1 монета</b><br>100$ по цене 50$ = "
               "<b>2 монеты</b><br>200$ ÷ 3 монеты = <b style='color:#4ADE9C'>66,7$</b></div>"),
        moral="Усреднение — это <em>больше денег</em> в сделке, которая уже против тебя.",
        moral_sub="Средняя цена ниже, но в убыточной сделке теперь вдвое больше денег.",
        title="Если подумал «75$» — тебе в этот ролик #квиз #усреднение #трейдинг",
        description=("Купил на 100$ по 100$ и на 100$ по 50$: средняя цена 66,7$, а не 75$ — "
                     "дешёвых монет куплено вдвое больше. Но и денег в убыточной сделке стало "
                     "вдвое больше. " + _TAIL + " #квиз #трейдинг #крипта #усреднение"),
    ),
    Quiz(
        key="17_kviz_martingeil",
        question="Удваиваешь ставку после <em>проигрыша</em>. Какая 8-я ставка?",
        sub="Первая — <b>10$</b>, проиграл 7 раз подряд.",
        wrong="80$", right="1280$",
        proof=(_tiles([(str(10 * 2 ** i), _R) for i in range(7)] + [("1280", _A)])
               + "<div class='proof'>Проиграно уже <b>1270$</b> — ради выигрыша <b>10$</b>."
               "</div>"),
        moral="Удвоение не отменяет серию — оно <em>делает её смертельной</em>.",
        moral_sub="7 проигрышей подряд за 100 бросков монеты — примерно в 1 случае из 3.",
        title="Если подумал «80$» — тебе в этот ролик #квиз #мартингейл #трейдинг",
        description=("Удвоение после проигрыша с 10$: после 7 проигрышей подряд 8-я ставка — "
                     "1280$, а проиграно уже 1270$ ради выигрыша 10$. Такая серия за 100 бросков "
                     "монеты бывает примерно в 1 случае из 3. "
                     + _TAIL + " #квиз #трейдинг #крипта #мартингейл"),
    ),
    Quiz(
        key="18_kviz_komissii",
        question="Сколько депозита съедят <em>комиссии</em> за 100 сделок?",
        sub="Сделка на весь депозит, плечо <b>×10</b>, комиссия <b>0,05%</b> за вход и выход.",
        wrong="5%", right="63%",
        proof=("<div class='proof'>Одна сделка: 0,1% × 10 = <b>1% депозита</b>.<br>"
               "100 сделок: 0,99¹⁰⁰ ≈ 0,37 — осталось <b>37%</b>.</div>"),
        moral="Плечо умножает <em>и комиссию</em>.",
        moral_sub="Даже если сделки в ноль, биржа забирает своё с каждого входа и выхода.",
        title="Если подумал «5%» — тебе в этот ролик #квиз #комиссии #трейдинг",
        description=("Плечо ×10 и комиссия 0,05% за вход и выход: каждая сделка на весь депозит "
                     "стоит 1% депозита, 100 сделок — около 63%, даже если сделки в ноль. "
                     + _TAIL + " #квиз #трейдинг #крипта #комиссии"),
    ),
    Quiz(
        key="19_kviz_25_procentov",
        question="Прибыль в <em>3 раза</em> больше убытка. Какой нужен винрейт?",
        sub="Чтобы <b>не терять</b>: тейк +3$, стоп −1$.",
        wrong="50%", right="25%",
        proof=(_dots(1, 3, "+3", "−1", cols=4)
               + "<div class='proof'>1 из 4 в плюс: <b>+3$</b> − <b>3$</b> = <b>0</b>. "
               "Всё, что выше 25%, — уже плюс (без учёта комиссий).</div>"),
        moral="Винрейт без соотношения прибыли к убытку — <em>полкартины</em>.",
        moral_sub="Можно ошибаться в 3 сделках из 4 и не терять, если прибыль втрое больше убытка.",
        title="Если подумал «50%» — тебе в этот ролик #квиз #винрейт #трейдинг",
        description=("Тейк +3$, стоп −1$: чтобы не терять, хватает 25% прибыльных сделок, а не "
                     "50% (без учёта комиссий). Винрейт без соотношения прибыли к убытку — "
                     "полкартины. " + _TAIL + " #квиз #трейдинг #крипта #винрейт"),
    ),
    Quiz(
        key="20_kviz_risk_10",
        question="Риск <em>10%</em> на сделку. 10 стопов подряд. Сколько осталось?",
        sub="Было <b>1000$</b>, риск — 10% от текущего депозита.",
        wrong="0$", right="349$",
        proof=(_bars([("риск 10%", 35, _R, "349$"), ("риск 1%", 90, _G, "904$")])
               + "<div class='proof'>Та же серия при риске 1% — минус&nbsp;<b>96$</b>.</div>"),
        moral="Серию переживает не тот, кто угадал, а тот, у кого <em>маленький риск</em>.",
        moral_sub="10 стопов при 1% — неприятно. При 10% — две трети депозита.",
        title="Если подумал «0$» — тебе в этот ролик #квиз #рискменеджмент #трейдинг",
        description=("10 стопов подряд при риске 10% от текущего депозита: из 1000$ остаётся 349$. "
                     "При риске 1% — 904$. Серию переживает тот, у кого маленький риск. "
                     + _TAIL + " #квиз #трейдинг #крипта #рискменеджмент"),
    ),
    # партия 30.10: темы не повторяют 11–20; каждое число — в _check() двумя способами
    Quiz(
        key="21_kviz_plecho",
        question="Плечо <em>×10</em>, цена против тебя −10%. Сколько потерял?",
        sub="Своих денег в сделке — <b>100$</b>.",
        wrong="10%", right="100%",
        proof=(_bars([("позиция", 100, _A, "1000$"), ("своих", 10, _G, "100$")])
               + "<div class='proof'>−10% от позиции <b>1000$</b> = <b>−100$</b> — все твои "
               "деньги. Биржа ликвидирует ещё раньше.</div>"),
        moral="Плечо умножает <em>и убыток</em>.",
        moral_sub="При ×10 движение цены на 10% забирает все деньги в сделке. Свой стоп "
                  "ставят раньше ликвидации.",
        title="Если подумал «10%» — тебе в этот ролик #квиз #плечо #трейдинг",
        description=("Плечо ×10: на 100$ своих — позиция 1000$. Цена −10% — это −100$, то есть все "
                     "деньги в сделке, а ликвидация наступит ещё раньше (поддерживающая маржа и "
                     "комиссии). Плечо умножает и убыток. "
                     + _TAIL + " #квиз #трейдинг #крипта #плечо #ликвидация"),
    ),
    Quiz(
        key="22_kviz_funding",
        question="Фандинг <em>0,01%</em> раз в 8 часов. Сколько съест за месяц?",
        sub="Лонг с плечом <b>×10</b>, держишь <b>30 дней</b>. Считаем от своих денег.",
        wrong="0,01%", right="9%",
        proof=("<div class='proof'>3 раза в день × 30 дней = <b>90 платежей</b>.<br>"
               "90 × 0,01% = <b>0,9%</b> позиции.<br>Позиция ×10 к твоим деньгам → "
               "<b style='color:#F16565'>9%</b>.</div>"),
        moral="Фандинг копеечный, <em>пока держишь недолго</em>.",
        moral_sub="Он берётся с позиции, а не с твоих денег, — плечо умножает и его.",
        title="Если подумал «0,01%» — тебе в этот ролик #квиз #фандинг #трейдинг",
        description=("Фандинг 0,01% каждые 8 часов — это 90 платежей за 30 дней, 0,9% от позиции. "
                     "С плечом ×10 — 9% твоих денег за месяц при неизменной цене. Ставка фандинга "
                     "меняется и бывает отрицательной; здесь посчитано для постоянных 0,01%, "
                     "которые платит лонг. " + _TAIL + " #квиз #трейдинг #крипта #фандинг"),
    ),
    Quiz(
        key="23_kviz_proskalzyvanie",
        question="Стоп <em>−1%</em>. Вход и стоп хуже на 0,2%. Насколько вырос убыток?",
        sub="Проскальзывание: <b>0,2%</b> на входе и ещё <b>0,2%</b> на стопе.",
        wrong="0,2%", right="+40%",
        proof=(_bars([("план", 71, _A, "1%"), ("по факту", 100, _R, "1,4%")])
               + "<div class='proof'>1% + 0,2% + 0,2% = <b>1,4%</b> вместо 1%. Убыток на "
               "<b>40%</b> больше плана.</div>"),
        moral="Короткий стоп <em>дороже</em>, чем кажется.",
        moral_sub=("Проскальзывание 0,4% при стопе 4% — это +10% к убытку, при стопе 1% — "
                   "уже +40%."),
        title="Если подумал «0,2%» — тебе в этот ролик #квиз #проскальзывание #трейдинг",
        description=("Стоп −1% и проскальзывание по 0,2% на входе и на стопе: убыток 1,4% вместо "
                     "1% — на 40% больше плана. Чем ближе стоп, тем большую долю риска съедают "
                     "проскальзывание и комиссии. "
                     + _TAIL + " #квиз #трейдинг #крипта #проскальзывание"),
    ),
    Quiz(
        key="24_kviz_udvoit",
        question="<em>+10%</em> за сделку. Сколько сделок, чтобы удвоить депозит?",
        sub="Каждая сделка — +10% к тому, что уже есть.",
        wrong="10", right="8",
        proof=(_tiles([(f"{1.1 ** i:.2f}".replace(".", ","), _A) for i in range(1, 8)]
                      + [("2,14", _G)])
               + "<div class='proof'>Каждые +10% — от уже выросшей суммы: после 7 сделок "
               "<b>×1,95</b>, после 8 — <b style='color:#4ADE9C'>×2,14</b>.</div>"),
        moral="Сложный процент работает <em>в обе стороны</em>.",
        moral_sub="8 плюсов подряд удваивают депозит, а 7 стопов по 10% забирают больше половины.",
        title="Если подумал «10» — тебе в этот ролик #квиз #сложныйпроцент #трейдинг",
        description=("+10% за сделку: депозит удваивается не за 10, а за 8 прибыльных сделок "
                     "подряд — каждые 10% считаются от уже выросшей суммы (1,1⁸ ≈ 2,14). Но "
                     "сложный процент работает и в обратную сторону: 7 стопов по 10% забирают "
                     "больше половины депозита. "
                     + _TAIL + " #квиз #трейдинг #крипта #сложныйпроцент"),
    ),
    Quiz(
        key="25_kviz_seriya_plusov",
        question="Винрейт <em>60%</em>. Шанс, что следующие 5 сделок все в плюс?",
        sub="Каждая сделка — <b>60%</b> на плюс, независимо от прошлых.",
        wrong="60%", right="7,8%",
        proof=(_bars([("1 сделка", 60, _G, "60%"), ("3 подряд", 22, _A, "22%"),
                      ("5 подряд", 8, _R, "7,8%")])
               + "<div class='proof'>0,6⁵ ≈ <b>0,078</b> — примерно "
               "<b>1&nbsp;раз из&nbsp;13</b>.</div>"),
        moral="Удача <em>не копится</em>.",
        moral_sub="После трёх плюсов подряд риск не повышают: следующая сделка — снова 60 на 40.",
        title="Если подумал «60%» — тебе в этот ролик #квиз #вероятность #трейдинг",
        description=("Винрейт 60%: шанс, что пять сделок подряд будут в плюс, — 0,6⁵ ≈ 7,8%, "
                     "примерно 1 раз из 13. Серия плюсов — редкость, а не закономерность, поэтому "
                     "после удачной серии риск не повышают. "
                     + _TAIL + " #квиз #трейдинг #крипта #вероятность"),
    ),
    Quiz(
        key="26_kviz_razmer_pozicii",
        question="Риск <em>1%</em>, стоп 2%. На сколько открывать позицию?",
        sub="Депозит <b>1000$</b>, стоп — в 2% от цены входа.",
        wrong="10$", right="500$",
        proof=("<div class='proof'>Риск 1% = <b>10$</b> — столько потеряешь на стопе.<br>"
               "10$ ÷ 2% = <b style='color:#4ADE9C'>500$</b> позиции.<br>"
               "Проверка: 500$ × 2% = <b>10$</b>.</div>"),
        moral="Риск 1% — это <em>сколько потеряешь</em>, а не сколько вложишь.",
        moral_sub="Размер позиции = сумма риска ÷ расстояние до стопа.",
        title="Если подумал «10$» — тебе в этот ролик #квиз #рискменеджмент #трейдинг",
        description=("Риск 1% от 1000$ — это 10$, которые теряешь на стопе. При стопе в 2% от "
                     "входа позиция — 500$: 500$ × 2% = 10$. Риск на сделку и размер позиции — "
                     "разные вещи. " + _TAIL + " #квиз #трейдинг #крипта #рискменеджмент"),
    ),
    Quiz(
        key="27_kviz_ozhidanie",
        question="Только <em>40%</em> сделок в плюс. Сколько в среднем даёт сделка?",
        sub="Плюс — <b>+200$</b>, минус — <b>−100$</b>.",
        wrong="Минус", right="+20$",
        proof=(_tiles([("+200", _G)] * 4 + [("−100", _R)] * 6)
               + "<div class='proof'>10 сделок: <b>+800$</b> − <b>600$</b> = "
               "<b style='color:#4ADE9C'>+200$</b>, то есть <b>+20$</b> на сделку "
               "(без комиссий).</div>"),
        moral="Считают не винрейт, а <em>ожидание сделки</em>.",
        moral_sub="Доля плюсов × прибыль − доля минусов × убыток: 0,4 × 200 − 0,6 × 100 = +20$.",
        title="Если подумал «минус» — тебе в этот ролик #квиз #матожидание #трейдинг",
        description=("Только 40% сделок в плюс, но плюс +200$, а минус −100$: на 10 сделок "
                     "+800$ − 600$ = +200$, в среднем +20$ на сделку (без комиссий). Считают не "
                     "винрейт, а ожидание сделки. "
                     + _TAIL + " #квиз #трейдинг #крипта #матожидание"),
    ),
    Quiz(
        key="28_kviz_korrelyaciya",
        question="8 альтов в лонг по <em>2%</em> риска. Биткоин упал — все в стоп. Итог?",
        sub="Риск каждой сделки — <b>2%</b> депозита <b>1000$</b>.",
        wrong="2%", right="16%",
        proof=(_tiles([("−20$", _R)] * 8)
               + "<div class='proof'>8 × 20$ = <b>160$</b> — <b style='color:#F16565'>16%</b> "
               "депозита за одно движение.</div>"),
        moral="Альты падают <em>вместе с биткоином</em>.",
        moral_sub="8 сделок в одну сторону — это одна большая ставка, а не восемь маленьких.",
        title="Если подумал «2%» — тебе в этот ролик #квиз #рискменеджмент #трейдинг",
        description=("8 лонгов по альтам с риском 2% на каждый: когда биткоин падает, альты "
                     "обычно падают вместе с ним, и стопы срабатывают разом — минус 160$, 16% "
                     "депозита 1000$. Сделки в одну сторону складывают риск. "
                     + _TAIL + " #квиз #трейдинг #крипта #альткоины"),
    ),
    Quiz(
        key="29_kviz_otygratsya",
        question="Депозит <em>−20%</em>. Сколько нужно, чтобы отыграться?",
        sub="Было <b>1000$</b> — стало <b>800$</b>.",
        wrong="+20%", right="+25%",
        proof=(_bars([("+20%", 96, _A, "960$"), ("+25%", 100, _G, "1000$")])
               + "<div class='proof'>+20% от <b>800$</b> — это только <b>960$</b>. До 1000$ "
               "нужно <b>+25%</b>.</div>"),
        moral="Минус 20% требует <em>+25%</em>&nbsp;— и терпения.",
        moral_sub="Попытка отыграться одной большой сделкой — самый частый путь ко второму минусу.",
        title="Если подумал «+20%» — тебе в этот ролик #квиз #просадка #трейдинг",
        description=("Минус 20%: из 1000$ осталось 800$, и +20% вернут только 960$. Чтобы выйти в "
                     "ноль, нужно +25%. Попытка отыграться одной большой сделкой — частый путь ко "
                     "второму минусу. " + _TAIL + " #квиз #трейдинг #крипта #просадка"),
    ),
    Quiz(
        key="30_kviz_monetka",
        question="6 из 10 сделок в плюс. Монетка <em>так умеет</em>?",
        sub="Честная монетка, 10 бросков: <b>6 орлов или больше</b>.",
        wrong="Нет", right="38%",
        proof=(_bars([("6+ из 10", 38, _A, "38%")])
               + "<div class='proof'>Шанс <b>37,7%</b> — чаще, чем каждый третий раз. "
               "А 60 и больше из&nbsp;100&nbsp;— уже <b>2,8%</b>.</div>"),
        moral="Стратегию судят <em>по сотням сделок</em>, а не по десяти.",
        moral_sub="Поэтому мы публикуем все сделки подряд — чтобы видеть не удачу, а результат.",
        title="Если подумал «нет» — тебе в этот ролик #квиз #вероятность #трейдинг",
        description=("6 или больше орлов из 10 бросков честной монетки выпадают в 37,7% случаев — "
                     "так что 6 сделок из 10 в плюс ещё ничего не доказывают. А 60 и больше из "
                     "100 — уже 2,8%. Стратегию судят по сотням сделок. "
                     + _TAIL + " #квиз #трейдинг #крипта #вероятность"),
    ),
)


def _check() -> None:
    """Арифметика роликов — до рендера."""
    assert round(1000 * (1 - 0.9)) == 100 and round(100 * (1 + 9.0)) == 1000   # +900% = ×10
    assert round(100 * 0.1 * 1.9) == 19                                        # 100 → 10 → 19
    assert 7 * 1 - 3 * 3 == -2                                                 # винрейт 70%
    assert round(p_streak(100, 5, 0.4) * 100) == 46                            # 5 стопов из 100
    assert round(p_streak(100, 7, 0.5), 2) == 0.32                             # ≈1 из 3
    assert (round(1.01 ** 100, 1), round(1.01 ** 200, 1), round(1.01 ** 365, 1)) == (2.7, 7.3, 37.8)
    assert round(200 / 3, 1) == 66.7                                           # усреднение
    assert 10 * 2 ** 7 == 1280 and sum(10 * 2 ** i for i in range(7)) == 1270  # мартингейл
    assert round((1 - 0.99 ** 100) * 100) == 63 and round(0.99 ** 100, 2) == 0.37  # комиссии
    assert 1 * 3 - 3 * 1 == 0                                                  # винрейт 25%
    assert round(1000 * 0.9 ** 10) == 349 and round(1000 * 0.99 ** 10) == 904  # риск 10% / 1%
    # партия 30.10 — каждое число двумя способами
    assert F(100) * 10 * F(10, 100) == 100 and 10 * F(10, 100) == 1           # 21: −10% × ×10
    assert 3 * 30 * F(1, 10000) * 10 == F(9, 100) == F(3, 10000) * 30 * 10    # 22: 90 платежей
    assert F(1) + F(2, 10) * 2 == F(14, 10) and F(4, 10) / 4 == F(1, 10)      # 23: +40% / +10%
    assert [f"{1.1 ** i:.2f}" for i in (7, 8)] == ["1.95", "2.14"]              # 24: удвоение
    assert 1.1 ** 7 < 2 < 1.1 ** 8 and math.ceil(math.log(2) / math.log(1.1)) == 8
    assert 0.9 ** 7 < 0.5                                                      # 24: 7 стопов по 10%
    assert F(3, 5) ** 5 == F(243, 3125) and round(0.6 ** 5 * 100, 1) == 7.8   # 25: 0,6⁵
    assert round(F(3125, 243)) == 13 and round(0.6 ** 3 * 100) == 22           # 25: 1 из 13
    assert F(1000) * F(1, 100) / F(2, 100) == 500 and 500 * F(2, 100) == 10   # 26: позиция
    assert F(4, 10) * 200 - F(6, 10) * 100 == 20 == F(4 * 200 - 6 * 100, 10)  # 27: ожидание
    assert 8 * F(2, 100) * 1000 == 160 and 8 * F(2, 100) == F(16, 100)       # 28: 8 стопов разом
    assert F(800) * F(6, 5) == 960 and F(1000, 800) - 1 == F(1, 4)            # 29: −20% → +25%
    p10 = F(sum(math.comb(10, k) for k in range(6, 11)), 2 ** 10)             # 30: хвост и
    assert p10 == 1 - F(sum(math.comb(10, k) for k in range(6)), 2 ** 10)    # дополнение
    assert round(float(p10) * 100, 1) == 37.7 and round(float(p10) * 100) == 38
    p100 = F(sum(math.comb(100, k) for k in range(60, 101)), 2 ** 100)
    assert p100 == 1 - F(sum(math.comb(100, k) for k in range(60)), 2 ** 100)
    assert round(float(p100) * 100, 1) == 2.8
    for q in QUIZZES:
        assert len(q.title) <= 100, q.key
        for leak in ("6226", "LTF", "1:1", "SMC", "демо", "виртуальн"):        # граница публикации
            assert leak not in (q.title + q.description).lower(), (q.key, leak)


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


def caption(q: Quiz) -> kit.Caption:
    """Подпись в формате kit.Caption: хэштеги из хвоста описания — отдельной строкой."""
    m = re.search(r"\s((?:#\S+\s*)+)$", q.description)
    desc, tags = (q.description[:m.start()], m.group(1).strip()) if m else (q.description, "")
    hook = _html.unescape(re.sub(r"<[^>]+>", "", q.question))
    return kit.Caption(title=q.title, hook=hook, description=desc, hashtags=tags)


def main() -> None:
    """Без аргументов — все квизы; иначе только с ключами на эти префиксы (14 15 …)."""
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("prefixes", nargs="*", help="префиксы ключей (21 22 …); без них — все")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--preview", action="store_true", help="только PNG ключевых кадров")
    a = ap.parse_args()
    _check()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for i, q in enumerate(QUIZZES):
        if a.prefixes and not any(q.key.startswith(p) for p in a.prefixes):
            continue
        frames, cues = build(q)
        cap = caption(q)
        kit.check_public([cap.render()])                    # страж — до рендера
        kit.check_public(kit.visible_text(h) for h, _d in frames)
        if a.preview:                                       # вопрос, ответ, доказательство, вывод
            picks = [0, 16, len(frames) - 2, len(frames) - 1]
            for p in kit.snapshot([frames[k][0] for k in picks], out / "preview", q.key):
                print(p)
            continue
        mp4 = replay.frames_to_mp4(frames, seed=20 + i, cues=cues)
        (out / f"{q.key}.mp4").write_bytes(mp4)
        (out / f"{q.key}.txt").write_text(cap.render(), encoding="utf-8")
        print(f"{q.key}.mp4 — {len(mp4) // 1024} КБ, {sum(d for _h, d in frames):.1f} с",
              flush=True)


if __name__ == "__main__":
    main()
