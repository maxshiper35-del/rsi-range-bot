"""Замер №18, шаг 0: держится ли прибыльность счетов Hyperliquid
(docs/measurements/2026-11-10-hl-persistence.md). Описательный: без торговли и сигналов.

  DATA_ROOT="/Users/mac/bot ctypto/data" .venv/bin/python -m scripts.measure_hl_persistence \
      [--jobs-out jobs.txt --roles-out addrs.txt] [--counts-only]

Читает кэш scripts/hl_fetch.py. Отбор «стабильно прибыльных» — только по ряду PnL до даты T,
результат — после T. Слой B (сделки до T и роль счёта) считается, если его данные собраны;
--jobs-out/--roles-out пишут задания для hl_fetch на счета S. --counts-only печатает только
размеры групп — для проверки хода сбора без взгляда на результат.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

DAY = 86_400_000
FORM_D, EVAL_D, MIN_AGE_D = 120, 60, 90
DATES = (("учебный", "2026-05-14"), ("проверочный", "2026-07-13"), ("отложенный", "2026-09-11"))
LIVE = ("лента", "2026-11-10")                 # пункт 9: отбор на сегодня, оценки нет
MIN_EQUITY, MIN_ACTIVE = 100_000.0, 6
RULES = {"S": (0.10, 2 / 3, 0.50), "S-": (0.05, 0.60, 0.60), "S+": (0.20, 0.75, 0.40)}
MAX_FILLS, MIN_EDGE_BPS = 20_000, 10.0
WATCH = {"BTC", "ETH", "SOL", "XRP", "ZEC", "HYPE", "NEAR", "kPEPE", "DOGE", "ENA", "SUI", "ADA"}


def root() -> Path:
    return Path(os.environ.get("DATA_ROOT", "data")) / "cache_research" / "hl"


def ms(day: str) -> int:
    return int(pd.Timestamp(day, tz="UTC").value // 1_000_000)


def load_series(lo: int = 0, hi: int = 40) -> dict:
    """{адрес: (время мс, накопленный PnL, капитал перп-счёта)} из portfolio.jsonl. Берутся
    счета, чей остаток по адресу (как в hl_fetch.in_share) лежит в [lo, hi): 0–40 —
    зарегистрированная выборка, 40–100 — остальные счета для повторения."""
    out = {}
    with open(root() / "portfolio.jsonl", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not lo <= int(r["a"][-6:], 16) % 100 < hi:
                continue
            a = r["all"]
            if len(a["pnl"]) < 2 or len(a["pnl"]) != len(a["av"]):
                continue
            out[r["a"]] = (np.array([t for t, _ in a["pnl"]], np.int64),
                           np.array([v for _, v in a["pnl"]], float),
                           np.array([v for _, v in a["av"]], float))
    return out


def features(ts, pnl, av, t_ms: int, with_eval: bool = True) -> dict | None:
    """Признаки счёта на дату T: формирование — только точки не позже T."""
    ie = int(np.searchsorted(ts, t_ms, "right")) - 1
    if ie < 1:
        return None
    i0 = int(np.searchsorted(ts, t_ms - FORM_D * DAY, "right")) - 1
    if i0 < 0:
        if ts[0] > t_ms - MIN_AGE_D * DAY:
            return None                                # слишком молод на эту дату
        i0 = 0
    if i0 >= ie:
        return None
    d = np.diff(pnl[i0:ie + 1])
    nz = d[d != 0]
    eq_t = float(av[ie])
    mean_eq = float(av[i0:ie + 1].mean())
    pnl_f = float(pnl[ie] - pnl[i0])
    pos = nz[nz > 0]
    out = dict(eq_t=eq_t, active=int(len(nz)), pnl_f=pnl_f,
               roi_f=pnl_f / mean_eq if mean_eq > 0 else np.nan,
               pos_share=float((nz > 0).mean()) if len(nz) else 0.0,
               conc=float(pos.max() / pos.sum()) if len(pos) else 1.0)
    out["universe"] = eq_t >= MIN_EQUITY and len(nz) >= MIN_ACTIVE
    for name, (roi, share, conc) in RULES.items():
        out[name] = bool(out["universe"] and pnl_f > 0 and out["roi_f"] >= roi
                         and out["pos_share"] >= share and out["conc"] <= conc)
    if with_eval:
        js = int(np.searchsorted(ts, t_ms, "left"))
        je = int(np.searchsorted(ts, t_ms + EVAL_D * DAY, "right")) - 1
        pnl_e = float(pnl[je] - pnl[js]) if js < len(ts) and je > js else 0.0
        out["roi_e"] = float(np.clip(pnl_e / eq_t, -1.0, 1.0)) if eq_t > 0 else np.nan
    return out


def table(series: dict) -> pd.DataFrame:
    rows = []
    for name, day in DATES + (LIVE,):
        t_ms = ms(day)
        for addr, (ts, pnl, av) in series.items():
            f = features(ts, pnl, av, t_ms, with_eval=name != LIVE[0])
            if f is not None and f["universe"]:
                rows.append(dict(part=name, addr=addr, t_ms=t_ms, **f))
    return pd.DataFrame(rows)


def fills_stats(addr: str, start: int) -> dict | None:
    """Слой B по сделкам окна формирования: число, оборот, прибыль (closedPnl − комиссии)."""
    path = root() / "fills" / f"{addr}_{start}.jsonl"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        head = json.loads(f.readline())
        rows = [json.loads(x) for x in f]
    vol = sum(float(x["px"]) * float(x["sz"]) for x in rows)
    profit = sum(float(x["closedPnl"]) - float(x["fee"]) for x in rows)
    return dict(n_fills=len(rows), truncated=bool(head["truncated"]), vol=vol, profit=profit,
                edge_bps=profit / vol * 1e4 if vol > 0 else 0.0, rows=rows)


def layer_b(df: pd.DataFrame) -> pd.Series:
    """True — счёт S прошёл фильтр B; NaN — данных слоя B по нему нет."""
    roles = {}
    rp = root() / "roles.jsonl"
    if rp.exists():
        for line in open(rp, encoding="utf-8"):
            r = json.loads(line)
            roles[r["a"]] = r["role"]
    out = pd.Series(np.nan, index=df.index, dtype=object)
    for i, r in df[df["S"]].iterrows():
        st = fills_stats(r.addr, r.t_ms - FORM_D * DAY)
        if st is None or r.addr not in roles:
            continue
        out[i] = bool(not st["truncated"] and st["n_fills"] <= MAX_FILLS
                      and st["edge_bps"] >= MIN_EDGE_BPS and roles[r.addr] == "user")
    return out


def boot_ci(x: np.ndarray, reps: int = 2000, seed: int = 0) -> tuple:
    rng = np.random.default_rng(seed)
    m = x[rng.integers(0, len(x), size=(reps, len(x)))].mean(1)
    return float(np.percentile(m, 5)), float(np.percentile(m, 95))


def describe(x: pd.Series) -> str:
    v = x.to_numpy(float)
    if not len(v):
        return "n=0"
    lo, hi = boot_ci(v)
    return (f"n={len(v):5d} | в плюсе {np.mean(v > 0):5.1%} | медиана {np.median(v):+7.2%} | "
            f"среднее {v.mean():+7.2%} [ДИ {lo:+.2%}; {hi:+.2%}] | "
            f"≤ −50%: {np.mean(v <= -0.5):.1%}")


def live_feed(df: pd.DataFrame) -> None:
    """Пункт 9: события открытия позиций по монетам вотчлиста за последние 30 дней."""
    live = df[(df["part"] == LIVE[0]) & (df["SB"] == True)]         # noqa: E712
    t_ms = ms(LIVE[1])
    ev = []
    for r in live.itertuples():
        st = fills_stats(r.addr, t_ms - FORM_D * DAY)
        seen = set()
        for x in st["rows"]:
            if (x["time"] >= t_ms - 30 * DAY and str(x["dir"]).startswith("Open")
                    and (x["oid"], x["coin"]) not in seen):
                seen.add((x["oid"], x["coin"]))
                ev.append((r.addr, x["coin"], x["time"] // DAY, x["dir"]))
    e = pd.DataFrame(ev, columns=["addr", "coin", "day", "dir"])
    w = e[e["coin"].isin(WATCH)]
    print(f"\n== Пункт 9: лента. Счетов S∩B на {LIVE[1]}: {len(live)}; торгуют монеты вотчлиста "
          f"за 30 дней: {w['addr'].nunique()}")
    if len(w):
        per_day = w.groupby("day").size().reindex(range(t_ms // DAY - 30, t_ms // DAY),
                                                  fill_value=0)
        print(f"  событий за 30 дней: {len(w)} (все монеты: {len(e)}); в день: медиана "
              f"{per_day.median():.0f}, мин {per_day.min()}, макс {per_day.max()}; дней без "
              f"событий: {int((per_day == 0).sum())}")
        print("  по монетам: " + ", ".join(f"{c} {n}" for c, n in
                                          w["coin"].value_counts().items()))
        print("  по счетам: событий на счёт — медиана "
              f"{w.groupby('addr').size().median():.0f}, макс {w.groupby('addr').size().max()}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs-out", default=None)
    ap.add_argument("--roles-out", default=None)
    ap.add_argument("--counts-only", action="store_true")
    ap.add_argument("--lo", type=int, default=0)
    ap.add_argument("--hi", type=int, default=40)
    a = ap.parse_args()
    series = load_series(a.lo, a.hi)
    print(f"выборка по адресу: остаток в [{a.lo}, {a.hi})")
    df = table(series)
    print(f"счетов с рядом: {len(series)}; строк вселенной по датам: "
          + ", ".join(f"{p} {n}" for p, n in df["part"].value_counts().items()))
    for name in RULES:
        print(f"  {name}: " + ", ".join(
            f"{p} {int(df[(df['part'] == p) & df[name]].shape[0])}" for p, _ in DATES + (LIVE,)))
    sel = df[df["S"]]
    if a.jobs_out:
        Path(a.jobs_out).write_text("".join(
            f"{r.addr} {r.t_ms - FORM_D * DAY} {r.t_ms}\n" for r in sel.itertuples()))
        print(f"задания сделок: {len(sel)} → {a.jobs_out}")
    if a.roles_out:
        addrs = sorted(set(sel["addr"]))
        Path(a.roles_out).write_text("\n".join(addrs) + "\n")
        print(f"задания ролей: {len(addrs)} → {a.roles_out}")
    df["SB"] = layer_b(df)
    have_b = df.loc[df["S"], "SB"].notna()
    print(f"слой B посчитан у {int(have_b.sum())} из {len(have_b)} строк S; прошли: "
          f"{int((df['SB'] == True).sum())}")                       # noqa: E712
    if a.counts_only:
        return

    ev = df[df["part"] != LIVE[0]]
    groups = {"U": ev["universe"], "S": ev["S"], "S∩B": ev["SB"] == True,    # noqa: E712
              "S-": ev["S-"], "S+": ev["S+"]}
    for part in [p for p, _ in DATES] + ["все три"]:
        m = ev["part"] == part if part != "все три" else pd.Series(True, index=ev.index)
        print(f"\n== {part}")
        for g, mask in groups.items():
            print(f"  {g:4s} {describe(ev.loc[m & mask, 'roi_e'])}")
        u = ev[m & ev["universe"]]
        rho = u[["roi_f", "roi_e"]].rank().corr().iloc[0, 1]
        print(f"  ранговая корреляция ROI_F и ROI_E во вселенной: {rho:+.3f}")

    b_ready = bool(have_b.all()) and len(have_b) > 0
    key = "S∩B" if b_ready else "S"
    print(f"\n== Вердикт по группе {key}" + ("" if b_ready else
                                            " (слой B не посчитан целиком — это НЕ вердикт)"))
    g = groups[key]
    means, beats = [], []
    for part, _ in DATES:
        m = ev["part"] == part
        mg, mu = ev.loc[m & g, "roi_e"].mean(), ev.loc[m & ev["universe"], "roi_e"].mean()
        means.append(mg)
        beats.append(mg > mu)
        print(f"  {part}: среднее ROI_E группы {mg:+.2%}, вселенной {mu:+.2%}")
    pooled = ev.loc[g, "roi_e"].to_numpy(float)
    lo, _hi = boot_ci(pooled) if len(pooled) else (np.nan, np.nan)
    checks = [all(x > 0 for x in means), lo > 0, float(np.mean(pooled > 0)) >= 0.55, all(beats)]
    for txt, okk in zip(("1) среднее > 0 на каждой дате", f"2) нижняя граница ДИ {lo:+.2%} > 0",
                         f"3) доля в плюсе {np.mean(pooled > 0):.1%} ≥ 55%",
                         "4) выше вселенной на каждой дате"), checks):
        print(f"  {txt}: {'да' if okk else 'НЕТ'}")
    print(f"  ИТОГ: {'ДЕРЖИТСЯ' if all(checks) else 'НЕ ДЕРЖИТСЯ'}")
    if b_ready and (df["part"] == LIVE[0]).any():
        live_feed(df)


if __name__ == "__main__":
    main()
