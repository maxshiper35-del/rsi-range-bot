#!/bin/zsh
# Туннель Mini App с мака (бесплатный quick-tunnel Cloudflare, без сервера).
# URL меняется при каждом старте туннеля — скрипт сам вписывает свежий адрес в
# runtime.webapp_url: инлайн-кнопка дашборда строится на каждый тап и всегда актуальна.
# KeepAlive у launchd перезапускает связку после обрыва/ребута.
cd "/Users/mac/bot ctypto" || exit 1
LOG=logs/tunnel.log
: > "$LOG"
./deploy/bin/cloudflared tunnel --url http://127.0.0.1:8091 --no-autoupdate >> "$LOG" 2>&1 &
CF=$!
URL=""
for i in {1..60}; do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1)
  [[ -n "$URL" ]] && break
  sleep 1
done
if [[ -n "$URL" ]]; then
  .venv/bin/python -c "from app import runtime; runtime.set_value('webapp_url', '$URL/')" \
    && echo "$(date -u +%FT%TZ) webapp_url -> $URL/" >> "$LOG"
else
  echo "$(date -u +%FT%TZ) URL туннеля не получен за 60с" >> "$LOG"
fi
wait $CF
