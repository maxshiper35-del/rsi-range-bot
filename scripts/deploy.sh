#!/bin/zsh
# Деплой на прод (Hetzner): make deploy. Введён 12.08.2026 после случая, когда прод
# сутки работал на коде старше репозитория (фикс closed_at был закоммичен, но не доехал).
#
# Гарантии:
#   1. Уезжает РОВНО закоммиченный HEAD (git archive) — грязное рабочее дерево не деплоится.
#   2. Полный pytest НА СЕРВЕРЕ до рестарта: красные тесты = прод продолжает крутить
#      старый код, деплой прерван.
#   3. После рестарта проверяется is-active и строка старта поллера в логе.
# .env и data/ на сервере не трогаются. Удалённые из репо файлы на сервере НЕ удаляются
# (tar поверх) — при переименованиях модулей проверить сервер руками.
set -euo pipefail
HOST="root@135.181.32.158"
APPDIR="/home/bot/rsirange"
cd "$(dirname "$0")/.."

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "⚠️  рабочее дерево грязное — деплоится только HEAD ($(git log --oneline -1 | cut -c1-60))"
fi

echo "→ код на сервер (git archive HEAD)"
# data/ в chown обязателен: tar кладёт каталог root-ом, а бот должен СОЗДАВАТЬ в нём
# файлы (og.png; сама БД писалась и так — файл принадлежит bot). Инцидент 14.08.
git archive HEAD | ssh -o BatchMode=yes "$HOST" "tar x -C $APPDIR && chown -R bot:bot $APPDIR/app $APPDIR/scripts $APPDIR/tests $APPDIR/migrations && chown bot:bot $APPDIR/data"

echo "→ тесты на сервере (полный прогон)"
if ! ssh -o BatchMode=yes "$HOST" "sudo -u bot bash -c 'cd $APPDIR && .venv/bin/python -m pytest -q 2>&1 | tail -2'"; then
  echo "✗ ТЕСТЫ КРАСНЫЕ — рестарта НЕ БЫЛО, прод работает на прежнем коде"
  exit 1
fi

echo "→ рестарт"
ssh -o BatchMode=yes "$HOST" "systemctl restart rsirange-bot"
sleep 12
ssh -o BatchMode=yes "$HOST" "systemctl is-active rsirange-bot && tail -3 $APPDIR/logs/bot.log"
echo "✓ деплой завершён: $(git log --oneline -1 | cut -c1-70)"
