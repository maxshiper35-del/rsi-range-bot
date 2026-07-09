"""Единый источник времени — только UTC ISO-8601. Никакого локального/наивного времени."""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    """Строка ISO-8601 в UTC — формат для всех колонок *_at и сообщений."""
    return datetime.now(timezone.utc).isoformat()
