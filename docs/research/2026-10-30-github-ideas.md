# Что взять с GitHub и из Telegram для @rsirange_bot (ресёрч 30.10.2026)

Звёзды и лицензии сняты с GitHub API 30.10.2026. У нас уже стоят aiogram 3.30.0, ccxt 4.5.64 и Lightweight Charts v4.2.3 (`app/webapp_lwc.js`).

Усилие: **S** — до 2 дней, **M** — 1–2 недели, **L** — больше.

**Не предлагаю, потому что у нас это уже есть:**
- healthchecks и ночные бэкапы;
- paid media за ⭐ и VIP-канал;
- sha256 CSV и верификатор;
- Cornix-блок (выключен);
- MFE/MAE в postmortem;
- календарь, симулятор депозита и бэктест по месяцам в Mini App;
- тихие часы;
- своя рефералка, inline-карточки, видео-повторы.

## Главное

1. **Новые механики Telegram, которых в коде ещё нет:**
   - guest mode (Bot API 10.0): ответ «@rsirange_bot» на чужой сигнал в группе даёт вердикт по свечам прямо там;
   - Affiliate Programs за Stars;
   - Main Mini App и попадание в Mini App Store;
   - Suggested posts — оплачиваемые Stars/TON размещения в чужих каналах.
2. **Открытого верификатора сигнальных каналов нет.** Есть только игрушечные парсеры и два закрытых SaaS в бете (Gloriam Insights, TraderRanker). Аудит канала по @username — свободная ниша. Методику можно взять у Cornix Marketplace и LuxAlgo Edge Score (MIT).
3. **Честность можно усилить криптографически:**
   - хэш каждого сигнала публикуем в момент входа, раскрываем после закрытия;
   - хэш-цепочка;
   - раз в день якорь в Bitcoin через OpenTimestamps.
4. **Удержание** (14 из 29 заблокировали бота):
   - вместо «выключить всё» — уровни on/silent/off по типам событий;
   - топики в личке;
   - нативное автопродление подписки.

## Топ-20

### Продукт

| # | Идея | Усилие |
|---|---|---|
| 1 | **Проверка через guest mode.** Ответ «@rsirange_bot» на чужой сигнал даёт вердикт со ссылкой на бота. Вирусная петля поверх готового чекера. | S–M |
| 3 | **Хэш на сигнал при входе, раскрытие после закрытия.** sha256(символ, сторона, вход, стоп, тейки, время, соль). Соль — 128 случайных бит, иначе цены подбираются перебором; тега сетапа в хэше нет. Плюс хэш-цепочка и ежедневный OpenTimestamps. | S–M |
| 4 | **Движок аудита канала.** Источник — t.me/s/ и экспорт Telegram Desktop. Разбор по формату Cornix, сверка со свечами, Edge Score, окна 1/3/6 месяцев, доля правок и удалений, карточка для шеринга. | M |
| 5 | **Удержание.** Уровни уведомлений по типам событий (вход, трейл, выход, дайджест, тизер, промо), топики в личке, автопродление подписки (`createInvoiceLink(subscription_period)`), события подписки 10.2. | S–M |
| 13 | **Деривативный контекст.** OI, фандинг, ликвидации в карточке и пост «ликвидации дня» через ccxt и Coinalyze. | M |
| 14 | **Механики игры.** «Толпа против факта», серии и сезоны, эфемерные ответы в группах. Призы только подарками или Premium, без платного входа. | M |

### Дашборд и UX

| # | Идея | Усилие |
|---|---|---|
| 6 | **Блок «чего ждать».** Распределение итога подписки за 30 дней со случайного старта, Monte Carlo серии минусов и просадки, доверительный интервал матожидания R. Помогает и конверсии, и против оттока после стопов. | S |
| 10 | **«Сколько отдаёт трейлинг».** MFE → итог, концентрация прибыли, результат по часу и дню недели (как у LuxAlgo). | S |
| 11 | **Нативные кнопки Mini App.** MainButton и SecondaryButton, полный экран с фиксацией ориентации, ярлык на домашний экран, shareMessage и shareToStory, ChatActionMiddleware. | S–M |
| 12 | **Два публичных трека по образцу Myfxbook:** бумажный журнал и реальные филлы BingX-демо, у каждого свой бейдж. Это прямо закрывает внешний аудит 30.10. | S–M |
| 15 | **Lightweight Charts v4.2.3 → v5.2.1.** Панель RSI, маркеры, LongShortPosition, живые свечи из WS Bybit. | M |

### Инженерия

| # | Идея | Усилие |
|---|---|---|
| 7 | **Стражи паритета.** Тест прогрева RSI (как recursive-analysis во freqtrade), lookahead-тест на обрезанных данных, ежедневный перепрогон живого периода через sim, одно ядро для лайва и бэктеста. | S–M |
| 16 | **Разрезать `bot.py`.** Router'ы и middleware (пользователь/БД, троттлинг через флаги, роутер ошибок), aiogram_dialog для меню, i18n, aiogram 3.31. | M |
| 17 | **Executor как стейт-машина** по образцу PositionExecutor/TripleBarrier из Hummingbot (Apache-2.0, код брать можно). Protections из freqtrade — только через замер. | M |
| 18 | **initData по Ed25519** — токен бота уходит из webapp-процесса. Заодно проверить защиту origin (с 20.07.2026). | S |
| 19 | **Отдельный heartbeat на каждую задачу и Litestream** (реплика SQLite в S3 в ЕС). | S |

### Рост

| # | Идея | Усилие |
|---|---|---|
| 2 | **Нативная дистрибуция:** Main Mini App с превью в Mini App Store, Affiliate Program (без своего кода), Apps Center с Analytics SDK. | S |
| 8 | **Платные размещения внутри Telegram:** Suggested posts, реинвест Stars в Ads со скидкой 30%, Adsgram — только на тест. | S–M |
| 9 | **Бейдж «проверено RSI Range» и рейтинг каналов.** Только с методикой и правом на ответ, лучше с согласия канала. | M |
| 20 | **Копитрейдинг BingX как витрина.** Только по решению владельца и после закрытия разрыва журнал/исполнение. | M–L |

## Источники по областям

### Торговые движки и бэктест

- **freqtrade** — https://github.com/freqtrade/freqtrade · ★56k · GPL-3.0, только идеи.
  - Уровни уведомлений: https://www.freqtrade.io/en/stable/telegram-usage/
  - lookahead-analysis: https://www.freqtrade.io/en/stable/lookahead-analysis/
  - recursive-analysis: https://www.freqtrade.io/en/stable/recursive-analysis/ . RSI Уайлдера рекурсивен, поэтому прогрев даёт расхождение лайва с бэктестом.
  - Protections: https://www.freqtrade.io/en/stable/plugins/
- **FreqUI** — https://github.com/freqtrade/frequi (GPL). Сравнение прогонов, экран «ожидание против факта».
- **jesse** — https://github.com/jesse-ai/jesse · MIT. Monte Carlo: https://docs.jesse.trade/docs/monte-carlo/
- **hummingbot** — https://github.com/hummingbot/hummingbot · Apache-2.0.
  - PositionExecutor: https://hummingbot.org/strategies/v2-strategies/executors/positionexecutor/ . Трейлинг там задан как activation_price + trailing_delta и отслеживается на клиенте по живой цене.
  - Condor — https://github.com/hummingbot/condor , Telegram-интерфейс.
- **passivbot** — https://github.com/enarjord/passivbot · Unlicense. Одно ядро решений для лайва и бэктеста.
- **nautilus_trader** — https://github.com/nautechsystems/nautilus_trader · LGPL. Взять принцип детерминизма; переезд не нужен.
- **vectorbt** — https://github.com/polakowo/vectorbt · Commons Clause. Тепловые карты параметров (плато против пика).
- **backtesting.py** — AGPL, только идеи: SQN, Kelly.
- **quantstats** — https://github.com/ranaroussi/quantstats · Apache-2.0. HTML-отчёт по ряду R.

### Форматы сигналов и аудит каналов

- **Cornix.**
  - Формат: https://help.cornix.io/en/articles/5814928-free-text-trade
  - Рейтинг: https://help.cornix.io/en/articles/11563121-marketplace-stats-faqs . Бэктест: $10k, $500 на сделку, окна 1/3/6 месяцев, вес 50/30/20.
- **LuxAlgo Edge Score v2** — https://github.com/LuxAlgo/trade-journal/blob/HEAD/docs/edge-score.md · MIT. При малой выборке скор не показывается; порог поднять примерно до 30 сделок.
- **Myfxbook** — https://www.myfxbook.com/help/verification . Два раздельных бейджа: «история совпадает с брокером» и «владелец контролирует счёт».
- **OpenTimestamps** — https://github.com/opentimestamps/opentimestamps-client (LGPL, использовать как CLI или библиотеку) · https://opentimestamps.org/
- **История публичного канала без аккаунта** — `t.me/s/<канал>?before=<id>`. Это неофициально и хрупко.
  - Для приватных каналов — JSON-экспорт Telegram Desktop.
  - Пропуски ID ≈ удалённые посты; калибровать, потому что служебные сообщения тоже занимают ID.

### Bot API 9.x–10.3 (последняя 10.3 от 24.08.2026)

Changelog: https://core.telegram.org/bots/api-changelog . aiogram 3.31.0 поддерживает 10.3, у нас стоит 3.30.0 (10.2).

- **Guest mode** — https://core.telegram.org/api/bots/guest-mode . Разбор на aiogram: https://mastergroosha.github.io/aiogram-3-guide/guest-bots/
  - Отвечает один раз через `answer_guest_query`.
  - Не работает в группах с защитой контента.
  - Включается в веб-BotFather: Bot Settings → Guest Chat Mode.
- **Эфемерные сообщения в группах** (10.2), **топики в личке** (9.3/9.4), **Rich Messages и таблицы** (10.1/10.3), **date_time** (9.5).
- **Stars:**
  - автопродление (`subscription_period`, 8.0);
  - события подписки (10.2);
  - sendGift и giftPremiumSubscription (9.0): https://docs.aiogram.dev/en/latest/api/methods/gift_premium_subscription.html
- **Mini App** — https://core.telegram.org/bots/webapps
  - shareMessage, shareToStory, requestFullscreen, addToHomeScreen, DeviceStorage и SecureStorage.
  - Защита origin с 20.07.2026.

### Mini App и aiogram

- **tma.js** — https://github.com/Telegram-Mini-Apps/tma.js · MIT. Справочник API, наш код переписывать не нужно.
- **init-data-py** — https://github.com/nimaxin/init-data-py · MIT. Проверка initData по Ed25519.
- **Telegram Analytics SDK** — https://github.com/telegram-mini-apps-dev/analytics
- **aiogram.**
  - Флаги и троттлинг: https://docs.aiogram.dev/en/latest/dispatcher/flags.html
  - i18n: https://docs.aiogram.dev/en/latest/utils/i18n.html
  - aiogram_dialog: https://github.com/Tishka17/aiogram_dialog
  - aiogram-3-guide: https://github.com/MasterGroosha/aiogram-3-guide
  - Шаблоны: https://github.com/Forden/aiogram-bot-template , https://github.com/donbarbos/telegram-bot-template

### Графики и журналы

- **Lightweight Charts v5.**
  - Миграция: https://tradingview.github.io/lightweight-charts/docs/migrations/from-v4-to-v5
  - Плагины: https://github.com/tradingview/lightweight-charts/releases
- **LongShortPosition** — https://github.com/difurious/lightweight-charts-line-tools-long-short-position (MPL-2.0, лицензию плагина проверить).
- **LuxAlgo/trade-journal** — https://github.com/LuxAlgo/trade-journal · MIT. Лучший донор: концентрация прибыли, результат по часу и дню, серии, replay.
- **TradeTally** — https://github.com/GeneBO98/tradetally · Apache-2.0.

### Деривативы (бесплатно)

- **Bybit `allLiquidation`** — https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation
- **ccxt 4.5.64** (уже установлен):
  - fetchOpenInterestHistory, fetchLongShortRatioHistory, fetchFundingRateHistory;
  - watchLiquidations у bybit, binanceusdm и okx.
- **Coinalyze** — https://api.coinalyze.net/v1/doc/ . Бесплатно, нужен ключ (заводит владелец), лимит 40 запросов в минуту.

### Надёжность

- **Healthchecks** — https://github.com/healthchecks/healthchecks . Отдельная проверка на каждую задачу.
- **Litestream** — https://github.com/benbjohnson/litestream . Потеря данных при сбое — секунды вместо 24 ч.
- **Uptime Kuma** — https://github.com/louislam/uptime-kuma

### Рост

- **Affiliate Programs.**
  - https://telegram.org/tour/affiliate-programs · https://core.telegram.org/api/bots/referrals
  - Процент можно только повышать.
  - Оплата VIP-канала, скорее всего, не засчитывается — проверить тестовой покупкой.
- **Apps Center** — https://docs.ton.org/v3/guidelines/dapps/tma/guidelines/publishing
- **Suggested posts** — https://core.telegram.org/api/suggested-posts . Деньги каналу приходят, только если пост провисел нужный срок.
- **Stars в Ads со скидкой 30%** — https://core.telegram.org/api/stars
- **BingX lead trader** — https://bingx.com/en/learn/article/how-to-become-a-lead-trader-on-bingx-copy-trading

## Не брать

- Переезд на freqtrade, jesse или nautilus и переписывание Mini App на React — это L без выигрыша.
- Код под GPL/AGPL (freqtrade, FreqUI, OctoBot, backtesting.py, backtrader, aggr, TradeNote) и CC BY-NC (Deltalytix) — только идеи.
- vectorbt — Commons Clause.
- Платное или по заявке: CoinGlass, FindMini, TradingView Advanced Charts.
