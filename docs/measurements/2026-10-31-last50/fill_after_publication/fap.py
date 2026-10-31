"""Общая часть замера «филл до постановки» (PREREG.md рядом): класс минуты касания.

Минута касания — первая 1m-свеча с low ≤ entry ≤ high в часе [t, t+1ч), t — решение (закрытие
решающего 1h-бара). P0 ⇔ m = 0, P01 ⇔ m ∈ {0,1}, P012 ⇔ m ∈ {0,1,2}.
"""
from __future__ import annotations

import numpy as np

MIN, HOUR = 60_000, 3_600_000
CLASSES = {"P0": 0, "P01": 1, "P012": 2}       # класс -> максимальная минута


def touch_minute(entry: float, t_ms: int, bars: np.ndarray) -> tuple:
    """bars (n,5): ts, o, h, l, c по возрастанию ts. -> (m | None, hole: bool, n_in_hour).

    m — номер минуты (0..59) первой свечи часа [t, t+1ч), диапазон которой содержит entry;
    None — ни одна свеча часа не содержит entry (или минуток в часе нет).
    hole — в данных нет свечи одной из минут 0..min(m,3)-1 (до первого касания, в зоне
    классов P0/P01/P012): тогда настоящее первое касание могло быть раньше."""
    if not len(bars):
        return None, True, 0
    ts = bars[:, 0]
    a = int(np.searchsorted(ts, t_ms, "left"))
    b = int(np.searchsorted(ts, t_ms + HOUR, "left"))
    seg = bars[a:b]
    if not len(seg):
        return None, True, 0
    hit = np.nonzero((seg[:, 3] <= entry) & (entry <= seg[:, 2]))[0]
    mins = ((seg[:, 0] - t_ms) // MIN).astype(np.int64)
    if not hit.size:
        need = range(0, 3)
        hole = any(k not in set(mins[:4].tolist()) for k in need)
        return None, hole, len(seg)
    m = int(mins[hit[0]])
    have = set(mins[: m + 1].tolist())
    hole = any(k not in have for k in range(0, min(m, 3)))
    return m, hole, len(seg)


def in_class(m, cls: str) -> bool:
    return m is not None and m <= CLASSES[cls]


# мем-монеты: список зафиксирован в scripts/research_aggr_clusters.py (замер 2026-10-31,
# регистрация до прогона), здесь — дословная копия; сравнение по базе без числового префикса
MEMES = {
    "DOGE", "SHIB", "PEPE", "FLOKI", "BONK", "WIF", "BOME", "MEME", "PEOPLE", "TURBO", "NEIRO",
    "NEIROETH", "MOODENG", "PNUT", "GOAT", "ACT", "POPCAT", "MEW", "BRETT", "TRUMP", "MELANIA",
    "FARTCOIN", "SPX", "PENGU", "MOG", "DOGS", "CAT", "SATS", "RATS", "BABYDOGE", "HIPPO",
    "CHILLGUY", "VINE", "LADYS", "AIDOGE", "COQ", "MYRO", "SLERF", "BANANA", "MBABYDOGE", "BOB",
    "WOJAK", "SUNDOG", "GIGA", "TOSHI", "PONKE", "MICHI", "FWOG", "SC", "PUMPFUN", "PUMP", "BAN",
    "ZEREBRO", "AIZ", "GRIFFAIN", "TST", "BROCCOLI", "MUBARAK", "哈基米", "币安人生", "我踏马来了",
    "牛来", "龙虾",
}


def _base(sym: str) -> str:
    """'1000PEPE/USDT:USDT' -> 'PEPE' (как research_aggr_fetch._base на BASEUSDT)."""
    import re
    s = sym.split("/")[0]
    s = re.sub(r"^\d+", "", s)
    return re.sub(r"1000$", "", s)


def is_meme(sym: str) -> bool:
    b = _base(sym)
    return b in MEMES or b.removeprefix("1M") in MEMES or "AI16Z" in sym
