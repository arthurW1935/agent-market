"""Attempt loop: discover -> assign -> dispatch -> await deliverable ->
verify -> settle/reroute. Runs as one asyncio task per funded task.

All rules per DESIGN.md §5-6: live re-rank each attempt, dispatch retries,
30s deliverable clock from the 202, graduated timeout reputation, atomic
settle, refund on exhaustion.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Optional

import httpx

import config
import events
import stores
import verifier
import wallet
from models import (
    AgentCard,
    CriterionResult,
    Deliverable,
    EventType,
    Task,
    TaskStatus,
    Verdict,
)

# task_id -> future resolved with the deliverable content when the callback lands
_pending: dict[str, asyncio.Future] = {}


def accept_deliverable(task_id: str, agent_id: str, content: str) -> None:
    """Called synchronously from the callback route AFTER token validation.
    Invalidates the token (late/duplicate submissions now 409) and wakes
    the pipeline. No awaits — atomic under the single loop."""
    stores.active_tokens[task_id] = None
    stores.deliverables[task_id] = Deliverable(
        task_id=task_id, agent_id=agent_id, content=content
    )
    future = _pending.get(task_id)
    if future is not None and not future.done():
        future.set_result(content)


def _rank_candidates(task: Task) -> list[AgentCard]:
    """Live registry, capability-filtered, minus agents that already failed
    this task; rep/price desc, ties -> lower price, then registration order."""
    order = {agent_id: i for i, agent_id in enumerate(stores.registry)}
    candidates = [
        agent for agent in stores.registry.values()
        if agent.id not in task.attempts
        and all(skill in agent.skills for skill in task.required_skills)
    ]
    return sorted(
        candidates,
        key=lambda a: (-(a.rep_score / a.price), a.price, order[a.id]),
    )


def _apply_rep(agent: AgentCard, delta: float) -> None:
    agent.rep_score = round(
        min(config.REP_CAP, max(config.REP_FLOOR, agent.rep_score + delta)), 2
    )


def _timeout_penalty(agent: AgentCard) -> None:
    """Graduated rule: first timeout/dispatch-failure ever is forgiven."""
    stores.agent_timeouts[agent.id] = stores.agent_timeouts.get(agent.id, 0) + 1
    if stores.agent_timeouts[agent.id] >= 2:
        _apply_rep(agent, config.REP_FAIL_DELTA)


async def _dispatch(task: Task, agent: AgentCard, token: str) -> bool:
    """POST {endpoint}/work with retries (conn/5xx x5, 4xx immediate).
    Returns True on a 202."""
    payload = {
        "task_id": task.id,
        "spec": task.spec,
        "rubric": [r.model_dump() for r in task.rubric],
        "callback_url": f"{config.PLATFORM_BASE_URL}/tasks/{task.id}/deliverable",
        "agent_token": token,
    }
    async with httpx.AsyncClient(timeout=config.DISPATCH_HTTP_TIMEOUT) as client:
        for attempt in range(config.DISPATCH_MAX_TRIES):
            try:
                response = await client.post(f"{agent.endpoint}/work", json=payload)
            except httpx.HTTPError:
                response = None
            if response is not None:
                if response.status_code == 202:
                    return True
                if response.status_code < 500:
                    return False  # agent is alive and declining — don't retry
            if attempt < config.DISPATCH_MAX_TRIES - 1:
                await asyncio.sleep(config.DISPATCH_BACKOFFS[attempt])
    return False


async def run(task_id: str) -> None:
    task = stores.tasks[task_id]
    previous_agent: Optional[str] = None
    failure_reason: Optional[str] = None

    while len(task.attempts) < config.MAX_ATTEMPTS:
        candidates = _rank_candidates(task)
        if not candidates:
            break  # pool exhausted — cap is a cap, not a quota

        if previous_agent is None:
            events.emit(task_id, EventType.candidates_found,
                        {"count": len(candidates), "agent_ids": [a.id for a in candidates]})
        else:
            events.emit(task_id, EventType.rerouted, {
                "from_agent": previous_agent,
                "to_agent": candidates[0].id,
                "attempt": len(task.attempts) + 1,
                "reason": failure_reason,
            })

        agent = candidates[0]
        task.assigned_agent = agent.id
        task.attempts.append(agent.id)
        task.status = TaskStatus.ASSIGNED
        events.emit(task_id, EventType.assigned,
                    {"agent_id": agent.id, "agent_name": agent.name})

        token = uuid.uuid4().hex
        stores.active_tokens[task_id] = token

        if not await _dispatch(task, agent, token):
            stores.active_tokens[task_id] = None
            _timeout_penalty(agent)
            previous_agent, failure_reason = agent.id, "dispatch_failed"
            continue

        task.status = TaskStatus.EXECUTING
        events.emit(task_id, EventType.dispatched,
                    {"agent_id": agent.id, "agent_name": agent.name})

        # 30s deliverable clock starts at the 202.
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        _pending[task_id] = future
        try:
            content = await asyncio.wait_for(future, timeout=config.DELIVERABLE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            stores.active_tokens[task_id] = None
            _timeout_penalty(agent)
            previous_agent, failure_reason = agent.id, "timeout"
            continue
        finally:
            _pending.pop(task_id, None)

        task.status = TaskStatus.VERIFYING
        events.emit(task_id, EventType.deliverable_submitted, {"agent_id": agent.id})

        try:
            result = await verifier.verify(task.spec, task.rubric, content)
            verifier_error = False
        except verifier.VerifierUnavailable:
            result = None
            verifier_error = True

        if verifier_error:
            verdict = Verdict(
                task_id=task_id, agent_id=agent.id,
                criteria=[CriterionResult(
                    name=r.criterion, passed=False,
                    evidence="verifier unavailable", note="verifier_error",
                ) for r in task.rubric],
                overall=False,
                fix_list=["Verifier was unavailable; the attempt was not judged."],
            )
            events.emit(task_id, EventType.verdict, verdict.model_dump())
            # Attempt burns; NO rep change, no fails/jobs increment.
            previous_agent, failure_reason = agent.id, "verdict_failed"
            continue

        verdict = Verdict(task_id=task_id, agent_id=agent.id,
                          criteria=result.criteria, overall=result.overall,
                          fix_list=result.fix_list)

        if verdict.overall:
            # Atomic settle: no awaits between these mutations.
            gross, take, net = wallet.release_escrow(task_id, agent.id)
            _apply_rep(agent, config.REP_PASS_DELTA)
            agent.passes += 1
            agent.jobs += 1
            task.status = TaskStatus.SETTLED
            events.emit(task_id, EventType.verdict, verdict.model_dump())
            events.emit(task_id, EventType.settled,
                        {"agent_id": agent.id, "gross": gross, "take": take, "net": net})
            return

        events.emit(task_id, EventType.verdict, verdict.model_dump())
        _apply_rep(agent, config.REP_FAIL_DELTA)
        agent.fails += 1
        agent.jobs += 1
        previous_agent, failure_reason = agent.id, "verdict_failed"

    # Attempts exhausted or candidate pool empty: refund, nobody gets anything.
    amount = wallet.refund_escrow(task_id)
    task.status = TaskStatus.FAILED_UNFULFILLED
    task.assigned_agent = None
    events.emit(task_id, EventType.refunded, {"amount": amount})
