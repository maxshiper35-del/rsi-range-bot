"""Замер: выход «застрявшей» сделки через 12ч (docs/measurements/2026-10-30-stall-exit.md).

  DATA_ROOT=/путь/к/data .venv/bin/python -m scripts.measure_stall_exit

* база — прод с 27.10 (только 1h, живой судья, M2′) ОРИГИНАЛЬНЫМ путём
  (app.sim.portfolio._live_outcome) — обязана дать паритет бит-в-бит;
* обвязка — исследовательская реплика судьи с пустым правилом: обязана совпасть с базой
  сделка-к-сделке (иначе вариант мерит не то);
* Z12 — реплика с правилом застоя (пороги не перебираем).
"""
from __future__ import annotations

from multiprocessing import Pool

import scripts.research_judge as rj

rj.rebind_data()

HOURS = 12.0          # правило проверяется ОДИН раз — на первом баре ≥ 12ч после бара филла
MIN_MFE_R = 0.5       # если лучший ход в пользу за бары после филла < +0.5R — выход по close


class StallExit:
    """Z12: застрявшая сделка (за 12ч после филла ни разу не была +0.5R) закрывается по
    close бара; иначе — без изменений. Трейл 1.5/0.2 и потолок 168ч не трогаются."""

    def start(self, sig, entry, stop, risk, long_):
        return {"e": entry, "r": risk, "long": long_, "mfe": float("-inf"), "done": False}

    def bar(self, st, elapsed_h, high, low, open_, close):
        e, r = st["e"], st["r"]
        fav = (high - e) / r if st["long"] else (e - low) / r
        if fav > st["mfe"]:
            st["mfe"] = fav
        if not st["done"] and elapsed_h >= HOURS:
            st["done"] = True
            if st["mfe"] < MIN_MFE_R:
                return ("stall", round((close - e) / r if st["long"] else (e - close) / r, 4),
                        close)
        return None

    def finish(self, st, ev, r, j, exit_px):
        return ev, r, j, {}


def _run(job: tuple) -> tuple:
    data, path = job
    bars, sigs = rj.load(data)
    if path == "noop":
        rj.install(hook=rj.NoopHook())
    elif path == "Z12":
        rj.install(hook=StallExit())
    rep = rj.simulate(bars, sigs)
    stalls = {rj.key(t.symbol, t.signal_dt, t.side, t.setup): t.result_r
              for t in rep.detail if t.outcome == "stall-stop"}
    return data, path, rj.trades(rep), stalls


def main() -> None:
    jobs = [(d, p) for d in ("5y", "rot") for p in ("orig", "noop", "Z12")]
    with Pool(3) as pool:
        res = {(d, p): (tr, st) for d, p, tr, st in pool.map(_run, jobs)}
    base = {d: res[(d, "orig")][0] for d in ("5y", "rot")}
    print("Паритет базы (оригинальный путь):")
    par = rj.check_parity(base)
    same = {d: res[(d, "noop")][0] == base[d] for d in base}
    print(f"Обвязка (реплика + пустое правило) = база сделка-к-сделке: {same}")
    if not (par and all(same.values())):
        print("СТОП: паритет/обвязка не сошлись — вариант не интерпретируем")
        return
    var = {d: res[(d, "Z12")][0] for d in base}
    print("Диагностика: выходы по застою и те же сигналы в базе (парно)")
    for d in base:
        stalls = res[(d, "Z12")][1]
        base_r = {x[3]: x for x in base[d]}
        for half, part in rj.halves(var[d], d).items():
            ks = [x[3] for x in part if x[3] in stalls]
            paired = [(stalls[k], base_r[k][2]) for k in ks if k in base_r]
            gain = sum(a - b for a, b in paired)
            mean_v = sum(stalls[k] for k in ks) / len(ks) if ks else 0.0
            print(f"  {d:3s} {half}: выходов по застою {len(ks)}, ср. R {mean_v:+.3f}; "
                  f"в базе те же {len(paired)} сделок → прирост {gain:+.1f}R")
    rj.verdict("Z12 застрявшая сделка: 12ч и ход < +0.5R → выход по close", base, var)


if __name__ == "__main__":
    main()
