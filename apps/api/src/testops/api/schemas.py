"""HTTP request and response models for the M2 control-plane API."""

from __future__ import annotations

from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

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

from .cron import CronValidationError, normalize_cron_expression, validate_timezone

ResourceKey = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]*$")]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC)


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
    run_timeout_seconds: Annotated[int, Field(ge=60, le=86_400)] | None = None

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
    run_timeout_seconds: int
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


class QualityPolicyUpdate(ApiModel):
    target_pass_rate_percent: Annotated[int, Field(ge=1, le=100)] | None = None
    window_days: Annotated[int, Field(ge=7, le=90)] | None = None
    alert_warning_drop_percentage_points: Annotated[int, Field(ge=1, le=100)] | None = None
    alert_critical_drop_percentage_points: Annotated[int, Field(ge=1, le=100)] | None = None

    @model_validator(mode="after")
    def require_change(self) -> QualityPolicyUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one quality policy field must be provided")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("quality policy fields cannot be null")
        if (
            self.alert_warning_drop_percentage_points is not None
            and self.alert_critical_drop_percentage_points is not None
            and self.alert_warning_drop_percentage_points
            >= self.alert_critical_drop_percentage_points
        ):
            raise ValueError("quality warning drop must be lower than critical drop")
        return self


class QualityPolicyResponse(ApiModel):
    project_id: UUID
    target_pass_rate_percent: Annotated[int, Field(ge=1, le=100)]
    window_days: Annotated[int, Field(ge=7, le=90)]
    alert_warning_drop_percentage_points: Annotated[int, Field(ge=1, le=100)]
    alert_critical_drop_percentage_points: Annotated[int, Field(ge=1, le=100)]
    updated_at: datetime


WebhookEndpoint = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2048),
]
WebhookSecretName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Z][A-Z0-9_]{0,99}$"),
]
WebhookSecretRef = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^secret://[^\s]{1,490}$"),
]
QualityAlertMetric = Literal["RUN_PASS_RATE", "CASE_PASS_RATE", "EXECUTION_RELIABILITY"]
QualityAlertOperatorNote = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]


class QualityWebhookConfigUpdate(ApiModel):
    enabled: bool | None = None
    endpoint_url: WebhookEndpoint | None = None
    minimum_alert_status: Literal["WARNING", "CRITICAL"] | None = None
    cooldown_seconds: Annotated[int, Field(ge=60, le=86400)] | None = None
    signing_secret_name: WebhookSecretName | None = None
    signing_secret_ref: WebhookSecretRef | None = None
    clear_signing_secret: bool = False

    @model_validator(mode="after")
    def validate_update(self) -> QualityWebhookConfigUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one quality webhook field must be provided")
        for field in ("enabled", "minimum_alert_status", "cooldown_seconds"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        if "endpoint_url" in self.model_fields_set:
            if self.endpoint_url is None:
                raise ValueError("endpoint_url cannot be null")
            if any(ord(character) < 32 for character in self.endpoint_url):
                raise ValueError("quality webhook endpoint cannot contain control characters")
            parsed = urlsplit(self.endpoint_url)
            if parsed.scheme.lower() != "https" or not parsed.hostname:
                raise ValueError("quality webhook endpoint must be an HTTPS URL")
            if parsed.username or parsed.password or parsed.fragment:
                raise ValueError(
                    "quality webhook endpoint cannot contain credentials or a fragment"
                )
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError("quality webhook endpoint has an invalid port") from exc
            if port not in {None, 443}:
                raise ValueError("quality webhook endpoint must use port 443")
            hostname = parsed.hostname.lower()
            if hostname == "localhost" or hostname.endswith(".localhost"):
                raise ValueError("quality webhook endpoint cannot use localhost")
            try:
                address = ip_address(hostname)
            except ValueError:
                pass
            else:
                if not address.is_global:
                    raise ValueError("quality webhook endpoint cannot use a private IP address")
        secret_fields = {"signing_secret_name", "signing_secret_ref"}
        supplied_secret_fields = secret_fields.intersection(self.model_fields_set)
        if supplied_secret_fields and supplied_secret_fields != secret_fields:
            raise ValueError("signing secret name and ref must be provided together")
        if supplied_secret_fields and (
            self.signing_secret_name is None or self.signing_secret_ref is None
        ):
            raise ValueError("signing secret name and ref cannot be null")
        if supplied_secret_fields and self.clear_signing_secret:
            raise ValueError("cannot replace and clear the signing secret together")
        if self.model_fields_set == {"clear_signing_secret"} and not self.clear_signing_secret:
            raise ValueError("clear_signing_secret must be true when used alone")
        return self


class QualityWebhookConfigResponse(ApiModel):
    project_id: UUID
    enabled: bool
    endpoint_configured: bool
    endpoint_display: str | None
    minimum_alert_status: Literal["WARNING", "CRITICAL"]
    cooldown_seconds: Annotated[int, Field(ge=60, le=86400)]
    signing_configured: bool
    last_evaluated_at: datetime | None
    next_evaluation_at: datetime | None
    silenced_until: datetime | None
    silenced_by: UUID | None
    silenced_by_display_name: str | None
    silence_reason: str | None
    updated_at: datetime | None


class QualityAlertSilenceUpdate(ApiModel):
    silenced_until: datetime
    reason: QualityAlertOperatorNote

    @field_validator("silenced_until")
    @classmethod
    def normalize_silenced_until(cls, value: datetime) -> datetime:
        normalized = _as_utc(value)
        assert normalized is not None
        return normalized


class QualityAlertAcknowledgementUpdate(ApiModel):
    note: QualityAlertOperatorNote


class QualityWebhookReplayRequest(ApiModel):
    reason: QualityAlertOperatorNote


class QualityWebhookDeliveryResponse(ApiModel):
    id: UUID
    project_id: UUID
    event_type: str
    destination_display: str
    status: Literal["PENDING", "DELIVERED", "FAILED"]
    attempts: Annotated[int, Field(ge=0)]
    response_status: Annotated[int, Field(ge=100, le=599)] | None
    last_error: str | None
    replay_of_id: UUID | None
    replayed_by: UUID | None
    replayed_by_display_name: str | None
    replay_reason: str | None
    created_at: datetime
    delivered_at: datetime | None


class QualityAlertStateResponse(ApiModel):
    project_id: UUID
    metric: QualityAlertMetric
    current_status: Literal["NO_DATA", "STABLE", "WARNING", "CRITICAL"]
    active_notification_status: Literal["WARNING", "CRITICAL"] | None
    current_percent: Annotated[float, Field(ge=0, le=100)] | None
    previous_percent: Annotated[float, Field(ge=0, le=100)] | None
    delta_percentage_points: Annotated[float, Field(ge=-100, le=100)] | None
    notification_sequence: Annotated[int, Field(ge=0)]
    last_evaluated_at: datetime
    last_transition_at: datetime
    last_notified_at: datetime | None
    cooldown_until: datetime | None
    last_delivery_id: UUID | None
    acknowledged_at: datetime | None
    acknowledged_by: UUID | None
    acknowledged_by_display_name: str | None
    acknowledgement_note: str | None


class QualityRunSummary(ApiModel):
    total_terminal_runs: Annotated[int, Field(ge=0)]
    conclusive_runs: Annotated[int, Field(ge=0)]
    passed_runs: Annotated[int, Field(ge=0)]
    failed_runs: Annotated[int, Field(ge=0)]
    canceled_runs: Annotated[int, Field(ge=0)]
    timed_out_runs: Annotated[int, Field(ge=0)]
    infra_error_runs: Annotated[int, Field(ge=0)]
    pass_rate_percent: Annotated[float, Field(ge=0, le=100)] | None
    execution_reliability_percent: Annotated[float, Field(ge=0, le=100)] | None


class QualityCaseSummary(ApiModel):
    total_terminal_cases: Annotated[int, Field(ge=0)]
    conclusive_cases: Annotated[int, Field(ge=0)]
    passed_cases: Annotated[int, Field(ge=0)]
    failed_cases: Annotated[int, Field(ge=0)]
    skipped_cases: Annotated[int, Field(ge=0)]
    canceled_cases: Annotated[int, Field(ge=0)]
    timed_out_cases: Annotated[int, Field(ge=0)]
    infra_error_cases: Annotated[int, Field(ge=0)]
    pass_rate_percent: Annotated[float, Field(ge=0, le=100)] | None


class QualityTrendPoint(ApiModel):
    bucket_started_at: datetime
    total_terminal_runs: Annotated[int, Field(ge=0)]
    passed_runs: Annotated[int, Field(ge=0)]
    failed_runs: Annotated[int, Field(ge=0)]
    canceled_runs: Annotated[int, Field(ge=0)]
    timed_out_runs: Annotated[int, Field(ge=0)]
    infra_error_runs: Annotated[int, Field(ge=0)]
    pass_rate_percent: Annotated[float, Field(ge=0, le=100)] | None


class FailureClusterResponse(ApiModel):
    fingerprint: Digest
    failure_category: str
    message_pattern: str
    occurrences: Annotated[int, Field(ge=1)]
    affected_runs: Annotated[int, Field(ge=1)]
    failed_occurrences: Annotated[int, Field(ge=0)]
    timed_out_occurrences: Annotated[int, Field(ge=0)]
    infra_error_occurrences: Annotated[int, Field(ge=0)]
    case_codes: tuple[str, ...]
    latest_at: datetime


class FlakyCaseResponse(ApiModel):
    case_id: UUID
    case_code: str
    conclusive_executions: Annotated[int, Field(ge=3)]
    passed_executions: Annotated[int, Field(ge=1)]
    failed_executions: Annotated[int, Field(ge=1)]
    pass_rate_percent: Annotated[float, Field(ge=0, le=100)]
    status_transitions: Annotated[int, Field(ge=2)]
    transition_rate_percent: Annotated[float, Field(ge=0, le=100)]
    latest_status: Literal["PASSED", "FAILED"]
    latest_completed_at: datetime


class FlakyCaseAnalysisResponse(ApiModel):
    minimum_conclusive_executions: Annotated[int, Field(ge=3)]
    minimum_status_transitions: Annotated[int, Field(ge=2)]
    analyzed_executions: Annotated[int, Field(ge=0)]
    detected_cases: Annotated[int, Field(ge=0)]
    data_truncated: bool
    cases: tuple[FlakyCaseResponse, ...]


class QualityDimensionFilters(ApiModel):
    target_id: UUID | None
    environment_id: UUID | None
    baseline_id: UUID | None


class QualityChangeSignal(ApiModel):
    metric: Literal["RUN_PASS_RATE", "CASE_PASS_RATE", "EXECUTION_RELIABILITY"]
    current_percent: Annotated[float, Field(ge=0, le=100)] | None
    previous_percent: Annotated[float, Field(ge=0, le=100)] | None
    delta_percentage_points: Annotated[float, Field(ge=-100, le=100)] | None
    alert_status: Literal["NO_DATA", "STABLE", "WARNING", "CRITICAL"]


class QualityWindowComparison(ApiModel):
    previous_window_started_at: datetime
    previous_window_ended_at: datetime
    warning_drop_percentage_points: Annotated[int, Field(ge=1, le=100)]
    critical_drop_percentage_points: Annotated[int, Field(ge=1, le=100)]
    alert_status: Literal["NO_DATA", "STABLE", "WARNING", "CRITICAL"]
    signals: tuple[QualityChangeSignal, ...]


class QualityAnalyticsResponse(ApiModel):
    project_id: UUID
    filters: QualityDimensionFilters
    window_days: Annotated[int, Field(ge=1, le=90)]
    window_started_at: datetime
    window_ended_at: datetime
    generated_at: datetime
    target_pass_rate_percent: Annotated[int, Field(ge=1, le=100)]
    slo_status: Literal["NO_DATA", "MET", "BREACHED"]
    comparison: QualityWindowComparison
    latest_completed_at: datetime | None
    runs: QualityRunSummary
    cases: QualityCaseSummary
    trend: tuple[QualityTrendPoint, ...]
    failure_clusters: tuple[FailureClusterResponse, ...]
    failure_data_truncated: bool
    flaky: FlakyCaseAnalysisResponse


class RegressionScheduleCreate(ApiModel):
    key: ResourceKey
    name: NonEmptyText
    description: str | None = None
    target_id: UUID
    environment_id: UUID
    baseline_id: UUID
    automation_package_id: UUID
    case_codes: tuple[Annotated[str, StringConstraints(pattern=r"^TC-[A-Z0-9-]+$")], ...] = ()
    cron_expression: Annotated[str, StringConstraints(strip_whitespace=True, max_length=128)]
    timezone: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    misfire_policy: Literal["SKIP", "FIRE_ONCE"] = "FIRE_ONCE"
    misfire_grace_seconds: Annotated[int, Field(ge=60, le=86_400)] = 300
    status: Literal["ACTIVE", "PAUSED"] = "ACTIVE"

    @field_validator("cron_expression")
    @classmethod
    def valid_cron_expression(cls, value: str) -> str:
        try:
            return normalize_cron_expression(value)
        except CronValidationError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            return validate_timezone(value)
        except CronValidationError as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="after")
    def unique_case_codes(self) -> RegressionScheduleCreate:
        if len(self.case_codes) != len(set(self.case_codes)):
            raise ValueError("case_codes contains duplicates")
        return self


class RegressionScheduleUpdate(ApiModel):
    name: NonEmptyText | None = None
    description: str | None = None
    target_id: UUID | None = None
    environment_id: UUID | None = None
    baseline_id: UUID | None = None
    automation_package_id: UUID | None = None
    case_codes: tuple[Annotated[str, StringConstraints(pattern=r"^TC-[A-Z0-9-]+$")], ...] | None = (
        None
    )
    cron_expression: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=128)] | None
    ) = None
    timezone: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)] | None
    ) = None
    misfire_policy: Literal["SKIP", "FIRE_ONCE"] | None = None
    misfire_grace_seconds: Annotated[int, Field(ge=60, le=86_400)] | None = None
    status: Literal["ACTIVE", "PAUSED", "ARCHIVED"] | None = None

    @field_validator("cron_expression")
    @classmethod
    def valid_cron_expression(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return normalize_cron_expression(value)
        except CronValidationError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return validate_timezone(value)
        except CronValidationError as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="after")
    def require_change(self) -> RegressionScheduleUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one regression schedule field must be provided")
        if any(
            field != "description" and getattr(self, field) is None
            for field in self.model_fields_set
        ):
            raise ValueError("regression schedule fields cannot be null")
        if self.case_codes is not None and len(self.case_codes) != len(set(self.case_codes)):
            raise ValueError("case_codes contains duplicates")
        return self


class RegressionScheduleResponse(ApiModel):
    id: UUID
    project_id: UUID
    key: str
    name: str
    description: str | None
    target_id: UUID
    environment_id: UUID
    baseline_id: UUID
    automation_package_id: UUID
    case_codes: tuple[str, ...]
    cron_expression: str
    timezone: str
    misfire_policy: Literal["SKIP", "FIRE_ONCE"]
    misfire_grace_seconds: int
    status: Literal["ACTIVE", "PAUSED", "ARCHIVED"]
    next_fire_at: datetime | None
    last_scheduled_for: datetime | None
    last_triggered_at: datetime | None
    last_run_id: UUID | None
    last_error: str | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "next_fire_at",
        "last_scheduled_for",
        "last_triggered_at",
        "created_at",
        "updated_at",
    )
    @classmethod
    def utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value)


class RegressionScheduleFiringResponse(ApiModel):
    id: UUID
    schedule_id: UUID
    run_id: UUID | None
    scheduled_for: datetime
    triggered_at: datetime | None
    trigger_kind: Literal["SCHEDULED", "MISFIRE", "MANUAL"]
    status: Literal["TRIGGERED", "SKIPPED", "BLOCKED"]
    error_message: str | None
    created_at: datetime

    @field_validator("scheduled_for", "triggered_at", "created_at")
    @classmethod
    def utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value)


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
    runner_type: Literal["WEB_PLAYWRIGHT"] = "WEB_PLAYWRIGHT"
    image_repository: NonEmptyText = "testops-worker"

    @field_validator("image_repository")
    @classmethod
    def repository_without_mutable_reference(cls, value: str) -> str:
        if "://" in value or "@" in value or any(character.isspace() for character in value):
            raise ValueError("image_repository must be an OCI repository without scheme or digest")
        if ":" in value.rsplit("/", 1)[-1]:
            raise ValueError("image_repository must not contain a mutable image tag")
        return value


class AutomationPackageDraftCreate(AutomationPackageCreate):
    supersedes_id: UUID | None = None


class AutomationPackageValidationRunCreate(ApiModel):
    environment_id: UUID
    baseline_id: UUID


class AutomationPackageActivateRequest(ApiModel):
    validation_run_id: UUID


class AutomationPackageStatusChangeRequest(ApiModel):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class AutomationPackageResponse(ApiModel):
    id: UUID
    project_id: UUID
    target_id: UUID
    name: str
    version: str
    digest: Digest
    runner_type: str
    image_repository: str
    status: str
    supersedes_id: UUID | None
    validated_run_id: UUID | None
    activated_by: UUID | None
    activated_at: datetime | None
    status_reason: str | None
    created_at: datetime
    updated_at: datetime


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
    regression_schedule_id: UUID | None
    scheduled_for: datetime | None
    source_run_id: UUID | None
    retry_mode: Literal["FULL", "FAILED_ONLY"] | None
    status: RunStatus
    case_count: int
    timeout_seconds: int
    snapshot_digest: Digest
    result_digest: Digest | None
    cancel_requested: bool
    dispatch_state: Literal["PENDING", "WAITING", "DISPATCHED"]
    dispatch_wait_reason: str | None
    dispatched_at: datetime | None
    created_by: UUID
    created_at: datetime
    started_at: datetime | None
    timeout_at: datetime | None
    finished_at: datetime | None
    error_message: str | None

    @field_validator("scheduled_for", "timeout_at")
    @classmethod
    def utc_run_time(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value)


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
    worker_key: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$"),
        ]
        | None
    ) = None


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
