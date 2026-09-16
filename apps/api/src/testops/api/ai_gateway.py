"""Bounded OpenAI-compatible gateway for the locally managed AI model."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from .config import Settings

LOGGER = logging.getLogger(__name__)


class AiGatewayError(RuntimeError):
    """The configured model endpoint could not provide a valid response."""


@dataclass(frozen=True, slots=True)
class AiGatewayConnection:
    """Runtime-only connection data for one OpenAI-compatible model."""

    base_url: str
    model: str
    api_key: str | None = field(default=None, repr=False)
    timeout_seconds: float = 60.0
    analysis_max_tokens: int = 16_384
    thinking_budget: int = 12_288
    max_concurrency: int = 1
    allow_insecure_http: bool = False

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "AI model Base URL must be HTTP(S) without credentials, query or fragment"
            )
        local_hosts = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
        if (
            parsed.scheme == "http"
            and parsed.hostname not in local_hosts
            and not self.allow_insecure_http
        ):
            raise ValueError(
                "AI model Base URL uses non-local HTTP; enable the trusted HTTP option first"
            )
        if not self.model.strip():
            raise ValueError("AI model name cannot be empty")


@dataclass(frozen=True, slots=True)
class AiProbeResult:
    model: str
    latency_ms: int


@dataclass(frozen=True, slots=True)
class AiStructuredResult[StructuredModel: BaseModel]:
    content: StructuredModel
    model: str
    latency_ms: int
    finish_reason: str | None = None


class OpenAiCompatibleGateway:
    """Small, bounded gateway that never exposes upstream response bodies."""

    def __init__(
        self,
        settings: Settings | AiGatewayConnection,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if isinstance(settings, Settings):
            if not settings.ai_enabled:
                raise ValueError("AI integration is disabled")
            assert settings.ai_base_url is not None
            assert settings.ai_api_key is not None
            assert settings.ai_model is not None
            connection = AiGatewayConnection(
                base_url=settings.ai_base_url,
                api_key=settings.ai_api_key,
                model=settings.ai_model,
                timeout_seconds=settings.ai_timeout_seconds,
                analysis_max_tokens=settings.ai_analysis_max_tokens,
                thinking_budget=settings.ai_thinking_budget,
                max_concurrency=settings.ai_max_concurrency,
                allow_insecure_http=settings.ai_allow_insecure_http,
            )
        else:
            connection = settings
        normalized_url = connection.base_url.rstrip("/")
        self._url = (
            normalized_url
            if normalized_url.endswith("/chat/completions")
            else normalized_url + "/chat/completions"
        )
        self._api_key = connection.api_key
        self._model = connection.model
        self._timeout_seconds = connection.timeout_seconds
        self._analysis_max_tokens = connection.analysis_max_tokens
        self._thinking_budget = connection.thinking_budget
        self._transport = transport
        self._semaphore = asyncio.Semaphore(connection.max_concurrency)

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAiCompatibleGateway | None:
        return cls(settings) if settings.ai_enabled else None

    async def _request(self, payload: dict[str, object]) -> tuple[str, int, str | None]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        started = time.perf_counter()
        try:
            async with self._semaphore:
                async with httpx.AsyncClient(
                    timeout=self._timeout_seconds,
                    transport=self._transport,
                ) as client:
                    response = await client.post(self._url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise AiGatewayError("AI model request timed out") from exc
        except httpx.RequestError as exc:
            raise AiGatewayError("AI model endpoint is unreachable") from exc
        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        if response.status_code != 200:
            raise AiGatewayError(f"AI model returned HTTP {response.status_code}")
        if len(response.content) > 1_000_000:
            raise AiGatewayError("AI model response exceeds the configured safety limit")
        try:
            document = response.json()
            choice = document["choices"][0]
            content = choice["message"]["content"]
            raw_finish_reason = choice.get("finish_reason")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AiGatewayError("AI model returned an invalid OpenAI-compatible response") from exc
        if not isinstance(content, str) or not content.strip():
            raise AiGatewayError("AI model returned an empty response")
        finish_reason = raw_finish_reason if isinstance(raw_finish_reason, str) else None
        LOGGER.info(
            "AI model response completed model=%s finish_reason=%s latency_ms=%s",
            self._model,
            finish_reason or "UNKNOWN",
            latency_ms,
        )
        return content, latency_ms, finish_reason

    async def probe(self) -> AiProbeResult:
        """Send a tiny deterministic request without exposing model output to callers."""

        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a TestOps connectivity probe. "
                        "Follow the requested output exactly."
                    ),
                },
                {"role": "user", "content": "Reply with exactly TOP_AI_READY"},
            ],
            "temperature": 0,
            "max_tokens": 64,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        _, latency_ms, _ = await self._request(payload)
        return AiProbeResult(model=self._model, latency_ms=latency_ms)

    async def complete_json[StructuredModel: BaseModel](
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[StructuredModel],
    ) -> AiStructuredResult[StructuredModel]:
        """Request one strict JSON object and validate it before returning it."""

        schema_json = json.dumps(
            response_model.model_json_schema(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{system_prompt}\n\n"
                        "Return only one JSON object that matches this JSON Schema exactly:\n"
                        f"{schema_json}"
                    ),
                },
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": self._analysis_max_tokens,
            "stream": False,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {
                "enable_thinking": self._thinking_budget > 0,
                "thinking_budget": self._thinking_budget,
            },
        }
        content, latency_ms, finish_reason = await self._request(payload)
        normalized = content.strip()
        if normalized.startswith("```") and normalized.endswith("```"):
            first_newline = normalized.find("\n")
            if first_newline != -1:
                normalized = normalized[first_newline + 1 : -3].strip()
        try:
            raw_document = json.loads(normalized)
        except json.JSONDecodeError as exc:
            LOGGER.warning(
                "AI structured response was not JSON model=%s response_model=%s "
                "finish_reason=%s error=%s",
                self._model,
                response_model.__name__,
                finish_reason or "UNKNOWN",
                exc.msg,
            )
            raise AiGatewayError("AI model returned an invalid structured analysis") from exc
        try:
            validated = response_model.model_validate(raw_document)
        except (ValidationError, TypeError) as exc:
            safe_errors = (
                [
                    {
                        "loc": ".".join(str(part) for part in error["loc"]),
                        "type": error["type"],
                    }
                    for error in exc.errors(include_input=False, include_url=False)[:12]
                ]
                if isinstance(exc, ValidationError)
                else [{"loc": "root", "type": "invalid_type"}]
            )
            LOGGER.warning(
                "AI structured response failed validation model=%s response_model=%s "
                "finish_reason=%s errors=%s",
                self._model,
                response_model.__name__,
                finish_reason or "UNKNOWN",
                safe_errors,
            )
            raise AiGatewayError("AI model returned an invalid structured analysis") from exc
        return AiStructuredResult(
            content=validated,
            model=self._model,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
        )
