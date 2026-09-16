"""Identity and project-membership HTTP schemas."""

from __future__ import annotations

from datetime import datetime
from email.utils import parseaddr
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator, model_validator

Username = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$"),
]
InitialPassword = Annotated[str, StringConstraints(min_length=6, max_length=256)]
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


class RunEmailStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    TIMED_OUT = "TIMED_OUT"
    INFRA_ERROR = "INFRA_ERROR"


MAX_RUN_EMAIL_RECIPIENTS = 5


def _validated_email(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 320 or "\r" in normalized or "\n" in normalized:
        raise ValueError("email address is invalid")
    display_name, address = parseaddr(normalized)
    local, separator, domain = address.rpartition("@")
    if display_name or address != normalized or not separator or not local or not domain:
        raise ValueError("email address is invalid")
    if any(character.isspace() for character in normalized) or "." not in domain:
        raise ValueError("email address is invalid")
    return f"{local}@{domain.casefold()}"


class BootstrapAdminRequest(IdentityModel):
    username: Username
    display_name: DisplayName
    password: Password


class UserCreate(IdentityModel):
    username: Username
    display_name: DisplayName
    password: InitialPassword
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
    password: InitialPassword


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


class RunEmailDeliveryResponse(IdentityModel):
    id: UUID
    event_type: str
    run_id: UUID | None
    destination_display: str
    status: str
    attempts: int
    last_error: str | None
    created_at: datetime
    delivered_at: datetime | None


class RunEmailPreferenceResponse(IdentityModel):
    email: str | None
    emails: tuple[str, ...]
    selected_emails: tuple[str, ...]
    enabled: bool
    statuses: tuple[RunEmailStatus, ...]
    smtp_configured: bool
    updated_at: datetime | None
    last_delivery: RunEmailDeliveryResponse | None = None


class RunEmailPreferenceUpdate(IdentityModel):
    email: str | None = None
    emails: tuple[str, ...] | None = None
    selected_emails: tuple[str, ...] | None = None
    enabled: bool = False
    statuses: tuple[RunEmailStatus, ...] = tuple(RunEmailStatus)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        return _validated_email(value) if value is not None else None

    @field_validator("emails")
    @classmethod
    def validate_emails(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        normalized = tuple(_validated_email(item) for item in value)
        if len(normalized) > MAX_RUN_EMAIL_RECIPIENTS:
            raise ValueError(f"at most {MAX_RUN_EMAIL_RECIPIENTS} recipient emails are allowed")
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise ValueError("recipient emails must be unique")
        return normalized

    @field_validator("selected_emails")
    @classmethod
    def validate_selected_emails(
        cls,
        value: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        normalized = tuple(_validated_email(item) for item in value)
        if len(normalized) > MAX_RUN_EMAIL_RECIPIENTS:
            raise ValueError(f"at most {MAX_RUN_EMAIL_RECIPIENTS} selected emails are allowed")
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise ValueError("selected emails must be unique")
        return normalized

    @field_validator("statuses")
    @classmethod
    def validate_statuses(
        cls,
        value: tuple[RunEmailStatus, ...],
    ) -> tuple[RunEmailStatus, ...]:
        if len(value) != len(set(value)):
            raise ValueError("notification statuses must be unique")
        return value

    @model_validator(mode="after")
    def require_enabled_destination(self) -> RunEmailPreferenceUpdate:
        recipients = self.emails
        if recipients is None:
            recipients = (self.email,) if self.email is not None else ()
        elif self.email is not None and (not recipients or recipients[0] != self.email):
            raise ValueError("legacy email must match the first recipient email")
        self.emails = recipients
        self.email = recipients[0] if recipients else None
        selected = self.selected_emails if self.selected_emails is not None else recipients
        recipient_keys = {item.casefold() for item in recipients}
        if any(item.casefold() not in recipient_keys for item in selected):
            raise ValueError("selected emails must be configured recipient emails")
        self.selected_emails = selected
        if self.enabled and not selected:
            raise ValueError(
                "at least one selected email is required when Run notifications are enabled"
            )
        if self.enabled and not self.statuses:
            raise ValueError("at least one Run status is required when notifications are enabled")
        return self


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
