# Операции прода (Hetzner, с 12.08.2026)

Сервер: `root@135.181.32.158` (SSH-ключ `~/.ssh/id_ed25519`, парольный вход выключен).
Бот: пользователь `bot`, код `/home/bot/rsirange`, python `/opt/cpython-3.11.15` + `.venv`.

## Ежедневное

```bash
# статус / логи / рестарт
ssh root@135.181.32.158 systemctl status rsirange-bot
ssh root@135.181.32.158 tail -30 /home/bot/rsirange/logs/bot.log
ssh root@135.181.32.158 systemctl restart rsirange-bot
```

## Деплой правок кода

```bash
make deploy        # git archive HEAD -> сервер -> pytest ТАМ -> рестарт только при зелёных
```

Красные тесты на сервере = рестарта не было, прод крутит прежний код. `.env` и `data/`
деплой не трогает. Удалённые из репо файлы на сервере не удаляются (tar поверх).

## Сторожа и бэкапы

- **Dead-man's switch**: пульс на healthchecks.io каждые 60с из maintenance-цикла,
  только при свежем тике поллера (<2ч). Молчание >15 мин -> алерт оператору.
  URL в `.env` сервера (`HEALTHCHECK_URL`); на маке (dev) не задан = выключен.
- **Watchdog поллера** (внутренний): процесс жив, поллер завис -> прямой HTTP-алерт
  админам в Telegram (двойной контур к dead-man's switch).
- **Бэкап БД**: `rsirange-backup.timer` 04:00 UTC, ротация 14, на сервере
  `data/backups/`. **Вторая копия**: мак ежедневно в 09:30 подтягивает свежайший
  бэкап (`com.rsirange.backup-pull` -> `data/backups-server/`, ротация 14,
  integrity_check перед принятием).

## Крайние случаи

```bash
# некаугнутые краши до инициализации логгера (аналог bot.stderr.log на маке)
ssh root@135.181.32.158 journalctl -u rsirange-bot -n 50 --no-pager

# счётчик рестартов systemd (растёт = крэш-луп)
ssh root@135.181.32.158 systemctl show rsirange-bot -p NRestarts
```

**Откат на мак** (если сервер умер): вернуть plist'ы из
`~/Library/LaunchAgents/disabled-rsirange/` в `~/Library/LaunchAgents/` и
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rsirange.bot.plist`;
БД восстановить из свежайшего `data/backups-server/signals-*.db` (положить в
`data/signals.db`). ДВА ИНСТАНСА ЖИТЬ НЕ МОГУТ — сначала убедиться, что сервер
действительно мёртв.

## Известные грабли

- Маковский `rsync` — это openrsync: GNU-флаги молча ломаются. Для передач — tar по ssh
  (так и работает `deploy.sh` через `git archive`).
- `/usr/bin/python3` на маке ломается лицензией Xcode — всё через `.venv`/`make`.
- Первый `launchctl bootstrap` сразу после `bootout` может упасть с
  «Input/output error» — подождать ~3с и повторить.
