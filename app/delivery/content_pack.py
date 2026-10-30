"""
content_pack.py — ежедневный контент-пак для соцсетей (карусель, сторис, подписи).

Пак за прошедший UTC-день: видео «Итог дня» (weekly_video, scope="day"), повторы закрытых
сделок (replay), карусель 1080×1350 для Instagram/TikTok-фото, сторис 1080×1920 и готовые
подписи. Те же правила, что у остального контента: все закрытые сделки без отбора, R до
комиссий, без уровней и без названий сетапов/скорингов (таксономия не публикуется).

Чистые функции (build_*_html, captions) — без браузера; render_* — только из потока
(asyncio.to_thread): Playwright sync API.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.delivery.graphics import _esc
from app.delivery.replay import _CSS, _r_text
from app.delivery.weekly_video import _WEEK_CSS, _tiles, equity_svg, n_trades, week_stats

CW, CH = 1080, 1350          # карусель (Instagram 4:5)
SW, SH = 1080, 1920          # сторис
ROWS_PER_SLIDE = 8
MAX_LIST_SLIDES = 3
MAX_REPLAYS = 6
TAGS = "#трейдинг #крипта #сигналы"

_SLIDE_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;color:#EDEFF3;overflow:hidden;
  background:radial-gradient(900px 620px at 50% -6%,rgba(90,110,150,.20),transparent 62%),
  linear-gradient(180deg,#0E1016,#0B0D11 55%,#090B0F)}
.sl{position:relative;width:1080px;height:1350px;padding:64px 60px 60px}
.top{display:flex;justify-content:space-between;align-items:center}
.brand{display:flex;gap:16px;align-items:center}
.mark{width:60px;height:60px;border-radius:17px;background:linear-gradient(155deg,#4C6EF5,#7C5CFC);
  display:flex;align-items:center;justify-content:center;font-weight:800;font-size:28px}
.name{font-size:28px;font-weight:800}.handle{font-size:21px;color:#7B8190;margin-top:2px}
.eyebrow{font-size:20px;font-weight:700;letter-spacing:.12em;color:#7B8190;
  text-transform:uppercase;text-align:right}
.lbl{font-size:22px;font-weight:700;letter-spacing:.14em;color:#7B8190;text-transform:uppercase;
  margin-top:56px}
.hv{font-size:180px;font-weight:800;letter-spacing:-5px;line-height:1.02;
  font-variant-numeric:tabular-nums}
.hs{font-size:30px;color:#8A909C;margin-top:6px}
.eq{margin-top:34px;border-radius:28px;padding:20px;
  background:linear-gradient(180deg,rgba(255,255,255,.04),rgba(255,255,255,.012));
  border:1px solid rgba(255,255,255,.08)}
.h1{font-size:64px;font-weight:800;letter-spacing:-1.5px;line-height:1.1;margin-top:56px;
  text-wrap:balance}
.rows{margin-top:36px;display:flex;flex-direction:column;gap:14px}
.row{display:grid;grid-template-columns:70px 1fr 130px 160px 200px;align-items:center;
  border-radius:20px;padding:22px 26px;background:rgba(255,255,255,.035);
  border:1px solid rgba(255,255,255,.07);font-size:34px}
.n{color:#5F6571;font-weight:700;font-size:26px}.c{font-weight:800}
.t{color:#7B8190;font-size:26px;font-variant-numeric:tabular-nums}
.rv{text-align:right;font-weight:800;font-variant-numeric:tabular-nums}
.side{font-size:24px;font-weight:800;letter-spacing:.04em;padding:6px 14px;border-radius:12px;
  justify-self:start}
.side.long{background:rgba(74,222,156,.14);color:#6FE3AC}
.side.short{background:rgba(241,101,101,.13);color:#F58787}
.pts{margin-top:40px;display:flex;flex-direction:column;gap:22px}
.pt{border-radius:24px;padding:28px 32px;background:rgba(255,255,255,.035);
  border:1px solid rgba(255,255,255,.07);font-size:34px;line-height:1.35;color:#C7CBD4}
.pt b{color:#EDEFF3}
.cta{display:inline-block;margin-top:56px;font-size:44px;font-weight:800;padding:28px 48px;
  border-radius:26px;background:linear-gradient(155deg,#4C6EF5,#7C5CFC)}
.foot{position:absolute;left:60px;right:60px;bottom:52px;display:flex;
  justify-content:space-between;font-size:22px;color:#5F6571}
.pos{color:#4ADE9C}.neg{color:#F16565}.neu{color:#C7CBD4}
"""


def day_period(day: date) -> tuple[datetime, datetime]:
    """[00:00, 24:00) UTC дня — тот же отбор, что у видео-итога (trades_for_period)."""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _r(v: float) -> str:
    return _r_text(v)[0].replace(".00R", "R")


def _head(channel: str, eyebrow: str) -> str:
    return (f"<div class='top'><div class='brand'><div class='mark'>R</div><div>"
            f"<div class='name'>RSI Range</div><div class='handle'>{_esc(channel)}</div>"
            f"</div></div><div class='eyebrow'>{eyebrow}</div></div>")


def _slide(inner: str, page: int, pages: int) -> str:
    return (f"<style>{_SLIDE_CSS}</style><div class='sl'>{inner}"
            f"<div class='foot'><div>R — в долях риска, до комиссий · не финансовый совет</div>"
            f"<div>{page}/{pages}</div></div></div>")


def build_carousel_html(period: str, trades: list[dict],
                        channel: str = "@rsi_range") -> list[str]:
    """Слайды: обложка с итогом и кривой → все сделки дня списком → как читать R → призыв."""
    st = week_stats(trades)
    val, cls = _r_text(st["total"])
    chunks = [trades[i:i + ROWS_PER_SLIDE] for i in range(0, len(trades), ROWS_PER_SLIDE)]
    hidden = sum(len(c) for c in chunks[MAX_LIST_SLIDES:])
    chunks = chunks[:MAX_LIST_SLIDES]
    pages = 1 + len(chunks) + 2
    eyebrow = f"Итог дня<br>{_esc(period)}"
    out = [_slide(
        _head(channel, eyebrow)
        + f"<div class='lbl'>Результат дня</div><div class='hv {cls}'>"
          f"{val.replace('.00R', 'R')}</div>"
        + f"<div class='hs'>{n_trades(st['n'])} · {st['wins']} в плюс / {st['losses']} в минус"
          f"</div><div class='eq'>{equity_svg(st['cum'], st['n'])}</div>"
        + "<div class='hs' style='margin-top:30px'>Все закрытые сделки, без отбора. "
          "Листайте →</div>", 1, pages)]
    num = 0
    for j, chunk in enumerate(chunks):
        rows = []
        for t in chunk:
            num += 1
            r = float(t["result_r"])
            side = t["side"]
            rows.append(
                f"<div class='row'><div class='n'>{num}</div>"
                f"<div class='c'>{_esc(t['symbol'].split('/')[0])}</div>"
                f"<div class='side {side.lower()}'>{_esc(side)}</div>"
                f"<div class='t'>{_esc(str(t['closed_at'])[11:16])} UTC</div>"
                f"<div class='rv {_r_text(r)[1]}'>{_r(r)}</div></div>")
        more = (f"<div class='hs' style='margin-top:20px'>+ ещё {hidden} — в канале</div>"
                if hidden and j == len(chunks) - 1 else "")
        title = "Все сделки дня" if j == 0 else "Сделки дня — продолжение"
        out.append(_slide(_head(channel, eyebrow) + f"<div class='h1'>{title}</div>"
                          f"<div class='rows'>{''.join(rows)}</div>{more}", 2 + j, pages))
    out.append(_slide(
        _head(channel, "Как читать<br>результат")
        + "<div class='h1'>Что такое R</div><div class='pts'>"
          "<div class='pt'><b>1R</b> — сумма, которую готовы потерять на сделке: "
          "расстояние от входа до стопа.</div>"
          "<div class='pt'><b class='neg'>−1R</b> — сработал стоп. Убыток ровно такой, "
          "как и планировали.</div>"
          "<div class='pt'><b class='pos'>+2R</b> — прибыль в два риска. "
          "Риск 10 $ → +20 $.</div>"
          "<div class='pt'>Считаем все сделки подряд, с минусами — "
          "иначе статистика врёт.</div></div>", pages - 1, pages))
    out.append(_slide(
        _head(channel, "Сигналы<br>в реальном времени")
        + "<div class='h1'>Каждая сделка — с входом, стопом и выходом. "
          "Плюсы и минусы публикуем одинаково.</div>"
        + f"<div class='cta'>Telegram {_esc(channel)}</div>"
        + "<div class='hs' style='margin-top:30px'>Ссылка — в профиле</div>",
        pages, pages))
    return out


def build_story_html(period: str, trades: list[dict], channel: str = "@rsi_range") -> str:
    """Сторис 1080×1920: итог дня и плитки сделок. Макет в безопасной зоне (сверху — шапка
    сторис, снизу — поле ответа), фон на весь экран."""
    st = week_stats(trades)
    val, cls = _r_text(st["total"])
    return (
        f"<style>{_CSS}{_WEEK_CSS}"
        ".page{transform:translate(60px,230px) scale(.89)}"
        ".scta{margin-top:44px;font-size:40px;font-weight:800;padding:26px 44px;"
        "border-radius:24px;background:linear-gradient(155deg,#4C6EF5,#7C5CFC);"
        "display:inline-block}</style><div class='page'>"
        + _head(channel, f"Итог дня<br>{_esc(period)}")
        + f"<div class='hero'><div class='hl'>Результат дня</div>"
          f"<div class='hv {cls}'>{val.replace('.00R', 'R')}</div>"
          f"<div class='hs'>{n_trades(st['n'])} · {st['wins']} в плюс / {st['losses']} "
          f"в минус</div></div><div class='eq'>{equity_svg(st['cum'], st['n'])}</div>"
          f"{_tiles(trades, st['n'])}"
        + f"<div class='scta'>Все сделки — в {_esc(channel)}</div>"
        + "<div class='hs' style='margin-top:22px'>все закрытые сделки, без отбора · "
          "R до комиссий · не финансовый совет</div></div>")


def captions(period: str, trades: list[dict], channel: str = "@rsi_range") -> dict[str, str]:
    """Подписи к видео-итогу и карусели — в формате, согласованном для публикаций."""
    st = week_stats(trades)
    base = (f"Итог дня {period}: {n_trades(st['n'])}, {st['wins']} в плюс / {st['losses']} "
            f"в минус, {_r(st['total'])} до комиссий. Все сделки, без отбора. "
            f"Сигналы — в Telegram: {channel}. Не финансовый совет. {TAGS}")
    carousel = (f"Итог дня {period}: {_r(st['total'])} до комиссий ({n_trades(st['n'])}). "
                "Листайте: все сделки дня и как читать R. "
                f"Сигналы — в Telegram: {channel}. Не финансовый совет. {TAGS}")
    return {"video": base, "carousel": carousel, "story": f"Итог дня: {_r(st['total'])}"}


def replay_caption(row: dict, channel: str = "@rsi_range") -> str:
    r = float(row["result_r"])
    how = "выход по трейлингу" if r > 0 else "выход по стопу"
    return (f"Повтор сделки: {row['symbol'].split('/')[0]} {row['side']}, "
            f"{_r(r)} до комиссий, {how}. Публикуем все сделки — плюсы и минусы. "
            f"Сигналы — в Telegram: {channel}. Не финансовый совет. {TAGS}")


def render_pngs(htmls: list[str], width: int, height: int) -> list[bytes]:
    """Один браузер на все слайды (каждый вызов Chromium — ~300МБ и секунды запуска)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            out = []
            for html in htmls:
                page.set_content(html)
                page.wait_for_timeout(60)
                out.append(page.screenshot())
            return out
        finally:
            browser.close()


def render_carousel(period: str, trades: list[dict], channel: str = "@rsi_range") -> list[bytes]:
    return render_pngs(build_carousel_html(period, trades, channel), CW, CH)


def render_story(period: str, trades: list[dict], channel: str = "@rsi_range") -> bytes:
    return render_pngs([build_story_html(period, trades, channel)], SW, SH)[0]
