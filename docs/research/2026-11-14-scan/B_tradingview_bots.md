# B. TradingView и open-source боты: какие идеи имеют честные доказательства

Дата обзора: 14.11.2026.

**Как читать.** Все числа и цитаты взяты со страниц, которые я реально открыл (список в разделе 5). Где прочитана только аннотация — это сказано. Не открылись: Reddit (все три способа), strat.ninja (домен припаркован), jesse.trade, octobot.cloud, SSRN — по ним выводов нет.

## Главный вывод

1. Независимую проверку с издержками в розничном мире проходит одно семейство: **медленный (4h–1d) trend following / channel breakout с широким стопом на корзине из 5–20 ликвидных монет, в основном в лонг**. Профиль везде одинаковый: win rate 30–45%, прибыль делают несколько больших трендов, в боковике серия мелких убытков.
2. Это то же семейство, что наш отклонённый №14, но доказательства относятся к другой конструкции: корзина 10–20 монет вместо BTC+ETH, ансамбль из 9 горизонтов, volatility targeting, порог ребаланса. Если команда считает это недостаточным отличием — честный ответ такой: больше ничего с доказательствами в этом срезе нет.
3. Ожидания скромные: 18% годовых при волатильности 9% без плеча (бэктест до марта 2025). Розничные замеры за 2025–2026 неровные: за 2025 от −7,4% (4h, пять монет) до +13,0% (daily, только лонг); за 2026 от −5,6% (daily) до +7,5% (4h, только BTC).
4. Всё остальное популярное — осцилляторный mean reversion на 5m–1h, MA cross на ≤1h, grid, DCA/martingale, envelope-индикаторы, «100% win rate» — либо без доказательств, либо доказуемый артефакт (раздел 3).
5. Ни у одного открытого бота (freqtrade, NFI, Hummingbot) я не нашёл аудируемого live-результата с плюсом. Единственный опубликованный live-эксперимент — минус.

Шкала оценок: **A** — независимый тест, после издержек, out-of-sample; **B** — независимый тест после издержек без out-of-sample, либо out-of-sample без издержек; **C** — собственный бэктест автора.

---

## 1. Кандидаты на реальную проверку

### 1. Ансамбль Donchian на ротационной корзине top-10–20 монет (long-only, volatility targeting)

- **Правило (дневные свечи).** Для каждого окна n из {5, 10, 20, 30, 60, 90, 150, 250, 360} дней: лонг, когда close равен максимуму closes за n дней; выход, когда close ≤ trailing stop. Стоп = максимум из вчерашнего стопа и середины канала Donchian (вниз не двигается). Вес модели = min(25% / годовая волатильность за 90 дней, 200%). Итоговая позиция — среднее по 9 моделям. Корзина пересобирается раз в месяц: листинг ≥ 365 дней, медианный дневной объём за 30 дней ≥ $2 млн, top-N по объёму, капитал поровну. Монета выбывает, если медианный объём < $1 млн или медианное дневное изменение цены < 0,5%. Размер из-за изменения волатильности пересчитывается, только если отклонение от цели > 20%.
- **Почему может работать.** У монет нет фундаментального якоря, участники склонны к стадному поведению — тренды длиннее. Корзина ловит тренды отдельных монет; beta к BTC получается 0,08.
- **Доказательства.**
  - Zarattini, Pagani, Barbon, «Catching Crypto Trends» (Swiss Finance Institute RP 25-80, апрель 2025), читал полный текст: https://concretumgroup.com/wp-content/uploads/2026/02/Catching-Crypto-Trends.pdf
    - Данные: CoinMarketCap, 21 616 монет, без survivorship bias. Период 01.01.2015–19.03.2025.
    - Издержки: 10 bps на сделку плюс порог ребаланса 20%.
    - Корзина 20 монет: CAGR 18%, волатильность 9%, Sharpe 1,57, MDD 11%, alpha к BTC 10,8% в год.
    - Корзина 5 монет: Sharpe 1,44, MDD 14%. Корзина 10: Sharpe 1,50, MDD 12%. Корзина 50: Sharpe 1,54.
    - Только BTC (с издержками): CAGR 30%, волатильность 17%, Sharpe 1,56, MDD 19%. ETH: Sharpe 1,51.
    - По 40 монетам отдельно Sharpe от 0,21 (BSV) до 1,68 (SOL); у многих 0,4–0,7 (BCH 0,48, DOT 0,60, UNI 0,33, AAVE 0,43).
    - Внутри top-100 по объёму (2020–2025) зависимости результата от ликвидности нет.
    - Чего нет: out-of-sample разбиения, фандинга, перпов. Исполнение — по дневному close агрегированной цены.
  - Man AHL, «In crypto we trend» (данные на 01.12.2024): https://www.man.com/insights/in-crypto-we-trend — с учётом издержек, стоимости шорта и ликвидности оптимум «around 10-15 coins» для MA cross и «around 10 coins» для breakout; средняя парная корреляция монет «around 0.6». Цифр доходности нет.
  - Hudson & Urquhart (Annals of Operations Research, 2021), читал аннотацию: https://ideas.repec.org/a/spr/annopr/v297y2021i1d10.1007_s10479-019-03357-1.html — почти 15 000 технических правил; break-even издержки «substantially higher» типичных; но «there is no predictability for Bitcoin in the out-of-sample period, although predictability remains in other cryptocurrency markets». Это прямой довод против теста только на BTC.
- **Данные.** Бесплатные: дневные свечи с публичных API бирж и data.binance.vision. Нужны и делистнутые монеты, иначе появится survivorship bias.
- **Соответствие ограничениям.** Решение раз в день; spot или перпы. При $1k и 20 монетах одна позиция выходит около $10–15 (моя оценка: 1/20 капитала × вес 0,25–0,3) — на границе минимального ордера. При малом капитале брать 5–10 монет.
- **Риски.** Нет out-of-sample в статье. Период после марта 2025 не покрыт, а розничные замеры за 2025–2026 слабые (кандидат 3). На перпах лонг платит фандинг. Доходность без плеча скромная. Корреляция монет 0,6 ограничивает пользу от корзины.
- **Пересечение с отклонённым.** Частично №14 (trend только на BTC+ETH) — отличие: корзина 10–20 монет с ротацией, 9 горизонтов, volatility targeting, порог ребаланса. Частично №1 (cross-sectional momentum) — отличие: сигнал по собственной истории монеты, без ранжирования по доходности.
- **Оценка: B.** Независимо от нас, после издержек, без survivorship bias, но без out-of-sample. Период апрель 2025 – ноябрь 2026 для нас готовый out-of-sample.

### 2. Быстрый breakout-слой (5–30 дней), правило «MAX»

- **Правило.** Держать лонг, пока цена на максимуме за N дней (N = 10–20); в варианте Donchian — модели 5/10/20/30 дней из кандидата 1.
- **Почему может работать.** Краткосрочное продолжение после нового локального максимума.
- **Доказательства.**
  - Quantpedia, «Revisiting Trend-following and Mean-reversion Strategies in Bitcoin» (12.09.2024): https://quantpedia.com/revisiting-trend-following-and-mean-reversion-strategies-in-bitcoin/ — дневные данные Gemini, 11.2015–08.2024. Out-of-sample 04.02.2022–20.08.2024: «the MAX strategy remains alive and well», причём 20-дневный максимум в этом периоде лучше 10-дневного. Вторая нога — покупка на N-дневном минимуме — дала «low or even negative returns». Издержки в тексте не упомянуты; таблицы на странице — картинки, цифр из них у меня нет.
  - Zarattini и др., таблица 1 (BTC, без издержек): Sharpe 1,66 (5 дней), 1,55 (10), 1,60 (20), 1,61 (30) против 0,99 (150). Сделок за 10 лет: 292 у 5-дневной модели, win rate 41%. При издержках 50 bps CAGR 5-дневной модели падает с 34% до 18%.
- **Данные.** Бесплатные дневные свечи.
- **Соответствие.** Раз в день; годится для spot и перпов.
- **Риски.** Самый чувствительный к издержкам слой. Out-of-sample есть только по BTC.
- **Пересечение.** Частично №14 — отличие: короткие окна и применение к корзине. С №4 (squeeze → expansion) не совпадает: условия сжатия волатильности нет.
- **Оценка: B.** Out-of-sample есть (BTC), издержки учтены только во втором источнике.

### 3. Daily breakout с фильтром тренда и R-ladder trailing на 5 мейджорах (розничная реализация)

- **Правило.** Только при EMA50 > EMA200: вход, когда дневной close выше EMA50 и выше максимума 5 предыдущих свечей. Стоп = вход − 1,5 × ATR(14) (это 1R). При +1R стоп в break-even, далее каждый +1R двигает стоп на 1R. Выход по стопу или по дневному close ниже EMA20. Родственный вариант на 4h: пробой канала 100 свечей, стоп = дневной close − 2 × дневной ATR(14), тейк 4R, выход по каналу 50 свечей.
- **Почему может работать.** То же, что в кандидате 1; широкий стоп делает издержки малой долей риска.
- **Доказательства (собственные тесты автора Algonney, скрипты опубликованы за 1–2 дня до обзора, почти без оценок).**
  - Daily: https://www.tradingview.com/script/rnYfjVk4-Daily-Trend-Breakout-R-Ladder-Trail/ — BTC, ETH, SOL, BNB, XRP (USDT perpetual), 01.2020–11.2026, риск 1% на сделку, плечо до 1x. Издержки: taker 0,055% на сторону, slippage 0,05%, спред, фандинг. Итог: +75,22%, PF 1,37 (1,59 до издержек), win rate 30,35%, 313 сделок, MDD −15,92%. По годам: 2020 +10,8%, 2021 +27,5%, 2022 0,0%, 2023 +6,3%, 2024 +9,4%, 2025 +13,0%, 2026 −5,6%. Фандинг — около 1 800 USDT при чистой прибыли 7 521 USDT. Автор: «The same idea on the 4H chart LOST money» и «The short version lost money on every timeframe I tried».
  - 4h channel 100/50: https://www.tradingview.com/script/fB8YZHDd-Channel-Breakout-100-50-Daily-ATR-Stop-480-Trades-5-Coins/ — BTC: +41,4%, PF 1,89, 104 сделки, MDD −6,0%. Пять монет: +196,4%, PF 1,52, 480 сделок, MDD −17,0%; лонги +174,7%, шорты +21,7%. 2025 год: −3,6% на BTC, −7,4% на пяти монетах. Соседние параметры 80/40 и 120/60 дают близкий результат.
- **Данные.** Бесплатные свечи и история фандинга.
- **Соответствие.** Раз в день или раз в 4 часа; перпы; риск 1% на сделку подходит малому капиталу.
- **Риски.** Никем не перепроверено. Проверка соседних параметров — не out-of-sample. В годовом выражении это примерно 8,5% (daily, пять монет) и 5% (4h, только BTC) — мой пересчёт из итоговых процентов за 6,9 года. Точные значения спреда не указаны.
- **Пересечение.** Частично №14 — отличие: 5 монет, фиксированный риск в R, дневной ATR-стоп на 4h-входе.
- **Оценка: C.** Ценность — единственный источник в обзоре с фандингом и разбивкой по годам до ноября 2026.

### 4. Long-short расширение trend (шорт на пробой вниз)

- **Правило.** Кандидат 1 плюс зеркальный шорт при close на минимуме за n дней.
- **Доказательства.** Zarattini и др., приложение: корзина 20 монет после 10 bps — CAGR 16%, Sharpe 1,24, MDD 13%, beta 0,00. На BTC ансамбль long-short без издержек даёт Sharpe 1,31 против 1,58 у long-only. Авторы сами пишут, что оценка стоимости шорта «remains highly uncertain». Против: у Algonney шорты дали +21,7% из +196,4% на 4h и минус на daily; в независимом тесте Supertrend на золоте шорты убыточны на трёх таймфреймах из четырёх (https://hackernoon.com/the-truth-about-tradingviews-supertrend-strategy-after-67-years-of-backtesting).
- **Данные.** Бесплатные свечи плюс история фандинга.
- **Соответствие.** Только перпы.
- **Риски.** Шорт альтов: фандинг, сквизы; стоимость шорта в статье не смоделирована.
- **Пересечение.** Частично №14 — отличие то же, что у кандидата 1, плюс нулевая beta.
- **Оценка: B−.** Проверять как вариант кандидата 1, не отдельно.

### 5. CTREND: cross-sectional trend factor из набора технических индикаторов

- **Правило (по аннотации).** Сигнал агрегирует ценовые и объёмные технические индикаторы на разных горизонтах методами машинного обучения; монеты ранжируются по сигналу. Частоту ребаланса и модель издержек я не проверял.
- **Почему может работать.** Комбинация многих слабых трендовых признаков устойчивее одного momentum.
- **Доказательства.** Fieberg, Liedtke, Poddig, Walker, Zaremba, JFQA (онлайн 15.07.2025), **читал только аннотацию**: https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/trend-factor-for-the-cross-section-of-cryptocurrency-returns/4C1509ACBA33D5DCAF0AC24379148178 — более 3 000 монет; эффект «survives the impact of transaction costs and persists in big and liquid coins»; устойчив по подпериодам.
- **Данные.** Бесплатные OHLCV.
- **Соответствие.** Зависит от частоты ребаланса — уточнить по полному тексту.
- **Риски.** Сложность модели, шорт альтов, затухание после публикации.
- **Пересечение.** Частично №1 — отличие: не сырая прошлая доходность, а набор индикаторов с объёмом; заявлена работа на крупных ликвидных монетах, тогда как у нас прибыль шла только из хвостов мем-коинов.
- **Оценка: A− по заявлению журнала, фактически не подтверждена мной** до чтения полного текста.

### 6. Внутридневной trend на BTC только в окне «Monday Asia Open»

- **Правило.** Торговать внутридневной trend/breakout только с воскресенья около 19:00 по Нью-Йорку в течение следующих 24 часов; вне окна не торговать. Точные правила авторы не раскрыли.
- **Почему может работать.** По версии авторов — приток ликвидности и позиционирование азиатских участников в начале недели дают устойчивый поток ордеров.
- **Доказательства.** Concretum, «Seasonality in Bitcoin Intraday Trend Trading»: https://concretumgroup.com/seasonality-in-bitcoin-intraday-trend-trading/ — long-short ансамбль внутридневных trend-моделей, volatility target 20%, 2018–2025: Sharpe около 1,6 **до комиссий** против менее 0,8 у long-only BTC с тем же таргетом. Прибыль сосредоточена в указанном окне; утро воскресенья по США отрицательное. Эффект «substantially more pronounced» после середины 2020.
- **Данные.** Бесплатные часовые свечи.
- **Соответствие.** Часовые решения подходят.
- **Риски.** Результат до комиссий; внутридневная частота съедается издержками; правила не опубликованы.
- **Пересечение.** Частично №5 (сезонность) — отличие: сезонность не доходности, а прибыльности trend-правила; частично №14.
- **Оценка: C.**

### 7. Компоненты снижения издержек: порог ребаланса, медленный таймфрейм, широкий стоп

- **Правило.** Не пересчитывать размер, пока отклонение от цели меньше 20%; не опускаться ниже 4h–1d; стоп на дневном ATR.
- **Почему работает.** Счёт за комиссии растёт с числом сделок независимо от наличия edge.
- **Доказательства.**
  - Zarattini и др.: порог 20% при издержках 50 bps возвращает примерно 100 bps в год.
  - «Cost Reality Check» (EMA 20/50, комиссия 0,10% на сторону, slippage 2 тика, 31.12.2024–12.11.2026): https://www.tradingview.com/script/psPN4gHb-Cost-Reality-Check-EMA-Cross-Before-and-After-Fees/ — BTC 30m: 323 сделки, −2,17% до комиссий, комиссии 6,19%, итог −8,35%. BTC 1h: 160 сделок, −5,44%. ETH 1h: 174 сделки, −7,97%. BTC 4h: 39 сделок, +0,67%.
  - Algonney: то же правило прибыльно на daily и убыточно на 4h.
  - Тест Supertrend на золоте: на M15 издержки забрали 96% прибыли, на H4 почти не заметны.
- **Оценка: B.** Это не источник прибыли, а условие выживания любой стратегии. **Пересечение:** нет.

### 8. Связка «trend-лонги + короткие mean-reversion шорты как хедж»

- **Правило.** Долгие momentum-лонги по top-10–20 монетам плюс краткосрочные шорты перегретых альтов; позиции 0,5–3% капитала, без жёстких стоп-лоссов, 15–20 моделей одновременно.
- **Доказательства.** Пересказ интервью с Pavel Kycek (Robuxio), 27.05.2025: https://algoadvantage.substack.com/p/040-pavel-kycek-generating-insane — только описание подхода, **цифр результатов в тексте нет**. Полезные детали: фьючерсы ликвиднее spot в 3–10 раз; данные обязательно с делистнутыми монетами; для новичков — trend на top-10–20.
- **Пересечение.** Частично №3 и №8 — отличие: шорт (мы проверяли лонг после пампа) и роль хеджа в портфеле, а не отдельной стратегии.
- **Оценка: C−.** Заявление продавца без опубликованных чисел. В список включено как единственная идея с шортовой стороны.

---

## 2. Проверять первыми

1. **Кандидат 1** — повторить на биржевых данных с делистнутыми монетами и продлить до ноября 2026: период после марта 2025 статья не видела, это чистый out-of-sample.
2. **Кандидат 2** — отдельно замерить быстрые окна 5–30 дней при наших издержках: по BTC у них лучший Sharpe и самая высокая чувствительность к комиссиям.
3. **Кандидат 3** — независимо пересчитать daily-правило Algonney с реальным фандингом: дёшево, и это единственные цифры за 2025–2026.
4. **Кандидат 6** — сравнить прибыль любого нашего часового trend-правила внутри и вне окна «воскресенье 19:00 NY + 24 часа» после издержек: один запрос к уже имеющимся данным.
5. **Кандидат 5** — сначала прочитать полный текст статьи (частота ребаланса, модель издержек), потом решать.

---

## 3. Иллюзии

| Что | Почему артефакт | Источник |
|---|---|---|
| **Open Close Cross Strategy R5** (9,2 тыс. boosts) | Repaint через старший таймфрейм. Автор выпустил «NoRepaint Version» со словами: «Backtesting performance will not be as good as the repaint version». Сторонний скрипт называется «Real Back Test // Not good». | https://www.tradingview.com/script/9wda3yUw-Open-Close-Cross-Strategy-NoRepaint-Version-by-JustUncleL/ ; https://www.tradingview.com/script/Fr6Ds7BZ-Open-Close-Cross-Strategy-Real-Back-Test-Not-good/ |
| **Nadaraya-Watson Envelope [LuxAlgo]** (35,5 тыс. boosts) | По умолчанию перерисовывается: «by default this indicator can be subject to repainting». Авторы: «We do not have data supporting the performance of this tool». | https://www.tradingview.com/script/Iko0E2kL-Nadaraya-Watson-Envelope-LuxAlgo/ |
| **Bollinger + RSI, Double Strategy (ChartArt) v1.1** (49,4 тыс. boosts, 2016) | Подгонка in-sample: версия 1.1 «made more successful in backtesting by now using a 200 period for the SMA». Комиссии, slippage и размер позиции в описании не упомянуты. Независимого теста именно этого скрипта не нашёл. | https://www.tradingview.com/script/uCV8I4xA-Bollinger-RSI-Double-Strategy-by-ChartArt-v1-1/ |
| **SuperTrend STRATEGY (KivancOzbilgic)** (25,7 тыс. boosts) | Прибыль от одного тренда, а не от правила. Независимый порт на тиках с издержками: золото H4 PF 1,97 за 2020–2026, но 97% прибыли из 2024–2026; за 2020–2023 PF 1,05. NZDUSD: M15, H1 и H4 убыточны (PF 0,80–0,86); из 32 наборов параметров на H1 и H4 ни один не дал PF выше 1,0. На крипте не тестировалось. | https://hackernoon.com/the-truth-about-tradingviews-supertrend-strategy-after-67-years-of-backtesting |
| **EMA cross на ≤1h** | Комиссии больше сырого результата: BTC 30m — счёт за комиссии почти втрое больше убытка до комиссий. | https://www.tradingview.com/script/psPN4gHb-Cost-Reality-Check-EMA-Cross-Before-and-After-Fees/ |
| **Martingale / Grid Like Strategy** (Editors' pick, 2,7 тыс. boosts) | Martingale-сайзинг. Сам автор: такие системы «totally unusable and dangerous»; 9 убытков подряд требуют позиции ×256. Автор второго скрипта: «anyone who using martingale strategy will kaboom their portfolio». | https://www.tradingview.com/script/oxvR5vMy-Grid-Like-Strategy/ ; https://www.tradingview.com/script/8OS1nbr8-inwCoin-Martingale-Strategy-for-Bitcoin/ |
| **DCA-бот с safety orders (стиль 3Commas)** | Martingale плюс оптимизация in-sample. Bollinger-DCA на DOGEUSDT 5m: in-sample (06.2021–09.2024) +942%, win rate 82,7%, 0 margin calls; out-of-sample (09.2024–02.2026) −52%, MDD −68%, 2 margin calls при win rate 82,9%. Автор: «The optimizer did not remove the failure mode. It delayed it.» Живой пример: бот на BNB израсходовал 26 safety orders и завис (январь 2022). | https://backtestsnotsignals.substack.com/p/i-built-a-safe-10-monthly-dca-strategy ; https://www.tradingview.com/chart/BNBUSD/jIsBTJc7-DCA-Bot-is-stuck/ |
| **Классический grid (Pionex и аналоги)** | Нулевое ожидание: по аннотации arXiv 2506.11921, ожидаемая доходность традиционного grid «essentially zero». Тест с комиссией Kraken 0,40%: в бычий месяц SOL (+207%) бот взял около 10% движения; 100% win rate — «arithmetic», потому что бот не продаёт в убыток. | https://arxiv.org/abs/2506.11921 ; https://dev.to/cart0ne/i-ran-backtests-looking-for-an-engine-i-found-an-airbag-4jp4 |
| **Узкий trailing stop / ROI в свечном бэктесте freqtrade** | Движок не знает порядок движения внутри свечи. Пример: $2 500 → $65 150 за 11 дней (+2506%); после пересчёта на 5m «all of the profits have vanished». Документация: ордера исполняются по запрошенной цене без slippage. | https://brookmiles.github.io/freqtrade-stuff/2021/04/12/backtesting-traps ; https://www.freqtrade.io/en/stable/backtesting/ |
| **DevilStra, GodStraNew, Zeus, wtc (freqtrade)** | Lookahead: нормализация через `.min()`/`.max()` по всему датафрейму. Лежат в официальной папке `lookahead_bias`. | https://github.com/freqtrade/freqtrade-strategies/tree/main/user_data/strategies/lookahead_bias |
| **NostalgiaForInfinity: «zero losses», win rate 100%** | Подгонка in-sample: к сигналу добавляют «protection chains», пока убытки на тех же окнах не станут нулём. PR №1345: 171 цепочка, 1 445 сделок, 0 убытков. PR №1420: с 4 924 сделок до 414, win rate 100,0%. Оба сигнала отключены (`enable=False`); слиты ли PR — не проверял. Live-жалоба (issue №660): сделка RIVER/USDT дошла примерно до −70%. | https://github.com/iterativv/NostalgiaForInfinity/pull/1345 ; https://github.com/iterativv/NostalgiaForInfinity/pull/1420 ; https://github.com/iterativv/NostalgiaForInfinity/issues/660 |
| **Hummingbot MACD + Bollinger (официальный эксперимент)** | Edge меньше комиссий: 11 дней live на APE-BUSD perp, 334 сделки, итог −1,538%. Авторы: «fee costs impacted profitability». | https://hummingbot.org/blog/directional-trading-with-macd-and-bollinger-bands/ |
| **Lorentzian Classification: таблица win rate** (37,6 тыс. boosts) | Это не бэктест. Автор: раздел Trade Stats «is NOT meant to be used as a substitute for proper backtesting». | https://www.tradingview.com/script/WhBzgfDu-Machine-Learning-Lorentzian-Classification/ |
| **Покупка BTC на N-дневном минимуме** | Не прошла out-of-sample: за 02.2022–08.2024 «low or even negative returns». | https://quantpedia.com/revisiting-trend-following-and-mean-reversion-strategies-in-bitcoin/ |
| **Pairs trading (distance / cointegration) на дневных свечах** | Не превосходит бенчмарки: 26 монет Binance, дневной distance-метод −0,07% в месяц; результат «quite sensitive» к издержкам и окну исполнения. На 5m заявлено 11,61% в месяц; цифры для 1h в аннотации нет. Читал только аннотацию. | https://api.semanticscholar.org/graph/v1/paper/DOI:10.1109/ACCESS.2020.3024619?fields=title,abstract |

Отдельное наблюдение: на первой странице Editors' Picks среди стратегий (23 скрипта) почти всё — шаблоны и учебные примеры (trailing stop, размер позиции, таблица месячных доходностей, шаблон для 3Commas). Торговых идей с доказательствами там нет; два скрипта про martingale опубликованы как предупреждение.

---

## 4. Как читать бэктест на TradingView

1. **Сравнить среднюю сделку с издержками.** В Properties должны стоять комиссия и slippage; у части популярных скриптов (например, SuperTrend STRATEGY) они нулевые. Если средняя сделка до комиссий не больше round trip с запасом — подбор входа не спасёт (правило из «Cost Reality Check»).
2. **Искать repaint.** В коде — `request.security()` с `lookahead_on`, `calc_on_order_fills`; в описании — слово «repaint»; у автора — отдельная «NoRepaint»-версия. Правила публикации TradingView называют lookahead «dangerously misleading behavior».
3. **Проверить тип графика.** Heikin Ashi, Renko, Kagi дают цены, которых не было; для стратегий это запрещено правилами публикации.
4. **Проверить сайзинг.** Pyramiding, safety orders, множитель объёма, margin 0 («infinite leverage» по правилам TradingView). Высокий win rate при усреднении ничего не значит: у DCA-бота он был 83% и до, и после ликвидаций.
5. **Смотреть разбивку по годам и по монетам.** Больше 100 сделок (ориентир из правил TradingView). Если прибыль дал один период или пять сделок — это рынок, а не правило (Supertrend: 97% прибыли из одного ралли).
6. **Не верить стопам внутри свечи.** Когда стоп и тейк попадают в одну свечу, эмулятор угадывает порядок. Узкий trailing stop почти всегда артефакт; нужен Bar Magnifier или младший таймфрейм.
7. **Отличать устойчивость от out-of-sample.** «Соседние параметры тоже работают» и «сделал успешнее в бэктесте» — не проверка на новых данных. Простой out-of-sample: всё, что случилось после даты публикации скрипта.
8. **Добавить фандинг вручную.** В стандартных настройках Strategy Tester фандинга нет, его надо вычитать отдельно; у daily-лонга на перпах он составил около 1 800 USDT при чистой прибыли 7 521 USDT.

---

## 5. Источники

### Открыты и прочитаны

**TradingView**
- https://www.tradingview.com/scripts/editors-picks/?script_type=strategies
- https://www.tradingview.com/scripts/?script_type=strategies&script_access=open
- https://www.tradingview.com/script/rnYfjVk4-Daily-Trend-Breakout-R-Ladder-Trail/
- https://www.tradingview.com/script/fB8YZHDd-Channel-Breakout-100-50-Daily-ATR-Stop-480-Trades-5-Coins/
- https://www.tradingview.com/script/psPN4gHb-Cost-Reality-Check-EMA-Cross-Before-and-After-Fees/
- https://www.tradingview.com/script/P5Gu6F8k/ (SuperTrend STRATEGY)
- https://www.tradingview.com/script/uCV8I4xA-Bollinger-RSI-Double-Strategy-by-ChartArt-v1-1/
- https://www.tradingview.com/script/vObmEraY-Open-Close-Cross-Strategy-R5-revised-by-JustUncleL/
- https://www.tradingview.com/script/9wda3yUw-Open-Close-Cross-Strategy-NoRepaint-Version-by-JustUncleL/
- https://www.tradingview.com/script/Fr6Ds7BZ-Open-Close-Cross-Strategy-Real-Back-Test-Not-good/
- https://www.tradingview.com/script/Iko0E2kL-Nadaraya-Watson-Envelope-LuxAlgo/
- https://www.tradingview.com/script/WhBzgfDu-Machine-Learning-Lorentzian-Classification/
- https://www.tradingview.com/script/MvlwAzSg-3Commas-Bot/
- https://www.tradingview.com/script/oxvR5vMy-Grid-Like-Strategy/
- https://www.tradingview.com/script/8OS1nbr8-inwCoin-Martingale-Strategy-for-Bitcoin/
- https://www.tradingview.com/scripts/dcabot/
- https://www.tradingview.com/chart/BNBUSD/jIsBTJc7-DCA-Bot-is-stuck/
- https://www.tradingview.com/script/WV74xxHh-Myth-Busting-E05-ORB-One-Candle-Retest-PyneSys/
- https://www.tradingview.com/script/5l935t47-Myth-Busting-E04-Sneaky-Pivot-PyneSys/
- https://www.tradingview.com/script/JVpFeVLc-Strategy-Auto-Optimized-by-Backpulse/ (пост продавца инфраструктуры)
- https://www.tradingview.com/support/solutions/43000614705/ (lookahead)
- https://www.tradingview.com/support/solutions/43000590599-script-publishing-rules/
- Страницы поиска TradingView по запросам «supertrend strategy», «bollinger rsi double strategy», «open close cross strategy»

**Open-source боты**
- https://github.com/freqtrade/freqtrade-strategies (README, список `user_data/strategies`, `lookahead_bias/readme.md`, `futures/Readme.md`)
- https://github.com/iterativv/NostalgiaForInfinity (README, метаданные репозитория, issues №607, №660, №1463, PR №1345, №1404, №1420)
- https://www.freqtrade.io/en/stable/lookahead-analysis/
- https://www.freqtrade.io/en/stable/backtesting/
- https://brookmiles.github.io/freqtrade-stuff/2021/04/12/backtesting-traps
- https://hummingbot.org/blog/directional-trading-with-macd-and-bollinger-bands/
- https://backtestsnotsignals.substack.com/p/i-built-a-safe-10-monthly-dca-strategy (открытая часть статьи)
- https://arxiv.org/abs/2506.11921 (аннотация)
- https://dev.to/cart0ne/i-ran-backtests-looking-for-an-engine-i-found-an-airbag-4jp4
- https://algoadvantage.substack.com/p/040-pavel-kycek-generating-insane

**Независимые оценки и статьи**
- https://concretumgroup.com/wp-content/uploads/2026/02/Catching-Crypto-Trends.pdf (полный текст)
- https://concretumgroup.com/catching-crypto-trends-a-tactical-approach-for-bitcoin-and-altcoins/
- https://abarbon.com/papers/catching-crypto-trends
- https://www.cxoadvisory.com/technical-trading/crypto-asset-trend-following-strategies/ (открытая часть)
- https://concretumgroup.com/seasonality-in-bitcoin-intraday-trend-trading/
- https://quantpedia.com/revisiting-trend-following-and-mean-reversion-strategies-in-bitcoin/
- https://www.man.com/insights/in-crypto-we-trend
- https://ideas.repec.org/a/spr/annopr/v297y2021i1d10.1007_s10479-019-03357-1.html (аннотация)
- https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/trend-factor-for-the-cross-section-of-cryptocurrency-returns/4C1509ACBA33D5DCAF0AC24379148178 (аннотация)
- https://api.semanticscholar.org/graph/v1/paper/DOI:10.1109/ACCESS.2020.3024619?fields=title,abstract (аннотация Fil & Kristoufek)
- https://hackernoon.com/the-truth-about-tradingviews-supertrend-strategy-after-67-years-of-backtesting
- https://dev.to/s_morozov/5-ways-tradingview-backtests-lie-to-you-and-how-to-fix-each-one-2hn8 (маркетинг сервиса автоматизации; заявление «247% → −12%» без названия стратегии, в выводах не использовано)

### Не открылись или непригодны

- https://strat.ninja/ — редирект на http://ww19.strat.ninja/, там только трекинговый пиксель (домен припаркован). Рейтинги проверить нельзя.
- web.archive.org — недоступен из инструмента.
- Reddit: old.reddit.com заблокирован, www.reddit.com (JSON) — 403, зеркало cal1.lr.ggtyler.dev — отказ соединения. Тред r/algotrading «Best backtested bitcoin strategy I found» не прочитан.
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5209907 — 403 (текст взят с сайта Concretum).
- https://jesse.trade/strategies — 403. По Jesse данных нет.
- https://www.octobot.cloud/en/explore — 403. По OctoBot данных нет.
- https://yodacom-site.pages.dev/research/grid-blind-validation-2023-2025 — открылась, но результаты «Withdrawn October 2026».
- doaj.org (403), ieeexplore.ieee.org (пустой ответ), PDF на jfqa.org (404).

---

## Приложение 1. Семейства TradingView: что показывает страница

| Семейство | Пример | Правило | Что видно про настройки бэктеста | Независимый тест с издержками |
|---|---|---|---|---|
| Supertrend | SuperTrend STRATEGY, KivancOzbilgic, 25,7 тыс. boosts, 2020 | ATR(10) × 3; разворот при close за линией; всегда в рынке, без стопа и тейка | В описании нет ни комиссий, ни цифр прибыли. Автор: «generally, it fails in a sideways-moving market» | Есть: золото и NZDUSD, не крипта (раздел 3) |
| Bollinger + RSI | ChartArt v1.1, 49,4 тыс. boosts, 2016 | Продажа выше верхней полосы при перекупленном RSI, покупка ниже нижней при перепроданном; база полос SMA 200 | Комиссии и размер позиции не упомянуты; заявлено отсутствие repaint после закрытия свечи | Не нашёл |
| MA / EMA cross | 3Commas Bot, Bjorgum, 15,3 тыс. boosts | MA cross, стоп и тейк 1:1, опциональный ATR trailing | По умолчанию комиссия 0,05% и небольшой slippage; размер — процент от equity; пример на BTCUSD 1h. Это шаблон, не заявление о прибыли | «Cost Reality Check»: EMA 20/50 на ≤1h в минусе |
| Channel breakout / Donchian | Algonney, ноябрь 2026, почти без оценок | Пробой канала 100 свечей 4h или 5 дней с фильтром EMA | Комиссия 0,055%, slippage 0,05%, спред, фандинг; риск 1%; 313 и 480 сделок; разбивка по годам | Самого скрипта — нет; семейство подтверждено статьёй (кандидат 1) |
| Open Close Cross | JustUncleL R5, 9,2 тыс. boosts | Пересечение MA по open и close со старшего таймфрейма | Repaint признан автором | Сторонний «Real Back Test // Not good» |
| Envelope / LuxAlgo | Nadaraya-Watson Envelope, 35,5 тыс. boosts | Контртренд от границ kernel-огибающей | Индикатор; repaint по умолчанию | Не нашёл |
| ML-индикаторы | Lorentzian Classification, 37,6 тыс. boosts | Классификатор ближайших соседей | Индикатор; после закрытия свечи не перерисовывается; таблица статистики — не бэктест | Не нашёл |
| DCA-боты | 3Commas Bot DCA Backtester (TrendFriend), 1 тыс. boosts | Базовый ордер плюс safety orders, общий тейк | Эмулятор DCA-бота 3Commas с 4 сигналами входа | Тест с разделением in/out-of-sample: ликвидации (раздел 3) |
| Grid / martingale | Grid Like Strategy, alexgrover, 2,7 тыс. boosts | Сетка уровней, множитель позиции после убытка | Опубликовано как демонстрация проблем | arXiv и тест на Kraken (раздел 3) |
| Pivot / ORB / VWAP | Myth Busting E04, E05 (PyneSys), ноябрь 2026 | Правила из YouTube-роликов для американских акций на 1m–15m | Комиссия и slippage нулевые по умолчанию — авторы прямо называют результат gross; E05: 26 сделок на AMD, 109 на четырёх акциях | Сами скрипты и есть проверка чужих правил; не крипта |
| SMC / liquidity sweep | Несколько скриптов blitz_algo в текущем списке популярных | Sweep ликвидности с фильтром режима | Страницы не открывал | Не искал: семейство у нас отклонено (№6) |
| Heikin Ashi, Ichimoku | — | — | Отдельные страницы не открывал. Правила TradingView запрещают сигналы на Heikin Ashi и других нестандартных графиках | Не нашёл |

## Приложение 2. Open-source боты

- **freqtrade-strategies (официальный репозиторий).** README: стратегии «for educational purposes only», результаты зависят от пар, таймфрейма и периода. Таблицы результатов нет. Есть папка `lookahead_bias` с четырьмя стратегиями-примерами ошибки.
- **NostalgiaForInfinity.** 3 529 звёзд, создан 14.07.2021, последний push 13.11.2026, около 28,7 тыс. коммитов. Рекомендации README: таймфрейм строго 5m, 40–80 пар, 6–12 открытых сделок. Live-результатов нет — только «For backtesting results, check out the comments in the individual commit page». Таймфрейм 5m не укладывается в наше ограничение «не чаще раза в час».
- **Что общего у долгожителей.** По NFI видно три вещи: множество сигналов входа (включая покупку просадок) по десяткам пар с доборами позиции (`position_adjustment`, `grinding`, `derisk`); высокий win rate при редких крупных убытках (в PR базовый win rate 88–96%, в issue №660 сделка около −70%); непрерывное сопровождение — в репозитории есть сервис, который проверяет blacklist каждые 60 секунд. Это долгая жизнь за счёт ручной подстройки, а не доказанный статичный edge.
- **Hummingbot.** Единственный найденный live-результат directional-стратегии — эксперимент MACD + Bollinger, −1,538% за 11 дней.
- **3Commas, Pionex.** Независимой аудируемой статистики по DCA- и grid-ботам не нашёл; механику провалов показывают источники из раздела 3.
- **Jesse, OctoBot, strat.ninja.** Страницы не открылись.
- **Проверки, которые даёт сам freqtrade.** Команда `lookahead-analysis`; документация предупреждает, что она проверяет только сработавшие сигналы и может дать ложно-отрицательный результат. Про бэктест там же: «backtesting will never replace running a strategy in dry-run mode».
