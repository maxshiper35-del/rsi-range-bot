# Операции прода (ЛОКАЛЬНО на Mac, с 30.08.2026)

Hetzner (135.181.32.158) не оплачен и остановлен — прод переехал на этот Mac.
Прошлый runbook (серверный) — в git-истории этого файла; вернётся сервер —
восстановить оттуда и вернуть `make deploy`.

Требования к маку: **всегда от сети** (05.10 ночью шёл от батареи, разрядился, утром
перезагрузился) и **без сна, включая закрытую крышку**: «Prevent automatic sleeping» и
`caffeinate` закрывают только сон по простою, а Clamshell Sleep останавливал прод на 2–11ч
(30.09–04.10, `pmset -g log`). Один раз, в Терминале (нужен пароль):

```bash
sudo pmset -a disablesleep 1
```

Проверка: `pmset -g | grep SleepDisabled` → `1`. Интернет постоянный.

## Процессы (launchd, gui-домен пользователя)

| агент | что делает | лог |
|---|---|---|
| `com.rsirange.bot` | сам бот (поллер+доставка+executor) | `logs/bot.log` |
| `com.rsirange.webapp` | Flask Mini App на 127.0.0.1:8091 (waitress) | `logs/webapp.log` |
| `com.rsirange.tunnel` | cloudflared quick-tunnel + сторож URL | `logs/tunnel.log` |
| `com.rsirange.backup` | суточный бэкап БД 04:00 по местному (02:00 UTC летом, 03:00 UTC зимой) + оффсайт + ротация логов | `logs/backup.log` |

Плисты: `~/Library/LaunchAgents/com.rsirange.*.plist`
(копии в репо: `deploy/launchagents/` — восстановление после переезда/ребута).

```bash
# статус / логи / рестарт
launchctl list | grep rsirange
tail -30 logs/bot.log
launchctl kickstart -k gui/$(id -u)/com.rsirange.bot
launchctl kickstart -k gui/$(id -u)/com.rsirange.webapp
# make НЕ работает: /usr/bin/make — шим Xcode, без принятой лицензии падает (exit 69)
```

⚠️ После ребута мака агенты gui-домена поднимаются ТОЛЬКО после логина пользователя —
после сбоя питания/обновления macOS нужно войти в систему. FileVault включён, автовхода нет:
мак ждёт пароль на экране FileVault. Автоустановку обновлений macOS лучше выключить
(аудит 30.10: `AutomaticallyInstallMacOSUpdates = 1` — ночное обновление положит бота до
утра). Отключение света дома гасит и роутер: 25–26.10 это ~12 ч без сети и 3.2 ч мёртвого
мака — нужен ИБП или резервный интернет.

⚠️ **Окно рестарта**: не перезапускать бота с HH:58 до HH:03 (UTC и местное совпадают по
минутам). В первые ~90 с часа идут скан, рассылка и executor. `poll_once` живёт в потоке,
и рестарт посреди такта может записать сигнал в журнал (executor его исполнит), не
опубликовав его (аудит 30.10, M5).

## Деплой правок кода

Локально: тесты зелёные (`.venv/bin/python -m pytest`) → две команды `launchctl kickstart -k`
выше (бот+webapp); правка плиста webapp — `cp` в ~/Library/LaunchAgents, затем
`launchctl bootout` + `bootstrap`. Бэкап вручную: `.venv/bin/python -m scripts.backup_db`.
`make deploy` (Hetzner)
сознательно отключён — двойной инстанс = конфликт getUpdates и дубли постов
(инцидент 27-29.08).

## Сторожа и бэкапы

- **Dead-man's switch**: пульс на healthchecks.io каждые 60с из maintenance-цикла
  (только при свежем тике поллера). `HEALTHCHECK_URL` в локальном `.env`.
  ⚠️ Telegram-интеграцию на healthchecks.io включает владелец (Integrations → Telegram).
- **Сторож слепоты** (в боте, с 30.10): тик поллера свежий, а баров биржи нет дольше ≈2.3 ч
  (Bybit лежит, бан или гео-блок IP) → DM админам. Пульс healthchecks такое НЕ ловит: он
  смотрит только на тик. `/health` → «Бары биржи получены». Каждый такт пишет в `bot.log`
  строку `такт: символов ок N, сбой M; запросов R, старший ТФ из кеша H, rate-limit L`.
  10006 в первые ~40 с часа — толкучка на бирже, ретраи гасят её (INFO); тревога — «сбой»
  > 0 несколько часов подряд.
- **Сторож Mini App** (в боте, с 02.09): каждые 60с проверяет публичный URL /healthz;
  5 фейлов подряд → DM-алерт админу + kickstart туннеля. Закрывает инцидент
  31.08-02.09 (cloudflared 2 суток висел в ретрай-лупе молча).
- **Сторож в самом туннеле** (`deploy/tunnel_webapp.sh`): проверяет свой URL насквозь,
  при 5 фейлах убивает cloudflared → launchd поднимает заново с новым URL,
  скрипт сам вписывает его в `runtime.webapp_url`.
- **Бэкап БД**: `com.rsirange.backup` ежесуточно 04:00 по местному → `data/backups/` (ротация 14)
  + **оффсайт-копия в iCloud Drive** (`RSIRange-Backups/`, ротация 7) — смерть диска
  не уносит и бэкапы. Integrity_check перед отметкой успеха; бот алертит, если
  бэкап протух.
- **Ротация логов**: в бэкап-скрипте, copy-truncate при >10 МБ (одно поколение .1).

## Mini App (дашборд)

Quick-tunnel Cloudflare — URL ротируется при каждом рестарте туннеля; свежий адрес
всегда в `settings.webapp_url`, кнопка «Дашборд» в боте строит инлайн-URL на каждый
тап (плюс кеш-бастер). PNG-фолбэк, если туннель лежит.

## Секция «Бэктест 5 лет · по месяцам» (квартальная процедура)

Сетка в дашборде — статический `app/backtest_monthly.json`; живые месяцы после
последнего месяца модели дорисовываются из журнала сами. Сам бэктест пересобирать
**раз в квартал, руками, с проверкой** (три инцидента «мерил не то» — см. память
backtest-parity-checklist):
1. `scripts/dump_history.py` — докачать бары 9 эталонных символов в `data/cache5y`.
2. `scripts/dump_signals.py` с ПАРИТЕТНЫМИ флагами (`--ob-single-candle`,
   `--htf-map "1h=4h,4h="`, time-filter ON, window 200, min_conf 0.5) -> `sig_parity`.
   Полная команда — в коммите 101b5a5.
3. `make backtest-monthly` — скрипт сам отказывает, если в by_setup нет всех трёх
   сетапов; сверить n/ΣR старого периода с прошлым JSON (должны совпасть бит-в-бит).
4. Коммит JSON; `/api/backtest` подхватывает по mtime, рестарт не нужен.

## Восстановление из бэкапа

Останавливать ОБА процесса, что держат БД (бот И webapp): с живым webapp `cp` поверх
файла и удаление -wal давали «database disk image is malformed» в Mini App и потерю
записей (проверено на копии, аудит 05.10). Ещё надёжнее — онлайн-восстановление через
SQLite backup API (согласовано с WAL, процессы можно не трогать):

```bash
.venv/bin/python -c "import sqlite3; sqlite3.connect('data/backups/signals-<свежий>.db').backup(sqlite3.connect('data/signals.db'))"
launchctl kickstart -k gui/$(id -u)/com.rsirange.bot
launchctl kickstart -k gui/$(id -u)/com.rsirange.webapp
```

Ручной вариант (если файл БД повреждён и не открывается):

```bash
launchctl bootout gui/$(id -u)/com.rsirange.bot
launchctl bootout gui/$(id -u)/com.rsirange.webapp
cp data/backups/signals-<свежий>.db data/signals.db
rm -f data/signals.db-wal data/signals.db-shm
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rsirange.bot.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rsirange.webapp.plist
```

Проверка: `/health` в боте (тик поллера, бэкап, executor), дашборд открывается,
`sqlite3 data/signals.db "PRAGMA integrity_check"`.

## «Если X — делай Y»

Короткий runbook для владельца и разбор простоев 16–29.10 —
`docs/audits/2026-10-30-reliability.md` (раздел «Runbook»).
