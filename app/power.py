"""Питание Mac (прод крутится на ноутбуке): статус батареи для алерта бота и дашборда.

09.10.2026 зарядку отключили вечером, сон выключен — Mac молча работал на батарее до
04:00, сел, и бот лежал 3.5 дня. Источник — `pmset -g batt` (только macOS).
"""
from __future__ import annotations

import re
from datetime import datetime


def pmset_batt() -> dict | None:
    """Статус питания Mac (sync, через to_thread); не macOS / ошибка — None."""
    import subprocess
    import sys
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["/usr/bin/pmset", "-g", "batt"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_pmset_batt(out)


def parse_pmset_batt(text: str) -> dict | None:
    """Вывод `pmset -g batt` -> {"on_battery", "pct", "remaining"}; None — не разобрать
    (не Mac / нет батареи). 09.10.2026 зарядку отключили вечером, сон выключен — Mac
    молча работал на батарее до 04:00, сел, и бот лежал 3.5 дня."""
    src = re.search(r"Now drawing from '([^']+)'", text or "")
    if not src:
        return None
    pct = re.search(r"(\d+)%", text)
    rem = re.search(r"(\d+:\d+) remaining", text)
    return {"on_battery": "Battery" in src.group(1),
            "pct": int(pct.group(1)) if pct else None,
            "remaining": rem.group(1) if rem else None}


def should_alert_battery(status: dict | None, last_alert_at: str | None,
                         now_iso: str) -> bool:
    """На батарее — алерт раз в час, при заряде <= 20% — раз в 15 минут."""
    if not status or not status["on_battery"]:
        return False
    if last_alert_at is None:
        return True
    every_min = 15 if (status["pct"] is not None and status["pct"] <= 20) else 60
    age = (datetime.fromisoformat(now_iso) - datetime.fromisoformat(last_alert_at))
    return age.total_seconds() >= every_min * 60


def battery_alert_text(status: dict) -> str:
    pct = f"{status['pct']}%" if status["pct"] is not None else "?"
    rem = (f", осталось ~{status['remaining']}"
           if status["remaining"] and status["remaining"] != "0:00" else "")
    return (f"🔋 Mac работает от батареи: {pct}{rem}. Сон отключён — когда батарея сядет, "
            "бот выключится и сделки останутся без сопровождения. Подключите зарядку.")
