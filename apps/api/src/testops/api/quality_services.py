"""Project quality analytics and SLO policy services."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .persistence import (
    AuditLogRecord,
    CaseBaselineRecord,
    EnvironmentRecord,
    ProjectRecord,
    RunCaseRecord,
    TestRunRecord,
    TestTargetRecord,
    utc_now,
)
from .schemas import (
    FailureClusterResponse,
    FlakyCaseAnalysisResponse,
    FlakyCaseResponse,
    QualityAnalyticsResponse,
    QualityCaseSummary,
    QualityDimensionFilters,
    QualityPolicyResponse,
    QualityPolicyUpdate,
    QualityRunSummary,
    QualityTrendPoint,
)
from .services import InvalidRequest, ResourceNotFound

TERMINAL_RUN_STATUSES = ("PASSED", "FAILED", "CANCELED", "TIMED_OUT", "INFRA_ERROR")
TERMINAL_CASE_STATUSES = (
    "PASSED",
    "FAILED",
    "SKIPPED",
    "CANCELED",
    "TIMED_OUT",
    "INFRA_ERROR",
)
FAILURE_CASE_STATUSES = ("FAILED", "TIMED_OUT", "INFRA_ERROR")
MAX_FAILURE_ROWS = 5_000
MAX_FLAKY_ROWS = 5_000
MAX_FLAKY_CASES = 20
FLAKY_MIN_CONCLUSIVE_EXECUTIONS = 3
FLAKY_MIN_STATUS_TRANSITIONS = 2

_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|authorization)\s*[:=]\s*[^\s,;]+"
)
_URL_PATTERN = re.compile(r"(?i)\b(?:https?|wss?)://[^\s]+")
_UUID_PATTERN = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)
_HEX_PATTERN = re.compile(r"(?i)\b[0-9a-f]{12,}\b")
_NUMBER_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\b")
_WHITESPACE_PATTERN = re.compile(r"\s+")


async def _project(session: AsyncSession, project_id: UUID) -> ProjectRecord:
    record = await session.get(ProjectRecord, project_id)
    if record is None:
        raise ResourceNotFound("project not found")
    return record


async def _locked_project(session: AsyncSession, project_id: UUID) -> ProjectRecord:
    record = await session.scalar(
        select(ProjectRecord).where(ProjectRecord.id == project_id).with_for_update()
    )
    if record is None:
        raise ResourceNotFound("project not found")
    return record


def _policy_response(project: ProjectRecord) -> QualityPolicyResponse:
    return QualityPolicyResponse(
        project_id=project.id,
        target_pass_rate_percent=project.quality_slo_target_percent,
        window_days=project.quality_slo_window_days,
        updated_at=project.updated_at,
    )


async def get_quality_policy(
    session: AsyncSession,
    project_id: UUID,
) -> QualityPolicyResponse:
    return _policy_response(await _project(session, project_id))


async def update_quality_policy(
    session: AsyncSession,
    project_id: UUID,
    payload: QualityPolicyUpdate,
    actor_id: UUID,
) -> QualityPolicyResponse:
    project = await _locked_project(session, project_id)
    before = {
        "target_pass_rate_percent": project.quality_slo_target_percent,
        "window_days": project.quality_slo_window_days,
    }
    if payload.target_pass_rate_percent is not None:
        project.quality_slo_target_percent = payload.target_pass_rate_percent
    if payload.window_days is not None:
        project.quality_slo_window_days = payload.window_days
    after = {
        "target_pass_rate_percent": project.quality_slo_target_percent,
        "window_days": project.quality_slo_window_days,
    }
    session.add(
        AuditLogRecord(
            actor_id=actor_id,
            action="project.quality_policy_updated",
            resource_type="project_quality_policy",
            resource_id=str(project.id),
            project_id=project.id,
            details={"before": before, "after": after},
        )
    )
    await session.commit()
    await session.refresh(project)
    return _policy_response(project)


def _percentage(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator * 100 / denominator, 2)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _bucket_date(value: Any) -> date:
    if isinstance(value, datetime):
        return _aware(value).astimezone(UTC).date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _message_pattern(message: str | None) -> str:
    normalized = (message or "No error message").strip()
    normalized = _CREDENTIAL_PATTERN.sub(lambda match: f"{match.group(1)}=<redacted>", normalized)
    normalized = _URL_PATTERN.sub("<url>", normalized)
    normalized = _UUID_PATTERN.sub("<uuid>", normalized)
    normalized = _HEX_PATTERN.sub("<hex>", normalized)
    normalized = _NUMBER_PATTERN.sub("<n>", normalized)
    normalized = _WHITESPACE_PATTERN.sub(" ", normalized).strip()
    return normalized[:240] or "No error message"


def _cluster_fingerprint(category: str, pattern: str) -> str:
    digest = hashlib.sha256(f"{category}\0{pattern.lower()}".encode()).hexdigest()
    return f"sha256:{digest}"


def _count_map(rows: Any) -> dict[str, int]:
    return {str(status): int(count) for status, count in rows}


def _detect_flaky_cases(rows: Any) -> tuple[tuple[FlakyCaseResponse, ...], int]:
    histories: dict[UUID, list[tuple[datetime, str, str, str]]] = {}
    for case_id, case_code, status_value, occurred_at, run_id in rows:
        status = str(status_value)
        if status not in {"PASSED", "FAILED"}:
            continue
        histories.setdefault(case_id, []).append(
            (_aware(occurred_at), str(run_id), str(case_code), status)
        )

    detected: list[FlakyCaseResponse] = []
    for case_id, observations in histories.items():
        ordered = sorted(observations, key=lambda item: (item[0], item[1]))
        if len(ordered) < FLAKY_MIN_CONCLUSIVE_EXECUTIONS:
            continue
        statuses = [item[3] for item in ordered]
        passed = statuses.count("PASSED")
        failed = statuses.count("FAILED")
        transitions = sum(
            previous != current for previous, current in zip(statuses, statuses[1:], strict=False)
        )
        if passed == 0 or failed == 0 or transitions < FLAKY_MIN_STATUS_TRANSITIONS:
            continue
        latest_at, _latest_run_id, latest_code, latest_status = ordered[-1]
        pass_rate = _percentage(passed, len(ordered))
        transition_rate = _percentage(transitions, len(ordered) - 1)
        assert pass_rate is not None and transition_rate is not None
        detected.append(
            FlakyCaseResponse(
                case_id=case_id,
                case_code=latest_code,
                conclusive_executions=len(ordered),
                passed_executions=passed,
                failed_executions=failed,
                pass_rate_percent=pass_rate,
                status_transitions=transitions,
                transition_rate_percent=transition_rate,
                latest_status=latest_status,
                latest_completed_at=latest_at,
            )
        )

    detected.sort(
        key=lambda item: (
            -item.transition_rate_percent,
            -item.status_transitions,
            -item.conclusive_executions,
            -item.latest_completed_at.timestamp(),
            item.case_code,
            str(item.case_id),
        )
    )
    return tuple(detected[:MAX_FLAKY_CASES]), len(detected)


async def _validate_dimension_filters(
    session: AsyncSession,
    project_id: UUID,
    *,
    target_id: UUID | None,
    environment_id: UUID | None,
    baseline_id: UUID | None,
) -> None:
    target: TestTargetRecord | None = None
    if target_id is not None:
        target = await session.get(TestTargetRecord, target_id)
        if target is None or target.project_id != project_id:
            raise ResourceNotFound("quality target filter not found in project")

    if environment_id is not None:
        environment = await session.get(EnvironmentRecord, environment_id)
        if environment is None or environment.project_id != project_id:
            raise ResourceNotFound("quality environment filter not found in project")
        if target is not None and environment.target_id != target.id:
            raise InvalidRequest("quality environment filter does not belong to target filter")

    if baseline_id is not None:
        baseline = await session.get(CaseBaselineRecord, baseline_id)
        if baseline is None or baseline.project_id != project_id:
            raise ResourceNotFound("quality baseline filter not found in project")


async def get_quality_analytics(
    session: AsyncSession,
    project_id: UUID,
    *,
    window_days: int | None = None,
    target_id: UUID | None = None,
    environment_id: UUID | None = None,
    baseline_id: UUID | None = None,
    now: datetime | None = None,
) -> QualityAnalyticsResponse:
    project = await _project(session, project_id)
    await _validate_dimension_filters(
        session,
        project_id,
        target_id=target_id,
        environment_id=environment_id,
        baseline_id=baseline_id,
    )
    days = window_days or project.quality_slo_window_days
    moment = _aware(now or utc_now()).astimezone(UTC)
    window_started_at = datetime.combine(
        moment.date() - timedelta(days=days - 1),
        time.min,
        tzinfo=UTC,
    )
    completed_at = func.coalesce(TestRunRecord.finished_at, TestRunRecord.updated_at)
    run_filter = [
        TestRunRecord.project_id == project_id,
        TestRunRecord.status.in_(TERMINAL_RUN_STATUSES),
        completed_at >= window_started_at,
        completed_at <= moment,
    ]
    if target_id is not None:
        run_filter.append(TestRunRecord.target_id == target_id)
    if environment_id is not None:
        run_filter.append(TestRunRecord.environment_id == environment_id)
    if baseline_id is not None:
        run_filter.append(TestRunRecord.baseline_id == baseline_id)

    run_status_rows = await session.execute(
        select(TestRunRecord.status, func.count()).where(*run_filter).group_by(TestRunRecord.status)
    )
    run_counts = _count_map(run_status_rows)
    passed_runs = run_counts.get("PASSED", 0)
    failed_runs = run_counts.get("FAILED", 0)
    canceled_runs = run_counts.get("CANCELED", 0)
    timed_out_runs = run_counts.get("TIMED_OUT", 0)
    infra_error_runs = run_counts.get("INFRA_ERROR", 0)
    conclusive_runs = passed_runs + failed_runs
    reliability_denominator = conclusive_runs + timed_out_runs + infra_error_runs
    total_terminal_runs = sum(run_counts.values())

    case_status_rows = await session.execute(
        select(RunCaseRecord.status, func.count())
        .join(TestRunRecord, TestRunRecord.id == RunCaseRecord.run_id)
        .where(*run_filter, RunCaseRecord.status.in_(TERMINAL_CASE_STATUSES))
        .group_by(RunCaseRecord.status)
    )
    case_counts = _count_map(case_status_rows)
    passed_cases = case_counts.get("PASSED", 0)
    failed_cases = case_counts.get("FAILED", 0)
    skipped_cases = case_counts.get("SKIPPED", 0)
    canceled_cases = case_counts.get("CANCELED", 0)
    timed_out_cases = case_counts.get("TIMED_OUT", 0)
    infra_error_cases = case_counts.get("INFRA_ERROR", 0)
    conclusive_cases = passed_cases + failed_cases

    bucket_expression = func.date(completed_at)
    trend_rows = await session.execute(
        select(bucket_expression, TestRunRecord.status, func.count())
        .where(*run_filter)
        .group_by(bucket_expression, TestRunRecord.status)
        .order_by(bucket_expression)
    )
    trend_counts: dict[date, dict[str, int]] = {}
    for bucket, status, count in trend_rows:
        trend_counts.setdefault(_bucket_date(bucket), {})[str(status)] = int(count)
    trend: list[QualityTrendPoint] = []
    for index in range(days):
        bucket = window_started_at.date() + timedelta(days=index)
        counts = trend_counts.get(bucket, {})
        bucket_passed = counts.get("PASSED", 0)
        bucket_failed = counts.get("FAILED", 0)
        trend.append(
            QualityTrendPoint(
                bucket_started_at=datetime.combine(bucket, time.min, tzinfo=UTC),
                total_terminal_runs=sum(counts.values()),
                passed_runs=bucket_passed,
                failed_runs=bucket_failed,
                canceled_runs=counts.get("CANCELED", 0),
                timed_out_runs=counts.get("TIMED_OUT", 0),
                infra_error_runs=counts.get("INFRA_ERROR", 0),
                pass_rate_percent=_percentage(
                    bucket_passed,
                    bucket_passed + bucket_failed,
                ),
            )
        )

    failure_result = await session.execute(
        select(
            RunCaseRecord.run_id,
            RunCaseRecord.case_code,
            RunCaseRecord.status,
            RunCaseRecord.failure_category,
            RunCaseRecord.error_message,
            completed_at,
        )
        .join(TestRunRecord, TestRunRecord.id == RunCaseRecord.run_id)
        .where(*run_filter, RunCaseRecord.status.in_(FAILURE_CASE_STATUSES))
        .order_by(completed_at.desc())
        .limit(MAX_FAILURE_ROWS + 1)
    )
    failure_rows = list(failure_result)
    failure_data_truncated = len(failure_rows) > MAX_FAILURE_ROWS
    clusters: dict[str, dict[str, Any]] = {}
    for run_id, case_code, status, category_value, message, occurred_at in failure_rows[
        :MAX_FAILURE_ROWS
    ]:
        category = str(category_value or ("TIMEOUT" if status == "TIMED_OUT" else "UNKNOWN"))
        pattern = _message_pattern(message)
        fingerprint = _cluster_fingerprint(category, pattern)
        cluster = clusters.setdefault(
            fingerprint,
            {
                "fingerprint": fingerprint,
                "failure_category": category,
                "message_pattern": pattern,
                "occurrences": 0,
                "run_ids": set(),
                "status_counts": Counter(),
                "case_counts": Counter(),
                "latest_at": _aware(occurred_at),
            },
        )
        cluster["occurrences"] += 1
        cluster["run_ids"].add(run_id)
        cluster["status_counts"][str(status)] += 1
        cluster["case_counts"][str(case_code)] += 1
        cluster["latest_at"] = max(cluster["latest_at"], _aware(occurred_at))

    failure_clusters = tuple(
        FailureClusterResponse(
            fingerprint=cluster["fingerprint"],
            failure_category=cluster["failure_category"],
            message_pattern=cluster["message_pattern"],
            occurrences=cluster["occurrences"],
            affected_runs=len(cluster["run_ids"]),
            failed_occurrences=cluster["status_counts"].get("FAILED", 0),
            timed_out_occurrences=cluster["status_counts"].get("TIMED_OUT", 0),
            infra_error_occurrences=cluster["status_counts"].get("INFRA_ERROR", 0),
            case_codes=tuple(code for code, _count in cluster["case_counts"].most_common(5)),
            latest_at=cluster["latest_at"],
        )
        for cluster in sorted(
            clusters.values(),
            key=lambda item: (
                -item["occurrences"],
                -item["latest_at"].timestamp(),
                item["fingerprint"],
            ),
        )[:20]
    )
    flaky_result = await session.execute(
        select(
            RunCaseRecord.case_id,
            RunCaseRecord.case_code,
            RunCaseRecord.status,
            completed_at,
            TestRunRecord.id,
        )
        .join(TestRunRecord, TestRunRecord.id == RunCaseRecord.run_id)
        .where(*run_filter, RunCaseRecord.status.in_(("PASSED", "FAILED")))
        .order_by(completed_at.desc(), TestRunRecord.id.desc())
        .limit(MAX_FLAKY_ROWS + 1)
    )
    flaky_rows = list(flaky_result)
    flaky_data_truncated = len(flaky_rows) > MAX_FLAKY_ROWS
    flaky_cases, flaky_case_count = _detect_flaky_cases(flaky_rows[:MAX_FLAKY_ROWS])
    latest_completed_at = await session.scalar(select(func.max(completed_at)).where(*run_filter))
    pass_rate = _percentage(passed_runs, conclusive_runs)
    if pass_rate is None:
        slo_status = "NO_DATA"
    elif pass_rate >= project.quality_slo_target_percent:
        slo_status = "MET"
    else:
        slo_status = "BREACHED"

    return QualityAnalyticsResponse(
        project_id=project.id,
        filters=QualityDimensionFilters(
            target_id=target_id,
            environment_id=environment_id,
            baseline_id=baseline_id,
        ),
        window_days=days,
        window_started_at=window_started_at,
        window_ended_at=moment,
        generated_at=moment,
        target_pass_rate_percent=project.quality_slo_target_percent,
        slo_status=slo_status,
        latest_completed_at=(
            _aware(latest_completed_at) if latest_completed_at is not None else None
        ),
        runs=QualityRunSummary(
            total_terminal_runs=total_terminal_runs,
            conclusive_runs=conclusive_runs,
            passed_runs=passed_runs,
            failed_runs=failed_runs,
            canceled_runs=canceled_runs,
            timed_out_runs=timed_out_runs,
            infra_error_runs=infra_error_runs,
            pass_rate_percent=pass_rate,
            execution_reliability_percent=_percentage(
                conclusive_runs,
                reliability_denominator,
            ),
        ),
        cases=QualityCaseSummary(
            total_terminal_cases=sum(case_counts.values()),
            conclusive_cases=conclusive_cases,
            passed_cases=passed_cases,
            failed_cases=failed_cases,
            skipped_cases=skipped_cases,
            canceled_cases=canceled_cases,
            timed_out_cases=timed_out_cases,
            infra_error_cases=infra_error_cases,
            pass_rate_percent=_percentage(passed_cases, conclusive_cases),
        ),
        trend=tuple(trend),
        failure_clusters=failure_clusters,
        failure_data_truncated=failure_data_truncated,
        flaky=FlakyCaseAnalysisResponse(
            minimum_conclusive_executions=FLAKY_MIN_CONCLUSIVE_EXECUTIONS,
            minimum_status_transitions=FLAKY_MIN_STATUS_TRANSITIONS,
            analyzed_executions=min(len(flaky_rows), MAX_FLAKY_ROWS),
            detected_cases=flaky_case_count,
            data_truncated=flaky_data_truncated,
            cases=flaky_cases,
        ),
    )
