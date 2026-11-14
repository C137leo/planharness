"""Execution effects are not inferred from a provider's `success` string."""
from enum import StrEnum
from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskState(StrEnum):
    PLANNED = "planned"
    VALIDATED = "validated"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    READY = "ready"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class VerifiedOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: TaskState
    operation_id: str
    remote_id: str | None = None
    verified: bool = False
    data: dict = Field(default_factory=dict)
    recovery: str = ""

    @model_validator(mode="after")
    def require_evidence(self):
        if self.state == TaskState.SUCCEEDED and not self.verified:
            raise ValueError("success_requires_verified_postcondition")
        if self.state == TaskState.UNKNOWN and self.verified:
            raise ValueError("unknown_cannot_be_verified_success")
        return self
