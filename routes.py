"""REST surface — exact wire formats and status codes per CONTRACTS §3-4,
plus two additive endpoints from DESIGN.md §3: GET /events (global SSE)
and GET /wallets."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

import events
import service
from models import (
    ConfirmRequest,
    CreateTaskRequest,
    DeliverableCallbackRequest,
    DepositRequest,
    RegisterAgentRequest,
    RubricMessageRequest,
)

router = APIRouter()


def _sse(generator) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# NOTE: every handler is `async def` on purpose — sync handlers run in
# FastAPI's threadpool, which would break the single-event-loop atomicity
# guarantee all state mutations rely on (DESIGN.md §7).

# ---- Agent-facing (CONTRACTS §3) ----

@router.post("/agents", status_code=201)
async def register_agent(req: RegisterAgentRequest):
    return service.register_agent(req).model_dump()


@router.post("/tasks/{task_id}/deliverable", status_code=202)
async def submit_deliverable(task_id: str, req: DeliverableCallbackRequest):
    return service.submit_deliverable(task_id, req)


# ---- Buyer-facing (CONTRACTS §4) ----

@router.post("/wallet/deposit")
async def deposit(req: DepositRequest):
    return service.deposit(req)


@router.post("/tasks")
async def create_task(req: CreateTaskRequest):
    status, body = await service.create_task(req)
    return JSONResponse(status_code=status, content=body)


@router.post("/tasks/{task_id}/rubric/message")
async def rubric_message(task_id: str, req: RubricMessageRequest):
    return await service.rubric_message(task_id, req)


@router.post("/tasks/{task_id}/rubric/confirm")
async def confirm_rubric(task_id: str, req: ConfirmRequest | None = None):
    body = await service.confirm_rubric(task_id, req or ConfirmRequest())
    return JSONResponse(status_code=402, content=body)


@router.post("/tasks/{task_id}/fund")
async def fund_task(task_id: str):
    return service.fund_task(task_id)


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    return service.get_task(task_id)


@router.get("/tasks/{task_id}/deliverable")
async def get_deliverable(task_id: str):
    return service.get_deliverable(task_id)


@router.get("/agents")
async def list_agents():
    return [a.model_dump() for a in service.list_agents()]


# ---- Events (contract per-task stream + additive global stream) ----

@router.get("/events")
async def global_events():
    return _sse(events.stream(None))


@router.get("/events/{task_id}")
async def task_events(task_id: str):
    return _sse(events.stream(task_id))


# ---- Additive: wallet read for the UI ----

@router.get("/wallets")
async def wallets():
    return service.wallets_view()


def install_error_handler(app) -> None:
    @app.exception_handler(service.ApiError)
    async def _api_error_handler(request: Request, error: service.ApiError):
        return JSONResponse(status_code=error.status, content=error.body)
