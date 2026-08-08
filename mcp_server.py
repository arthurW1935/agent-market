"""MCP surface — the product's front door for buyer agents (DESIGN.md §2).

Eight buyer tools, each a thin wrapper over the same service layer the REST
routes call. Mounted at /mcp on the same app/port (Streamable HTTP):

    claude mcp add --transport http agent-market http://localhost:8000/mcp
"""

from __future__ import annotations

from typing import Any, Optional

from mcp.server import MCPServer

import service
from models import (
    ConfirmRequest,
    CreateTaskRequest,
    DepositRequest,
    RubricItem,
    RubricMessageRequest,
)

mcp = MCPServer(
    name="agent-market",
    instructions=(
        "A marketplace where you hire worker agents for verified work. "
        "Flow: deposit_funds -> create_task -> discuss_rubric (optional) -> "
        "confirm_rubric -> fund_escrow. The platform then discovers, assigns "
        "and verifies work autonomously; watch progress with get_task_status. "
        "Money moves only when an independent verifier passes the deliverable "
        "against the rubric you confirmed."
    ),
)


def _err(e: service.ApiError) -> dict[str, Any]:
    return {"http_status": e.status, **e.body}


@mcp.tool()
async def deposit_funds(amount: int) -> dict:
    """Deposit coins into your buyer wallet. Returns the new balance."""
    try:
        return service.deposit(DepositRequest(owner="buyer", amount=amount))
    except service.ApiError as e:
        return _err(e)


@mcp.tool()
async def create_task(spec: str, bounty: int) -> dict:
    """Post a task. Returns the task with a proposed rubric (status
    RUBRIC_DISCUSSION), or NO_SUPPLY if no registered agent has the skills."""
    try:
        _, body = await service.create_task(CreateTaskRequest(spec=spec, bounty=bounty))
        return body
    except service.ApiError as e:
        return _err(e)


@mcp.tool()
async def discuss_rubric(task_id: str, message: str) -> dict:
    """One rubric discussion round. Returns the full revised rubric plus a
    summary of what changed and why. Vague asks get translated into
    checkable criteria, not refused."""
    try:
        return await service.rubric_message(task_id, RubricMessageRequest(message=message))
    except service.ApiError as e:
        return _err(e)


@mcp.tool()
async def confirm_rubric(task_id: str, rubric: Optional[list[dict]] = None) -> dict:
    """Freeze the rubric (optionally with your own edits — they are checked
    for verifiability). Returns the amount due; call fund_escrow next."""
    try:
        edits = [RubricItem(**r) for r in rubric] if rubric else None
        return await service.confirm_rubric(task_id, ConfirmRequest(rubric=edits))
    except service.ApiError as e:
        return _err(e)


@mcp.tool()
async def fund_escrow(task_id: str) -> dict:
    """Lock the bounty in escrow. Discovery, assignment, execution and
    verification start automatically — nothing left for you to drive."""
    try:
        return service.fund_task(task_id)
    except service.ApiError as e:
        return _err(e)


@mcp.tool()
async def get_task_status(task_id: str) -> dict:
    """Task status plus the full event log (who was picked and why, verdicts
    with evidence, settlement). Deliverable included only once SETTLED."""
    try:
        return service.get_task(task_id)
    except service.ApiError as e:
        return _err(e)


@mcp.tool()
async def get_deliverable(task_id: str) -> dict:
    """The verified deliverable. Locked (423) until the task is SETTLED —
    money and work swap atomically."""
    try:
        return service.get_deliverable(task_id)
    except service.ApiError as e:
        return _err(e)


@mcp.tool()
async def list_agents() -> list[dict]:
    """Registered worker agents, reputation-ranked."""
    return [a.model_dump() for a in service.list_agents()]
