"""YouTube Data API для канала: авторизация, черновики, отложенная публикация.

Файл ролика загружается через браузер (YouTube Studio), а название, описание и время
публикации ставятся здесь, через API. Причина: ролики, загруженные через videos.insert из
непроверенного API-проекта, YouTube оставляет приватными до прохождения аудита, а
videos.update для уже загруженных так не ограничен.

Ключи — вне git, в data/secrets/ (client_secret.json и youtube_token.json):

  .venv-yt/bin/python scripts/youtube_api.py auth            # один раз, владелец жмёт «Разрешить»
  .venv-yt/bin/python scripts/youtube_api.py drafts          # черновики и приватные ролики канала
  .venv-yt/bin/python scripts/youtube_api.py schedule <video_id> <caption.txt> <ГГГГ-ММ-ДДTЧЧ:ММ>
      [--tz Europe/Berlin]

Отдельное окружение .venv-yt: библиотеки Google не ставим в окружение прод-бота.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SECRETS = Path("/Users/mac/bot ctypto/data/secrets")
CLIENT = SECRETS / "client_secret.json"
TOKEN = SECRETS / "youtube_token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube"]
CATEGORY_EDUCATION = "27"


def _service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not TOKEN.exists():
        sys.exit("нет data/secrets/youtube_token.json — сначала `auth`")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def cmd_auth(port: int) -> None:
    """Печатает ссылку согласия и ждёт редиректа на localhost:<port>. Токен — в data/secrets."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES)
    creds = flow.run_local_server(port=port, open_browser=False, prompt="consent",
                                  authorization_prompt_message="ССЫЛКА: {url}",
                                  success_message="Готово, вкладку можно закрыть.")
    SECRETS.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(creds.to_json())
    TOKEN.chmod(0o600)
    print("токен сохранён")


def cmd_drafts() -> None:
    yt = _service()
    ch = yt.channels().list(part="contentDetails,snippet", mine=True).execute()["items"][0]
    print("канал:", ch["snippet"]["title"])
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    items = yt.playlistItems().list(part="contentDetails", playlistId=uploads,
                                    maxResults=50).execute()["items"]
    ids = [i["contentDetails"]["videoId"] for i in items]
    vids = yt.videos().list(part="snippet,status", id=",".join(ids)).execute()["items"]
    for v in vids:
        st = v["status"]
        if st["privacyStatus"] != "public":
            print(v["id"], st["privacyStatus"], st.get("publishAt", "-"), st.get("uploadStatus"),
                  "|", v["snippet"]["title"][:70])


def parse_caption(path: Path) -> tuple[str, str]:
    """Подпись kit.Caption из .txt рядом с роликом -> (заголовок, описание с хэштегами)."""
    t = path.read_text()
    title = re.search(r"^Заголовок: (.*)$", t, re.M).group(1).strip()
    desc = t.split("Описание:", 1)[1].split("Хэштеги:", 1)[0].strip()
    tags = re.search(r"^Хэштеги: (.*)$", t, re.M).group(1).strip()
    return title, f"{desc}\n\n{tags}"


def cmd_schedule(video_id: str, caption: Path, when: str, tz: str) -> None:
    title, desc = parse_caption(caption)
    if len(title) > 100:
        sys.exit(f"заголовок длиннее 100 символов: {len(title)}")
    publish_at = datetime.fromisoformat(when).replace(tzinfo=ZoneInfo(tz))
    if publish_at <= datetime.now(publish_at.tzinfo):
        sys.exit("время публикации уже прошло")
    yt = _service()
    body = {
        "id": video_id,
        "snippet": {"title": title, "description": desc, "categoryId": CATEGORY_EDUCATION,
                    "defaultLanguage": "ru"},
        "status": {"privacyStatus": "private", "publishAt": publish_at.isoformat(),
                   "selfDeclaredMadeForKids": False},
    }
    res = yt.videos().update(part="snippet,status", body=body).execute()
    st = res["status"]
    print(json.dumps({"id": res["id"], "title": res["snippet"]["title"],
                      "privacy": st["privacyStatus"], "publishAt": st.get("publishAt")},
                     ensure_ascii=False))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("auth")
    a.add_argument("--port", type=int, default=8765)
    sub.add_parser("drafts")
    s = sub.add_parser("schedule")
    s.add_argument("video_id")
    s.add_argument("caption", type=Path)
    s.add_argument("when", help="локальное время публикации, ГГГГ-ММ-ДДTЧЧ:ММ")
    s.add_argument("--tz", default="Europe/Berlin")
    args = ap.parse_args()
    if args.cmd == "auth":
        cmd_auth(args.port)
    elif args.cmd == "drafts":
        cmd_drafts()
    else:
        cmd_schedule(args.video_id, args.caption, args.when, args.tz)


if __name__ == "__main__":
    main()
