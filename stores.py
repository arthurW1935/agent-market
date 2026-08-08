"""In-memory state. Single asyncio loop, no locks — atomicity is guaranteed by
never awaiting inside a read-modify-write (see DESIGN.md §7)."""

from __future__ import annotations

import itertools
import uuid
from typing import Optional

from models import AgentCard, Deliverable, Event, Task

# owner -> balance ("buyer", agent ids, "platform")
wallets: dict[str, int] = {}

# task_id -> Task
tasks: dict[str, Task] = {}

# task_id -> {"amount": int, "state": EscrowState}
escrow: dict[str, dict] = {}

# task_id -> Deliverable (escrowed; released only on SETTLED)
deliverables: dict[str, Deliverable] = {}

# agent_id -> AgentCard  (insertion order = registration order, used as ranking tie-break)
registry: dict[str, AgentCard] = {}

# agent_id -> count of dispatch-failures/timeouts (graduated rep rule)
agent_timeouts: dict[str, int] = {}

# task_id -> active agent_token (None when no attempt is live)
active_tokens: dict[str, Optional[str]] = {}

# Global ordered event log; per-task views are filtered from it.
event_log: list[Event] = []

_seq = itertools.count(1)


def next_seq() -> int:
    return next(_seq)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def task_events(task_id: str) -> list[Event]:
    return [e for e in event_log if e.task_id == task_id]
