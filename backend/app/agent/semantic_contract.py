"""Semantic contract: one model call describes goals, not permission to bypass tools."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class ProposalDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = ""
    benefits: list[str] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class SemanticTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=64)
    domain: str = "training"
    intent: str = ""  # Backward-compatible annotation; dispatch derives it from capability metadata.
    operation: str
    scope: Literal["today", "meal", "current_workout", "long_term", "message", "none"]
    action_type: str = "update"
    task_order: int = 1
    user_goal: str = ""
    confidence: float = Field(default=0.8, ge=0, le=1)
    dependencies: list[str] = Field(default_factory=list)
    slots: dict = Field(default_factory=dict)
    missing_slots: list[str] = Field(default_factory=list)
    clarification_needed: bool = False
    context_reference: dict = Field(default_factory=dict)
    proposal_details: ProposalDetails | None = None
    execution_mode: Literal["execute", "propose", "noop"] = "execute"


class SemanticPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dialogue_act: Literal["request", "question", "correction", "cancel", "clarify", "acknowledge"] = "request"
    execution_mode: Literal["execute", "propose", "noop"] = "execute"
    clarification_question: str = ""
    tasks: list[SemanticTask] = Field(default_factory=list, max_length=16)
