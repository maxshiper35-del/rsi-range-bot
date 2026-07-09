"""signals_repo.py — доступ к таблицам signals и (append-only) signal_events.

Единственное место с SQL по этим таблицам. UPDATE/DELETE в signal_events
запрещены на уровне репозитория — это реализация неизменяемого журнала.

Статус: заглушка каркаса. См. PLAN_FABLE5.md → «3. journal.py».
"""
from __future__ import annotations

# TODO(Fable 5): insert_signal(signal), get_open(), set_status(id, status, result_r),
#                append_event(signal_id, event, price, r_at_event), aggregates() для /stats.
