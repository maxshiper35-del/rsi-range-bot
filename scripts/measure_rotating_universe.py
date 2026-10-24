"""Замер: стратегия на ротационном вотчлисте (docs/measurements/2026-10-24-rotating-universe.md).

Шаги (каждый кэширует результат в data/cache_univ, повторный запуск докачивает недостающее):

  .venv/bin/python -m scripts.measure_rotating_universe instruments
  .venv/bin/python -m scripts.measure_rotating_universe turnover
  .venv/bin/python -m scripts.measure_rotating_universe candidates   # кто бывал в топ-15
  .venv/bin/python -m scripts.measure_rotating_universe bars         # 1h+4h кандидатов
  # снимок сигналов — dump_signals --prod --cache data/cache_univ --out data/cache_univ/sig
  .venv/bin/python -m scripts.measure_rotating_universe run

Бары и обороты — Bybit v5 kline напрямую: делистнутых символов нет в рынках ccxt, а без
них вселенная страдает ошибкой выжившего.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

from scripts.dump_history import cache_name

OUT = Path("data/cache_univ")
FROM = pd.Timestamp("2024-04-01", tz="UTC")       # 6 мес. гейта + окно движка до старта
START = pd.Timestamp("2024-10-24", tz="UTC")      # начало торгового периода
END = pd.Timestamp("2026-10-24", tz="UTC")
SPLIT = pd.Timestamp("2025-10-24", tz="UTC")
TOP_N, MIN_TURNOVER = 15, 10_000_000.0            # runtime.watchlist_size, top_liquid_symbols
_INTERVAL = {"1h": ("60", 3_600_000), "4h": ("240", 14_400_000), "1d": ("D", 86_400_000)}


def _ex():
    from app.data.market_data import _exchange
    return _exchange("bybit")


def ccxt_symbol(inst: dict) -> str:
    return f"{inst['baseCoin']}/USDT:USDT"


def fetch_instruments() -> list[dict]:
    ex, out = _ex(), []
    for status in ("Trading", "Closed"):
        cur = None
        while True:
            p = {"category": "linear", "limit": 1000, "status": status}
            if cur:
                p["cursor"] = cur
            r = ex.publicGetV5MarketInstrumentsInfo(p)["result"]
            out += [x for x in r["list"] if x.get("settleCoin") == "USDT"
                    and x.get("contractType") == "LinearPerpetual"]
            cur = r.get("nextPageCursor")
            if not cur or not r["list"]:
                break
    return out


def fetch_klines(ex, bybit_symbol: str, tf: str, since_ms: int, end_ms: int) -> pd.DataFrame:
    """Свечи [since, end] назад страницами по 1000 (делистнутым нужен end ≤ deliveryTime).
    Последняя (формирующаяся) свеча отбрасывается — как fetch_history."""
    interval, step = _INTERVAL[tf]
    rows: dict[int, list] = {}
    end = end_ms
    while end >= since_ms:
        for attempt in range(5):
            try:
                r = ex.publicGetV5MarketKline({"category": "linear", "symbol": bybit_symbol,
                                               "interval": interval, "limit": 1000,
                                               "end": end})
                break
            except Exception as e:                     # noqa: BLE001 — сеть: повтор с паузой
                if attempt == 4:
                    raise
                print(f"  {bybit_symbol} {tf}: {e} — повтор", file=sys.stderr)
                time.sleep(2 + attempt * 3)
        lst = r["result"]["list"]
        if not lst:
            break
        for k in lst:
            rows[int(k[0])] = k
        oldest = min(int(k[0]) for k in lst)
        if oldest <= since_ms or len(lst) < 1000:
            break
        end = oldest - 1
    if not rows:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume",
                                     "turnover", "dt"])
    df = pd.DataFrame([rows[t] for t in sorted(rows) if t >= since_ms],
                      columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
    df = df.astype({"ts": "int64", "open": float, "high": float, "low": float,
                    "close": float, "volume": float, "turnover": float})
    now_ms = int(time.time() * 1000)
    df = df[df["ts"] + step <= min(end_ms + 1, now_ms)]      # только закрытые свечи
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.reset_index(drop=True)


def _end_ms(inst: dict) -> int:
    now = int(time.time() * 1000)
    d = int(inst.get("deliveryTime") or 0)
    return min(now, d) if d > 0 else now


def cmd_instruments(_a) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    inst = fetch_instruments()
    (OUT / "instruments.json").write_text(json.dumps(inst))
    names: dict[str, list] = {}
    for x in inst:
        names.setdefault(ccxt_symbol(x), []).append(x["symbol"])
    dup = {k: v for k, v in names.items() if len(v) > 1}
    print(f"инструментов {len(inst)} (Trading {sum(x['status'] == 'Trading' for x in inst)}, "
          f"Closed {sum(x['status'] == 'Closed' for x in inst)}); коллизий имён {len(dup)}: "
          f"{list(dup.items())[:10]}")


def _instruments() -> list[dict]:
    return json.loads((OUT / "instruments.json").read_text())


def cmd_turnover(_a) -> None:
    """Дневной оборот всех инструментов, активных после FROM -> turnover.csv."""
    ex, path = _ex(), OUT / "turnover.csv"
    done = set(pd.read_csv(path)["bybit"].unique()) if path.exists() else set()
    since = int(FROM.timestamp() * 1000)
    todo = [x for x in _instruments() if x["symbol"] not in done and _end_ms(x) >= since]
    print(f"оборот: {len(todo)} к загрузке ({len(done)} уже есть)")
    for i, x in enumerate(todo, 1):
        df = fetch_klines(ex, x["symbol"], "1d", since, _end_ms(x))
        df = df.assign(bybit=x["symbol"], symbol=ccxt_symbol(x), status=x["status"])
        if df.empty:      # пустой тоже пишем строкой-заглушкой, чтобы не качать снова
            df = pd.DataFrame([{"bybit": x["symbol"], "symbol": ccxt_symbol(x),
                                "status": x["status"], "ts": None, "dt": None,
                                "close": None, "turnover": None}])
        df[["bybit", "symbol", "status", "ts", "dt", "close", "turnover"]].to_csv(
            path, mode="a", header=not path.exists(), index=False)
        if i % 50 == 0:
            print(f"  {i}/{len(todo)}", flush=True)


def daily_top(turnover: pd.DataFrame) -> dict:
    """{день D: [символы топ-N по обороту дня D−1, ≥ MIN_TURNOVER]} на [START, END)."""
    t = turnover.dropna(subset=["ts"]).copy()
    t["day"] = pd.to_datetime(t["ts"], unit="ms", utc=True).dt.normalize()
    t = t[t["turnover"] >= MIN_TURNOVER]
    # два bybit-id на одно имя (переименование/релистинг) — берём больший оборот за день
    t = t.sort_values("turnover", ascending=False).drop_duplicates(["day", "symbol"])
    out = {}
    for day, g in t.groupby("day"):
        d = day + pd.Timedelta(days=1)
        if START <= d < END:
            out[d] = list(g.sort_values("turnover", ascending=False)["symbol"][:TOP_N])
    return out


def cmd_candidates(_a) -> None:
    top = daily_top(pd.read_csv(OUT / "turnover.csv"))
    days: dict[str, int] = {}
    for syms in top.values():
        for s in syms:
            days[s] = days.get(s, 0) + 1
    (OUT / "candidates.json").write_text(json.dumps(dict(sorted(days.items(),
                                                               key=lambda kv: -kv[1]))))
    print(f"дней {len(top)}, символов в топ-{TOP_N} хотя бы раз: {len(days)}")
    print(", ".join(f"{s.split('/')[0]}:{n}" for s, n in sorted(days.items(),
                                                                key=lambda kv: -kv[1])[:60]))


def cmd_bars(a) -> None:
    """1h+4h кандидатов, потоками (запросы упираются в задержку сети, не в лимит Bybit)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    cands = json.loads((OUT / "candidates.json").read_text())
    by_name: dict[str, list] = {}
    for x in _instruments():
        by_name.setdefault(ccxt_symbol(x), []).append(x)
    since = int(FROM.timestamp() * 1000)
    syms = [s for s in cands if not a.only or s in a.only.split(",")]
    local = threading.local()
    done = [0]

    def one(sym: str) -> None:
        if not hasattr(local, "ex"):
            # ccxt не потокобезопасен, а _exchange — общий кэш: свой инстанс на поток,
            # rateLimit 200мс у каждого (≤5 запр/с на поток; лимит IP Bybit ~120/с)
            import ccxt
            local.ex = ccxt.bybit({"enableRateLimit": True, "rateLimit": 200,
                                   "timeout": 20000})
        for tf in ("1h", "4h"):
            path = OUT / cache_name(sym, tf)
            if path.exists():
                continue
            parts = [fetch_klines(local.ex, x["symbol"], tf, since, _end_ms(x))
                     for x in by_name[sym]]
            df = pd.concat(parts).sort_values("ts").drop_duplicates("ts", keep="last")
            tmp = path.with_suffix(".tmp")
            df[["ts", "open", "high", "low", "close", "volume", "dt"]].to_csv(tmp, index=False)
            tmp.rename(path)
        done[0] += 1
        if done[0] % 20 == 0:
            print(f"  {done[0]}/{len(syms)}", flush=True)

    with ThreadPoolExecutor(a.threads) as pool:
        list(pool.map(one, syms))


def needed_masks(sym: str, days: list) -> dict:
    """Бары, сигналы которых замер реально читает: 1h — окно гейта (6 мес. до дня кандидатства,
    если история достаточна) и сами дни в топе; 4h — дни в топе (±сутки с запасом)."""
    import numpy as np
    out = {}
    for tf in ("1h", "4h"):
        dt = pd.read_csv(OUT / cache_name(sym, tf), usecols=["dt"], parse_dates=["dt"])["dt"]
        mask = np.zeros(len(dt), dtype=bool)
        for d in days:
            a = int(dt.searchsorted(d - pd.Timedelta(days=1)))
            b = int(dt.searchsorted(d + pd.Timedelta(days=1)))
            mask[a:b] = True
            if tf == "1h":
                k = int(dt.searchsorted(d))
                if min(k, GATE_BARS) >= GATE_BARS * 0.9:
                    mask[max(0, k - GATE_BARS):k] = True
        out[tf] = mask
    return out


def _dump_one(job: tuple) -> tuple:
    """Снимок --prod одного символа по маскам (процесс пула; конфиг прода — как dump_signals)."""
    sym, days = job
    from scripts.dump_signals import _prod_config, scan_signals
    args = argparse.Namespace(set=[], tfs="1h,4h", window=200)
    cfg, htf_map = _prod_config(args)
    masks = needed_masks(sym, days)
    n = {}
    for tf in ("1h", "4h"):
        df = pd.read_csv(OUT / cache_name(sym, tf), parse_dates=["dt"])
        htf_tf = htf_map.get(tf)
        htf = pd.read_csv(OUT / cache_name(sym, htf_tf), parse_dates=["dt"]) if htf_tf else None
        sigs = scan_signals(df, sym, tf, cfg, args.window, htf_tf, htf_bars=htf,
                            only=masks[tf]) if len(df) else []
        dest = SIG / f"{cache_name(sym, tf)[:-4]}.json"
        tmp = dest.with_suffix(".tmp")
        tmp.write_text(json.dumps(sigs, ensure_ascii=False))
        tmp.rename(dest)
        n[tf] = (len(sigs), int(masks[tf].sum()))
    return sym, n


def cmd_dump(a) -> None:
    """Снимок по маскам для символов без готового снимка; список частичных — sig/partial.json."""
    from multiprocessing import Pool
    top = daily_top(pd.read_csv(OUT / "turnover.csv"))
    days: dict = {}
    for d, syms in top.items():
        for s in syms:
            days.setdefault(s, []).append(d)
    SIG.mkdir(parents=True, exist_ok=True)
    part_path = SIG / "partial.json"
    partial = set(json.loads(part_path.read_text())) if part_path.exists() else set()
    done = {s for s in days
            if all((SIG / f"{cache_name(s, tf)[:-4]}.json").exists() for tf in ("1h", "4h"))}
    jobs = [(s, ds) for s, ds in sorted(days.items(), key=lambda kv: -len(kv[1]))
            if s not in done and (not a.only or s in a.only.split(","))]
    print(f"к снимку по маскам: {len(jobs)} (готово {len(done)})", flush=True)
    t0 = time.time()
    with Pool(a.workers) as pool:
        for i, (sym, n) in enumerate(pool.imap_unordered(_dump_one, jobs), 1):
            partial.add(sym)
            part_path.write_text(json.dumps(sorted(partial)))
            if i % 10 == 0 or i == len(jobs):
                print(f"  {i}/{len(jobs)} за {(time.time() - t0) / 60:.0f} мин "
                      f"(последний {sym}: {n})", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("instruments")
    sub.add_parser("turnover")
    sub.add_parser("candidates")
    b = sub.add_parser("bars")
    b.add_argument("--only", default=None)
    b.add_argument("--threads", type=int, default=6)
    dm = sub.add_parser("dump")
    dm.add_argument("--only", default=None)
    dm.add_argument("--workers", type=int, default=6)
    sub.add_parser("run")
    sub.add_parser("parity")
    a = ap.parse_args()
    {"instruments": cmd_instruments, "turnover": cmd_turnover, "candidates": cmd_candidates,
     "bars": cmd_bars, "dump": cmd_dump, "run": lambda _a: run(),
     "parity": lambda _a: parity()}[a.cmd](a)


SIG = OUT / "sig"
GATE_BARS = int(6 * 30.44 * 86400 / 3600)          # _watchlist_gate: 6 мес. 1h
SHADOW = {("LTF-BB", "4h")}                         # runtime.shadow_setups (прод с 05.10)


def _bars(sym: str, tf: str, cache: Path = OUT) -> pd.DataFrame | None:
    path = cache / cache_name(sym, tf)
    if not path.exists():
        return None
    df = pd.read_csv(path, parse_dates=["dt"])
    return df if len(df) else None


def _signals(sym: str, tf: str, sig_dir: Path = SIG) -> list[dict]:
    path = sig_dir / f"{cache_name(sym, tf)[:-4]}.json"
    return json.loads(path.read_text()) if path.exists() else []


def _decision(sig: dict) -> pd.Timestamp:
    from app.brain.smc_setups import SMCEngine
    dt = pd.Timestamp(sig["dt"])
    dt = dt.tz_localize("UTC") if dt.tzinfo is None else dt.tz_convert("UTC")
    return dt + pd.Timedelta(minutes=SMCEngine.TF_MINUTES.get(sig["tf"], 60))


class Gate:
    """Реплика _watchlist_gate по снимку 1h-сигналов: run_backtest-логика (по одной позиции,
    выход по тейку _simulate_open, M0) за 6 мес. до дня D, только закрытое к D."""

    def __init__(self) -> None:
        self._df: dict = {}
        self._sigs: dict = {}
        self.log: list = []

    def __call__(self, sym: str, day: pd.Timestamp) -> bool:
        from app.core.signal_engine import Side
        from app.delivery.bot import watchlist_gate_decision
        from app.sim.backtest import _max_wait_bars, _simulate_open
        max_wait = _max_wait_bars("1h")                  # как run_backtest
        if sym not in self._df:
            self._df[sym] = _bars(sym, "1h")
            self._sigs[sym] = _signals(sym, "1h")
        df = self._df[sym]
        if df is None:
            self.log.append((day, sym, False, 0.0, 0, "нет баров"))
            return False
        k = int(df["dt"].searchsorted(day))              # бары с dt < D (закрытые к D)
        lo = max(0, k - GATE_BARS)
        if k - lo < GATE_BARS * 0.9:                     # короткая история = новый листинг
            self.log.append((day, sym, False, 0.0, 0, f"история {k - lo}"))
            return False
        cut = df.iloc[:k]
        rs, last_exit = [], -1
        for s in self._sigs[sym]:
            i = s["bar_index"]
            if i < lo + 20 or i >= k or i <= last_exit:   # min_bars=20 у run_backtest
                continue
            fill = next((j for j in range(i + 1, min(k, i + 1 + max_wait))
                         if cut["low"].iat[j] <= s["entry"] <= cut["high"].iat[j]), None)
            if fill is None:
                continue
            res = _simulate_open(cut, Side.LONG if s["side"] == "LONG" else Side.SHORT,
                                 s["entry"], s["stop"],
                                 [{"level": s["tp"], "r": s["rr"], "frac": 1.0, "done": False}],
                                 fill, None, 0.055, 0.02, 0.0)
            if res is None:
                last_exit = k
                continue
            rs.append(res[1])
            last_exit = res[4]
        exp = sum(rs) / len(rs) if rs else 0.0
        ok = watchlist_gate_decision(exp, len(rs))
        self.log.append((day, sym, ok, round(exp, 3), len(rs), ""))
        return ok


def rotate(top: dict, gate, seed: tuple = ()) -> dict:
    """{день: состав} по правилам plan_watchlist_rotation; gate=None — топ без гейта.
    seed — стартовый список (прод начинал авто-ротацию с ручных 9 монет: кто из них в топе,
    гейт не проходит никогда)."""
    from app.delivery.bot import plan_watchlist_rotation
    current: list = list(seed)
    out = {}
    for day in sorted(top):
        res = {s: True for s in top[day]} if gate is None else {
            s: gate(s, day) for s in top[day] if s not in current}
        current, _added, _removed = plan_watchlist_rotation(current, top[day], res)
        out[day] = list(current)
    return out


def members_signals(membership: dict) -> list[dict]:
    """Сигналы символов, бывших в списке на момент решения; LTF-BB@4h — в тени."""
    syms = sorted({s for v in membership.values() for s in v})
    days = {d: set(v) for d, v in membership.items()}
    out = []
    for sym in syms:
        for tf in ("1h", "4h"):
            for s in _signals(sym, tf):
                if (s["setup"], tf) in SHADOW:
                    continue
                d = _decision(s)
                if not (START <= d < END):
                    continue
                if sym in days.get(d.normalize(), ()):
                    out.append(s)
    return out


def simulate(signals: list[dict], slip: float, cache: Path = OUT):
    """Портфель с правилами прода (как backtest_monthly), живой судья на 1h."""
    from app.sim.portfolio import simulate_portfolio
    syms = sorted({s["symbol"] for s in signals})
    bars = {}
    for sym in syms:
        for tf in ("1h", "4h"):
            df = _bars(sym, tf, cache)
            if df is not None:
                bars[(sym, tf)] = df
    h1 = {sym: bars[(sym, "1h")] for sym in syms if (sym, "1h") in bars}
    signals = [s for s in signals if (s["symbol"], s["tf"]) in bars and s["symbol"] in h1]
    return simulate_portfolio(
        bars, signals, max_open_per_side=6, cooldown_stops=3, cooldown_hours=6.0,
        dedup=True, fee_pct=0.055, slippage_pct=slip, funding_bps_per_bar=0.125,
        trail_activate_r=1.5, trail_distance_r=0.2, entry_fill="touch",
        live_judge_bars=h1, tp_exit=False, max_hold_hours=168.0)


def week_ci(trades, n_boot: int = 5000, seed: int = 0) -> tuple:
    """95% ДИ exp — блочный бутстрап по неделям закрытия (сделки недели коррелированы)."""
    import numpy as np
    wk: dict = {}
    for t in trades:
        key = pd.Timestamp(t.exit_dt).to_period("W")
        s, n = wk.get(key, (0.0, 0))
        wk[key] = (s + t.result_r, n + 1)
    arr = np.array(list(wk.values()))
    if not len(arr):
        return (None, None)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(arr), size=(n_boot, len(arr)))
    exps = arr[idx, 0].sum(1) / arr[idx, 1].sum(1)
    return (round(float(np.percentile(exps, 2.5)), 3), round(float(np.percentile(exps, 97.5)), 3))


def _utc(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _row(name: str, trades) -> dict:
    def agg(ts):
        rs = [t.result_r for t in ts]
        peak = cum = dd = 0.0
        for r in rs:
            cum += r
            peak = max(peak, cum)
            dd = max(dd, peak - cum)
        return {"n": len(rs), "sum": round(sum(rs), 1),
                "exp": round(sum(rs) / len(rs), 3) if rs else None, "dd": round(dd, 1)}
    ts = sorted(trades, key=lambda t: t.exit_dt)
    y1 = [t for t in ts if _utc(t.signal_dt) < SPLIT]
    y2 = [t for t in ts if _utc(t.signal_dt) >= SPLIT]
    return {"name": name, "all": agg(ts), "y1": agg(y1), "y2": agg(y2), "ci": week_ci(ts)}


def parity() -> None:
    """Сверка инструмента с продом (до вывода): (1) гейт-реплика против живого лога
    `watchlist-гейт` с 07.10 (гейт входа включён; 20–24.10 min_stop был 2.0 — исключены);
    (2) символы живых сигналов журнала 10.08–24.10 против реконструкции A."""
    import re

    from dotenv import load_dotenv
    load_dotenv(".env")
    from app import db
    pat = re.compile(r"^(\S+) INFO bot watchlist-гейт (\S+): exp=([+-][\d.]+)R, "
                     r"сделок=(\d+) -> (\S+)")
    live = {}
    for line in Path("logs/bot.log").read_text(errors="ignore").splitlines():
        m = pat.match(line)
        if not m:
            continue
        ts = pd.Timestamp(m.group(1), tz="UTC")
        if not (pd.Timestamp("2026-10-07", tz="UTC") <= ts < pd.Timestamp("2026-10-20", tz="UTC")):
            continue
        live[(ts.floor("h"), m.group(2))] = (
            m.group(5) == "допущен", float(m.group(3)), int(m.group(4)))
    gate = Gate()
    agree = 0
    live = {k: v for k, v in live.items()
            if (SIG / f"{cache_name(k[1], '1h')[:-4]}.json").exists()}
    for (day, sym), (ok, exp, n) in sorted(live.items()):
        mine = gate(sym, day)
        _d, _s, _ok, mexp, mn, note = gate.log[-1]
        agree += mine == ok
        print(f"  {day.date()} {sym:22s} прод {'+' if ok else '-'} {exp:+.3f}/{n:3d} | "
              f"реплика {'+' if mine else '-'} {mexp:+.3f}/{mn:3d} {note}")
    print(f"гейт: совпало {agree}/{len(live)}")
    if not (OUT / "membership_a.json").exists():
        return
    mem = json.loads((OUT / "membership_a.json").read_text())
    rows = db.query("SELECT symbol, timeframe, created_at FROM signals WHERE "
                    "status NOT IN ('candidate','rejected') AND created_at >= '2026-08-10'")
    hit = 0
    miss: dict = {}
    for r in rows:
        d = str((pd.Timestamp(r["created_at"]) + pd.Timedelta(
            hours=1 if r["timeframe"] == "1h" else 4)).date())
        if r["symbol"] in mem.get(d, []):
            hit += 1
        else:
            miss[r["symbol"]] = miss.get(r["symbol"], 0) + 1
    print(f"живые сигналы 10.08–24.10: символ в реконструкции A — {hit}/{len(rows)}; "
          f"мимо: {sorted(miss.items(), key=lambda kv: -kv[1])[:20]}")


def run() -> None:
    top = daily_top(pd.read_csv(OUT / "turnover.csv"))
    gate = Gate()
    mem_a = rotate(top, gate)
    mem_b = rotate(top, None)
    nine = [s.strip() for s in ("BTC,ETH,SOL,XRP,1000PEPE,NEAR,ONDO,HYPE,ZEC").split(",")]
    mem_a2 = rotate(top, gate, seed=tuple(f"{b}/USDT:USDT" for b in nine))
    (OUT / "membership_a2.json").write_text(json.dumps(
        {str(d.date()): v for d, v in mem_a2.items()}))
    pd.DataFrame(gate.log, columns=["day", "symbol", "ok", "exp", "n", "note"]).to_csv(
        OUT / "gate_log.csv", index=False)
    (OUT / "membership_a.json").write_text(json.dumps(
        {str(d.date()): v for d, v in mem_a.items()}))
    sizes = [len(v) for v in mem_a.values()]
    print(f"A: дней {len(mem_a)}, средний размер списка {sum(sizes) / len(sizes):.1f}, "
          f"символов за период {len({s for v in mem_a.values() for s in v})}; "
          f"гейт: вызовов {len(gate.log)}, допусков {sum(1 for x in gate.log if x[2])}")
    sig_a, sig_b = members_signals(mem_a), members_signals(mem_b)
    sig_a2 = members_signals(mem_a2)
    # C — снимок этого замера: на отрезке эталонного кэша (до 28.07.2026) совпадает с
    # sig_prod_late_entry сигнал в сигнал (15601/15601), а дальше доходит до конца периода
    sig_c = []
    for b in nine:
        for tf in ("1h", "4h"):
            for s in _signals(f"{b}/USDT:USDT", tf):
                if (s["setup"], tf) not in SHADOW and START <= _decision(s) < END:
                    sig_c.append(s)
    jobs = [(f"{name} {model}", model, slip, sigs)
            for name, sigs in (("A ротация+гейт", sig_a), ("A' старт-9", sig_a2),
                               ("B топ-15 без гейта", sig_b), ("C 9 монет", sig_c))
            for model, slip in (("M0", 0.02), ("M2'", 0.15), ("M2", 0.25))]
    from multiprocessing import Pool
    with Pool(6) as pool:
        rows = pool.map(_sim_job, jobs)
    for r in rows:
        a, y1, y2 = r["all"], r["y1"], r["y2"]
        print(f"{r['name']:24s} n={a['n']:5d} Σ={a['sum']:+7.1f} exp={a['exp']:+.3f} "
              f"DD={a['dd']:6.1f} ДИ95={r['ci']} | Y1 {y1['n']} {y1['sum']:+.1f} "
              f"{y1['exp']:+.3f} | Y2 {y2['n']} {y2['sum']:+.1f} {y2['exp']:+.3f} "
              f"{r['by_setup']} {r['by_tf']}", flush=True)
    (OUT / "result.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))


def _sim_job(job: tuple) -> dict:
    """Один прогон (процесс пула): строка итогов + сделки в CSV для разборов."""
    name, model, slip, sigs = job
    rep = simulate(sigs, slip)
    r = _row(name, rep.detail)
    r["by_setup"] = {k: v["trades"] for k, v in rep.by_setup.items()}
    r["by_tf"] = {tf: sum(1 for t in rep.detail if t.tf == tf) for tf in ("1h", "4h")}
    r["signals"] = len(sigs)
    tag = (name.split()[0] + "_" + model).replace("'", "p")
    pd.DataFrame([{"symbol": t.symbol, "tf": t.tf, "setup": t.setup, "side": t.side,
                   "signal_dt": t.signal_dt, "exit_dt": t.exit_dt, "outcome": t.outcome,
                   "r": t.result_r} for t in rep.detail]).to_csv(
        OUT / f"trades_{tag}.csv", index=False)
    return r


if __name__ == "__main__":
    main()
