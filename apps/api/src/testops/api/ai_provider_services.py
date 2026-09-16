"""Encrypted model-provider configuration and runtime gateway resolution."""

from __future__ import annotations

import base64
from hashlib import sha256
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .ai_gateway import AiGatewayConnection, AiGatewayError, OpenAiCompatibleGateway
from .config import Settings
from .persistence import AiModelProviderRecord, AuditLogRecord
from .schemas import AiModelOption, AiModelProviderCreate, AiModelProviderUpdate
from .services import InvalidRequest, ResourceConflict, ResourceNotFound

ENVIRONMENT_PROVIDER_KEY = "environment"


class AiProviderSecretBox:
    """Encrypt provider credentials without persisting the wrapping material."""

    def __init__(self, material: str) -> None:
        digest = sha256(b"testops-ai-provider-v1\x00" + material.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    @classmethod
    def from_settings(cls, settings: Settings) -> AiProviderSecretBox | None:
        material = settings.ai_provider_encryption_key or settings.ai_api_key
        return cls(material) if material else None

    def encrypt(self, secret: str) -> str:
        return "fernet:v1:" + self._fernet.encrypt(secret.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        prefix = "fernet:v1:"
        if not ciphertext.startswith(prefix):
            raise AiGatewayError("AI model credential uses an unsupported encrypted format")
        try:
            return self._fernet.decrypt(ciphertext.removeprefix(prefix).encode("ascii")).decode(
                "utf-8"
            )
        except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
            raise AiGatewayError("AI model credential cannot be decrypted") from exc


def transport_security(
    base_url: str | None,
) -> Literal["UNCONFIGURED", "HTTPS", "LOCAL_HTTP", "INSECURE_HTTP"]:
    if not base_url:
        return "UNCONFIGURED"
    parsed = urlsplit(base_url)
    if parsed.scheme == "https":
        return "HTTPS"
    if parsed.hostname in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}:
        return "LOCAL_HTTP"
    return "INSECURE_HTTP"


def _validate_provider_connection(
    *,
    settings: Settings,
    base_url: str,
    model: str,
    api_key: str | None = None,
) -> None:
    try:
        AiGatewayConnection(
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout_seconds=settings.ai_timeout_seconds,
            analysis_max_tokens=settings.ai_analysis_max_tokens,
            thinking_budget=settings.ai_thinking_budget,
            max_concurrency=settings.ai_max_concurrency,
            allow_insecure_http=settings.ai_allow_insecure_http,
        )
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc


def _audit(
    *,
    actor_id: UUID,
    action: str,
    provider: AiModelProviderRecord,
    details: dict[str, object] | None = None,
) -> AuditLogRecord:
    return AuditLogRecord(
        actor_id=actor_id,
        action=action,
        resource_type="ai_model_provider",
        resource_id=str(provider.id),
        project_id=None,
        details={
            "name": provider.name,
            "model": provider.model,
            "status": provider.status,
            "is_default": provider.is_default,
            **(details or {}),
        },
    )


async def list_ai_model_providers(session: AsyncSession) -> tuple[AiModelProviderRecord, ...]:
    records = await session.scalars(
        select(AiModelProviderRecord).order_by(
            AiModelProviderRecord.is_default.desc(),
            AiModelProviderRecord.name,
        )
    )
    return tuple(records)


async def get_ai_model_provider(
    session: AsyncSession,
    provider_id: UUID,
    *,
    active_only: bool = False,
) -> AiModelProviderRecord:
    statement = select(AiModelProviderRecord).where(AiModelProviderRecord.id == provider_id)
    if active_only:
        statement = statement.where(AiModelProviderRecord.status == "ACTIVE")
    record = await session.scalar(statement)
    if record is None:
        raise ResourceNotFound("AI model provider not found")
    return record


async def create_ai_model_provider(
    session: AsyncSession,
    payload: AiModelProviderCreate,
    actor_id: UUID,
    settings: Settings,
) -> AiModelProviderRecord:
    _validate_provider_connection(
        settings=settings,
        base_url=payload.base_url,
        model=payload.model,
        api_key=payload.api_key,
    )
    secret_box = AiProviderSecretBox.from_settings(settings)
    if payload.api_key and secret_box is None:
        raise InvalidRequest(
            "AI credential encryption is unavailable; configure AI_PROVIDER_ENCRYPTION_KEY"
        )
    if payload.is_default:
        await session.execute(update(AiModelProviderRecord).values(is_default=False))
    record = AiModelProviderRecord(
        id=uuid4(),
        name=payload.name,
        base_url=payload.base_url,
        model=payload.model,
        api_key_ciphertext=(secret_box.encrypt(payload.api_key) if payload.api_key else None),
        status=payload.status,
        is_default=payload.is_default,
        created_by=actor_id,
        updated_by=actor_id,
    )
    session.add(record)
    session.add(_audit(actor_id=actor_id, action="ai.model_provider_created", provider=record))
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ResourceConflict("AI model provider name already exists") from exc
    await session.refresh(record)
    return record


async def update_ai_model_provider(
    session: AsyncSession,
    provider_id: UUID,
    payload: AiModelProviderUpdate,
    actor_id: UUID,
    settings: Settings,
) -> AiModelProviderRecord:
    record = await session.scalar(
        select(AiModelProviderRecord)
        .where(AiModelProviderRecord.id == provider_id)
        .with_for_update()
    )
    if record is None:
        raise ResourceNotFound("AI model provider not found")
    next_base_url = payload.base_url if payload.base_url is not None else record.base_url
    next_model = payload.model if payload.model is not None else record.model
    next_status = payload.status if payload.status is not None else record.status
    next_default = payload.is_default if payload.is_default is not None else record.is_default
    if payload.is_default is True and next_status != "ACTIVE":
        raise InvalidRequest("the default AI model provider must be active")
    if next_status != "ACTIVE":
        next_default = False
    _validate_provider_connection(
        settings=settings,
        base_url=next_base_url,
        model=next_model,
        api_key=payload.api_key,
    )
    secret_box = AiProviderSecretBox.from_settings(settings)
    if payload.api_key is not None:
        if secret_box is None:
            raise InvalidRequest(
                "AI credential encryption is unavailable; configure AI_PROVIDER_ENCRYPTION_KEY"
            )
        record.api_key_ciphertext = secret_box.encrypt(payload.api_key)
    elif payload.clear_api_key:
        record.api_key_ciphertext = None
    if payload.name is not None:
        record.name = payload.name
    record.base_url = next_base_url
    record.model = next_model
    record.status = next_status
    if next_default:
        await session.execute(
            update(AiModelProviderRecord)
            .where(AiModelProviderRecord.id != record.id)
            .values(is_default=False)
        )
    record.is_default = next_default and next_status == "ACTIVE"
    record.updated_by = actor_id
    session.add(
        _audit(
            actor_id=actor_id,
            action="ai.model_provider_updated",
            provider=record,
            details={
                "credential_updated": payload.api_key is not None,
                "credential_cleared": payload.clear_api_key,
            },
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ResourceConflict("AI model provider name already exists") from exc
    await session.refresh(record)
    return record


async def delete_ai_model_provider(
    session: AsyncSession,
    provider_id: UUID,
    actor_id: UUID,
) -> None:
    record = await get_ai_model_provider(session, provider_id)
    session.add(_audit(actor_id=actor_id, action="ai.model_provider_deleted", provider=record))
    await session.delete(record)
    await session.commit()


class AiGatewayManager:
    """Resolve the environment gateway or a persisted custom provider per request."""

    def __init__(
        self,
        settings: Settings,
        environment_gateway: OpenAiCompatibleGateway | None,
    ) -> None:
        self._settings = settings
        self._environment_gateway = environment_gateway
        self._secret_box = AiProviderSecretBox.from_settings(settings)
        self._cache: dict[UUID, tuple[str, OpenAiCompatibleGateway]] = {}

    async def model_options(self, session: AsyncSession) -> tuple[AiModelOption, ...]:
        records = tuple(
            await session.scalars(
                select(AiModelProviderRecord)
                .where(AiModelProviderRecord.status == "ACTIVE")
                .order_by(AiModelProviderRecord.is_default.desc(), AiModelProviderRecord.name)
            )
        )
        custom_default = next((record for record in records if record.is_default), None)
        options: list[AiModelOption] = []
        if self._environment_gateway is not None and self._settings.ai_model is not None:
            options.append(
                AiModelOption(
                    key=ENVIRONMENT_PROVIDER_KEY,
                    name="环境变量模型",
                    model=self._settings.ai_model,
                    source="ENVIRONMENT",
                    is_default=custom_default is None,
                )
            )
        options.extend(
            AiModelOption(
                key=str(record.id),
                name=record.name,
                model=record.model,
                source="CUSTOM",
                is_default=record.is_default,
            )
            for record in records
        )
        return tuple(options)

    async def resolve(
        self,
        session: AsyncSession,
        provider_key: str | None = None,
    ) -> OpenAiCompatibleGateway:
        if provider_key == ENVIRONMENT_PROVIDER_KEY:
            if self._environment_gateway is None:
                raise AiGatewayError("The environment AI model is not configured")
            return self._environment_gateway
        record: AiModelProviderRecord | None = None
        if provider_key:
            try:
                provider_id = UUID(provider_key)
            except ValueError as exc:
                raise AiGatewayError("Unknown AI model provider") from exc
            try:
                record = await get_ai_model_provider(session, provider_id, active_only=True)
            except ResourceNotFound as exc:
                raise AiGatewayError("AI model provider is unavailable") from exc
        else:
            record = await session.scalar(
                select(AiModelProviderRecord)
                .where(
                    AiModelProviderRecord.status == "ACTIVE",
                    AiModelProviderRecord.is_default.is_(True),
                )
                .order_by(AiModelProviderRecord.updated_at.desc())
            )
            if record is None and self._environment_gateway is not None:
                return self._environment_gateway
            if record is None:
                record = await session.scalar(
                    select(AiModelProviderRecord)
                    .where(AiModelProviderRecord.status == "ACTIVE")
                    .order_by(AiModelProviderRecord.name)
                )
        if record is None:
            raise AiGatewayError("AI integration is not configured")
        return self._gateway_for(record)

    def gateway_for_record(self, record: AiModelProviderRecord) -> OpenAiCompatibleGateway:
        if record.status != "ACTIVE":
            raise AiGatewayError("AI model provider is disabled")
        return self._gateway_for(record)

    def _gateway_for(self, record: AiModelProviderRecord) -> OpenAiCompatibleGateway:
        fingerprint = sha256(
            "\x00".join(
                [
                    record.base_url,
                    record.model,
                    record.api_key_ciphertext or "",
                    str(self._settings.ai_timeout_seconds),
                    str(self._settings.ai_analysis_max_tokens),
                    str(self._settings.ai_thinking_budget),
                    str(self._settings.ai_max_concurrency),
                ]
            ).encode("utf-8")
        ).hexdigest()
        cached = self._cache.get(record.id)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        api_key = None
        if record.api_key_ciphertext:
            if self._secret_box is None:
                raise AiGatewayError("AI model credential encryption is not configured")
            api_key = self._secret_box.decrypt(record.api_key_ciphertext)
        try:
            gateway = OpenAiCompatibleGateway(
                AiGatewayConnection(
                    base_url=record.base_url,
                    model=record.model,
                    api_key=api_key,
                    timeout_seconds=self._settings.ai_timeout_seconds,
                    analysis_max_tokens=self._settings.ai_analysis_max_tokens,
                    thinking_budget=self._settings.ai_thinking_budget,
                    max_concurrency=self._settings.ai_max_concurrency,
                    allow_insecure_http=self._settings.ai_allow_insecure_http,
                )
            )
        except ValueError as exc:
            raise AiGatewayError(str(exc)) from exc
        self._cache[record.id] = (fingerprint, gateway)
        return gateway
