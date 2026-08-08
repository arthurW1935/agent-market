"""Wire-format objects and enums per CONTRACTS.md §1–2. The contract wins over taste."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TaskStatus(str, Enum):
    CREATED = "CREATED"
    NO_SUPPLY = "NO_SUPPLY"
    RUBRIC_DISCUSSION = "RUBRIC_DISCUSSION"
    CONFIRMED_UNFUNDED = "CONFIRMED_UNFUNDED"
    FUNDED = "FUNDED"
    ASSIGNED = "ASSIGNED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    SETTLED = "SETTLED"
    FAILED_UNFULFILLED = "FAILED_UNFULFILLED"


class EscrowState(str, Enum):
    locked = "locked"
    released = "released"
    refunded = "refunded"


class EventType(str, Enum):
    task_posted = "task_posted"
    no_supply = "no_supply"
    rubric_proposed = "rubric_proposed"
    rubric_revised = "rubric_revised"
    rubric_confirmed = "rubric_confirmed"
    deposit = "deposit"
    escrow_locked = "escrow_locked"
    candidates_found = "candidates_found"
    assigned = "assigned"
    dispatched = "dispatched"
    deliverable_submitted = "deliverable_submitted"
    verdict = "verdict"
    rerouted = "rerouted"
    settled = "settled"
    refunded = "refunded"
    agent_registered = "agent_registered"


class AgentCard(BaseModel):
    id: str
    name: str
    skills: list[str]
    price: int
    endpoint: str
    rep_score: float = 3.0
    jobs: int = 0
    passes: int = 0
    fails: int = 0


class RubricItem(BaseModel):
    criterion: str
    checkable_test: str


class CriterionResult(BaseModel):
    name: str
    passed: bool
    evidence: str
    note: str = ""


class Verdict(BaseModel):
    task_id: str
    agent_id: str
    criteria: list[CriterionResult]
    overall: bool
    fix_list: list[str] = Field(default_factory=list)


class Deliverable(BaseModel):
    task_id: str
    agent_id: str
    content: str
    submitted_at: str = Field(default_factory=now_iso)


class Event(BaseModel):
    task_id: Optional[str]
    type: EventType
    payload: dict[str, Any]
    ts: str = Field(default_factory=now_iso)


class Task(BaseModel):
    id: str
    spec: str
    bounty: int
    status: TaskStatus
    rubric: list[RubricItem] = Field(default_factory=list)
    assigned_agent: Optional[str] = None
    attempts: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now_iso)
    # Internal fields, excluded from the contract Task shape:
    rubric_thread: list[dict[str, str]] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    auto_confirm: bool = False
    auto_fund: bool = False

    def to_wire(self) -> dict[str, Any]:
        """Exact Task shape from CONTRACTS §4."""
        return {
            "id": self.id,
            "spec": self.spec,
            "bounty": self.bounty,
            "status": self.status.value,
            "rubric": [r.model_dump() for r in self.rubric],
            "assigned_agent": self.assigned_agent,
            "attempts": list(self.attempts),
            "created_at": self.created_at,
        }


# ---- Request bodies (CONTRACTS §3–4) ----

class RegisterAgentRequest(BaseModel):
    name: str
    skills: list[str]
    price: int
    endpoint: str


class DepositRequest(BaseModel):
    owner: str
    amount: int


class CreateTaskRequest(BaseModel):
    spec: str
    bounty: int
    auto_confirm: bool = False
    auto_fund: bool = False


class RubricMessageRequest(BaseModel):
    message: str


class ConfirmRequest(BaseModel):
    rubric: Optional[list[RubricItem]] = None


class DeliverableCallbackRequest(BaseModel):
    agent_id: str
    agent_token: str
    content: str


# ---- LLM structured-output schemas ----

class CompileResult(BaseModel):
    required_skills: list[Literal["research", "writing", "extraction"]]
    rubric: list[RubricItem]


class ReviseResult(BaseModel):
    rubric: list[RubricItem]
    changes: str


class FalsifiabilityResult(BaseModel):
    admissible: bool
    detail: str
    suggestion: list[RubricItem]


class VerdictLLMResult(BaseModel):
    criteria: list[CriterionResult]
    overall: bool
    fix_list: list[str]
