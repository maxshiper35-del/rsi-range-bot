"""Замер №23: «монеты после листинга фьючерсов на Binance дешевеют» — шорт новых USDT-M
(docs/measurements/2026-11-12-new-listing-short.md).

  DATA_ROOT="/Users/mac/bot ctypto/data" .venv/bin/python -m scripts.measure_listing_short fetch
  DATA_ROOT="/Users/mac/bot ctypto/data" .venv/bin/python -m scripts.measure_listing_short run

fetch — список новых бессрочных …USDT (первый день metrics ≥ 2022-01-05; акции, сырьё и индексы
        по exchangeInfo исключены), их 1h-свечи и фандинг за первые месяцы, первый месяц спота
        базовой монеты, свечи «старых» символов для поиска переименований. Кэш общий с №20.
run   — 8 вариантов (вход через k суток, удержание H, стоп +s), периоды по дате выхода, два
        плацебо (крупные старые альты и старые альты той же волатильности), критерии регистрации.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.binance_metrics_fetch import HOST, S3, fetch, get, run_calls
from scripts.binance_metrics_fetch import root as bm_root

FIRST_MIN = "2022-01-05"            # 2021-12 — начало архива metrics: «первый день» там не листинг
TAKER = 0.00055
SLIP_NEW, SLIP_OLD = 0.0010, 0.0002  # на сторону: новые листинги тонкие, крупные альты — нет
STOP_EXTRA = 0.01                   # стоп у новой монеты исполняется на 1% хуже уровня (1h, не 1m)
KS, HS, STOPS = (1, 7), (14, 30), (0.30, 0.60)
PERIODS = (("учёба", "2022-01-01", "2024-12-31"), ("проверка A", "2025-01-01", "2025-12-31"),
           ("проверка B", "2026-01-01", "2026-07-31"), ("заморозка", "2026-08-01", "2026-10-30"))
INDEXES = {"BTCDOMUSDT", "DEFIUSDT", "FOOTBALLUSDT", "BLUEBIRDUSDT", "ALLUSDT"}
MULTS = (1, 10, 100, 1000, 10000, 1e5, 1e6, 0.1, 0.01, 0.001)


def lroot() -> Path:
    p = Path(os.environ.get("DATA_ROOT", "data")) / "cache_research" / "listing"
    p.mkdir(parents=True, exist_ok=True)
    return p


def ym_add(ym: str, k: int) -> str:
    y, m = map(int, ym.split("-"))
    m += k
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def meta() -> dict:
    """Символ → (тип базового актива, тип контракта, onboardDate) по exchangeInfo; у делистнутых
    записи нет — считаем их монетами (акции на Binance появились в 2026, все ещё торгуются)."""
    p = lroot() / "exchangeInfo.json"
    if not p.exists():
        p.write_bytes(get("https://fapi.binance.com/fapi/v1/exchangeInfo"))
    d = json.loads(p.read_text())
    return {s["symbol"]: (s.get("underlyingType"), s.get("contractType"), s.get("onboardDate"))
            for s in d["symbols"]}


def first_days() -> dict[str, tuple[str, str]]:
    """Символ → (первый, последний день metrics) по индексу №20."""
    out = {}
    for f in sorted((bm_root() / "index").glob("*.txt")):
        days = f.read_text().split()
        if days:
            out[f.stem] = (days[0], days[-1])
    return out


def new_listings() -> list[str]:
    m, fd = meta(), first_days()
    out = []
    for s, (d0, _d1) in fd.items():
        if d0 < FIRST_MIN or s in INDEXES or not re.fullmatch(r"[A-Z0-9]+USDT", s):
            continue
        ut, ct, _ = m.get(s, ("COIN", "PERPETUAL", None))
        if ut not in ("COIN", "PREMARKET") or (ct or "").startswith("TRADIFI"):
            continue
        out.append(s)
    return sorted(out)


def base_asset(sym: str) -> str:
    b = sym[:-4]
    return re.sub(r"^(1000000|100000|10000|1000|1M)(?=[A-Z])", "", b)


def spot_first_month(base: str) -> str:
    """Первый месяц 1d-свечей спота <BASE>USDT в архиве ('' — спота нет)."""
    p = lroot() / "spot_first" / f"{base}.txt"
    if p.exists():
        return p.read_text().strip()
    xml = get(f"{S3}?prefix=data/spot/monthly/klines/{base}USDT/1d/&max-keys=5").decode()
    ms = re.findall(rf"{base}USDT-1d-(\d{{4}}-\d{{2}})\.zip</Key>", xml)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text((min(ms) if ms else "") + "\n")
    return min(ms) if ms else ""


def kfile(s: str, ym: str) -> Path:
    return bm_root() / "klines" / s / f"{s}-1h-{ym}.csv"


def ffile(s: str, ym: str) -> Path:
    return bm_root() / "funding" / s / f"{s}-fundingRate-{ym}.csv"


def jobs_for(s: str, months: list[str]) -> list:
    out = []
    for ym in months:
        out.append(lambda s=s, ym=ym: fetch(f"{HOST}/monthly/klines/{s}/1h/{s}-1h-{ym}.zip",
                                            kfile(s, ym)))
        out.append(lambda s=s, ym=ym: fetch(
            f"{HOST}/monthly/fundingRate/{s}/{s}-fundingRate-{ym}.zip", ffile(s, ym)))
    return out


def rename_candidates(fd: dict, new: list[str]) -> dict[str, list[str]]:
    """Новому символу N — «старые» символы, чья история кончилась рядом с его стартом:
    последний день в [старт N − 3 дн; старт N + 60 дн], первый — раньше старта N на 30+ дней."""
    out = {}
    for n in new:
        t0 = pd.Timestamp(fd[n][0])
        c = [o for o, (a, b) in fd.items() if o != n
             and t0 - pd.Timedelta(days=3) <= pd.Timestamp(b) <= t0 + pd.Timedelta(days=60)
             and pd.Timestamp(a) < t0 - pd.Timedelta(days=30)]
        if c:
            out[n] = c
    return out


def cmd_fetch(_a) -> None:
    fd = first_days()
    new = new_listings()
    (lroot() / "new_symbols.txt").write_text("\n".join(new) + "\n")
    print(f"новых бессрочных …USDT с {FIRST_MIN}: {len(new)}", flush=True)
    calls = []
    for s in new:
        m0 = fd[s][0][:7]
        calls += jobs_for(s, [ym_add(m0, k) for k in range(0, 3) if ym_add(m0, k) <= "2026-10"])
    ren = rename_candidates(fd, new)
    for n, olds in ren.items():
        m0 = fd[n][0][:7]
        for o in olds:
            calls += jobs_for(o, [m0])
    calls += [lambda b=b: spot_first_month(b) or "нет спота" for b in sorted({base_asset(s)
                                                                              for s in new})]
    run_calls(calls)


def load_1h(s: str, months: list[str]) -> pd.DataFrame | None:
    parts = []
    for ym in months:
        p = kfile(s, ym)
        if p.exists():
            d = pd.read_csv(p, header=None, usecols=range(5),
                            names=["t", "open", "high", "low", "close"])
            d = d[pd.to_numeric(d["t"], errors="coerce").notna()]
            parts.append(d.astype(float))
    if not parts:
        return None
    d = pd.concat(parts).drop_duplicates("t").sort_values("t")
    d["t"] = pd.to_datetime(d["t"].astype("int64"), unit="ms", utc=True)
    return d.set_index("t")


def load_funding(s: str, months: list[str]) -> pd.Series:
    parts = []
    for ym in months:
        p = ffile(s, ym)
        if p.exists():
            d = pd.read_csv(p)
            if len(d):
                parts.append(d)
    if not parts:                                   # фандинга нет — пустой ряд с индексом времени
        return pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC"))
    d = pd.concat(parts)
    t = pd.to_datetime(pd.to_numeric(d["calc_time"]), unit="ms", utc=True)
    return pd.Series(d["last_funding_rate"].astype(float).to_numpy(), index=t).sort_index()


def short_trade(px: pd.DataFrame, fund: pd.Series, t_in: pd.Timestamp, h_days: int,
                stop: float, slip: float, stop_extra: float) -> dict | None:
    """Шорт с открытия часа t_in на h_days суток; стоп +stop от входа по максимуму часа.
    -> net, gross, funding, исход; None — нет данных на входе или дыра в окне."""
    t_out = t_in + pd.Timedelta(days=h_days)
    w = px.loc[t_in:t_out]
    if not len(w) or w.index[0] != t_in:
        return None
    entry = w["open"].iloc[0]
    lvl = entry * (1 + stop)
    end_t, exit_px, how = None, None, "время"
    body = w.iloc[:-1] if w.index[-1] == t_out else w
    hit = body.index[body["high"].to_numpy() >= lvl]
    if len(hit):
        end_t = hit[0]
        exit_px = max(body.at[end_t, "open"], lvl) * (1 + stop_extra)
        how = "стоп"
    elif w.index[-1] == t_out:
        end_t, exit_px = t_out, w.at[t_out, "open"]
    else:                                               # история кончилась раньше: делистинг?
        last = px.index[-1]
        if last > w.index[-1] or last >= t_out:
            return None                                 # дыра в данных, а не конец истории
        end_t, exit_px, how = last, w["close"].iloc[-1], "делистинг"
    gross = (entry - exit_px) / entry
    f = fund.loc[(fund.index > t_in) & (fund.index <= end_t)]
    fr = 0.0
    if len(f):
        pf = px["close"].reindex(f.index.floor("h"), method="ffill").to_numpy()
        fr = float(np.nansum(f.to_numpy() * pf / entry))
    cost = 2 * (TAKER + slip)
    return dict(net=gross + fr - cost, gross=gross, fund=fr, how=how, t_out=end_t)


def period_of(t: pd.Timestamp) -> str | None:
    d = t.strftime("%Y-%m-%d")
    return next((n for n, a, b in PERIODS if a <= d <= b), None)


def block_ci(df: pd.DataFrame, col: str, reps: int = 2000) -> tuple[float, float]:
    """90% ДИ среднего бутстрепом блоками «месяц листинга» (листинги идут волнами)."""
    g = [x[col].to_numpy() for _, x in df.groupby("lmonth")]
    if len(g) < 3:
        return math.nan, math.nan
    rng = np.random.default_rng(23)
    ms = []
    for _ in range(reps):
        pick = rng.integers(0, len(g), len(g))
        v = np.concatenate([g[i] for i in pick])
        ms.append(v.mean())
    return float(np.percentile(ms, 5)), float(np.percentile(ms, 95))


def detect_renames(fd: dict, new: list[str]) -> dict[str, str]:
    """N — переименование O, если в первый общий час цены совпадают с точностью 3% с учётом
    множителя (1000X и т. п.)."""
    out = {}
    for n, olds in rename_candidates(fd, new).items():
        m0 = fd[n][0][:7]
        pn = load_1h(n, [m0])
        if pn is None:
            continue
        for o in olds:
            po = load_1h(o, [m0])
            if po is None:
                continue
            common = pn.index.intersection(po.index)
            if not len(common):
                continue
            r = po.at[common[0], "close"] / pn.at[common[0], "close"]
            if any(abs(r / m - 1) < 0.03 for m in MULTS):
                out[n] = o
                break
    return out


class OldUniverse:
    """«Старые» символы для плацебо — кэш №20 (топ-40 по OI каждый месяц с 2022-01, 207 шт.):
    1h-свечи и фандинг за всю историю, скользящая волатильность часовых доходностей."""

    def __init__(self) -> None:
        self.px, self.fund, self.vol, self.first = {}, {}, {}, {}
        fd = first_days()
        for d in sorted((bm_root() / "klines").iterdir()):
            months = sorted(f.stem.rsplit("-1h-", 1)[1] for f in d.glob("*.csv"))
            if len(months) < 13:
                continue
            px = load_1h(d.name, months)
            if px is None:
                continue
            self.px[d.name] = px
            self.fund[d.name] = load_funding(d.name, months)
            r = np.log(px["close"]).diff()
            self.vol[d.name] = {k: r.rolling(24 * k, min_periods=12 * k).std().shift(1) for k in KS}
            self.first[d.name] = pd.Timestamp(fd.get(d.name, (months[0] + "-01",))[0], tz="UTC")
        u = pd.read_csv(bm_root() / "universe.csv")
        u["month"] = pd.to_datetime(u["month"], utc=True)
        self.rank = u
        # первый месяц в топ-40 по OI: кандидат плацебо — только символ, уже побывавший там к
        # месяцу входа (поправка до прогона 12.11: иначе в пул попадали будущие «выросшие»)
        self.first_top = u.groupby("symbol")["month"].min().to_dict()
        print(f"старых символов в кэше: {len(self.px)}", flush=True)

    def old_ok(self, s: str, t: pd.Timestamp) -> bool:
        seen = self.first_top.get(s)
        return (s in self.px and self.first[s] <= t - pd.Timedelta(days=365) and s != "BTCUSDT"
                and seen is not None and seen <= t.normalize().replace(day=1))

    def basket_big(self, t: pd.Timestamp) -> list[str]:
        m = self.rank[self.rank["month"] == t.normalize().replace(day=1)]
        return [s for s in m[(m["rank"] >= 3) & (m["rank"] <= 30)]["symbol"] if self.old_ok(s, t)]

    def basket_vol(self, t: pd.Timestamp, k: int, v_new: float, n: int = 10) -> tuple:
        c = []
        for s, vv in self.vol.items():
            if not self.old_ok(s, t):
                continue
            v = vv[k].get(t, np.nan)
            if np.isfinite(v) and v > 0:
                c.append((abs(math.log(v / v_new)), s, v))
        c.sort()
        pick = c[:n]
        med = float(np.median([v for *_, v in pick])) if pick else math.nan
        return [s for _, s, _ in pick], med

    def mean_short(self, syms: list[str], t_in, h, stop) -> float:
        out = []
        for s in syms:
            tr = short_trade(self.px[s], self.fund[s], t_in, h, stop, SLIP_OLD, 0.0)
            if tr is not None:
                out.append(tr["net"])
        return float(np.mean(out)) if out else math.nan

    def btc_ret(self, t_in, t_out) -> float:
        px = self.px["BTCUSDT"]["open"]
        try:
            return float(px.asof(t_out) / px.asof(t_in) - 1)
        except (KeyError, ZeroDivisionError):
            return math.nan


def build(old: OldUniverse) -> tuple[pd.DataFrame, dict]:
    fd, info = first_days(), {}
    new = [s for s in (lroot() / "new_symbols.txt").read_text().split() if s]
    ren = detect_renames(fd, new)
    info["renames"] = ren
    rows, skipped = [], {"мало данных": 0}
    for s in new:
        if s in ren:
            continue
        m0 = fd[s][0][:7]
        months = [ym_add(m0, k) for k in range(-1, 3)]
        px = load_1h(s, months)
        if px is None or len(px) < 48:
            skipped["мало данных"] += 1
            continue
        fund = load_funding(s, months)
        t_list = px.index[0]
        spot = spot_first_month(base_asset(s))
        young = spot == "" or spot >= ym_add(t_list.strftime("%Y-%m"), -3)
        lr = np.log(px["close"]).diff()
        for k in KS:
            t_in = t_list + pd.Timedelta(days=k)
            v_new = float(lr.loc[:t_in - pd.Timedelta(hours=1)].std())
            big = old.basket_big(t_in)
            volb, v_match = old.basket_vol(t_in, k, v_new) if v_new > 0 else ([], math.nan)
            for h in HS:
                for st in STOPS:
                    tr = short_trade(px, fund, t_in, h, st, SLIP_NEW, STOP_EXTRA)
                    if tr is None:
                        continue
                    per = period_of(tr["t_out"])
                    if per is None:
                        continue
                    rows.append(dict(sym=s, k=k, H=h, stop=st, t_in=t_in, lmonth=m0, young=young,
                                     period=per, v_new=v_new, v_match=v_match,
                                     p_big=old.mean_short(big, t_in, h, st),
                                     p_vol=old.mean_short(volb, t_in, h, st),
                                     btc=old.btc_ret(t_in, tr["t_out"]), **tr))
    info["skipped"] = skipped
    return pd.DataFrame(rows), info


def summary(d: pd.DataFrame) -> str:
    if not len(d):
        return "n 0"
    ex = (d["net"] - d["p_vol"]).dropna()
    return (f"n {len(d):3d} | net {d['net'].mean():+.2%} (gross {d['gross'].mean():+.2%}, фандинг "
            f"{d['fund'].mean():+.2%}) | стопов {np.mean(d['how'] == 'стоп'):.0%} | плацебо: "
            f"крупные {d['p_big'].mean():+.2%}, той же вол. {d['p_vol'].mean():+.2%} | "
            f"сверх плацебо вол. {ex.mean():+.2%}")


def cmd_run(a) -> None:
    old = OldUniverse()
    d, info = build(old)
    d.to_pickle(lroot() / "trades.pkl")
    print(f"переименования (исключены): {info['renames']}")
    print(f"пропущено: {info['skipped']}")
    one = d[(d.k == KS[0]) & (d.H == HS[0]) & (d.stop == STOPS[0])]
    young = one.drop_duplicates("sym")["young"].mean()
    print(f"листингов в замере: {one['sym'].nunique()}; молодых {young:.0%}")
    print(one.groupby("period")["sym"].nunique().to_string())
    vol_ratio = (one["v_new"] / one["v_match"]).median()
    print(f"волатильность новой монеты / плацебо той же вол. (медиана): {vol_ratio:.2f}")
    print("\n== Все 8 вариантов, учёба (выбор — max net на сделку при n ≥ 30)")
    best, best_v = None, -math.inf
    for (k, h, st), g in d[d.period == "учёба"].groupby(["k", "H", "stop"]):
        print(f"  k={k} H={h} стоп +{st:.0%}: {summary(g)}")
        if len(g) >= 30 and g["net"].mean() > best_v:
            best, best_v = (k, h, st), g["net"].mean()
    k, h, st = best
    print(f"\nВЫБРАН: вход через {k} сут, удержание {h} сут, стоп +{st:.0%}")
    sel = d[(d.k == k) & (d.H == h) & (d.stop == st)]
    for per in ("учёба", "проверка A", "проверка B"):
        g = sel[sel.period == per]
        print(f"  {per:10s}: {summary(g)}")
        print(f"             молодые: {summary(g[g.young])}")
        print(f"             старые:  {summary(g[~g.young])}")
        if len(g):
            print(f"             ниже входа на выходе: {np.mean(g['gross'] > 0):.0%}; "
                  f"с хеджем лонг BTC: {(g['net'] + g['btc']).mean():+.2%}")
    v = sel[sel.period.isin(["проверка A", "проверка B"])].copy()
    lo, hi = block_ci(v, "net")
    v["ex"] = v["net"] - v["p_vol"]
    elo, ehi = block_ci(v.dropna(subset=["ex"]), "ex")
    top5 = v["net"].nlargest(5).index
    wo = v.drop(top5)["net"].mean()
    mon = v.groupby(v["t_out"].dt.strftime("%Y-%m"))["net"].sum()
    share = mon.max() / mon.sum() if mon.sum() > 0 else math.nan
    cum = v.sort_values("t_out")["net"].cumsum()
    dd = float((cum.cummax() - cum).max()) if len(cum) else math.nan
    print(f"\n  проверка A+B: net {v['net'].mean():+.2%} [90% ДИ блоками {lo:+.2%}; {hi:+.2%}], "
          f"сверх плацебо вол. {v['ex'].mean():+.2%} [{elo:+.2%}; {ehi:+.2%}]")
    print(f"  без 5 лучших сделок: {wo:+.2%}; лучший месяц — {share:.0%} суммы; "
          f"просадка суммы {dd:.2f} (в долях позиции)")
    a_ = sel[sel.period == "проверка A"]
    b_ = sel[sel.period == "проверка B"]
    crit = {
        "1 net > 0 в A и в B": a_["net"].mean() > 0 and b_["net"].mean() > 0,
        "2 сверх плацебо вол. > 0 в A и в B": (a_["net"] - a_["p_vol"]).mean() > 0
        and (b_["net"] - b_["p_vol"]).mean() > 0,
        "3 нижняя граница ДИ net A+B > 0": lo > 0,
        "4 без 5 лучших сделок net > 0": wo > 0,
        "5 лучший месяц < 50% суммы": share < 0.5,
    }
    for c_, ok in crit.items():
        print(f"  {'✓' if ok else '✗'} {c_}")
    print(f"\nИТОГ: {'КАНДИДАТ — дальше заморозка и 1m' if all(crit.values()) else 'не кандидат'}")
    if a.frozen:
        z = sel[sel.period == "заморозка"]
        print(f"\n== ЗАМОРОЗКА (один раз): {summary(z)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    p = sub.add_parser("run")
    p.add_argument("--frozen", action="store_true", help="показать заморозку (один раз)")
    a = ap.parse_args()
    {"fetch": cmd_fetch, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    main()
