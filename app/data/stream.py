"""
stream.py — реалтайм-поток Bybit v5 (public linear WebSocket): kline-стрим.

Назначение v1 (мега-план MAX, «скорость сигнала»): мгновенное «вход исполнен» —
касание entry видно внутри бара, а не на закрытии (выигрыш до часа на 1h ТФ).
ОФИЦИАЛЬНЫЙ скоринг исходов НЕ меняется: bar-based «стоп первым» остаётся
единственным судьёй результата (контракт трек-рекорда), poller — бэкстопом
для филлов (guard filled_at IS NULL исключает дубли в обе стороны).

Зависимостей нет: aiohttp уже приходит с aiogram.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

WS_URL = "wss://stream.bybit.com/v5/public/linear"

# наш лейбл ТФ -> интервал Bybit v5
_BYBIT_INTERVAL = {"1m": "1", "5m": "5", "15m": "15", "30m": "30",
                   "1h": "60", "4h": "240", "1d": "D"}

_RECONNECT_MAX_AGE = 900.0      # сек: пересобрать соединение (подхватить смену watchlist)
_PING_EVERY = 20.0              # Bybit требует ping не реже 20с


def ws_symbol(symbol: str) -> str:
    """'BTC/USDT:USDT' (ccxt unified) -> 'BTCUSDT' (топик Bybit)."""
    return symbol.split(":")[0].replace("/", "")


def bybit_interval(tf: str) -> str:
    """Наш лейбл ТФ -> интервал Bybit. ValueError на неизвестном — конфиг-ошибка видна сразу."""
    if tf not in _BYBIT_INTERVAL:
        raise ValueError(f"неизвестный ТФ {tf!r} (знаю: {sorted(_BYBIT_INTERVAL)})")
    return _BYBIT_INTERVAL[tf]


@dataclass
class KlineEvt:
    symbol_ws: str          # 'BTCUSDT'
    high: float
    low: float
    confirm: bool           # True = бар закрыт


def parse_kline(raw: str) -> list:
    """Сообщение Bybit v5 kline -> [KlineEvt]. Не-kline (подтверждения подписки, pong,
    мусор) -> []. Никогда не бросает — стрим не должен падать на одном кривом кадре."""
    try:
        msg = json.loads(raw)
        topic = msg.get("topic", "")
        if not topic.startswith("kline."):
            return []
        symbol_ws = topic.split(".")[-1]
        return [KlineEvt(symbol_ws=symbol_ws,
                         high=float(d["high"]), low=float(d["low"]),
                         confirm=bool(d.get("confirm")))
                for d in msg.get("data", [])]
    except Exception:                                   # noqa: BLE001
        return []


def entry_touched(entry: float, high: float, low: float) -> bool:
    """Лимитка касается диапазона бара — то же правило, что в bar-скоринге journal."""
    return low <= entry <= high


async def run_stream(get_symbols, get_tf, on_intrabar, enabled, log) -> None:
    """Вечный цикл стрима: connect -> subscribe kline.{tf}.{sym} -> события -> reconnect.

    get_symbols() -> list[str] (unified), get_tf() -> str (наш лейбл, напр. '1h'),
    on_intrabar(symbol_ws, high, low) -> awaitable, enabled() -> bool (флаг runtime,
    выключение подхватывается не позже _RECONNECT_MAX_AGE), log — логгер.
    Ошибки цикла гасятся с бэкоффом — стрим никогда не роняет процесс."""
    import aiohttp

    backoff = 1.0
    while True:
        try:
            # чек флага ВНУТРИ try: enabled() — синхронное чтение SQLite, редкий
            # OperationalError (гонка с ночным бэкапом) вне try убивал таску навсегда
            # (аудит-2 ops [2]) — реалтайм-филлы молча деградировали до бар-скоринга
            if not enabled():
                await asyncio.sleep(30)
                continue
            symbols = get_symbols()
            interval = bybit_interval(get_tf())
            topics = [f"kline.{interval}.{ws_symbol(s)}" for s in symbols]
            planned = False                             # False -> сервер закрыл (close/error)
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(WS_URL, heartbeat=None) as ws:
                    await ws.send_json({"op": "subscribe", "args": topics})
                    log.info("стрим подключён: %d топиков (%s)", len(topics), interval)
                    started = asyncio.get_event_loop().time()
                    last_ping = started
                    while True:
                        now = asyncio.get_event_loop().time()
                        if now - started > _RECONNECT_MAX_AGE or not enabled():
                            planned = True
                            break                       # плановый reconnect / выключили флаг
                        if now - last_ping > _PING_EVERY:
                            await ws.send_json({"op": "ping"})
                            last_ping = now
                        try:
                            frame = await ws.receive(timeout=5.0)
                        except asyncio.TimeoutError:
                            continue
                        if frame.type != aiohttp.WSMsgType.TEXT:
                            break                       # close/error frame -> reconnect с backoff
                        # отказ подписки Bybit молча терялся в parse_kline -> стрим «подключён»,
                        # а филлы не приходят (аудит-2 ops [5]) — теперь виден в логе
                        if '"success":false' in frame.data.replace(" ", ""):
                            log.warning("стрим: отказ операции: %.200s", frame.data)
                        for evt in parse_kline(frame.data):
                            await on_intrabar(evt.symbol_ws, evt.high, evt.low)
            if planned:
                backoff = 1.0                           # штатное соединение отжило свой срок
            else:
                # сервер закрыл соединение (close/error) — backoff, чтобы не устроить
                # reconnect-шторм по бирже, пока она шеддит нагрузку (аудит [10]).
                # Раньше этот путь реконнектил мгновенно, без задержки.
                log.warning("стрим: соединение закрыто сервером — reconnect через %.0fс", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
        except asyncio.CancelledError:
            raise
        except Exception as e:                          # noqa: BLE001 — сеть/биржа
            log.warning("стрим: %s — reconnect через %.0fс", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
