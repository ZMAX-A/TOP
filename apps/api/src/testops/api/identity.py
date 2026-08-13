"""Local identity, opaque sessions and project-scoped RBAC."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, Header
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_session
from .identity_schemas import (
    BootstrapAdminRequest,
    LoginRequest,
    ProjectMemberUpsert,
    ProjectRole,
    SystemRole,
    UserCreate,
    UserPasswordReset,
    UserStatus,
    UserUpdate,
)
from .persistence import (
    AuditLogRecord,
    AuthSessionRecord,
    ProjectMemberRecord,
    ProjectRecord,
    SystemSettingRecord,
    UserRecord,
    utc_now,
)
from .security import (
    DUMMY_PASSWORD_HASH,
    hash_password,
    issue_session_token,
    session_token_hash,
    verify_password,
)
from .services import ResourceConflict, ResourceNotFound, ServiceError


class AuthenticationRequired(ServiceError):
    status_code = 401


class PermissionDenied(ServiceError):
    status_code = 403


class BootstrapUnavailable(ServiceError):
    status_code = 503


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: UUID
    session_id: UUID
    username: str
    display_name: str
    system_role: SystemRole

    @property
    def is_system_admin(self) -> bool:
        return self.system_role == SystemRole.SYSTEM_ADMIN


PROJECT_PERMISSIONS: dict[ProjectRole, frozenset[str]] = {
    ProjectRole.VIEWER: frozenset({"project:read", "baseline:read", "run:read"}),
    ProjectRole.TESTER: frozenset(
        {
            "project:read",
            "baseline:read",
            "run:read",
            "run:create",
            "run:cancel",
            "change:read",
            "change:create",
            "change:edit",
            "change:submit",
        }
    ),
    ProjectRole.REVIEWER: frozenset(
        {
            "project:read",
            "baseline:read",
            "run:read",
            "change:read",
            "change:review",
        }
    ),
    ProjectRole.PROJECT_ADMIN: frozenset({"*"}),
}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def current_principal(
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    if authorization is None:
        raise AuthenticationRequired("Bearer authentication is required")
    scheme, separator, token = authorization.partition(" ")
    token = token.strip()
    if not separator or scheme.casefold() != "bearer" or not token:
        raise AuthenticationRequired("Bearer authentication is required")
    row = (
        await session.execute(
            select(AuthSessionRecord, UserRecord)
            .join(UserRecord, UserRecord.id == AuthSessionRecord.user_id)
            .where(AuthSessionRecord.token_hash == session_token_hash(token))
        )
    ).one_or_none()
    if row is None:
        raise AuthenticationRequired("invalid or expired session")
    auth_session, user = row
    if (
        auth_session.revoked_at is not None
        or _aware(auth_session.expires_at) <= utc_now()
        or user.status != "ACTIVE"
    ):
        raise AuthenticationRequired("invalid or expired session")
    return Principal(
        user_id=user.id,
        session_id=auth_session.id,
        username=user.username,
        display_name=user.display_name,
        system_role=SystemRole(user.system_role),
    )


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def require_system_admin(principal: Principal) -> None:
    if not principal.is_system_admin:
        raise PermissionDenied("system administrator permission is required")


async def authorize_project(
    session: AsyncSession,
    principal: Principal,
    project_id: UUID,
    permission: str,
) -> ProjectRole | None:
    if principal.is_system_admin:
        return None
    role_value = await session.scalar(
        select(ProjectMemberRecord.role).where(
            ProjectMemberRecord.project_id == project_id,
            ProjectMemberRecord.user_id == principal.user_id,
        )
    )
    if role_value is None:
        raise PermissionDenied("project membership is required")
    role = ProjectRole(role_value)
    permissions = PROJECT_PERMISSIONS[role]
    if "*" not in permissions and permission not in permissions:
        raise PermissionDenied(f"project permission is required: {permission}")
    return role


async def bootstrap_admin(
    session: AsyncSession,
    payload: BootstrapAdminRequest,
    provided_token: str | None,
    configured_token: str | None,
) -> UserRecord:
    if not configured_token:
        raise BootstrapUnavailable("administrator bootstrap is not configured")
    if provided_token is None or not hmac.compare_digest(provided_token, configured_token):
        raise AuthenticationRequired("invalid bootstrap token")
    user_count = await session.scalar(select(func.count()).select_from(UserRecord))
    if user_count:
        raise ResourceConflict("administrator bootstrap is already complete")
    user = UserRecord(
        id=uuid4(),
        username=payload.username,
        display_name=payload.display_name,
        password_hash=hash_password(payload.password),
        system_role=SystemRole.SYSTEM_ADMIN.value,
    )
    session.add(
        SystemSettingRecord(
            key="identity.bootstrapped",
            value={"completed_by": str(user.id), "username": user.username},
        )
    )
    session.add(user)
    session.add(
        AuditLogRecord(
            actor_id=user.id,
            action="identity.admin_bootstrapped",
            resource_type="user",
            resource_id=str(user.id),
            details={"username": user.username},
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ResourceConflict("administrator bootstrap is already complete") from exc
    await session.refresh(user)
    return user


async def create_user(
    session: AsyncSession,
    payload: UserCreate,
    principal: Principal,
) -> UserRecord:
    require_system_admin(principal)
    user = UserRecord(
        id=uuid4(),
        username=payload.username,
        display_name=payload.display_name,
        password_hash=hash_password(payload.password),
        system_role=payload.system_role.value,
    )
    session.add(user)
    session.add(
        AuditLogRecord(
            actor_id=principal.user_id,
            action="identity.user_created",
            resource_type="user",
            resource_id=str(user.id),
            details={"username": user.username, "system_role": user.system_role},
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ResourceConflict("username already exists") from exc
    await session.refresh(user)
    return user


async def authenticate(
    session: AsyncSession,
    payload: LoginRequest,
    *,
    session_ttl_hours: int,
) -> tuple[UserRecord, str, datetime]:
    user = await session.scalar(select(UserRecord).where(UserRecord.username == payload.username))
    password_hash = (
        user.password_hash if user is not None and user.status == "ACTIVE" else DUMMY_PASSWORD_HASH
    )
    password_is_valid = verify_password(payload.password, password_hash)
    if user is None or user.status != "ACTIVE" or not password_is_valid:
        raise AuthenticationRequired("invalid username or password")
    token = issue_session_token()
    expires_at = utc_now() + timedelta(hours=session_ttl_hours)
    auth_session = AuthSessionRecord(
        id=uuid4(),
        user_id=user.id,
        token_hash=session_token_hash(token),
        expires_at=expires_at,
    )
    session.add(auth_session)
    session.add(
        AuditLogRecord(
            actor_id=user.id,
            action="identity.session_created",
            resource_type="auth_session",
            resource_id=str(auth_session.id),
            details={"expires_at": expires_at.isoformat()},
        )
    )
    await session.commit()
    return user, token, expires_at


async def logout(session: AsyncSession, principal: Principal) -> None:
    auth_session = await session.get(AuthSessionRecord, principal.session_id)
    if auth_session is not None and auth_session.revoked_at is None:
        auth_session.revoked_at = utc_now()
        session.add(
            AuditLogRecord(
                actor_id=principal.user_id,
                action="identity.session_revoked",
                resource_type="auth_session",
                resource_id=str(principal.session_id),
                details={},
            )
        )
        await session.commit()


async def list_users(session: AsyncSession, principal: Principal) -> tuple[UserRecord, ...]:
    require_system_admin(principal)
    return tuple(await session.scalars(select(UserRecord).order_by(UserRecord.username)))


async def list_users_page(
    session: AsyncSession,
    principal: Principal,
    *,
    query: str | None,
    status: UserStatus | None,
    system_role: SystemRole | None,
    limit: int,
    offset: int,
) -> tuple[tuple[UserRecord, ...], int]:
    require_system_admin(principal)
    filters = []
    if query:
        pattern = f"%{query.strip().lower()}%"
        filters.append(
            or_(
                func.lower(UserRecord.username).like(pattern),
                func.lower(UserRecord.display_name).like(pattern),
            )
        )
    if status is not None:
        filters.append(UserRecord.status == status.value)
    if system_role is not None:
        filters.append(UserRecord.system_role == system_role.value)
    total = await session.scalar(select(func.count()).select_from(UserRecord).where(*filters))
    users = await session.scalars(
        select(UserRecord).where(*filters).order_by(UserRecord.username).limit(limit).offset(offset)
    )
    return tuple(users), int(total or 0)


async def _ensure_admin_remains(
    session: AsyncSession,
    user: UserRecord,
    *,
    next_role: SystemRole,
    next_status: UserStatus,
) -> None:
    currently_active_admin = (
        user.system_role == SystemRole.SYSTEM_ADMIN.value and user.status == UserStatus.ACTIVE.value
    )
    remains_active_admin = next_role == SystemRole.SYSTEM_ADMIN and next_status == UserStatus.ACTIVE
    if not currently_active_admin or remains_active_admin:
        return
    count = await session.scalar(
        select(func.count())
        .select_from(UserRecord)
        .where(
            UserRecord.system_role == SystemRole.SYSTEM_ADMIN.value,
            UserRecord.status == UserStatus.ACTIVE.value,
        )
    )
    if int(count or 0) <= 1:
        raise ResourceConflict("system must keep at least one active system administrator")


async def _revoke_user_sessions(session: AsyncSession, user_id: UUID) -> int:
    revoked_at = utc_now()
    result = await session.execute(
        update(AuthSessionRecord)
        .where(
            AuthSessionRecord.user_id == user_id,
            AuthSessionRecord.revoked_at.is_(None),
        )
        .values(revoked_at=revoked_at)
    )
    return int(result.rowcount or 0)


async def update_user(
    session: AsyncSession,
    user_id: UUID,
    payload: UserUpdate,
    principal: Principal,
) -> UserRecord:
    require_system_admin(principal)
    user = await session.get(UserRecord, user_id)
    if user is None:
        raise ResourceNotFound("user not found")
    next_role = payload.system_role or SystemRole(user.system_role)
    next_status = payload.status or UserStatus(user.status)
    if user_id == principal.user_id and (
        next_role != SystemRole.SYSTEM_ADMIN or next_status != UserStatus.ACTIVE
    ):
        raise ResourceConflict("current administrator cannot demote or disable itself")
    await _ensure_admin_remains(
        session,
        user,
        next_role=next_role,
        next_status=next_status,
    )
    before = {
        "display_name": user.display_name,
        "system_role": user.system_role,
        "status": user.status,
    }
    if payload.display_name is not None:
        user.display_name = payload.display_name
    user.system_role = next_role.value
    user.status = next_status.value
    revoked_sessions = 0
    if user.status == UserStatus.DISABLED.value:
        revoked_sessions = await _revoke_user_sessions(session, user.id)
    session.add(
        AuditLogRecord(
            actor_id=principal.user_id,
            action="identity.user_updated",
            resource_type="user",
            resource_id=str(user.id),
            details={
                "before": before,
                "after": {
                    "display_name": user.display_name,
                    "system_role": user.system_role,
                    "status": user.status,
                },
                "revoked_sessions": revoked_sessions,
            },
        )
    )
    await session.commit()
    await session.refresh(user)
    return user


async def reset_user_password(
    session: AsyncSession,
    user_id: UUID,
    payload: UserPasswordReset,
    principal: Principal,
) -> UserRecord:
    require_system_admin(principal)
    user = await session.get(UserRecord, user_id)
    if user is None:
        raise ResourceNotFound("user not found")
    user.password_hash = hash_password(payload.password)
    revoked_sessions = await _revoke_user_sessions(session, user.id)
    session.add(
        AuditLogRecord(
            actor_id=principal.user_id,
            action="identity.password_reset",
            resource_type="user",
            resource_id=str(user.id),
            details={"revoked_sessions": revoked_sessions},
        )
    )
    await session.commit()
    await session.refresh(user)
    return user


async def list_managed_sessions(
    session: AsyncSession,
    principal: Principal,
    *,
    user_id: UUID | None,
    active_only: bool,
    limit: int,
    offset: int,
) -> tuple[tuple[tuple[AuthSessionRecord, UserRecord], ...], int]:
    require_system_admin(principal)
    conditions = []
    if user_id is not None:
        conditions.append(AuthSessionRecord.user_id == user_id)
    if active_only:
        conditions.extend(
            (
                AuthSessionRecord.revoked_at.is_(None),
                AuthSessionRecord.expires_at > utc_now(),
            )
        )
    total = await session.scalar(
        select(func.count()).select_from(AuthSessionRecord).where(*conditions)
    )
    rows = await session.execute(
        select(AuthSessionRecord, UserRecord)
        .join(UserRecord, UserRecord.id == AuthSessionRecord.user_id)
        .where(*conditions)
        .order_by(AuthSessionRecord.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return tuple(rows.all()), int(total or 0)


async def revoke_managed_session(
    session: AsyncSession,
    session_id: UUID,
    principal: Principal,
) -> tuple[AuthSessionRecord, UserRecord]:
    require_system_admin(principal)
    row = (
        await session.execute(
            select(AuthSessionRecord, UserRecord)
            .join(UserRecord, UserRecord.id == AuthSessionRecord.user_id)
            .where(AuthSessionRecord.id == session_id)
        )
    ).one_or_none()
    if row is None:
        raise ResourceNotFound("session not found")
    auth_session, user = row
    changed = auth_session.revoked_at is None
    if changed:
        auth_session.revoked_at = utc_now()
        session.add(
            AuditLogRecord(
                actor_id=principal.user_id,
                action="identity.session_revoked_by_admin",
                resource_type="auth_session",
                resource_id=str(auth_session.id),
                details={"user_id": str(user.id)},
            )
        )
        await session.commit()
        await session.refresh(auth_session)
    return auth_session, user


async def list_audit_logs(
    session: AsyncSession,
    principal: Principal,
    *,
    project_id: UUID | None,
    actor_id: UUID | None,
    action: str | None,
    resource_type: str | None,
    limit: int,
    offset: int,
) -> tuple[tuple[tuple[AuditLogRecord, UserRecord | None], ...], int]:
    require_system_admin(principal)
    conditions = []
    if project_id is not None:
        conditions.append(AuditLogRecord.project_id == project_id)
    if actor_id is not None:
        conditions.append(AuditLogRecord.actor_id == actor_id)
    if action:
        conditions.append(func.lower(AuditLogRecord.action).like(f"%{action.strip().lower()}%"))
    if resource_type:
        conditions.append(AuditLogRecord.resource_type == resource_type.strip())
    total = await session.scalar(
        select(func.count()).select_from(AuditLogRecord).where(*conditions)
    )
    rows = await session.execute(
        select(AuditLogRecord, UserRecord)
        .outerjoin(UserRecord, UserRecord.id == AuditLogRecord.actor_id)
        .where(*conditions)
        .order_by(AuditLogRecord.created_at.desc(), AuditLogRecord.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return tuple(rows.all()), int(total or 0)


async def upsert_project_member(
    session: AsyncSession,
    project_id: UUID,
    payload: ProjectMemberUpsert,
    principal: Principal,
) -> tuple[ProjectMemberRecord, UserRecord]:
    await authorize_project(session, principal, project_id, "member:manage")
    if await session.get(ProjectRecord, project_id) is None:
        raise ResourceNotFound("project not found")
    user = await session.get(UserRecord, payload.user_id)
    if user is None or user.status != "ACTIVE":
        raise ResourceNotFound("active user not found")
    member = await session.scalar(
        select(ProjectMemberRecord).where(
            ProjectMemberRecord.project_id == project_id,
            ProjectMemberRecord.user_id == payload.user_id,
        )
    )
    action = "project.member_role_changed"
    if member is None:
        member = ProjectMemberRecord(
            id=uuid4(),
            project_id=project_id,
            user_id=payload.user_id,
            role=payload.role.value,
            created_by=principal.user_id,
        )
        session.add(member)
        action = "project.member_added"
    else:
        if (
            member.role == ProjectRole.PROJECT_ADMIN.value
            and payload.role != ProjectRole.PROJECT_ADMIN
        ):
            admin_count = await session.scalar(
                select(func.count())
                .select_from(ProjectMemberRecord)
                .where(
                    ProjectMemberRecord.project_id == project_id,
                    ProjectMemberRecord.role == ProjectRole.PROJECT_ADMIN.value,
                )
            )
            if int(admin_count or 0) <= 1:
                raise ResourceConflict("project must keep at least one project administrator")
        member.role = payload.role.value
    session.add(
        AuditLogRecord(
            actor_id=principal.user_id,
            project_id=project_id,
            action=action,
            resource_type="project_member",
            resource_id=str(member.id),
            details={"user_id": str(user.id), "role": member.role},
        )
    )
    await session.commit()
    await session.refresh(member)
    return member, user


async def remove_project_member(
    session: AsyncSession,
    project_id: UUID,
    user_id: UUID,
    principal: Principal,
) -> None:
    await authorize_project(session, principal, project_id, "member:manage")
    member = await session.scalar(
        select(ProjectMemberRecord).where(
            ProjectMemberRecord.project_id == project_id,
            ProjectMemberRecord.user_id == user_id,
        )
    )
    if member is None:
        raise ResourceNotFound("project member not found")
    if member.role == ProjectRole.PROJECT_ADMIN.value:
        admin_count = await session.scalar(
            select(func.count())
            .select_from(ProjectMemberRecord)
            .where(
                ProjectMemberRecord.project_id == project_id,
                ProjectMemberRecord.role == ProjectRole.PROJECT_ADMIN.value,
            )
        )
        if int(admin_count or 0) <= 1:
            raise ResourceConflict("project must keep at least one project administrator")
    await session.delete(member)
    session.add(
        AuditLogRecord(
            actor_id=principal.user_id,
            project_id=project_id,
            action="project.member_removed",
            resource_type="project_member",
            resource_id=str(member.id),
            details={"user_id": str(user_id), "role": member.role},
        )
    )
    await session.commit()


async def list_project_member_candidates(
    session: AsyncSession,
    project_id: UUID,
    principal: Principal,
    *,
    query: str | None,
    limit: int,
) -> tuple[UserRecord, ...]:
    await authorize_project(session, principal, project_id, "member:manage")
    if await session.get(ProjectRecord, project_id) is None:
        raise ResourceNotFound("project not found")
    statement = select(UserRecord).where(UserRecord.status == "ACTIVE")
    if query:
        pattern = f"%{query.strip().lower()}%"
        statement = statement.where(
            or_(
                func.lower(UserRecord.username).like(pattern),
                func.lower(UserRecord.display_name).like(pattern),
            )
        )
    return tuple(await session.scalars(statement.order_by(UserRecord.username).limit(limit)))


async def list_project_members(
    session: AsyncSession,
    project_id: UUID,
    principal: Principal,
) -> tuple[tuple[ProjectMemberRecord, UserRecord], ...]:
    await authorize_project(session, principal, project_id, "project:read")
    rows = await session.execute(
        select(ProjectMemberRecord, UserRecord)
        .join(UserRecord, UserRecord.id == ProjectMemberRecord.user_id)
        .where(ProjectMemberRecord.project_id == project_id)
        .order_by(UserRecord.username)
    )
    return tuple(rows.all())
