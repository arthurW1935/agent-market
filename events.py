"""Event log + SSE fan-out.

Two streams (DESIGN.md §3):
  GET /events            — global firehose, the UI's single subscription
  GET /events/{task_id}  — per-task stream per CONTRACTS §4

Both replay full history on connect, then go live. Wire format is one
`data: {Event JSON}` message per event, with `: ping` comments to keep
connections alive.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Optional

import config
import stores
from models import Event, EventType

# Live subscribers: each is (task_id_filter | None, queue). None = global.
_subscribers: list[tuple[Optional[str], asyncio.Queue]] = []


def emit(task_id: Optional[str], type: EventType, payload: dict[str, Any]) -> Event:
    event = Event(task_id=task_id, type=type, payload=payload)
    stores.event_log.append(event)
    for filter_id, queue in _subscribers:
        if filter_id is None or filter_id == task_id:
            queue.put_nowait(event)
    return event


def _format_sse(event: Event) -> str:
    return f"data: {json.dumps(event.model_dump(mode='json'))}\n\n"


async def stream(task_id: Optional[str] = None) -> AsyncIterator[str]:
    """Replay history matching the filter, then stream live events."""
    queue: asyncio.Queue = asyncio.Queue()
    subscription = (task_id, queue)

    # Snapshot history BEFORE subscribing would race with emit(); since we're
    # single-threaded and don't await between these lines, snapshot+subscribe
    # is atomic and no event can be lost or duplicated.
    history = [e for e in stores.event_log if task_id is None or e.task_id == task_id]
    _subscribers.append(subscription)

    try:
        for event in history:
            yield _format_sse(event)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=config.SSE_PING_SECONDS)
                yield _format_sse(event)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
    finally:
        _subscribers.remove(subscription)
