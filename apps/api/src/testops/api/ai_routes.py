"""Authenticated model-configuration and connectivity endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from .ai_analysis import (
    CHAT_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    AiAnalysisError,
    build_project_chat_evidence,
    build_run_evidence,
    redact_text,
    sanitize_and_validate_analysis,
    sanitize_and_validate_chat,
)
from .ai_chat_services import (
    AiConversationNotFound,
    delete_ai_conversation,
    get_ai_chat_question,
    get_ai_conversation,
    list_ai_conversation_messages,
    list_ai_conversations,
    load_ai_chat_context,
    mark_ai_chat_question_failed,
    save_ai_chat_answer,
    save_ai_chat_exchange,
    save_ai_chat_question,
)
from .ai_gateway import AiGatewayError, OpenAiCompatibleGateway
from .ai_provider_services import (
    AiGatewayManager,
    create_ai_model_provider,
    delete_ai_model_provider,
    get_ai_model_provider,
    list_ai_model_providers,
    transport_security,
    update_ai_model_provider,
)
from .database import get_session
from .identity import CurrentPrincipal, authorize_project, require_system_admin
from .persistence import AiChatConversationRecord, AiChatMessageRecord, AiModelProviderRecord
from .schemas import (
    AiChatQuestionRequest,
    AiChatQuestionResponse,
    AiConversationDetailResponse,
    AiConversationListResponse,
    AiConversationMessageResponse,
    AiConversationSummary,
    AiModelCatalogResponse,
    AiModelOption,
    AiModelProviderCreate,
    AiModelProviderProbeResponse,
    AiModelProviderResponse,
    AiModelProviderUpdate,
    AiProbeResponse,
    AiProjectChatContent,
    AiProjectChatRequest,
    AiProjectChatResponse,
    AiRunAnalysisContent,
    AiRunAnalysisResponse,
    AiStatusResponse,
)
from .services import (
    get_run,
    list_run_artifacts,
    list_run_cases,
    list_run_events,
    list_runs,
)

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])
Session = Annotated[AsyncSession, Depends(get_session)]
LOGGER = logging.getLogger(__name__)

_RETRYABLE_AI_GATEWAY_ERRORS = frozenset(
    {
        "AI model endpoint is unreachable",
        "AI model request timed out",
        "AI model returned HTTP 429",
        "AI model returned an invalid structured analysis",
    }
)
_RETRYABLE_AI_ANALYSIS_ERRORS = frozenset(
    {
        "AI model cited evidence that was not supplied",
        "AI model returned an incomplete chat answer",
        "AI model response was token-truncated",
    }
)


def _retryable_gateway_error(error: AiGatewayError) -> bool:
    detail = str(error)
    return detail in _RETRYABLE_AI_GATEWAY_ERRORS or detail.startswith(
        "AI model returned HTTP 5"
    )


def _conversation_summary(record: AiChatConversationRecord) -> AiConversationSummary:
    return AiConversationSummary.model_validate(record)


def _conversation_message(record: AiChatMessageRecord) -> AiConversationMessageResponse:
    metadata = record.response_metadata
    return AiConversationMessageResponse(
        id=record.id,
        role=record.role,
        content=record.content,
        sections=record.sections,
        evidence_refs=record.evidence_refs,
        suggested_questions=record.suggested_questions,
        model=metadata.get("model"),
        latency_ms=metadata.get("latency_ms"),
        generated_at=metadata.get("generated_at"),
        evidence_truncated=bool(metadata.get("evidence_truncated", False)),
        status=metadata.get("status"),
        created_at=record.created_at,
    )


def _provider_response(provider: AiModelProviderRecord) -> AiModelProviderResponse:
    return AiModelProviderResponse(
        id=provider.id,
        name=provider.name,
        provider_type=provider.provider_type,
        base_url=provider.base_url,
        model=provider.model,
        status=provider.status,
        is_default=provider.is_default,
        api_key_configured=provider.api_key_ciphertext is not None,
        transport_security=transport_security(provider.base_url),
        created_at=provider.created_at,
        updated_at=provider.updated_at,
    )


async def _resolve_gateway(
    request: Request,
    session: AsyncSession,
    provider_key: str | None = None,
) -> OpenAiCompatibleGateway:
    manager: AiGatewayManager | None = getattr(
        request.app.state,
        "ai_gateway_manager",
        None,
    )
    if manager is not None:
        try:
            return await manager.resolve(session, provider_key)
        except AiGatewayError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    gateway: OpenAiCompatibleGateway | None = getattr(
        request.app.state,
        "ai_gateway",
        None,
    )
    if gateway is None:
        raise HTTPException(status_code=503, detail="AI integration is not configured")
    return gateway


@router.get("/status", response_model=AiStatusResponse)
async def get_ai_status(
    request: Request,
    principal: CurrentPrincipal,
) -> AiStatusResponse:
    require_system_admin(principal)
    settings = request.app.state.settings
    return AiStatusResponse(
        enabled=settings.ai_enabled,
        configured=request.app.state.ai_gateway is not None,
        model=settings.ai_model,
        transport_security=transport_security(settings.ai_base_url),
    )


@router.get("/models", response_model=AiModelCatalogResponse)
async def get_ai_models(
    request: Request,
    _principal: CurrentPrincipal,
    session: Session,
) -> AiModelCatalogResponse:
    manager: AiGatewayManager | None = getattr(request.app.state, "ai_gateway_manager", None)
    if manager is None:
        settings = request.app.state.settings
        items = ()
        if request.app.state.ai_gateway is not None and settings.ai_model:
            items = (
                AiModelOption(
                    key="environment",
                    name="环境变量模型",
                    model=settings.ai_model,
                    source="ENVIRONMENT",
                    is_default=True,
                ),
            )
        return AiModelCatalogResponse(items=items)
    return AiModelCatalogResponse(items=await manager.model_options(session))


@router.get("/providers", response_model=tuple[AiModelProviderResponse, ...])
async def get_ai_model_providers(
    principal: CurrentPrincipal,
    session: Session,
) -> tuple[AiModelProviderResponse, ...]:
    require_system_admin(principal)
    return tuple(
        _provider_response(record) for record in await list_ai_model_providers(session)
    )


@router.post("/providers", response_model=AiModelProviderResponse, status_code=201)
async def post_ai_model_provider(
    payload: AiModelProviderCreate,
    request: Request,
    principal: CurrentPrincipal,
    session: Session,
) -> AiModelProviderResponse:
    require_system_admin(principal)
    record = await create_ai_model_provider(
        session,
        payload,
        principal.user_id,
        request.app.state.settings,
    )
    return _provider_response(record)


@router.patch("/providers/{provider_id}", response_model=AiModelProviderResponse)
async def patch_ai_model_provider(
    provider_id: UUID,
    payload: AiModelProviderUpdate,
    request: Request,
    principal: CurrentPrincipal,
    session: Session,
) -> AiModelProviderResponse:
    require_system_admin(principal)
    record = await update_ai_model_provider(
        session,
        provider_id,
        payload,
        principal.user_id,
        request.app.state.settings,
    )
    return _provider_response(record)


@router.delete("/providers/{provider_id}", status_code=204)
async def remove_ai_model_provider(
    provider_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> Response:
    require_system_admin(principal)
    await delete_ai_model_provider(session, provider_id, principal.user_id)
    return Response(status_code=204)


@router.post(
    "/providers/{provider_id}/probe",
    response_model=AiModelProviderProbeResponse,
)
async def post_ai_model_provider_probe(
    provider_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    session: Session,
) -> AiModelProviderProbeResponse:
    require_system_admin(principal)
    record = await get_ai_model_provider(session, provider_id)
    manager: AiGatewayManager = request.app.state.ai_gateway_manager
    try:
        result = await manager.gateway_for_record(record).probe()
    except AiGatewayError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return AiModelProviderProbeResponse(
        status="READY",
        provider_id=record.id,
        model=result.model,
        latency_ms=result.latency_ms,
    )


@router.post("/probe", response_model=AiProbeResponse)
async def post_ai_probe(
    request: Request,
    principal: CurrentPrincipal,
) -> AiProbeResponse:
    require_system_admin(principal)
    gateway: OpenAiCompatibleGateway | None = request.app.state.ai_gateway
    if gateway is None:
        raise HTTPException(status_code=503, detail="Environment AI integration is not configured")
    try:
        result = await gateway.probe()
    except AiGatewayError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return AiProbeResponse(status="READY", model=result.model, latency_ms=result.latency_ms)


@router.post("/runs/{run_id}/analysis", response_model=AiRunAnalysisResponse)
async def post_run_failure_analysis(
    run_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    session: Session,
) -> AiRunAnalysisResponse:
    """Analyze one terminal abnormal Run without persisting or changing its result."""

    run = await get_run(session, run_id)
    await authorize_project(session, principal, run.project_id, "run:read")
    if run.status not in {"FAILED", "CANCELED", "TIMED_OUT", "INFRA_ERROR"}:
        raise HTTPException(
            status_code=409,
            detail="AI failure analysis is available only for terminal abnormal runs",
        )
    gateway = await _resolve_gateway(request, session)
    cases = await list_run_cases(session, run_id)
    events = await list_run_events(session, run_id, limit=500)
    artifacts = await list_run_artifacts(session, run_id)
    settings = request.app.state.settings
    try:
        evidence = build_run_evidence(
            run,
            cases,
            events,
            artifacts,
            max_chars=settings.ai_max_evidence_chars,
        )
        completion = await gateway.complete_json(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=(
                "请分析以下 TestOps 运行证据，并严格输出约定的 JSON。\n"
                f"<testops_evidence>{evidence.serialized}</testops_evidence>"
            ),
            response_model=AiRunAnalysisContent,
        )
        analysis = sanitize_and_validate_analysis(
            completion.content,
            evidence.evidence_refs,
        )
    except (AiGatewayError, AiAnalysisError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return AiRunAnalysisResponse(
        **analysis.model_dump(),
        run_id=run.id,
        run_status=run.status,
        model=completion.model,
        generated_at=datetime.now(UTC),
        latency_ms=completion.latency_ms,
        analyzed_case_count=evidence.analyzed_case_count,
        evidence_truncated=evidence.truncated,
    )


async def _complete_prepared_project_ai_chat(
    *,
    request: Request,
    principal: object,
    project_id: UUID,
    run_id: UUID | None,
    conversation_id: UUID,
    question_message_id: UUID,
    model_provider_key: str | None,
) -> None:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        try:
            await post_project_ai_chat(
                project_id,
                AiProjectChatRequest(
                    message="prepared question",
                    run_id=run_id,
                    conversation_id=conversation_id,
                    question_message_id=question_message_id,
                    model_provider_key=model_provider_key,
                ),
                request,
                principal,
                session,
            )
        except Exception:
            await session.rollback()
            LOGGER.exception(
                "Background AI chat completion failed for conversation=%s question=%s",
                conversation_id,
                question_message_id,
            )
            try:
                await mark_ai_chat_question_failed(
                    session,
                    conversation_id=conversation_id,
                    question_message_id=question_message_id,
                )
            except Exception:
                LOGGER.exception(
                    "Failed to mark background AI question as failed: question=%s",
                    question_message_id,
                )


def _schedule_project_ai_chat_completion(
    *,
    request: Request,
    principal: object,
    project_id: UUID,
    run_id: UUID | None,
    conversation_id: UUID,
    question_message_id: UUID,
    model_provider_key: str | None,
) -> None:
    task = asyncio.create_task(
        _complete_prepared_project_ai_chat(
            request=request,
            principal=principal,
            project_id=project_id,
            run_id=run_id,
            conversation_id=conversation_id,
            question_message_id=question_message_id,
            model_provider_key=model_provider_key,
        )
    )
    tasks: set[asyncio.Task[None]] = getattr(request.app.state, "ai_chat_tasks", set())
    request.app.state.ai_chat_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)


@router.post(
    "/projects/{project_id}/chat/questions",
    response_model=AiChatQuestionResponse,
    status_code=202,
)
async def post_project_ai_chat_question(
    project_id: UUID,
    payload: AiChatQuestionRequest,
    request: Request,
    principal: CurrentPrincipal,
    session: Session,
) -> AiChatQuestionResponse:
    """Persist a user question before the slow model request begins."""

    await authorize_project(session, principal, project_id, "run:read")
    if payload.run_id is not None:
        run = await get_run(session, payload.run_id)
        if run.project_id != project_id:
            raise HTTPException(status_code=404, detail="Run not found in project")
    try:
        conversation_record, _ = await load_ai_chat_context(
            session,
            conversation_id=payload.conversation_id,
            user_id=principal.user_id,
            project_id=project_id,
            run_id=payload.run_id,
        )
        conversation_record, question_record = await save_ai_chat_question(
            session,
            conversation=conversation_record,
            user_id=principal.user_id,
            project_id=project_id,
            run_id=payload.run_id,
            question=redact_text(payload.message),
        )
    except AiConversationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _schedule_project_ai_chat_completion(
        request=request,
        principal=principal,
        project_id=project_id,
        run_id=payload.run_id,
        conversation_id=conversation_record.id,
        question_message_id=question_record.id,
        model_provider_key=payload.model_provider_key,
    )
    return AiChatQuestionResponse(
        conversation_id=conversation_record.id,
        message_id=question_record.id,
        created_at=question_record.created_at,
    )


@router.post(
    "/projects/{project_id}/chat",
    response_model=AiProjectChatResponse,
)
async def post_project_ai_chat(
    project_id: UUID,
    payload: AiProjectChatRequest,
    request: Request,
    principal: CurrentPrincipal,
    session: Session,
) -> AiProjectChatResponse:
    """Answer a bounded project/Run question and persist its scoped conversation."""

    await authorize_project(session, principal, project_id, "run:read")
    recent_runs, _ = await list_runs(
        session,
        project_id,
        limit=10,
        offset=0,
    )
    settings = request.app.state.settings
    selected_run = None
    if payload.run_id is not None:
        run = await get_run(session, payload.run_id)
        if run.project_id != project_id:
            raise HTTPException(status_code=404, detail="Run not found in project")
        cases = await list_run_cases(session, run.id)
        events = await list_run_events(session, run.id, limit=500)
        artifacts = await list_run_artifacts(session, run.id)
        try:
            selected_run = build_run_evidence(
                run,
                cases,
                events,
                artifacts,
                max_chars=max(4_000, settings.ai_max_evidence_chars - 4_000),
            )
        except AiAnalysisError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    try:
        conversation_record, persisted_history = await load_ai_chat_context(
            session,
            conversation_id=payload.conversation_id,
            user_id=principal.user_id,
            project_id=project_id,
            run_id=payload.run_id,
            exclude_message_id=payload.question_message_id,
        )
        prepared_question = None
        if payload.question_message_id is not None:
            if conversation_record is None:
                raise HTTPException(
                    status_code=400,
                    detail="conversation_id is required for a prepared AI question",
                )
            prepared_question = await get_ai_chat_question(
                session,
                conversation_id=conversation_record.id,
                question_message_id=payload.question_message_id,
            )
    except AiConversationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        gateway = await _resolve_gateway(request, session, payload.model_provider_key)
        safe_question = (
            prepared_question.content
            if prepared_question is not None
            else redact_text(payload.message)
        )
        history = persisted_history if conversation_record is not None else payload.history
        knowledge_query = "\n".join(
            [*(redact_text(turn.content) for turn in history[-4:]), safe_question]
        )
        evidence = build_project_chat_evidence(
            project_id,
            recent_runs,
            selected_run=selected_run,
            knowledge_query=knowledge_query,
            max_chars=settings.ai_max_evidence_chars,
        )
        conversation = {
            "history": [
                {"role": turn.role, "content": redact_text(turn.content)} for turn in history
            ],
            "question": safe_question,
        }
        user_prompt = (
            "请根据证据回答当前问题。\n"
            f"<conversation>{json.dumps(conversation, ensure_ascii=False)}</conversation>\n"
            f"<testops_evidence>{evidence.serialized}</testops_evidence>"
        )
        completion = None
        content = None
        latency_ms = 0
        for attempt in range(2):
            retry_instruction = (
                "\nThe previous response did not match the required JSON Schema, was incomplete, "
                "was token-truncated, or cited an evidence_ref that was not supplied. Regenerate "
                "the whole JSON object from the beginning, respect every field limit, and finish "
                "every sentence. Use only evidence_ref values explicitly present in "
                "<testops_evidence>; return an empty evidence_refs array when none apply."
                if attempt
                else ""
            )
            try:
                completion = await gateway.complete_json(
                    system_prompt=CHAT_SYSTEM_PROMPT + retry_instruction,
                    user_prompt=user_prompt,
                    response_model=AiProjectChatContent,
                )
            except AiGatewayError as exc:
                if attempt == 0 and _retryable_gateway_error(exc):
                    if str(exc) in {
                        "AI model endpoint is unreachable",
                        "AI model request timed out",
                    }:
                        await asyncio.sleep(1)
                    continue
                raise
            latency_ms += completion.latency_ms
            try:
                if (completion.finish_reason or "").lower() in {"length", "max_tokens"}:
                    raise AiAnalysisError("AI model response was token-truncated")
                content = sanitize_and_validate_chat(
                    completion.content,
                    evidence.evidence_refs,
                )
            except AiAnalysisError as exc:
                if attempt == 0 and str(exc) in _RETRYABLE_AI_ANALYSIS_ERRORS:
                    continue
                raise
            break
        if completion is None or content is None:
            raise AiAnalysisError("AI model could not provide a complete chat answer")
    except HTTPException:
        if prepared_question is not None and conversation_record is not None:
            await mark_ai_chat_question_failed(
                session,
                conversation_id=conversation_record.id,
                question_message_id=prepared_question.id,
            )
        raise
    except AiGatewayError as exc:
        if prepared_question is not None and conversation_record is not None:
            await mark_ai_chat_question_failed(
                session,
                conversation_id=conversation_record.id,
                question_message_id=prepared_question.id,
            )
        detail = (
            "AI 返回格式不完整，平台自动重试后仍无法解析，请重新提问。"
            if str(exc) == "AI model returned an invalid structured analysis"
            else str(exc)
        )
        raise HTTPException(status_code=502, detail=detail) from exc
    except AiAnalysisError as exc:
        if prepared_question is not None and conversation_record is not None:
            await mark_ai_chat_question_failed(
                session,
                conversation_id=conversation_record.id,
                question_message_id=prepared_question.id,
            )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    generated_at = datetime.now(UTC)
    try:
        if prepared_question is not None and conversation_record is not None:
            conversation_record = await save_ai_chat_answer(
                session,
                conversation_id=conversation_record.id,
                question_message_id=prepared_question.id,
                user_id=principal.user_id,
                project_id=project_id,
                run_id=payload.run_id,
                content=content,
                model=completion.model,
                latency_ms=latency_ms,
                generated_at=generated_at,
                evidence_truncated=evidence.truncated,
            )
        else:
            conversation_record = await save_ai_chat_exchange(
                session,
                conversation=conversation_record,
                user_id=principal.user_id,
                project_id=project_id,
                run_id=payload.run_id,
                question=safe_question,
                content=content,
                model=completion.model,
                latency_ms=latency_ms,
                generated_at=generated_at,
                evidence_truncated=evidence.truncated,
            )
    except AiConversationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return AiProjectChatResponse(
        **content.model_dump(),
        conversation_id=conversation_record.id,
        project_id=project_id,
        run_id=payload.run_id,
        model=completion.model,
        generated_at=generated_at,
        latency_ms=latency_ms,
        evidence_truncated=evidence.truncated,
    )


@router.get(
    "/projects/{project_id}/conversations",
    response_model=AiConversationListResponse,
)
async def get_project_ai_conversations(
    project_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
    run_id: UUID | None = None,
) -> AiConversationListResponse:
    await authorize_project(session, principal, project_id, "run:read")
    if run_id is not None:
        run = await get_run(session, run_id)
        if run.project_id != project_id:
            raise HTTPException(status_code=404, detail="Run not found in project")
    records = await list_ai_conversations(
        session,
        user_id=principal.user_id,
        project_id=project_id,
        run_id=run_id,
    )
    return AiConversationListResponse(
        items=tuple(_conversation_summary(record) for record in records)
    )


@router.get(
    "/projects/{project_id}/conversations/{conversation_id}",
    response_model=AiConversationDetailResponse,
)
async def get_project_ai_conversation(
    project_id: UUID,
    conversation_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
    run_id: UUID | None = None,
) -> AiConversationDetailResponse:
    await authorize_project(session, principal, project_id, "run:read")
    try:
        conversation = await get_ai_conversation(
            session,
            conversation_id=conversation_id,
            user_id=principal.user_id,
            project_id=project_id,
            run_id=run_id,
        )
    except AiConversationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    messages = await list_ai_conversation_messages(session, conversation.id)
    return AiConversationDetailResponse(
        conversation=_conversation_summary(conversation),
        messages=tuple(_conversation_message(record) for record in messages),
    )


@router.delete(
    "/projects/{project_id}/conversations/{conversation_id}",
    status_code=204,
)
async def delete_project_ai_conversation(
    project_id: UUID,
    conversation_id: UUID,
    principal: CurrentPrincipal,
    session: Session,
) -> Response:
    await authorize_project(session, principal, project_id, "run:read")
    try:
        await delete_ai_conversation(
            session,
            conversation_id=conversation_id,
            user_id=principal.user_id,
            project_id=project_id,
        )
    except AiConversationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=204)
