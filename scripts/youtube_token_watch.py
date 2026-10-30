"""Сторож токена YouTube API: приложение в тестовом режиме, refresh-токен живёт 7 дней.

Раз в сутки (launchd com.rsirange.yt-token) пробует обновить access-токен. Если обновился —
пишет дату в data/secrets/youtube_token.meta.json. Если токен старше 5 дней или обновление
упало (invalid_grant) — шлёт админам в Telegram напоминание: владельцу нужно один раз нажать
«Разрешить» (ведущая сессия запускает `scripts/youtube_api.py auth`).

Отдельный процесс и отдельное окружение .venv-yt: библиотеки Google в прод-окружение бота не
ставим, а прод-бот от состояния токена не зависит.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SECRETS = Path("/Users/mac/bot ctypto/data/secrets")
TOKEN = SECRETS / "youtube_token.json"
META = SECRETS / "youtube_token.meta.json"
ENV = Path("/Users/mac/bot ctypto/.env")
WARN_AFTER = timedelta(days=5)          # refresh-токен тестового режима живёт 7 дней
SCOPES = ["https://www.googleapis.com/auth/youtube"]


def _env(key: str) -> str:
    for line in ENV.read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _tg(text: str) -> None:
    token, ids = _env("TELEGRAM_BOT_TOKEN"), _env("TELEGRAM_ADMIN_IDS")
    if not token or not ids:
        print("нет TELEGRAM_BOT_TOKEN/ADMIN_IDS — напоминание не отправлено")
        return
    for uid in [x.strip() for x in ids.split(",") if x.strip()]:
        data = urllib.parse.urlencode({"chat_id": uid, "text": text}).encode()
        try:
            urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=15).read()
        except Exception as e:                          # noqa: BLE001 — сторож не падает на сети
            print("telegram:", e)


def _meta() -> dict:
    try:
        return json.loads(META.read_text())
    except (OSError, ValueError):
        return {}


def main() -> int:
    if not TOKEN.exists():
        _tg("🎬 YouTube: токена нет — ролики в отложку ставить нечем. Нужно «Разрешить» "
            "(scripts/youtube_api.py auth).")
        return 1
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    meta = _meta()
    granted = meta.get("granted_at")
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json())
        os.chmod(TOKEN, 0o600)
        ok = True
        err = ""
    except Exception as e:                              # noqa: BLE001 — любое падение = нужен re-auth
        ok, err = False, f"{type(e).__name__}: {str(e)[:120]}"
    now = datetime.now(timezone.utc)
    if ok and not granted:
        granted = now.isoformat()
    if ok:
        META.write_text(json.dumps({"granted_at": granted, "checked_at": now.isoformat()}))
        age = now - datetime.fromisoformat(granted)
        if age >= WARN_AFTER:
            left = max(0, 7 - age.days)
            _tg(f"🎬 YouTube: доступу {age.days} дн., через ~{left} дн. он истечёт. Когда будет "
                "минута — напиши в чат «ютуб», и я открою окно «Разрешить».")
        print(f"ok, токену {age.days} дн.")
        return 0
    META.write_text(json.dumps({"granted_at": None, "checked_at": now.isoformat(), "error": err}))
    _tg(f"🎬 YouTube: доступ истёк ({err}). Отложка роликов встанет. Напиши в чат «ютуб» — "
        "открою окно «Разрешить».")
    print("refresh failed:", err)
    return 1


if __name__ == "__main__":
    sys.exit(main())
