"""Case-change governance schemas for draft, review and release."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from testops.contracts import CaseBaseline, CaseDefinition

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
CandidateVersion = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^case-v[0-9]+\.[0-9]+\.[0-9]+$"),
]
Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


class GovernanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class ChangeType(StrEnum):
    ADD = "ADD"
    MODIFY = "MODIFY"
    DELETE = "DELETE"


class ChangeRequestStatus(StrEnum):
    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    CANDIDATE = "CANDIDATE"
    PUBLISHED = "PUBLISHED"


class ReviewDecision(StrEnum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"


class ChangeOperation(GovernanceModel):
    change_type: ChangeType
    case_id: UUID | None = None
    case: CaseDefinition | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> ChangeOperation:
        if self.change_type == ChangeType.ADD:
            if self.case is None or self.case_id is not None:
                raise ValueError("ADD requires case and must omit case_id")
        elif self.change_type == ChangeType.MODIFY:
            if self.case is None or self.case_id is None or self.case.case_id != self.case_id:
                raise ValueError("MODIFY requires matching case_id and case")
        elif self.case_id is None or self.case is not None:
            raise ValueError("DELETE requires case_id and must omit case")
        return self


class ChangeRequestCreate(GovernanceModel):
    base_baseline_id: UUID
    candidate_version: CandidateVersion
    title: Annotated[NonEmptyText, StringConstraints(max_length=200)]
    reason: Annotated[NonEmptyText, StringConstraints(max_length=4000)]
    changes: Annotated[tuple[ChangeOperation, ...], Field(min_length=1, max_length=500)]


class ChangeRequestUpdate(GovernanceModel):
    candidate_version: CandidateVersion
    title: Annotated[NonEmptyText, StringConstraints(max_length=200)]
    reason: Annotated[NonEmptyText, StringConstraints(max_length=4000)]
    changes: Annotated[tuple[ChangeOperation, ...], Field(min_length=1, max_length=500)]


class ChangeItemResponse(GovernanceModel):
    id: UUID
    sequence: int
    change_type: ChangeType
    case_id: UUID
    case_code: str
    before: dict[str, object] | None
    after: dict[str, object] | None
    changed_fields: tuple[str, ...]


class ApprovalResponse(GovernanceModel):
    id: UUID
    reviewer_id: UUID
    decision: ReviewDecision
    comment: str | None
    created_at: datetime


class ChangeRequestResponse(GovernanceModel):
    id: UUID
    project_id: UUID
    base_baseline_id: UUID
    candidate_baseline_id: UUID
    candidate_version: str
    candidate_digest: Digest
    title: str
    reason: str
    status: ChangeRequestStatus
    validation_status: str
    validation_run_id: UUID | None
    created_by: UUID
    submitted_at: datetime | None
    reviewed_at: datetime | None
    published_at: datetime | None
    published_baseline_id: UUID | None
    created_at: datetime
    updated_at: datetime
    change_count: int


class ChangeRequestDetailResponse(ChangeRequestResponse):
    candidate_baseline: CaseBaseline
    changes: tuple[ChangeItemResponse, ...]
    approvals: tuple[ApprovalResponse, ...]


class ReviewDecisionRequest(GovernanceModel):
    decision: ReviewDecision
    comment: Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)] | None = None

    @model_validator(mode="after")
    def require_rework_comment(self) -> ReviewDecisionRequest:
        if self.decision == ReviewDecision.REQUEST_CHANGES and not self.comment:
            raise ValueError("REQUEST_CHANGES requires a comment")
        return self


class CandidateRunCreate(GovernanceModel):
    target_id: UUID
    environment_id: UUID
    automation_package_id: UUID


class PublishChangeRequest(GovernanceModel):
    regression_run_id: UUID
    confirmation: Literal["PUBLISH"]
