"""Замер гейта `bias > 0.5` по зарегистрированному критерию
(docs/measurements/2026-10-04-bias-gate.md). Запуск:

  .venv/bin/python -m scripts.measure_bias_gate

Сигналы — data/cache5y/sig_bias (пересъём sig_parity с полем scores; паритет проверяется
здесь же). Живая конфигурация и издержки — как в scripts/backtest_monthly.py.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

from app.sim.portfolio import simulate_portfolio
from scripts.backtest_monthly import SYMS
from scripts.run_portfolio import load, parse_args

OOS_FROM = "2025-07-28"
FEE = 0.055
BASELINE = {"n": 5353, "total_r": 622.4, "dd": 50.3}     # запись 02.09.2026 (taker)
KEY = ("symbol", "tf", "dt", "side", "setup", "entry", "stop", "confidence")


def _args(from_dt: str | None = None, to_dt: str | None = None, sig_dir: str = "sig_bias"):
    argv = ["--symbols", SYMS, "--tfs", "1h,4h", "--cache", "data/cache5y",
            "--signals", f"data/cache5y/{sig_dir}", "--setups", "6226,LTF",
            "--min-stop-pct", "1.3", "--trail-activate", "1.5", "--trail-dist", "0.2",
            "--fee", str(FEE), "--slip", "0.02", "--funding", "0.125",
            "--entry-fill", "touch"]
    if from_dt:
        argv += ["--from-dt", from_dt]
    if to_dt:
        argv += ["--to-dt", to_dt]
    return parse_args(argv)


def _sim(bars, signals):
    return simulate_portfolio(
        bars, signals, max_open_per_side=6, cooldown_stops=3, cooldown_hours=6.0,
        dedup=True, fee_pct=FEE, slippage_pct=0.02, funding_bps_per_bar=0.125,
        trail_activate_r=1.5, trail_distance_r=0.2, entry_fill="touch")


def _bias(s: dict) -> float:
    return float((s.get("scores") or {}).get("bias", -1.0))


def parity_check() -> int:
    """Ноль расхождений sig_bias против эталонного sig_parity по ключевым полям."""
    diffs = 0
    for f in sorted(Path("data/cache5y/sig_parity").glob("*.json")):
        g = Path("data/cache5y/sig_bias") / f.name
        a = json.loads(f.read_text())
        b = json.loads(g.read_text()) if g.exists() else []
        ka = [tuple(x[k] for k in KEY) for x in a]
        kb = [tuple(x[k] for k in KEY) for x in b]
        if ka != kb:
            diffs += abs(len(ka) - len(kb)) + sum(1 for x, y in zip(ka, kb) if x != y)
            print(f"  ПАРИТЕТ: расхождение в {f.name}: {len(ka)} vs {len(kb)}")
    return diffs


def welch_z(a: list, b: list) -> float:
    if len(a) < 2 or len(b) < 2:
        return 0.0
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    return (ma - mb) / se if se else 0.0


def run_half(label: str, from_dt: str | None, to_dt: str | None) -> dict:
    bars, sigs = load(_args(from_dt, to_dt))
    base = _sim(bars, sigs)
    gated_sigs = [s for s in sigs if _bias(s) > 0.5]
    gated = _sim(bars, gated_sigs)
    out = {"label": label, "signals": len(sigs), "signals_gated": len(gated_sigs),
           "base": base, "gated": gated, "sigs": sigs}
    print(f"\n[{label}] сигналов {len(sigs)} -> {len(gated_sigs)} после гейта")
    for name, r in (("база  ", base), ("bias>.5", gated)):
        print(f"  {name}: n={r.trades:5d} exp={r.expectancy_r:+.3f}R ΣR={r.total_r:+8.1f} "
              f"PF={r.profit_factor:.2f} DD={r.max_drawdown_r:.1f}R WR={r.winrate*100:.1f}%")
    return out


def diagnostics(full: dict) -> tuple[float, list, list]:
    """Группы bias по сделкам БЕЙСЛАЙНА (какие сделки реально открылись)."""
    idx = {(s["symbol"], s["tf"], s["side"], s["setup"], str(s["dt"])): s for s in full["sigs"]}
    groups: dict = defaultdict(list)
    unmatched = 0
    for t in full["base"].detail:
        s = idx.get((t.symbol, t.tf, t.side, t.setup, str(t.signal_dt)))
        if s is None:
            unmatched += 1
            continue
        groups[_bias(s)].append(t.result_r)
    print(f"\nДиагностика по bias (сделки бейслайна, несопоставлено: {unmatched}):")
    hi, lo = [], []
    for b in sorted(groups):
        v = groups[b]
        print(f"  bias={b:4.2f}: n={len(v):5d} exp={sum(v)/len(v):+.3f}R ΣR={sum(v):+8.1f} "
              f"WR={100*sum(1 for x in v if x > 0)/len(v):.1f}%")
        (hi if b > 0.5 else lo).extend(v)
    z = welch_z(hi, lo)
    print(f"  bias>0.5: n={len(hi)} exp={sum(hi)/len(hi):+.3f}R | "
          f"bias<=0.5: n={len(lo)} exp={sum(lo)/len(lo):+.3f}R | Welch z={z:+.2f}")
    return z, hi, lo


def main() -> None:
    print("== паритет sig_bias против sig_parity ==")
    d = parity_check()
    print(f"  расхождений: {d}")
    if d:
        raise SystemExit("паритет сломан — замер недействителен")

    full = run_half("ВСЯ ИСТОРИЯ", None, None)
    b = full["base"]
    ok = (b.trades == BASELINE["n"] and abs(b.total_r - BASELINE["total_r"]) < 0.5
          and abs(b.max_drawdown_r - BASELINE["dd"]) < 0.1)
    print(f"  бейслайн {'ВОСПРОИЗВЕДЁН' if ok else 'НЕ СОВПАЛ с записью'}: "
          f"n={b.trades} ΣR={b.total_r:+.1f} DD={b.max_drawdown_r:.1f} "
          f"(запись {BASELINE})")
    print("  by_setup бейслайна:", {k: (v.get('trades') if isinstance(v, dict) else v)
                                    for k, v in b.by_setup.items()})
    if not ok:
        raise SystemExit("бейслайн не воспроизведён — замер недействителен")

    train = run_half("TRAIN (до 2025-07-28)", None, OOS_FROM)
    oos = run_half("OOS (с 2025-07-28)", OOS_FROM, None)
    z, _, _ = diagnostics(full)

    print("\n== ЗАРЕГИСТРИРОВАННЫЙ КРИТЕРИЙ ==")
    verdicts = {}
    for h in (train, oos):
        b_, g_ = h["base"], h["gated"]
        c1 = g_.expectancy_r >= b_.expectancy_r + 0.05
        c2 = g_.total_r >= 0.85 * b_.total_r
        c3 = g_.max_drawdown_r <= b_.max_drawdown_r
        verdicts[h["label"]] = (c1, c2, c3)
        print(f"  {h['label']}: (1) exp {g_.expectancy_r:+.3f} vs {b_.expectancy_r:+.3f}+0.05 "
              f"-> {c1} | (2) ΣR {g_.total_r:+.1f} vs 0.85×{b_.total_r:+.1f}="
              f"{0.85*b_.total_r:+.1f} -> {c2} | (3) DD {g_.max_drawdown_r:.1f} vs "
              f"{b_.max_drawdown_r:.1f} -> {c3}")
    c4 = z >= 2.0
    print(f"  (4) Welch z {z:+.2f} >= 2.0 -> {c4}")
    all_ok = all(all(v) for v in verdicts.values()) and c4
    c1_both = all(v[0] for v in verdicts.values())
    if all_ok:
        verdict = "ПРИНЯТЬ: все 4 пункта выполнены на обеих половинах"
    elif c1_both and c4:
        verdict = ("КАНДИДАТ НА ВЕС РИСКА (эдж есть, но гейт срезает объём/просадку); "
                   "гейт не включать")
    else:
        verdict = "ОТВЕРГНУТО"
    print(f"\nВЕРДИКТ: {verdict}")


if __name__ == "__main__":
    main()
