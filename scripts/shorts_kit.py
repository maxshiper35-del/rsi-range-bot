"""Общий каркас новых форматов 9:16 (Shorts/TikTok/Reels): безопасная зона, бренд, числа,
таймлайн кадров, финальная карточка с CTA, стражи публикации и превью ключевых кадров.

Уроки квизов (scripts/make_quiz.py): контент заполняет кадр, текст крупный, ничего важного
под кнопками справа и подписью снизу. Безопасная зона: x 64…924, y 200…1500 — фон и
декор могут выходить за неё, цифры и слова — нет.

Рендер — тот же движок, что у повтора сделки: replay.frames_to_mp4 (Playwright-кадры →
ffmpeg, своя музыка soundtrack.py, громкость ~−16 LUFS). Синхронно — только из скрипта.
"""
from __future__ import annotations

import html as _html
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

W, H = 1080, 1920
SAFE_L, SAFE_T, SAFE_W, SAFE_H = 64, 200, 860, 1300
G, R, A, IND, MUTED = "#4ADE9C", "#F16565", "#F2B84B", "#9BA8FA", "#8A909C"
BOT = "@rsirange_bot"
NOT_ADVICE = "Не финансовый совет"
FPS = 30
F1 = 1 / FPS                      # один кадр видео: короче — ffmpeg склеит/выбросит

# граница публикации: таксономия сетапов и скоринги — никогда наружу (правило владельца)
_FORBIDDEN = (
    re.compile(r"(?<![\d.,])6226(?![\d.,])"),
    re.compile(r"\bLTF\b", re.I),
    re.compile(r"(?<![\d:])1:1(?![\d:])"),
    re.compile(r"SMC", re.I),
    re.compile(r"sub-?score|суб-?скор|скоринг", re.I),
    re.compile(r"\bиксы\b|\bиксов\b", re.I),
    # владелец 30.10: в роликах без «демо»/«демо-счёт» и «виртуальных денег» (честная
    # оговорка про расчётные цифры — «расчёт по журналу»)
    re.compile(r"\bдемо(?![нг])", re.I),
    re.compile(r"виртуальн\w*\s+(?:деньг|доллар|сч[её]т)", re.I),
)

BASE_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:1080px;height:1920px;overflow:hidden;color:#EDEFF3;
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;background:#0A0C10}
.bg{position:absolute;inset:0;
  background:radial-gradient(1000px 760px at 50% 22%,var(--glow,rgba(124,92,252,.16)),
  transparent 66%),
  linear-gradient(180deg,#0E1016,#0A0C10 60%,#08090C)}
.safe{position:absolute;left:64px;top:200px;width:860px;height:1300px;
  display:flex;flex-direction:column}
.brandrow{display:flex;justify-content:space-between;align-items:center;height:64px}
.brand{display:flex;gap:14px;align-items:center;font-size:30px;font-weight:800;
  letter-spacing:-.3px;color:#D6D9E0}
.mark{width:56px;height:56px;border-radius:16px;
  background:linear-gradient(155deg,#4C6EF5,#7C5CFC);display:flex;align-items:center;
  justify-content:center;font-weight:900;font-size:28px;color:#fff}
.tag{font-size:26px;font-weight:800;letter-spacing:.08em;padding:10px 18px;border-radius:14px;
  text-transform:uppercase;background:rgba(242,184,75,.12);color:#F2B84B;
  border:2px solid rgba(242,184,75,.45)}
.kick{font-size:38px;font-weight:850;letter-spacing:.12em;color:#F2B84B;text-transform:uppercase}
.num{font-variant-numeric:tabular-nums}
.pos{color:#4ADE9C}.neg{color:#F16565}.amb{color:#F2B84B}.mut{color:#8A909C}
.fine{font-size:28px;line-height:1.35;color:#7B8190;font-weight:600}
.end{position:absolute;inset:0;display:flex;flex-direction:column;justify-content:center}
.end-h{font-size:96px;font-weight:900;letter-spacing:-3px;line-height:1.04}
.end-h em{font-style:normal;color:#F2B84B}
.end-s{font-size:50px;font-weight:650;color:#C7CBD4;line-height:1.28;margin-top:34px}
.cta-l{margin-top:60px;font-size:46px;font-weight:800;color:#EDEFF3}
.cta{margin-top:18px;align-self:flex-start;font-size:72px;font-weight:900;padding:28px 46px;
  border-radius:28px;background:linear-gradient(155deg,#4C6EF5,#7C5CFC);color:#fff;
  letter-spacing:-1px;box-shadow:0 18px 60px rgba(92,110,252,.35);white-space:nowrap}
.nfa{margin-top:40px;font-size:40px;font-weight:800;color:#EDEFF3}
.endfine{margin-top:22px}
"""


def esc(v: object) -> str:
    return _html.escape(str(v), quote=True)


def _group(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def money(v: float, sign: bool = False, digits: int = 0) -> str:
    """10194.4 -> «10 194 $»; sign=True -> «+363 $» / «−98 $» (типографский минус)."""
    neg = v < 0
    a = abs(v)
    whole = int(round(a)) if digits == 0 else int(a)
    s = _group(whole)
    if digits:
        frac = f"{a:.{digits}f}".split(".")[1]
        s = f"{s},{frac}"
    pre = "−" if neg else ("+" if sign and v > 0 else "")
    return f"{pre}{s} $"


def num(v: float, digits: int = 0, sign: bool = False) -> str:
    """Число по-русски: группы через неразрывный пробел, десятичная запятая, «−»."""
    neg = v < 0
    a = abs(v)
    if digits:
        whole, frac = f"{a:.{digits}f}".split(".")
        s = f"{_group(int(whole))},{frac}"
    else:
        s = _group(int(round(a)))
    pre = "−" if neg else ("+" if sign and v > 0 else "")
    return pre + s


def pct(v: float, digits: int = 1, sign: bool = True) -> str:
    return num(v, digits, sign) + "%"


def rr(v: float, digits: int = 2) -> str:
    """R-мультипликатор: +2,40R / −1R (целые без хвоста)."""
    if abs(v - round(v)) < 1e-9:
        return num(v, 0, sign=True) + "R"
    return num(v, digits, sign=True) + "R"


def price(v: float) -> str:
    """Цена с разумной точностью: 78 192 / 101 / 0,2068 / 0,0000280."""
    a = abs(v)
    if a >= 1000:
        return num(v, 0)
    if a >= 100:
        d = 2 if abs(v - round(v)) > 1e-9 else 0
        return num(v, d)
    if a >= 1:
        s = f"{v:.4f}".rstrip("0").rstrip(".")
        return s.replace(".", ",")
    s = f"{v:.8f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def plural(n: int, one: str, few: str, many: str) -> str:
    """«1 стоп», «3 стопа», «6 стопов» (с числом)."""
    m10, m100 = n % 10, n % 100
    word = (one if m10 == 1 and m100 != 11
            else few if 2 <= m10 <= 4 and not 12 <= m100 <= 14 else many)
    return f"{n} {word}"


def ease_out(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return 1 - (1 - x) ** 3


def page(body: str, css: str = "", glow: str | None = None) -> str:
    """Кадр 1080×1920: фон на весь экран, содержимое — в безопасной зоне."""
    g = f" style='--glow:{glow}'" if glow else ""
    return (f"<!doctype html><meta charset='utf-8'><style>{BASE_CSS}{css}</style>"
            f"<div class='bg'{g}></div><div class='safe'>{body}</div>")


def brand_row(tag: str = "") -> str:
    t = f"<div class='tag'>{esc(tag)}</div>" if tag else ""
    return (f"<div class='brandrow'><div class='brand'><div class='mark'>R</div>"
            f"RSI Range</div>{t}</div>")


def end_card(headline: str, sub: str, cta_label: str, fine: str) -> str:
    """Финал: заголовок (HTML), подзаголовок, призыв + кнопка с ником бота, «не финсовет»."""
    return (f"<div class='end'><div class='end-h'>{headline}</div>"
            f"<div class='end-s'>{sub}</div><div class='cta-l'>{cta_label}</div>"
            f"<div class='cta'>{BOT}</div><div class='nfa'>{NOT_ADVICE}</div>"
            f"<div class='fine endfine'>{fine}</div></div>")


@dataclass
class Timeline:
    """Кадры [(HTML, длительность с)] и звуковые акценты [(время с, вид)]."""
    frames: list[tuple[str, float]] = field(default_factory=list)
    cues: list[tuple[float, str]] = field(default_factory=list)
    t: float = 0.0

    def add(self, html: str, dur: float, cue: str | None = None) -> None:
        dur = max(F1, round(dur * FPS) / FPS)           # длительность кратна кадру 30 fps
        if cue:
            self.cues.append((self.t, cue))
        self.frames.append((html, dur))
        self.t += dur


def visible_text(html: str) -> str:
    """Текст, который увидит зритель: без <style>, тегов и атрибутов."""
    t = re.sub(r"<style>.*?</style>", " ", html, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return _html.unescape(t)


def check_public(texts: Iterable[str], extra: Iterable[str] = ()) -> None:
    """Страж публикации: таксономия, «иксы» и (для проверки канала) имя источника —
    ни в кадрах, ни в подписи. Падает ДО рендера."""
    extra = [e.lower() for e in extra if e]
    for t in texts:
        for rx in _FORBIDDEN:
            m = rx.search(t)
            if m:
                raise AssertionError(f"граница публикации: «{m.group(0)}» в «{t[:80]}…»")
        low = t.lower()
        for e in extra:
            if e in low:
                raise AssertionError(f"анонимизация: «{e}» в «{t[:80]}…»")


@dataclass(frozen=True)
class Caption:
    title: str               # заголовок Shorts (≤100 символов)
    hook: str                # что на первом кадре
    description: str
    hashtags: str

    def render(self) -> str:
        return (f"Заголовок: {self.title}\n"
                f"Хук (первый кадр): {self.hook}\n\n"
                f"Описание:\n{self.description}\n\n"
                f"Хэштеги: {self.hashtags}\n")


def render(tl: Timeline, out_dir: Path, key: str, seed: int, caption: Caption,
           extra_forbidden: Iterable[str] = ()) -> Path:
    """Стражи → MP4 + .txt в out_dir. Возвращает путь к MP4."""
    from app.delivery import replay

    extra = list(extra_forbidden)
    assert len(caption.title) <= 100, caption.title
    check_public([caption.render()], extra)
    check_public((visible_text(h) for h, _d in tl.frames), extra)
    mp4 = replay.frames_to_mp4(tl.frames, seed=seed, cues=tl.cues)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{key}.mp4"
    path.write_bytes(mp4)
    (out_dir / f"{key}.txt").write_text(caption.render(), encoding="utf-8")
    return path


def snapshot(frames: list[str], out_dir: Path, prefix: str) -> list[Path]:
    """Превью: PNG выбранных кадров (одним браузером) — проверить вёрстку до рендера."""
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            pg = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
            for i, html in enumerate(frames):
                pg.set_content(html)
                path = out_dir / f"{prefix}_{i:02d}.png"
                pg.screenshot(path=str(path))
                paths.append(path)
        finally:
            browser.close()
    return paths
