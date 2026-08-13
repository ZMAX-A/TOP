"""HTTP request and response models for the M2 control-plane API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from testops.contracts import (
    CaseBaseline,
    CaseResultStatus,
    RunResult,
    RunSnapshot,
    RunStatus,
    RuntimeVariable,
    SecretBinding,
    TargetType,
    WebRunConfig,
)

ResourceKey = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]*$")]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class ProjectCreate(ApiModel):
    key: ResourceKey
    name: NonEmptyText
    description: str | None = None


class ProjectUpdate(ApiModel):
    name: NonEmptyText | None = None
    description: str | None = None
    status: Literal["ACTIVE", "ARCHIVED"] | None = None

    @model_validator(mode="after")
    def require_change(self) -> ProjectUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one project field must be provided")
        return self


class ProjectResponse(ApiModel):
    id: UUID
    key: str
    name: str
    description: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class ExecutionPolicyUpdate(ApiModel):
    max_in_flight_runs: Annotated[int, Field(ge=1, le=500)] | None = None
    max_daily_runs: Annotated[int, Field(ge=1, le=100_000)] | None = None

    @model_validator(mode="after")
    def require_change(self) -> ExecutionPolicyUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one execution policy field must be provided")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("execution policy fields cannot be null")
        return self


class ExecutionPolicyResponse(ApiModel):
    project_id: UUID
    max_in_flight_runs: int
    max_daily_runs: int
    in_flight_runs: Annotated[int, Field(ge=0)]
    queued_runs: Annotated[int, Field(ge=0)]
    preparing_runs: Annotated[int, Field(ge=0)]
    running_runs: Annotated[int, Field(ge=0)]
    runs_created_today: Annotated[int, Field(ge=0)]
    remaining_in_flight_runs: Annotated[int, Field(ge=0)]
    remaining_daily_runs: Annotated[int, Field(ge=0)]
    quota_status: Literal["AVAILABLE", "NEAR_LIMIT", "BLOCKED"]
    daily_window_started_at: datetime
    generated_at: datetime
    updated_at: datetime


class RunnerCapabilities(ApiModel):
    target_types: Annotated[tuple[TargetType, ...], Field(min_length=1, max_length=8)]
    browsers: Annotated[
        tuple[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)], ...],
        Field(max_length=16),
    ] = ()
    labels: dict[
        Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.-]*$")],
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)],
    ] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_capabilities(self) -> RunnerCapabilities:
        if len(self.target_types) != len(set(self.target_types)):
            raise ValueError("target_types contains duplicates")
        if len(self.browsers) != len(set(self.browsers)):
            raise ValueError("browsers contains duplicates")
        return self


class RunnerPoolCreate(ApiModel):
    key: ResourceKey
    name: NonEmptyText
    description: str | None = None
    target_types: Annotated[tuple[TargetType, ...], Field(min_length=1, max_length=8)]
    max_concurrency: Annotated[int, Field(ge=1, le=500)] = 1

    @model_validator(mode="after")
    def unique_target_types(self) -> RunnerPoolCreate:
        if len(self.target_types) != len(set(self.target_types)):
            raise ValueError("target_types contains duplicates")
        return self


class RunnerPoolUpdate(ApiModel):
    name: NonEmptyText | None = None
    description: str | None = None
    target_types: Annotated[tuple[TargetType, ...], Field(min_length=1, max_length=8)] | None = None
    max_concurrency: Annotated[int, Field(ge=1, le=500)] | None = None
    status: Literal["ACTIVE", "DRAINING", "DISABLED"] | None = None

    @model_validator(mode="after")
    def require_change(self) -> RunnerPoolUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one Runner Pool field must be provided")
        if any(
            field != "description" and getattr(self, field) is None
            for field in self.model_fields_set
        ):
            raise ValueError("Runner Pool fields cannot be null")
        if self.target_types is not None and len(self.target_types) != len(set(self.target_types)):
            raise ValueError("target_types contains duplicates")
        return self


class RunnerPoolResponse(ApiModel):
    id: UUID
    key: str
    name: str
    description: str | None
    target_types: tuple[TargetType, ...]
    queue_name: str
    max_concurrency: int
    status: Literal["ACTIVE", "DRAINING", "DISABLED"]
    healthy_workers: Annotated[int, Field(ge=0)]
    total_worker_slots: Annotated[int, Field(ge=0)]
    active_leases: Annotated[int, Field(ge=0)]
    available_slots: Annotated[int, Field(ge=0)]
    created_at: datetime
    updated_at: datetime


class RunnerPoolCatalogResponse(ApiModel):
    id: UUID
    key: str
    name: str
    target_types: tuple[TargetType, ...]
    status: Literal["ACTIVE", "DRAINING", "DISABLED"]
    available_slots: Annotated[int, Field(ge=0)]


class RunnerWorkerHeartbeat(ApiModel):
    pool_key: ResourceKey
    display_name: NonEmptyText
    runner_version: NonEmptyText
    max_slots: Annotated[int, Field(ge=1, le=100)] = 1
    capabilities: RunnerCapabilities


class RunnerWorkerUpdate(ApiModel):
    status: Literal["ACTIVE", "DRAINING", "DISABLED"]


class RunnerWorkerResponse(ApiModel):
    id: UUID
    pool_id: UUID
    pool_key: str
    worker_key: str
    display_name: str
    runner_version: str
    max_slots: int
    capabilities: RunnerCapabilities
    status: Literal["ACTIVE", "DRAINING", "DISABLED"]
    health: Literal["ONLINE", "STALE"]
    last_heartbeat_at: datetime
    created_at: datetime
    updated_at: datetime


class TargetCreate(ApiModel):
    key: ResourceKey
    name: NonEmptyText
    target_type: TargetType
    browser: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None = None
    runner_pool_id: UUID | None = None


class TargetUpdate(ApiModel):
    name: NonEmptyText | None = None
    browser: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None = None
    status: Literal["ACTIVE", "ARCHIVED"] | None = None
    runner_pool_id: UUID | None = None

    @model_validator(mode="after")
    def require_change(self) -> TargetUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one target field must be provided")
        return self


class TargetResponse(ApiModel):
    id: UUID
    project_id: UUID
    key: str
    name: str
    target_type: TargetType
    browser: str | None
    runner_pool_id: UUID | None
    status: str
    created_at: datetime
    updated_at: datetime


class EnvironmentCreate(ApiModel):
    key: ResourceKey
    name: NonEmptyText
    web_config: WebRunConfig | None = None
    variables: tuple[RuntimeVariable, ...] = ()
    secret_bindings: tuple[SecretBinding, ...] = ()
    runner_pool_id: UUID | None = None


class EnvironmentUpdate(ApiModel):
    name: NonEmptyText | None = None
    web_config: WebRunConfig | None = None
    variables: tuple[RuntimeVariable, ...] | None = None
    secret_bindings: tuple[SecretBinding, ...] | None = None
    status: Literal["ACTIVE", "ARCHIVED"] | None = None
    runner_pool_id: UUID | None = None

    @model_validator(mode="after")
    def require_change(self) -> EnvironmentUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one environment field must be provided")
        return self


class EnvironmentResponse(ApiModel):
    id: UUID
    project_id: UUID
    target_id: UUID
    runner_pool_id: UUID | None
    key: str
    name: str
    web_config: WebRunConfig | None
    variables: tuple[RuntimeVariable, ...]
    secret_bindings: tuple[SecretBinding, ...]
    config_hash: Digest
    status: str
    created_at: datetime
    updated_at: datetime


class BaselinePublishRequest(ApiModel):
    baseline: CaseBaseline
    digest: Digest


class BaselineResponse(ApiModel):
    baseline_id: UUID
    project_id: UUID
    version: str
    digest: Digest
    case_count: int
    enabled_case_count: int
    source_kind: str
    status: str
    created_at: datetime


class AutomationPackageCreate(ApiModel):
    name: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]*$")]
    version: NonEmptyText
    digest: Digest


class AutomationPackageResponse(ApiModel):
    id: UUID
    project_id: UUID
    target_id: UUID
    name: str
    version: str
    digest: Digest
    status: str
    created_at: datetime


class RunCreate(ApiModel):
    project_id: UUID
    target_id: UUID
    environment_id: UUID
    baseline_id: UUID
    automation_package_id: UUID
    case_codes: tuple[Annotated[str, StringConstraints(pattern=r"^TC-[A-Z0-9-]+$")], ...] = ()


class RunResponse(ApiModel):
    id: UUID
    project_id: UUID
    target_id: UUID
    environment_id: UUID
    baseline_id: UUID
    automation_package_id: UUID
    runner_pool_id: UUID | None
    source_run_id: UUID | None
    retry_mode: Literal["FULL", "FAILED_ONLY"] | None
    status: RunStatus
    case_count: int
    snapshot_digest: Digest
    result_digest: Digest | None
    cancel_requested: bool
    dispatch_state: Literal["PENDING", "WAITING", "DISPATCHED"]
    dispatch_wait_reason: str | None
    dispatched_at: datetime | None
    created_by: UUID
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None


class RunCaseResponse(ApiModel):
    id: UUID
    case_id: UUID
    case_code: str
    sequence: int
    status: str
    duration_ms: int | None
    failure_category: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class ArtifactResponse(ApiModel):
    artifact_id: UUID
    run_id: UUID
    kind: str
    name: str
    uri: str
    digest: Digest
    size_bytes: int
    created_at: datetime


class RunDetailResponse(RunResponse):
    snapshot: RunSnapshot
    result: RunResult | None
    cases: tuple[RunCaseResponse, ...]
    artifacts: tuple[ArtifactResponse, ...]


class RunListResponse(ApiModel):
    items: tuple[RunResponse, ...]
    total: Annotated[int, Field(ge=0)]


class RunRerunRequest(ApiModel):
    mode: Literal["FULL", "FAILED_ONLY"] = "FULL"


class RunBatchCancelRequest(ApiModel):
    run_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def unique_run_ids(self) -> RunBatchCancelRequest:
        if len(self.run_ids) != len(set(self.run_ids)):
            raise ValueError("run_ids contains duplicates")
        return self


class RunBatchCancelResponse(ApiModel):
    items: tuple[RunResponse, ...]
    requested: Annotated[int, Field(ge=1)]
    changed: Annotated[int, Field(ge=0)]


class InternalRunStatusUpdate(ApiModel):
    status: RunStatus


class InternalRunStatusResponse(ApiModel):
    run_id: UUID
    status: RunStatus
    changed: bool


class InternalRunResultResponse(ApiModel):
    run_id: UUID
    status: RunStatus
    result_digest: Digest
    created: bool


RunnerEventType = Literal["run_started", "case_started", "case_finished", "run_finished"]


class InternalRunEventCreate(ApiModel):
    run_id: UUID
    event: RunnerEventType
    at: datetime
    case_code: Annotated[str, StringConstraints(pattern=r"^TC-[A-Z0-9-]+$")] | None = None
    status: RunStatus | CaseResultStatus | None = None

    @model_validator(mode="after")
    def validate_event_shape(self) -> InternalRunEventCreate:
        if self.at.tzinfo is None or self.at.utcoffset() is None:
            raise ValueError("Run event timestamp must include timezone")
        case_event = self.event in {"case_started", "case_finished"}
        if case_event != (self.case_code is not None):
            raise ValueError("case events require case_code and Run events forbid it")
        finished_event = self.event in {"case_finished", "run_finished"}
        if finished_event != (self.status is not None):
            raise ValueError("finished events require status and started events forbid it")
        status_value = self.status.value if self.status is not None else None
        if self.event == "case_finished" and status_value not in {
            item.value for item in CaseResultStatus
        }:
            raise ValueError("case_finished requires a Case Result status")
        if self.event == "run_finished" and status_value not in {
            RunStatus.PASSED.value,
            RunStatus.FAILED.value,
            RunStatus.CANCELED.value,
            RunStatus.TIMED_OUT.value,
            RunStatus.INFRA_ERROR.value,
        }:
            raise ValueError("run_finished requires a terminal Run status")
        return self


class RunEventResponse(ApiModel):
    id: UUID
    run_id: UUID
    sequence: int
    source: str
    event_type: str
    case_code: str | None
    status: str | None
    payload: dict[str, object]
    occurred_at: datetime
    created_at: datetime


class ArtifactAccessResponse(ApiModel):
    artifact_id: UUID
    name: str
    kind: str
    digest: Digest
    size_bytes: int
    url: str
    expires_at: datetime
