"""Бесплатные прайс-алерты (/alert) — утилита-воронка на WS-стриме klines.

Зачем бесплатно всем: люди приходят за утилитой («пингани меня на 65k»), остаются
за сигналами — это верх воронки, а не фича подписки. Работает на УЖЕ подключённом
стриме (bot._on_intrabar), отдельных соединений не создаёт, поэтому символы
ограничены текущим вотчлистом.

One-shot семантика: сработал -> triggered_at -> уведомление один раз. Взведение
заново — новый /alert (иначе болтание цены у уровня спамило бы каждым баром).
"""
from __future__ import annotations

from app import clock, db

MAX_ACTIVE_PER_USER = 10


def base_of(symbol: str) -> str:
    """'BTC/USDT:USDT' -> 'BTC' (короткое имя для юзера)."""
    return symbol.split("/")[0]


def resolve_symbol(token: str, watchlist: tuple | list) -> str | None:
    """Пользовательский ввод ('btc', 'BTC', 'BTC/USDT') -> unified символ вотчлиста."""
    t = (token or "").strip().upper().replace("/USDT:USDT", "").replace("/USDT", "")
    for s in watchlist:
        if base_of(s).upper() == t:
            return s
    return None


def create_alert(tg_id: int, symbol: str, price: float) -> dict:
    """-> {'ok': bool, 'reason': None|'limit', 'id': int|None}."""
    row = db.query_one("SELECT COUNT(*) AS n FROM price_alerts "
                       "WHERE tg_id=? AND triggered_at IS NULL", (tg_id,))
    n = int(row["n"]) if row else 0     # COUNT всегда отдаёт строку; ветка — для mypy
    if n >= MAX_ACTIVE_PER_USER:
        return {"ok": False, "reason": "limit", "id": None}
    aid = db.execute("INSERT INTO price_alerts(tg_id, symbol, price, created_at) "
                     "VALUES (?,?,?,?)", (tg_id, symbol, price, clock.utcnow_iso()))
    return {"ok": True, "reason": None, "id": aid}


def active_alerts(tg_id: int) -> list:
    return db.query("SELECT id, symbol, price FROM price_alerts "
                    "WHERE tg_id=? AND triggered_at IS NULL ORDER BY id", (tg_id,))


def delete_alert(tg_id: int, alert_id: int) -> bool:
    """Удалить СВОЙ армированный алерт (чужой id — отказ, не утечка информации)."""
    cur = db.execute("DELETE FROM price_alerts WHERE id=? AND tg_id=? "
                     "AND triggered_at IS NULL", (alert_id, tg_id))
    return bool(cur)


def rearm(alert_id: int) -> None:
    """Перевзвести алерт после ТРАНЗИЕНТНОГО сбоя доставки (не блокировки): «one-shot»
    — это обещание доставить один пинг, а не сжечь его молча при сетевом хикапе."""
    db.execute("UPDATE price_alerts SET triggered_at=NULL WHERE id=?", (alert_id,))


def purge_old_triggered(days: int = 30) -> int:
    """Чистка давно сработавших строк (таблица иначе растёт монотонно). -> удалено."""
    from datetime import timedelta
    cutoff = (clock.utcnow() - timedelta(days=days)).isoformat()
    return db.execute("DELETE FROM price_alerts WHERE triggered_at IS NOT NULL "
                      "AND triggered_at < ?", (cutoff,))


def check_and_trigger(symbol: str, high: float, low: float) -> list:
    """Армированные алерты символа, чей уровень внутри диапазона бара [low, high] ->
    пометить сработавшими АТОМАРНО и вернуть [{id, tg_id, symbol, price}] для
    уведомлений. Атомарность: два одновременных тика не продублируют пуш —
    UPDATE ... WHERE triggered_at IS NULL выигрывает ровно один раз."""
    rows = db.query("SELECT id, tg_id, symbol, price FROM price_alerts "
                    "WHERE symbol=? AND triggered_at IS NULL AND price >= ? AND price <= ?",
                    (symbol, low, high))
    fired = []
    now = clock.utcnow_iso()
    for r in rows:
        won = db.execute("UPDATE price_alerts SET triggered_at=? "
                         "WHERE id=? AND triggered_at IS NULL", (now, r["id"]))
        if won:
            fired.append(dict(r))
    return fired
