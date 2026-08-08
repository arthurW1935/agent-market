"""Service layer — the single implementation both surfaces call.

REST routes (routes.py) and MCP tools (mcp_server.py) are thin wrappers
around these functions. Errors are raised as ApiError(status, body) and
translated by each surface.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import config
import events
import rubric
import stores
import wallet
from models import (
    AgentCard,
    ConfirmRequest,
    CreateTaskRequest,
    DeliverableCallbackRequest,
    DepositRequest,
    EventType,
    RegisterAgentRequest,
    RubricItem,
    RubricMessageRequest,
    Task,
    TaskStatus,
)


class ApiError(Exception):
    def __init__(self, status: int, body: dict[str, Any]):
        self.status = status
        self.body = body
        super().__init__(str(body))


def _get_task(task_id: str) -> Task:
    task = stores.tasks.get(task_id)
    if task is None:
        raise ApiError(404, {"error": "task_not_found"})
    return task


# ---- Supply side ----

def register_agent(req: RegisterAgentRequest) -> AgentCard:
    unknown = [s for s in req.skills if s not in config.ALLOWED_SKILLS]
    if unknown:
        raise ApiError(400, {"error": "unknown_skills", "unknown": unknown,
                             "allowed": config.ALLOWED_SKILLS})
    card = AgentCard(
        id=stores.new_id("agt"),
        name=req.name,
        skills=req.skills,
        price=req.price,
        endpoint=req.endpoint,
        rep_score=config.REP_START,
    )
    stores.registry[card.id] = card
    events.emit(None, EventType.agent_registered, card.model_dump())
    return card


def list_agents() -> list[AgentCard]:
    return sorted(stores.registry.values(), key=lambda a: -a.rep_score)


# ---- Wallet ----

def deposit(req: DepositRequest) -> dict[str, Any]:
    if req.amount <= 0:
        raise ApiError(400, {"error": "invalid_amount"})
    new_balance = wallet.deposit(req.owner, req.amount)
    events.emit(None, EventType.deposit,
                {"owner": req.owner, "amount": req.amount, "balance": new_balance})
    return {"owner": req.owner, "balance": new_balance}


def wallets_view() -> dict[str, int]:
    return dict(stores.wallets)


# ---- Buyer flow ----

def _supply_exists(required_skills: list[str]) -> bool:
    return any(
        all(skill in agent.skills for skill in required_skills)
        for agent in stores.registry.values()
    )


def _nearest_capabilities(required_skills: list[str]) -> list[str]:
    """Skills the market DOES have, among (or near) the required set."""
    available: set[str] = set()
    for agent in stores.registry.values():
        available.update(agent.skills)
    nearest = [s for s in required_skills if s in available]
    return nearest or sorted(available)


async def create_task(req: CreateTaskRequest) -> tuple[int, dict[str, Any]]:
    """Returns (http_status, body): (201, Task) or (200, NO_SUPPLY body)."""
    try:
        compiled = await rubric.compile(req.spec)
    except rubric.LLMUnavailable:
        raise ApiError(503, {"error": "rubric_unavailable", "detail": "try again"})

    task = Task(
        id=stores.new_id("tsk"),
        spec=req.spec,
        bounty=req.bounty,
        status=TaskStatus.CREATED,
        rubric=compiled.rubric,
        required_skills=list(compiled.required_skills),
        auto_confirm=req.auto_confirm,
        auto_fund=req.auto_fund,
    )
    stores.tasks[task.id] = task
    events.emit(task.id, EventType.task_posted, {"spec": task.spec, "bounty": task.bounty})

    if not _supply_exists(task.required_skills):
        # Persist as unmet demand; the response stays the bare contract body.
        task.status = TaskStatus.NO_SUPPLY
        nearest = _nearest_capabilities(task.required_skills)
        events.emit(task.id, EventType.no_supply,
                    {"required_skills": task.required_skills,
                     "nearest_capabilities": nearest})
        return 200, {"status": "NO_SUPPLY", "nearest_capabilities": nearest}

    task.status = TaskStatus.RUBRIC_DISCUSSION
    events.emit(task.id, EventType.rubric_proposed,
                {"rubric": [r.model_dump() for r in task.rubric]})

    if req.auto_confirm:
        await confirm_rubric(task.id, ConfirmRequest())
        if req.auto_fund:
            fund_task(task.id)

    return 201, task.to_wire()


async def rubric_message(task_id: str, req: RubricMessageRequest) -> dict[str, Any]:
    task = _get_task(task_id)
    if task.status != TaskStatus.RUBRIC_DISCUSSION:
        raise ApiError(409, {"error": "not_in_rubric_discussion", "status": task.status.value})

    rounds = sum(1 for m in task.rubric_thread if m["role"] == "buyer")
    try:
        result = await rubric.revise(task.rubric, task.rubric_thread, req.message)
    except rubric.LLMUnavailable:
        raise ApiError(503, {"error": "rubric_unavailable", "detail": "try again"})

    task.rubric_thread.append({"role": "buyer", "content": req.message})
    task.rubric_thread.append({"role": "platform", "content": result.changes})
    task.rubric = result.rubric

    changes = result.changes
    if rounds + 1 >= config.RUBRIC_DISCUSSION_SOFT_CAP:
        changes += " [Discussion cap reached — please confirm (confirm-with-edits is allowed).]"

    events.emit(task_id, EventType.rubric_revised,
                {"rubric": [r.model_dump() for r in task.rubric], "changes": changes})
    return {"rubric": [r.model_dump() for r in task.rubric], "changes": changes}


async def confirm_rubric(task_id: str, req: ConfirmRequest) -> dict[str, Any]:
    """Freezes the rubric. Returns the 402 body per CONTRACTS §4."""
    task = _get_task(task_id)
    if task.status != TaskStatus.RUBRIC_DISCUSSION:
        raise ApiError(409, {"error": "not_in_rubric_discussion", "status": task.status.value})

    if req.rubric is not None:  # confirm-with-edits: guard falsifiability
        try:
            audit = await rubric.check_falsifiability(req.rubric)
        except rubric.LLMUnavailable:
            raise ApiError(503, {"error": "rubric_unavailable", "detail": "try again"})
        if not audit.admissible:
            raise ApiError(400, {
                "error": "unverifiable_criteria",
                "detail": audit.detail,
                "suggestion": [r.model_dump() for r in audit.suggestion],
            })
        task.rubric = req.rubric

    task.status = TaskStatus.CONFIRMED_UNFUNDED
    events.emit(task_id, EventType.rubric_confirmed, {"amount_due": task.bounty})
    return {"amount_due": task.bounty, "fund_via": f"POST /tasks/{task_id}/fund"}


def fund_task(task_id: str) -> dict[str, Any]:
    task = _get_task(task_id)
    if task.status != TaskStatus.CONFIRMED_UNFUNDED:
        raise ApiError(409, {"error": "not_confirmed", "status": task.status.value})

    buyer_balance = wallet.balance("buyer")
    if buyer_balance < task.bounty:
        raise ApiError(400, {"error": "insufficient_balance",
                             "balance": buyer_balance, "needed": task.bounty})

    wallet.lock_escrow(task_id, "buyer", task.bounty)
    task.status = TaskStatus.FUNDED
    events.emit(task_id, EventType.escrow_locked, {"amount": task.bounty})

    import pipeline  # lazy: avoids import cycle and lets steps 1-5 run standalone
    asyncio.get_running_loop().create_task(pipeline.run(task_id))
    return task.to_wire()


def get_task(task_id: str) -> dict[str, Any]:
    task = _get_task(task_id)
    body = task.to_wire()
    body["events"] = [e.model_dump(mode="json") for e in stores.task_events(task_id)]
    if task.status == TaskStatus.SETTLED:
        body["deliverable"] = stores.deliverables[task_id].content
    return body


def get_deliverable(task_id: str) -> dict[str, Any]:
    task = _get_task(task_id)
    if task.status != TaskStatus.SETTLED:
        raise ApiError(423, {"error": "locked_until_settled", "status": task.status.value})
    return {"content": stores.deliverables[task_id].content}


# ---- Worker callback ----

def submit_deliverable(task_id: str, req: DeliverableCallbackRequest) -> dict[str, Any]:
    task = stores.tasks.get(task_id)
    if task is None:
        raise ApiError(409, {"error": "unknown_task"})
    active = stores.active_tokens.get(task_id)
    if (
        active is None
        or req.agent_token != active
        or req.agent_id != task.assigned_agent
        or task.status != TaskStatus.EXECUTING
    ):
        # Late / duplicate / cross-agent / unknown token — all the same wall.
        raise ApiError(409, {"error": "invalid_submission"})

    import pipeline
    pipeline.accept_deliverable(task_id, req.agent_id, req.content)
    return {"received": True}
