"""Бэктест кросс-секционного моментума на перпетуалах Bybit — правила и критерий в
docs/measurements/2026-10-31-xs-momentum.md (регистрация до прогона, коммит 8ed5a7e).

Данные — scripts/research_xsmom_fetch.py (дневные свечи всех USDT-перпетуалов, включая
делистнутые, и фандинг). Модель недели: веса ставятся по close(t), держатся до
close(t + 7д), внутри недели дрейфуют с ценой; издержки — тейкер + проскальзывание по рангу
оборота на проторгованный номинал, фандинг — по меткам в (t, t + 7д].

  nice -n 15 .venv/bin/python -m scripts.research_xsmom universe   # список для фандинга
  nice -n 15 .venv/bin/python -m scripts.research_xsmom run        # учёба -> выбор -> проверка
  nice -n 15 .venv/bin/python -m scripts.research_xsmom frozen     # одна оценка заморозки
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

CACHE = Path("/Users/mac/bot ctypto/data/cache_research/xsmom")
DAY = pd.Timedelta(days=1)
TRAIN = (pd.Timestamp("2021-01-01", tz="UTC"), pd.Timestamp("2023-12-31", tz="UTC"))
VALID = (pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2026-07-31", tz="UTC"))
V1 = (pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2025-03-31", tz="UTC"))
V2 = (pd.Timestamp("2025-04-01", tz="UTC"), pd.Timestamp("2026-07-31", tz="UTC"))
FROZEN_FROM = pd.Timestamp("2026-08-01", tz="UTC")
FEE = 0.00055
MIN_AGE = 35
TURN_WIN = 30
MIN_UNIVERSE = 20
STABLE = {"USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDE", "USDD", "PYUSD", "USTC", "UST",
          "USDP", "GUSD", "EURT", "EUR", "USD1", "RLUSD", "USDQ", "USDT0"}
GRID_L = (1, 2, 4)
GRID_S = (0, 1)
GRID_K = (3, 5, 10)


SLIP_MULT = [1.0]      # проверка устойчивости: проскальзывание ×3


def slip_for_rank(rank: float) -> float:
    if rank <= 20:
        return 0.0002 * SLIP_MULT[0]
    if rank <= 50:
        return 0.0005 * SLIP_MULT[0]
    return 0.0010 * SLIP_MULT[0]


# ----------------------------------------------------------------------------- данные

@dataclass
class Panel:
    close: pd.DataFrame      # индекс — момент закрытия свечи (UTC 00:00 следующего дня)
    turn: pd.DataFrame       # дневной оборот, USDT
    funding: dict            # символ -> pd.Series ставок по меткам времени
    age: pd.DataFrame | None = None     # число закрытий до момента (включительно)
    turn30: pd.DataFrame | None = None  # средний оборот за 30 дней до момента
    _uni: dict | None = None            # кэш вселенных


def _base(sym: str) -> str:
    b = sym[:-4]
    return b.lstrip("0123456789") if b[:1].isdigit() else b


def load_panel(with_funding: bool = True) -> Panel:
    closes, turns = {}, {}
    for f in sorted((CACHE / "klines").glob("*.csv")):
        sym = f.stem
        if _base(sym) in STABLE:
            continue
        df = pd.read_csv(f)
        if df.empty:
            continue
        idx = pd.to_datetime(df["ts"], unit="ms", utc=True) + DAY
        closes[sym] = pd.Series(df["close"].to_numpy(float), index=idx)
        turns[sym] = pd.Series(df["turnover"].to_numpy(float), index=idx)
    close = pd.DataFrame(closes).sort_index()
    close = close[close.index <= pd.Timestamp.now(tz="UTC")]   # незакрытая свеча — не цена
    turn = pd.DataFrame(turns).reindex(close.index)
    full = pd.date_range(close.index.min(), close.index.max(), freq="D")
    close, turn = close.reindex(full), turn.reindex(full)
    funding = {}
    if with_funding:
        for f in sorted((CACHE / "funding").glob("*.csv")):
            df = pd.read_csv(f)
            if not df.empty:
                funding[f.stem] = pd.Series(df["rate"].to_numpy(float),
                                            index=pd.to_datetime(df["ts"], unit="ms", utc=True))
    age = close.notna().cumsum()
    turn30 = turn.rolling(TURN_WIN, min_periods=1).mean()
    return Panel(close, turn, funding, age, turn30, {})


def mondays(p: Panel, start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    """Понедельники t в [start, end], для которых известна вся неделя вперёд (close(t+7))."""
    last = p.close.index.max()
    out = []
    for t in pd.date_range(start, end, freq="W-MON"):
        if t + 7 * DAY <= last:
            out.append(t)
    return out


# ------------------------------------------------------------------------- вселенная

def universe(p: Panel, t: pd.Timestamp, n: int, exclude: frozenset = frozenset()
             ) -> tuple[list[str], dict[str, int]]:
    """Топ-n по среднему обороту за 30 дней до t среди торгуемых символов возраста ≥ 35.
    -> (вселенная, ранг оборота всех подходящих символов — для проскальзывания)."""
    key = (t, n, exclude)
    if key in p._uni:
        return p._uni[key]
    if t not in p.close.index:
        return [], {}
    alive = p.close.loc[t].notna()
    ok = alive & (p.age.loc[t] >= MIN_AGE)
    tv = p.turn30.loc[t][ok].dropna().sort_values(ascending=False)
    ranks = {s: i + 1 for i, s in enumerate(tv.index)}
    cand = [s for s in tv.index if s not in exclude]
    p._uni[key] = (cand[:n], ranks)
    return p._uni[key]


# --------------------------------------------------------------------------- бэктест

@dataclass(frozen=True)
class Variant:
    family: str   # "A" лонг-шорт, "B" лонг против BTC
    L: int
    s: int
    K: int

    @property
    def name(self) -> str:
        return f"{self.family} L{self.L} s{self.s} K{self.K}"


ALL = [Variant(f, L, s, K) for f in ("A", "B") for L, s, K in product(GRID_L, GRID_S, GRID_K)]


def neighbors(v: Variant) -> list[Variant]:
    out = []
    li, ki = GRID_L.index(v.L), GRID_K.index(v.K)
    for d in (-1, 1):
        if 0 <= li + d < len(GRID_L):
            out.append(Variant(v.family, GRID_L[li + d], v.s, v.K))
        if 0 <= ki + d < len(GRID_K):
            out.append(Variant(v.family, v.L, v.s, GRID_K[ki + d]))
    out.append(Variant(v.family, v.L, 1 - v.s, v.K))
    return out


def target_weights(p: Panel, v: Variant, t: pd.Timestamp, uni: list[str]) -> dict[str, float]:
    a, b = t - v.s * DAY, t - v.s * DAY - 7 * v.L * DAY
    if a not in p.close.index or b not in p.close.index:
        return {}
    ca, cb = p.close.loc[a, uni], p.close.loc[b, uni]
    r = (ca / cb - 1).replace([np.inf, -np.inf], np.nan).dropna()
    if v.family == "A":
        if len(r) < 2 * v.K:
            return {}
        r = r.sort_values(ascending=False)
        w = {s: 0.5 / v.K for s in r.index[:v.K]}
        w.update({s: -0.5 / v.K for s in r.index[-v.K:]})
        return w
    if "BTCUSDT" not in p.close.columns or pd.isna(p.close.at[t, "BTCUSDT"]):
        return {}
    r = r.drop("BTCUSDT", errors="ignore").sort_values(ascending=False)
    if len(r) < v.K:
        return {}
    w = {s: 0.5 / v.K for s in r.index[:v.K]}
    w["BTCUSDT"] = -0.5
    return w


FUNDING_MTM = [True]   # см. раздел 9 документа: модель регистрации — номинал на момент t


def _funding_sum(p: Panel, sym: str, t0: pd.Timestamp, t1: pd.Timestamp) -> float:
    """Σ ставок фандинга в (t0, t1] в долях начального номинала позиции.

    FUNDING_MTM (исправление после первого прогона, раздел 9): каждая ставка умножается на
    P(τ)/P(t0) — номинал на момент начисления, цена = последнее дневное закрытие ≤ τ. Модель
    регистрации («номинал на момент ребалансировки») завышала фандинг до 10 раз там, где
    цена за неделю падала на 90% (RAVE 13.04.2026)."""
    s = p.funding.get(sym)
    if s is None:
        return 0.0
    seg = s[(s.index > t0) & (s.index <= t1)]
    if seg.empty:
        return 0.0
    if not FUNDING_MTM[0]:
        return float(seg.sum())
    px = p.close[sym].loc[:t1].dropna()
    p0 = px.loc[:t0].iloc[-1]
    rel = px.reindex(seg.index, method="ffill").to_numpy(float) / p0
    return float((seg.to_numpy(float) * rel).sum())


def run_variant(p: Panel, v: Variant, weeks: list[pd.Timestamp], n: int = 50,
                exclude: frozenset = frozenset(), delay: int = 0,
                track: bool = False) -> pd.DataFrame:
    """Недельный ряд варианта. delay — сдвиг исполнения в днях (сигнал по t, сделки по
    close(t + delay), держим до close(t + delay + 7))."""
    rows = []
    held: dict[str, float] = {}           # веса после дрейфа на момент следующей ребалансировки
    contrib: dict[str, float] = {}
    for t in weeks:
        uni, ranks = universe(p, t, n, exclude)
        te = t + delay * DAY
        t1 = te + 7 * DAY
        if len(uni) < MIN_UNIVERSE or te not in p.close.index or t1 > p.close.index.max():
            w_new: dict[str, float] = {}
        else:
            w_new = target_weights(p, v, t, uni)
            w_new = {s: w for s, w in w_new.items() if not pd.isna(p.close.at[te, s])}
        # ребалансировка: проторгованный номинал по союзу символов
        cost = 0.0
        for s in set(held) | set(w_new):
            dw = abs(w_new.get(s, 0.0) - held.get(s, 0.0))
            if dw:
                c = dw * (FEE + slip_for_rank(ranks.get(s, 999)))
                cost += c
                contrib[s] = contrib.get(s, 0.0) - c
        gross = fund = 0.0
        after = {}
        if w_new:
            for s, w in w_new.items():
                seg = p.close.loc[te:t1, s]
                c0 = seg.iloc[0]
                last_ok = seg.dropna()
                c1 = last_ok.iloc[-1]
                end_t = last_ok.index[-1]
                r = c1 / c0 - 1
                f = -w * _funding_sum(p, s, te, end_t)
                pnl = w * r
                if end_t < t1:                  # делистинг внутри недели: выход по последней
                    exit_c = abs(w * (1 + r)) * (FEE + 0.0010)
                    pnl -= exit_c
                    cost += exit_c
                else:
                    after[s] = w * (1 + r)
                gross += w * r
                fund += f
                contrib[s] = contrib.get(s, 0.0) + pnl + f
            net = gross + fund - cost
            eq = 1 + net
            held = {s: x / eq for s, x in after.items()} if eq > 0 else {}
        else:
            net = -cost
            held = {}
        btc = np.nan
        if "BTCUSDT" in p.close.columns and te in p.close.index and t1 <= p.close.index.max():
            b0, b1 = p.close.at[te, "BTCUSDT"], p.close.at[t1, "BTCUSDT"]
            if not (pd.isna(b0) or pd.isna(b1)):
                btc = b1 / b0 - 1
        rows.append(dict(t=t, net=net, gross=gross, fund=fund, cost=cost,
                         active=bool(w_new), btc=btc, n_uni=len(uni)))
    df = pd.DataFrame(rows).set_index("t")
    if track:
        df.attrs["contrib"] = contrib
    return df


def turnover_series(p: Panel, v: Variant, weeks: list[pd.Timestamp], n: int = 50) -> float:
    """Средний недельный проторгованный номинал Σ|Δw| (доля капитала, обе стороны)."""
    held: dict[str, float] = {}
    tot, k = 0.0, 0
    for t in weeks:
        uni, _ = universe(p, t, n)
        t1 = t + 7 * DAY
        w_new = target_weights(p, v, t, uni) if len(uni) >= MIN_UNIVERSE else {}
        w_new = {s: w for s, w in w_new.items() if not pd.isna(p.close.at[t, s])}
        tot += sum(abs(w_new.get(s, 0.0) - held.get(s, 0.0)) for s in set(held) | set(w_new))
        k += 1
        after = {}
        g = 0.0
        for s, w in w_new.items():
            seg = p.close.loc[t:t1, s].dropna()
            r = seg.iloc[-1] / seg.iloc[0] - 1
            g += w * r
            if seg.index[-1] == t1:
                after[s] = w * (1 + r)
        held = {s: x / (1 + g) for s, x in after.items()}
    return tot / max(k, 1)


# -------------------------------------------------------------------------- метрики

def metrics(df: pd.DataFrame, lo: pd.Timestamp, hi: pd.Timestamp, col: str = "net") -> dict:
    d = df[(df.index >= lo) & (df.index <= hi)]
    act = d[d["active"]]
    r = d[col].astype(float)
    n = len(r)
    if n == 0 or len(act) == 0:
        return dict(n=0, ann=np.nan, sharpe=np.nan, mdd=np.nan, win=np.nan, beta=np.nan,
                    total=np.nan)
    eq = (1 + r).cumprod()
    ann = eq.iloc[-1] ** (52 / n) - 1 if eq.iloc[-1] > 0 else -1.0
    sd = r.std(ddof=1)
    sharpe = r.mean() / sd * math.sqrt(52) if sd > 0 else np.nan
    mdd = float((eq / eq.cummax() - 1).min())
    win = float((act[col] > 0).mean())
    b = d.dropna(subset=["btc"])
    beta = float(np.polyfit(b["btc"], b[col], 1)[0]) if len(b) > 5 else np.nan
    return dict(n=n, ann=float(ann), sharpe=float(sharpe), mdd=mdd, win=win, beta=beta,
                total=float(eq.iloc[-1] - 1))


def fmt(m: dict) -> str:
    if not m["n"]:
        return "нет недель"
    return (f"n={m['n']:3d}  год {m['ann']*100:+7.1f}%  Шарп {m['sharpe']:+5.2f}  "
            f"просадка {m['mdd']*100:6.1f}%  прибыльных {m['win']*100:4.0f}%  "
            f"бета {m['beta']:+.2f}  итог {m['total']*100:+7.1f}%")


# ------------------------------------------------------------------------- команды

def cmd_universe() -> None:
    """Символы, хоть раз попадавшие в топ-100 вселенную до сегодняшнего дня -> файл для
    докачки фандинга (старт — дата первого попадания минус 8 дней)."""
    p = load_panel(with_funding=False)
    first: dict[str, pd.Timestamp] = {}
    last: dict[str, pd.Timestamp] = {}
    for t in mondays(p, TRAIN[0], p.close.index.max()):
        uni, _ = universe(p, t, 100)
        for s in uni:
            first.setdefault(s, t)
            last[s] = t
    # окно членства ± запас: держим неделю (+1 день задержки) после последней ребалансировки
    todo = {s: [int((first[s] - 8 * DAY).timestamp() * 1000),
                int((last[s] + 10 * DAY).timestamp() * 1000)] for s in first}
    out = CACHE / "funding_todo.json"
    out.write_text(json.dumps(todo))
    print(f"символов во вселенной топ-100: {len(todo)} -> {out}")


def cmd_run() -> None:
    p = load_panel()
    print(f"символов со свечами: {p.close.shape[1]}, дней: {p.close.shape[0]}, "
          f"фандинг: {len(p.funding)} символов")
    weeks_tv = mondays(p, TRAIN[0], VALID[1])
    # заморозка в этих прогонах не участвует: недели строго до 2026-07-31
    assert all(t <= VALID[1] for t in weeks_tv)
    results = {}
    print("\n=== УЧЁБА 2021–2023 (выбор только здесь), net ===")
    for v in ALL:
        df = run_variant(p, v, weeks_tv)
        results[v] = df
        m = metrics(df, *TRAIN)
        gs = metrics(df, *TRAIN, col="gross")["sharpe"]
        print(f"{v.name:14s} {fmt(m)}  gross-Шарп {gs:+.2f}")
    chosen = {}
    for fam in ("A", "B"):
        best = max((v for v in ALL if v.family == fam),
                   key=lambda v: metrics(results[v], *TRAIN)["sharpe"])
        chosen[fam] = best
        print(f"\nВЫБОР семейства {fam} по учёбе: {best.name}")
    (CACHE / "chosen.json").write_text(json.dumps({f: v.name for f, v in chosen.items()}))

    print("\n=== ПРОВЕРКА выбранных и критерий ===")
    for v in chosen.values():
        df = results[v]
        mv, m1, m2 = metrics(df, *VALID), metrics(df, *V1), metrics(df, *V2)
        print(f"\n{v.name}")
        print(f"  учёба    {fmt(metrics(df, *TRAIN))}")
        print(f"  проверка {fmt(mv)}")
        print(f"  V1       {fmt(m1)}")
        print(f"  V2       {fmt(m2)}")
        c1 = mv["sharpe"] > 1.0
        c2 = m1["total"] > 0 and m2["total"] > 0
        nb = neighbors(v)
        nb_ok = []
        for u in nb:
            mu = metrics(results[u], *VALID)
            nb_ok.append(mu["total"] > 0)
            print(f"  сосед {u.name:14s} проверка {fmt(mu)}")
        c3 = all(nb_ok)
        # без двух главных вкладчиков на проверке
        weeks_v = [t for t in weeks_tv if t >= VALID[0]]
        tr = run_variant(p, v, weeks_v, track=True)
        contrib = tr.attrs["contrib"]
        top2 = sorted(contrib, key=contrib.get, reverse=True)[:2]
        ex = run_variant(p, v, weeks_v, exclude=frozenset(top2))
        mex = metrics(ex, *VALID)
        print(f"  главные вкладчики: {', '.join(f'{s} {contrib[s]*100:+.1f}%' for s in top2)}")
        print(f"  без них  {fmt(mex)}")
        top10 = sorted(contrib, key=contrib.get, reverse=True)[:10]
        ex10 = run_variant(p, v, weeks_v, exclude=frozenset(top10))
        print(f"  без топ-10 вкладчиков ({', '.join(top10)}): {fmt(metrics(ex10, *VALID))}")
        SLIP_MULT[0] = 3.0
        s3 = run_variant(p, v, weeks_tv)
        SLIP_MULT[0] = 1.0
        print(f"  проскальзывание ×3: учёба {fmt(metrics(s3, *TRAIN))}")
        print(f"                      проверка {fmt(metrics(s3, *VALID))}")
        c4 = mex["sharpe"] > 0.5 and mex["total"] > 0
        verdict = "КАНДИДАТ" if (c1 and c2 and c3 and c4) else "не кандидат"
        print(f"  критерий: Шарп>1 {c1} | обе половины>0 {c2} | соседи>0 {c3} | "
              f"без топ-2 {c4} -> {verdict}")
        print(f"  оборот Σ|Δw| в неделю (учёба+проверка): "
              f"{turnover_series(p, v, weeks_tv):.2f}")
        # устойчивость (не для выбора)
        for nn in (30, 100):
            dn = run_variant(p, v, weeks_tv, n=nn)
            print(f"  N={nn:3d} проверка {fmt(metrics(dn, *VALID))}")
        dd = run_variant(p, v, weeks_tv, delay=1)
        print(f"  задержка 1д проверка {fmt(metrics(dd, *VALID))}")
        for y in range(2021, 2027):
            lo = pd.Timestamp(f"{y}-01-01", tz="UTC")
            hi = min(pd.Timestamp(f"{y}-12-31", tz="UTC"), VALID[1])
            print(f"  {y} {fmt(metrics(df, lo, hi))}")
        fb = df[(df.index >= VALID[0]) & (df.index <= VALID[1])]
        print(f"  проверка: gross {fb['gross'].sum()*100:+.1f}%  фандинг "
              f"{fb['fund'].sum()*100:+.1f}%  издержки {-fb['cost'].sum()*100:+.1f}% (суммы)")

    print("\n=== ПРИЛОЖЕНИЕ: проверка всех 36 (только для прозрачности, не для выбора) ===")
    for v in ALL:
        print(f"{v.name:14s} {fmt(metrics(results[v], *VALID))}")


def cmd_frozen() -> None:
    """Одна оценка заморозки для уже выбранных вариантов (chosen.json)."""
    p = load_panel()
    chosen = json.loads((CACHE / "chosen.json").read_text())
    weeks = mondays(p, FROZEN_FROM, p.close.index.max())
    print(f"заморозка: недель {len(weeks)}, {weeks[0].date()} … {weeks[-1].date()}")
    for name in chosen.values():
        fam, L, s, K = name.split()
        v = Variant(fam, int(L[1:]), int(s[1:]), int(K[1:]))
        # история нужна для дрейфа весов: начинаем с первой недели заморозки
        df = run_variant(p, v, weeks)
        print(f"{v.name:14s} {fmt(metrics(df, FROZEN_FROM, p.close.index.max()))}")


def cmd_telegram() -> None:
    """Минимальный депозит подписчика для выбранных вариантов: по всем позициям проверки
    депозит, при котором ордер ≥ минимума биржи и ошибка округления по шагу количества
    ≤ 5% позиции. Спецификации инструментов — текущие (instruments-info), цены — на дату."""
    import urllib.request
    with urllib.request.urlopen("https://api.bybit.com/v5/market/instruments-info"
                                "?category=linear&limit=1000", timeout=30) as r:
        spec = {x["symbol"]: x["lotSizeFilter"] for x in json.load(r)["result"]["list"]}
    p = load_panel(with_funding=False)
    chosen = json.loads((CACHE / "chosen.json").read_text())
    weeks = mondays(p, VALID[0], VALID[1])
    for name in chosen.values():
        fam, L, s, K = name.split()
        v = Variant(fam, int(L[1:]), int(s[1:]), int(K[1:]))
        need = []
        for t in weeks:
            uni, _ = universe(p, t, 50)
            for sym, w in target_weights(p, v, t, uni).items():
                f = spec.get(sym)
                px = p.close.at[t, sym]
                if f is None or pd.isna(px):
                    continue
                step = float(f["qtyStep"]) * px
                mn = max(float(f.get("minNotionalValue") or 0), float(f["minOrderQty"]) * px)
                need.append(max(mn / abs(w), step / (0.05 * abs(w))))
        q = np.percentile(need, [50, 90, 99]) if need else [np.nan] * 3
        print(f"{v.name}: позиций {len(need)}; депозит для 5%-точности: медиана "
              f"{q[0]:,.0f} $, 90% {q[1]:,.0f} $, 99% {q[2]:,.0f} $")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["universe", "run", "frozen", "telegram"])
    ap.add_argument("--funding", choices=["mtm", "fixed"], default="mtm",
                    help="fixed — модель регистрации (номинал на момент t), mtm — исправленная")
    a = ap.parse_args()
    FUNDING_MTM[0] = a.funding == "mtm"
    mode = ("номинал на момент начисления (mtm)" if FUNDING_MTM[0]
            else "номинал на момент t (как в регистрации)")
    print(f"фандинг: {mode}")
    {"universe": cmd_universe, "run": cmd_run, "frozen": cmd_frozen,
     "telegram": cmd_telegram}[a.cmd]()


if __name__ == "__main__":
    main()
