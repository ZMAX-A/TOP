"""Authentication, user administration and project-membership routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_session
from .identity import (
    CurrentPrincipal,
    authenticate,
    bootstrap_admin,
    create_user,
    list_audit_logs,
    list_managed_sessions,
    list_project_member_candidates,
    list_project_members,
    list_users,
    list_users_page,
    logout,
    remove_project_member,
    reset_user_password,
    revoke_managed_session,
    update_user,
    upsert_project_member,
)
from .identity_schemas import (
    AuditLogPageResponse,
    AuditLogResponse,
    BootstrapAdminRequest,
    LoginRequest,
    ManagedSessionPageResponse,
    ManagedSessionResponse,
    ProjectMemberCandidateResponse,
    ProjectMemberResponse,
    ProjectMemberUpsert,
    SessionResponse,
    SystemRole,
    UserCreate,
    UserPageResponse,
    UserPasswordReset,
    UserResponse,
    UserStatus,
    UserUpdate,
)
from .persistence import UserRecord

Session = Annotated[AsyncSession, Depends(get_session)]
router = APIRouter(prefix="/api/v1")


def _user_response(user: object) -> UserResponse:
    return UserResponse.model_validate(user)


def _managed_session_response(auth_session: object, user: object) -> ManagedSessionResponse:
    expires_at = auth_session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return ManagedSessionResponse(
        id=auth_session.id,
        user_id=auth_session.user_id,
        username=user.username,
        display_name=user.display_name,
        expires_at=auth_session.expires_at,
        created_at=auth_session.created_at,
        revoked_at=auth_session.revoked_at,
        active=auth_session.revoked_at is None and expires_at > datetime.now(UTC),
    )


@router.post(
    "/auth/bootstrap",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["authentication"],
)
async def post_bootstrap_admin(
    payload: BootstrapAdminRequest,
    request: Request,
    session: Session,
    bootstrap_token: Annotated[str | None, Header(alias="X-Bootstrap-Token")] = None,
) -> UserResponse:
    user = await bootstrap_admin(
        session,
        payload,
        bootstrap_token,
        request.app.state.settings.bootstrap_admin_token,
    )
    return _user_response(user)


@router.post("/auth/login", response_model=SessionResponse, tags=["authentication"])
async def post_login(
    payload: LoginRequest,
    request: Request,
    session: Session,
) -> SessionResponse:
    user, token, expires_at = await authenticate(
        session,
        payload,
        session_ttl_hours=request.app.state.settings.session_ttl_hours,
    )
    return SessionResponse(access_token=token, expires_at=expires_at, user=_user_response(user))


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, tags=["authentication"])
async def post_logout(principal: CurrentPrincipal, session: Session) -> None:
    await logout(session, principal)


@router.get("/auth/me", response_model=UserResponse, tags=["authentication"])
async def get_me(principal: CurrentPrincipal, session: Session) -> UserResponse:
    user = await session.get(UserRecord, principal.user_id)
    assert user is not None
    return _user_response(user)


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["users"],
)
async def post_user(
    payload: UserCreate,
    principal: CurrentPrincipal,
    session: Session,
) -> UserResponse:
    return _user_response(await create_user(session, payload, principal))


@router.get("/users", response_model=tuple[UserResponse, ...], tags=["users"])
async def get_users(principal: CurrentPrincipal, session: Session) -> tuple[UserResponse, ...]:
    return tuple(_user_response(user) for user in await list_users(session, principal))


@router.get("/admin/users", response_model=UserPageResponse, tags=["system-management"])
async def get_admin_users(
    principal: CurrentPrincipal,
    session: Session,
    query: Annotated[str | None, Query(max_length=128)] = None,
    user_status: Annotated[UserStatus | None, Query(alias="status")] = None,
    system_role: SystemRole | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> UserPageResponse:
    users, total = await list_users_page(
        session,
        principal,
        query=query,
        status=user_status,
        system_role=system_role,
        limit=limit,
        offset=offset,
    )
    return UserPageResponse(
        items=tuple(_user_response(user) for user in users),
        total=total,
        offset=offset,
        limit=limit,
    )


@router.patch(
    "/admin/users/{user_id}",
    response_model=UserResponse,
    tags=["system-management"],
)
async def patch_admin_user(
    user_id: UUID,
    payload: UserUpdate,
    principal: CurrentPrincipal,
    session: Session,
) -> UserResponse:
    return _user_response(await update_user(session, user_id, payload, principal))


@router.post(
    "/admin/users/{user_id}/password-reset",
    response_model=UserResponse,
    tags=["system-management"],
)
async def post_admin_user_password_reset(
    user_id: UUID,
    payload: UserPasswordReset,
    principal: CurrentPrincipal,
    session: Session,
) -> UserResponse:
    return _user_response(await reset_user_password(session, user_id, payload, principal))


@router.get(
    "/admin/sessions",
    response_model=ManagedSessionPageResponse,
    tags=["system-management"],
)
async def get_admin_sessions(
    principal: CurrentPrincipal,
    session: Session,
    user_id: UUID | None = None,
    active_only: bool = True,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ManagedSessionPageResponse:
    rows, total = await list_managed_sessions(
        session,
        principal,
        user_id=user_id,
        active_only=active_only,
        limit=limit,
        offset=offset,
    )
    return ManagedSessionPageResponse(
        items=tuple(_managed_session_response(item, user) for item, user in rows),
        total=total,
        offset=offset,
        limit=limit,
    )


@router.post(
    "/admin/sessions/{session_id}/revoke",
    response_model=ManagedSessionResponse,
    tags=["system-management"],
)
async def post_admin_session_revoke(
    session_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> ManagedSessionResponse:
    auth_session, user = await revoke_managed_session(session, session_id, principal)
    return _managed_session_response(auth_session, user)


@router.get(
    "/admin/audit-logs",
    response_model=AuditLogPageResponse,
    tags=["system-management"],
)
async def get_admin_audit_logs(
    principal: CurrentPrincipal,
    session: Session,
    project_id: UUID | None = None,
    actor_id: UUID | None = None,
    action: Annotated[str | None, Query(max_length=100)] = None,
    resource_type: Annotated[str | None, Query(max_length=64)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AuditLogPageResponse:
    rows, total = await list_audit_logs(
        session,
        principal,
        project_id=project_id,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        limit=limit,
        offset=offset,
    )
    return AuditLogPageResponse(
        items=tuple(
            AuditLogResponse(
                id=item.id,
                project_id=item.project_id,
                actor_id=item.actor_id,
                actor_username=user.username if user else None,
                actor_display_name=user.display_name if user else None,
                action=item.action,
                resource_type=item.resource_type,
                resource_id=item.resource_id,
                details=item.details,
                created_at=item.created_at,
            )
            for item, user in rows
        ),
        total=total,
        offset=offset,
        limit=limit,
    )


def _member_response(member: object, user: object) -> ProjectMemberResponse:
    return ProjectMemberResponse(
        id=member.id,
        project_id=member.project_id,
        user_id=member.user_id,
        username=user.username,
        display_name=user.display_name,
        role=member.role,
        created_at=member.created_at,
        updated_at=member.updated_at,
    )


@router.put(
    "/projects/{project_id}/members",
    response_model=ProjectMemberResponse,
    tags=["project-members"],
)
async def put_project_member(
    project_id: UUID,
    payload: ProjectMemberUpsert,
    principal: CurrentPrincipal,
    session: Session,
) -> ProjectMemberResponse:
    member, user = await upsert_project_member(session, project_id, payload, principal)
    return _member_response(member, user)


@router.get(
    "/projects/{project_id}/members",
    response_model=tuple[ProjectMemberResponse, ...],
    tags=["project-members"],
)
async def get_project_members(
    project_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> tuple[ProjectMemberResponse, ...]:
    rows = await list_project_members(session, project_id, principal)
    return tuple(_member_response(member, user) for member, user in rows)


@router.get(
    "/projects/{project_id}/member-candidates",
    response_model=tuple[ProjectMemberCandidateResponse, ...],
    tags=["project-members"],
)
async def get_project_member_candidates(
    project_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
    query: Annotated[str | None, Query(max_length=128)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> tuple[ProjectMemberCandidateResponse, ...]:
    users = await list_project_member_candidates(
        session,
        project_id,
        principal,
        query=query,
        limit=limit,
    )
    return tuple(ProjectMemberCandidateResponse.model_validate(user) for user in users)


@router.delete(
    "/projects/{project_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["project-members"],
)
async def delete_project_member(
    project_id: UUID,
    user_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> None:
    await remove_project_member(session, project_id, user_id, principal)
