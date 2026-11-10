"""Сбор публичных данных Hyperliquid для замера №18
(docs/measurements/2026-11-10-hl-persistence.md).

  DATA_ROOT="/Users/mac/bot ctypto/data" .venv/bin/python -m scripts.hl_fetch portfolio [--share 40]
  DATA_ROOT=... .venv/bin/python -m scripts.hl_fetch fills --jobs jobs.txt [--pages 10]
  DATA_ROOT=... .venv/bin/python -m scripts.hl_fetch role --addrs addrs.txt

Только чтение, без ключей. Один поток, пауза между запросами (вес portfolio и userFillsByTime —
20, userRole — 60 при лимите 1200 в минуту на IP). Кэш — DATA_ROOT/cache_research/hl/, сбор
возобновляется с места остановки; файл STOP в этой папке останавливает его после текущего запроса.

  portfolio — кандидаты из последнего снимка рейтинга (оборот за всё время ≥ $10 млн, не вольт),
              доля --share процентов по адресу; пишет portfolio.jsonl: ряд PnL и капитала перпов.
  fills     — jobs.txt: строки «адрес начало_мс конец_мс»; пишет fills/<адрес>_<начало>.jsonl,
              только перпы; страниц не больше --pages (сделок — 2000 на страницу).
  role      — addrs.txt: адрес в строке; пишет roles.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.hyperliquid.xyz/info"
MIN_VLM = 10_000_000.0
PAUSE = {"portfolio": 1.6, "userFillsByTime": 1.6, "userRole": 4.0}
_last = [0.0]


def root() -> Path:
    p = Path(os.environ.get("DATA_ROOT", "data")) / "cache_research" / "hl"
    p.mkdir(parents=True, exist_ok=True)
    return p


def post(body: dict):
    """Один запрос info с паузой по типу; 429 и сеть — повтор с растущей паузой."""
    wait = PAUSE.get(body["type"], 1.6) - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    err = ""
    for attempt in range(6):
        _last[0] = time.time()
        req = urllib.request.Request(API, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
            time.sleep(60 if e.code == 429 else 5 + 5 * attempt)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            err = str(e)[:80]
            time.sleep(5 + 5 * attempt)
    raise RuntimeError(f"{body['type']} {body.get('user', '')}: {err}")


def in_share(addr: str, share: int) -> bool:
    """Детерминированная выборка: последние 6 hex-цифр адреса как число, остаток от 100."""
    return int(addr[-6:], 16) % 100 < share


def candidates(share: int) -> list[str]:
    snap = sorted(root().glob("leaderboard_*.json"))[-1]
    vaults = {(v.get("summary") or v).get("vaultAddress")
              for v in json.loads((root() / "vaults.json").read_text())}
    out = []
    for r in json.loads(snap.read_text())["leaderboardRows"]:
        a = r["ethAddress"]
        if a in vaults or float(dict(r["windowPerformances"])["allTime"]["vlm"]) < MIN_VLM:
            continue
        if in_share(a, share):
            out.append(a)
    return sorted(out, key=lambda a: a[-6:])           # порядок псевдослучайный, стабильный


def done_keys(path: Path, key: str = "a") -> set:
    if not path.exists():
        return set()
    out = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                out.add(json.loads(line)[key])
            except (ValueError, KeyError):
                continue                               # оборванная последняя строка
    return out


def stop_asked() -> bool:
    return (root() / "STOP").exists()


def series(win: dict) -> dict:
    return {"av": [[int(t), round(float(v), 2)] for t, v in win["accountValueHistory"]],
            "pnl": [[int(t), round(float(v), 2)] for t, v in win["pnlHistory"]],
            "vlm": round(float(win["vlm"]), 0)}


def cmd_portfolio(a) -> None:
    out = root() / "portfolio.jsonl"
    have = done_keys(out)
    todo = [x for x in candidates(a.share) if x not in have][:a.limit or None]
    print(f"portfolio: к сбору {len(todo)} счетов (доля {a.share}%), уже есть {len(have)}",
          flush=True)
    fails = 0
    with open(out, "a", encoding="utf-8") as f:
        for i, addr in enumerate(todo, 1):
            if stop_asked():
                print("STOP — выхожу", flush=True)
                break
            try:
                p = dict(post({"type": "portfolio", "user": addr}))
                row = {"a": addr, "t": int(time.time() * 1000),
                       "all": series(p["perpAllTime"]), "month": series(p["perpMonth"])}
            except Exception as e:                      # noqa: BLE001 — счёт пропущен, сбор идёт
                fails += 1
                print(f"  {addr}: {type(e).__name__}: {str(e)[:100]}", flush=True)
                if fails > 200:
                    print("слишком много сбоев — стоп", flush=True)
                    break
                continue
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
            f.flush()
            if i % 200 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)} сбоев {fails} {time.strftime('%H:%M:%S')}", flush=True)


def is_perp(coin: str) -> bool:
    return not coin.startswith(("@", "#"))


def cmd_fills(a) -> None:
    out_dir = root() / "fills"
    out_dir.mkdir(exist_ok=True)
    jobs = [ln.split() for ln in Path(a.jobs).read_text().splitlines() if ln.strip()]
    print(f"fills: заданий {len(jobs)}", flush=True)
    for i, (addr, s, e) in enumerate(jobs, 1):
        path = out_dir / f"{addr}_{s}.jsonl"
        if path.exists() or stop_asked():
            continue
        rows, seen, start, pages, full = [], set(), int(s), 0, False
        try:
            while pages < a.pages:
                page = post({"type": "userFillsByTime", "user": addr, "startTime": start,
                             "endTime": int(e)})
                pages += 1
                new = [x for x in page if x["tid"] not in seen]
                for x in new:
                    seen.add(x["tid"])
                    if is_perp(x["coin"]):
                        rows.append({k: x.get(k) for k in (
                            "time", "coin", "px", "sz", "side", "dir", "startPosition",
                            "closedPnl", "fee", "crossed", "oid")})
                if len(page) < 2000 or not new:
                    break
                start = int(page[-1]["time"])           # тот же мс повторится — дубли по tid
            else:
                full = True                             # страниц не хватило: сделок больше
        except Exception as ex:                         # noqa: BLE001
            print(f"  {addr}: {type(ex).__name__}: {str(ex)[:100]}", flush=True)
            continue
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"addr": addr, "start": int(s), "end": int(e), "pages": pages,
                                "truncated": full, "n": len(rows)}) + "\n")
            for x in rows:
                f.write(json.dumps(x, separators=(",", ":")) + "\n")
        if i % 25 == 0 or i == len(jobs):
            print(f"  {i}/{len(jobs)} {time.strftime('%H:%M:%S')}", flush=True)


def cmd_role(a) -> None:
    out = root() / "roles.jsonl"
    have = done_keys(out)
    todo = [x.strip() for x in Path(a.addrs).read_text().splitlines()
            if x.strip() and x.strip() not in have]
    print(f"role: к сбору {len(todo)}", flush=True)
    with open(out, "a", encoding="utf-8") as f:
        for addr in todo:
            if stop_asked():
                break
            try:
                r = post({"type": "userRole", "user": addr})
            except Exception as e:                      # noqa: BLE001
                print(f"  {addr}: {type(e).__name__}", flush=True)
                continue
            f.write(json.dumps({"a": addr, "role": r.get("role")}) + "\n")
            f.flush()


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("portfolio")
    p.add_argument("--share", type=int, default=40)
    p.add_argument("--limit", type=int, default=0, help="не больше N счетов за запуск")
    p = sub.add_parser("fills")
    p.add_argument("--jobs", required=True)
    p.add_argument("--pages", type=int, default=10)
    p = sub.add_parser("role")
    p.add_argument("--addrs", required=True)
    a = ap.parse_args()
    {"portfolio": cmd_portfolio, "fills": cmd_fills, "role": cmd_role}[a.cmd](a)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
