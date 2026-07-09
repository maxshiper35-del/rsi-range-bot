"""
market_data.py — данные биржи и планировщик опроса.

Реализовано: fetch_closed_ohlcv (общий ccxt-инстанс, ретраи с бэкоффом, только
закрытые бары), load_frames (мультиТФ), load_correlated (SMT-партнёр),
seconds_to_next_close / poll_once / run_poller (такт по закрытию свечи).
"""
from __future__ import annotations

from typing import Callable

import pandas as pd


_TF_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def seconds_to_next_close(tf: str, now_ts: float) -> float:
    """Секунд до закрытия текущей свечи ТФ (UTC-выравнивание). Всегда в (0, period]."""
    period = _TF_SECONDS.get(tf)
    if period is None:
        raise ValueError(f"неизвестный таймфрейм: {tf!r} (жду {sorted(_TF_SECONDS)})")
    rem = period - (now_ts % period)
    return rem if rem > 0 else float(period)


def poll_once(symbols: list, timeframes: list, engine=None,
              frames_loader: Callable | None = None, pause_sec: float = 0.0,
              mode: str | None = None) -> list:
    """Один цикл сканирования: по каждому символу загрузить кадры и записать сигналы.

    mode: 'auto' — сигнал сразу активен (журнал+статистика); 'semiauto' — кандидат
    на подтверждение оператором. None -> из Settings.signal_mode.
    Возвращает список (Signal, signal_id). Ошибка по одному символу — лог + skip.
    """
    from app.brain.smc_setups import SMCEngine
    from app.delivery.bot import scan_auto, scan_semiauto
    from app.logging_conf import get_logger

    if mode is None:
        from app import runtime
        mode = runtime.signal_mode()
    scan = scan_auto if mode == "auto" else scan_semiauto

    log = get_logger("poller")
    loader = frames_loader or load_frames
    engine = engine or SMCEngine()
    import time as _time

    found = []
    bars_by_symbol = {}
    for i, symbol in enumerate(symbols):
        if pause_sec and i:
            _time.sleep(pause_sec)                      # разрядка запросов — не дразнить rate-limit
        try:
            frames = loader(symbol, list(timeframes))
            # бары входного ТФ (самого младшего) — для скоринга исходов
            entry_tf = min(frames, key=lambda t: SMCEngine.TF_MINUTES.get(t, 10 ** 9))
            bars_by_symbol[symbol] = frames[entry_tf]
            res = scan(symbol, frames, engine)
            if res is not None:
                found.append(res)
        except Exception as e:                          # noqa: BLE001 — один символ не роняет цикл
            log.warning("poll %s: %s", symbol, e)
    # каждый такт скорим исходы open/rejected по свежим закрытым барам
    closed = []
    try:
        from app.brain.journal import update_outcomes_from_bars
        closed = update_outcomes_from_bars(bars_by_symbol)
        if closed:
            log.info("исходов разрешено: %d", len(closed))
    except Exception as e:                              # noqa: BLE001
        log.warning("update_outcomes_from_bars: %s", e)
    return found, closed


async def run_poller(on_candidate, engine=None, on_tick=None, on_close=None) -> None:
    """Бесконечный цикл: спать до закрытия младшей свечи из Settings.timeframes,
    затем poll_once по watchlist; на каждого кандидата вызвать on_candidate(sig, sid).
    on_close (опц.) — колбэк со списком закрытых сделок (для постинга результатов).
    on_tick (опц.) — колбэк раз в такт (обслуживание подписок/платежей).
    Поддерживаются и sync-, и async-колбэки."""
    import asyncio
    import inspect
    import time as _time

    from app import runtime
    from app.brain.smc_setups import SMCConfig, SMCEngine
    from app.logging_conf import get_logger

    log = get_logger("poller")
    log.info("poller старт: символы=%s ТФ=%s", runtime.symbols(), runtime.timeframes())
    while True:
        # тело такта целиком под try: одно исключение НЕ убивает цикл навсегда
        try:
            # настройки читаем ДИНАМИЧЕСКИ каждый такт (правятся из /admin без рестарта)
            tfs = [tf for tf in runtime.timeframes() if tf in _TF_SECONDS] or ["1h"]
            base_tf = min(tfs, key=lambda t: _TF_SECONDS[t])
            delay = seconds_to_next_close(base_tf, _time.time()) + 2.0  # +2с бирже на закрытие
            await asyncio.sleep(delay)

            if runtime.paused():                        # пауза: сигналы не ищем, но обслуживаем подписки
                log.info("poller на паузе — пропускаю скан")
                found, closed = [], []
            else:
                eng = engine or SMCEngine(SMCConfig(
                    min_confidence=runtime.min_confidence(),
                    min_impulse_atr=runtime.min_impulse_atr(),
                    enabled_setups=runtime.enabled_setups()))
                found, closed = await asyncio.to_thread(
                    poll_once, runtime.symbols(), tfs, eng, None, 1.5)
            for sig, sid in found:
                try:
                    res = on_candidate(sig, sid)
                    if inspect.isawaitable(res):
                        await res
                except Exception:                       # noqa: BLE001 — колбэк не роняет цикл
                    log.exception("on_candidate #%s", sid)
            if closed and on_close is not None:         # посты с результатами сделок
                try:
                    res = on_close(closed)
                    if inspect.isawaitable(res):
                        await res
                except Exception:                       # noqa: BLE001
                    log.exception("on_close")
            if on_tick is not None:                     # обслуживание подписок/платежей
                try:
                    res = on_tick()
                    if inspect.isawaitable(res):
                        await res
                except Exception:                       # noqa: BLE001 — обслуживание не роняет цикл
                    log.exception("on_tick")
        except asyncio.CancelledError:
            raise                                       # штатная остановка
        except Exception:                               # noqa: BLE001
            log.exception("такт poller упал — продолжаю со следующей свечи")


# ---------------------------------------------------------------------------
# Мультитаймфрейм и SMT-партнёр
# ---------------------------------------------------------------------------
def _exchange(exchange_id: str):
    """ОДИН ccxt-инстанс на биржу (кэш): встроенный rate-limiter ccxt работает только
    внутри инстанса — новый объект на каждый вызов даёт залп запросов и 10006 от Bybit."""
    import ccxt  # ленивый импорт

    cache = getattr(_exchange, "_cache", None)
    if cache is None:
        cache = _exchange._cache = {}
    if exchange_id not in cache:
        cache[exchange_id] = getattr(ccxt, exchange_id)({
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })
    return cache[exchange_id]


def fetch_closed_ohlcv(symbol: str, timeframe: str, limit: int = 200,
                       exchange_id: str | None = None, retries: int = 3,
                       backoff_base: float = 2.0) -> pd.DataFrame:
    """OHLCV только с ЗАКРЫТЫМИ барами; общий инстанс биржи + ретраи с бэкоффом.

    Ретраит сетевые ошибки/rate-limit (2с, 4с, ...); после retries попыток — исключение
    (poll_once ловит его per-symbol). Последний (незакрытый) бар отбрасывается.
    """
    import time as _time

    from app.logging_conf import get_logger

    if exchange_id is None:
        from app.config import get_settings
        exchange_id = get_settings().exchange_id
    ex = _exchange(exchange_id)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            break
        except Exception as e:                          # noqa: BLE001 — ccxt Network/RateLimit/Exchange
            last_err = e
            if attempt == retries - 1:
                raise
            delay = backoff_base * (2 ** attempt)
            get_logger("market_data").warning(
                "fetch %s %s: %s — ретрай через %.0fс", symbol, timeframe, e, delay)
            _time.sleep(delay)
    else:                                               # pragma: no cover
        raise last_err  # type: ignore[misc]
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.iloc[:-1].reset_index(drop=True)          # последний бар может быть незакрыт


def load_frames(symbol: str, timeframes: list[str], limit: int = 200,
                exchange_id: str | None = None) -> dict[str, pd.DataFrame]:
    """Загрузить закрытые OHLCV по нескольким ТФ -> {tf: df} для SMCEngine.analyze.

    Каждый df содержит колонку dt (UTC) — временной гейт 6226 работает вживую.
    """
    return {tf: fetch_closed_ohlcv(symbol, tf, limit=limit, exchange_id=exchange_id)
            for tf in timeframes}


def load_correlated(symbol: str, timeframe: str = "1h", limit: int = 200,
                    exchange_id: str | None = None) -> tuple[pd.DataFrame, str] | None:
    """df коррелируемого символа для SMT (по таблице CORRELATIONS) или None, если пары нет."""
    from app.brain.smc_detectors import CORRELATIONS

    base = symbol.split("/")[0].replace(":USDT", "")
    for (a, b), (_coef, _kind) in CORRELATIONS.items():
        partner = None
        if base == a:
            partner = b
        elif base == b:
            partner = a
        if partner:
            pair = f"{partner}/USDT:USDT" if not partner.startswith(("US", "DXY", "XA")) else partner
            return fetch_closed_ohlcv(pair, timeframe, limit=limit,
                                      exchange_id=exchange_id), partner
    return None
