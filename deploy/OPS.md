# Операции прода (ЛОКАЛЬНО на Mac, с 30.08.2026)

Hetzner (135.181.32.158) не оплачен и остановлен — прод переехал на этот Mac.
Прошлый runbook (серверный) — в git-истории этого файла; вернётся сервер —
восстановить оттуда и вернуть `make deploy`.

Требования к маку: не выключать, не давать засыпать (System Settings → Displays →
Advanced → «Prevent automatic sleeping…» или `caffeinate`), интернет постоянный.

## Процессы (launchd, gui-домен пользователя)

| агент | что делает | лог |
|---|---|---|
| `com.rsirange.bot` | сам бот (поллер+доставка+executor) | `logs/bot.log` |
| `com.rsirange.webapp` | Flask Mini App на 127.0.0.1:8091 (waitress) | `logs/webapp.log` |
| `com.rsirange.tunnel` | cloudflared quick-tunnel + сторож URL | `logs/tunnel.log` |
| `com.rsirange.backup` | суточный бэкап БД 02:00 + оффсайт + ротация логов | `logs/backup.log` |

Плисты: `~/Library/LaunchAgents/com.rsirange.*.plist`
(копии в репо: `deploy/launchagents/` — восстановление после переезда/ребута).

```bash
# статус / логи / рестарт
launchctl list | grep rsirange
tail -30 logs/bot.log
launchctl kickstart -k gui/$(id -u)/com.rsirange.bot      # или make restart-local
```

⚠️ После ребута мака агенты gui-домена поднимаются ТОЛЬКО после логина пользователя —
после сбоя питания/обновления macOS нужно войти в систему.

## Деплой правок кода

Локально: тесты зелёные → `make restart-local` (бот+webapp). `make deploy` (Hetzner)
сознательно отключён — двойной инстанс = конфликт getUpdates и дубли постов
(инцидент 27-29.08).

## Сторожа и бэкапы

- **Dead-man's switch**: пульс на healthchecks.io каждые 60с из maintenance-цикла
  (только при свежем тике поллера). `HEALTHCHECK_URL` в локальном `.env`.
  ⚠️ Telegram-интеграцию на healthchecks.io включает владелец (Integrations → Telegram).
- **Сторож Mini App** (в боте, с 02.09): каждые 60с проверяет публичный URL /healthz;
  5 фейлов подряд → DM-алерт админу + kickstart туннеля. Закрывает инцидент
  31.08-02.09 (cloudflared 2 суток висел в ретрай-лупе молча).
- **Сторож в самом туннеле** (`deploy/tunnel_webapp.sh`): проверяет свой URL насквозь,
  при 5 фейлах убивает cloudflared → launchd поднимает заново с новым URL,
  скрипт сам вписывает его в `runtime.webapp_url`.
- **Бэкап БД**: `com.rsirange.backup` ежесуточно 02:00 → `data/backups/` (ротация 14)
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

```bash
launchctl bootout gui/$(id -u)/com.rsirange.bot
cp data/backups/signals-<свежий>.db data/signals.db
rm -f data/signals.db-wal data/signals.db-shm
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rsirange.bot.plist
```

Проверка: `/health` в боте (тик поллера, бэкап, executor), дашборд открывается,
`sqlite3 data/signals.db "PRAGMA integrity_check"`.
