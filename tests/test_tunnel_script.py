"""deploy/tunnel_webapp.sh: извлечение адреса quick-tunnel из лога cloudflared.

Гоняем РОВНО ту строку извлечения, что в скрипте (сам скрипт запускать нельзя — он
поднимает cloudflared и пишет webapp_url в боевую БД)."""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "tunnel_webapp.sh"

# реальный лог 29.10.2026 03:41 (tunnel.log.1): провал запроса туннеля — в тексте ОШИБКИ
# есть https://api.trycloudflare.com, и старый шаблон вписал его в webapp_url
_FAILED = (
    "2026-10-29T03:41:24Z INF Requesting new quick Tunnel on trycloudflare.com...\n"
    'failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": context '
    "deadline exceeded (Client.Timeout exceeded while awaiting headers)\n"
)
_CREATED = (
    "2026-10-29T03:41:52Z INF |  Your quick Tunnel has been created! Visit it at (it may "
    "take some time to be reachable):  |\n"
    "2026-10-29T03:41:52Z INF |  https://backgrounds-decade-approaches-relate.trycloudflare.com"
    "                            |\n"
)


def _extract(tmp_path, log_text: str) -> str:
    script = SCRIPT.read_text(encoding="utf-8")
    url_re = re.search(r"^URL_RE='([^']+)'$", script, re.M)
    line = re.search(r"^\s*(URL=\$\(grep .*\))\s*$", script, re.M)
    assert url_re and line, "в скрипте нет URL_RE / строки извлечения URL"
    log = tmp_path / "tunnel.log"
    log.write_text(log_text, encoding="utf-8")
    shell = shutil.which("zsh") or shutil.which("sh")
    if shell is None:                                   # pragma: no cover
        pytest.skip("нет POSIX-шелла")
    harness = f"URL_RE='{url_re.group(1)}'\nLOG='{log}'\n{line.group(1)}\nprintf '%s' \"$URL\"\n"
    out = subprocess.run([shell, "-c", harness], capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_error_text_url_is_not_taken_as_tunnel(tmp_path):
    assert _extract(tmp_path, _FAILED) == ""            # ждём дальше / рестарт, webapp_url цел


def test_created_tunnel_url_is_extracted(tmp_path):
    want = "https://backgrounds-decade-approaches-relate.trycloudflare.com"
    assert _extract(tmp_path, _CREATED) == want
    # провал и затем успех в одном логе (ретрай cloudflared) — берём настоящий адрес
    assert _extract(tmp_path, _FAILED + _CREATED) == want
