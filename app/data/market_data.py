"""
market_data.py — данные биржи и планировщик опроса.

Реализовано: fetch_closed_ohlcv (общий ccxt-инстанс, ретраи с бэкоффом, только
закрытые бары), fetch_closed_ohlcv_cached (старшие ТФ — кеш до закрытия их следующего
бара), load_frames (мультиТФ), load_correlated (SMT-партнёр),
seconds_to_next_close / poll_once / run_poller (такт по закрытию свечи).
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import pandas as pd

from app import clock

_TF_SECONDS = {"15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}

# Попытки фетча в такте поллера. 10006 у Bybit — толкучка всей биржи в первые ~40с после
# закрытия часа (пик :06–:12, к :40 ноль), а не наш темп: мы шлём ≤1 запрос/с (throttle
# ccxt = rateLimit 200мс × cost 5), BTC (первый запрос, :02) за 14 дней не упал ни разу,
# а часы 4h-закрытий при той же нашей нагрузке дают больше отказов. Три попытки (t, +2с,
# +6с) целиком лежат в горячем окне — дважды за 14 дней символ выпадал из такта целиком;
# четвёртая (+14с) уходит туда, где отказов почти нет (аудит надёжности 30.10).
_POLL_RETRIES = 4

# счётчики фетчей для итоговой строки такта: сколько реально запросов ушло, сколько
# старших ТФ отдал кеш и сколько раз биржа ответила rate-limit (их ретраи — INFO, не WARNING)
_FETCH_STATS = {"requests": 0, "rate_limited": 0, "cache_hits": 0}
_STATS_LOCK = threading.Lock()

# итог последнего poll_once — run_poller по нему ставит метку «данные биржи живы»
LAST_POLL_STATS: dict = {}


def _bump(key: str, n: int = 1) -> None:
    with _STATS_LOCK:
        _FETCH_STATS[key] += n


def _stats_snapshot() -> dict:
    with _STATS_LOCK:
        return dict(_FETCH_STATS)


def _is_rate_limit(e: Exception) -> bool:
    """Отказ по лимиту запросов (Bybit 10006/10018/403 -> ccxt.RateLimitExceeded/DDoSProtection).
    Строковый фолбэк — для обёрток, теряющих тип исключения."""
    try:
        import ccxt
        if isinstance(e, (ccxt.RateLimitExceeded, ccxt.DDoSProtection)):
            return True
    except ImportError:                                 # pragma: no cover — ccxt в зависимостях
        pass
    s = str(e)
    return "10006" in s or "Too many visits" in s


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

    def _default_loader(symbol_: str, tfs_: list) -> dict:
        # входной ТФ — свежий фетч с глубиной под скоринг старых открытых сигналов;
        # старшие ТФ (4h) читает ТОЛЬКО движок, и ровно ENGINE_WINDOW баров (см. ниже), —
        # им окно 200 и кеш до закрытия их следующего бара: 3 часа из 4 закрытые 4h-бары
        # те же самые, а запрос за ними попадал в горячие секунды после закрытия часа
        # (половина 10006 — фетчи 4h). Вход движка бит-в-бит прежний (аудит надёжности 30.10)
        out = {}
        for tf in tfs_:
            if tf == base_tf:
                out[tf] = fetch_closed_ohlcv(symbol_, tf, limit=_scoring_limit(symbol_, base_tf),
                                             retries=_POLL_RETRIES)
            else:
                out[tf] = fetch_closed_ohlcv_cached(symbol_, tf, limit=ENGINE_WINDOW + 1,
                                                    retries=_POLL_RETRIES)
        return out

    # инъектированный загрузчик (тесты) сохраняет двухаргументный контракт (symbol, tfs)
    loader = frames_loader or _default_loader
    engine = engine or SMCEngine()
    import time as _time

    started = _time.monotonic()
    LAST_POLL_STATS.clear()                     # упавший посреди такт не оставит старый «ок»
    stats_before = _stats_snapshot()
    n_ok = n_fail = 0
    found = []
    bars_by_symbol = {}
    for i, symbol in enumerate(symbols):
        if pause_sec and i:
            _time.sleep(pause_sec)                      # разрядка запросов — не дразнить rate-limit
        loaded = False
        try:
            frames = loader(symbol, list(timeframes))
            loaded = True
            n_ok += 1
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
            if not loaded:                              # счётчики — про загрузку баров, не скан
                n_fail += 1
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
            n_ok += 1
        except Exception as e:                          # noqa: BLE001 — один сирота не роняет цикл
            n_fail += 1
            log.warning("orphan-скоринг %s: %s", symbol, e)
    # итог такта одной строкой: вместо россыпи WARNING про штатные 10006 — счётчики, по
    # которым видно и толкучку биржи, и пользу кеша старших ТФ. ok/failed — символы, чьи
    # бары загрузились / не загрузились (сбой самого скана виден отдельно, WARNING выше)
    stats_after = _stats_snapshot()
    LAST_POLL_STATS.clear()
    LAST_POLL_STATS.update(
        ok=n_ok, failed=n_fail, seconds=round(_time.monotonic() - started, 1),
        **{k: stats_after[k] - stats_before[k] for k in stats_after})
    log.info("такт: символов ок %d, сбой %d; запросов %d, старший ТФ из кеша %d, "
             "rate-limit %d; %.0fс", n_ok, n_fail, LAST_POLL_STATS["requests"],
             LAST_POLL_STATS["cache_hits"], LAST_POLL_STATS["rate_limited"],
             LAST_POLL_STATS["seconds"])
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
                late_entry_gate=runtime.late_entry_gate(),
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
                _bump("requests")
                raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            break
        except Exception as e:                          # noqa: BLE001 — ccxt Network/RateLimit/Exchange
            last_err = e
            limited = _is_rate_limit(e)
            if limited:
                _bump("rate_limited")
            if attempt == retries - 1:
                raise
            delay = backoff_base * (2 ** attempt)
            # отказ по лимиту в секунды после закрытия бара — штатная толкучка биржи, которую
            # ретрай гасит: INFO (счётчик — в итоговой строке такта), иначе ~30 WARNING в сутки
            # вытесняли настоящие проблемы из хвоста лога в админ-панели. Прочие сбои и
            # окончательный отказ («poll SYM») — по-прежнему WARNING
            get_logger("market_data").log(
                logging.INFO if limited else logging.WARNING,
                "fetch %s %s: %s — ретрай через %.0fс", symbol, timeframe, e, delay)
            _time.sleep(delay)
    else:                                               # pragma: no cover
        raise last_err  # type: ignore[misc]
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.iloc[:-1].reset_index(drop=True)          # последний бар может быть незакрыт


def _now_ts() -> float:
    """Часы кеша старших ТФ (точка подмены в тестах)."""
    import time as _time

    return _time.time()


def last_closed_open_ms(tf: str, now_ts: float) -> int:
    """Время ОТКРЫТИЯ последнего закрытого бара ТФ на момент now_ts, мс (UTC-выравнивание
    баров Bybit: 4h — 00/04/08…, 1d — 00:00)."""
    period = _TF_SECONDS[tf]
    return (int(now_ts // period) * period - period) * 1000


# (биржа, символ, ТФ) -> (limit, df): закрытые бары старших ТФ между их закрытиями
_HTF_CACHE: dict[tuple, tuple[int, pd.DataFrame]] = {}
_HTF_LOCK = threading.Lock()


def fetch_closed_ohlcv_cached(symbol: str, timeframe: str, limit: int = 200,
                              exchange_id: str | None = None, now_ts: float | None = None,
                              retries: int = 3, backoff_base: float = 2.0) -> pd.DataFrame:
    """fetch_closed_ohlcv с кешем до закрытия СЛЕДУЮЩЕГО бара этого ТФ.

    Кеш самопроверяемый: копия отдаётся, только если её последний бар — ровно тот, что
    должен быть последним закрытым сейчас (last_closed_open_ms), и она снята с limit не
    меньше запрошенного. Тогда она совпадает с тем, что вернул бы свежий фетч (закрытый
    бар биржа не переписывает), и движок видит прежний вход бит-в-бит. Ответ, в котором
    биржа ещё не выкатила новый бар (запаздывание на закрытии), в кеш не кладётся — его
    отдаём как есть, как и без кеша. Сбитые часы мака -> промах кеша -> обычный фетч."""
    if exchange_id is None:
        from app.config import get_settings
        exchange_id = get_settings().exchange_id
    now_ts = _now_ts() if now_ts is None else now_ts
    want = last_closed_open_ms(timeframe, now_ts)
    key = (exchange_id, symbol, timeframe)
    with _HTF_LOCK:
        hit = _HTF_CACHE.get(key)
    if hit is not None:
        cached_limit, cached = hit
        if cached_limit >= limit and len(cached) and int(cached["ts"].iloc[-1]) == want:
            _bump("cache_hits")
            # копия: потребитель не должен мочь испортить кеш на следующие часы
            return cached.tail(max(limit - 1, 0)).reset_index(drop=True).copy()
    df = fetch_closed_ohlcv(symbol, timeframe, limit=limit, exchange_id=exchange_id,
                            retries=retries, backoff_base=backoff_base)
    if len(df) and int(df["ts"].iloc[-1]) == want:
        with _HTF_LOCK:
            _HTF_CACHE[key] = (limit, df.copy())
    return df


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


_VENUE_CACHE: dict = {}


def venue_symbols(venue: str, sandbox: bool = True, ttl_sec: float = 6 * 3600) -> set[str]:
    """Линейные USDT-свопы площадки исполнения ('bingx') — авто-watchlist берёт только их.

    Решение владельца 27.10.2026: сигналы — только по монетам, которые реально торгуются
    там, где стоит исполнение (демо BingX); иначе журнал копит сделки, которые executor
    повторить не может (plan() пропускает их как «нет на BingX»). sandbox=True — тот же
    список контрактов, что видит демо-executor (VST). Публичный эндпоинт, ключи не нужны;
    кеш на ttl_sec — список контрактов меняется редко."""
    import time as _time

    import ccxt

    key = (venue, sandbox)
    hit = _VENUE_CACHE.get(key)
    if hit is not None and _time.monotonic() - hit[0] < ttl_sec:
        return hit[1]
    ex = getattr(ccxt, venue)({"timeout": 20000, "options": {"defaultType": "swap"}})
    if sandbox:
        ex.set_sandbox_mode(True)
    ex.load_markets()
    syms = {s for s, m in ex.markets.items()
            if m.get("swap") and m.get("linear") and s.endswith("/USDT:USDT")
            and m.get("active", True) is not False}
    _VENUE_CACHE[key] = (_time.monotonic(), syms)
    return syms


def top_liquid_symbols(n: int, blacklist: tuple = (), exchange_id: str | None = None,
                       min_quote_volume: float = 10_000_000.0,
                       exchange=None, allowed: set | None = None) -> list[str]:
    """Топ-N ликвидных USDT-перпетуалов по суточному объёму (авто-watchlist, мега-план MAX).

    Фильтры: только linear-свопы "*/USDT:USDT", объём 24ч не ниже min_quote_volume USD,
    не из blacklist, и (allowed не None) есть на площадке исполнения — фильтр ДО отсечения
    топ-N, чтобы недоступная монета не занимала место в списке. exchange= — инъекция для
    тестов. Сортировка по quoteVolume убыв."""
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
        if allowed is not None and sym not in allowed:
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
