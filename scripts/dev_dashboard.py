"""Dev-просмотр дашборда вне Telegram — ТОЛЬКО локально, на КОПИИ базы.

  .venv/bin/python -m scripts.dev_dashboard              # как админ, порт 8092
  .venv/bin/python -m scripts.dev_dashboard --as 12345   # как юзер 12345
  .venv/bin/python -m scripts.dev_dashboard --public     # без подписи (публика)

Прод не затронут: подмена авторизации — monkeypatch ВНУТРИ этого процесса, база —
копия data/dev_dash.db (кнопки админки пишут в копию), вместо telegram-web-app.js —
заглушка Telegram.WebApp. Сетевые запросы цен идут на публичный Bybit, как в проде.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

_DEV_DB = Path("data/dev_dash.db")

_TG_SHIM = """<script>
window.Telegram = {WebApp: {platform: "dev", initData: "dev", ready() {}, expand() {},
  setHeaderColor() {}, setBackgroundColor() {},
  showConfirm(q, cb) { cb(window.confirm(q)); }, showAlert(t) { window.alert(t); },
  openTelegramLink(u) { console.log("openTelegramLink", u); },
  openLink(u) { console.log("openLink", u); },
  HapticFeedback: {impactOccurred() {}, notificationOccurred() {}, selectionChanged() {}}}};
</script>"""


def _copy_db(src: Path) -> None:
    """Консистентная копия живой базы (sqlite backup API — вместе с WAL)."""
    _DEV_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(src) as s, sqlite3.connect(_DEV_DB) as d:
        s.backup(d)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as", dest="uid", type=int, default=None, help="tg_id юзера")
    ap.add_argument("--public", action="store_true")
    ap.add_argument("--port", type=int, default=8092)
    ap.add_argument("--reuse", action="store_true", help="не перекопировать базу")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(".env")
    src = Path(os.environ.get("DB_PATH", "data/signals.db"))
    if not args.reuse or not _DEV_DB.exists():
        _copy_db(src)
    os.environ["DB_PATH"] = str(_DEV_DB)               # ДО импорта app.* (get_settings кеширует)

    from app import webapp
    from app.config import get_settings
    uid = args.uid or (sorted(get_settings().admin_ids) or [0])[0]
    if not args.public:
        webapp._auth_user = lambda max_age_s=0: {"id": uid}      # noqa: E731 — только этот процесс

    index_view = webapp.app.view_functions["index"]

    def dev_index():
        resp = index_view()
        html = resp.get_data(as_text=True)
        html = html.replace('<script src="https://telegram.org/js/telegram-web-app.js"></script>',
                            "" if args.public else _TG_SHIM)
        resp.set_data(html)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    webapp.app.view_functions["index"] = dev_index
    who = "публика" if args.public else f"uid {uid}"
    print(f"dev-дашборд: http://127.0.0.1:{args.port}/  ({who}, база {_DEV_DB})", flush=True)
    from waitress import serve
    serve(webapp.app, host="127.0.0.1", port=args.port, threads=4)


if __name__ == "__main__":
    main()
