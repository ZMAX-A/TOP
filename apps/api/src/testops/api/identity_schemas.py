"""Identity and project-membership HTTP schemas."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

Username = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$"),
]
Password = Annotated[str, StringConstraints(min_length=12, max_length=256)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class IdentityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class SystemRole(StrEnum):
    USER = "USER"
    SYSTEM_ADMIN = "SYSTEM_ADMIN"


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class ProjectRole(StrEnum):
    VIEWER = "VIEWER"
    TESTER = "TESTER"
    REVIEWER = "REVIEWER"
    PROJECT_ADMIN = "PROJECT_ADMIN"


class BootstrapAdminRequest(IdentityModel):
    username: Username
    display_name: DisplayName
    password: Password


class UserCreate(IdentityModel):
    username: Username
    display_name: DisplayName
    password: Password
    system_role: SystemRole = SystemRole.USER


class UserUpdate(IdentityModel):
    display_name: DisplayName | None = None
    system_role: SystemRole | None = None
    status: UserStatus | None = None

    @model_validator(mode="after")
    def require_change(self) -> UserUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one user field must be provided")
        return self


class UserPasswordReset(IdentityModel):
    password: Password


class LoginRequest(IdentityModel):
    username: Username
    password: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class UserResponse(IdentityModel):
    id: UUID
    username: str
    display_name: str
    system_role: SystemRole
    status: str
    created_at: datetime


class UserPageResponse(IdentityModel):
    items: tuple[UserResponse, ...]
    total: int
    offset: int
    limit: int


class SessionResponse(IdentityModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: UserResponse


class ManagedSessionResponse(IdentityModel):
    id: UUID
    user_id: UUID
    username: str
    display_name: str
    expires_at: datetime
    created_at: datetime
    revoked_at: datetime | None
    active: bool


class ManagedSessionPageResponse(IdentityModel):
    items: tuple[ManagedSessionResponse, ...]
    total: int
    offset: int
    limit: int


class AuditLogResponse(IdentityModel):
    id: UUID
    project_id: UUID | None
    actor_id: UUID
    actor_username: str | None
    actor_display_name: str | None
    action: str
    resource_type: str
    resource_id: str
    details: dict[str, object]
    created_at: datetime


class AuditLogPageResponse(IdentityModel):
    items: tuple[AuditLogResponse, ...]
    total: int
    offset: int
    limit: int


class ProjectMemberUpsert(IdentityModel):
    user_id: UUID
    role: ProjectRole


class ProjectMemberResponse(IdentityModel):
    id: UUID
    project_id: UUID
    user_id: UUID
    username: str
    display_name: str
    role: ProjectRole
    created_at: datetime
    updated_at: datetime


class ProjectMemberCandidateResponse(IdentityModel):
    id: UUID
    username: str
    display_name: str
