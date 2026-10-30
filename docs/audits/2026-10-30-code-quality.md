# Аудит качества кода и архитектуры — 30.10.2026

**Срез:** `c6df9b0` (master). Это только документ, код не менялся. Номера строк даны по этому коммиту.
Пока писался отчёт, master ушёл на `d129b10`: в него влиты игра и правки платежей. Что изменилось
и как пересчитать номера строк `bot.py`, сказано в §6.

**Что делалось:**
- чтение кода;
- AST-сканы: синхронные вызовы внутри `async def`, вызовы внутри `db.transaction()`, разбивка
  `main()` по доменам, покрытие по функциям;
- `ruff check` (чисто) плюс правила C901/BLE/S110/SIM105;
- `mypy app` (чисто) и `mypy --check-untyped-defs` (9 ошибок);
- шесть прогонов `pytest` в изоляции: заглушка dotenv, своя пустая или мигрированная БД,
  сетевой страж;
- построчное покрытие трассировщиком из stdlib: `pytest-cov`/`coverage` в venv нет, а ставить
  пакеты в прод-venv нельзя.

Все вспомогательные скрипты лежат вне репозитория (см. приложение).

**Чего не делалось:**
- `.env` и `data/signals.db` прода не читались;
- Telegram и приватные API бирж не вызывались, прод не перезапускался.

Оговорка: в трёх ранних прогонах, до того как появился сетевой страж, пять тестов из
`tests/test_webapp.py` сами обращались к публичному `api.bybit.com` (load_markets/fetch_tickers,
без ключей). Это одна из находок, см. §4.1.

---

## 0. Главное за минуту

1. **Тесты и скрипты из git-worktree видят прод.** Агенты работают в `.claude/worktrees/…` внутри
   прод-каталога. `load_dotenv()` (`app/config.py:69-71`) ищет `.env` вверх по дереву и находит
   **`/Users/mac/bot ctypto/.env`**: проверено, `find_dotenv()` из worktree возвращает именно его.
   Четыре теста без `fresh_db` читают ту БД, на которую указывает `DB_PATH`. В прод-каталоге это
   живая `data/signals.db`, на пустой БД эти тесты падают. Пять тестов webapp ходят в сеть.
   Это самое дешёвое и самое важное исправление (§4.1, топ-1).
2. **`app/delivery/bot.py` — 5 329 строк.**
   - `main()` — одно замыкание на 3 598 строк (McCabe 660, при пороге 15). Внутри 136 `async def`,
     101 хэндлер и 115 регистраций.
   - **Покрытие `main()` — 0 %** (2 908 исполняемых строк). Код модульного уровня покрыт на 93 %.
   - Этот файл трогали 30 из 96 коммитов за месяц. Игра (`57c0852`) уже влита: на `d129b10`
     bot.py — 5 498 строк и 117 регистраций. Ветка «Мой счёт» (`5fa7f05`) добавит ещё ~170 строк.
   - План распила — в §2: 5 фаз, ≈7–9 дней. Каждый шаг — чистый перенос кода, тесты при этом
     остаются зелёными.
3. **Деньги: функции покрыты, связки между ними — нет.** Доменные функции `payments.py` покрыты
   на 93 %. Оркестрация в хэндлерах не покрыта вообще: Stars-инвойс → pre_checkout →
   successful_payment, `/refund`, paid media, USDT-тиры. Тарифы описаны в **четырёх** местах, а
   pre_checkout сверяет дни по равенству float — любое расхождение молча отклонит оплату.
   Нижнюю границу длительности подписки задают три точки входа: две пропускают `sub_days=0`,
   только webapp запрещает. Кто считается «платящим», тоже определено в нескольких местах.
   После `d129b10` юзер, у которого единственная оплата — ручной USDT через `/grant`, перестаёт
   получать напоминания о продлении (§6).
4. **В обслуживании нет переборок.** `_maintenance` (460 строк, McCabe 85) — одна корутина. Если
   ранний блок падает устойчиво, выключаются все следующие обязанности: активация USDT,
   истечение подписок, дрип, табло, недельные посты. Вместе с ними пропадает и пульс dead-man.
5. **Настоящие, хотя и мелкие, баги:**
   - `/post` и `/growth` берут канал из `.env`, а не из runtime-настройки;
   - две фоновые задачи создаются без сильной ссылки (`_send_digests`, `_push_alerts`), их может
     собрать GC;
   - `_task_died` делает синхронный HTTP прямо в event loop;
   - `/calc` печатает объём мелких альтов в экспоненте;
   - половина кнопок меню не сбрасывает FSM-диалог;
   - кнопка USDT показывает «30.0 дн».
6. **Мусор в репозитории.** Около 12 мёртвых модулей и фрагментов: заглушки «Fable 5»,
   `crypto.py`, `errors.py`, легаси-движок EMA/RSI, `scan_watchlist`. В git лежит бинарник
   cloudflared на 41,7 МБ. Docker-конфиг при запуске поднимет второй поллер — это тот же класс
   инцидента, что дубли постов 27–29.08.

Что уже хорошо и что стоит сохранить при рефакторинге:
- все записи в БД из async-кода идут через `to_thread` (AST-скан не нашёл ни одной прямой записи);
- предикаты расписания вынесены в чистые функции и протестированы (`should_*`, `*_is_fresh`);
- маркеры идемпотентности ставятся «до отправки»;
- `signal_events` — append-only;
- `bingx_executor.plan()` — чистая функция;
- в транзакциях нет реентрантных записей, способных вызвать дедлок на `threading.Lock`;
- комментарии со ссылками на инциденты — ценный институциональный слой.

---

## 1. Карта архитектуры

### 1.1 Процессы и циклы

```
                         launchd, gui-домен владельца (Mac, 16 CPU, Python 3.11.15 .venv)
┌──────────────────────────────── com.rsirange.bot ─────────────────────────────────┐
│ app.delivery.bot:main() → db.init_db() → Bot/Dispatcher → asyncio.run(_main)       │
│  ├─ dp.start_polling ── 101 хэндлер: команды, кнопки меню, FSM, платежи, inline    │
│  ├─ task «poller»  ── market_data.run_poller: такт = закрытие младшей свечи (1h)   │
│  │     to_thread(poll_once): OHLCV (ccxt Bybit под _EXCHANGE_LOCK) → SMCEngine →   │
│  │     scan_auto (гейты) → journal.record_signal → update_outcomes_from_bars       │
│  │     → _broadcast/_deliver_signal (канал + DM) → _on_close (посты цикла сделки) │
│  ├─ task «stream»  ── data.stream.run_stream: Bybit WS kline (флаг stream_enabled) │
│  │     → _on_intrabar: прайс-алерты; try_mark_filled → пост «вход исполнен»        │
│  ├─ task «maintenance» ── каждые 60 с: _maintenance() → executor.reconcile →        │
│  │     пинг healthchecks → forward/exec-отчёты → og-карточка → сброс ERROR-буфера │
│  └─ фоновые: inline-прогрев, дайджест, видео недели, разбор дня, контент-пак      │
└───────────────┬───────────────────────────────────────────────────────────────────┘
                │ SQLite WAL data/signals.db   (+ data/og.png, logs/bot.log как каналы)
┌───────────────┴──────── com.rsirange.webapp ─────┐   ┌──── com.rsirange.tunnel ────┐
│ waitress 127.0.0.1:8091 → app.webapp (Flask):    │◄──┤ cloudflared quick-tunnel +  │
│ Mini App, публичный API, админ-пульт (пишет      │   │ сторож; новый URL пишет в   │
│ settings / users / user_taken)                    │   │ settings.webapp_url         │
└──────────────────────────────────────────────────┘   └─────────────────────────────┘
com.rsirange.backup (02:00) → scripts.backup_db: data/backups (14) + iCloud (7) + ротация логов
```

### 1.2 Модули и ответственность

| Пакет | Файлы (строк) | Ответственность | Замечания |
|---|---|---|---|
| `app/core` | signal_engine (373) | контракт `Signal`/`Side`/`Indicators`, `calculate_position` | рядом лежат легаси-движок EMA/RSI (`SignalEngine`, `StrategyConfig`) и свой `fetch_ohlcv`, в проде мёртвые |
| `app/brain` | smc_detectors (512), smc_setups (758), smc_time (74), journal (1158), forward_check (126), risk (19) | стратегия SMC; журнал как источник правды: запись, судья исходов по барам, статистика, экспорт | `journal` совмещает запись, судью, 8 агрегаторов и выборки для контента |
| `app/data` | market_data (417), stream (134) | ccxt-данные, поллер, топ ликвидности; WS-стрим Bybit | `poll_once` импортирует `scan_auto` из `delivery.bot` (инверсия слоёв, §1.5) |
| `app/delivery` | bot (5329), payments (912), templates (555), graphics (686), signal_check (344), replay (382), weekly_video (236), content_pack (218), postmortem (157), soundtrack (154), market_card (104), alerts (85), keyboards (7, заглушка) | Telegram-UI, пользователи, биллинг, воронка, тексты, PNG/MP4, проверка чужих сигналов, прайс-алерты | bot.py — монолит; `payments.py` = пользователи + биллинг + квоты воронки + настройки уведомлений + аналитика роста |
| `app/sim` | backtest (286), portfolio (383), paper (20, заглушка) | бэктест символа, портфельный симулятор | |
| `app/v2` | bingx_executor (1235), executor (20, заглушка), tradingview_webhook (21, заглушка) | демо-исполнение BingX (VST) как зеркало журнала | |
| корень `app/` | runtime (753), webapp (1368) + webapp_index.html (2048), db (124), config (95), logging_conf, power, clock, crypto, errors | runtime-настройки поверх `.env`; Mini App; доступ к SQLite | `crypto`, `errors` не импортируются |
| `app/repositories` | 4 × 8 строк | — | заглушки, 0 импортов |
| `scripts/` | 38 файлов: 20 `measure_*`, `dump_*`, `make_*` (контент), backup_db, backtest_monthly, dev_dashboard, init_db, run_* | замеры, контент, ops | каркас замеров скопирован из файла в файл (`_run/_job/_sim/_utc/_load/_in_half`) |
| `migrations/` | 001–025 (на `d129b10` — 026) | схема | 026 (игра) уже в master; 027 («Мой счёт») пока в ветке |
| `deploy/` | launchd-плисты (две копии), systemd (Hetzner, неактивно), tunnel_webapp.sh, bin/cloudflared (41,7 МБ) | ops | §3.1 |

### 1.3 Поток данных: сигнал → журнал → доставка → executor

1. **Такт поллера.** `run_poller` (`market_data.py:135`) спит до закрытия младшей свечи плюс 2 с
   и пишет `last_poller_tick_at`. Затем собирает `SMCConfig` из 8 runtime-геттеров (166–174) и
   вызывает `to_thread(poll_once, …, pause_sec=1.5)`.
2. **Скан.** `poll_once` (`market_data.py:59`) для каждого символа:
   - загружает кадры `load_frames`; глубину фетча под старые открытые сигналы задаёт
     `_scoring_limit`, движку отдаются ровно 199 баров;
   - вызывает `scan_auto` (`bot.py:136`). Гейты по порядку: свежесть решения, дедуп
     `find_duplicate_setup`, лимит стороны, тормоз стороны, тень. Прошедший сигнал пишется через
     `journal.record_signal` — INSERT в `signals` и `signal_events`, в snapshot кладётся правило
     трейлинга.

   После символов вотчлиста — сироты, затем `journal.update_outcomes_from_bars`. Судья определяет
   филл, стоп, тейк, трейл, таймаут и персистит `trail_stop` для executor.
3. **Доставка.** Каждый `(sig, sid)` уходит в `_broadcast` (`bot.py:4259`; в semiauto — только
   оператору), оттуда в `_deliver_signal` (4278):
   - пост в канал: полный или тизер, карта сигнала по флагу;
   - DM по `payments.plan_recipients`: платным и админам — полный текст, free — лимит в неделю,
     дальше тизер с кнопкой «🔓 Открыть за ⭐»;
   - учёт отправок в `deliveries`.

   Всё это поллер ждёт (`await`) прямо в своём такте.
4. **Исходы.** `_on_close` (4938):
   - алерт о серии стопов на сетапе;
   - посты «вход исполнен / отменён / результат», только свежие (не старше 12 ч), с «Рентгеном» и
     картой;
   - вехи по числу закрытых сделок.
5. **Реалтайм.** `run_stream` → `_on_intrabar` (5072). Касание входа → `journal.try_mark_filled`
   (атомарный UPDATE … WHERE filled_at IS NULL, без гонки с судьёй) → пост «вход исполнен». Там же
   — кроссинг прайс-алертов.
6. **Executor.** `_maintenance_loop` (5146) раз в 60 с при `executor_enabled` вызывает
   `to_thread(bingx_executor.reconcile)` (`bingx_executor.py:211`):
   - берёт open-сигналы, созданные после `executor_since`;
   - ставит TRIGGER_MARKET с аварийным стопом −1R;
   - переносит трейл из `signals.trail_stop` в стоп-ордер;
   - закрывает позицию reduceOnly, когда журнал закрыл сделку;
   - учёт ведёт в `executor_orders`.

   Сам executor решений не принимает.
7. **Публичный слой.** webapp читает ту же БД: статистику, историю, графики. Админ-пульт пишет
   `settings` и `users` и вызывает `payments.admin_grant` / `bingx_executor.status` напрямую.

### 1.4 Где живёт состояние

**SQLite (`data/signals.db`, WAL).** Пишут два процесса — бот и webapp; ещё пишут tunnel_webapp.sh
и backup_db.

| Таблица | Кто пишет | Кто читает | Примечание |
|---|---|---|---|
| `signals` | journal (record_*, судья, try_mark_filled, trail_stop) | все | уровни не меняются, статус и исход пишет только судья |
| `signal_events` | journal | journal (когорты), аудит | append-only |
| `users` | payments: регистрация, подписка, дрип, win-back, реф, источник, mute/quiet/depo; webapp `/api/prefs` | все | колонки `api_key_enc`/`api_secret_enc`/`api_exchange` не используются |
| `payments` | payments: Stars, CryptoBot, refund; `/grant` гасит pending | payments, webapp, `/growth` | **покупки paid media сюда не пишутся** (§3.8) |
| `promo_codes`, `promo_redemptions` | payments | payments | |
| `usdt_claims` | payments | maintenance (эскалация) | |
| `deliveries` | payments.record_delivery / mark_delivery_unlocked | plan_recipients, «что ты упустил» | квоты воронки |
| `price_alerts` | alerts | stream | |
| `executor_orders` | bingx_executor | webapp, отчёты | |
| `user_taken` | webapp `/api/take` | payments.taken_stats, `/mycard` | |
| `ledger_hashes` | journal.record_ledger_hash | — | sha256 трек-рекорда |
| `settings` | runtime.set_value (бот, webapp, tunnel_webapp.sh, backup_db) | все | около 79 ключей, см. ниже |
| `paper_trades` | — | — | мёртвая таблица |

**`settings` смешивает три разных вида данных** (108 функций в `runtime.py`, около 79 ключей):
- **Конфигурация и флаги** (~45): символы, ТФ, режимы, сетапы, тени, гейты стратегии, трейл,
  все цены и тарифы, канал, флаги графиков, стрима, executor и вотчлиста.
- **Маркеры планировщика и троттлинга** (~25): `last_poller_tick_at`, `last_backup_at`,
  `last_*_alert_at`, `*_last_week`, `*_last_day`, `last_board_update_at`,
  `last_watchlist_refresh_at`, `side_cooldown_alerted`, `last_milestone`,
  `executor_day_anchor/halt_day`. По сути это самодельный планировщик заданий.
- **Кэш внешних сущностей**: `live_board_message_id`, `bot_username`, `webapp_url`,
  `vip_invite_link`, `ad_spend:<метка>`.

**Файлы.**
- `data/og.png` — бот рисует, webapp отдаёт.
- `logs/bot.log` — webapp показывает его хвост в админке.
- `data/backups/` и iCloud — бэкапы.
- `data/content/`, `data/cache*/` — артефакты замеров.
- `app/backtest_monthly.json` — статический артефакт для webapp.

**Память процесса (теряется при рестарте).**
- Троттлинги `_mycard_last`, `_unlock_last`, `_check_log`. Лимит «20 проверок в сутки» обнуляется
  каждым рестартом.
- `_last_price` — сид кроссинга алертов.
- `_webapp_watch`, `_ERRBUF`, `bot_info`.
- Кеши ccxt-инстансов, тикеров webapp, графиков, inline-карточек.

### 1.5 Нарушения слоёв

- **data → delivery.** `market_data.poll_once` импортирует `scan_auto`/`scan_semiauto` из
  `app.delivery.bot` (`market_data.py:73`). Скан-конвейер — доменная логика: гейты, дедуп, тени.
  Живёт он в Telegram-модуле.
- **webapp → внутренности бота.** webapp — второе приложение, которое ходит прямо во внутренние
  функции бота: `payments.admin_grant` / `reward_referrer_on_payment` (`webapp.py:812-819`),
  `bingx_executor.status` (958), `payments.taken_stats` (341). Сервисного слоя нет, поэтому одна и
  та же операция живёт в двух вариантах (§3.2, п. 9).
- **Повторы бизнес-логики в UI.** `payments.py` хранит UI-текст (`subscription_status_text`);
  `bot.py` хранит бизнес-правила тарифов и гейты сигналов.

---

## 2. bot.py: план распила

### 2.1 Анатомия

Модульный уровень — 90 функций и 1 319 строк. Сюда входят: скан-конвейер (33–233),
`format_*`-тексты (23 функции), предикаты `should_*` и `*_is_fresh`, разметка CTA, CSV
трек-рекорда, меню и команды, алерты крашей.

`main()` занимает строки 1728–5325. Ниже — её содержимое, разложенное эвристикой по именам
(точность ±10 %):

| Домен | def | хэндлеров | строк | что внутри |
|---|---:|---:|---:|---|
| обслуживание | 7 | 0 | ≈753 | `_maintenance`, `_maintenance_loop`, `_send_digests`, `_watch_webapp`, `_watchlist_gate`, `_task_died`, `_main` |
| платежи | 39 | 30 | ≈579 | subscribe, Stars-тиры, USDT-тиры, claim, pre_checkout, successful_payment, unlock, paid media, grant/revoke/refund/addpromo, price/usdt*/cryptobottoken/vipchannel, `_reward_referrer`, `_subscribe_kb`, `_current_stars_tiers` |
| админ | 35 | 29 | ≈543 | панель и 4 клавиатуры, `_admin_cb` (McCabe 28), числовой FSM, symbols/pause/resume/setmode/channel*, setups/shadow/bysetup/why, executor, confirm/reject/candidates/report, post |
| доставка | 17 | 2 | ≈426 | `_broadcast`, `_deliver_signal`, `_send_retry`, `_send_lifecycle_post`, `_on_close`, `_on_intrabar`, `_push_alerts`, `_stream`, `_poller`, `sigchart`/`sigcalc`, рендер карт |
| аккаунт | 30 | 26 | ≈378 | start, mycard, about/terms/backtest/glossary/help/bybit, referral, notify, depo, alert/delalert, calc + FSM, promo + FSM, cancel, stats, trackrecord, dashboard, my_chat_member |
| контент | 17 | 6 | ≈366 | video/weekvideo/pack/review, postmortem, контент-пак, inline-карточки |
| рост | 6 | 5 | ≈99 | growth (+качество трафика), ad, affiliate, users, announce |
| проверка сигналов | 4 | 3 | ≈80 | `/check`, пример проверки, пересланный пост |
| игра | — | 2 | +169 | на срезе ещё нет; на `d129b10`: `game.py` (1 036 строк, 41 тест), а в bot.py — `/game`, колбэки `game:`, `_game_tick` фоном из `_maintenance` (§6) |

Почему это критично. Код `main()` недостижим для тестов: замыкание над `bot`, `dp`, `cfg`, `log`,
`bot_info` и десятком общих хелперов. Поэтому денежная оркестрация не тестируется (§4.4). И почти
каждая фича конфликтует по этому файлу с параллельными ветками агентов.

### 2.2 Целевая структура

```
app/core/pipeline.py          # scan (auto|semiauto одной функцией), гейты стороны, late-shadows,
                              # decision_is_fresh, is_shadow_setup, SMCConfig.from_runtime()
app/delivery/
  bot.py                      # точка входа ≤150 строк: ctx → build_dispatcher → таски → polling
  tg/context.py               # BotContext: bot, cfg, log, bot_info, admin_ids; spawn(),
                              # notify_admins(), send_retry(), is_dead_recipient(), render_png()
  tg/dispatcher.py            # build_dispatcher(ctx) -> Dispatcher: include_router в фикс. порядке
  tg/keyboards.py             # главное меню, CTA канала, админ-клавиатуры (заменяет заглушку)
  tg/tiers.py                 # ЕДИНЫЙ реестр тарифов Stars/USDT (§3.2, п. 1)
  texts/{info,admin,growth,channel,calc}.py   # format_* из bot.py по доменам
  routers/menu.py             # кнопки меню и /cancel со state.clear(); подключается ПЕРВЫМ
  routers/{info,account,payments,signals,admin,growth,content,checker,game}.py
  services/delivery.py        # broadcast, deliver_signal, lifecycle-посты, on_close, on_intrabar
  services/maintenance.py     # реестр задач обслуживания + цикл (§3.6, п. 4)
```

Передача контекста — через workflow data aiogram (`dp["ctx"] = ctx`, параметр хэндлера
`ctx: BotContext`) или функцию `register(router, ctx)`. Изменяемое состояние замыканий
(`_mycard_last`, `_unlock_last`, `_check_log`, `_last_price`, `_webapp_watch`) переезжает в модуль
своего роутера или в `ctx`.

### 2.3 Порядок извлечения

Каждый шаг — отдельный PR и чистый перенос кода без изменения логики. Ревью делается по
`git diff --color-moved=zebra`. После каждого шага: полный pytest зелёный, снапшот регистрации
(шаг 0.2) не изменился, рестарт `com.rsirange.bot`, смоук `/health`, `/start` и `/subscribe` в
админ-чате.

**Фаза 0 — страховочная сетка** (≈1,5–2 дня; без неё распил вслепую)

| Шаг | Что | Риск | Оценка |
|---|---|---|---|
| 0.1 | Герметичные тесты: явный путь `.env`, session-фикстура с временной мигрированной БД, сетевой страж, правка 4 + 5 тестов (§4.1) | низкий | 2–3 ч |
| 0.2 | Разрезать `main()` на `build_dispatcher(bot, cfg) -> Dispatcher` (только регистрация) и `run()` (таски и polling). Добавить **снапшот регистрации**: для каждого observer список `(callback.__name__, repr(filters))`. Сравнивать как мультимножество (ничего не потеряно и не задвоено) и отдельно проверять порядок известных пересечений (FSM-шаги, `F.forward_origin`, тексты меню) | низкий | 2–3 ч |
| 0.3 | Харнесс хэндлеров: свой `BaseSession`, который записывает API-вызовы и возвращает фейковые `Message`, плюс `await dp.feed_update(bot, Update(...))`. Нужен токен формата `123:ABC`: `Bot("test-token")` не проходит валидацию aiogram. Первые тесты: `/start`, `/subscribe`, `pay_stars*` → `send_invoice` (payload/цена), `pre_checkout_query`, `successful_payment` | средний: внутренности aiogram, версия закреплена `aiogram==3.30.0` | 0,5–1 д |

**Фаза 1 — общие хелперы** (≈0,5 дня)

| Шаг | Что | Риск | Оценка |
|---|---|---|---|
| 1.1 | `BotContext` и хелперы: `notify_admins(text, **kw)` вместо 24 копий цикла; `spawn(coro, name)` с сильной ссылкой и логом исключения в done-callback (закрывает GC-баг §3.6, п. 8); `is_dead_recipient(e)` по типу исключения (`TelegramForbiddenError`, «chat not found») вместо поиска подстрок, сейчас эта проверка написана дважды; `send_retry`, `render_png` | низкий; тексты логов меняются | 3–4 ч |

**Фаза 2 — вынос модульного уровня** (≈1–1,5 дня)

| Шаг | Что | Риск | Оценка |
|---|---|---|---|
| 2.1 | `app/core/pipeline.py`: `scan_auto`/`scan_semiauto` (совпадают на 90 %) — одна функция с параметром «как записывать»; гейты стороны, `_record_late_shadows`, `decision_is_fresh`; `SMCConfig.from_runtime()` для поллера, гейта вотчлиста и `dump_signals --prod`. `market_data` импортирует pipeline вместо bot | средний (паритет движка). Тест: три потребителя получают один и тот же конфиг | 3–4 ч |
| 2.2 | `texts/*.py`: 23 функции `format_*`, CSV трек-рекорда, live board. В bot.py — реэкспорты, потому что тесты обращаются примерно к 70 атрибутам `bot.*` | низкий | 3–4 ч |
| 2.3 | `tg/tiers.py`: единый реестр тарифов (ключ, дни, геттер цены, подпись, callback, USDT-цена). Из него строятся `_subscribe_kb`, `_current_stars_tiers`, хэндлеры `pay_stars_*`/`pay_usdt_*`. Тест: payload каждого включённого тарифа проходит `precheckout_ok` | **высокий: деньги**. Только после 0.3 | 0,5 д |

**Фаза 3 — роутеры по доменам** (≈2,5–3,5 дня; от простого к рискованному)

| Шаг | Роутер | Риск | Оценка |
|---|---|---|---|
| 3.1 | `info` (about/terms/backtest/glossary/help/bybit) | низкий | 1–2 ч |
| 3.2 | `checker` (`/check`, `check_demo`, пересланный пост) | низкий; `F.forward_origin` держать после FSM-роутеров | 1–2 ч |
| 3.3 | `content` (video/weekvideo/pack/review, inline-карточки) | низкий (только админ) | 2–3 ч |
| 3.4 | `growth` (growth/ad/affiliate/users/announce/teasers/freeweek) | низкий | 1–2 ч |
| 3.5 | `menu` + `account` (start, mycard, referral, notify, depo, alert, calc и promo FSM, stats, trackrecord, dashboard). Здесь же осознанно чинится порядок меню и FSM (§3.6, п. 7) | **средний**: FSM чувствителен к порядку | 0,5 д |
| 3.6 | `admin` (панель, `_admin_cb`, числовой FSM, команды настроек, executor, semiauto) | средний | 0,5–1 д |
| 3.7 | `payments` (subscribe, инвойсы, checkout, successful_payment, unlock и paid media, USDT, grant/revoke/refund/promo) | **высокий**: только с тестами 0.3 и реестром 2.3 | 1 д |
| 3.8 | `game`: `/game`, колбэки `game:`, `_game_render`/`_game_offer_round`/`_game_join_flow` (уже в master) — в свой роутер; `_game_tick` — в задачи шага 4.2. «Мой счёт» (`5fa7f05`) — в `account` | средний: мерж параллельной работы | 2–3 ч |

**Фаза 4 — циклы и сервисы** (≈2–2,5 дня)

| Шаг | Что | Риск | Оценка |
|---|---|---|---|
| 4.1 | `services/delivery.py`: broadcast, deliver_signal, lifecycle-посты, `_on_close`, `_on_intrabar`, push alerts | средний-высокий: ядро продукта; тесты на харнессе | 0,5–1 д |
| 4.2 | `services/maintenance.py`: реестр задач (§3.6, п. 4). Каждый блок `_maintenance` и `_maintenance_loop` становится задачей со своим try/except, таймаутом, длительностью в логе и меткой успеха. Долгие задачи (гейт вотчлиста, недельные посты, рассылки) выносятся в фон | **высокий**: порядок «маркер до отправки» должен сохраниться. Отдельный тест на каждую задачу | 1–1,5 д |
| 4.3 | Убрать реэкспорты, тесты переходят на новые модули, `bot.py` сжимается до точки входа | низкий | 2 ч |

### 2.4 Как держать тесты зелёными

- **Реэкспорты.** Пока шаг 4.3 не сделан, `bot.py` реэкспортирует перенесённые имена
  (`from app.delivery.texts.info import format_about  # noqa: F401`). Тесты обращаются к ~70
  атрибутам `bot.*`; в параллельных ветках так же. Зелёный pytest на каждом шаге не требует
  правок тестов.
- **Ruff ловит ошибки переноса.** Pre-commit уже гоняет `ruff --select F821,F401` по всему `app/`.
  Имя из замыкания, которое после переноса не определено в модуле, всплывёт как F821 до запуска.
  Это главный страховочный механизм против NameError после выноса.
- **Снапшот регистрации (0.2)** ловит потерянные и задвоенные хэндлеры и изменение порядка там,
  где фильтры пересекаются. Без него перенос в `Router` молча меняет приоритет: хэндлеры самого
  `dp` проверяются раньше всех подключённых роутеров.
- **Поведенческие тесты на харнессе (0.3)** — до переноса соответствующего домена, особенно
  платежей.
- **Окно заморозки.** Игра уже влита. Перед фазой 3 нужно влить «Мой счёт» (+~170 строк в
  bot.py) или договориться, что новые фичи сразу идут в роутеры. Иначе каждый шаг фазы 3
  конфликтует с параллельными ветками.

### 2.5 Риски и как их гасить

| Риск | Где | Митигация |
|---|---|---|
| Меняется порядок хэндлеров при пересечении фильтров | FSM-шаги с `~F.text.startswith("/")`, `F.forward_origin`, тексты меню, `F.data.startswith("adm_"/"unlock:"/"sig…")` | снапшот 0.2; роутер `menu` первым со `state.clear()`; FSM-роутеры после доменных |
| Общее изменяемое состояние замыканий | `_mycard_last`, `_unlock_last`, `_check_log`, `_last_price`, `_webapp_watch`, `bot_info`, `_MENU_TEXTS` | переезжает в модуль роутера или в `ctx`; тест троттлинга на харнессе |
| Денежные хэндлеры | §4.4 | 0.3 и 2.3 до 3.7; деплой отдельно от остальных шагов; смоук на тестовом тарифе 10⭐/5ч |
| Семантика маркеров обслуживания | `_maintenance` | 4.2 последним; тест на задачу: «маркер ставится до отправки», «сбой одной задачи не трогает другие» |
| Импортные циклы | `market_data` ↔ `pipeline` ↔ `journal`/`runtime` | ленивые импорты, как сейчас, внутри функций; `pipeline` не импортирует aiogram |
| Конфликты с параллельными ветками | 31 % коммитов трогают bot.py | окно заморозки; маленькие PR; новые фичи сразу в роутеры |

### 2.6 Оценка

| Фаза | Дни |
|---|---|
| 0. Страховочная сетка | 1,5–2 |
| 1. Общие хелперы | 0,5 |
| 2. Модульный уровень | 1–1,5 |
| 3. Роутеры | 2,5–3,5 |
| 4. Циклы и сервисы | 2–2,5 |
| **Итого** | **≈7–9** (около 15 небольших PR) |

Отдача начинается уже с фазы 0: к ней появляются тесты денежной оркестрации, независимо от того,
продолжается ли распил.

---

## 3. Находки по коду

### 3.1 Мёртвый код и артефакты

| Что | Доказательство | Действие |
|---|---|---|
| `app/repositories/*.py` (4 файла) | только `# TODO(Fable 5)`, 0 импортов; ссылаются на несуществующий `PLAN_FABLE5.md` | удалить пакет |
| `app/sim/paper.py`, `app/v2/executor.py`, `app/v2/tradingview_webhook.py` | `raise NotImplementedError`, 0 импортов; `make run-webhook` и закомментированный сервис в docker-compose ведут на заглушку | удалить вместе с целью Makefile |
| `app/delivery/keyboards.py` | заглушка на 7 строк | заменить настоящими клавиатурами (шаг 1.1/2.x) |
| `app/brain/risk.py` | только реэкспорт `calculate_position` | можно оставить как фасад, TODO убрать |
| `app/errors.py` | 0 импортов; доменные исключения не используются ни разу | удалить или начать использовать (§3.6, п. 6) |
| `app/crypto.py` + `Settings.fernet_key` + `FERNET_KEY` + зависимость `cryptography` | 0 импортов; executor торгует ключом владельца из env | удалить код; колонки `users.api_key_enc/api_secret_enc/api_exchange` оставить (миграции не трогаем) |
| `paper_trades` | никто не пишет и не читает | оставить в схеме, но пометить мёртвой |
| `bot.scan_watchlist` (209), `bot.scan_frames` (33) | первая без вызовов; вторая только из неё и двух тестов | удалить при шаге 2.1 |
| `journal.update_outcomes` (437) | «упрощённый скоринг для тестов»: 17 вызовов в `test_journal.py`, в проде не используется | перенести в фикстуры тестов, тесты статистики — на `update_outcomes_from_bars` |
| `core/signal_engine.SignalEngine`, `StrategyConfig`, `fetch_ohlcv`, `__main__`-демо | легаси-движок EMA/RSI; используют только `test_signal_engine.py` и `scripts/run_backtest.py`; свой ccxt-инстанс без лока и ретраев | вынести в `scripts/legacy/` или удалить вместе с тестом |
| поля Settings `poll_interval_sec`, `default_risk_pct`, `env`, `log_level` | 0 чтений; поллер выровнен по свечам; логгер читает `LOG_LEVEL` из env сам | удалить вместе со строками `.env.example` |
| `deploy/com.rsirange.{bot,backup}.plist` | дубли `deploy/launchagents/…`: bot один в один, backup отличается комментарием | удалить корневые копии |
| systemd-юниты, `com.rsirange.backup-pull.plist`, `pull_backup_mac.sh`, `scripts/deploy.sh` | прод на Mac с 30.08, `make deploy` отключён | переложить в `deploy/legacy-hetzner/` |
| `Dockerfile`, `docker-compose.yml` | Python 3.12 против прода 3.11; `docker compose up` поднимет **второй поллер** с `restart: unless-stopped` — двойной getUpdates и дубли постов, класс инцидента 27–29.08 | удалить или добавить защиту «не в проде» |
| `deploy/bin/cloudflared` | бинарник 41,7 МБ в git (коммит `e98a529`): раздувает репо, нет контрольной суммы и пути обновления | ставить через brew или релиз с sha256; убрать из индекса (история останется) |
| `app/delivery/soundtrack.py` | живой (через `replay`), но покрытие 0 % | см. §4.4 |

### 3.2 Дублирование логики

1. **Тарифы Stars/USDT — 4 описания одного и того же:**
   - `_subscribe_kb` (`bot.py:1825-1888`);
   - `_current_stars_tiers` (2612-2620), по которому сверяет pre_checkout;
   - шесть хэндлеров `_pay_stars_*` (2220-2245) с захардкоженными днями;
   - `_usdt_tier_price` (1817) и `_pay_usdt_*` (2437-2465).

   Пробный тариф дважды считается как `round(intro_hours/24, 6)` (2226 и 2619). `precheckout_ok`
   (837) сравнивает дни по `abs(d - days) < 1e-6`, так что расхождение любой копии отклонит
   реальную оплату — «Тариф изменился».
2. **Реестры настроек — 4 штуки с разными границами:**
   - 108 геттеров в `runtime.py` и `effective()`;
   - `_ADMIN_NUMERIC_FIELDS` (`bot.py:545`), валидация через `assert`;
   - `/price` со своим `spec` (3136-3138);
   - `_ADMIN_SETTINGS` и `_admin_settings_payload` в webapp (486-645) с min/max.

   Пример расхождения: `sub_days` — бот через FSM и `/price` разрешает `0`, webapp требует
   `≥0.01`. Если выставить 0, тариф будет продавать подписку на 0 дней за полную цену: payload
   `sub:0.0` пройдёт pre_checkout. `sub_price_stars`: FSM пускает 0, `/price` и webapp требуют
   ≥1. Ещё около 30 вызовов `runtime.set_value("<литерал>")`: опечатка в ключе молча создаёт
   новый ключ.
3. **Агрегаты закрытых сделок:**
   - журнал: 6 вариантов (`stats`, `stats_since_created`, `stats_for_period`, `stats_by_symbol`,
     `stats_by_setup`, `agg` в `cohort_stats`) и ещё 18 SQL с `result_r IS NOT NULL`;
   - webapp: `_CLOSED`, 12 использований;
   - `payments._CLOSED_SQL`, `forward_check._CLOSED`.

   Наборы статусов расходятся: `stats()` не включает `'expired'`, остальные включают. Сейчас это
   эквивалентно, потому что у expired `result_r = NULL`. Но докстринг `equity_series`
   (`journal.py:966`) обещает «тот же фильтр, что stats()», а фильтр другой. Нужны одна константа
   и один `aggregate(rows)` плюс тест паритета итогов журнала и webapp.
4. **Депозит, объём, R→$ — четыре реализации:**
   - `calculate_position` (`signal_engine.py:279`);
   - `bingx_executor.calc_qty` (60) — та же формула, плюс шаг, минимум и кап;
   - JS-калькулятор в `webapp_index.html:684-700` с захардкоженным порогом плеча ×20 вместо
     `MAX_LEVERAGE`;
   - JS-симулятор депозита (1661-1680, сложный процент).

   Дайджест считает «$ за неделю» простой суммой (`templates.py:298-302`), а ветка «Мой счёт»
   добавляет ещё один пересчёт. Пользователь может увидеть разные «$» за один и тот же период.
   Плюс баг форматирования: `format_calc` (`bot.py:294-312`) печатает объём через `{:g}`, и
   0.0000025 PEPE превращается в `2.5e-06`. В `personal_size_line` это уже исправлено через
   `_price` (`templates.py:490-492`).
5. **Две системы шаблонов и разбросанные хелперы.**
   - `templates.render_*` (17 функций) обслуживают жизненный цикл сделки и воронку,
     `bot.format_*` (23 функции) — всё остальное.
   - `_LINE` и `LINE` — одна и та же константа в двух модулях.
   - `_fmt_days` дважды: `bot.py:1689` и `payments.py:890`.
   - 28 мест с `symbol.split('/')[0]` при существующем `alerts.base_of`.
   - `graphics._esc` дублирует `templates.esc`.
   - Три запускателя Chromium: `graphics.render_html_to_png`, `content_pack.render_pngs`,
     `replay`.
   - Хэндлы `@rsi_range` / `rsirange_bot` захардкожены примерно в 25 местах (§3.5).
6. **Таблицы ТФ.** `market_data._TF_SECONDS`, `webapp._TF_SEC`, `bot._TF_SECONDS_VIEW`, два
   инлайн-словаря (`bot.py:4050`, `4136`), `SMCEngine.TF_MINUTES`, `stream._BYBIT_INTERVAL`.
   Инлайн-словарь в `_run_check` (4050) индексируется без `.get` и упадёт с KeyError, если
   `signal_check.pick_timeframe` когда-нибудь вернёт `1d`.
7. **Скан.** `scan_auto` и `scan_semiauto` (`bot.py:136-190`) совпадают на 90 %. `SMCConfig`
   «как в проде» собирается в трёх местах (`market_data.py:166-174`, `bot.py:4225-4235`,
   `scripts/dump_signals.py:209-217`). Именно такое расхождение уже ловили: комментарий
   «ПАРИТЕТ… гейт мерил другую стратегию» в `_watchlist_gate` (`bot.py:4222`).
8. **Алерты админам.** 24 копии цикла
   `for admin_id in cfg.admin_ids: try: await bot.send_message(...) except: log.warning(...)`.
   Проверка «мёртвый получатель» написана дважды (`bot.py:4240` и `5066`).
9. **Ручная выдача подписки двумя путями.**
   - `/grant` в боте (`bot.py:3678-3690`): любые дни, реферер получает уведомление.
   - `/api/admin/user` в webapp (`webapp.py:797-824`): дни 1..365, без уведомления реферера.

   Нужна одна сервисная функция `billing.grant_manual(tg_id, days, actor)`.
10. **«Реальный R» executor** считается дважды и по разным формулам:
    `bingx_executor.execution_report` (1141-1218) и `webapp.api_admin_executor` (952-998).
11. **ccxt-инстансы — шесть мест:** общий с локом в `market_data`, свой bybit в webapp (98), BingX
    без лока, новый инстанс при каждом `venue_symbols`, новый при каждом
    `signal_engine.fetch_ohlcv`, `replay._exchange("bybit")`.
12. **Каркас замеров.** В двадцати `scripts/measure_*.py` скопированы `_run/_job/_sim/_utc/_load/
    _in_half` (до 9 копий одной функции). Общий `scripts/_measure_common.py` с единым
    `SMCConfig.from_runtime()` закрыл бы класс ошибок «замер мерил не ту конфигурацию».

### 3.3 Устаревшие комментарии и документы

- **README:**
  - «230 тестов» (строки 17, 56, 173), сейчас 553;
  - «Executor … сознательно не реализован» (192), а демо-executor BingX работает с августа;
  - «migrations/ 001 … 009» (43), сейчас 025;
  - дерево архитектуры без `webapp.py`, `v2/`, `data/stream.py` и контент-модулей;
  - бэкап «04:00» (164), а в OPS.md — 02:00;
  - ссылка на устаревший `NEXT_STEPS_FABLE5.md`.
- **`app/webapp.py:1-11`.** «Почти read-only: единственная запись — user_taken»: пишут ещё
  `/api/admin/set`, `/api/admin/user`, `/api/prefs`. «Хостится на VPS за Caddy… systemd» — сейчас
  Mac, cloudflared и launchd.
- **`app/config.py:1-6`.** «прод целится в 3.12», «каркас запускается на 3.9» — прод на 3.11.15.
- **Мелкие.** `app/crypto.py` («окружение VPS»); `bot.py:1-6` (обещает функцию `scan_symbol`,
  которой нет); `bot.py:75` («systemd WorkingDirectory»); лог старта «платежи=Stars»
  (`bot.py:5313`), хотя USDT давно есть; `bingx_executor.py:221` и модульный докстринг («ключи в
  .env сервера», «IP-whitelist сервера»).
- **Версия Python в трёх местах.** `pyproject.toml`: `requires-python >=3.9` и ruff
  `target-version = "py39"`, mypy — 3.11, Dockerfile — 3.12, прод — 3.11. В `requirements.txt`
  есть `waitress`, в `pyproject.toml` нет.

### 3.4 TODO

- Единственный «живой» TODO, `bot.py:5312` («/calc интерактивный; USDT через CryptoBot»), устарел:
  оба пункта сделаны. Удалить.
- Восемь `TODO(Fable 5)` в заглушках (§3.1) уйдут вместе с ними.
- TODO, которые стоит завести явно, — из комментариев о будущих работах:
  - `_scoring_limit` (`market_data.py:46-54`): при старом якоре кап в 1000 баров означает, что
    сигнал «не скорится вообще». Есть сторож `overdue_open_signals`, но исход теряется до ручного
    вмешательства. Можно догружать через `fetch_history`.
  - «Кандидат на замер: `find_duplicate_setup` блокирует повтор сетапа навсегда» (из аудита
    02.09) — сверить с бэктест-дедупом.

### 3.5 Дрейф конфигурации: `.env` против runtime-настроек

1. **Поиск `.env` идёт вверх по каталогам** (`app/config.py:69-71`). Любой процесс, запущенный
   из worktree или подкаталога, грузит **прод-`.env`**: токены, ключи BingX, `HEALTHCHECK_URL`,
   `DB_PATH`. `scripts/dump_signals._prod_config` дополнительно зовёт `load_dotenv(".env")`
   относительно cwd. Если `DB_PATH` в проде относительный, worktree-процесс получит пустую БД в
   worktree. Тогда `dump_signals --prod` и замеры молча меряют дефолты вместо прод-настроек. Если
   путь абсолютный, а тесты, `init_db` и замеры из worktree работают с живой БД. Какой из
   вариантов реален, не проверялось: `.env` не читался. Опасны оба. Исправление — явный путь
   `Path(__file__).resolve().parents[1] / ".env"` или `ENV_FILE`, и строка вида «DB: <путь>,
   overrides: N» на старте скриптов.
2. **Два источника правды для ~10 значений.** `SYMBOLS`, `TIMEFRAMES`, `SIGNAL_MODE`,
   `SIGNALS_CHANNEL`, `CRYPTOBOT_TOKEN`, `SUB_PRICE_STARS/USDT`, `SUB_DAYS`,
   `FREE_SIGNALS_PER_WEEK` — это лишь дефолты: после первого override правка `.env` молча ни на
   что не влияет. Код, который читает `cfg.*` напрямую, обходит override, и это уже **баги**:
   - `/post` проверяет `cfg.signals_channel` (`bot.py:2792`), а пишет в
     `runtime.signals_channel()`. Если канал задан через `/channel`, а в `.env` пусто, команда
     ответит «Канал не задан».
   - `/growth`, качество трафика: членство проверяется в канале из `.env`
     (`bot.py:3530`, `3533`).
3. **Переменные окружения мимо `Settings`.** `BINGX_API_KEY`/`BINGX_API_SECRET`
   (`bingx_executor.py:182-183, 219, 1092`), `LOG_FILE`/`LOG_LEVEL` (`logging_conf.py`). В
   `.env.example` нет `HEALTHCHECK_URL`, `BINGX_*`, `LOG_FILE`. Зато там есть мёртвые
   `POLL_INTERVAL_SEC`, `DEFAULT_RISK_PCT`, `FERNET_KEY`, `ENV`.
4. **`EXCHANGE_ID` — псевдонастройка.** Bybit захардкожен в `stream.py:18`, `webapp.py:98`,
   `replay.py:271`, `journal.py:162` и в тексте `_run_check`. Смена `EXCHANGE_ID` разведёт поллер
   со стримом, webapp и судьёй. Нужно либо честно зафиксировать Bybit, либо протянуть настройку
   везде.
5. **Захардкоженные хэндлы.** `CHANNEL_URL` (`bot.py:1190`), `open_channel="@rsi_range"` (4293),
   реферальная ссылка `t.me/rsirange_bot` (2055) при известном `bot_info["username"]`, плюс тексты
   templates, content_pack и replay. В `.env` канал задан числовым id, поэтому публичному хэндлу
   нужна своя настройка.
6. **Тулинг в worktree.** Makefile и pre-commit ищут `.venv/bin/python` относительно корня
   worktree, не находят его и откатываются на `/usr/bin/python3` — тот самый шим Xcode, из-за
   которого после инцидента 30.07 ввели правило «только venv». В worktree хук либо падает, либо
   гоняет pytest другим интерпретатором, и прод-`.env` при этом подтягивается.
7. **Две ротации `logs/bot.log`.** Процесс ротирует сам (`RotatingFileHandler`, 10 МБ × 5), а
   `backup_db.rotate_logs` делает copy-truncate в `.1` при >10 МБ. Пока порог одинаковый, они не
   встречаются. Если пороги разъедутся, copy-truncate перезапишет `bot.log.1` хэндлера.

### 3.6 Обработка ошибок

1. **Широкие `except`.** 166 `except Exception` в `app/`, из них 126 в bot.py; 161 помечены
   `# noqa: BLE001` с обоснованием. В основном это оправдано: изоляция получателей рассылки,
   best-effort рендеры. Проблемы — в частностях ниже.
2. **13 молчаливых `except Exception: pass`.** Худшие — отправка админам executor- и
   forward-отчётов и ERROR-буфера (`bot.py:5175, 5192, 5215, 5245`): сбой доставки отчёта не
   оставляет следа даже в логе.
3. **`assert` как валидация ввода.** `_parse_admin_field_value` (`bot.py:570-583`), шаги
   calc-FSM (2534 и далее), `/executor limit` (3186), `/price` (3142). Под `python -O` проверки
   исчезают, а AssertionError смешивается с настоящими багами.
4. **В `_maintenance` нет переборок** (`bot.py:4421-4879`). Без локального try идут, например:
   - `should_alert_backup_stale(…)` (4428);
   - `payments.expire_overdue()` (4562);
   - `due_for_reminder` (4573);
   - `stale_usdt_claims` (4599);
   - дрип триала (4615-4620).

   Разовый сбой БД пропускает остаток минуты — терпимо. А устойчивый сбой в раннем блоке (битая
   метка в settings, баг в предикате) навсегда выключает все последующие обязанности и пульс
   dead-man. Внешний алерт придёт, но лечится это только правкой кода. Решение — реестр задач:
   `Job(name, every, timeout, critical)`, свой try/except на задачу, длительность и последний
   успех в логе и в `/health`; пульс зависит только от «критичных» задач.
5. **Деньги без компенсации.** `@dp.error()` (`bot.py:5032`) возвращает True для всего. Если в
   `successful_payment` упала `grant_subscription` (например, `database is locked` после
   15-секундного busy_timeout), то Stars списаны, подписка не выдана, юзер не получает ни слова,
   а Telegram апдейт не повторит. Админ получит алерт, исправление — вручную через `/grant`.
   Нужно иначе:
   - сначала идемпотентно записать сырой `successful_payment` (charge id) в outbox;
   - ответить «оплата получена, активирую»;
   - выдать подписку с ретраями;
   - добавить задачу обслуживания, которая досводит outbox.
6. **ccxt-ошибки не различаются.** `fetch_closed_ohlcv` и `fetch_history`
   (`market_data.py:258-270`, `304-316`) ретраят любое исключение. BadSymbol (делистинг сироты)
   каждый такт тратит 2 + 4 с сна в потоке поллера. Ретраить стоит только
   `NetworkError`/`RateLimitExceeded`/`ExchangeNotAvailable`. Заодно наконец понадобился бы
   `app/errors.py`.
7. **Порядок меню и FSM.** В aiogram 3 хэндлер без state-фильтра срабатывает в любом состоянии.
   Кнопки, зарегистрированные **до** FSM-шагов — «ℹ️ О боте», «🧪 Бэктест», «📖 Глоссарий»,
   «💳 Подписка», «🤝 Пригласить» (`bot.py:1998-2052`), — перехватывают апдейт и **не сбрасывают**
   диалог. Следующий свободный текст юзера уйдёт как промокод или число калькулятора. Кнопки
   после FSM ловятся `_fsm_menu_escape`, и их приходится нажимать дважды. Это лечится роутером
   `menu` со `state.clear()` (шаг 3.5).
8. **Фоновые задачи без лога исключений.** Задачи в `_BG_TASKS` не имеют done-callback с логом:
   исключение всплывёт только как «Task exception was never retrieved» при сборке мусора.
   `_send_digests` (`bot.py:4875`) и `_push_alerts` (5094) создаются вообще без сильной ссылки.
   Документация asyncio прямо предупреждает, что такую задачу может собрать GC посреди работы.
   Лечится хелпером `spawn()` (шаг 1.1).

### 3.7 Синхронные вызовы в event loop и что может заморозить бота

**Записи — чисто.** AST-скан: ни одной прямой записи в БД из `async def`. Всего 229 мест
`asyncio.to_thread`.

**Чтения — нет.** Найдено 128 прямых синхронных вызовов в 41 async-функции:
- в основном runtime-геттеры: `_maintenance` — 33, `_on_close` — 13, `_admin_cb` — 12,
  `_deliver_signal` — 12, `_on_intrabar` — 7;
- полные сканы `journal.stats()` в `_deliver_signal` (4293) и в win-back (4649),
  `journal.recent_streak` в `_on_close` (4999);
- синхронные хелперы, которые зовутся из async: `_subscribe_kb` (около 15 чтений),
  `_admin_kb`, `_current_stars_tiers`.

В WAL читатели не ждут писателей, так что сейчас это время CPU в loop, а не заморозка. Но оно
растёт с размером `signals`. Два шаблона доступа к БД из async-кода живут рядом.

| Что | Где | Эффект |
|---|---|---|
| `_task_died` шлёт алерт синхронным `urllib` (таймаут 10 с × число админов) прямо в done-callback | `bot.py:5251-5270` → `1676-1686` | **заморозка loop** до 10 с × N, ровно когда сеть деградировала и таски умирают. Polling и стрим стоят. Отправку — в поток |
| Горячий путь стрима | `stream.py:87, 102`; `bot.py:5072-5125` | на каждый WS-кадр синхронные `enabled()`, `runtime.symbols()`, `runtime.timeframes()` плюс `to_thread(unfilled_open_signals)` и `to_thread(check_and_trigger)`. При 15 символах это десятки чтений и хопов в секунду. Нужен снимок runtime с TTL около 5 с и кеш неисполненных входов, сбрасываемый на record/fill |
| Гейт авто-вотчлиста | `bot.py:4668-4676` | бэктест на 6 месяцев 1h для каждого кандидата, последовательно и **внутри `_maintenance`**: цикл стоит минутами. Активация USDT, истечение подписок, пульс healthchecks ждут, возможен ложный алерт «бот мёртв». Флаг по умолчанию выключен; вынести в отдельную задачу |
| Рассылка сигнала | `bot.py:4278-4377`; `payments.py:381-405, 793-808` | на получателя 3 хопа в поток и пауза 0,05 с. Ждёт сам поллер: пост в канал по второму сигналу стоит, пока не уйдут все DM по первому. Сейчас около 29 юзеров, с ростом аудитории станет узким местом. Сначала все посты в канал, потом DM фоновой очередью с пакетным чтением настроек |
| Общий `_EXCHANGE_LOCK` | `market_data.py:214` | все вызовы Bybit в процессе сериализованы (поллер, `/check`, карты, inline-карточки, гейт). Пользовательские действия во время часового скана ждут в очереди, таймаут ccxt 15 с. Это задержка, не заморозка |
| Слоты рендера `_RENDER_SEM = 2` | `bot.py:47` | MP4 (30–60 с) делит те же два слота с картами постов канала. В недельный такт видео для двух админов могут на минуту задержать карты разбора недели |
| Пул потоков | — | на Mac 16 CPU, пул по умолчанию — 20 воркеров, голодание маловероятно. На прежнем 2-vCPU сервере (6 воркеров) было реальным |
| `healthcheck_ping` | `bot.py:1163` | `urlopen` без `with`. CPython закроет сокет сам, но это гигиена |
| Холодный старт | — | `import aiogram.types` занял **13,8 с** при замере (параллельно шли другие агенты). Это простой после каждого рестарта |

### 3.8 Прочее

- **BingX без лока.** Кешированный sync-ccxt-инстанс (`bingx_executor.py:168-191`) одновременно
  используют `reconcile()` из maintenance и хэндлеры `/executor status|orphans|sweep`
  (`bot.py:3166-3214`), каждый в своём `to_thread`. Это тот же класс гонки, который для Bybit
  закрыли `_EXCHANGE_LOCK`. `sweep` закрывает позиции по рынку. Нужен модульный лок вокруг
  reconcile/sweep/status.
- **Paid media не учитывается в деньгах.** Покупки сигналов за ⭐ (`bot.py:2300-2338`) не пишутся в
  `payments`. Выручка в `/growth` (`payments.revenue_by_source`) занижена, следа для возвратов нет.
- **Бонус USDT берётся на момент оплаты.** `poll_cryptobot` добавляет `usdt_bonus_days`
  (`payments.py:534`),
  актуальный в момент оплаты, а не в момент выставления счёта. Если админ поменял бонус между
  этими моментами, юзер получит не то, что ему обещали в сообщении со счётом.
- **Косметика.** Кнопка USDT показывает «30.0 дн» (`bot.py:1861`, `runtime.sub_days()` — float).
  Описание инвойса CryptoBot — «на 36500 дней».
- **Параллельные миграции.** Ветки агентов независимо занимают номера (026 — игра, 027 — «Мой
  счёт»). Сейчас совпадений нет, но правила нет. `init_db` применит любые новые `.sql` из рабочего
  дерева к прод-БД при рестарте — такое уже было с 023, см. комментарий в `backup_db.main`.
- **Сложность (McCabe, порог 15):** `main` 660, `_maintenance` 85, `reconcile` 35,
  `simulate_portfolio` 31, `_outcome_from_bars` 30, `_admin_cb` 28, `_maintenance_loop` 25,
  `parse_signal` 22, `_on_close` 21, `_deliver_signal` 18.
- **Типы.** `mypy` зелёный, но аргументы хэндлеров (`m`, `cq`, `q`, `p`) не аннотированы, и тела
  ~100 хэндлеров mypy не проверяет вовсе. `--check-untyped-defs` даёт 9 ошибок, в основном это
  Optional-нарроуинг; одна — переменная `link`, переиспользованная под другой тип
  (`bot.py:3051`). После распила стоит аннотировать `Message`/`CallbackQuery` и включить
  `check_untyped_defs` для роутеров.

---

## 4. Тесты

### 4.1 Герметичность — главная проблема набора

- **`.env` прода.** `conftest.py` подставляет только `TELEGRAM_BOT_TOKEN` и `ENV` через
  `setdefault`. Всё остальное (`DB_PATH`, `CRYPTOBOT_TOKEN`, `BINGX_*`, `HEALTHCHECK_URL`,
  `TELEGRAM_ADMIN_IDS`) `load_dotenv()` при первом `get_settings()` подтягивает из найденного
  `.env`. Из worktree это прод-`.env` (§3.5, п. 1).
- **Тесты без `fresh_db` ходят в «настоящую» БД.** Это `test_bot.py::test_format_about_contains_disclaimer_parts`
  (140), `test_bot.py::test_format_channel_live_board` (245),
  `test_dump_signals.py::test_prod_config_mirrors_runtime` (66) и
  `::test_prod_config_set_overrides` (80). Они читают `settings` той БД, куда указывает
  `DB_PATH`; в прод-каталоге это живая `data/signals.db`. Проверено: на пустой БД все четыре
  падают с `no such table: settings`. Сейчас они только читают, но вывод зависит от прод-настроек.
  Например, `affiliate_url` меняет текст `format_about`.
- **Сеть.** Пять тестов webapp — `test_api_stats_shape_and_no_strategy_leak` (35),
  `test_open_signal_age_counts_from_fill` (63), `test_symbols_best_worst_public` (289),
  `test_stats_r_series_for_simulator` (468), `test_api_stats_splits_and_daily` (663) — ходят
  в `api.bybit.com` через `webapp._last_prices` → `ccxt.bybit().load_markets()` и `fetch_tickers`.
  Офлайн они проходят только потому, что ошибка проглатывается. Таймаут ccxt — 25 с на вызов.
  При разрешённой сети это стоило около 18 с прогона (13,9 + 1,6 + 1,1 + 1,1 + 0,8).

**Исправление** (≈2–3 ч):
- в `conftest.py`:
  - глушить dotenv;
  - session-фикстура `DB_PATH` во временный каталог с `init_db`;
  - очищать чувствительные переменные;
  - сетевой страж на `socket.getaddrinfo` и `socket.socket.connect` с явным маркером для
    сетевых тестов;
- в самих тестах:
  - пять тестов webapp — монкипатч `_last_prices`, как уже сделано в соседних тестах файла;
  - четыре теста без БД — `fresh_db`;
- явный путь `.env` в `config.py`.

### 4.2 Время и медленные тесты

553 теста. Изолированный прогон — **68–73 с**, под трассировкой покрытия — 117 с.

| Тест | Время | Причина |
|---|---:|---|
| `test_bot.py::test_channel_cta_markup` | 9,6–16,5 с | первый тест, который импортирует `aiogram.types` (≈13,8 с отдельно). Платит за весь набор; ленивый импорт в `conftest` или session-фикстура этого не уберут, только переместят |
| `test_webapp.py::test_api_stats_shape_and_no_strategy_leak` | 13,9 с | сеть: load_markets Bybit (при страже — около 0 с) |
| `test_backtest.py::test_backtest_csv_roundtrip_matches_live_df` | 3,4–6,1 с | реальный бэктест |
| `test_graphics.py` (3 теста PNG) | 2,3–3,8 с каждый | запуск Chromium |
| `test_backtest.py::test_backtest_no_signal_is_clean_report` | 1,7–3,1 с | бэктест |
| `test_executor.py::test_reconcile_daily_kill_switch` | 1,6–2,4 с | — |

Нестабильности нет: шесть прогонов дали детерминированный результат. Плавает только время,
из-за нагрузки от параллельных агентов. Время почти везде передаётся в предикаты явно (`now=`),
часы замораживает лишь один тест — риск «бомб времени» низкий.

### 4.3 Покрытие по модулям

Измерено трассировщиком из stdlib (`sys.settrace`; исполняемые строки — `co_lines()` всех
code-объектов). Это приближение к statement coverage из coverage.py.

| Модуль | Исп. строк | Покрыто | Комментарий |
|---|---:|---:|---|
| `app/delivery/bot.py` | 3 857 | **23 %** | модульный уровень 93 %; **`main()` 0 %** (2 908 строк) |
| `app/data/stream.py` | 87 | 47 % | `run_stream` не тестируется |
| `app/data/market_data.py` | 279 | 49 % | `run_poller`, `venue_symbols`, `top_liquid_symbols` (частично) |
| `app/delivery/replay.py` | 260 | 67 % | сборка MP4 |
| `app/v2/bingx_executor.py` | 816 | 70 % | ветки `reconcile`, `sweep_orphans` |
| `app/core/signal_engine.py` | 258 | 73 % | легаси-движок |
| `app/db.py` | 86 | 78 % | ветки rollback |
| `app/delivery/weekly_video.py` | 152 | 86 % | |
| `app/brain/forward_check.py` | 96 | 86 % | |
| `app/brain/smc_detectors.py` | 316 | 87 % | |
| `app/delivery/content_pack.py` | 116 | 88 % | |
| `app/delivery/alerts.py` | 47 | 89 % | |
| `app/runtime.py` | 318 | 90 % | |
| `app/webapp.py` | 1 050 | 90 % | |
| `app/sim/portfolio.py` | 294 | 91 % | |
| `app/delivery/postmortem.py` | 115 | 91 % | |
| `app/brain/journal.py` | 682 | 92 % | |
| `app/delivery/graphics.py` | 212 | 92 % | |
| `app/sim/backtest.py` | 219 | 92 % | |
| `app/delivery/payments.py` | 491 | **93 %** | доменные функции покрыты хорошо |
| `app/delivery/signal_check.py` | 256 | 93 % | |
| `app/delivery/market_card.py` | 65 | 94 % | |
| `app/delivery/templates.py` | 305 | 96 % | |
| `app/brain/smc_setups.py` | 457 | 97 % | |
| `soundtrack.py` (117), `crypto.py`, `errors.py`, заглушки | — | 0 % | soundtrack живой, остальное мёртвое |
| **Всего `app/`** | 11 219 | **62 %** | без `main()` — **84 %** (7 010 / 8 311) |

### 4.4 Пробелы покрытия, прежде всего денежные

| Поток | Код | Доменная функция в тестах | Оркестрация в тестах | Чем грозит |
|---|---|---|---|---|
| Кнопка тарифа → `send_invoice` (payload `sub:<days>`, цена) | `bot.py:2196-2245` | — | **нет** | payload и цена разъедутся с pre_checkout |
| `pre_checkout_query` по таблице тарифов | `bot.py:2612-2635` | `precheckout_ok` (test_bot) | **нет** (`_current_stars_tiers`) | реальная оплата молча отклонена |
| `successful_payment` → разбор payload и фолбэк → выдача → реф-награда | `bot.py:2637-2657` | `grant_subscription`, `reward_referrer_on_payment` | **нет** | при сбое БД деньги списаны, подписки нет, повтора нет (§3.6, п. 5) |
| `/refund`: сначала API, потом БД; `CHARGE_ALREADY_REFUNDED` | `bot.py:3703-3744` | `find_refundable_stars_payment`, `mark_refunded` | **нет** | двойное вычитание дней или зацикливание |
| Paid media: `unlock` → `send_paid_media` (тизер в caption); `purchased_paid_media` → доставка, затем учёт | `bot.py:2247-2338` | `unlock_verdict`, `mark_delivery_unlocked` | **нет** | утечка уровней до оплаты; покупка не в `payments` |
| USDT: цены тиров, `createInvoice`, активация, уведомление и реф-награда в maintenance | `bot.py:1817-1888, 2412-2484, 4584-4596` | `create_cryptobot_invoice`, `poll_cryptobot` | **нет** | неверная сумма тира, тихая активация |
| Ручная выдача: `/grant` в боте против `/api/admin/user` | `bot.py:3678-3690`; `webapp.py:797-824` | `admin_grant` | webapp — да, бот — **нет** | разные границы и побочные эффекты |
| Истечение, напоминания, win-back, дрип: отправка и порядок маркеров | `bot.py:4560-4652` | `expire_overdue`, `due_for_*` | **нет** | спам или тишина при ошибке порядка «маркер / отправка» |
| Поллер и стрим как циклы | `market_data.py:135-205`, `stream.py:72-134` | `poll_once`, `parse_kline` | **нет** | бэкофф, переподключение, пауза |
| Паритет итогов статистики webapp и журнала | — | — | **нет** | расхождение итогов на поверхностях |

Ещё один дефект набора: тесты статистики журнала строятся на `journal.update_outcomes` — скоринге
по снимку цены, которого нет в проде (17 вызовов). Их стоит перевести на
`update_outcomes_from_bars`.

---

## 5. Топ-15 улучшений по соотношению ценности и трудозатрат

Ценность — от 1 до 5. Трудозатраты: S ≤ полдня, M ≈ 1–2 дня, L ≈ 3+ дня.

| # | Улучшение | Ценность | Трудозатраты | Файлы |
|---:|---|:---:|:---:|---|
| 1 | **Герметичные тесты и явный `.env`**: dotenv с явным путём, session-БД, сетевой страж, правка 4 + 5 тестов. Закрывает утечку прод-БД, секретов и сети из worktree | 5 | S | `app/config.py:69-71`, `tests/conftest.py`, `tests/test_bot.py:140,245`, `tests/test_dump_signals.py:66,80`, `tests/test_webapp.py:35,63,289,468,663`, `scripts/dump_signals.py:205-208` |
| 2 | **`notify_admins` / `spawn` / `is_dead_recipient`**: 24 цикла превращаются в 1; фикс GC-задач; лог вместо `pass` | 4 | S | `bot.py:4240, 4875, 5066, 5094, 5175, 5192, 5215, 5245` и все циклы `for admin_id` |
| 3 | **Быстрые фиксы дрейфа конфигурации**: `cfg.signals_channel` → runtime; мёртвые поля Settings; `.env.example` (`BINGX_*`, `HEALTHCHECK_URL`, `LOG_FILE`); решение по `EXCHANGE_ID`; хэндл канала как настройка; venv в хуке и Makefile для worktree | 4 | S | `bot.py:1190, 2792, 3530, 4293`; `app/config.py`; `.env.example`; `Makefile`; `scripts/hooks/pre-commit` |
| 4 | **`build_dispatcher()`, снапшот регистрации, харнесс с фейковой сессией aiogram** — без этого невозможен ни распил, ни тесты хэндлеров | 5 | M | `bot.py:1728-1765, 5272-5326`; новый `tests/tg_harness.py` |
| 5 | **Тесты денежной оркестрации, единый реестр тарифов, outbox для `successful_payment`, paid media в учёте, одно определение «платящего»** (подтверждённый через `/grant` USDT тоже оставляет запись, см. §6) | 5 | M | `bot.py:1817-1888, 2196-2338, 2412-2484, 2612-2657, 3703-3744`; `payments.py` (`admin_grant`, `due_for_reminder`, `revenue_by_source`) |
| 6 | **Скан-конвейер в `app/core/pipeline.py` и `SMCConfig.from_runtime()`**: паритет конфигурации встроен в код, инверсия слоёв убрана | 4 | S–M | `bot.py:33-233, 4225-4235`; `market_data.py:73, 166-174`; `scripts/dump_signals.py:209-217` |
| 7 | **Удалить мёртвое**: заглушки, `crypto`, `errors` (или начать использовать), легаси-движок, `scan_watchlist`, дубли плистов, Docker, cloudflared из git, устаревший TODO | 3 | S | §3.1 |
| 8 | **Распил bot.py на роутеры** (фаза 3 из §2) — убирает главную точку конфликтов параллельных веток | 5 | L | `bot.py:1728-5325` |
| 9 | **Реестр задач обслуживания с переборками**; долгие задачи — в фон | 4 | M | `bot.py:4379-4936, 5146-5270` |
| 10 | **Лок вокруг BingX-инстанса** для `reconcile`, `sweep` и `status` | 3 | S | `bingx_executor.py:168-191, 1087-1115, 1220-1235`; `bot.py:3166-3214` |
| 11 | **Гигиена event loop**: `_task_died` в поток; снимок runtime с TTL и кеш входов для стрима; убрать `journal.stats()` из loop; ретраить ccxt только сетевые ошибки | 3 | S–M | `bot.py:1163, 1676-1686, 4293, 4649, 4999, 5072-5125, 5251-5270`; `stream.py:87,102`; `market_data.py:258-270` |
| 12 | **Одна агрегация закрытых сделок**: константа, `aggregate()`, тест паритета webapp и журнала; одна функция R→$ для дайджеста, «Моего счёта» и Mini App | 3 | S–M | `journal.py:795-1055`; `webapp.py:279` и далее; `payments.py:712`; `forward_check.py:21`; `templates.py:298-302`; `webapp_index.html:684-700, 1661-1680` |
| 13 | **Единый реестр настроек** (ключ, тип, дефолт, границы, подпись, группа, где редактируется), из которого строятся runtime-геттеры, `effective()`, FSM бота, `/price` и схема webapp; `set_value` отвергает неизвестные ключи | 4 | M–L | `runtime.py`; `bot.py:545-583, 3130-3150, 3800-3923`; `webapp.py:486-645` |
| 14 | **Рассылка сигнала очередью**: сначала каналы, потом DM в фоне; пакетное чтение настроек получателей | 2–3 (растёт с аудиторией) | M | `bot.py:4278-4377`; `payments.py:381-405, 793-808` |
| 15 | **Обновить документы и процесс**: README, OPS, докстринги; правило номеров миграций для параллельных веток | 2 | S | §3.3; `migrations/` |

Порядок пунктов 1–7 сам по себе даёт фазы 0–2 распила: после них фаза 3 становится механической.

---

## 6. Дельта до текущего master (`d129b10`)

Новые коммиты после среза:
- `57c0852` — ежедневная игра «Лонг или шорт?»;
- `b72873c` — приз игры без мгновенного «продлите»;
- `d129b10` — напоминание о продлении только платившим; квоту free не едят сигналы триала.

Суммарно 9 файлов, +2 386/−9: `game.py` (+1 036), `graphics.py` (+200), `bot.py` (+169),
`payments.py` (+41/−9), `runtime.py` (+9), `templates.py`, миграция `026_daily_game.sql`,
`tests/test_game.py` (41 тест), `tests/test_payments.py` (+40).

**Как пересчитать номера строк bot.py из этого отчёта на `d129b10`.** Вставки идут пятью блоками,
внутри них прежний код не менялся:

| Строка на `c6df9b0` | Сдвиг на `d129b10` |
|---|---:|
| 1–1413 | 0 |
| 1414–1987 | +2 |
| 1988–2183 | +7 |
| 2184–4420 | +115 |
| 4421–4425 | +166 |
| 4426 и дальше | +169 |

**Что это меняет в выводах:**
- **Ни одна находка не снята.** На master: bot.py — 5 498 строк, 117 регистраций, `except Exception`
  в bot.py — 131 (+5), циклов алертов админам — те же 24.
- **Покрытие.** Домен игры покрыт 41 тестом. Его оркестрация в `main()` — `/game`, `_game_cb`,
  `_game_tick` — не тестируется, как и всё остальное в `main()`. Тесты на `d129b10` не
  прогонялись: код не менялся, а 553 теста среза зелёные.
- **Транзакции.** Новые транзакции `game.py` прогнаны тем же AST-сканом. Реентрантных записей
  нет: `payments.grant_gift_days(conn, …)` работает внутри транзакции вызывающего, и об
  ограничении `_write_lock` в докстринге сказано явно. Фоновая `_game_tick` держится в
  `_game_bg["task"]` — сильная ссылка есть.
- **Ещё один путь выдачи доступа.** Приз игры — уже девятый путь продления доступа: Stars, USDT
  авто, `/grant`, `/api/admin/user`, промо, триал, реф-награда, win-back и теперь игра. Все сходятся
  в `_extend_and_activate`, и это хорошо. Но правила вида «перевзводить win-back», «гасить
  напоминание», «считать платежом» разложены по вызывающим функциям. `grant_gift_days` берёт окно
  в 3 дня из `due_for_reminder(3)` внутри `bot._maintenance` — то есть магическое число живёт в
  двух модулях. Сервис биллинга с явными «видами выдачи» снимет это (топ-5, топ-13).
- **Новая находка: кто считается «платящим».** `d129b10` шлёт напоминание о продлении только при
  `EXISTS(payments.status='paid')`. Но ручное подтверждение оплаты USDT через `/grant` (поток
  «Я оплатил», `payments.admin_grant`) строку в `payments` не пишет. Не пишут её и покупки paid
  media. Значит, юзер, у которого единственная оплата — ручной USDT через `/grant`, после
  `d129b10` не получит «продлите». Ещё раньше он выпадал из выручки `/growth`. Предложение:
  при `/grant` в режиме подтверждения оплаты писать строку `payments` (новый `method` требует
  миграции CHECK) или завести явный `users.ever_paid`. После этого одно определение «платящего» использовать в напоминаниях,
  выручке, реф-награде и win-back. Тест — сценарий «Я оплатил» → `/grant` → `due_for_reminder`.

---

## Приложение: как воспроизвести замеры

Вспомогательные файлы лежали в scratchpad-каталоге сессии, не в репозитории. Повторить
несложно.

**pytest-плагин**, подключается через `-p`, переменная `PYTHONPATH` указывает на его каталог:
- `dotenv.load_dotenv = lambda *a, **k: False` — до любого импорта `app.*`;
- `DB_PATH` во временный каталог; при `AUDIT_MIGRATE=1` — `db.init_db()` в `pytest_sessionstart`;
- очистка `CRYPTOBOT_TOKEN`, `BINGX_*`, `HEALTHCHECK_URL`, `SIGNALS_CHANNEL`,
  `TELEGRAM_ADMIN_IDS`;
- сетевой страж: подменённые `socket.getaddrinfo` и `socket.socket.connect` бросают ошибку для
  нелокальных адресов и записывают id теста;
- при `AUDIT_COV=1` — `sys.settrace` и `threading.settrace` с фильтром по `app/`; исполняемые
  строки — `co_lines()` всех code-объектов.

Запуск:

```
AUDIT_MIGRATE=1 PYTHONPATH=<каталог плагина> .venv/bin/python -m pytest -p auditplug -o addopts="" -q
```

**AST-сканы:**
- прямые вызовы `runtime./journal./payments./db.*` внутри `async def`, без захода во вложенные
  `def`;
- вызовы внутри `with db.transaction()`;
- разбивка `main()` по доменам по именам функций.

**Сложность:** `ruff check app --select C901 --config 'lint.mccabe.max-complexity=15'`.
