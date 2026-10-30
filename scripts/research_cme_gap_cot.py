"""Замер «гэп CME и COT» по BTC (docs/measurements/2026-10-31-cme-gap-cot.md).

  .venv/bin/python -m scripts.research_cme_gap_cot fetch   # данные в data/cache_research/cme/
  .venv/bin/python -m scripts.research_cme_gap_cot run     # A0, A1, B0 (+ B1, если B0 прошёл)

Цена — Binance spot BTCUSDT (1h для гэпов и описательной части, 1m в окне сделки), фандинг —
Binance USDT-M BTCUSDT, COT — CFTC TFF Futures Only (gpe5-46if), контракт 133741.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

CACHE = Path("/Users/mac/bot ctypto/data/cache_research/cme")
CT = ZoneInfo("America/Chicago")
NY = ZoneInfo("America/New_York")
UTC = timezone.utc
START = datetime(2017, 12, 1, tzinfo=UTC)
END = datetime(2026, 10, 30, 23, 0, tzinfo=UTC)
TRAIN_END = pd.Timestamp("2024-01-01", tz="UTC")
VAL_END = pd.Timestamp("2026-08-01", tz="UTC")
FEE = 0.00055          # тейкер на сторону
SLIP = 0.0002          # проскальзывание на рыночном исполнении
DEFAULT_FUNDING = 0.0001   # до 2019-09-10, за 8 ч
THRESHOLDS = (0.01, 0.02, 0.03)
HOLD_H = 72
_last_call = [0.0]


def _get(url: str, tries: int = 6):
    for k in range(tries):
        wait = 0.34 - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:                      # noqa: BLE001 — сеть/429: пауза и повтор
            if k == tries - 1:
                raise
            time.sleep(2 ** k)
            print("повтор", k + 1, type(e).__name__, url[:80], flush=True)
    return None


def _klines(interval: str, start: datetime, end: datetime, step_ms: int) -> pd.DataFrame:
    rows, t = [], int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    while t < end_ms:
        q = urllib.parse.urlencode({"symbol": "BTCUSDT", "interval": interval, "startTime": t,
                                    "endTime": end_ms, "limit": 1000})
        part = _get(f"https://api.binance.com/api/v3/klines?{q}")
        if not part:
            break
        rows += part
        t = part[-1][0] + step_ms
        if len(part) < 1000:
            break
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol", "cts", "qv",
                                     "n", "tbv", "tbq", "ign"])
    df = df[["ts", "open", "high", "low", "close"]].astype(float)
    df["ts"] = df["ts"].astype("int64")
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def fetch_1h() -> pd.DataFrame:
    p = CACHE / "btc_1h.csv"
    if p.exists():
        return pd.read_csv(p)
    df = _klines("1h", START, END, 3_600_000)
    df.to_csv(p, index=False)
    return df


def weekends(h1: pd.DataFrame) -> pd.DataFrame:
    """Пятничное закрытие CME (16:00 CT) и воскресное открытие (17:00 CT) по 1h-свечам спота."""
    by_ts = h1.set_index("ts")
    out = []
    d = datetime(2017, 12, 22).date()          # первая пятница после старта фьючерсов CME (17.12)
    while True:
        fri_close = datetime(d.year, d.month, d.day, 16, 0, tzinfo=CT).astimezone(UTC)
        sun = d + timedelta(days=2)
        sun_open = datetime(sun.year, sun.month, sun.day, 17, 0, tzinfo=CT).astimezone(UTC)
        if sun_open > END - timedelta(hours=1):
            break
        c_ts = int((fri_close - timedelta(hours=1)).timestamp() * 1000)
        o_ts = int(sun_open.timestamp() * 1000)
        if c_ts in by_ts.index and o_ts in by_ts.index:
            close = float(by_ts.at[c_ts, "close"])
            opn = float(by_ts.at[o_ts, "open"])
            out.append({"fri_close": fri_close, "sun_open": sun_open, "close": close, "open": opn,
                        "gap": (opn - close) / close})
        d += timedelta(days=7)
    return pd.DataFrame(out)


def fetch_1m_windows(wk: pd.DataFrame) -> None:
    (CACHE / "m1").mkdir(parents=True, exist_ok=True)
    todo = wk[wk["gap"].abs() >= min(THRESHOLDS)]
    for i, r in enumerate(todo.itertuples()):
        p = CACHE / "m1" / f"{int(r.sun_open.timestamp())}.csv"
        if p.exists():
            continue
        df = _klines("1m", r.sun_open, r.sun_open + timedelta(hours=HOLD_H + 1), 60_000)
        df.to_csv(p, index=False)
        if i % 20 == 0:
            print(f"1m: {i + 1}/{len(todo)}", flush=True)


def fetch_funding() -> pd.DataFrame:
    p = CACHE / "funding.csv"
    if p.exists():
        return pd.read_csv(p)
    rows, t = [], int(datetime(2019, 9, 1, tzinfo=UTC).timestamp() * 1000)
    end_ms = int(END.timestamp() * 1000)
    while t < end_ms:
        q = urllib.parse.urlencode({"symbol": "BTCUSDT", "startTime": t, "limit": 1000})
        part = _get(f"https://fapi.binance.com/fapi/v1/fundingRate?{q}")
        if not part:
            break
        rows += [(int(x["fundingTime"]), float(x["fundingRate"])) for x in part]
        t = part[-1]["fundingTime"] + 1
        if len(part) < 1000:
            break
    df = pd.DataFrame(rows, columns=["ts", "rate"]).drop_duplicates("ts")
    df.to_csv(p, index=False)
    return df


def fetch_cot() -> pd.DataFrame:
    p = CACHE / "cot_tff_133741.csv"
    if p.exists():
        return pd.read_csv(p)
    q = urllib.parse.urlencode({
        "$select": "report_date_as_yyyy_mm_dd,lev_money_positions_long,lev_money_positions_short",
        "$where": "cftc_contract_market_code='133741'",
        "$order": "report_date_as_yyyy_mm_dd", "$limit": 5000})
    rows = _get(f"https://publicreporting.cftc.gov/resource/gpe5-46if.json?{q}")
    df = pd.DataFrame(rows)
    df.columns = ["date", "long", "short"]
    df["long"] = df["long"].astype(float)
    df["short"] = df["short"].astype(float)
    df.to_csv(p, index=False)
    return df


# ---------------------------------------------------------------- A0: описательная часть
def a0(h1: pd.DataFrame, wk: pd.DataFrame) -> pd.DataFrame:
    hi = h1.set_index("ts")
    rows = []
    for r in wk.itertuples():
        o_ts = int(r.sun_open.timestamp() * 1000)
        seg = hi.loc[o_ts: o_ts + 7 * 24 * 3_600_000 - 1]
        d = abs(r.open - r.close)
        up = r.gap > 0
        lvl, mirror = r.close, (r.open + d if up else r.open - d)
        res = {"sun_open": r.sun_open, "gap": r.gap}
        for days in (1, 3, 7):
            s = seg.iloc[: days * 24]
            if up:
                res[f"fill{days}"] = bool((s["low"] <= lvl).any())
                res[f"mir{days}"] = bool((s["high"] >= mirror).any())
            else:
                res[f"fill{days}"] = bool((s["high"] >= lvl).any())
                res[f"mir{days}"] = bool((s["low"] <= mirror).any())
        rows.append(res)
    df = pd.DataFrame(rows)
    df["bucket"] = pd.cut(df["gap"].abs(), [0, .005, .01, .02, .03, 10],
                          labels=["<0.5%", "0.5–1%", "1–2%", "2–3%", "≥3%"], right=False)
    return df


# ---------------------------------------------------------------- A1: торговая версия
def _funding_lookup(fund: pd.DataFrame):
    ts = fund["ts"].to_numpy()
    rate = fund["rate"].to_numpy()
    first = ts[0] if len(ts) else 10 ** 15

    def between(t0: int, t1: int) -> list[float]:
        out = []
        # до появления фандинга Binance — моменты 00/08/16 UTC по 0.01%
        t = (t0 // 28_800_000 + 1) * 28_800_000
        while t <= t1 and t < first:
            out.append(DEFAULT_FUNDING)
            t += 28_800_000
        i = np.searchsorted(ts, max(t0 + 1, first), side="left")
        j = np.searchsorted(ts, t1, side="right")
        out += list(rate[i:j])
        return out
    return between


def simulate(r, m1: pd.DataFrame, funding_between) -> dict:
    up = r.gap > 0                              # гэп вверх -> шорт к пятничному закрытию
    d = abs(r.open - r.close)
    stop = r.open + d if up else r.open - d
    target = r.close
    entry = r.open * (1 - SLIP) if up else r.open * (1 + SLIP)
    t_end = int((r.sun_open + timedelta(hours=HOLD_H)).timestamp() * 1000)
    seg = m1[m1["ts"] < t_end]
    exit_px, kind, t_exit = None, "time", None
    for row in seg.itertuples():
        if up:
            hit_stop, hit_tgt = row.high >= stop, row.low <= target
        else:
            hit_stop, hit_tgt = row.low <= stop, row.high >= target
        if hit_stop:                            # стоп раньше цели в одной минутке — худший случай
            exit_px = stop * (1 + SLIP) if up else stop * (1 - SLIP)
            kind, t_exit = "stop", row.ts
            break
        if hit_tgt:
            exit_px, kind, t_exit = target, "target", row.ts
            break
    if exit_px is None:
        last = seg.iloc[-1]
        exit_px = last["close"] * (1 + SLIP) if up else last["close"] * (1 - SLIP)
        t_exit = int(last["ts"]) + 60_000
    gross = (entry - exit_px) / entry if up else (exit_px - entry) / entry
    gross_nocost = ((r.open - (target if kind == "target" else (stop if kind == "stop" else
                     seg.iloc[-1]["close"]))) / r.open) * (1 if up else -1)
    fund = sum(funding_between(int(r.sun_open.timestamp() * 1000), int(t_exit)))
    fund_pnl = fund if up else -fund            # шорт получает положительный фандинг
    net = gross - 2 * FEE + fund_pnl
    risk = abs(r.gap)
    return {"sun_open": r.sun_open, "gap": r.gap, "side": "short" if up else "long", "kind": kind,
            "R_gross": gross_nocost / risk, "R_net": net / risk,
            "hours": (t_exit - r.sun_open.timestamp() * 1000) / 3.6e6}


def _period(ts: pd.Timestamp) -> str:
    if ts < TRAIN_END:
        return "train"
    return "val" if ts < VAL_END else "frozen"


def _half(ts: pd.Timestamp) -> str:
    return f"{ts.year}H{1 if ts.month <= 6 else 2}"


def _dd(r: pd.Series) -> float:
    c = r.cumsum()
    return float((c.cummax() - c).max()) if len(c) else 0.0


def summarize(tr: pd.DataFrame) -> dict:
    return {"n": int(len(tr)), "gross": round(float(tr["R_gross"].mean()), 3) if len(tr) else None,
            "net": round(float(tr["R_net"].mean()), 3) if len(tr) else None,
            "sumR": round(float(tr["R_net"].sum()), 1),
            "win": round(float((tr["R_net"] > 0).mean()), 2) if len(tr) else None,
            "dd": round(_dd(tr["R_net"]), 1),
            "target%": round(float((tr["kind"] == "target").mean()), 2) if len(tr) else None}


# ---------------------------------------------------------------- B: COT
def cot_events(cot: pd.DataFrame, h1: pd.DataFrame) -> pd.DataFrame:
    cot = cot.copy()
    cot["date"] = pd.to_datetime(cot["date"]).dt.tz_localize(None)
    cot = cot.sort_values("date").reset_index(drop=True)
    cot["net"] = cot["long"] - cot["short"]
    roll = cot["net"].rolling(52, min_periods=26)
    cot["z"] = (cot["net"] - roll.mean()) / roll.std(ddof=0)
    pubs = []
    for d in cot["date"]:
        pd_ = (d + pd.Timedelta(days=3)).date()
        pubs.append(datetime(pd_.year, pd_.month, pd_.day, 15, 30, tzinfo=NY).astimezone(UTC))
    cot["pub"] = pubs
    ts = h1["ts"].to_numpy()
    op = h1["open"].to_numpy()

    def px_at(t: datetime) -> float | None:
        i = np.searchsorted(ts, int(t.timestamp() * 1000), side="left")
        return float(op[i]) if i < len(ts) else None
    for k in (1, 4, 12):
        vals = []
        for t in cot["pub"]:
            p0, p1 = px_at(t), px_at(t + timedelta(weeks=k))
            late = t + timedelta(weeks=k) > END
            vals.append(None if (p0 is None or p1 is None or late) else p1 / p0 - 1)
        cot[f"r{k}w"] = vals
    return cot


def _episodes(mask: pd.Series) -> pd.Series:
    return mask & ~mask.shift(1, fill_value=False)


def run() -> None:
    h1 = fetch_1h()
    wk = weekends(h1)
    out: dict = {"weekends": int(len(wk))}

    # A0
    a = a0(h1, wk)
    tab = a.groupby("bucket", observed=True).agg(
        n=("gap", "size"), fill1=("fill1", "mean"), fill3=("fill3", "mean"),
        fill7=("fill7", "mean"), mir1=("mir1", "mean"), mir3=("mir3", "mean"),
        mir7=("mir7", "mean")).round(3)
    a["dir"] = np.where(a["gap"] > 0, "up", "down")
    tab_dir = a.groupby(["bucket", "dir"], observed=True).agg(
        n=("gap", "size"), fill3=("fill3", "mean"), mir3=("mir3", "mean")).round(3)
    out["A0"] = {"all": {k: v for k, v in tab.reset_index().to_dict("index").items()},
                 "by_dir": tab_dir.reset_index().to_dict("records"),
                 "total": a[["fill1", "fill3", "fill7", "mir1", "mir3", "mir7"]]
                 .mean().round(3).to_dict()}
    print("A0:\n", tab.to_string(), flush=True)

    # A1
    fund = fetch_funding()
    between = _funding_lookup(fund)
    trades = []
    for r in wk[wk["gap"].abs() >= min(THRESHOLDS)].itertuples():
        p = CACHE / "m1" / f"{int(r.sun_open.timestamp())}.csv"
        m1 = pd.read_csv(p)
        if m1.empty:
            continue
        trades.append(simulate(r, m1, between))
    tr = pd.DataFrame(trades)
    tr["sun_open"] = pd.to_datetime(tr["sun_open"], utc=True)
    tr["period"] = tr["sun_open"].map(_period)
    tr["half"] = tr["sun_open"].map(_half)
    tr.to_csv(CACHE / "a1_trades.csv", index=False)
    res = {}
    for T in THRESHOLDS:
        s = tr[tr["gap"].abs() >= T]
        sv = s[s.period == "val"]
        res[T] = {"train": summarize(s[s.period == "train"]), "val": summarize(sv),
                  "val_halves": {h: round(float(g["R_net"].mean()), 3)
                                 for h, g in sv.groupby("half")},
                  "val_halves_n": {h: int(len(g)) for h, g in sv.groupby("half")}}
    elig = [T for T in THRESHOLDS if res[T]["train"]["n"] >= 30]
    pick = max(elig, key=lambda T: res[T]["train"]["net"]) if elig else None
    out["A1"] = {str(k): v for k, v in res.items()}
    out["A1_pick"] = pick
    if pick is not None:
        v = res[pick]
        nb = [0.02] if pick == 0.01 else ([0.01, 0.03] if pick == 0.02 else [0.02])
        c1 = all(x > 0 for x in v["val_halves"].values()) and len(v["val_halves"]) >= 5
        c2 = any((res[n]["val"]["net"] or -1) > 0 for n in nb)
        c3 = v["val"]["n"] >= 60
        out["A1_criteria"] = {"each_half_positive": c1, "neighbor_positive": c2, "n_val>=60": c3,
                              "candidate": bool(c1 and c2 and c3)}
        s = tr[(tr["gap"].abs() >= pick) & (tr.period == "frozen")]
        out["A1_frozen"] = summarize(s)
    print(json.dumps({"A1": out["A1"], "pick": pick, "crit": out.get("A1_criteria"),
                      "frozen": out.get("A1_frozen")}, ensure_ascii=False, indent=1, default=str),
          flush=True)

    # B0
    cot = cot_events(fetch_cot(), h1)
    cot.to_csv(CACHE / "cot_events.csv", index=False)
    pre = cot["pub"] < pd.Timestamp("2024-01-01", tz="UTC")
    b0 = {}
    for name, mask in (("z<=-2", cot["z"] <= -2), ("z>=+2", cot["z"] >= 2)):
        ep = _episodes(mask.fillna(False))
        row = {"weeks": int(mask.sum()), "episodes": int(ep.sum()),
               "episodes_pre2024": int((ep & pre).sum()), "episodes_2024+": int((ep & ~pre).sum())}
        for k in (1, 4, 12):
            col = f"r{k}w"
            for lbl, per in (("pre", pre), ("post", ~pre)):
                base = cot.loc[per, col].dropna()
                evt = cot.loc[ep & per, col].dropna()
                row[f"{col}_{lbl}_event"] = round(float(evt.mean()), 4) if len(evt) else None
                row[f"{col}_{lbl}_base"] = round(float(base.mean()), 4) if len(base) else None
                row[f"{col}_{lbl}_n"] = int(len(evt))
        b0[name] = row
    out["B0"] = b0
    crit = {}
    for name, row in b0.items():
        same = []
        for k in (1, 4, 12):
            e1, b1 = row[f"r{k}w_pre_event"], row[f"r{k}w_pre_base"]
            e2, b2 = row[f"r{k}w_post_event"], row[f"r{k}w_post_base"]
            if None in (e1, b1, e2, b2):
                same.append(False)
            else:
                same.append(np.sign(e1 - b1) == np.sign(e2 - b2) != 0)
        crit[name] = {"same_sign_all_horizons": bool(all(same)),
                      "episodes>20": row["episodes"] > 20,
                      "pass": bool(all(same) and row["episodes"] > 20)}
    out["B0_criteria"] = crit
    out["cot_reports"] = int(len(cot))
    out["cot_range"] = [str(cot["date"].min().date()), str(cot["date"].max().date())]
    print(json.dumps({"B0": b0, "crit": crit}, ensure_ascii=False, indent=1), flush=True)
    (CACHE / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "run"])
    a = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    if a.cmd == "fetch":
        h1 = fetch_1h()
        print("1h:", len(h1), flush=True)
        wk = weekends(h1)
        print("weekends:", len(wk), "with |gap|>=1%:", int((wk["gap"].abs() >= 0.01).sum()),
              flush=True)
        fetch_1m_windows(wk)
        print("funding:", len(fetch_funding()), "cot:", len(fetch_cot()), flush=True)
    else:
        run()


if __name__ == "__main__":
    main()
