"""ПОСТ-ФАКТУМ диагностика замера R1 (docs/measurements/2026-10-30-order-refresh.md),
в вердикт НЕ входит: цепочки обновлений заявки.

  DATA_ROOT=/путь/к/data .venv/bin/python -m scripts.measure_order_refresh_chains

Зарегистрированная диагностика R1 считала «снятыми» и промежуточные заявки цепочки
(A → B → C: снимаются A и B), поэтому не разделяла два механизма. Здесь: корень цепочки —
исходная заявка, снятая первой; финал — последняя заявка цепочки (сделка или истечение = 0).
Прямой эффект «свежих уровней» = Σ(финалы) − Σ(корни, если бы их оставили); остальной
прирост — эффект портфеля (короче блокировка дедупом → больше сделок).
Цикл — тот же, что scripts.research_portfolio.simulate(refresh=True), плюс учёт корней;
ΣR по половинам обязан совпасть с R1.
"""
from __future__ import annotations

from multiprocessing import Pool

import pandas as pd

import app.sim.portfolio as _pf
import scripts.research_judge as rj
import scripts.research_portfolio as rp
from app.brain.smc_setups import SMCEngine

rj.rebind_data()

R1_SUM = {("5y", "H1"): 196.8, ("5y", "H2"): 54.9, ("rot", "H1"): 22.0, ("rot", "H2"): 247.8}


def simulate_chains(bars: dict, signals: list) -> tuple[list, list]:
    live = {sym: df for (sym, tf), df in bars.items() if tf == "1h"}
    live_idx = {sym: pd.DatetimeIndex(pd.to_datetime(df["dt"], utc=True))
                for sym, df in live.items()}
    events = sorted(signals, key=lambda s: (str(s["dt"]), s["symbol"], s["tf"]))
    active: list = []
    closed: list = []
    trades: list = []
    roots: dict = {}

    def side_counts(now: pd.Timestamp) -> dict:
        out = {"LONG": 0, "SHORT": 0}
        for a in active:
            if a["signal_dt"] <= now < a["closed_dt"]:
                out[a["side"]] += 1
        return out

    def recent_stops(side: str, now: pd.Timestamp) -> int:
        cutoff = now - pd.Timedelta(hours=rp.COOL_HOURS)
        return sum(1 for c in closed if c["side"] == side and c["outcome"].endswith("stop")
                   and cutoff <= c["closed_dt"] <= now)

    for sig in events:
        now = pd.Timestamp(sig["dt"])
        dup = next((a for a in active
                    if a["symbol"] == sig["symbol"] and a["side"] == sig["side"]
                    and a["tf"] == sig["tf"] and a["setup"] == sig["setup"]
                    and a["signal_dt"] <= now < a["closed_dt"]), None)
        root = None
        if dup is not None:
            decision = now + pd.Timedelta(minutes=SMCEngine.TF_MINUTES.get(sig["tf"], 60))
            pending = dup["outcome"] == "expired" or dup["fill_dt"] >= decision
            differs = sig["entry"] != dup["entry"] or sig["stop"] != dup["stop"]
            if not (pending and differs and recent_stops(sig["side"], now) < rp.COOL_STOPS):
                continue
            if dup["outcome"] != "expired":
                closed[:] = [c for c in closed if c is not dup]
                trades[:] = [t for t in trades if t is not dup["trade"]]
            root = dup.get("root") or dup
            if root is dup:
                kept = dup["result_r"] if dup["outcome"] != "expired" else 0.0
                roots[id(root)] = {"dec": now + rj.HOUR, "root_r": kept, "final": None}
            dup["closed_dt"] = now
            dup["outcome"] = "cancelled"
        if (side_counts(now)[sig["side"]] >= rp.CAP
                or recent_stops(sig["side"], now) >= rp.COOL_STOPS):
            continue
        lres = _pf._live_outcome(sig, live[sig["symbol"]], live_idx[sig["symbol"]],
                                 **rp.JUDGE_KW)
        if lres is None:
            continue
        kind, r_net, fill_dt, closed_dt = lres
        rec = {**sig, "signal_dt": now, "closed_dt": closed_dt, "outcome": kind,
               "result_r": r_net, "fill_dt": fill_dt, "root": root}
        active.append(rec)
        if kind != "expired":
            rec["trade"] = (rj.utc(now) + rj.HOUR, rj.utc(closed_dt), round(r_net, 4))
            closed.append(rec)
            trades.append(rec["trade"])
        if root is not None:
            roots[id(root)]["final"] = rec
    chains = []
    for v in roots.values():
        f = v["final"]
        done = f is not None and f["outcome"] not in ("expired", "cancelled")
        chains.append((v["dec"], None, 0.0, None, round(v["root_r"], 4),
                       round(f["result_r"], 4) if done else 0.0))
    return chains, trades


def _job(data: str) -> tuple:
    bars, sigs = rj.load(data)
    rj.install()
    chains, trades = simulate_chains(bars, sigs)
    return data, chains, trades


def main() -> None:
    with Pool(2) as pool:
        res = pool.map(_job, ["5y", "rot"])
    for data, chains, trades in res:
        hc, ht = rj.halves(chains, data), rj.halves(sorted(trades), data)
        for half in ("H1", "H2"):
            s = sum(x[2] for x in ht[half])
            root_s = sum(x[4] for x in hc[half])
            fin_s = sum(x[5] for x in hc[half])
            direct = fin_s - root_s
            total = s - rj.PARITY[(data, half)][1]
            print(f"{data:3s} {half}: ΣR {s:+.1f} (R1 {R1_SUM[(data, half)]:+.1f}); цепочек "
                  f"{len(hc[half])}; корни Σ {root_s:+.1f} → финалы Σ {fin_s:+.1f}: прямой "
                  f"эффект {direct:+.1f}R; весь прирост {total:+.1f}R → портфель "
                  f"{total - direct:+.1f}R")


if __name__ == "__main__":
    main()
