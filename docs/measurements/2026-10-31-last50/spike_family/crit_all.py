"""Справочно: пункты 1–5 критерия для всех трёх основных вариантов (из run.pkl)."""
import pickle
import measure_spike_family as m

d = pickle.load(open("run.pkl", "rb"))
tab = d["tab"]
base = d["base"]["rot"]
for v in m.MAIN:
    for h in m.HALVES_ROT:
        b = m.in_half(base, h)
        sb, sv = tab[("base", h)], tab[(v, h)]
        pp = m.p95(b, min(sv["n"], sb["n"]))
        c = [sv["net"] - sb["net"] >= 0.03, sv["net"] > pp, sv["dd"] <= sb["dd"],
             sv["n"] >= 0.5 * sb["n"], sv["rem_e"] <= sb["net"] - 0.10]
        print(f"{v:5s} {h:8s} ΔE {sv['net'] - sb['net']:+.4f} | E {sv['net']:+.4f} p95 {pp:+.3f} | "
              f"DD {sv['dd']:.1f}/{sb['dd']:.1f} | n {sv['n']}/{sb['n']} | E снятых "
              f"{sv['rem_e']:+.4f} порог {sb['net'] - 0.10:+.4f} | ΣR {sv['sum']:+.1f}/{sb['sum']:+.1f}"
              f" → {''.join('+' if x else '-' for x in c)}")
