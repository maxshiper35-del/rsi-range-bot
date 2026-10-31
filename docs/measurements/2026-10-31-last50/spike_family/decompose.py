"""Разложение ΔE по составу (из run.pkl, без новых прогонов; только rot H1 / rot H2′)."""
import pickle, sys
import numpy as np
import measure_spike_family as m

d = pickle.load(open("run.pkl", "rb"))
base = d["base"]["rot"]
for v in ("T10", "C48", "P30L", "T5", "T20", "C24", "C72", "P20L", "P50L"):
    rows = d["rows"][("rot", v)]
    drop = set(d["drops"][("rot", v)])
    for h in m.HALVES_ROT:
        b = m.in_half(base, h)
        x = m.in_half(rows, h)
        kb = {r[1]: r for r in b}
        kx = {r[1]: r for r in x}
        common = [k for k in kx if k in kb]
        same_res = sum(1 for k in common if abs(kx[k][4] - kb[k][4]) < 1e-9)
        new = [kx[k] for k in kx if k not in kb]
        gone = [kb[k] for k in kb if k not in kx]
        gone_flag = [r for r in gone if r[1][1:] in drop]
        gone_other = [r for r in gone if r[1][1:] not in drop]
        f = lambda rr: (len(rr), m.mean(r[4] for r in rr), sum(r[4] for r in rr))
        print(f"{v:5s} {h:8s} общие {len(common)} (тот же исход {same_res}) | снятые фильтром "
              f"n={f(gone_flag)[0]} E={f(gone_flag)[1]:+.3f} Σ={f(gone_flag)[2]:+.1f} | выпали "
              f"иначе n={f(gone_other)[0]} E={f(gone_other)[1]:+.3f} | новые (слоты) "
              f"n={f(new)[0]} E={f(new)[1]:+.3f} Σ={f(new)[2]:+.1f}")
