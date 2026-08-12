#!/bin/zsh
# Вторая (внесерверная) копия бэкапов журнала: мак ежедневно подтягивает свежайший
# ночной бэкап с прод-сервера. Смысл: все бэкапы rsirange-backup.timer лежат НА ТОМ ЖЕ
# VPS — гибель сервера без этой копии означала бы потерю публичного трек-рекорда.
# Best-effort: мак спит/выключен — молча пропускаем, завтра догоним (ротация 14 копий
# держит окно в две недели). Запускается launchd-агентом com.rsirange.backup-pull.
set -u
HOST="root@135.181.32.158"
DEST="/Users/mac/bot ctypto/data/backups-server"
mkdir -p "$DEST"

LATEST=$(ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST" \
  'ls -t /home/bot/rsirange/data/backups/signals-*.db 2>/dev/null | head -1') || exit 0
[[ -z "$LATEST" ]] && exit 0
NAME=$(basename "$LATEST")
[[ -f "$DEST/$NAME" ]] && exit 0                       # уже забирали — идемпотентно

scp -o BatchMode=yes -q "$HOST:$LATEST" "$DEST/$NAME.tmp" || { rm -f "$DEST/$NAME.tmp"; exit 0; }
# бэкап без WAL — integrity_check перед тем, как считать копию состоявшейся
if ! sqlite3 "$DEST/$NAME.tmp" "PRAGMA integrity_check;" | grep -q "^ok$"; then
  rm -f "$DEST/$NAME.tmp"*; exit 0
fi
mv "$DEST/$NAME.tmp" "$DEST/$NAME"
rm -f "$DEST/$NAME.tmp-shm" "$DEST/$NAME.tmp-wal"      # мусор от integrity_check по WAL-базе
ls -t "$DEST"/signals-*.db 2>/dev/null | tail -n +15 | xargs rm -f 2>/dev/null   # ротация 14
echo "$(date -u +%FT%TZ) pulled $NAME" >> "$DEST/pull.log"
