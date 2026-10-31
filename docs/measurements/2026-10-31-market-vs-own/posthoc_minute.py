"""Справочно ПОСЛЕ прогона (в регистрации не было): чувствительность разложения к цене рынка
в минуте филла и минуте выхода; где копится рыночная часть.

  PYTHONPATH=<корень репо> DATA_ROOT="/Users/mac/bot ctypto/data" nice -n 15 \
      .venv/bin/python docs/measurements/2026-10-31-market-vs-own/posthoc_minute.py market_own.pkl

market_own.pkl — результат `scripts/measure_market_own.py --out`. Новых сделок не считает.
"""
import pickle
import sys

import numpy as np

import scripts.fetch_m1 as m1
import scripts.measure_market_own as mm

df = pickle.load(open(sys.argv[1], "rb"))["df"]
bm = m1.load(mm.BTC)
bts = bm[:, 0].astype(np.int64)


def pos(t):
    i = np.searchsorted(bts, t)
    assert (bts[i] == t).all()
    return i


def price(t, where):
    """where: 0 — open минуты t, 1 — её close, 0.5 — середина."""
    i = pos(t)
    o, c = bm[i, 1], bm[i, 4]
    return o if where == 0 else c if where == 1 else (o + c) / 2


def lpx(t, close):
    i = pos(np.array([t]))[0]
    return float(np.log(bm[i, 4] if close else bm[i, 1]))


s = np.where(df["side"] == "LONG", 1.0, -1.0)
btc = (df["symbol"] == mm.BTC).to_numpy()
g, dd, c = df["g"].to_numpy(), df["d"].to_numpy(), df["c"].to_numpy()
beta = df["beta30"].to_numpy()
tf, tx = df["t_f"].to_numpy(np.int64), df["t_x"].to_numpy(np.int64)
idx = mm.Index()


def r_index(k, ws, we):
    """Как в основном скрипте, но точки входа/выхода внутри минуты — по ws/we."""
    r = df.iloc[k]
    lc = idx.loo(r.symbol)
    t_s, t_e = int(tf[k] + ws * mm.MIN), max(int(tx[k] + we * mm.MIN), int(tf[k] + ws * mm.MIN))
    hs, he = t_s - t_s % mm.HOUR, t_e - t_e % mm.HOUR
    if t_e == he and t_e > t_s:              # конец ровно на границе часа = close прошлого часа
        he -= mm.HOUR
    js, je = (hs - mm.G0) // mm.HOUR, (he - mm.G0) // mm.HOUR
    fs, fe = (t_s - hs) / mm.HOUR, (t_e - he) / mm.HOUR
    seg = lc[js:je + 1]
    bs, be = np.log(price(np.array([tf[k]]), ws)[0]), np.log(price(np.array([tx[k]]), we)[0])
    dev_s = bs - lpx(hs, False) - fs * (lpx(hs + 59 * mm.MIN, True) - lpx(hs, False))
    dev_e = be - lpx(he, False) - fe * (lpx(he + 59 * mm.MIN, True) - lpx(he, False))
    lev = float(seg[:-1].sum() + fe * seg[-1] - fs * seg[0])
    return float(np.expm1(lev + df["bI_btc"].iloc[k] * (dev_e - dev_s)))


def m_btc(r):
    return np.where(btc, g, s * beta * r / dd)


print("== 1. Где копится рыночная часть (BTC β30, без сделок по BTC; R на сделку)")
o_f, c_f = price(tf, 0), price(tf, 1)
c_1h = price(np.minimum(tf + 60 * mm.MIN, tx), 1)
parts = {"час до филла": s * beta * (o_f / price(tf - 60 * mm.MIN, 0) - 1) / dd,
         "минута филла": m_btc(c_f / o_f - 1), "первый час после": m_btc(c_1h / c_f - 1),
         "вся сделка (регистрация)": df["M30"].to_numpy()}
for h in mm.HALVES:
    m = (df["half"] == h).to_numpy() & ~btc
    print(f"  {h:8s} " + " | ".join(f"{k} {v[m].mean():+.4f}" for k, v in parts.items())
          + f" | после минуты филла {(parts['вся сделка (регистрация)'] - parts['минута филла'])[m].mean():+.4f}")

print("\n== 2. Вилка соглашений: цена рынка в минуте филла → в минуте выхода (O BTC / O_I индекс)")
conv = {"open→close (регистрация)": (0, 1), "середина→середина": (0.5, 0.5),
        "close→open (узкое окно)": (1, 0)}
res = {}
for name, (ws, we) in conv.items():
    ps, pe = price(tf, ws), price(tx, we)
    if ws > we:
        pe = np.where(tx == tf, ps, pe)
    rI = np.array([r_index(k, ws, we) for k in range(len(df))])
    res[name] = (g - m_btc(pe / ps - 1), g - s * df["betaI"].to_numpy() * rI / dd)
for h in mm.HALVES:
    m = (df["half"] == h).to_numpy()
    print(f"  {h:8s} C {c[m].mean():.4f} | " + " | ".join(
        f"{k}: {o[m].mean():+.4f} / {oi[m].mean():+.4f}" for k, (o, oi) in res.items()))
o0, oi0 = res["open→close (регистрация)"]
print(f"  контроль: open→close = основной прогон, макс. расхождение O "
      f"{float(np.abs(o0 - df['O30']).max()):.1e}, O_I {float(np.nanmax(np.abs(oi0 - df['OI']))):.1e}")
