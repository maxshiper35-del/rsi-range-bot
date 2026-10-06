"""
market_data.py — данные биржи и планировщик опроса.

Реализовано: fetch_closed_ohlcv (общий ccxt-инстанс, ретраи с бэкоффом, только
закрытые бары), load_frames (мультиТФ), load_correlated (SMT-партнёр),
seconds_to_next_close / poll_once / run_poller (такт по закрытию свечи).
"""
from __future__ import annotations

import threading
from collections.abc import Callable

import pandas as pd

from app import clock

_TF_SECONDS = {"15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


def seconds_to_next_close(tf: str, now_ts: float) -> float:
    """Секунд до закрытия текущей свечи ТФ (UTC-выравнивание). Всегда в (0, period]."""
    period = _TF_SECONDS.get(tf)
    if period is None:
        raise ValueError(f"неизвестный таймфрейм: {tf!r} (жду {sorted(_TF_SECONDS)})")
    rem = period - (now_ts % period)
    return rem if rem > 0 else float(period)


# окно движка: limit=200 минус формирующийся бар — так снят и бэктест (dump_signals --prod)
ENGINE_WINDOW = 199


def _scoring_limit(symbol: str, tf: str) -> int:
    """Глубина фетча для скоринга: если у символа есть старый нескорённый сигнал (филл или
    создание за левым краем стандартных 200 баров), обрезанное окно даёт слепые зоны —
    пропущенный стоп в дыре флипал бы исход (аудит-2 математики [5]). -> [200..1000]."""
    from datetime import datetime, timezone

    from app.brain.journal import oldest_open_anchor
    try:
        anchor = oldest_open_anchor(symbol)
        if not anchor:
            return 200
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(anchor)).total_seconds()
        need = int(age / _TF_SECONDS.get(tf, 3600)) + 80    # запас на окно ожидания/лаг
        if need > 1000:
            # кап сработал: якорь старше окна -> сигнал не скорится ВООБЩЕ, публичная
            # статистика молча недосчитывает исход (аудит 02.09). Warning вместо
            # тишины: виден в bot.log и в ERROR-буфере не всплывает (не спам)
            from app.logging_conf import get_logger
            get_logger("market_data").warning(
                "скоринг %s %s: нужно %d баров, кап 1000 — старый сигнал вне окна "
                "не скорится (якорь %s)", symbol, tf, need, anchor)
        return min(1000, max(200, need))
    except Exception:                                       # noqa: BLE001 — глубина не роняет цикл
        return 200


def poll_once(symbols: list, timeframes: list, engine=None,
              frames_loader: Callable | None = None, pause_sec: float = 0.0,
              mode: str | None = None, scan_new: bool = True) -> tuple[list, list]:
    """Один цикл сканирования: по каждому символу загрузить кадры и записать сигналы.

    mode: 'auto' — сигнал сразу активен (журнал+статистика); 'semiauto' — кандидат
    на подтверждение оператором. None -> из Settings.signal_mode.
    Возвращает список (Signal, signal_id). Ошибка по одному символу — лог + skip.
    scan_new=False — режим паузы: новые сигналы не ищем, но открытые сделки ДОСКАРИВАЕМ
    (филлы/стопы/трейл). Пауза без скоринга оставляла открытые сигналы нескорёнными:
    дашборд считал «плавающий» R по давно закрытым сделкам (13.09–04.10.2026: −86R на
    панели при реальных +0.66R тех же 12 сделок).
    """
    from app.brain.smc_setups import SMCEngine
    from app.delivery.bot import scan_auto, scan_semiauto
    from app.logging_conf import get_logger

    if mode is None:
        from app import runtime
        mode = runtime.signal_mode()
    scan = scan_auto if mode == "auto" else scan_semiauto

    log = get_logger("poller")
    base_tf = min(timeframes, key=lambda t: SMCEngine.TF_MINUTES.get(t, 10 ** 9))
    # дефолтный загрузчик углубляет фетч под старые нескорённые сигналы символа;
    # инъектированный (тесты) сохраняет двухаргументный контракт (symbol, tfs)
    loader = frames_loader or (
        lambda s, tfs_: load_frames(s, tfs_, limit=_scoring_limit(s, base_tf)))
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
            if scan_new:
                # движку — ровно ENGINE_WINDOW закрытых баров: углублённый под скоринг фетч
                # (_scoring_limit, до 999) менял сигналы (BTC 1h, 5 лет: 199 баров — 877,
                # 999 — 1038, общих 656) и делал их зависимыми от чужой открытой позиции
                engine_frames = {tf: df.tail(ENGINE_WINDOW).reset_index(drop=True)
                                 for tf, df in frames.items()}
                found.extend(scan(symbol, engine_frames, engine))  # (Signal, sid) на каждый ТФ
        except Exception as e:                          # noqa: BLE001 — один символ не роняет цикл
            log.warning("poll %s: %s", symbol, e)
    # осиротевшие позиции: символы с открытыми сигналами, ВЫПАВШИЕ из вотчлиста
    # (авто-ротация или ручное удаление) — их исход всё равно надо доскорить свежими
    # барами, иначе сигнал висит open вечно и публичная статистика недосчитывает
    # закрытых сделок. Только СКОРИНГ (никакого scan) — новые сигналы по ним не ищем.
    from app.brain.journal import symbols_with_open_positions
    for symbol in symbols_with_open_positions():
        if symbol in bars_by_symbol:
            continue
        if pause_sec:
            _time.sleep(pause_sec)
        try:                                            # loader, не fetch — инъектится в тестах
            bars_by_symbol[symbol] = loader(symbol, [base_tf])[base_tf]
        except Exception as e:                          # noqa: BLE001 — один сирота не роняет цикл
            log.warning("orphan-скоринг %s: %s", symbol, e)
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
            # такт "проснулся по расписанию" — сигнал живости для /health, не зависит от
            # успеха дальнейшей работы (скан/on_tick могут упасть, поток жив в любом случае)
            await asyncio.to_thread(runtime.set_last_poller_tick_at, clock.utcnow_iso())

            paused = runtime.paused()
            if paused:        # пауза: новые сигналы не ищем, открытые сделки ведём до конца
                log.info("poller на паузе — новые сигналы не ищу, открытые сопровождаю")
            eng = engine or SMCEngine(SMCConfig(
                min_confidence=runtime.min_confidence(),
                min_impulse_atr=runtime.min_impulse_atr(),
                min_rr_open_model=runtime.min_rr_open_model(),
                enabled_setups=runtime.enabled_setups(),
                htf_block_countertrend=runtime.htf_block_countertrend(),
                min_stop_pct=runtime.min_stop_pct(),
                use_time_filter=runtime.time_filter_enabled()))
            found, closed = await asyncio.to_thread(
                poll_once, runtime.symbols(), tfs, eng, None, 1.5, scan_new=not paused)
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
            # backoff обязателен: если исключение прилетело ДО планового sleep (напр.
            # runtime.timeframes() при временно недоступной БД), без паузы цикл крутится
            # в hot-loop на 100% CPU и заваливает лог трейсбеками (аудит [9])
            log.exception("такт poller упал — пауза 30с, продолжаю со следующей свечи")
            await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# Мультитаймфрейм и SMT-партнёр
# ---------------------------------------------------------------------------
# синхронный ccxt НЕ потокобезопасен, а инстанс общий для поллера (to_thread) и хендлеров
# (график/гейт): гонка учёта троттлера недодерживала паузы ровно при совпадении рендера
# с тактом -> залпы и 10006 (318 ретраев за 5.5 дней, аудит-2 ops [3][4])
_EXCHANGE_LOCK = threading.Lock()


def _exchange(exchange_id: str):
    """ОДИН ccxt-инстанс на биржу (кэш): встроенный rate-limiter ccxt работает только
    внутри инстанса — новый объект на каждый вызов даёт залп запросов и 10006 от Bybit.
    Вызовы fetch_* на инстансе — только под _EXCHANGE_LOCK."""
    import ccxt  # ленивый импорт

    cache = getattr(_exchange, "_cache", None)
    if cache is None:
        cache = _exchange._cache = {}  # type: ignore[attr-defined]  # кэш-на-функции — намеренно
    if exchange_id not in cache:
        cache[exchange_id] = getattr(ccxt, exchange_id)({
            "enableRateLimit": True,
            # 200мс между запросами (дефолт bybit ~20мс): на этом IP ещё три бота —
            # общий лимит биржи выбирается залпами (аудит-2 ops [3])
            "rateLimit": 200,
            # явный сетевой таймаут: дефолт ccxt (10с) неявен, а зависший запрос без
            # таймаута мог бы подвесить синхронный fetch в to_thread на минуты и заморозить
            # такт поллера — watchdog это заметит, но лучше не допускать
            "timeout": 15000,
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
            with _EXCHANGE_LOCK:                        # sync-ccxt не потокобезопасен
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


def fetch_history(symbol: str, timeframe: str, target_bars: int,
                  exchange_id: str | None = None, page_limit: int = 1000,
                  retries: int = 3, backoff_base: float = 2.0,
                  exchange=None) -> pd.DataFrame:
    """Постраничная история OHLCV (since-курсор): target_bars закрытых баров для бэктеста.

    Одиночный fetch_ohlcv у Bybit ограничен ~1000 барами — для гейта F/G (бриф: сравнение
    ТФ на 6-12 месяцах) нужна пагинация. Страницы ретраятся как в fetch_closed_ohlcv;
    rate-limiter — встроенный ccxt (общий кэш-инстанс). exchange= — инъекция для тестов.
    """
    import time as _time

    from app.logging_conf import get_logger

    if timeframe not in _TF_SECONDS:
        raise ValueError(f"неизвестный ТФ {timeframe!r} (знаю: {sorted(_TF_SECONDS)})")
    if exchange is None:
        if exchange_id is None:
            from app.config import get_settings
            exchange_id = get_settings().exchange_id
        exchange = _exchange(exchange_id)
    tf_ms = _TF_SECONDS[timeframe] * 1000
    since = int(_time.time() * 1000) - (target_bars + 2) * tf_ms
    rows: list = []
    while len(rows) < target_bars + 1:                  # +1: последний бар отбросим как незакрытый
        batch = None
        for attempt in range(retries):
            try:
                with _EXCHANGE_LOCK:                    # sync-ccxt не потокобезопасен
                    batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe,
                                                 since=since, limit=page_limit)
                break
            except Exception as e:                      # noqa: BLE001 — ccxt Network/RateLimit
                if attempt == retries - 1:
                    raise
                delay = backoff_base * (2 ** attempt)
                get_logger("market_data").warning(
                    "fetch_history %s %s: %s — ретрай через %.0fс", symbol, timeframe, e, delay)
                _time.sleep(delay)
        if not batch:
            break                                       # история кончилась (листинг моложе окна)
        if rows:
            batch = [b for b in batch if b[0] > rows[-1][0]]    # без дублей на стыке страниц
            if not batch:
                break
        rows.extend(batch)
        since = rows[-1][0] + tf_ms
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.iloc[:-1].tail(target_bars).reset_index(drop=True)  # закрытые, не больше цели


def top_liquid_symbols(n: int, blacklist: tuple = (), exchange_id: str | None = None,
                       min_quote_volume: float = 10_000_000.0,
                       exchange=None) -> list[str]:
    """Топ-N ликвидных USDT-перпетуалов по суточному объёму (авто-watchlist, мега-план MAX).

    Фильтры: только linear-свопы "*/USDT:USDT", объём 24ч не ниже min_quote_volume USD,
    не из blacklist. exchange= — инъекция для тестов. Сортировка по quoteVolume убыв."""
    if exchange is None:
        if exchange_id is None:
            from app.config import get_settings
            exchange_id = get_settings().exchange_id
        exchange = _exchange(exchange_id)
    with _EXCHANGE_LOCK:                                # sync-ccxt не потокобезопасен
        tickers = exchange.fetch_tickers()
    ranked = []
    for sym, t in tickers.items():
        if not sym.endswith("/USDT:USDT") or sym in blacklist:
            continue
        vol = t.get("quoteVolume")
        if vol is None or vol < min_quote_volume:
            continue
        ranked.append((vol, sym))
    ranked.sort(reverse=True)
    return [sym for _, sym in ranked[:n]]


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
            is_index = partner.startswith(("US", "DXY", "XA"))
            pair = partner if is_index else f"{partner}/USDT:USDT"
            return fetch_closed_ohlcv(pair, timeframe, limit=limit,
                                      exchange_id=exchange_id), partner
    return None
