"""HTTP routes for governed Case Baseline changes."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from testops.contracts import CaseBaseline

from .database import get_session
from .governance import (
    change_count,
    change_request_approvals,
    change_request_items,
    create_candidate_run,
    create_change_request,
    create_validation_run,
    get_change_request,
    list_change_requests,
    publish_change_request,
    review_change_request,
    submit_change_request,
    update_change_request,
)
from .governance_schemas import (
    ApprovalResponse,
    CandidateRunCreate,
    ChangeItemResponse,
    ChangeRequestCreate,
    ChangeRequestDetailResponse,
    ChangeRequestResponse,
    ChangeRequestUpdate,
    PublishChangeRequest,
    ReviewDecisionRequest,
)
from .identity import CurrentPrincipal, authorize_project
from .identity_schemas import ProjectRole
from .persistence import ApprovalRecord, ChangeItemRecord, ChangeRequestRecord
from .schemas import RunResponse

Session = Annotated[AsyncSession, Depends(get_session)]
router = APIRouter(prefix="/api/v1")


def _item_response(item: ChangeItemRecord) -> ChangeItemResponse:
    return ChangeItemResponse(
        id=item.id,
        sequence=item.sequence,
        change_type=item.change_type,
        case_id=item.case_id,
        case_code=item.case_code,
        before=item.before_document,
        after=item.after_document,
        changed_fields=tuple(item.changed_fields),
    )


def _approval_response(approval: ApprovalRecord) -> ApprovalResponse:
    return ApprovalResponse.model_validate(approval)


async def _summary_response(
    session: AsyncSession,
    record: ChangeRequestRecord,
) -> ChangeRequestResponse:
    return ChangeRequestResponse(
        **{
            field: getattr(record, field)
            for field in ChangeRequestResponse.model_fields
            if field != "change_count"
        },
        change_count=await change_count(session, record.id),
    )


async def _detail_response(
    session: AsyncSession,
    record: ChangeRequestRecord,
) -> ChangeRequestDetailResponse:
    summary = await _summary_response(session, record)
    items = await change_request_items(session, record.id)
    approvals = await change_request_approvals(session, record.id)
    return ChangeRequestDetailResponse(
        **summary.model_dump(),
        candidate_baseline=CaseBaseline.model_validate(record.candidate_document),
        changes=tuple(_item_response(item) for item in items),
        approvals=tuple(_approval_response(approval) for approval in approvals),
    )


@router.post(
    "/projects/{project_id}/change-requests",
    response_model=ChangeRequestDetailResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["case-governance"],
)
async def post_change_request(
    project_id: UUID,
    payload: ChangeRequestCreate,
    principal: CurrentPrincipal,
    session: Session,
) -> ChangeRequestDetailResponse:
    await authorize_project(session, principal, project_id, "change:create")
    record = await create_change_request(session, project_id, payload, principal.user_id)
    return await _detail_response(session, record)


@router.get(
    "/projects/{project_id}/change-requests",
    response_model=tuple[ChangeRequestResponse, ...],
    tags=["case-governance"],
)
async def get_change_requests(
    project_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> tuple[ChangeRequestResponse, ...]:
    await authorize_project(session, principal, project_id, "change:read")
    records = await list_change_requests(session, project_id)
    return tuple([await _summary_response(session, record) for record in records])


@router.get(
    "/projects/{project_id}/change-requests/{request_id}",
    response_model=ChangeRequestDetailResponse,
    tags=["case-governance"],
)
async def get_change_request_detail(
    project_id: UUID,
    request_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> ChangeRequestDetailResponse:
    await authorize_project(session, principal, project_id, "change:read")
    record = await get_change_request(session, project_id, request_id)
    return await _detail_response(session, record)


@router.put(
    "/projects/{project_id}/change-requests/{request_id}",
    response_model=ChangeRequestDetailResponse,
    tags=["case-governance"],
)
async def put_change_request(
    project_id: UUID,
    request_id: UUID,
    payload: ChangeRequestUpdate,
    principal: CurrentPrincipal,
    session: Session,
) -> ChangeRequestDetailResponse:
    role = await authorize_project(session, principal, project_id, "change:edit")
    record = await update_change_request(
        session,
        project_id,
        request_id,
        payload,
        principal.user_id,
        actor_is_project_admin=principal.is_system_admin or role == ProjectRole.PROJECT_ADMIN,
    )
    return await _detail_response(session, record)


@router.post(
    "/projects/{project_id}/change-requests/{request_id}/submit",
    response_model=ChangeRequestDetailResponse,
    tags=["case-governance"],
)
async def post_change_request_submit(
    project_id: UUID,
    request_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> ChangeRequestDetailResponse:
    await authorize_project(session, principal, project_id, "change:submit")
    record = await submit_change_request(session, project_id, request_id, principal.user_id)
    return await _detail_response(session, record)


@router.post(
    "/projects/{project_id}/change-requests/{request_id}/decision",
    response_model=ChangeRequestDetailResponse,
    tags=["case-governance"],
)
async def post_change_request_decision(
    project_id: UUID,
    request_id: UUID,
    payload: ReviewDecisionRequest,
    principal: CurrentPrincipal,
    session: Session,
) -> ChangeRequestDetailResponse:
    await authorize_project(session, principal, project_id, "change:review")
    record = await review_change_request(
        session, project_id, request_id, payload, principal.user_id
    )
    return await _detail_response(session, record)


@router.post(
    "/projects/{project_id}/change-requests/{request_id}/validation-runs",
    response_model=RunResponse | None,
    status_code=status.HTTP_201_CREATED,
    tags=["case-governance"],
)
async def post_change_validation_run(
    project_id: UUID,
    request_id: UUID,
    payload: CandidateRunCreate,
    principal: CurrentPrincipal,
    session: Session,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=128),
    ],
) -> RunResponse | None:
    await authorize_project(session, principal, project_id, "run:create")
    record, created = await create_validation_run(
        session,
        project_id,
        request_id,
        payload,
        idempotency_key=idempotency_key,
        actor_id=principal.user_id,
    )
    if record is None or not created:
        response.status_code = status.HTTP_200_OK
    return RunResponse.model_validate(record) if record is not None else None


@router.post(
    "/projects/{project_id}/change-requests/{request_id}/regression-runs",
    response_model=RunResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["case-governance"],
)
async def post_candidate_regression_run(
    project_id: UUID,
    request_id: UUID,
    payload: CandidateRunCreate,
    principal: CurrentPrincipal,
    session: Session,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=128),
    ],
) -> RunResponse:
    await authorize_project(session, principal, project_id, "run:create")
    record, created = await create_candidate_run(
        session,
        project_id,
        request_id,
        payload,
        idempotency_key=idempotency_key,
        actor_id=principal.user_id,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return RunResponse.model_validate(record)


@router.post(
    "/projects/{project_id}/change-requests/{request_id}/publish",
    response_model=ChangeRequestDetailResponse,
    tags=["case-governance"],
)
async def post_change_request_publish(
    project_id: UUID,
    request_id: UUID,
    payload: PublishChangeRequest,
    principal: CurrentPrincipal,
    session: Session,
) -> ChangeRequestDetailResponse:
    await authorize_project(session, principal, project_id, "baseline:publish")
    record, _baseline = await publish_change_request(
        session, project_id, request_id, payload, principal.user_id
    )
    return await _detail_response(session, record)
