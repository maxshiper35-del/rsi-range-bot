"""Замер: обновление неисполненной заявки новым сигналом того же сетапа
(docs/measurements/2026-10-30-order-refresh.md).

  DATA_ROOT=/путь/к/data .venv/bin/python -m scripts.measure_order_refresh

* база — прод с 27.10 ОРИГИНАЛЬНЫМ путём (simulate_portfolio + живой судья) — паритет;
* копия — scripts.research_portfolio с refresh=False и репликой судьи: обязана совпасть
  с базой сделка-к-сделке;
* R1 — та же копия с refresh=True.
"""
from __future__ import annotations

from multiprocessing import Pool

import scripts.research_judge as rj
import scripts.research_portfolio as rp

rj.rebind_data()


def _run(job: tuple) -> tuple:
    data, path = job
    bars, sigs = rj.load(data)
    if path == "orig":
        rep = rj.simulate(bars, sigs)
        return data, path, rj.trades(rep), [], rep.skipped_dedup
    rj.install()
    rep = rp.simulate(bars, sigs, refresh=(path == "R1"))
    ref = [(rj.utc(now) + rj.HOUR, None if gone is None else gone.result_r,
            rj.key(s["symbol"], s["dt"], s["side"], s["setup"]))
           for now, gone, s in rep.refreshed]
    return data, path, rj.trades(rep), ref, rep.skipped["dedup"]


def main() -> None:
    jobs = [(d, p) for d in ("5y", "rot") for p in ("orig", "copy", "R1")]
    with Pool(3) as pool:
        res = {(d, p): (tr, ref, dd) for d, p, tr, ref, dd in pool.map(_run, jobs)}
    base = {d: res[(d, "orig")][0] for d in ("5y", "rot")}
    print("Паритет базы (оригинальный путь):")
    par = rj.check_parity(base)
    same = {d: res[(d, "copy")][0] == base[d] for d in base}
    print(f"Копия цикла (refresh=False) = база сделка-к-сделке: {same}")
    if not (par and all(same.values())):
        print("СТОП: паритет/копия не сошлись — вариант не интерпретируем")
        return
    var = {d: res[(d, "R1")][0] for d in base}
    print("Диагностика R1: обновлений; снятые заявки, которые исполнились бы (их R в этом "
          "прогоне); новые заявки (исполнились / их ΣR)")
    for d in base:
        ref, dd = res[(d, "R1")][1], res[(d, "R1")][2]
        trk = {x[3]: x[2] for x in var[d]}
        for half, part in rj.halves([(t, g, k) for t, g, k in ref], d).items():
            gone = [g for _t, g, _k in part if g is not None]
            new = [trk[k] for _t, _g, k in part if k in trk]
            print(f"  {d:3s} {half}: обновлений {len(part)}; снято исполнившихся бы "
                  f"{len(gone)} (ΣR {sum(gone):+.1f}); новые исполнились {len(new)} "
                  f"(ΣR {sum(new):+.1f})")
        print(f"  {d:3s} пропусков дедупом: база {res[(d, 'orig')][2]} → R1 {dd}")
    rj.verdict("R1 обновление неисполненной заявки новым сигналом сетапа", base, var)


if __name__ == "__main__":
    main()
