"""Независимая сверка №24 (docs/measurements/2026-11-12-trend-btc-eth.md, разделы 2–7).

Свой код по тексту регистрации b5ce196, написан без чтения scripts/measure_trend_btceth.py.
12.11.2026 повторил цифры замера: вся история +1857%, Шарп 1.09, тайминг +20.2% в год; S4 +1.5%.
Годится как образец для следующих сверок.

  DATA_ROOT="/Users/mac/bot ctypto/data" .venv/bin/python -m scripts.verify_trend_btceth fetch
  DATA_ROOT=… .venv/bin/python -m scripts.verify_trend_btceth run [--variant V | --all]

fetch — заново свечи Coinbase BTC-USD и ETH-USD 1d и фактический фандинг Binance за 2020
        → cache_research/trend24_verify/ (публичные данные, без ключей). BitMEX (2017–2019)
        12.11 отвечал 403 — фактического фандинга до 2020 здесь нет.
run   — система T-L, держание перпа и спота, «та же доля без тайминга» по участкам, критерий.
        День i — «открытие i → открытие i+1»: подпозиция, решённая по закрытию t, входит по
        открытию t+1. Варианты --variant (проверки чувствительности):
  base        как в регистрации;
  lvl_exec    начальный уровень трейла — середина канала дня исполнения t+1;
  noexit_t1   в день исполнения выход не проверяется;
  reentry     выход и вход по одному закрытию: позиция остаётся, уровень заново;
  fund_left   фандинг дня d — события [d; d+1) вместо (d; d+1];
  jitter      без округления времени фандинга: calc_time Binance сдвинут на 5–31 мс, событие
              полуночи уходит в соседний день — ловушка, на которой сверка сначала разошлась;
  no_fund     без фандинга (как на споте);
  real2020    справочно: фактический фандинг Binance за 2020 вместо 0.01% за 8 ч;
  cost2       двойные издержки (S4 +0.006% — пункт 1 на грани; с ×2.05 уже −0.07%);
  gross_cost  издержки по каждой подпозиции, без взаимозачёта внутри монеты;
  close_close доходность дня — закрытие i−1 → закрытие i;
  lookahead   НАМЕРЕННАЯ ошибка: подпозиция зарабатывает доходность дня сигнала.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

WINDOWS = (5, 10, 20, 30, 60, 90, 150, 250, 360)
COST = 0.00055 + 0.0002            # тейкер + проскальзывание, на сторону
F8 = 0.0001                        # фандинг до 2021 и в пропусках — 0.01% за 8 ч
SEGS = (("S1", "2017-06-01", "2020-12-31"), ("S2", "2021-01-01", "2022-12-31"),
        ("S3", "2023-01-01", "2025-03-31"), ("S4", "2025-04-01", "2026-07-31"),
        ("вся", "2017-06-01", "2026-07-31"), ("заморозка", "2026-08-01", "2026-10-30"))
VARIANTS = ("base", "lvl_exec", "noexit_t1", "reentry", "fund_left", "jitter", "no_fund",
            "real2020", "cost2", "gross_cost", "close_close", "lookahead")
UA = {"User-Agent": "research-script/1.0"}


def root() -> Path:
    return Path(os.environ.get("DATA_ROOT", "data")) / "cache_research"


def vroot() -> Path:
    p = root() / "trend24_verify"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------- сбор ----------

def get(url: str) -> bytes:
    for k in range(5):
        try:
            req = urllib.request.Request(url, headers=UA)
            return urllib.request.urlopen(req, timeout=60).read()
        except Exception as ex:                                  # noqa: BLE001
            print("повтор", k, ex, flush=True)
            time.sleep(10 * (k + 1))
    raise SystemExit("не скачалось: " + url)


def fetch_coinbase(pid: str, start: str, out: Path) -> None:
    t = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = {}
    while t < end:
        e = min(t + timedelta(days=290), end)                    # не больше 300 свечей
        url = (f"https://api.exchange.coinbase.com/products/{pid}/candles?granularity=86400"
               f"&start={t:%Y-%m-%dT%H:%M:%SZ}&end={e:%Y-%m-%dT%H:%M:%SZ}")
        for r in json.loads(get(url)):
            rows[int(r[0])] = r
        t = e
        time.sleep(1.5)
    with open(out, "w") as f:
        f.write("t,low,high,open,close,volume\n")
        for k in sorted(rows):
            f.write(",".join(str(x) for x in rows[k]) + "\n")
    print(f"{pid}: {len(rows)} дней → {out}")


def cmd_fetch(_a) -> None:
    fetch_coinbase("BTC-USD", "2015-07-01", vroot() / "BTC_cb.csv")
    fetch_coinbase("ETH-USD", "2016-05-01", vroot() / "ETH_cb.csv")
    for s in ("BTCUSDT", "ETHUSDT"):
        parts = []
        for m in range(1, 13):
            z = zipfile.ZipFile(io.BytesIO(get(
                "https://data.binance.vision/data/futures/um/monthly/fundingRate/"
                f"{s}/{s}-fundingRate-2020-{m:02d}.zip")))
            parts.append(pd.read_csv(z.open(z.namelist()[0])))
            time.sleep(1)
        pd.concat(parts).to_csv(vroot() / f"binance2020_{s}.csv", index=False)
        print(f"{s}: фандинг Binance 2020 → binance2020_{s}.csv")


# ---------- расчёт ----------

def load_prices(src: Path, coin: str) -> pd.DataFrame:
    path = src / f"{coin}_cb.csv"
    if not path.exists():
        path = src / f"{coin}_usd_1d.csv"                        # кэш замера №24
    d = pd.read_csv(path)
    d.index = pd.to_datetime(d["t"], unit="s")
    d = d.sort_index()[["open", "high", "low", "close"]].astype(float)
    full = pd.date_range(d.index[0], d.index[-1], freq="D")
    miss = full.difference(d.index)
    if len(miss):
        print(f"  {path.name}: пропущенных дней {len(miss)} "
              f"({', '.join(str(x.date()) for x in miss[:5])}) — день без движения")
    return d.reindex(full).ffill()


def load_funding(coin: str, variant: str) -> pd.DataFrame:
    """Сумма и число событий фандинга по дням: Bybit 2021-01…11 (кэш №5), Binance с 2021-12
    (кэш №20); real2020 — ещё фактический Binance за 2020."""
    by = pd.read_csv(root() / "funding" / f"{coin}USDT_funding.csv")
    by = pd.Series(by["rate"].to_numpy(float), index=pd.to_datetime(by["ts"], unit="ms"))
    by = by[(by.index >= "2021-01-01") & (by.index < "2021-12-01")]
    files = sorted((root() / "binance_metrics" / "funding" / f"{coin}USDT").glob("*.csv"))
    bn = pd.concat([p for p in (pd.read_csv(f) for f in files) if len(p)])
    bn = pd.Series(bn["last_funding_rate"].to_numpy(float),
                   index=pd.to_datetime(bn["calc_time"].astype("int64"), unit="ms"))
    parts = [by, bn[bn.index >= "2021-12-01"]]
    if variant == "real2020":
        b0 = pd.read_csv(vroot() / f"binance2020_{coin}USDT.csv")
        parts.append(pd.Series(b0["last_funding_rate"].to_numpy(float),
                               index=pd.to_datetime(b0["calc_time"].astype("int64"), unit="ms")))
    ev = pd.concat(parts).sort_index()
    if variant != "jitter":
        ev.index = ev.index.round("min")
    if variant == "fund_left":
        day = ev.index.floor("D")
    else:                                                        # (d 00:00; d+1 00:00] → день d
        day = (ev.index - pd.Timedelta(milliseconds=1)).floor("D")
    g = pd.DataFrame({"rate": ev.to_numpy(), "day": day})
    return g.groupby("day")["rate"].agg(["sum", "count"])


def daily_funding(idx: pd.DatetimeIndex, g: pd.DataFrame) -> np.ndarray:
    s = g.reindex(idx)
    n = s["count"].fillna(0).to_numpy()
    return s["sum"].fillna(0).to_numpy() + np.clip(3 - n, 0, None) * F8


def subposition(close: np.ndarray, L: int, variant: str) -> tuple[np.ndarray, np.ndarray]:
    """pos[i] — подпозиция держится в день i; ent[i] — вход по открытию i."""
    c = pd.Series(close)
    prev_max = c.shift(1).rolling(L, min_periods=L).max().to_numpy()
    mid = ((c.rolling(L, min_periods=L).max() + c.rolling(L, min_periods=L).min()) / 2).to_numpy()
    n = len(close)
    pos, ent = np.zeros(n), np.zeros(n)
    in_trade, level, t_sig = False, np.nan, -1
    for t in range(n - 1):
        if in_trade:
            level = mid[t] if variant == "lvl_exec" and t == t_sig + 1 else max(level, mid[t])
            check = not (variant == "noexit_t1" and t == t_sig + 1)
            if check and close[t] < level:
                if variant == "reentry" and np.isfinite(prev_max[t]) and close[t] > prev_max[t]:
                    level, t_sig = mid[t], t
                    pos[t + 1] = 1
                    continue
                in_trade = False                                 # выход по открытию t+1
            else:
                pos[t + 1] = 1
        elif np.isfinite(prev_max[t]) and close[t] > prev_max[t]:
            in_trade, level, t_sig = True, mid[t], t             # уровень — по закрытие t вкл.
            pos[t + 1] = ent[t + 1] = 1
    return pos, ent


def build(src: Path, variant: str) -> dict[str, pd.DataFrame]:
    out = {}
    for coin in ("BTC", "ETH"):
        px = load_prices(src, coin)
        o, c = px["open"].to_numpy(), px["close"].to_numpy()
        r = np.append(o[1:] / o[:-1] - 1, np.nan)               # открытие i → открытие i+1
        r_cc = np.append(np.nan, c[1:] / c[:-1] - 1)
        if variant == "close_close":
            r = r_cc
        elif variant == "lookahead":
            r = np.append(np.nan, r_cc[:-1])
        subs = [subposition(c, L, variant) for L in WINDOWS]
        pos = np.vstack([p for p, _ in subs])
        f = np.zeros(len(px)) if variant == "no_fund" else daily_funding(
            px.index, load_funding(coin, variant))
        share = 0.5 / 9 * pos.sum(0)
        if variant == "gross_cost":
            turn = 0.5 / 9 * np.abs(np.diff(pos, axis=1, prepend=0)).sum(0)
        else:
            turn = np.abs(np.diff(share, prepend=0))
        out[coin] = pd.DataFrame({"r": r, "f": f, "share": share, "turn": turn,
                                  "ent": np.vstack([e for _, e in subs]).sum(0)}, index=px.index)
    idx = out["BTC"].index.intersection(out["ETH"].index)        # общее начало — 2016-05-18
    return {k: v.loc[idx] for k, v in out.items()}


def stats(x: pd.Series) -> dict:
    x = x.dropna()
    eq = np.concatenate([[1.0], np.cumprod(1 + x.to_numpy())])
    tot = eq[-1] - 1
    return dict(tot=tot, ann=(1 + tot) ** (365 / len(x)) - 1,
                sh=x.mean() / x.std(ddof=1) * np.sqrt(365),
                dd=float(np.max(1 - eq / np.maximum.accumulate(eq))))


def block_ci(diff: pd.Series, reps: int = 2000, seed: int = 0) -> tuple[float, float]:
    """90% ДИ средней дневной разницы в годовых, бутстрэп блоками «календарный месяц»."""
    groups = [g.to_numpy() for _, g in diff.groupby(diff.index.to_period("M"))]
    rng = np.random.default_rng(seed)
    ms = [np.concatenate([groups[i] for i in rng.integers(0, len(groups), len(groups))]).mean()
          for _ in range(reps)]
    return float(np.percentile(ms, 5) * 365), float(np.percentile(ms, 95) * 365)


def report(src: Path, variant: str) -> None:
    print(f"\n== вариант {variant}")
    b = build(src, variant)
    B, E = b["BTC"], b["ETH"]
    cm = 2.0 if variant == "cost2" else 1.0
    sys_r = B.share * (B.r - B.f) + E.share * (E.r - E.f) - (B.turn + E.turn) * COST * cm
    perp = 0.5 * (B.r - B.f) + 0.5 * (E.r - E.f)
    spot = 0.5 * B.r + 0.5 * E.r
    res = {}
    for name, a, z in SEGS:
        m = (sys_r.index >= a) & (sys_r.index <= z)
        aB, aE = B.share[m].mean(), E.share[m].mean()
        same = (aB * (B.r - B.f) + aE * (E.r - E.f))[m]
        s, p, h, q = stats(sys_r[m]), stats(perp[m]), stats(spot[m]), stats(same)
        diff = (sys_r[m] - same).dropna()
        lo, hi = block_ci(diff)
        res[name] = dict(s=s, p=p, timing=diff.mean() * 365, lo=lo)
        over = ((B.share - aB) * B.f + (E.share - aE) * E.f)[m].mean() * 365
        print(f"  {name:9s} система {s['tot']:+8.1%} ({s['ann']:+6.1%}/г) Шарп {s['sh']:5.2f} "
              f"DD {s['dd']:5.1%} | перп {p['tot']:+7.1%} Шарп {p['sh']:5.2f} DD {p['dd']:5.1%} "
              f"| спот {h['tot']:+7.1%} | та же доля {q['tot']:+7.1%} (доля {aB + aE:.2f})")
        print(f"  {'':9s} тайминг {diff.mean() * 365:+6.1%}/г [{lo:+6.1%}; {hi:+6.1%}] | входов "
              f"BTC {int(B.ent[m].sum())} ETH {int(E.ent[m].sum())} | издержки "
              f"{((B.turn + E.turn)[m] * COST * cm).sum():.2%} | фандинг "
              f"{((B.share * B.f + E.share * E.f)[m]).sum():.1%}, переплата за тайминг "
              f"{over:+.1%}/г")
    print(f"  входов с 2016-05-18 (начало ETH): BTC {int(B.ent.sum())}, ETH {int(E.ent.sum())}; "
          "BTC считается по истории с 2015-07-20, с обрезкой по 2016-05-18 — 689, как в замере")
    s4, w = res["S4"], res["вся"]
    crit = {
        "1 S4: итог > 0": s4["s"]["tot"] > 0,
        "2 S4: Шарп выше держания перпа": s4["s"]["sh"] > s4["p"]["sh"],
        "3 S1, S2, S3: итог > 0": all(res[k]["s"]["tot"] > 0 for k in ("S1", "S2", "S3")),
        "4 вся: Шарп выше и просадка меньше, чем у перпа":
            w["s"]["sh"] > w["p"]["sh"] and w["s"]["dd"] < w["p"]["dd"],
        "5 вся: тайминг > 0, нижняя граница ДИ > 0": w["timing"] > 0 and w["lo"] > 0,
    }
    for k, ok in crit.items():
        print(f"  {'✓' if ok else '✗'} {k}")
    print(f"  ИТОГ: {'кандидат' if all(crit.values()) else 'не кандидат'}")


def cmd_run(a) -> None:
    src = Path(a.prices) if a.prices else vroot()
    for v in (VARIANTS if a.all else (a.variant,)):
        report(src, v)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    p = sub.add_parser("run")
    p.add_argument("--prices", default=None, help="папка со свечами (по умолчанию trend24_verify)")
    p.add_argument("--variant", default="base", choices=VARIANTS)
    p.add_argument("--all", action="store_true", help="все варианты подряд")
    a = ap.parse_args()
    {"fetch": cmd_fetch, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    main()
