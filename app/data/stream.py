"""
stream.py — реалтайм-поток Bybit v5 (public linear WebSocket): kline-стрим.

Назначение v1 (мега-план MAX, «скорость сигнала»): мгновенное «вход исполнен» —
касание entry видно внутри бара, а не на закрытии (выигрыш до часа на 1h ТФ).
ОФИЦИАЛЬНЫЙ скоринг исходов НЕ меняется: bar-based «стоп первым» остаётся
единственным судьёй результата (контракт трек-рекорда), poller — бэкстопом
для филлов (guard filled_at IS NULL исключает дубли в обе стороны).

Состав подписки (#881): вотчлист целиком + монеты открытых публичных сигналов сверх него
(extra_symbols, не больше EXTRA_SYMBOLS_MAX). Монета, вышедшая из вотчлиста при открытом
сигнале, остаётся в топиках: по кадрам стрима журнал засчитывает вход, а executor ставит
биржевой трейл.

Зависимостей нет: aiohttp уже приходит с aiogram.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass

WS_URL = "wss://stream.bybit.com/v5/public/linear"

# наш лейбл ТФ -> интервал Bybit v5
_BYBIT_INTERVAL = {"1m": "1", "5m": "5", "15m": "15", "30m": "30",
                   "1h": "60", "4h": "240", "1d": "D"}

# сек: пересобрать соединение (подхватить смену состава — вотчлист и открытые сигналы)
_RECONNECT_MAX_AGE = 900.0
_PING_EVERY = 20.0              # Bybit требует ping не реже 20с
# Потолок монет СВЕРХ вотчлиста в подписке (#881). По логу с 30.10 монет с открытыми
# сигналами вне вотчлиста обычно 1–3 (docs/research/2026-11-10-trail-activation-881.md);
# потолок — страховка от разрастания подписки, а не рабочий режим
EXTRA_SYMBOLS_MAX = 10


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
    close: float            # последняя цена тика — для кроссинг-семантики прайс-алертов
    confirm: bool           # True = бар закрыт
    # открытие бара, мс (поле Bybit 'start'): executor отличает бар филла (его high/low
    # включают цену ДО входа) от баров целиком после филла; 0 — поле не пришло
    start: int = 0


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
                         close=float(d["close"]),
                         confirm=bool(d.get("confirm")),
                         start=int(d.get("start") or 0))
                for d in msg.get("data", [])]
    except Exception:                                   # noqa: BLE001
        return []


def entry_touched(entry: float, high: float, low: float) -> bool:
    """Лимитка касается диапазона бара — то же правило, что в bar-скоринге journal."""
    return low <= entry <= high


def extra_symbols(watchlist: list, open_symbols: list,
                  cap: int = EXTRA_SYMBOLS_MAX) -> tuple[list, list]:
    """Монеты открытых сигналов, которые стрим слушает СВЕРХ вотчлиста (#881).

    watchlist — вотчлист (unified): в подписку он идёт всегда целиком. open_symbols — монеты
    публичных открытых сигналов в порядке приоритета (journal.open_signal_symbols: сначала
    с позицией, внутри — от свежего сигнала к старому). -> (добавка, не вошедшие). Добавка —
    монеты вне вотчлиста в том же порядке, не больше cap. Не вошедшие — остаток сверх потолка:
    стрим их не слушает, как было до правки (вход засчитает часовой такт, активацию трейла
    поймает опрос цены раз в минуту). Сравнение — по топику Bybit: монета вотчлиста в добавку
    не попадает, и ни одна монета не идёт в подписку дважды. Без сети и БД."""
    seen = {ws_symbol(s) for s in watchlist}
    extra: list = []
    skipped: list = []
    for symbol in open_symbols:
        key = ws_symbol(symbol)
        if key in seen:
            continue
        seen.add(key)
        (extra if len(extra) < cap else skipped).append(symbol)
    return extra, skipped


_REJECT_TOPIC = re.compile(r"topic:([A-Za-z0-9_.]+)")


def rejected_topic(raw: str) -> str | None:
    """Кадр — отказ операции Bybit? -> топик из ответа; '' — отказ без топика (не подписка);
    None — не отказ. На недействительный топик биржа отвечает (снято вживую 10.11):
    {"success":false,"ret_msg":"error:handler not found,topic:kline.60.<SYMBOL>",
    "op":"subscribe"}."""
    if '"success":false' not in raw.replace(" ", ""):
        return None
    m = _REJECT_TOPIC.search(raw)
    return m.group(1) if m else ""


def composition_line(n_topics: int, extra: list, skipped: list) -> str:
    """Строка лога о составе подписки: сколько топиков и какие монеты сверх вотчлиста.
    run_stream пишет её только при смене состава — по ней при разборе видно, слушал ли
    стрим монету в нужный час (у #881 состав восстанавливали по косвенным признакам)."""
    line = f"стрим: состав подписки — {n_topics} топиков, сверх вотчлиста "
    line += f"{len(extra)}: {', '.join(extra)}" if extra else "нет"
    if skipped:                     # непуст только при полной добавке: len(extra) — потолок
        line += f"; без подписки (потолок {len(extra)}): {', '.join(skipped)}"
    return line


async def run_stream(get_symbols, get_tf, on_intrabar, enabled, log,
                     get_open_symbols=None) -> None:
    """Вечный цикл стрима: connect -> subscribe kline.{tf}.{sym} -> события -> reconnect.

    get_symbols() -> list[str] (unified) — вотчлист, get_tf() -> str (наш лейбл, напр. '1h'),
    on_intrabar(symbol_ws, high, low, close, start_ms) -> awaitable, enabled() -> bool
    (флаг runtime, выключение подхватывается не позже _RECONNECT_MAX_AGE), log — логгер.
    get_open_symbols() -> list[str] (unified) — монеты открытых публичных сигналов в порядке
    приоритета (сначала с позицией): те, что вне вотчлиста, идут в подписку сверх него
    (extra_symbols); None — только вотчлист. Состав пересчитывается при КАЖДОМ подключении,
    поэтому монета с открытым сигналом не теряет подписку, когда выходит из вотчлиста.
    Обработчик получает кадры и таких монет — «кадр = монета вотчлиста» больше не верно.
    Ошибки цикла гасятся с бэкоффом — стрим никогда не роняет процесс."""
    import aiohttp

    backoff = 1.0
    composition = None              # состав прошлой подписки: строка о нём — только при смене
    reported: set = set()           # топики вотчлиста, об отказе по которым уже был ERROR
    while True:
        try:
            # чек флага ВНУТРИ try: enabled() — синхронное чтение SQLite, редкий
            # OperationalError (гонка с ночным бэкапом) вне try убивал таску навсегда
            # (аудит-2 ops [2]) — реалтайм-филлы молча деградировали до бар-скоринга
            if not enabled():
                await asyncio.sleep(30)
                continue
            symbols = get_symbols()
            extra, skipped = extra_symbols(
                symbols, get_open_symbols() if get_open_symbols is not None else [])
            interval = bybit_interval(get_tf())
            topics = [f"kline.{interval}.{ws_symbol(s)}" for s in symbols]
            extra_topics = [f"kline.{interval}.{ws_symbol(s)}" for s in extra]
            watch_topics = dict(zip(topics, symbols))   # топик -> монета вотчлиста
            planned = False                             # False -> сервер закрыл (close/error)
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(WS_URL, heartbeat=None) as ws:
                    # каждый топик — своим запросом: запрос с одним недействительным топиком
                    # Bybit отклоняет ЦЕЛИКОМ (проверено вживую 10.11 — вотчлист из трёх монет
                    # с одной несуществующей не получил ни кадра). Монета, снятая с торгов,
                    # или опечатка в /addsymbol оставляли без стрима весь вотчлист: ни
                    # реалтайм-входов, ни активации трейла. Теперь без кадров остаётся только
                    # сама такая монета, а отказ по ней виден в логе (ниже)
                    for topic in topics + extra_topics:
                        await ws.send_json({"op": "subscribe", "args": [topic]})
                    n_topics = len(topics) + len(extra_topics)
                    log.info("стрим подключён: %d топиков (%s)", n_topics, interval)
                    subscribed = (frozenset(topics + extra_topics), frozenset(extra),
                                  frozenset(skipped))
                    if subscribed != composition:       # не при каждом плановом reconnect
                        composition = subscribed
                        line = composition_line(n_topics, extra, skipped)
                        if skipped:                     # потолок оставил монеты без стрима
                            log.warning("%s", line)
                        else:
                            log.info("%s", line)
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
                        # а филлы не приходят (аудит-2 ops [5]) — теперь виден в логе. Отказ по
                        # монете ВОТЧЛИСТА — ERROR (уходит админам), раз на топик за процесс:
                        # по ней нет ни реалтайм-входа, ни активации трейла, и сама она это не
                        # исправит. Повторы на переподключениях и отказ по добавке — WARNING
                        bad = rejected_topic(frame.data)
                        if bad is not None:
                            if bad in watch_topics and bad not in reported:
                                reported.add(bad)
                                log.error(
                                    "стрим: Bybit отклонил подписку на монету вотчлиста %s (%s)"
                                    " — кадров по ней нет: реалтайм-вход и активация трейла по "
                                    "стриму для неё не работают. Проверьте, торгуется ли монета,"
                                    " и уберите её из вотчлиста, если нет",
                                    watch_topics[bad], bad)
                            else:
                                log.warning("стрим: отказ операции: %.200s", frame.data)
                        for evt in parse_kline(frame.data):
                            await on_intrabar(evt.symbol_ws, evt.high, evt.low, evt.close,
                                              evt.start)
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
