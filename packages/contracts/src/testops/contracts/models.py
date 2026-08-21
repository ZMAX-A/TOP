"""Versioned contracts for cases, immutable jobs and structured results."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .capabilities import get_assertion_spec, get_operation_spec

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-fA-F]{64}$")]
SecretRef = Annotated[str, StringConstraints(pattern=r"^secret://[A-Za-z0-9._/-]+$")]
ProjectKey = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]*$")]
CaseBaselineVersion = Annotated[
    str,
    StringConstraints(pattern=r"^case-v[0-9]+\.[0-9]+\.[0-9]+$"),
]


class FrozenModel(BaseModel):
    # Input data can intentionally be a single space. Normalization therefore
    # belongs at field/import boundaries rather than in the global model config.
    model_config = ConfigDict(extra="forbid", frozen=True)


class CasePriority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class TargetType(StrEnum):
    WEB = "WEB"
    APP = "APP"
    API = "API"


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    TIMED_OUT = "TIMED_OUT"
    INFRA_ERROR = "INFRA_ERROR"


class CaseResultStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    INFRA_ERROR = "INFRA_ERROR"


class ArtifactKind(StrEnum):
    LOG = "LOG"
    SCREENSHOT = "SCREENSHOT"
    VIDEO = "VIDEO"
    TRACE = "TRACE"
    ALLURE = "ALLURE"
    OTHER = "OTHER"


StepInput = str | int | float | bool | list[str] | dict[str, str] | None


class StepDefinition(FrozenModel):
    operation: NonEmptyStr
    locator: str | None = None
    input: StepInput = None
    timeout_seconds: Annotated[float, Field(gt=0, le=600)] | None = None

    @model_validator(mode="after")
    def validate_capability(self) -> StepDefinition:
        spec = get_operation_spec(self.operation)
        object.__setattr__(self, "operation", spec.key)
        if spec.requires_locator and not (self.locator and self.locator.strip()):
            raise ValueError(f"operation '{spec.key}' requires locator")
        if spec.requires_input and self.input is None:
            raise ValueError(f"operation '{spec.key}' requires input")
        if (
            spec.key == "nav"
            and self.input in (None, "")
            and not (self.locator and self.locator.strip())
        ):
            raise ValueError("operation 'nav' requires URL in locator or input")
        return self


class AssertionDefinition(FrozenModel):
    type: NonEmptyStr
    expected: StepInput = None
    locator: str | None = None
    timeout_seconds: Annotated[float, Field(gt=0, le=600)] | None = None

    @model_validator(mode="after")
    def validate_capability(self) -> AssertionDefinition:
        spec = get_assertion_spec(self.type)
        object.__setattr__(self, "type", spec.key)
        if spec.requires_locator and not (self.locator and self.locator.strip()):
            raise ValueError(f"assertion '{spec.key}' requires locator")
        return self


class CaseSourceTrace(FrozenModel):
    kind: Literal["legacy_excel"] = "legacy_excel"
    worksheet: NonEmptyStr
    row_number: Annotated[int, Field(ge=2)]


class CaseDefinition(FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    case_id: UUID
    case_code: Annotated[str, StringConstraints(pattern=r"^TC-[A-Z0-9]+-[A-Z0-9-]+$")]
    module_key: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]*$")]
    module_name: NonEmptyStr
    title: NonEmptyStr
    test_point: NonEmptyStr
    priority: CasePriority
    preconditions: tuple[NonEmptyStr, ...] = ()
    steps: Annotated[tuple[StepDefinition, ...], Field(min_length=1)]
    assertion: AssertionDefinition
    tags: tuple[NonEmptyStr, ...] = ()
    enabled: bool = True
    source_instructions: str | None = None
    data_type: str | None = None
    expected_result: str | None = None
    timeout_seconds: Annotated[float, Field(gt=0, le=600)] | None = None
    source_trace: CaseSourceTrace | None = None
    notes: str | None = None


class CaseBaselineSource(FrozenModel):
    kind: Literal["legacy_excel"] = "legacy_excel"
    file_name: NonEmptyStr
    file_digest: Sha256Digest
    worksheet: NonEmptyStr


class DerivedCaseBaselineSource(FrozenModel):
    kind: Literal["derived_baseline"] = "derived_baseline"
    parent_baseline_id: UUID
    parent_version: CaseBaselineVersion
    parent_digest: Sha256Digest
    change_set: NonEmptyStr


class CaseBaseline(FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    baseline_id: UUID
    project_key: ProjectKey
    version: CaseBaselineVersion
    source: CaseBaselineSource | DerivedCaseBaselineSource
    cases: Annotated[tuple[CaseDefinition, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_cases(self) -> CaseBaseline:
        case_ids = [case.case_id for case in self.cases]
        case_codes = [case.case_code for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case baseline contains duplicate case_id")
        if len(case_codes) != len(set(case_codes)):
            raise ValueError("case baseline contains duplicate case_code")
        return self


class CaseBaselineRef(FrozenModel):
    baseline_id: UUID
    version: CaseBaselineVersion
    digest: Sha256Digest
    case_count: Annotated[int, Field(ge=1)]


class RuntimeVariable(FrozenModel):
    name: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$")]
    value: str

    @model_validator(mode="after")
    def reject_secret_like_names(self) -> RuntimeVariable:
        if re.search(r"(?:PASSWORD|PASSWD|TOKEN|SECRET|API_KEY|PRIVATE_KEY)", self.name):
            raise ValueError(f"runtime variable '{self.name}' must use a secret binding")
        return self


class SecretBinding(FrozenModel):
    name: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$")]
    ref: SecretRef


class WebRunConfig(FrozenModel):
    base_url: Annotated[str, StringConstraints(pattern=r"^https?://[^\s]+$")]
    headless: bool = True
    viewport_width: Annotated[int, Field(ge=320, le=7680)] = 1440
    viewport_height: Annotated[int, Field(ge=240, le=4320)] = 900
    action_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 15
    navigation_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 30
    ignore_https_errors: bool = False
    capture_trace: bool = True


class AutomationPackageRuntimeRef(FrozenModel):
    runner_type: Literal["WEB_PLAYWRIGHT"] = "WEB_PLAYWRIGHT"
    image_repository: NonEmptyStr = "testops-worker"
    digest: Sha256Digest

    @field_validator("image_repository")
    @classmethod
    def repository_without_mutable_reference(cls, value: str) -> str:
        if "://" in value or "@" in value or any(character.isspace() for character in value):
            raise ValueError("image_repository must be an OCI repository without scheme or digest")
        if ":" in value.rsplit("/", 1)[-1]:
            raise ValueError("image_repository must not contain a mutable image tag")
        return value


class AutomationPackageSupplyChainRef(FrozenModel):
    verification_id: UUID
    report_digest: Sha256Digest
    policy_version: NonEmptyStr
    verifier: NonEmptyStr
    signature_bundle_digest: Sha256Digest
    provenance_digest: Sha256Digest
    sbom_digest: Sha256Digest
    certificate_issuer: NonEmptyStr
    certificate_identity: NonEmptyStr
    builder_id: NonEmptyStr
    source_repository: NonEmptyStr
    source_revision: NonEmptyStr


class AutomationPackageRef(AutomationPackageRuntimeRef):
    name: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]*$")]
    version: NonEmptyStr
    supply_chain: AutomationPackageSupplyChainRef | None = None


class RunSnapshot(FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: UUID
    project_id: UUID
    target_id: UUID
    target_type: TargetType
    environment_id: UUID
    case_baseline: CaseBaselineRef
    automation_package: AutomationPackageRef
    cases: Annotated[tuple[CaseDefinition, ...], Field(min_length=1)]
    browser: NonEmptyStr | None = None
    web_config: WebRunConfig | None = None
    config_hash: Sha256Digest
    variables: tuple[RuntimeVariable, ...] = ()
    secret_bindings: tuple[SecretBinding, ...] = ()
    created_by: UUID
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include timezone")
        return value

    @model_validator(mode="after")
    def validate_target_fields(self) -> RunSnapshot:
        if self.target_type == TargetType.WEB:
            if not self.browser:
                raise ValueError("WEB run requires browser")
            if not self.web_config:
                raise ValueError("WEB run requires web_config")
        elif self.web_config:
            raise ValueError("web_config is only valid for WEB runs")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("run snapshot contains duplicate case_id")
        if len(self.cases) > self.case_baseline.case_count:
            raise ValueError("run snapshot case count exceeds case baseline reference")
        names = [binding.name for binding in (*self.variables, *self.secret_bindings)]
        if len(names) != len(set(names)):
            raise ValueError("run snapshot contains duplicate variable binding")
        return self


class Artifact(FrozenModel):
    artifact_id: UUID
    kind: ArtifactKind
    name: NonEmptyStr
    uri: NonEmptyStr
    digest: Sha256Digest
    size_bytes: Annotated[int, Field(ge=0)]


class CaseResult(FrozenModel):
    case_id: UUID
    case_code: NonEmptyStr
    status: CaseResultStatus
    started_at: datetime
    finished_at: datetime
    duration_ms: Annotated[int, Field(ge=0)]
    failure_category: str | None = None
    error_message: str | None = None
    artifact_ids: tuple[UUID, ...] = ()

    @field_validator("started_at", "finished_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("result timestamps must include timezone")
        return value

    @model_validator(mode="after")
    def validate_times(self) -> CaseResult:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not be earlier than started_at")
        if self.status in {CaseResultStatus.FAILED, CaseResultStatus.INFRA_ERROR}:
            if not self.error_message:
                raise ValueError("failed result requires error_message")
        return self


class RunExecutionIsolationEvidence(FrozenModel):
    mode: Literal["IN_PROCESS", "SUBPROCESS", "CONTAINER", "KUBERNETES"]
    executor_version: NonEmptyStr
    dedicated_process: bool
    credential_scope: Literal["WORKER", "RUN_SECRETS_ONLY"]
    workspace_scope: Literal["RUN_DIRECTORY"] = "RUN_DIRECTORY"
    read_only_root_filesystem: bool
    network_policy: Literal["WORKER_DEFAULT", "DENY_ALL", "ALLOWLIST"]
    resource_limits_enforced: bool
    runtime_image_id: Sha256Digest | None = None
    memory_limit_bytes: Annotated[int, Field(ge=128 * 1024 * 1024)] | None = None
    cpu_limit_millis: Annotated[int, Field(ge=100, le=32_000)] | None = None
    pids_limit: Annotated[int, Field(ge=16, le=4096)] | None = None
    ephemeral_storage_limit_bytes: Annotated[int, Field(ge=64 * 1024 * 1024)] | None = None
    orchestrator_namespace: (
        Annotated[
            str,
            StringConstraints(pattern=r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$"),
        ]
        | None
    ) = None
    service_account_name: (
        Annotated[
            str,
            StringConstraints(pattern=r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$"),
        ]
        | None
    ) = None
    service_account_token_automounted: bool | None = None

    @model_validator(mode="after")
    def hard_isolation_evidence_is_complete(self) -> RunExecutionIsolationEvidence:
        if self.mode not in {"CONTAINER", "KUBERNETES"}:
            return self
        if not self.dedicated_process or self.credential_scope != "RUN_SECRETS_ONLY":
            raise ValueError("hard isolation requires a dedicated process and Run credentials")
        if not self.read_only_root_filesystem or self.network_policy == "WORKER_DEFAULT":
            raise ValueError("hard isolation requires filesystem and network enforcement")
        if not self.resource_limits_enforced:
            raise ValueError("hard isolation requires resource enforcement")
        if None in (self.runtime_image_id, self.memory_limit_bytes, self.cpu_limit_millis):
            raise ValueError("hard isolation requires an immutable image and exact limits")
        if self.mode == "CONTAINER" and self.pids_limit is None:
            raise ValueError("container isolation requires immutable image and exact limits")
        if self.mode == "KUBERNETES":
            if self.ephemeral_storage_limit_bytes is None:
                raise ValueError("Kubernetes isolation requires an ephemeral storage limit")
            if not self.orchestrator_namespace or not self.service_account_name:
                raise ValueError(
                    "Kubernetes isolation requires namespace and ServiceAccount evidence"
                )
            if self.service_account_name == "default":
                raise ValueError("Kubernetes isolation cannot use the default ServiceAccount")
            if self.service_account_token_automounted is not False:
                raise ValueError("Kubernetes isolation must disable ServiceAccount token automount")
        return self


class RunResult(FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: UUID
    status: RunStatus
    started_at: datetime
    finished_at: datetime
    runner_version: NonEmptyStr
    case_results: tuple[CaseResult, ...]
    artifacts: tuple[Artifact, ...] = ()
    execution_isolation: RunExecutionIsolationEvidence | None = None

    @field_validator("started_at", "finished_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("result timestamps must include timezone")
        return value

    @model_validator(mode="after")
    def validate_terminal_result(self) -> RunResult:
        terminal = {
            RunStatus.PASSED,
            RunStatus.FAILED,
            RunStatus.CANCELED,
            RunStatus.TIMED_OUT,
            RunStatus.INFRA_ERROR,
        }
        if self.status not in terminal:
            raise ValueError("run result requires a terminal status")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not be earlier than started_at")
        return self
