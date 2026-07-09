# RSI Range Signals — план до топ-уровня

> Как читать этот документ: это синтез трёх источников — черновики четырёх разделов (продукт/доверие,
> монетизация, рост, UX/надёжность), придирчивая критика этих черновиков и сырой research по нише.
> Черновики трижды независимо предлагали одни и те же фичи (реферальная система, SLA по USDT-заявкам,
> Cornix-формат, регистрация в TGStat) с несовместимыми схемами БД и номерами миграций — здесь для
> каждой дублирующейся фичи оставлен **один канонический вариант**, а миграции пронумерованы **сквозно
> по всему документу** (`004`–`011`, следующий свободный на дату записи — свериться с `migrations/`
> перед стартом, вдруг что-то уже сделано не в этом порядке). Формат — как в `docs/NEXT_STEPS_FABLE5.md`:
> для код-задач — Файлы/Шаги/Критерий приёмки/Промпт для Fable 5, для не-код-задач — Шаги/Критерий.

## TL;DR

1. **Честность — это не утешительный приз, это единственное реальное УТП.** Walk-forward бэктест на
   BTC/ETH/SOL за 12 мес не нашёл устойчивого механического края — значит бот физически не может
   конкурировать на «мы точнее». Зато вся ниша забита завышенными винрейтами (заявленные 89–98% против
   верифицированных независимыми трекерами 52–86%, nftevening.com, smartoptions.io) — и честная публикация
   отрицательного результата бэктеста прямо в боте (`/backtest`, `/about`) — ход, которого не делает
   НИ ОДИН конкурент в исследованной нише.
2. **100%-автопубликация исходов (включая стопы) уже реализована технически — её просто не видно.**
   Это ровно та механика, за которую хвалят лидеров ниши (CoinCodeCap Signals, Rocket Wallet Signals).
   Задача — сделать её заметной и проверяемой: закреплённый манифест, независимый архиватор (TGStat/
   Telemetr), еженедельная криптографическая контрольная сумма журнала, скачиваемый CSV по `/trackrecord`.
3. **Закрыть дыру в надёжности раньше, чем усиливать доверие поверх неё.** `data/signals.db` — весь
   актив проекта (сделки, подписчики, публичный журнал) — сейчас не бэкапится вообще. Без этого
   защищать нечего.
4. **Убрать топ-жалобу ниши на монетизацию** — «оплатил, доступ не выдали вовремя» (Learn2Trade, Wolfx,
   trustpilot.com) — через SLA-отслеживание USDT-заявок и разблокировку уже готового авто-USDT (не
   хватает только токена).
5. **Рост и монетизация должны ехать НА честности, а не отвлекать от неё**: реальный триал вместо
   бессрочного free-тира, реферальная программа, которая платит только за реальную оплату (анти-накрутка),
   годовой тариф со скидкой — но без lifetime-тарифа, потому что бессрочное обещание при недоказанном
   крае — плохая ставка.
6. **Целиться в аудиторию, которая уже говорит на языке проекта** (SMC/ICT Telegram- и Discord-сообщества,
   пользователи Cornix), а не в общий крипто-трафик — и с первого дня метить источники привлечения
   (`acquisition_source`), чтобы платный рост (реклама, KOL) обосновывался цифрами, а не ощущениями.
7. **Список «чего не делать» защищает единственный актив от тех же ловушек, на которых уже погорели
   похожие сервисы**: без заявлений о винрейте, без lifetime-тарифа, без геймификации прибыли (прецедент
   Robinhood, $7.5M), без NFT/токен-гейтинга, без executor без доказанного края, без карточного
   эквайринга для крипто-продукта.

## Конкурентный ландшафт

Кратко — кого исследовали и что конкретно стоит взять (или явно НЕ повторять):

- **CoinCodeCap Signals, Rocket Wallet Signals** (signals.coincodecap.com) — публичный лог ВСЕХ сигналов,
  без цензуры проигрышей. У проекта эта механика уже реализована (`format_channel_result`) — взять их
  подход к тому, как это *подаётся* (явное публичное обязательство, а не тихая политика).
- **Binance Killers, Wolfx, Learn2Trade** — классические примеры разрыва между заявленным и фактическим
  винрейтом: WolfX заявляли 98%, независимый трекер SmartOptions.io насчитал 86.44%; Binance Killers
  заявляли ~92% факт 77.78% (smartoptions.io). Урок — не повторять: любое число точности без независимой
  верификации будет воспринято как то же самое вранье.
- **Cornix** (cornix.io, help.cornix.io) — де-факто отраслевой стандарт авто-исполнения сигналов из
  Telegram. Взять: машиночитаемый формат поста (Entry/Stop-Loss/Take-Profit N), не встраивая свой
  executor и не касаясь чужих ключей.
- **Myfxbook** (myfxbook.com) — эталон верификации трек-рекорда у форекс-сигнальных провайдеров через
  независимый сторонний сервис. Прямой аналог для крипто-Telegram-ниши — TGStat/Telemetr (архив постов)
  + попытка попасть в сравнительные обзоры типа CoinCodeCap/SmartOptions.io (независимый пересчёт метрик,
  не только архивация).
- **AO Trading** (aotrading.io) — 7 дней полного доступа → промо-месяц $49 → $149 стандарт; **Learn2Trade**
  — 30-дневный триал, заявляет 82% retention. Ориентир для реального триала вместо текущего бессрочного
  free-тира.
- **Ogvio** ($10 за каждые 3 друга), **Onward BTC** (месяц бесплатно за оплаченную регистрацию по
  рефералке), **Learn2Trade** (партнёрская комиссия) — паттерн: реф-бонус всегда за реальный платёж,
  никогда за регистрацию.
- **TeleClaw** — lifetime revenue share для партнёров-инфлюенсеров; ориентир для бартерных договорённостей
  с микро-KOL в SMC/ICT-нише.
- **SchoolofTrade.com** — оштрафован CFTC на $5 млн именно за нечестный трек-рекорд сигнального сервиса
  (courthousenews.com, law360.com). Прямое подтверждение цены вопроса, а не абстрактный риск.
- **Binance Killers, Fed Russian Insiders** — ценник $249–750/мес продаёт «институциональную альфу» без
  верификации. Проект с честным near-zero результатом обязан оставаться в нижнем ценовом сегменте
  ($30–150/мес, mudrex.com, margex.com) — цена должна отражать «инструмент + честная статистика», а не
  «алгоритм с эджем».

## Roadmap по приоритету

### Быстрые победы (1–2 недели)

> **СТАТУС (08.07.2026): все код-задачи корзины РЕАЛИЗОВАНЫ** (108 тестов зелёные, прошли
> адверсариальную ревизию — 19 подтверждённых финдингов исправлено, в т.ч.: пустой override
> cryptobot_token не гасил env-токен; win-back стрелял в том же такте, что и «подписка истекла»
> (добавлена нижняя граница задержки + окно [2, 5] дней); эскалация заявок могла зациклиться
> при недоставке одному из админов (метка теперь безусловная); Cornix-блок терял точность цен
> через {:g} (фикс-формат до 8 знаков); бэкап через tmp+atomic replace; заявка «Я оплатил»
> пишется до пинга админам + дедуп открытых заявок; закрытие заявок атомарно с /grant).
> Выполнено: задачи 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 18 + манифест честности (12) опубликован
> и закреплён в канале. Осталось НЕ-код (делает пользователь): 6 (получить Crypto Pay токен →
> /cryptobottoken), 13 (TGStat/Telemetr), 14 (верификаторы), 15 (Telegram Affiliate),
> 16 (Bybit Affiliate), 17 (чек-лист — применять), 19 (взаимный пиар).

#### A. Честность внутри бота (код)

**1. `/about` — честный дисклеймер v2 и анти-скам врезка.** Юристы ниши (Bracewell/Cointelegraph) прямо
пишут, что голое «not financial advice» само по себе бесполезно без реального соответствия — нужен состав
из трёх частей: природа сервиса, честный итог бэктеста, гео/возрастной дисклеймер + анти-имперсонация.
Похожий трёхчастный стандарт использует UK FCA для крипто-маркетинга (fca.org.uk).

**Файлы:** `app/delivery/bot.py` (константа `DISCLAIMER_FULL`, функция `format_about()`, хэндлер
`Command("about")`), `tests/test_bot.py`.

**Шаги:**
1. Добавить константу `DISCLAIMER_FULL` рядом с `_LINE` и функцию `format_about() -> str` с тремя блоками:
   (а) «RSI Range Signals — автоматический публикатор технических SMC-сетапов. Это НЕ инвестиционный
   совет, НЕ индивидуальная рекомендация и НЕ услуга торгового советника — регистрации как CTA/RIA у
   сервиса нет»; (б) «По собственному walk-forward бэктесту за 12 мес на BTC/ETH/SOL устойчивого
   преимущества после издержек не обнаружено (см. `/backtest`). Сигналы — инструмент для проверки
   собственной гипотезы, не обещание результата»; (в) «18+. Не предназначено для юрисдикций, где такие
   услуги запрещены. Мы никогда не пишем первыми в личку и не просим оплату вне этого бота — если кто-то
   пишет от имени RSI Range, это не мы».
2. Зарегистрировать `@dp.message(Command("about"))` → `m.answer(format_about())`.
3. В `_start` и `_help` добавить одну строку-ссылку: `"/about — как устроен сервис и честный дисклеймер"`.
4. Тест `test_format_about_contains_disclaimer_parts` — проверка ключевых подстрок (не инвестсовет,
   бэктест, 18+, «не мы»).
5. Оставить в `format_about()` место для одной строки, которая добавится задачами ниже: ссылка на
   TGStat/Telemetr (квик-вин E), пометка про Cornix-совместимость (квик-вин C), упоминание бонуса за
   USDT (квик-вин B) — не плодить для этого отдельные функции, дописывать в этот же текст.

**Критерий приёмки:** `/about` отвечает текстом с тремя блоками; `/start`/`/help` содержат ссылку;
`make test` зелёный.

**Промпт для Fable 5:** «В `app/delivery/bot.py` добавь функцию `format_about() -> str` (без побочных
эффектов, по образцу `format_report`/`format_stats`) с текстом из трёх абзацев: описание природы сервиса
(не инвестсовет), честная сводка бэктеста со ссылкой на `/backtest`, гео/возрастной дисклеймер и
анти-имперсонация предупреждение. Зарегистрируй `@dp.message(Command("about"))` в `main()`. Добавь одну
строку про `/about` в текст хэндлеров `_start` и `_help`. Добавь тест в `tests/test_bot.py`, проверяющий
нужные подстроки. Прогони `make test`.»

---

**2. `/backtest` — честный результат прямо в боте.** Сейчас факт «бэктест не нашёл края» живёт только в
`docs/chs_docs/` (вынесено из репозитория). CFTC Rule 4.41 требует от торговых советников сопровождать
гипотетические результаты явным предупреждением «HYPOTHETICAL PERFORMANCE RESULTS HAVE MANY INHERENT
LIMITATIONS» (образец такого текста в проде — crosstrade.io/legal/trading-disclaimer) — это одновременно
юридическая необходимость и упущенное УТП: ни один конкурент не публикует отрицательный бэктест.

**Файлы:** `app/delivery/bot.py` (`format_backtest_summary()`, хэндлер `Command("backtest")`).

**Шаги:**
1. Добавить `format_backtest_summary() -> str`: период (12 мес, BTC/ETH/SOL), метод (walk-forward без
   лукахеда), вывод («устойчивого механического края не обнаружено, лучший кандидат ETH/15m Setup1:1
   фальсифицирован на genuine out-of-sample»), и явный дисклеймер в духе CFTC 4.41 — «это смоделированный
   расчёт задним числом, подвержен hindsight bias, не учитывает реальные проскальзывание/ликвидность —
   в отличие от `/stats`, который считает по факту исполненных сделок».
2. Хэндлер `@dp.message(Command("backtest"))` → `m.answer(format_backtest_summary())`.
3. Упомянуть `/backtest` в `/start` и `/help`.
4. Тест на ключевые фразы («смоделирован»/«гипотетич», «не обнаружено», «/stats»).

**Критерий приёмки:** `/backtest` отдаёт честную сводку без цифр несуществующего винрейта; `/backtest`
не зависит от БД (статический текст, не падает при пустом контексте); `make test` зелёный.

**Промпт для Fable 5:** «Добавь в `app/delivery/bot.py` функцию `format_backtest_summary() -> str` —
статический честный текст о результате бэктеста (BTC/ETH/SOL, 12 мес walk-forward без лукахеда,
устойчивого края не найдено, лучший кандидат ETH/15m Setup1:1 фальсифицирован на out-of-sample) с явным
дисклеймером в стиле CFTC Rule 4.41: «это смоделированный расчёт задним числом, подвержен hindsight
bias — в отличие от `/stats`, который считает исход по факту исполненных сделок». Зарегистрируй
`Command("backtest")` в `main()`. Добавь упоминание в `_start`/`_help`. Тест в `tests/test_bot.py` на
ключевые фразы. `make test` зелёный.»

---

**3. Честные метки в `/stats` и канале — размер выборки + «без ручных правок».** `format_stats` уже
показывает `expectancy_r` (лучше win-rate-only подхода конкурентов), но не предупреждает о статистически
малой выборке — порог доверия 30–50+ закрытых сделок, значимости — 100+ (tradezella.com, crosstrade.io).
Отдельно: в коде физически нет пути ручного изменения `result_r`/`status` — исход всегда пишет
автоматический скоринг (`journal.py: update_outcomes_from_bars`) — это уже истинное утверждение, конкуренты
как раз замечены на удалении слитых сигналов задним числом (mudrex.com, coincodecap.com), и явная метка
формирует доверие почти бесплатно.

**Файлы:** `app/delivery/bot.py` (`format_stats`, `format_channel_stats`), `tests/test_bot.py`.

**Шаги:**
1. В обеих функциях: если `st["trades"] < 100` — добавить строку `f"⚠️ Выборка пока мала
   ({st['trades']} закрытых сделок) — для статистической значимости нужно 100+. Не делай выводов о
   точности на этом объёме."`.
2. В обеих функциях добавить строку `"✅ 100% исходов — автоматический скоринг по OHLCV, без ручных
   правок"` рядом с существующим дисклеймером «не финансовый совет».
3. Обновить тесты `test_format_stats_and_calc`/`test_format_channel_stats` — случаи `trades<100` (есть
   предупреждение) и `trades>=100` (нет), плюс наличие строки про «без ручных правок» в обоих случаях.

**Критерий приёмки:** обе метки видны в нужных условиях; тесты зелёные.

**Промпт для Fable 5:** «В `app/delivery/bot.py` в функциях `format_stats` и `format_channel_stats`
добавь: (1) условную строку-предупреждение о малой выборке, если `st['trades'] < 100` (текст:
`⚠️ Выборка пока мала ({N} закрытых сделок) — для статистической значимости нужно 100+.`); (2)
безусловную строку `✅ 100% исходов — автоматический скоринг по OHLCV, без ручных правок` рядом с
существующим дисклеймером. Обнови/добавь тесты в `tests/test_bot.py` на оба случая (N<100 и N>=100) и
на наличие второй строки. `make test` зелёный.»

---

**4. Анти-скам строка в `/start`/`/help` + аудит триггерных слов.** Ниша наводнена имперсонаторами и
«инвестклубами», вербующими в личку под видом сигнальных сервисов (SEC против Morocoin/Berge/Cirkor,
декабрь 2025, $14 млн; ФСБ — дело «Волк с Мосбиржи», апрель 2026, forbes.ru, themoscowtimes.com). ЦБ РФ
и независимые обзорщики (cbr.ru/inside/warning-list, coinspot.io, vklader.com) относят слова «премиум»,
«эксклюзивный», «гарантия», «инвестиционная рекомендация», «советник» к прямым маркерам финпирамиды.

**Файлы:** `app/delivery/bot.py` (`_start`, `_help`).

**Шаги:**
1. Добавить в оба текста строку: `"🔒 Мы никогда не пишем первыми в личку с предложениями инвестировать
   и не просим оплату вне этого бота (только кнопки /subscribe)."`
2. Прогнать `grep -ni 'премиум\|эксклюзив\|гарант\|советник' app/delivery/bot.py` — перефразировать
   найденное на нейтральные формулировки о прозрачности/статистике.

**Критерий приёмки:** обе команды содержат анти-скам строку; grep по триггерным словам в пользовательских
текстах `bot.py` пуст; `make test` зелёный (обновить тесты с точным сравнением текста, если такие есть).

**Промпт для Fable 5:** «В `app/delivery/bot.py` в обработчиках `Command("start")` и `Command("help")`
добавь строку `"🔒 Мы никогда не пишем первыми в личку с предложениями инвестировать и не просим оплату
вне этого бота (только кнопки /subscribe)."`. Прогони `grep -ni 'премиум\|эксклюзив\|гарант\|советник'
app/delivery/bot.py` и убедись, что таких слов в пользовательских текстах нет (если найдёшь — перефразируй).
Прогони `make test`, обнови тесты с точным сравнением текста, если правка их задела.»

---

#### B. Платежи — снять трение (код + не код)

**5. CryptoBot-токен из Telegram, без правки `.env`.** Вся админ-панель принципиально работает без
правки `.env`/рестартов через `runtime.py` — кроме `CRYPTOBOT_TOKEN`. Это разрыв ровно там, где он
больнее всего: переход с полу-ручного USDT на авто закрывает топ-жалобу ниши «оплата прошла — доступ
выдан с задержкой» (Learn2Trade, Wolfx, trustpilot.com). Код `create_cryptobot_invoice`/`poll_cryptobot`
уже готов — не хватает точки входа для токена.

**Файлы:** `app/runtime.py`, `app/delivery/payments.py` (`cryptobot_enabled`, `_cryptobot_call`,
`create_cryptobot_invoice`), `app/delivery/bot.py` (`/cryptobottoken`).

**Шаги:**
1. В `runtime.py`: `cryptobot_token() -> str` — `_raw("cryptobot_token") or get_settings().cryptobot_token
   or ""` (админский override приоритетнее `.env`).
2. В `payments.py` заменить чтение `get_settings().cryptobot_token` на `runtime.cryptobot_token()` во всех
   трёх местах (лениво импортировать `runtime`, как уже делается в файле).
3. В `bot.py`: `/cryptobottoken <токен>` (admin-only, паттерн `_set(m, "cryptobot_token", token, ...)`),
   `/cryptobottoken -` очищает. В ответе НЕ echo'ить токен целиком — только последние 4 символа.

**Критерий приёмки:** после `/cryptobottoken <tok>` без правки `.env` и рестарта `payments.
cryptobot_enabled()` возвращает `True`, кнопка USDT в `/subscribe` переключается на авто-режим. Тест с
моком `_cryptobot_call`. `make test` зелёный.

**Промпт для Fable 5:** «В `app/runtime.py` добавь `cryptobot_token() -> str` (settings-override поверх
`get_settings().cryptobot_token`, дефолт `""`), по образцу `usdt_invoice_url()`. В
`app/delivery/payments.py` замени все обращения к `get_settings().cryptobot_token` (в
`cryptobot_enabled()`, `_cryptobot_call()`, `create_cryptobot_invoice()`) на `runtime.cryptobot_token()`
(лениво `from app import runtime` внутри функций). В `app/delivery/bot.py` добавь
`@dp.message(Command("cryptobottoken"))` admin-only, формат `/cryptobottoken <токен>` (сохранить через
`_set(m, "cryptobot_token", token, f"Токен сохранён (…{token[-4:]})")`), `/cryptobottoken -` очищает.
Обнови `tests/test_payments.py` — тест, что после `runtime.set_value("cryptobot_token", "x")`
`cryptobot_enabled()` истинно даже без `CRYPTOBOT_TOKEN` в окружении. `make test`.»

---

**6. Включить авто-USDT через CryptoBot** (не код — зарегистрироваться в @CryptoBot, получить API-токен,
ввести через `/cryptobottoken` из задачи 5). Пока активен полу-ручной режим (`/usdtlink` → «Я оплатил» →
ручной `/grant`), проект структурно уязвим к самой частой жалобе ниши.

**Шаги:** зарегистрировать бота в @CryptoBot, получить токен, ввести `/cryptobottoken <токен>` в самом
боте (не в `.env`).

**Критерий приёмки:** `/subscribe` показывает автоматическую кнопку оплаты USDT вместо ссылки+«Я оплатил».

---

**7. Бонус-дни за оплату USDT/CryptoBot вместо Stars** (зависит от 5–6). Bot Payments API официально
поддерживает Stars (`currency=XTR`), но эффективная комиссия на мобильных достигает ~38%: Apple/Google
берут 30% с покупки Stars, плюс 5–15% спред при конвертации Stars→TON через Fragment
(core.telegram.org/bots/payments-stars). USDT через CryptoBot обходится продавцу заметно дешевле —
разумно перенаправлять туда бонусом, а не наценкой на Stars.

**Файлы:** `app/runtime.py`, `app/delivery/payments.py` (`poll_cryptobot`), `app/delivery/bot.py`
(`/usdtbonus`, тексты `_pay_usdt`/`_subscribe_kb`).

**Шаги:** добавить `runtime.usdt_bonus_days() -> int` (ключ `usdt_bonus_days`, дефолт 2); в
`poll_cryptobot()` при активации передавать `days=p["days"] + runtime.usdt_bonus_days()`; добавить
`/usdtbonus <N>` (admin, паттерн `/freeweek`); упомянуть бонус в тексте кнопки/сообщения `pay_usdt`.

**Критерий приёмки:** оплаченный cryptobot-инвойс на N дней продлевает `paid_until` на
`N + usdt_bonus_days()`. Тест в `tests/test_payments.py`. `make test` зелёный.

**Промпт для Fable 5:** «В `app/runtime.py` добавь `usdt_bonus_days() -> int` (ключ settings
`usdt_bonus_days`, дефолт 2) по образцу `free_per_week()`. В `app/delivery/payments.py::poll_cryptobot()`
замени `_extend_and_activate(conn, p["tg_id"], p["days"])` на `_extend_and_activate(conn, p["tg_id"],
p["days"] + runtime.usdt_bonus_days())` (лениво `from app import runtime`). В `app/delivery/bot.py`
добавь `/usdtbonus <N>` (админ, паттерн `/freeweek`) и обнови текст сообщения после `pay_usdt` — добавь
«+N дней бонусом за оплату в USDT». Тест в `tests/test_payments.py`, `make test`.»

---

**8. USDT-заявка: SLA + эскалация + win-back напоминание при истечении.** Разбор трёх черновиков этой
фичи с несовместимыми схемами (`004_usdt_claims.sql` с разными колонками в двух местах, `006_usdt_claims.sql`
как третий вариант) — здесь один канонический вариант, объединяющий SLA-эскалацию заявок и win-back в
одну миграцию, потому что обе фичи касаются одного и того же цикла «жизнь подписки». Индустриальные
данные по dunning (elenaverna.com, baremetrics.com, butterpayments.com): многоканальный повтор/эскалация
восстанавливает 55–65% зависших оплат против ~35% при одной попытке, 80% восстановлений — в первые 7 дней;
~40% отказов на первом периоде — involuntary/friction churn, а не осознанный отказ.

**Файлы:** `migrations/004_usdt_claims_and_winback.sql` (новая), `app/delivery/payments.py`
(`record_usdt_claim`, `resolve_usdt_claims`, `stale_usdt_claims`, `mark_claim_escalated`,
`due_for_winback`, `mark_winback_reminded`, правка `admin_grant`), `app/delivery/bot.py` (`_usdt_claim`,
`_maintenance`), `tests/test_payments.py`.

**Шаги:**
1. Миграция:
   ```sql
   CREATE TABLE IF NOT EXISTS usdt_claims (
       id             INTEGER PRIMARY KEY AUTOINCREMENT,
       tg_id          INTEGER NOT NULL REFERENCES users(tg_id),
       created_at     TEXT    NOT NULL,
       resolved_at    TEXT,
       escalated_at   TEXT
   );
   CREATE INDEX IF NOT EXISTS idx_usdt_claims_open ON usdt_claims(tg_id, resolved_at);
   ALTER TABLE users ADD COLUMN winback_reminded_at TEXT;
   ```
2. В `payments.py`: `record_usdt_claim(tg_id: int) -> int` (INSERT, вернуть id); `resolve_usdt_claims
   (tg_id: int) -> None` (UPDATE всех открытых заявок юзера `resolved_at=now WHERE tg_id=? AND
   resolved_at IS NULL`); `stale_usdt_claims(hours: int = 6) -> list` (`resolved_at IS NULL AND
   escalated_at IS NULL AND created_at <= now-hours`); `mark_claim_escalated(claim_id: int) -> None`;
   `due_for_winback(days_after: int = 2) -> list` (`tier='free' AND paid_until IS NOT NULL AND
   paid_until <= now AND paid_until >= now-days_after AND winback_reminded_at IS NULL`);
   `mark_winback_reminded(tg_id: int) -> None`.
3. В `admin_grant(tg_id, days)` — в начале функции вызвать `resolve_usdt_claims(tg_id)` (`/grant` в
   проекте сейчас используется ТОЛЬКО как подтверждение реальной оплаты по потоку «Я оплатил» — если
   позже `/grant` начнут использовать и для чистых подарков (см. задачу про giveaway в среднем сроке),
   нужно завести отдельную команду `/grantfree`, не закрывающую заявки).
4. В `_extend_and_activate` при продлении сбрасывать `winback_reminded_at=NULL` вместе с существующим
   `renew_reminded_at=NULL`.
5. В `bot.py`, `_usdt_claim`: после уведомления админов вызвать `payments.record_usdt_claim(u.id)`; в
   ответе юзеру — конкретный SLA: «Спасибо! Обычно подтверждаем в течение нескольких часов (максимум —
   сутки)».
6. В `_maintenance()`: цикл по `payments.stale_usdt_claims(6)` — напоминание админам `f"⏰ Заявка на
   оплату USDT от {tg_id} висит без ответа >6ч — /grant {tg_id}"`, затем `mark_claim_escalated` (пингует
   один раз, не спамит); цикл по `payments.due_for_winback(2)` — сообщение юзеру `"👋 Соскучились по
   сигналам? /subscribe — вернуть доступ за минуту."`, затем `mark_winback_reminded`. Оба цикла — в
   try/except с `log.warning` на каждого получателя, по аналогии с остальными блоками `_maintenance`.
7. Тесты: запись/резолв заявки, `stale_usdt_claims` находит просроченную и не находит свежую/резолвнутую/
   уже эскалированную, `due_for_winback` находит только юзеров в окне `[now-2d, now]` без предыдущего
   напоминания.

**Критерий приёмки:** заявка сохраняется при «Я оплатил»; `/grant` закрывает все открытые заявки юзера;
заявка без ответа >6ч пингует админа ровно один раз; истёкший 1–2 дня назад юзер получает ровно один
win-back за цикл истечения; `make test` зелёный.

**Промпт для Fable 5:** «Добавь миграцию `migrations/004_usdt_claims_and_winback.sql`: таблица
`usdt_claims(id, tg_id REFERENCES users(tg_id), created_at, resolved_at, escalated_at)` + индекс на
`(tg_id, resolved_at)`, плюс `ALTER TABLE users ADD COLUMN winback_reminded_at TEXT`. В
`app/delivery/payments.py` добавь `record_usdt_claim(tg_id) -> int`, `resolve_usdt_claims(tg_id) -> None`
(закрывает ВСЕ незакрытые заявки юзера), `stale_usdt_claims(hours=6) -> list` (незакрытые,
неэскалированные, старше `hours`), `mark_claim_escalated(claim_id) -> None`, `due_for_winback(days_after=2)
-> list` (по образцу `due_for_reminder`, но для уже истёкших: `tier='free' AND paid_until IS NOT NULL AND
paid_until BETWEEN (now-days_after) AND now AND winback_reminded_at IS NULL`), `mark_winback_reminded
(tg_id) -> None`. В `admin_grant` в начале вызови `resolve_usdt_claims(tg_id)`. В `_extend_and_activate`
добавь `winback_reminded_at=NULL` в UPDATE рядом с `renew_reminded_at=NULL`. В `app/delivery/bot.py`: в
хэндлере `_usdt_claim` вызови `payments.record_usdt_claim(u.id)` и добавь фразу про SLA (`несколько часов,
максимум сутки`) в ответ пользователю. В `_maintenance()` добавь два цикла — эскалация по
`stale_usdt_claims(6)` с сообщением админам и `mark_claim_escalated`, и win-back по `due_for_winback(2)`
с сообщением юзерам и `mark_winback_reminded` — оба в try/except с `log.warning` на каждого получателя.
Добавь тесты в `tests/test_payments.py`. `make test` зелёный.»

---

#### C. Cornix-совместимость (код, малый объём)

**9. Cornix-совместимые поля в сигнале канала.** Cornix (cornix.io, help.cornix.io) — де-факто стандарт
исполнения сигналов из Telegram-каналов, множество топ-каналов (Wolfx, CryptoNinjas) рекламируют
«Cornix-совместимость» как фичу. Не встраивая свой executor, можно дать пользователям подключить СВОЙ
Cornix к каналу `RSI_Range`, сделав пост машиночитаемым. Официальный формат
(help.cornix.io/signal-posting-format): `Pair: BTC/USDT`, `Type: Long`, `Entry: 42,500`, `Take-Profit 1:
43,500`, `Stop-Loss: 41,800`, `Exchange: Binance` — важно писать «Stop-Loss» полным словом (не «S/L»), TP
нумеровать отдельными строками.

**Файлы:** `app/delivery/bot.py` (`format_cornix_block`, подключение в `_broadcast`), `app/runtime.py`
(`cornix_block_enabled`), `tests/test_bot.py`, `tests/test_runtime.py`.

**Шаги:**
1. Добавить рядом с `format_channel_signal` (саму функцию не менять):
   ```python
   def format_cornix_block(signal: Signal) -> str:
       """Доп. блок для авто-парсинга Cornix (help.cornix.io/signal-posting-format): открытым текстом,
       без эмодзи рядом с цифрами, TP пронумерованы, полное слово 'Stop-Loss'. Добавляется К обычному
       посту канала, не заменяет его."""
       pair = signal.symbol.split(":")[0]
       side = "Long" if signal.side.value == "LONG" else "Short"
       tps = "\n".join(f"Take-Profit {i}: {t:g}" for i, t in enumerate(signal.take_profits, 1))
       return (
           f"\n\nPair: {pair}\nType: {side}\nEntry: {signal.entry:g}\n"
           f"{tps}\nStop-Loss: {signal.stop:g}\nExchange: Bybit"
       )
   ```
2. В `runtime.py`: `cornix_block_enabled() -> bool` (дефолт `False`, ключ `cornix_block_enabled`).
3. В `_broadcast`: если `runtime.channel_mode() == "full"` и `cornix_block_enabled()` — добавить
   `format_cornix_block(sig)` к тексту канального поста (не в личные сообщения, не в тизер).
4. Админ-команда `/cornix on|off` (паттерн `/channel on|off`).
5. Дефолт — выключено. Перед включением на боевом канале — ручная проверка в тестовой Telegram-группе с
   реальным бесплатным Cornix-ботом (формат Cornix может меняться — сверяться с актуальной
   help.cornix.io/articles/11659507).
6. В `format_about()` (задача 1) добавить строку: «Формат сигналов совместим с ручной настройкой
   парсера Cornix — Entry/SL/TP размечены явно».

**Критерий приёмки:** `make test` зелёный; по умолчанию блок выключен; `/cornix on` включает его только
в постах канала при `channel_mode=full`; ручная сверка с реальным Cornix зафиксирована в коммите/PR
перед включением на проде.

**Промпт для Fable 5:** «Добавь опциональный Cornix-совместимый блок к постам сигналов в канале RSI Range
Signals. Добавь `format_cornix_block(signal: Signal) -> str` в `app/delivery/bot.py` рядом с
`format_channel_signal` (саму функцию не меняй). Формат см. help.cornix.io/signal-posting-format:
`Pair: BTC/USDT`, `Type: Long|Short`, `Entry: <число>`, `Take-Profit N: <число>` (по строке на каждый TP),
`Stop-Loss: <число>` (не `S/L`), `Exchange: Bybit`. Symbol в проекте вида `BTC/USDT:USDT` — pair =
`symbol.split(':')[0]`. В `app/runtime.py` добавь `cornix_block_enabled() -> bool` (дефолт `False`, ключ
`cornix_block_enabled`). В `_broadcast` — если `channel_mode()=='full'` и `cornix_block_enabled()`,
добавь к `ch_text` результат `format_cornix_block(sig)`. Добавь `/cornix on|off` (admin, паттерн
`/channel on|off`). Добавь тесты: `format_cornix_block` на фиксированном `Signal` (проверить
Pair/Type/Entry/Take-Profit N/Stop-Loss/Exchange в тексте), дефолт `cornix_block_enabled()==False`.
Обнови `README.md` таблицу админ-команд. `make test` зелёный. В коммите отметь: перед `/cornix on` на
проде нужна ручная проверка в реальном Cornix (тестовая Telegram-группа) — не покрывается автотестами.»

---

#### D. Надёжность — бэкап БД (код, критично, делать первым в этой корзине)

**10. Авто-бэкап SQLite (`data/signals.db`).** Единственный источник правды (сделки, подписчики, деньги,
весь публичный трек-рекорд) сейчас не бэкапится вообще. Самый дешёвый anti-churn/anti-trust-loss щит из
всего документа: потеря базы = потеря того единственного, что удерживает подписчиков в этой нише
(прозрачная история, не обещания доходности). Сделать ДО или вместе с задачей `/trackrecord` (крупная
ставка ниже) — иначе первому экспорту нечего будет защищать при потере базы.

**Файлы:** `scripts/backup_db.py` (новый), `deploy/com.rsirange.backup.plist` (новый), `Makefile`.

**Шаги:**
1. `backup_once(dst_dir: Path = Path("data/backups"), keep_last: int = 14) -> Path`: через
   `sqlite3.Connection.backup()` (встроенный SQLite Backup API — онлайн-копирование, безопасно при
   активной записи под WAL, не требует останавливать бота) скопировать `get_settings().db_path` в
   `dst_dir/signals-<UTC ISO компакт>.db`.
2. Ротация: оставлять последние `keep_last` файлов по маске `signals-*.db`, старые удалять.
3. `main()` вызывает `backup_once()`, логирует путь через `app.logging_conf.get_logger`.
4. Отдельный `launchd`-агент `deploy/com.rsirange.backup.plist` (по образцу `com.rsirange.bot.plist`, но
   `StartCalendarInterval` раз в сутки в 04:00 вместо `KeepAlive`) — независим от процесса бота, чтобы
   продолжать работать, даже если бот временно упал.
5. `Makefile`: цель `backup-db`. `README.md`: раздел «Бэкап БД» с инструкцией установки агента.

**Критерий приёмки:** `python3 -m scripts.backup_db` создаёт файл в `data/backups/`, бот продолжает
работать во время бэкапа, при >14 файлах старые удаляются. Тест `tests/test_backup_db.py`: бэкап
временной БД читается отдельным соединением; отдельный тест на ротацию.

**Промпт для Fable 5:** «Реализуй `scripts/backup_db.py`: `backup_once(dst_dir: Path =
Path("data/backups"), keep_last: int = 14) -> Path`, использующая `sqlite3.Connection.backup()` для
копирования `get_settings().db_path` в `dst_dir/signals-<UTC ISO компакт>.db`, с ротацией по `keep_last`.
`main()` вызывает `backup_once()` и логирует путь через `app.logging_conf.get_logger('backup_db')`, по
образцу `scripts/init_db.py`. Добавь `deploy/com.rsirange.backup.plist` — launchd-агент по образцу
`deploy/com.rsirange.bot.plist`, но с `StartCalendarInterval` (час 4, минута 0) вместо `KeepAlive`,
запускающий `python3 -m scripts.backup_db`. Добавь цель `backup-db` в `Makefile`. Напиши
`tests/test_backup_db.py` по образцу фикстуры `fresh_db` из `tests/conftest.py`: создать тестовую БД,
вставить строки в `signals`, вызвать `backup_once` с `tmp_path`, проверить читаемость результата отдельным
sqlite3-соединением; тест на ротацию `keep_last`. `make test` зелёный.»

---

#### E. Атрибуция роста — базовая схема (код, малый объём)

**11. Метка источника подписчика (`acquisition_source`) в deep-link.** План запускает 5+ платных и
бесплатных каналов роста (реферал, Cornix-магнит, TGStat, партнёрка Telegram, партнёрка биржи, бартер с
инфлюенсерами, взаимный пиар, платная реклама, KOL) — но кроме `/referrals` (покрывает один канал из
девяти) нет ни одного способа узнать, откуда пришёл платящий пользователь. Дёшево закрыть простым полем
у `users` и суффиксом в deep-link — используется дальше в среднесрочной задаче «атрибуция-дашборд».

**Файлы:** `migrations/005_acquisition_source.sql` (новая), `app/delivery/payments.py`
(`set_acquisition_source`), `app/delivery/bot.py` (`_start`).

**Шаги:**
1. `ALTER TABLE users ADD COLUMN acquisition_source TEXT;`
2. `set_acquisition_source(tg_id: int, source: str) -> None` — простой `UPDATE ... WHERE tg_id=? AND
   acquisition_source IS NULL` (first-touch — не перезаписывать, если юзер уже пришёл раньше по другой
   ссылке).
3. В `_start`: расширить существующий парсинг deep-link — если `/start src_<label>` (не `ref_<id>`),
   вызвать `set_acquisition_source(tg_id, label)` для новых юзеров. Формат ссылок для партнёров:
   `https://t.me/rsirange_bot?start=src_kolname` / `src_tgstat` / `src_ads1` и т.п. — короткие
   произвольные метки, без пробелов.

**Критерий приёмки:** новый юзер, пришедший по `/start src_x`, сохраняет `acquisition_source='x'`;
повторный визит по другой ссылке ничего не меняет; юзер без метки — `NULL` как раньше. Тест в
`tests/test_payments.py`.

**Промпт для Fable 5:** «Добавь миграцию `migrations/005_acquisition_source.sql`: `ALTER TABLE users ADD
COLUMN acquisition_source TEXT;`. В `app/delivery/payments.py` добавь `set_acquisition_source(tg_id: int,
source: str) -> None` (`UPDATE users SET acquisition_source=? WHERE tg_id=? AND acquisition_source IS
NULL` — first-touch, не перезаписывать). В `app/delivery/bot.py`, в хэндлере `_start`, рядом с уже
существующим (или добавляемым в задаче про реферальную программу) парсингом `/start ref_<id>` добавь
ветку: если параметр начинается с `src_` — вызови `set_acquisition_source(tg_id, label)` для новых
юзеров, где `label = parts[1][4:]`. Тест в `tests/test_payments.py`: метка сохраняется один раз, вторая
ссылка её не перезаписывает. `make test` зелёный.»

---

#### F. Видимая репутация вовне (не код + мелкая правка текста)

**12. Манифест честности — закреплённый пост в канале.** Проект уже реализует главную trust-механику
ниши (публикация каждого исхода, включая стопы), но нигде явно не называет это политикой.
CoinCodeCap/Rocket Wallet Signals прямо формулируют это как обещание.

**Шаги:** разместить и закрепить в канале `RSI_Range` пост: «Мы публикуем 100% исходов сигналов — победы
и стопы — без возможности задним числом удалить или отредактировать пост (журнал идемпотентен). Каждая
сделка на счету. `/stats` — вся статистика честно».

**Критерий приёмки:** пост опубликован и закреплён.

---

**13. Регистрация канала в TGStat/Telemetr + ссылка в `/about`.** Оба сервиса — независимые архиваторы
Telegram-каналов, хранящие ВСЕ посты, включая теоретически удалённые (dtf.ru/ruslan-titov/3816217,
tgstat.ru). Готовый бесплатный «нотариус» — прямой аналог того, как форекс-провайдеры ссылаются на
Myfxbook (myfxbook.com/help/knowledge-base/verification). Заодно — видимость в каталогах TGStat/Telemetr
как канал прироста трафика.

**Шаги:** зарегистрировать `RSI_Range` на tgstat.ru и/или telemetr.me (подтверждение владения — код в
описании канала или временный пост, ~10–15 минут); добавить одну строку со ссылкой в текст
`format_about()` из задачи 1.

**Критерий приёмки:** ссылка открывается и показывает реальную историю канала; `/about` содержит ссылку.

---

**14. Обращение к независимым верификаторам (CoinCodeCap Signals, SmartOptions.io).** TGStat/Telemetr
архивируют посты, но не пересчитывают винрейт независимо — это делают именно те сайты, которые в research
ловят конкурентов на завышении (signals.coincodecap.com, smartoptions.io). Единственный тип верификации,
который реально ловит ложь у конкурентов, а не просто фиксирует неудаление постов.

**Шаги:** написать в CoinCodeCap Signals/SmartOptions.io с предложением включить `RSI_Range` в
сравнительный обзор как пример честного near-zero edge (после того как накопится трек-рекорд за
несколько месяцев — иначе показывать нечего).

**Критерий приёмки:** обращение отправлено; независимо от ответа — сама попытка задокументирована как
довод для будущего маркетинга («мы открыты для независимой проверки»).

---

#### G. Партнёрские платформы (не код)

**15. Нативная Telegram Affiliate Program для Stars-оплат.** С декабря 2024 Telegram даёт любому боту
встроенную партнёрскую программу для Stars-платежей: владелец задаёт `commission_permille` (обычно 20–40%
recurring для подписочных ботов) через профиль бота в приложении (`Edit → Affiliate Program`), Telegram
сам считает и платит партнёрам звёздами (telegram.org/tour/affiliate-programs, blog.telegram.org). Не
заменяет реферальную систему из среднего срока (та работает для всех методов оплаты, эта — только для
Stars), а дополняет бесплатным каналом привлечения незнакомых партнёров.

**Шаги:** открыть профиль `@rsirange_bot` → Edit → Affiliate Program → задать `commission_permille`
(например 20%, **ставку после создания можно только повышать, не понижать** — начинать консервативно) и
`duration_months` (можно не ограничивать) → опубликовать.

**Критерий приёмки:** программа видна в разделе Affiliate Programs Telegram; тестовый переход и оплата
через Stars на втором аккаунте корректно засчитывается партнёру.

---

**16. Партнёрство с биржей — Bybit Affiliate Program.** Пропало между research и черновиками плана, хотя
явно названо в исследовании отдельным каналом монетизации (модель OnwardBTC — бесплатный доступ за
реф-регистрацию на бирже). Bybit Affiliate Program даёт до 50% revenue share с торговых комиссий
приведённых юзеров, мультиуровнево, пожизненно (affiliates.bybit.com, whaleportal.com). Проект уже на
ccxt+Bybit — дополнительный канал заработка без конфликта с гейтом на `executor` (не требует доступа к
чужим ключам, просто реферальная ссылка на регистрацию биржи).

**Шаги:** зарегистрироваться в affiliates.bybit.com, получить персональную реф-ссылку, разместить в
`/about` и в закреплённом посте канала («откроешь позицию по нашему сигналу на Bybit через нашу
ссылку — поддержишь канал, тебе комиссия не меняется»).

**Критерий приёмки:** реф-ссылка зарегистрирована и размещена; отслеживается через личный кабинет
партнёрки Bybit (не через код проекта).

---

#### H. Ценовая и маркетинговая политика (не код)

**17. Ценовой и маркетинговый чек-лист.** Зафиксировать явные решения на основе research:
- Не вводить заявления о винрейте/точности («92% accuracy») ни в канал, ни в описание бота — продавать
  честный `/backtest` и `/stats` как УТП.
- Не заводить lifetime-тариф — при неподтверждённом эдже и полу-ручном/single-admin процессе это
  бессрочное обязательство при возможном развороте стратегии; максимум месяц + год со скидкой ~15–20%
  (см. годовой тариф в среднем сроке).
- Избегать слов «премиум», «эксклюзивный», «гарантия», «инвестиционная рекомендация», «советник» в
  постах канала и в `/announce` (cbr.ru/inside/warning-list).
- Не вводить performance-based money-back guarantee — только за неиспользованный период подписки, без
  привязки к прибыльности.
- Держать позиционирование в нижнем ценовом сегменте ($30–150/мес, mudrex.com, margex.com, nftplazas.com)
  — текущие дефолты (`SUB_PRICE_STARS=1000⭐`/`SUB_PRICE_USDT=$20`) уже там, не поднимать выше без нового
  повода.

**Критерий приёмки:** чек-лист прочитан и применяется как фильтр для каждого нового маркетингового текста
до публикации (не только на старте).

---

#### I. Онбординг новичков под SMC-жаргон (код, малый объём)

**18. `/glossary` — глоссарий терминов SMC/ICT.** Research прямо предупреждает: «SMC-позиционирование
требует пояснения жаргона неподготовленным подписчикам» — но нет ни одного места, объясняющего «BOS/
CHOCH», «killzone», «Setup 1:1» новому юзеру. Первый же сигнал в стиле «BOS confirmed in NY killzone, FVG
retest» без объяснения — прямой повод для отписки до того, как юзер увидит честную статистику.

**Файлы:** `app/delivery/bot.py` (`format_glossary`, хэндлер `Command("glossary")`, `_start`).

**Шаги:**
1. `format_glossary() -> str` — короткий словарь ключевых терминов, которые встречаются в
   `signal.reasons`/постах канала: BOS (Break of Structure), CHOCH (Change of Character), FVG (Fair Value
   Gap), OB (Order Block), свип ликвидности, killzone (Asia/London/NY), Setup 1:1, LTF-POI — по одному
   короткому предложению на термин, без воды.
2. Хэндлер `Command("glossary")` → `m.answer(format_glossary())`.
3. В `_start` для новых юзеров — одна строка: «Не знаком с терминами SMC? `/glossary` — коротко объясним».

**Критерий приёмки:** `/glossary` отвечает списком терминов; `/start` упоминает команду; тест на наличие
ключевых терминов в тексте.

**Промпт для Fable 5:** «Добавь в `app/delivery/bot.py` функцию `format_glossary() -> str` — короткий
глоссарий терминов SMC/ICT, используемых в постах бота: BOS, CHOCH, FVG, OB, свип ликвидности, killzone
(Asia/London/NY), Setup 1:1, LTF-POI — одно короткое пояснение на термин. Зарегистрируй
`Command("glossary")`. В хэндлере `_start` добавь строку с упоминанием команды для новых пользователей.
Добавь тест в `tests/test_bot.py`, проверяющий наличие ключевых терминов (BOS, CHOCH, FVG, killzone) в
результате. `make test` зелёный.»

---

#### J. Взаимный пиар — первый заход (не код)

**19. Обмен постами с каналами сопоставимого размера.** Даёт типично +100–300 подписчиков за один заход
(dtf.ru/pro-smm/4762864). Готовые чаты для поиска партнёров, в т.ч. нишевой крипто-чат `@crypto_vp`, и
боты-автоматизаторы (Erzy Cross); для каналов до ~3000 подписчиков часто бесплатно попасть в тематическую
папку-подборку (habr.com/ru/articles/934572).

**Шаги:** найти 3–5 партнёрских трейдинг/аналитика-каналов через `@crypto_vp` и аналогичные чаты,
договориться об обмене постами; приоритет — партнёрам, которые сами не обещают гарантированную
доходность (несовместимо с честной позицией проекта, их аудитория придёт с чужими ожиданиями).

**Критерий приёмки:** минимум 2 состоявшихся обмена; прирост подписчиков за 48 часов после каждого сверен
по счётчику Telegram или через TGStat (задача 13).

---

### Средний срок (1–2 мес)

#### A. Монетизация — жизненный цикл подписки (код)

**20. Триал — короткий ПОЛНЫЙ доступ вместо текущей недельной модели.** Текущий free-тир
(`free_signals_per_week`) — бессрочное «N сигналов/нед», а не триал. Индустриальные бенчмарки конверсии
показывают разницу на порядок: голый freemium — 2–5% (хорошо)/8–12% (отлично), полноценный
time-limited триал — 8.9–18% (opt-in без карты) и 25–50%+ (с картой) (firstpagesage.com, 1capture.io,
userpilot.com). Примеры из ниши: AO Trading — 7 дней полного доступа (aotrading.io); Learn2Trade —
30-дневный триал, заявляет 82% retention (bitdegree.org). Для проекта разумный диапазон — 3–7 дней.

**Файлы:** `migrations/006_trial.sql` (новая), `app/delivery/payments.py` (`grant_trial`),
`app/runtime.py` (`trial_days`), `app/delivery/bot.py` (`_start`, `/trialdays`).

**Шаги:**
1. `ALTER TABLE users ADD COLUMN trial_granted_at TEXT;`
2. `grant_trial(tg_id: int, days: int) -> bool` — если `trial_granted_at IS NULL` и юзер никогда не
   платил (`paid_until IS NULL`), в транзакции: `trial_granted_at=now`, вызвать
   `_extend_and_activate(conn, tg_id, days)`, вернуть `True`; иначе `False`. **Важно:** `grant_trial` не
   должен вызывать `credit_referral_bonus` (задача 22) — триал не считается реальной оплатой, это
   достигается просто тем, что `grant_trial` не вызывает эту функцию явно (без флагов и веток).
3. `runtime.trial_days() -> int` (ключ `trial_days`, дефолт 5), `/trialdays <N>` (admin, паттерн
   `/freeweek`).
4. В `_start` — после `register_user`: `applied = await asyncio.to_thread(payments.grant_trial,
   m.from_user.id, runtime.trial_days())`; если `True` — добавить в приветствие «🎁 Открыт полный доступ
   на N дней — все сигналы без ограничений». Даунгрейд по истечении — автоматически через уже
   существующий `expire_overdue()`.

**Критерий приёмки:** новый юзер получает `tier='paid'` сразу на N дней ровно один раз; повторный
`/start` триал повторно не выдаёт; по истечении `_maintenance` понижает как обычную подписку. Тест в
`tests/test_payments.py`.

**Промпт для Fable 5:** «В `migrations/006_trial.sql` добавь `ALTER TABLE users ADD COLUMN
trial_granted_at TEXT`. В `app/delivery/payments.py` добавь `grant_trial(tg_id: int, days: int) -> bool`:
в транзакции — если `trial_granted_at IS NULL` и `paid_until IS NULL` у юзера, ставь
`trial_granted_at=clock.utcnow_iso()`, вызови `_extend_and_activate(conn, tg_id, days)`, верни `True`;
иначе `False`. Не вызывай из этой функции ничего, связанного с реферальными бонусами. В
`app/runtime.py` добавь `trial_days() -> int` (ключ `trial_days`, дефолт 5). В `app/delivery/bot.py`
добавь `/trialdays <N>` (admin, паттерн `/freeweek`) и в хэндлере `_start` после `register_user` вызови
`grant_trial` и добавь строку в приветствие при `applied=True`. Тесты в `tests/test_payments.py`: триал
выдаётся один раз, не выдаётся уже плативший юзеру, естественно истекает через `expire_overdue`. `make
test`.»

---

**21. Промокоды на +N дней подписки.** В нише промокоды почти всегда дают готовые дни, а не % (проще
для ручных выдач одним администратором, не требует пересчёта цены инвойса).

**Файлы:** `migrations/007_promo_codes.sql` (новая), `app/delivery/payments.py` (`redeem_promo`,
`create_promo`), `app/delivery/bot.py` (`/promo`, `/addpromo`).

**Шаги:**
1. `promo_codes(code TEXT PRIMARY KEY, days INTEGER NOT NULL, max_uses INTEGER, used_count INTEGER NOT
   NULL DEFAULT 0, expires_at TEXT, created_at TEXT NOT NULL)`; `promo_redemptions(id INTEGER PRIMARY KEY
   AUTOINCREMENT, code TEXT NOT NULL REFERENCES promo_codes(code), tg_id INTEGER NOT NULL REFERENCES
   users(tg_id), redeemed_at TEXT NOT NULL, UNIQUE(code, tg_id))` — дедуп: один код на юзера один раз.
2. `redeem_promo(tg_id: int, code: str) -> dict` (`{"ok": bool, "days": int|None, "reason": str|None}`):
   проверить существование/срок/лимит; в транзакции — `INSERT INTO promo_redemptions` (перехват
   `IntegrityError` → `already_used`), инкремент `used_count`, `_extend_and_activate(conn, tg_id,
   row["days"])` (промокод — не платёж, реферала не награждает, как и триал).
3. `create_promo(code, days, max_uses=None, expires_at=None) -> None`.
4. `/promo <код>` (юзерская), `/addpromo <код> <дней> [лимит]` (admin).

**Критерий приёмки:** повторное применение того же кода тем же юзером не начисляет дни дважды;
исчерпанный/просроченный код отклоняется с понятным текстом. Тест в `tests/test_payments.py`.

**Промпт для Fable 5:** «В `migrations/007_promo_codes.sql` создай `promo_codes(code TEXT PRIMARY KEY,
days INTEGER NOT NULL, max_uses INTEGER, used_count INTEGER NOT NULL DEFAULT 0, expires_at TEXT,
created_at TEXT NOT NULL)` и `promo_redemptions(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL
REFERENCES promo_codes(code), tg_id INTEGER NOT NULL REFERENCES users(tg_id), redeemed_at TEXT NOT NULL,
UNIQUE(code, tg_id))`. В `app/delivery/payments.py` добавь `redeem_promo(tg_id: int, code: str) -> dict`
и `create_promo(code: str, days: int, max_uses: int | None = None, expires_at: str | None = None) ->
None` — логику дедупа и лимитов бери из докстринга `grant_subscription` как образец идемпотентности
(UNIQUE + перехват `IntegrityError`). Используй `_extend_and_activate(conn, tg_id, days)`, не вызывай
реферальные функции. В `app/delivery/bot.py` добавь `/promo <код>` (любой юзер) и `/addpromo <код>
<дней> [лимит]` (admin, паттерн `/freeweek`). Тесты в `tests/test_payments.py`. `make test`.»

---

**22. Годовой тариф со скидкой — отдельный тир, НЕ lifetime.** Рыночный ориентир на скидку за долгий
срок — 15–20% (фрейм «2 месяца в подарок», blog.invitemember.com, retentioncheck.com): MYC Signals —
$99×12=$1188 номинал vs $999 факт годовая цена (~16% off). По общим SaaS-бенчмаркам переход снижает
отток с 5–8%/мес (месячные) до 0.5–1.5%/мес (годовые) — убирает 11 из 12 ежемесячных «точек решения об
отмене» (elenaverna.com). Технически Stars как нативная recurring-подписка ограничены циклом ровно 30
дней (core.telegram.org/api/subscriptions) — НЕ проблема: годовой тариф продаётся как обычный разовый
`send_invoice` на 365 дней, а не через нативный Stars-subscription-объект.

**⚠️ Осторожно с A/B-тестами цены при запуске этого тарифа:** тестировать новую цену можно только
ПОСЛЕДОВАТЕЛЬНО (сменить глобально через `/priceyear`, сравнить окна конверсии до/после), а не показывать
разным юзерам разную цену одновременно без объяснения — по данным HBR, 76% пользователей негативно
реагируют, узнав, что платили больше других за то же самое (getmonetizely.com), а скрытая ценовая
дискриминация прямо противоречит выстроенной в этом документе позиции «мы ничего не скрываем».

**Файлы:** `app/runtime.py` (`price_stars_year`, `price_usdt_year`, `sub_days_year`), `app/delivery/bot.py`
(`/priceyear`, `_subscribe_kb`, `_pay_stars`, `_pay_usdt`).

**Шаги:**
1. `runtime.py`: `price_stars_year()`/`price_usdt_year()` (ключи `sub_price_stars_year`/
   `sub_price_usdt_year`, дефолт `0` — не настроен, не показывается), `sub_days_year()` (ключ
   `sub_days_year`, дефолт 365).
2. `/priceyear stars <amount>` / `/priceyear usdt <amount>` (admin, паттерн `/price`).
3. В `_subscribe_kb()` — кнопка года при `price_stars_year() > 0` (аналогично USDT при
   `price_usdt_year() > 0 and cryptobot_enabled()`).
4. Расширить фильтр `_pay_stars` до `F.data.in_({"pay_stars", "pay_stars_year"})` и `_pay_usdt`
   аналогично, выбирая `days`/цену по суффиксу `_year`.

**Критерий приёмки:** при настроенном годовом тире в `/subscribe` видны обе кнопки; при `price_*_year=0`
кнопка года не показывается (обратная совместимость). Тест в `tests/test_bot.py`/`tests/test_runtime.py`.

**Промпт для Fable 5:** «В `app/runtime.py` добавь `price_stars_year() -> int` (ключ
`sub_price_stars_year`, дефолт 0), `price_usdt_year() -> float` (ключ `sub_price_usdt_year`, дефолт 0.0),
`sub_days_year() -> int` (ключ `sub_days_year`, дефолт 365) — по образцу `price_stars()`/`price_usdt()`/
`sub_days()`. В `app/delivery/bot.py` добавь `/priceyear stars|usdt <amount>` (admin, паттерн `/price`).
В `_subscribe_kb()` добавь кнопку года с `callback_data="pay_stars_year"` при `price_stars_year() > 0`,
аналогично USDT при `price_usdt_year() > 0 and cryptobot_enabled()`. Расширь фильтры `_pay_stars`/
`_pay_usdt` до `F.data.in_({...,"..._year"})`, выбирая `days`/цену по суффиксу. Не меняй поведение при
незаданных `*_year`-настройках. Тесты в `tests/test_bot.py`. `make test`.»

---

#### B. Реферальная программа (код, канонический вариант)

**23. Двусторонняя реферальная программа — бонус только после подтверждённой оплаты.** По практике
ниши вознаграждение всегда привязано к реальному платящему действию: Ogvio — $10 за каждые 3 друга,
Onward BTC — месяц бесплатно за оплаченную регистрацию по рефералке, Learn2Trade — комиссия через
партнёрку (coincodecap.com, bitdegree.org). Бонус — только дополнительные дни подписки (не деньги, не
проценты, один уровень без цепочек) — защита от накрутки и от юридической квалификации «финансовая
пирамида» по критериям Банка России (cbr.ru/inside/warning-list фиксирует многоуровневые денежные схемы
как красный флаг).

**Файлы:** `migrations/008_referrals.sql` (новая), `app/delivery/payments.py` (`register_referral`,
`credit_referral_bonus`, `referral_stats`, хуки в `grant_subscription()`/`poll_cryptobot()`/
`admin_grant()`), `app/runtime.py` (`referral_bonus_days`), `app/delivery/bot.py` (deep-link в `_start`,
`/referrals`, `/refbonus`), `tests/test_payments.py`, `README.md`.

**Шаги:**
1. Миграция:
   ```sql
   CREATE TABLE IF NOT EXISTS referrals (
       id             INTEGER PRIMARY KEY AUTOINCREMENT,
       referrer_tg_id INTEGER NOT NULL REFERENCES users(tg_id),
       referred_tg_id INTEGER NOT NULL REFERENCES users(tg_id),
       created_at     TEXT    NOT NULL,
       credited_at    TEXT,
       bonus_days     INTEGER,
       UNIQUE (referred_tg_id)
   );
   CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_tg_id);
   ```
2. `register_referral(referrer_tg_id: int, referred_tg_id: int) -> bool` — если `referrer_tg_id ==
   referred_tg_id` → `False`; если `get_user(referrer_tg_id)` не существует → `False`; иначе INSERT,
   перехват `IntegrityError` (UNIQUE `referred_tg_id`, уже привязан) → `False`.
3. `credit_referral_bonus(referred_tg_id: int) -> int | None` — найти незакрытую запись по
   `referred_tg_id`, начислить `runtime.referral_bonus_days()` дней рефереру через
   `_extend_and_activate(conn, row["referrer_tg_id"], bonus_days)`, проставить `credited_at`/`bonus_days`
   (UPDATE с условием `credited_at IS NULL`, идемпотентно), вернуть `referrer_tg_id` или `None`.
4. `referral_stats(tg_id) -> dict` — `{"invited": ..., "paid": ..., "bonus_days_total": ...}`.
5. **Критично, источник частых багов:** `db.transaction()` берёт `threading.Lock` (не реентрантный,
   `app/db.py`). Вызывать `credit_referral_bonus(...)` СТРОГО ВНЕ уже открытого `with db.transaction()`
   — иначе deadlock. Подключить в трёх местах, все — ПОСЛЕ закрытия транзакции, перед `return`:
   `grant_subscription` (Stars), `poll_cryptobot` (авто-USDT, в цикле — перед `activated.append`),
   `admin_grant` (текущий полу-ручной USDT-флоу — `/grant` в проекте сейчас = подтверждение реальной
   оплаты). **Не** подключать в `grant_trial` (задача 20) и `redeem_promo` (задача 21) — они не должны
   вызывать `credit_referral_bonus`, это и есть анти-накрутка механика без дополнительных флагов.
   **Оговорка на будущее:** если `/grant` когда-нибудь станет использоваться и для чистых подарков
   (см. giveaway ниже) — завести отдельную команду `/grantfree`, не вызывающую `credit_referral_bonus`.
6. `runtime.referral_bonus_days() -> int` (ключ `referral_bonus_days`, дефолт 7).
7. В `_start`: определить `is_new` ДО `register_user`, распарсить `/start ref_<tg_id>` (в одном месте с
   уже добавленным в задаче 11 парсингом `src_<label>` — единая точка разбора deep-link параметра по
   префиксу), вызвать `register_referral` только для новых юзеров.
8. `/referrals` — личная ссылка `https://t.me/{bot_username}?start=ref_{tg_id}` + `referral_stats`.
9. `/refbonus <N>` (admin, паттерн `/minimpulse` через `_set`).
10. Тесты: бонус начисляется только после первой реальной оплаты (Stars/авто-USDT/`admin_grant`), не
    задваивается на продлениях, self-refer блокируется, один реферер на юзера, триал/промокод бонус не
    триггерят.
11. `README.md`: `/referrals` в списке пользовательских команд, `/refbonus <N>` в таблице админ-команд.

**Критерий приёмки:** новый пользователь по `/start ref_<id>` привязывается к рефереру один раз;
повторный переход по чужой ссылке ничего не меняет; бонус начисляется только при первой успешной оплате
приглашённого (Stars, авто-USDT, полу-ручной USDT через `/grant`) и не задваивается на продлениях;
`/referrals` показывает личную ссылку и живую статистику; `make test` зелёный.

**Промпт для Fable 5:** «Реализуй реферальную программу в RSI Range Signals (aiogram 3.x, SQLite WAL,
Python 3.9-совместимо). 1) Создай `migrations/008_referrals.sql` с таблицей `referrals`
(`referrer_tg_id`, `referred_tg_id` UNIQUE, `created_at`, `credited_at` NULLABLE, `bonus_days`) — DDL см.
задачу «Двусторонняя реферальная программа» в `docs/TOP_LEVEL_PLAN.md`. 2) В `app/delivery/payments.py`
добавь `register_referral(referrer_tg_id, referred_tg_id) -> bool`, `credit_referral_bonus
(referred_tg_id) -> int | None`, `referral_stats(tg_id) -> dict`. ВАЖНО: `credit_referral_bonus`
использует `db.transaction()` (threading.Lock, НЕ реентрантный) — вызывай её строго ПОСЛЕ закрытия
любого внешнего `with db.transaction()`, никогда изнутри. Подключи `credit_referral_bonus()` в трёх
местах: `grant_subscription` (после блока транзакции, перед `return True`), `poll_cryptobot` (в цикле,
перед `activated.append`), `admin_grant` (после блока транзакции, перед `return True`). НЕ подключай в
`grant_trial`/`redeem_promo`, если они уже реализованы. 3) В `app/runtime.py` добавь
`referral_bonus_days() -> int` (дефолт 7, ключ settings `referral_bonus_days`). 4) В
`app/delivery/bot.py`: в хэндлере `/start` определи `is_new` ДО `register_user`, распарси
`/start ref_<tg_id>` (`m.text.split(maxsplit=1)`, `parts[1].startswith("ref_")`), вызови
`register_referral` только для новых юзеров (если рядом уже есть парсинг `src_<label>` из задачи про
`acquisition_source` — используй общую точку разбора по префиксу). Добавь `/referrals` (личная ссылка
`https://t.me/{bot_username}?start=ref_{tg_id}` + `referral_stats`) и `/refbonus <N>` (admin, паттерн
`/minimpulse` через `_set`). 5) Добавь тесты в `tests/test_payments.py`: бонус только после реальной
оплаты (Stars), self-refer блокируется, один реферер на юзера, бонус не задваивается на продлении, бонус
начисляется и через `admin_grant`. 6) Обнови `README.md`. 7) Прогони `make test` — все тесты должны быть
зелёными. Не трогай `app/v2/executor.py` и `app/sim/paper.py`.»

---

#### C. Персонализация доставки (код)

**24. `/mydeposit` — автоподстановка размера позиции в личный сигнал.** Cornix и другие лидеры
персонализируют именно исполнение (риск% от депозита), а не сам сигнал — «сигнал общий, риск персональный»
(cornix.io). Калькулятор размера позиции — самостоятельная популярная категория с готовностью платить.

**Файлы:** `migrations/009_user_risk_profile.sql` (новая), `app/delivery/payments.py`
(`set_risk_profile`), `app/delivery/bot.py` (`/mydeposit`, `format_personal_position`, `_broadcast`).

**Шаги:**
1. `ALTER TABLE users ADD COLUMN deposit_usd REAL; ALTER TABLE users ADD COLUMN default_risk_pct REAL;`
2. `set_risk_profile(tg_id: int, deposit_usd: float, risk_pct: float) -> None`.
3. `/mydeposit <депозит> <риск%>` — парсинг чисел как в `_calc`, валидация `deposit>0` и
   `0<risk_pct<=5`.
4. `format_personal_position(sig: Signal, user_row) -> str` — если у `user_row` есть `deposit_usd`/
   `default_risk_pct`, вызвать `calculate_position(deposit, risk_pct, sig.entry, sig.stop,
   cfg.max_leverage)` из `app.brain.risk`, вернуть короткий блок с явной пометкой **«калькулятор, не
   рекомендация»** (юридически важно — публичная безличная рассылка теряет защиту `publisher's
   exclusion`, если начинает выглядеть как персональный совет, см. риски ниже); иначе — `""`.
5. В `_broadcast`, для `kind=='full'`, подгрузить `payments.get_user(uid)` и добавить
   `format_personal_position` к тексту. В канальный пост (`format_channel_signal`) не подставлять — там
   нет депозита конкретного юзера.

**Критерий приёмки:** `/mydeposit 500 1.5` сохраняет профиль; следующий личный сигнал содержит блок
объёма/номинала/плеча; у юзера без профиля текст не меняется. Тест на обе ветки `format_personal_position`.

**Промпт для Fable 5:** «1) Миграция `migrations/009_user_risk_profile.sql`: `ALTER TABLE users ADD
COLUMN deposit_usd REAL; ALTER TABLE users ADD COLUMN default_risk_pct REAL;`. 2) В
`app/delivery/payments.py` добавь `set_risk_profile(tg_id: int, deposit_usd: float, risk_pct: float) ->
None`. 3) В `app/delivery/bot.py` добавь `Command("mydeposit")`: формат `/mydeposit <депозит> <риск%>`,
парсинг чисел как в `_calc`, валидация `deposit>0` и `0<risk_pct<=5`. 4) Добавь чистую функцию
`format_personal_position(sig: Signal, user_row) -> str`, использующую `calculate_position` из
`app.brain.risk` (`calculate_position(deposit, risk_pct, entry, stop, max_leverage=20) -> dict`,
возвращает `position_qty`/`notional_usd`/`implied_leverage`/`warning`/`error`). Если у `user_row` нет
`deposit_usd`/`default_risk_pct` — верни `""`. Явно пометь блок как «калькулятор, не рекомендация». 5) В
`_broadcast(sig, sid)` для получателей `kind=='full'` подгружай `payments.get_user(uid)` и добавляй
`format_personal_position` к тексту. Канальный пост не трогай. Тесты в `tests/test_bot.py`. `make test`
зелёный.»

---

**25. Персонализация по инструментам и личный порог уверенности (`/mysymbols`, `/myminconf`).** Сейчас
личные сигналы шлются всем платным по всему watchlist без выбора. Notification fatigue (100+
нефильтрованных алертов/день → +22% импульсивных решений, +18% дольше держат убыточные позиции) советует
закладывать фильтрацию заранее, до расширения списка символов. Cornix даёт персонализацию именно на
уровне риска/исполнения на каждый источник сигналов — прямой аналог для личной фильтрации по инструменту.

**Файлы:** `migrations/010_user_prefs.sql` (новая), `app/delivery/payments.py` (`set_symbols_filter`,
`set_minconf_override`, новая сигнатура `plan_recipients`), `app/delivery/bot.py` (`/mysymbols`,
`/myminconf`).

**Шаги:**
1. `ALTER TABLE users ADD COLUMN symbols_filter TEXT; ALTER TABLE users ADD COLUMN minconf_override
   REAL;` (`NULL` = без ограничения в обоих случаях).
2. `set_symbols_filter(tg_id, symbols: list[str] | None) -> None`, `set_minconf_override(tg_id, value:
   float | None) -> None`.
3. Изменить сигнатуру `plan_recipients(signal_id, symbol, confidence, free_limit, admin_ids) -> list` —
   для не-админов: если `symbols_filter` задан и `symbol` не входит — пропустить; если
   `minconf_override` задан и `confidence < override` — пропустить. Админы — без фильтра.
4. Обновить вызов в `_broadcast` на новую сигнатуру.
5. `/mysymbols` (инлайн-клавиатура чекбоксов по `runtime.symbols()`), `/myminconf <0..1|off>`.

**Критерий приёмки:** юзер с фильтром не получает сигналы по не выбранным инструментам, но получает по
выбранным; юзер без фильтра — как раньше (обратная совместимость по умолчанию `NULL`).

**Промпт для Fable 5:** «1) `migrations/010_user_prefs.sql`: `ALTER TABLE users ADD COLUMN symbols_filter
TEXT; ALTER TABLE users ADD COLUMN minconf_override REAL;`. 2) В `app/delivery/payments.py` добавь
`set_symbols_filter(tg_id: int, symbols: list[str] | None) -> None` и `set_minconf_override(tg_id: int,
value: float | None) -> None`. 3) Измени сигнатуру `plan_recipients(signal_id, symbol, confidence,
free_limit, admin_ids)` — добавь фильтрацию не-админов по `u['symbols_filter']` (CSV) и
`u['minconf_override']`. Обнови вызов в `_broadcast`. 4) Добавь `/mysymbols` (инлайн-клавиатура чекбоксов
по `runtime.symbols()`) и `/myminconf <0..1|off>`. 5) Обнови существующие тесты на `plan_recipients` под
новую сигнатуру, добавь тесты на оба фильтра. `make test` зелёный.»

---

#### D. Живое табло статистики в канале (код)

**26. Авто-обновляемый закреплённый пост статистики.** Показывать не win rate (самая манипулируемая
метрика ниши — smartoptions.io фиксирует разрыв заявленного и реального accuracy у Binance Killers на 14
п.п., у CoinCodeCap на 27 п.п.), а матожидание в R, размер выборки, разбивку по инструментам, и — когда
включат `semiauto` — когортную статистику confirmed vs rejected как уникальный контент, которого нет ни у
одного конкурента. Прямой аналог Bybit/Binance leaderboard и MyFXBook-виджета — данные «полностью
открытые, не скрытые» (myfxbook.com).

**Файлы:** `app/brain/journal.py` (`stats_by_symbol`), `app/runtime.py` (`last_board_update_at`,
`live_board_message_id` + сеттеры), `app/delivery/bot.py` (`format_channel_live_board`,
`should_refresh_board`, блок в `_maintenance`).

**Шаги:**
1. `journal.stats_by_symbol() -> dict[str, dict]` — по каждому `DISTINCT symbol` посчитать
   `trades`/`expectancy_r`/`total_r` по закрытым сделкам, тот же паттерн, что в `stats()`.
2. `runtime.py`: геттеры/сеттеры `last_board_update_at()`, `live_board_message_id()` поверх `_raw`/
   `set_value`.
3. `format_channel_live_board(st, by_symbol, cohort: dict | None = None) -> str` — trades/open,
   `expectancy_r`, пометка «выборка мала» при `trades<100`, разбивка по символам, и если передан
   `cohort` (только при `runtime.signal_mode()=='semiauto'`) — блок confirmed/rejected из
   `journal.cohort_stats()`; финальная строка «Публикуем 100% исходов — включая убыточные».
4. Чистая функция `should_refresh_board(last_update: str | None, now_iso: str, hours: int = 24) -> bool`.
5. В `_maintenance()`: если канал включён и `should_refresh_board(...)` истинно — собрать `journal.
   stats()` + `journal.stats_by_symbol()` (+ `journal.cohort_stats()` при `semiauto`), отрендерить; если
   `live_board_message_id()` есть — `bot.edit_message_text` в try/except (проглотить «message is not
   modified»/удалённое сообщение), иначе — `bot.send_message` + `bot.pin_chat_message
   (disable_notification=True)`, сохранить `message_id`; в любом случае обновить
   `last_board_update_at`.

**Критерий приёмки:** при первом запуске создаётся и закрепляется пост; повторные запуски в течение 24ч
не трогают его; после 24ч — редактируется тот же `message_id`; при `semiauto` появляется блок когорт.

**Промпт для Fable 5:** «Реализуй авто-обновляемое живое табло статистики в канале RSI_Range. 1) В
`app/brain/journal.py` добавь `stats_by_symbol() -> dict[str, dict]` — по каждому `DISTINCT symbol`
`trades`/`expectancy_r`/`total_r` по закрытым сделкам, по образцу `stats()`. 2) В `app/runtime.py` добавь
`last_board_update_at() -> str | None`, `live_board_message_id() -> int | None` поверх `_raw`/`set_value`.
3) В `app/delivery/bot.py` добавь `format_channel_live_board(st, by_symbol, cohort=None) -> str` (см.
описание в `docs/TOP_LEVEL_PLAN.md`) и чистую `should_refresh_board(last_update, now_iso, hours=24) ->
bool` с юнит-тестом. 4) В `_maintenance()` добавь блок: раз в 24ч (используя `should_refresh_board`)
собирает `journal.stats`, `journal.stats_by_symbol`, и `journal.cohort_stats()` только если
`runtime.signal_mode()=='semiauto'`, рендерит `format_channel_live_board`, редактирует существующее
закреплённое сообщение или создаёт новое и закрепляет (`disable_notification=True`), сохраняет
`message_id`/дату через `runtime.set_value`. Оберни в try/except с `log.warning`. Тесты в
`tests/test_journal.py` (`stats_by_symbol`) и `tests/test_bot.py` (`format_channel_live_board`,
`should_refresh_board`). `make test` зелёный.»

---

#### E. Рост — новые каналы (код + не код)

**27. Английская локаль (i18n).** Раздел роста целится в SMC/ICT-KOL и площадки, которые в research
почти все англоязычные (Wolfx, Binance Killers, Learn2Trade). aiogram имеет готовую i18n-инфраструктуру
(`aiogram.utils.i18n`, пакет `aiogram-i18n` на PyPI, gettext/Babel) — все тексты уже вынесены в
`format_*`-функции, добавить английскую локаль дёшево. Без неё платная реклама/KOL на глобальную
аудиторию бессмысленна.

**Файлы:** `app/delivery/bot.py` (все `format_*`-функции), новый `app/locales/` (`ru.po`/`en.po`),
`requirements.txt` (`aiogram-i18n`).

**Шаги:**
1. Добавить `aiogram-i18n` в зависимости.
2. Обернуть строки в `format_*`-функциях в вызовы `i18n.gettext`/аналог (или, для меньшего объёма правок,
   минимальный вариант — язык выбирается один раз через `/lang ru|en`, хранится в `users.lang`, и
   отдельные `format_*_en`-варианты для самых важных публичных текстов — `/about`, `/backtest`, `/stats`,
   канальные посты — вместо полного перевода всех 40+ строк сразу; полный gettext-перевод — по мере
   расширения).
3. `/lang ru|en` — команда смены языка, дефолт `ru`.
4. Приоритет перевода: `/about`, `/backtest`, `format_channel_signal`, `format_channel_result`,
   `format_stats`/`format_channel_stats` — тексты, которые увидит внешний трафик с платной рекламы/KOL
   раньше остальных.

**Критерий приёмки:** `/lang en` переключает язык; ключевые публичные тексты (см. приоритет выше)
доступны на английском; `ru` остаётся дефолтом и не ломается для существующих пользователей.

**Промпт для Fable 5:** «Добавь минимальную английскую локаль в RSI Range Signals. Не делай полный
gettext-перевод всех текстов сразу — начни с `users.lang` (`ru`/`en`, дефолт `ru`), команды `/lang
ru|en`, и англоязычных вариантов для `format_about`, `format_backtest_summary`, `format_channel_signal`,
`format_channel_result`, `format_stats`/`format_channel_stats` (эти функции — то, что увидит трафик с
платной рекламы/KOL раньше остального). Структура — на твой выбор (параллельные `_en`-функции или
словарь строк с ключом языка), главное не сломать существующие `ru`-тексты и все текущие тесты. Добавь
тесты на `/lang en` переключение и на то, что английские варианты содержат ожидаемые ключевые слова.
`make test` зелёный.»

---

**28. Cross-promotion в Discord.** SMC/ICT-аудитория частично живёт в Discord, не только в Telegram
(«No signals or copy trading — just concepts, discussions», disboard.org/servers/tag/smc); реальные
комьюнити вроде «SMC Trading» (11.6k участников, discord.com/invite/smc-trading) — конкретная цель.

**Шаги:** найти 5–10 активных SMC/ICT Discord-серверов через disboard.org/servers/tag/smc, представиться
как честный (near-zero edge) сигнальный проект — не продавать, а предложить контент/обсуждение;
разместить ссылку на канал/бота там, где сервер это разрешает.

**Критерий приёмки:** минимум 3 Discord-сервера с размещённой ссылкой/упоминанием; отслеживать переходы
через `acquisition_source=discord_x` (задача 11).

---

**29. Статический лендинг для OG-preview.** Отдельная от `/trackrecord` (задача 32, там был явный отказ от
внешней инфраструктуры для конкретно того фичи) задача: голая `t.me/rsirange_bot`-ссылка не даёт
нормального OG-preview и краулинга для платной рекламы/KOL-ссылок. Статическая страница на Cloudflare
Pages/GitHub Pages — это НЕ VPS: нет процесса, нет rescue-скрипта, деплой одним `git push`, бесплатно.

**Файлы:** новая директория (вне основного репозитория бота или в отдельном `docs/site/` с деплоем через
GitHub Pages/Cloudflare Pages) — 1 статический HTML с OG-тегами, ссылкой на бота, кратким честным питчем
(«инструмент + честная статистика, не обещание доходности») и ссылкой на TGStat-архив.

**Шаги:** сверстать одну статическую страницу (можно без сборки — чистый HTML/CSS), задеплоить на
Cloudflare Pages или GitHub Pages (`git push` в отдельный репозиторий/ветку `gh-pages`), прописать OG-теги
(`og:title`, `og:description`, `og:image`) для нормального превью при шаринге ссылки.

**Критерий приёмки:** ссылка на лендинг даёт корректный OG-preview в Telegram/Twitter/Discord при вставке
URL; страница ведёт на `t.me/rsirange_bot`.

---

**30. Атрибуция-дашборд — админ-команда с разбивкой по источникам.** Расширяет базовую схему из задачи
11 (`acquisition_source`) и `referral_stats` из задачи 23 в единый обзор: план запускает 9+ каналов роста
— без сводной команды невозможно понять, какой из них реально работает.

**Файлы:** `app/delivery/payments.py` (`acquisition_breakdown`), `app/delivery/bot.py` (`/growth`).

**Шаги:**
1. `acquisition_breakdown() -> dict` — `SELECT acquisition_source, COUNT(*), SUM(CASE WHEN paid_until IS
   NOT NULL THEN 1 ELSE 0 END) FROM users GROUP BY acquisition_source` плюс отдельно агрегат по
   `referrals` (сколько платящих пришло через реферальные ссылки — они не попадают в
   `acquisition_source`, так как используют `ref_<id>`, а не `src_<label>`).
2. `/growth` (admin) — таблица «источник → приглашено → оплатили → % конверсии», отдельной строкой
   «рефералы» из `referrals`.

**Критерий приёмки:** `/growth` показывает разбивку по всем меткам `src_*` и агрегат по рефералам; юзеры
без метки — отдельной строкой «неизвестно/органика».

**Промпт для Fable 5:** «В `app/delivery/payments.py` добавь `acquisition_breakdown() -> dict` —
группировку `users` по `acquisition_source` с подсчётом приглашённых и оплативших (`paid_until IS NOT
NULL`), плюс отдельный агрегат по таблице `referrals` (общее число рефералов и оплативших через них). В
`app/delivery/bot.py` добавь `/growth` (admin-only) — форматированную таблицу «источник → приглашено →
оплатили → %», с отдельной строкой для рефералов и строкой «неизвестно» для `acquisition_source IS
NULL AND` юзер не пришёл по `ref_`. Тест в `tests/test_payments.py`. `make test` зелёный.»

---

**31. Инфлюенсер-бартер и giveaway** (не код). Два пункта роста без бюджета:
- **Инфлюенсер-бартер**: бесплатный доступ к подписке в обмен на упоминание + персональная реферальная
  ссылка (задача 23). Целевая аудитория — микро-каналы/чаты по SMC/ICT (Silver Bullet, killzones,
  BOS/CHOCH — активная YouTube/TradingView ниша). Модель похожа на TeleClaw (lifetime revenue share).
  Шаги: список 5–10 микро-каналов/блогеров, личное предложение, отслеживание через `/referrals` (задача
  23). Избегать партнёров, которые сами обещают гарантированную прибыль — их аудитория придёт с чужими
  ожиданиями.
- **Giveaway**: либо официальный Boost-giveaway (Channel Settings → Statistics and Boosts → Giveaway,
  timeweb.com, popsters.ru — недорого, ~$15–30 за несколько призов ×1 месяц Premium/Stars, побочный
  эффект — рост уровня буста канала), либо ручной giveaway с продуктовым призом (N месяцев подписки за
  репост/комментарий, выдаётся через `/grant` — **не** через `credit_referral_bonus`-путь, см. оговорку
  в задаче 23 про `/grantfree`). Рекомендация — начать с продуктового формата (нулевая денежная
  стоимость, продуктово честно — не разыгрывать деньги/USDT).

**Критерий приёмки:** минимум 3 бартерных партнёрства заключены со своими реф-ссылками; минимум один
giveaway проведён и приз выдан.

---

**32. Платная реклама в тематических каналах — с чек-листом проверки** (не код, требует бюджета).
Крипто/трейдинг — «перегретая» рекламная ниша: CPM 1200–3000+ руб/1000 показов против 300–1200 для
массовых тем (elama.ru). Обязательный чек-лист перед оплатой (habr.com/ru/articles/762376,
help.telega.in): три отчёта TGStat («Подписчики», «Охваты публикаций», «Цитирование»), проверка
соотношения подписчики/просмотры (если подписчиков в 10 раз больше просмотров — накрутка), красная
метка/SCAM-FAKE = не покупать, обязательно запросить у админа канала свежие скриншоты статистики за
7–14 дней.

**Шаги:** шорт-лист 5–10 каналов в SMC/трейдинг-нише через telega.in/telega.io, прогнать каждый через
чек-лист, при ограниченном бюджете протестировать 1–2 микро-канала (5–20k подписчиков) параллельно
вместо одного крупного дорогого топа. Рекламный текст — прямая отсылка к публичному трек-рекорду
(`/stats`, TGStat-ссылка): «не очередной сигнальный канал с нарисованным винрейтом — каждая сделка,
включая убыточные, публикуется автоматически». Каждая ссылка — со своей меткой `acquisition_source`
(задача 11) для расчёта цены за платящего подписчика.

**Критерий приёмки:** минимум 1 закупка с задокументированным чек-листом проверки площадки до оплаты;
посчитана цена за подписчика и за платящего подписчика (через `/growth`, задача 30).

---

#### F. Когортный отчёт как контент (не код, условно)

**33. Когортный отчёт confirmed vs rejected как публичный контент.** Когда включат `semiauto` и
наберётся выборка, публиковать агрегат `/report` в канал регулярно (например ежемесячно) как органический
контент: «мы не только даём сигналы — мы измеряем, помогает ли человеческое подтверждение». Ни у одного
конкурента нет такой рефлексии — бесплатный PR-повод для vc.ru/Хабра.

**Шаги:** публиковать только когда `min(confirmed.trades, rejected.trades) >= 30` (порог уже встроен в
`format_report` как предупреждение) — раньше это неоправданно.

**Критерий приёмки:** условие для активации задокументировано, публикация начинается только при выполнении
порога.

---

### Крупные ставки (стратегические)

**34. Еженедельный публичный экспорт журнала (CSV + sha256) в канал, плюс `/trackrecord` по запросу —
флагманская мера доверия.** Объединяет две находки research: (1) модель CoinCodeCap Signals/Rocket Wallet
Signals — публичный лог ВСЕХ сигналов без cherry-picking; (2) дешёвая крипто-версия commit-reveal
доказательства без блокчейна и без VPS — sha256-хэш данных делает незаметную посмертную правку истории
криптографически обнаружимой. Файл и его хэш живут прямо в Telegram-канале — без внешней инфраструктуры,
соответствует принципу «без VPS». **Делать ПОСЛЕ или вместе с задачей 10 (авто-бэкап SQLite)** — иначе
первому экспорту нечего будет защищать при потере базы.

Чек-сумма считается по тем же строкам, что публикует CSV (кандидаты и отклонённые — сознательно исключены,
это отдельный когортный эксперимент `/report`, не публичный трек-рекорд): это одна и та же логика для
обоих механизмов вместо двух разных сериализаций с разными хэшами, как было в черновиках.

**Файлы:** `migrations/011_ledger_hashes.sql` (новая), `app/brain/journal.py`
(`export_public_trades`), `app/delivery/bot.py` (`build_trackrecord_csv`, `trackrecord_checksum`,
`should_post_weekly`, хэндлер `Command("trackrecord")`, блок в `_maintenance`), `app/runtime.py`
(`trackrecord_last_week`), `tests/test_journal.py`, `tests/test_bot.py`.

**Шаги:**
1. Миграция: `CREATE TABLE IF NOT EXISTS ledger_hashes (id INTEGER PRIMARY KEY AUTOINCREMENT, row_count
   INTEGER NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL);` — локальный архив хэшей (на
   случай, если канал позже удалят/заблокируют — хэши остаются в уже забэкапленной БД).
2. `journal.export_public_trades() -> list`: `SELECT id, symbol, side, timeframe, entry, stop, tp1, tp2,
   tp3, status, result_r, created_at, closed_at FROM signals WHERE status NOT IN ('candidate',
   'rejected') ORDER BY created_at` — включает и открытые, и закрытые сигналы.
3. `build_trackrecord_csv(rows) -> bytes` (через `csv.writer` поверх `io.StringIO`, `.encode("utf-8")`, с
   заголовком колонок); `trackrecord_checksum(csv_bytes: bytes) -> str`
   (`hashlib.sha256(csv_bytes).hexdigest()`) — хэш считается по готовым CSV-байтам, это и есть артефакт,
   который публикуется и скачивается, единый источник правды для обеих механик (еженедельный пост и
   `/trackrecord`).
4. `should_post_weekly(last_week_key: str | None, current_week_key: str) -> bool` — сравнение по ISO-
   неделе (`datetime.isocalendar()`).
5. `runtime.trackrecord_last_week()` геттер.
6. `Command("trackrecord")`: собрать CSV, отправить документом через `bot.send_document(m.chat.id,
   BufferedInputFile(csv_bytes, filename="rsirange_trackrecord.csv"), caption=f"📄 Полный публичный
   журнал ({len(rows)} сигналов). SHA-256: {checksum[:16]}…")`.
7. В `_maintenance()`: раз в неделю (`should_post_weekly`) — тот же CSV+хэш в канал документом, плюс
   INSERT в `ledger_hashes` (row_count, sha256, created_at) для локального архива; обновить
   `trackrecord_last_week`.
8. Тесты: `export_public_trades` исключает `candidate`/`rejected`; CSV парсится обратно и содержит все
   колонки; одинаковые данные → одинаковый хэш, разные → разный; `should_post_weekly` на границах недели.

**Критерий приёмки:** `/trackrecord` отдаёт CSV с корректным содержимым и хэшем в подписи; раз в неделю
тот же документ уходит в канал без дублей внутри одной недели; `export_public_trades` не включает
`candidate`/`rejected`; `make test` зелёный.

**Промпт для Fable 5:** «В `app/brain/journal.py` добавь `export_public_trades() -> list` — SELECT всех
колонок сигналов (id, symbol, side, timeframe, entry, stop, tp1, tp2, tp3, status, result_r, created_at,
closed_at) кроме статусов `candidate` и `rejected`, `ORDER BY created_at`. В `app/delivery/bot.py` добавь
три чистые функции: `build_trackrecord_csv(rows) -> bytes` (CSV через модуль `csv` и `io.StringIO`,
utf-8), `trackrecord_checksum(csv_bytes: bytes) -> str` (`hashlib.sha256(...).hexdigest()` от готовых
CSV-байтов — не пересчитывай хэш отдельной сериализацией, используй именно опубликованный CSV),
`should_post_weekly(last_week_key: str | None, current_week_key: str) -> bool` (сравнение ISO-недель). В
`app/runtime.py` добавь `trackrecord_last_week()` по образцу остальных геттеров. Создай
`migrations/011_ledger_hashes.sql`: `CREATE TABLE IF NOT EXISTS ledger_hashes (id INTEGER PRIMARY KEY
AUTOINCREMENT, row_count INTEGER NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL);`. Зарегистрируй
`Command("trackrecord")`: строит CSV из `journal.export_public_trades()`, шлёт документом через
`bot.send_document` с `aiogram.types.BufferedInputFile(csv_bytes, filename="rsirange_trackrecord.csv")` и
подписью с числом сделок и первыми 16 символами sha256. Добавь в `_maintenance()` блок, который раз в
календарную неделю (используя `should_post_weekly`) публикует тот же документ в канал, дополнительно
пишет строку в `ledger_hashes`, обновляет `trackrecord_last_week`, обёрнутый в try/except с
`log.warning`. Добавь тесты в `tests/test_journal.py` и `tests/test_bot.py` на все новые функции. `make
test` зелёный. Примечание: эта задача даёт максимум пользы, только если уже сделан авто-бэкап SQLite
(`scripts/backup_db.py`) — свериться, что он реализован, иначе предупредить пользователя.»

---

**35. TradingView webhook через Cloudflare Tunnel — админ-очередь кандидатов.** Разблокировка
`app/v2/tradingview_webhook.py` без нарушения принципа «без VPS»: `cloudflared tunnel --url
http://localhost:PORT` даёт стабильный публичный HTTPS-эндпоинт через исходящее соединение с Mac к edge
Cloudflare — без открытых портов, без статического IP, бесплатно (tareq.co/2025/11/local-webhook-
cloudflare-tunnel, twilio.com/en-us/blog/expose-localhost-to-internet-with-tunnel). Строго как источник
кандидатов для `semiauto`-очереди (админ подтверждает/отклоняет через `/confirm`/`/reject`), НЕ как
триггер авто-исполнения — не нарушает гейт на `app/v2/executor.py`. Формат payload — стандартный
TradingView Pine-alert JSON (`{{ticker}}`, `{{strategy.order.action}}`, `{{close}}`), который уже
используют 3Commas/TradersPost/WunderTrading (webhookrelay.com/blog/trading-view).

**Файлы:** `app/v2/tradingview_webhook.py` (реализовать `create_app`), `app/brain/journal.py`
(переиспользовать `record_candidate`), `deploy/cloudflared/`, `deploy/com.rsirange.webhook.plist`,
`deploy/com.rsirange.tunnel.plist`, `.env.example` (`TRADINGVIEW_WEBHOOK_SECRET`).

**Шаги:**
1. `create_app()` — Flask-приложение, `POST /webhook/<secret>`, сверка с `TRADINGVIEW_WEBHOOK_SECRET` из
   `get_settings()`, при несовпадении — 403 без записи в БД.
2. Парсинг JSON (`ticker`, `action` buy/sell, `close`, опционально `sl`/`tp`), нормализация символа под
   формат ccxt (`BTC/USDT:USDT`), маппинг `action` → `Side`.
3. Построить `Signal` из полей вебхука (`entry=close`, `stop`/`take_profits` из payload или дефолтный
   ATR-множитель) с `indicators_snapshot={"source": "tradingview_webhook", "raw": payload}` и
   `timeframe="webhook"` — явная метка происхождения.
4. ВСЕГДА `journal.record_candidate(signal)` — независимо от текущего `SIGNAL_MODE`, внешние алерты
   ВСЕГДА проходят ручное подтверждение админа, даже если основной конвейер в `auto`; не вызывать ничего
   из `app/v2/executor.py`.
5. Дедуп по `(ticker, action, close)` в окне 60 сек — не более одного кандидата на комбинацию.
6. После записи — алерт админам (переиспользовать `format_candidate_alert`, отправка через
   `requests`/`urllib` к Bot API `sendMessage` напрямую — `create_app` не имеет доступа к aiogram-
   инстансу, не импортировать `app.delivery.bot`, чтобы не тянуть aiogram в Flask-процесс).
7. `deploy/cloudflared/config.yml` — именованный туннель на `localhost:<PORT из .env, дефолт 8088>`,
   `deploy/com.rsirange.tunnel.plist` + `deploy/com.rsirange.webhook.plist` — launchd-агенты (KeepAlive)
   для `cloudflared tunnel run <name>` и `python3 -m app.v2.tradingview_webhook` (`make run-webhook` уже
   существует).
8. `README.md`: раздел «TradingView webhook (опционально)».

**Критерий приёмки:** валидный секрет+payload создаёт кандидата (`status='candidate'`) и алерт админам;
неверный секрет — 403, ничего не пишется; повторный POST в окне дедупа не создаёт дубль; внешний вебхук
работает независимо от `SIGNAL_MODE`. Тесты в `tests/test_tradingview_webhook.py` через Flask test client,
без реального туннеля.

**Промпт для Fable 5:** «Реализуй `app/v2/tradingview_webhook.py` как admin-only приёмник внешних алертов
TradingView, встраивающийся в существующую `semiauto`-очередь кандидатов (`app/brain/journal.py:
record_candidate`). 1) В `app/config.py` добавь `tradingview_webhook_secret: str | None`. 2) `create_app()`
— Flask, `POST /webhook/<secret>`, сверка секрета, при несовпадении — 403 без записи в БД. 3) Парсинг
JSON (`ticker`, `action` buy/sell, `close`, опционально `sl`/`tp`), нормализация символа под ccxt-формат
проекта. 4) Построй `Signal` из полей вебхука с `indicators_snapshot={'source': 'tradingview_webhook',
'raw': payload}` и `timeframe='webhook'`. 5) ВСЕГДА вызывай `record_candidate(signal)`, независимо от
`runtime.signal_mode()`; не вызывай ничего из `app/v2/executor.py`. 6) Дедуп по `(ticker, action, close)`
в окне 60 секунд. 7) После записи — отправь алерт админам через прямой HTTP-вызов Bot API `sendMessage`
(`requests`/`urllib`, без импорта `app.delivery.bot`), переиспользуя текст из `format_candidate_alert`.
8) Добавь `deploy/cloudflared/config.yml`, `deploy/com.rsirange.tunnel.plist`,
`deploy/com.rsirange.webhook.plist` (launchd, KeepAlive) по образцу `deploy/com.rsirange.bot.plist`. 9)
Обнови `README.md`. 10) Напиши `tests/test_tradingview_webhook.py`: валидный секрет+payload создаёт
кандидата; неверный секрет — 403; повторный запрос в окне дедупа не создаёт второй кандидат. `make test`
зелёный, `mypy app` без новых ошибок.»

---

**36. Платные KOL-партнёрства — делать последней.** Ставки крипто-KOL в 2025–2026: микро-инфлюенсеры
(5–50k вовлечённой аудитории) — $200–2000 за интеграцию либо $3000–8000/мес; средний сегмент (50–200k) —
$10 000–25 000/мес (mediax.agency, tokenminds.co). Самая дорогая и рискованная строка бюджета — делать
последней и только после того, как готовы дешёвые бесплатные активы, повышающие конверсию: публичный
трек-рекорд с историей (задачи 13, 34), работающая реферальная программа (задача 23) для отслеживания
привода, живое табло статистики (задача 26) — усиливает питч KOL, у которого «есть на что посмотреть», а
не голое обещание.

**Шаги:** после накопления публичного трек-рекорда за несколько месяцев подготовить питч-документ (не
лендинг, а ссылка на честную статистику + пример постов-результатов ✅/🛑), связаться с 2–3 микро-KOL в
SMC/ICT-нише, договориться о разовой интеграции (не абонемент) для первого теста, отслеживать через
персональный `/start src_kolname` (задача 11).

**Критерий приёмки:** решение о запуске принимается только после того, как готовы задачи 13, 23, 26, 34;
первая закупка — тестовая (одна интеграция, не абонемент), с измеримым результатом через `/growth`
(задача 30).

## Риски и чего не делать

Контекст: у бота честно **нет** доказанного механического края (walk-forward бэктест на BTC/ETH/SOL —
около нуля после издержек), зато есть то, чего почти ни у кого в нише нет — автопубликация 100% исходов
без возможности тихо подчистить проигрыш. Это единственный настоящий актив доверия проекта. Индустрия сама
подтверждает цену вопроса: SmartOptions.io поймал WolfX Signals на завышении винрейта (заявляли 98%, факт
86.44%), Binance Killers — заявляли ~92%, факт 77.78%; CFTC оштрафовал SchoolofTrade.com на $5 млн именно
за нечестный трек-рекорд сигнального сервиса (courthousenews.com, law360.com); ФСБ в апреле 2026 задержала
админов трёх RU-каналов сигналов («Волк с Мосбиржи» и др.) за манипулирование рынком через сигналы
(forbes.ru, themoscowtimes.com). Усиливать честность — это не только этика, это конкретная защита от
юридической и репутационной претензии, за которую уже наказывали похожие сервисы.

**Комплаенс и деньги:**
- Проверить актуальные ToS Crypto Pay API перед включением авто-USDT (задача 6) — токен переносит
  AML/KYC-периметр на CryptoBot как VASP-посредника, стоит один раз прочитать условия на предмет
  обязательств мерчанта.
- Не переходить на карточный эквайринг (Stripe/PayPal) — оба официально относят крипто-активность и
  money-transmission к запрещённым/high-risk категориям, типичный паттерн — закрытие мерчант-аккаунта
  задним числом с удержанием средств до 180 дней (luqra.com, durangomerchantservices.com). Текущий выбор
  Stars+USDT — осознанный риск-менеджмент, не менять.
- При текущем полу-ручном USDT-выводе — не смешивать личные и «сервисные» крипто-потоки на одной
  банковской карте: массовый вывод USDT сериями мелких переводов от разных `tg_id` в РФ/СНГ
  документированно триггерит блокировку счёта по 115-ФЗ даже на суммах 50–60 тыс. руб. (dtf.ru,
  vasilev-law.ru).
- `/calc`/`/mydeposit` (задача 24) — держать строго как «калькулятор поверх уже опубликованного
  безличного сигнала», никогда как персональную рекомендацию: в США безличная рассылка защищена
  «publisher's exclusion» (Lowe v. SEC, 472 U.S. 181; CFTC Rule 4.14(a)(9), natlawreview.com) —
  персонализация под конкретного юзера эту защиту размывает. Проактивно в широковещательные посты не
  подставлять, только по явному запросу.
- Не продавать `semiauto`/подтверждённые админом сигналы как premium-ценность подписки без отдельного
  юридического ревью — ручное подтверждение конкретных сигналов конкретным людям ближе к дискреционному
  совету, чем безличная публикация.
- Гейт на `app/v2/executor.py` — организационное решение, не техническое. Не закладывать заранее в схему
  БД поля под будущий executor (зашифрованный `exchange_api_key`, `permission_scope` и т.п.), хотя это
  технически дёшево подготовить — готовая схема создаёт имплицитное ожидание «скоро включим» и снижает
  трение перед решением, которое обязано остаться трудным. Авто-исполнение по чужим ключам без
  доказанного края — прямой путь к лицензии CASP как «portfolio management» по MiCA (skadden.com), к
  риску регистрации как CTA/RIA в США (kelman.law), и по духу — к тому же паттерну, за который в апреле
  2026 ФСБ задержала админов российских каналов сигналов.

**Продукт и маркетинг:**
- Не заявлять винрейт/точность как продающий аргумент (никаких «92% accuracy») — рынок и ревьюеры
  (SmartOptions.io, nftevening.com) уже умеют вскрывать разрыв; честный «near-zero edge» — редкое и
  сильное УТП именно потому, что этого не делает никто из конкурентов.
- Не вводить lifetime-тариф — бессрочное обязательство при непроверенном edge и полу-ручном процессе
  одного администратора — плохой компромисс.
- Не вводить лидерборды/бейджи/streak-механики, привязанные к прибыльности или частоте подтверждения
  сигналов — прямой аналог прецедента Robinhood ($7.5M штраф Massachusetts Securities Division, 2021, за
  геймификацию, поощряющую частоту действий).
- **Не вводить NFT-гейтинг доступа, собственный токен проекта или эирдропы** — самый типичный
  growth-хак в крипто-нише и прямой признак финансовой пирамиды по критериям ЦБ РФ
  (cbr.ru/inside/warning-list), той же категории риска, что и уже запрещённые выше пункты.
- Не тестировать разные цены для разных юзеров ОДНОВРЕМЕННО без объяснения (см. задачу 22) — 76%
  пользователей негативно реагируют, узнав, что платили больше других за то же самое (getmonetizely.com);
  скрытая ценовая дискриминация прямо противоречит позиции «мы ничего не скрываем». Тестировать цену
  можно только последовательно (сменить глобально, сравнить окна конверсии).
- Не гнаться за официальным Telegram verified-бейджем (голубая звезда) — требует уже существующей
  публичной известности и упоминаний в прессе, недостижимо на этой стадии (telegram.org/verify).
- Не добавлять whale/on-chain/sentiment-данные или произвольный «AI-score» как новый источник сигналов —
  при честном near-zero результате бэктеста это риск размыть единственную протестированную логику ложной
  точностью.
- Не вводить performance-based money-back guarantee («вернём деньги, если сигналы не сработали») —
  противоречит дисклеймеру «не финсовет»; если гарантия нужна — только за неиспользованный период
  подписки, без привязки к прибыльности.
- Не использовать в маркетинге слова «премиум», «эксклюзивный», «гарантия», «инвестиционная
  рекомендация», «советник» (cbr.ru/inside/warning-list, coinspot.io, vklader.com).

## Источники

**Верификация трек-рекорда и разрыв заявленного/фактического винрейта:** nftevening.com; smartoptions.io
(WolfX Signals 98%→86.44%, Binance Killers ~92%→77.78%, разрыв у CoinCodeCap ~27 п.п.);
signals.coincodecap.com; myfxbook.com/help/knowledge-base/verification (эталон верификации у форекс-
провайдеров); tgstat.ru, telemetr.me, dtf.ru/ruslan-titov/3816217 (независимая архивация Telegram-
каналов).

**Регуляторные и юридические прецеденты:** courthousenews.com, law360.com (CFTC vs SchoolofTrade.com, $5M
за нечестный трек-рекорд); forbes.ru, themoscowtimes.com (ФСБ, «Волк с Мосбиржи», апрель 2026); sec.gov
(SEC vs Morocoin/Berge/Cirkor, декабрь 2025, $14 млн); cbr.ru/inside/warning-list (критерии ЦБ РФ для
финансовых пирамид); natlawreview.com / Winstead PC (publisher's exclusion, Lowe v. SEC 472 U.S. 181);
kelman.law (риск регистрации как CTA/RIA); skadden.com (MiCA, авто-исполнение как CASP portfolio
management); fca.org.uk (стандарт крипто-маркетинга UK FCA); cointelegraph.com со ссылкой на Bracewell
LLP (недостаточность голого «not financial advice»); crosstrade.io/legal/trading-disclaimer (образец
CFTC Rule 4.41 дисклеймера); dtf.ru, vasilev-law.ru (115-ФЗ и блокировки счетов при обналичивании крипты
в РФ/СНГ).

**Монетизация, триал, retention/dunning:** firstpagesage.com, 1capture.io, userpilot.com (бенчмарки
freemium vs триал-конверсии); aotrading.io (7-дневный триал → промо-месяц); bitdegree.org (Learn2Trade
30-дневный триал, 82% retention); elenaverna.com, baremetrics.com, butterpayments.com, retentioncheck.com
(dunning, win-back, годовые тарифы снижают отток); blog.invitemember.com (скидка 15–20% за годовую
подписку); getmonetizely.com (HBR — 76% негативной реакции на скрытую ценовую дискриминацию); mudrex.com,
margex.com, nftplazas.com (ценовой ландшафт ниши); trustpilot.com (жалобы на Learn2Trade/Wolfx на
задержку выдачи доступа).

**Реферальные и партнёрские программы:** coincodecap.com, bitdegree.org (Ogvio, Onward BTC, Learn2Trade —
реф-бонус только за реальную оплату); affiliates.bybit.com, whaleportal.com (Bybit Affiliate Program, до
50% revenue share); telegram.org/tour/affiliate-programs, blog.telegram.org (нативная Telegram Affiliate
Program для Stars, декабрь 2024); mediax.agency, tokenminds.co (ставки крипто-KOL 2025–2026).

**Технические источники:** cornix.io, help.cornix.io (формат сигналов, стандарт исполнения);
core.telegram.org/bots/payments-stars (комиссия Stars ~38% эффективная на мобильных);
core.telegram.org/api/subscriptions (ограничение цикла Stars-подписки в 30 дней); telegram.org/verify
(требования verified-бейджа); tareq.co, twilio.com/en-us/blog/expose-localhost-to-internet-with-tunnel
(Cloudflare Tunnel без VPS); webhookrelay.com/blog/trading-view (стандартный формат TradingView-алертов);
aiogram-i18n (PyPI, i18n-инфраструктура для aiogram 3.x).

**Рост и продвижение:** disboard.org/servers/tag/smc, discord.com/invite/smc-trading (SMC/ICT Discord-
сообщества); dtf.ru/pro-smm/4762864 (эффект взаимного пиара, +100–300 подписчиков); habr.com/ru/
articles/934572 (тематические папки-подборки каналов); habr.com/ru/articles/762376, help.telega.in,
telega.in (чек-лист проверки площадки перед покупкой рекламы); elama.ru (CPM-бенчмарки крипто/трейдинг-
рекламы); timeweb.com, popsters.ru (механика Boost-giveaway в Telegram).
