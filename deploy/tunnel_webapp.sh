#!/bin/zsh
# Туннель Mini App с мака (бесплатный quick-tunnel Cloudflare, без сервера).
# URL меняется при каждом старте туннеля — скрипт сам вписывает свежий адрес в
# runtime.webapp_url: инлайн-кнопка дашборда строится на каждый тап и всегда актуальна.
#
# СТОРОЖ (инцидент 31.08-02.09: cloudflared завис в вечном ретрай-лупе на ~2 суток,
# «wait $CF» не отпускал, KeepAlive был бессилен — Mini App лежал молча):
# после получения URL каждые 60с проверяем /healthz СКВОЗЬ публичный адрес;
# 5 фейлов подряд -> kill cloudflared -> exit 1 -> launchd перезапускает связку,
# новый туннель получает новый URL и сам вписывает его в webapp_url.
cd "/Users/mac/bot ctypto" || exit 1
LOG=logs/tunnel.log
# адрес quick-tunnel — всегда слова через дефис (foo-bar-baz.trycloudflare.com). Без
# требования дефиса шаблон ловил и https://api.trycloudflare.com из текста ОШИБКИ
# cloudflared («failed to request quick Tunnel: Post "https://api.trycloudflare.com/…"») —
# 29.10 03:41 он ушёл в webapp_url, кнопка «Дашборд» вела на API Cloudflare (аудит 30.10)
URL_RE='https://[a-z0-9]+(-[a-z0-9]+)+\.trycloudflare\.com'
# ротация: ретрай-спам надувал лог на мегабайты — начинаем такт с чистого листа,
# предыдущий хвост держим в tunnel.log.1 для разбора
[[ -f "$LOG" ]] && mv "$LOG" "$LOG.1"
: > "$LOG"
./deploy/bin/cloudflared tunnel --url http://127.0.0.1:8091 --no-autoupdate >> "$LOG" 2>&1 &
CF=$!
trap 'kill "$CF" 2>/dev/null' EXIT
URL=""
for i in {1..60}; do
  URL=$(grep -oE "$URL_RE" "$LOG" | grep -v '^https://api\.' | head -1)
  [[ -n "$URL" ]] && break
  sleep 1
done
if [[ -z "$URL" ]]; then
  # webapp_url НЕ трогаем (лучше старый живой, чем пустой) — рестарт за launchd
  echo "$(date -u +%FT%TZ) URL туннеля не получен за 60с — рестарт" >> "$LOG"
  exit 1
fi
.venv/bin/python -c "from app import runtime; runtime.set_value('webapp_url', '$URL/')" \
  && echo "$(date -u +%FT%TZ) webapp_url -> $URL/" >> "$LOG"

FAILS=0
while kill -0 "$CF" 2>/dev/null; do
  sleep 60
  # -f: HTTP >= 400 = провал. Без него отключённый туннель (edge отвечает 530) считался
  # живым — сторож не срабатывал (инцидент 31.08–02.09, аудит 05.10)
  if curl -fs -o /dev/null -m 10 "$URL/healthz"; then
    FAILS=0
  else
    FAILS=$((FAILS + 1))
    echo "$(date -u +%FT%TZ) сторож: $URL/healthz не отвечает ($FAILS/5)" >> "$LOG"
    if [[ $FAILS -ge 5 ]]; then
      echo "$(date -u +%FT%TZ) сторож: туннель мёртв — перезапуск" >> "$LOG"
      exit 1                              # trap убьёт cloudflared, launchd поднимет заново
    fi
  fi
done
echo "$(date -u +%FT%TZ) cloudflared завершился сам — рестарт за launchd" >> "$LOG"
exit 1
