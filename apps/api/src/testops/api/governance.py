"""Governed case-change workflow built on immutable baseline candidates."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from testops.contracts import (
    CaseBaseline,
    CaseDefinition,
    DerivedCaseBaselineSource,
    RunStatus,
    canonical_sha256,
)

from .governance_schemas import (
    CandidateRunCreate,
    ChangeOperation,
    ChangeRequestCreate,
    ChangeRequestStatus,
    ChangeRequestUpdate,
    ChangeType,
    PublishChangeRequest,
    ReviewDecision,
    ReviewDecisionRequest,
)
from .persistence import (
    ApprovalRecord,
    CaseBaselineRecord,
    ChangeItemRecord,
    ChangeRequestRecord,
    TestRunRecord,
    utc_now,
)
from .schemas import RunCreate
from .services import (
    InvalidRequest,
    ResourceConflict,
    ResourceNotFound,
    _audit,
    _project,
    create_run,
)


def _operation_case_id(operation: ChangeOperation) -> UUID:
    if operation.case is not None:
        return operation.case.case_id
    assert operation.case_id is not None
    return operation.case_id


def _changed_fields(
    before: dict[str, object] | None,
    after: dict[str, object] | None,
) -> list[str]:
    if before is None or after is None:
        return ["$case"]
    return sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))


def _compose_candidate(
    *,
    base: CaseBaseline,
    candidate_baseline_id: UUID,
    candidate_version: str,
    change_request_id: UUID,
    operations: tuple[ChangeOperation, ...],
) -> tuple[CaseBaseline, list[dict[str, object]]]:
    operation_ids = [_operation_case_id(operation) for operation in operations]
    if len(operation_ids) != len(set(operation_ids)):
        raise InvalidRequest("a case may only appear once in a Change Request")

    base_by_id = {case.case_id: case for case in base.cases}
    result_by_id = dict(base_by_id)
    additions: list[CaseDefinition] = []
    item_documents: list[dict[str, object]] = []
    for sequence, operation in enumerate(operations, start=1):
        case_id = _operation_case_id(operation)
        before_case = base_by_id.get(case_id)
        after_case = operation.case
        if operation.change_type == ChangeType.ADD:
            assert after_case is not None
            if before_case is not None:
                raise InvalidRequest(f"ADD case_id already exists: {case_id}")
            if any(case.case_code == after_case.case_code for case in result_by_id.values()):
                raise InvalidRequest(f"ADD case_code already exists: {after_case.case_code}")
            result_by_id[case_id] = after_case
            additions.append(after_case)
        elif operation.change_type == ChangeType.MODIFY:
            assert after_case is not None
            if before_case is None:
                raise InvalidRequest(f"MODIFY case_id does not exist: {case_id}")
            if before_case.case_code != after_case.case_code:
                raise InvalidRequest("MODIFY cannot change the stable case_code")
            if before_case == after_case:
                raise InvalidRequest(f"MODIFY has no field changes: {before_case.case_code}")
            result_by_id[case_id] = after_case
        else:
            if before_case is None:
                raise InvalidRequest(f"DELETE case_id does not exist: {case_id}")
            del result_by_id[case_id]

        before = before_case.model_dump(mode="json", exclude_none=True) if before_case else None
        after = after_case.model_dump(mode="json", exclude_none=True) if after_case else None
        item_documents.append(
            {
                "sequence": sequence,
                "change_type": operation.change_type.value,
                "case_id": case_id,
                "case_code": (after_case or before_case).case_code,
                "before_document": before,
                "after_document": after,
                "changed_fields": _changed_fields(before, after),
            }
        )

    ordered_cases = [case for case in base.cases if case.case_id in result_by_id]
    ordered_cases = [result_by_id[case.case_id] for case in ordered_cases]
    ordered_cases.extend(additions)
    if not ordered_cases:
        raise InvalidRequest("a candidate baseline must retain at least one case")
    candidate = CaseBaseline(
        baseline_id=candidate_baseline_id,
        project_key=base.project_key,
        version=candidate_version,
        source=DerivedCaseBaselineSource(
            parent_baseline_id=base.baseline_id,
            parent_version=base.version,
            parent_digest=canonical_sha256(base),
            change_set=f"change-request:{change_request_id}",
        ),
        cases=tuple(ordered_cases),
    )
    return candidate, item_documents


async def _released_base(
    session: AsyncSession,
    project_id: UUID,
    baseline_id: UUID,
) -> CaseBaselineRecord:
    record = await session.get(CaseBaselineRecord, baseline_id)
    if record is None or record.project_id != project_id or record.status != "RELEASED":
        raise ResourceNotFound("released base baseline not found in project")
    return record


async def _ensure_candidate_version_available(
    session: AsyncSession,
    project_id: UUID,
    candidate_version: str,
    *,
    exclude_request_id: UUID | None = None,
    exclude_baseline_id: UUID | None = None,
) -> None:
    baseline_query = select(CaseBaselineRecord.baseline_id).where(
        CaseBaselineRecord.project_id == project_id,
        CaseBaselineRecord.version == candidate_version,
    )
    if exclude_baseline_id is not None:
        baseline_query = baseline_query.where(CaseBaselineRecord.baseline_id != exclude_baseline_id)
    baseline = await session.scalar(baseline_query)
    query = select(ChangeRequestRecord.id).where(
        ChangeRequestRecord.project_id == project_id,
        ChangeRequestRecord.candidate_version == candidate_version,
    )
    if exclude_request_id is not None:
        query = query.where(ChangeRequestRecord.id != exclude_request_id)
    request_id = await session.scalar(query)
    if baseline is not None or request_id is not None:
        raise ResourceConflict("candidate baseline version already exists in this project")


def _new_items(
    change_request_id: UUID,
    documents: Iterable[dict[str, object]],
) -> list[ChangeItemRecord]:
    return [
        ChangeItemRecord(
            id=uuid4(),
            change_request_id=change_request_id,
            **document,
        )
        for document in documents
    ]


async def create_change_request(
    session: AsyncSession,
    project_id: UUID,
    payload: ChangeRequestCreate,
    actor_id: UUID,
) -> ChangeRequestRecord:
    await _project(session, project_id)
    base_record = await _released_base(session, project_id, payload.base_baseline_id)
    await _ensure_candidate_version_available(session, project_id, payload.candidate_version)
    request_id = uuid4()
    candidate_baseline_id = uuid4()
    base = CaseBaseline.model_validate(base_record.document)
    candidate, documents = _compose_candidate(
        base=base,
        candidate_baseline_id=candidate_baseline_id,
        candidate_version=payload.candidate_version,
        change_request_id=request_id,
        operations=payload.changes,
    )
    validation_required = any(
        document["change_type"] in {ChangeType.ADD.value, ChangeType.MODIFY.value}
        and document["after_document"] is not None
        and bool(document["after_document"].get("enabled", True))
        for document in documents
    )
    record = ChangeRequestRecord(
        id=request_id,
        project_id=project_id,
        base_baseline_id=base_record.baseline_id,
        candidate_baseline_id=candidate_baseline_id,
        candidate_version=candidate.version,
        candidate_digest=canonical_sha256(candidate),
        candidate_document=candidate.model_dump(mode="json", exclude_none=True),
        title=payload.title,
        reason=payload.reason,
        created_by=actor_id,
        validation_status="PENDING_EXECUTION" if validation_required else "NOT_REQUIRED",
    )
    session.add(record)
    session.add_all(_new_items(request_id, documents))
    session.add(
        CaseBaselineRecord(
            baseline_id=candidate.baseline_id,
            project_id=project_id,
            version=candidate.version,
            digest=record.candidate_digest,
            case_count=len(candidate.cases),
            enabled_case_count=sum(case.enabled for case in candidate.cases),
            source_kind=candidate.source.kind,
            status="DRAFT",
            document=record.candidate_document,
        )
    )
    session.add(
        _audit(
            actor_id=actor_id,
            action="change_request.created",
            resource_type="change_request",
            resource_id=request_id,
            project_id=project_id,
            details={
                "base_baseline_id": str(base_record.baseline_id),
                "candidate_version": candidate.version,
                "change_count": len(documents),
            },
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ResourceConflict("candidate baseline version already exists") from exc
    await session.refresh(record)
    return record


async def get_change_request(
    session: AsyncSession,
    project_id: UUID,
    request_id: UUID,
    *,
    for_update: bool = False,
) -> ChangeRequestRecord:
    query = select(ChangeRequestRecord).where(
        ChangeRequestRecord.id == request_id,
        ChangeRequestRecord.project_id == project_id,
    )
    if for_update:
        query = query.with_for_update()
    record = await session.scalar(query)
    if record is None:
        raise ResourceNotFound("Change Request not found in project")
    return record


async def list_change_requests(
    session: AsyncSession,
    project_id: UUID,
) -> tuple[ChangeRequestRecord, ...]:
    return tuple(
        await session.scalars(
            select(ChangeRequestRecord)
            .where(ChangeRequestRecord.project_id == project_id)
            .order_by(ChangeRequestRecord.created_at.desc())
        )
    )


async def change_request_items(
    session: AsyncSession,
    request_id: UUID,
) -> tuple[ChangeItemRecord, ...]:
    return tuple(
        await session.scalars(
            select(ChangeItemRecord)
            .where(ChangeItemRecord.change_request_id == request_id)
            .order_by(ChangeItemRecord.sequence)
        )
    )


async def change_request_approvals(
    session: AsyncSession,
    request_id: UUID,
) -> tuple[ApprovalRecord, ...]:
    return tuple(
        await session.scalars(
            select(ApprovalRecord)
            .where(ApprovalRecord.change_request_id == request_id)
            .order_by(ApprovalRecord.created_at)
        )
    )


async def change_count(session: AsyncSession, request_id: UUID) -> int:
    value = await session.scalar(
        select(func.count())
        .select_from(ChangeItemRecord)
        .where(ChangeItemRecord.change_request_id == request_id)
    )
    return int(value or 0)


async def update_change_request(
    session: AsyncSession,
    project_id: UUID,
    request_id: UUID,
    payload: ChangeRequestUpdate,
    actor_id: UUID,
    *,
    actor_is_project_admin: bool,
) -> ChangeRequestRecord:
    record = await get_change_request(session, project_id, request_id, for_update=True)
    if record.status not in {
        ChangeRequestStatus.DRAFT.value,
        ChangeRequestStatus.CHANGES_REQUESTED.value,
    }:
        raise ResourceConflict("only a draft or changes-requested item can be edited")
    if record.created_by != actor_id and not actor_is_project_admin:
        raise ResourceConflict("only the submitter or Project Admin can edit this draft")
    await _ensure_candidate_version_available(
        session,
        project_id,
        payload.candidate_version,
        exclude_request_id=request_id,
        exclude_baseline_id=record.candidate_baseline_id,
    )
    base_record = await _released_base(session, project_id, record.base_baseline_id)
    old_baseline = await session.get(CaseBaselineRecord, record.candidate_baseline_id)
    if old_baseline is None or old_baseline.status != "DRAFT":
        raise ResourceConflict("editable draft baseline is missing")
    old_baseline.status = "SUPERSEDED"
    old_baseline.version = f"{old_baseline.version}~{old_baseline.baseline_id.hex[:8]}"
    new_candidate_baseline_id = uuid4()
    candidate, documents = _compose_candidate(
        base=CaseBaseline.model_validate(base_record.document),
        candidate_baseline_id=new_candidate_baseline_id,
        candidate_version=payload.candidate_version,
        change_request_id=request_id,
        operations=payload.changes,
    )
    await session.execute(
        delete(ChangeItemRecord).where(ChangeItemRecord.change_request_id == request_id)
    )
    record.candidate_version = candidate.version
    record.candidate_baseline_id = candidate.baseline_id
    record.candidate_digest = canonical_sha256(candidate)
    record.candidate_document = candidate.model_dump(mode="json", exclude_none=True)
    record.title = payload.title
    record.reason = payload.reason
    record.status = ChangeRequestStatus.DRAFT.value
    record.submitted_at = None
    record.reviewed_at = None
    record.validation_run_id = None
    record.validation_status = (
        "PENDING_EXECUTION"
        if any(
            document["change_type"] in {ChangeType.ADD.value, ChangeType.MODIFY.value}
            and document["after_document"] is not None
            and bool(document["after_document"].get("enabled", True))
            for document in documents
        )
        else "NOT_REQUIRED"
    )
    session.add_all(_new_items(request_id, documents))
    session.add(
        CaseBaselineRecord(
            baseline_id=candidate.baseline_id,
            project_id=project_id,
            version=candidate.version,
            digest=record.candidate_digest,
            case_count=len(candidate.cases),
            enabled_case_count=sum(case.enabled for case in candidate.cases),
            source_kind=candidate.source.kind,
            status="DRAFT",
            document=record.candidate_document,
        )
    )
    session.add(
        _audit(
            actor_id=actor_id,
            action="change_request.updated",
            resource_type="change_request",
            resource_id=request_id,
            project_id=project_id,
            details={"candidate_version": candidate.version, "change_count": len(documents)},
        )
    )
    await session.commit()
    await session.refresh(record)
    return record


async def submit_change_request(
    session: AsyncSession,
    project_id: UUID,
    request_id: UUID,
    actor_id: UUID,
) -> ChangeRequestRecord:
    record = await get_change_request(session, project_id, request_id, for_update=True)
    if record.created_by != actor_id:
        raise ResourceConflict("only the Change Request submitter can submit it")
    if record.status != ChangeRequestStatus.DRAFT.value:
        raise ResourceConflict("only a DRAFT Change Request can be submitted")
    if record.validation_status not in {"PASSED", "NOT_REQUIRED"}:
        raise ResourceConflict("affected cases must pass draft validation before submission")
    record.status = ChangeRequestStatus.IN_REVIEW.value
    record.submitted_at = utc_now()
    session.add(
        _audit(
            actor_id=actor_id,
            action="change_request.submitted",
            resource_type="change_request",
            resource_id=request_id,
            project_id=project_id,
            details={"candidate_digest": record.candidate_digest},
        )
    )
    await session.commit()
    await session.refresh(record)
    return record


async def review_change_request(
    session: AsyncSession,
    project_id: UUID,
    request_id: UUID,
    payload: ReviewDecisionRequest,
    reviewer_id: UUID,
) -> ChangeRequestRecord:
    record = await get_change_request(session, project_id, request_id, for_update=True)
    if record.status != ChangeRequestStatus.IN_REVIEW.value:
        raise ResourceConflict("Change Request is not awaiting review")
    if record.created_by == reviewer_id:
        raise ResourceConflict("the submitter cannot review their own Change Request")
    approval = ApprovalRecord(
        id=uuid4(),
        change_request_id=request_id,
        reviewer_id=reviewer_id,
        decision=payload.decision.value,
        comment=payload.comment,
    )
    session.add(approval)
    record.reviewed_at = utc_now()
    if payload.decision == ReviewDecision.REQUEST_CHANGES:
        record.status = ChangeRequestStatus.CHANGES_REQUESTED.value
    else:
        candidate = await session.get(CaseBaselineRecord, record.candidate_baseline_id)
        if candidate is None or candidate.status != "DRAFT":
            raise ResourceConflict("approved draft baseline is missing")
        candidate.status = "CANDIDATE"
        record.status = ChangeRequestStatus.CANDIDATE.value
    session.add(
        _audit(
            actor_id=reviewer_id,
            action="change_request.reviewed",
            resource_type="change_request",
            resource_id=request_id,
            project_id=project_id,
            details={"decision": payload.decision.value, "status": record.status},
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ResourceConflict("candidate baseline version or digest already exists") from exc
    await session.refresh(record)
    return record


async def create_candidate_run(
    session: AsyncSession,
    project_id: UUID,
    request_id: UUID,
    payload: CandidateRunCreate,
    *,
    idempotency_key: str,
    actor_id: UUID,
) -> tuple[TestRunRecord, bool]:
    record = await get_change_request(session, project_id, request_id)
    if record.status != ChangeRequestStatus.CANDIDATE.value:
        raise ResourceConflict("only an approved CANDIDATE can run full regression")
    run_payload = RunCreate(
        project_id=project_id,
        target_id=payload.target_id,
        environment_id=payload.environment_id,
        baseline_id=record.candidate_baseline_id,
        automation_package_id=payload.automation_package_id,
        case_codes=(),
    )
    return await create_run(
        session,
        run_payload,
        idempotency_key=idempotency_key,
        actor_id=actor_id,
        allowed_baseline_statuses=frozenset({"CANDIDATE"}),
    )


async def create_validation_run(
    session: AsyncSession,
    project_id: UUID,
    request_id: UUID,
    payload: CandidateRunCreate,
    *,
    idempotency_key: str,
    actor_id: UUID,
) -> tuple[TestRunRecord | None, bool]:
    record = await get_change_request(session, project_id, request_id, for_update=True)
    if record.status not in {
        ChangeRequestStatus.DRAFT.value,
        ChangeRequestStatus.CHANGES_REQUESTED.value,
    }:
        raise ResourceConflict("only an editable draft can run affected-case validation")
    items = await change_request_items(session, request_id)
    case_codes = tuple(
        item.case_code
        for item in items
        if item.change_type in {ChangeType.ADD.value, ChangeType.MODIFY.value}
        and item.after_document is not None
        and bool(item.after_document.get("enabled", True))
    )
    if not case_codes:
        record.validation_status = "NOT_REQUIRED"
        record.validation_run_id = None
        await session.commit()
        await session.refresh(record)
        return None, True
    if record.validation_run_id is not None:
        existing_run = await session.get(TestRunRecord, record.validation_run_id)
        if existing_run is not None:
            if existing_run.idempotency_key == idempotency_key:
                return existing_run, False
            if RunStatus(existing_run.status) not in {
                RunStatus.PASSED,
                RunStatus.FAILED,
                RunStatus.CANCELED,
                RunStatus.TIMED_OUT,
                RunStatus.INFRA_ERROR,
            }:
                raise ResourceConflict("draft validation is already running")
    run_payload = RunCreate(
        project_id=project_id,
        target_id=payload.target_id,
        environment_id=payload.environment_id,
        baseline_id=record.candidate_baseline_id,
        automation_package_id=payload.automation_package_id,
        case_codes=case_codes,
    )
    run, created = await create_run(
        session,
        run_payload,
        idempotency_key=idempotency_key,
        actor_id=actor_id,
        allowed_baseline_statuses=frozenset({"DRAFT"}),
    )
    record = await get_change_request(session, project_id, request_id, for_update=True)
    record.validation_run_id = run.id
    record.validation_status = "QUEUED"
    session.add(
        _audit(
            actor_id=actor_id,
            action="change_request.validation_started",
            resource_type="change_request",
            resource_id=request_id,
            project_id=project_id,
            details={"run_id": str(run.id), "case_count": run.case_count},
        )
    )
    await session.commit()
    return run, created


async def publish_change_request(
    session: AsyncSession,
    project_id: UUID,
    request_id: UUID,
    payload: PublishChangeRequest,
    actor_id: UUID,
) -> tuple[ChangeRequestRecord, CaseBaselineRecord]:
    record = await get_change_request(session, project_id, request_id, for_update=True)
    if record.status != ChangeRequestStatus.CANDIDATE.value:
        raise ResourceConflict("only a CANDIDATE Change Request can be published")
    run = await session.get(TestRunRecord, payload.regression_run_id)
    if (
        run is None
        or run.project_id != project_id
        or run.baseline_id != record.candidate_baseline_id
    ):
        raise InvalidRequest("regression Run does not execute this candidate baseline")
    if RunStatus(run.status) != RunStatus.PASSED or run.result_digest is None:
        raise ResourceConflict("candidate full regression must finish PASSED before publishing")
    baseline = await session.get(CaseBaselineRecord, record.candidate_baseline_id)
    if baseline is None or baseline.status != "CANDIDATE":
        raise ResourceConflict("candidate baseline is missing or already published")
    if run.case_count != baseline.enabled_case_count:
        raise InvalidRequest("publishing requires a full regression of every enabled case")
    baseline.status = "RELEASED"
    record.status = ChangeRequestStatus.PUBLISHED.value
    record.published_at = utc_now()
    record.published_baseline_id = baseline.baseline_id
    session.add(
        _audit(
            actor_id=actor_id,
            action="change_request.published",
            resource_type="change_request",
            resource_id=request_id,
            project_id=project_id,
            details={
                "baseline_id": str(baseline.baseline_id),
                "version": baseline.version,
                "regression_run_id": str(run.id),
            },
        )
    )
    await session.commit()
    await session.refresh(record)
    await session.refresh(baseline)
    return record, baseline
