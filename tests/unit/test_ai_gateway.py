from __future__ import annotations

import json
import unittest

import httpx

from testops.api.ai_gateway import AiGatewayError, OpenAiCompatibleGateway
from testops.api.config import Settings
from testops.api.schemas import AiRunAnalysisContent


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "ai_enabled": True,
        "ai_base_url": "https://model.example/api",
        "ai_api_key": "unit-test-secret",
        "ai_model": "unit-test-model",
    }
    values.update(overrides)
    return Settings(**values)


class AiConfigurationTests(unittest.TestCase):
    def test_enabled_ai_requires_every_connection_setting(self) -> None:
        with self.assertRaisesRegex(ValueError, "AI_API_KEY"):
            Settings(
                ai_enabled=True,
                ai_base_url="https://model.example/api",
                ai_model="unit-test-model",
            )

    def test_remote_plain_http_requires_explicit_opt_in(self) -> None:
        with self.assertRaisesRegex(ValueError, "AI_ALLOW_INSECURE_HTTP"):
            _settings(ai_base_url="http://model.example/api")

    def test_remote_plain_http_can_be_explicitly_enabled(self) -> None:
        settings = _settings(
            ai_base_url="http://model.example/api",
            ai_allow_insecure_http=True,
        )
        self.assertTrue(settings.ai_enabled)

    def test_settings_repr_does_not_expose_api_key(self) -> None:
        settings = _settings(ai_provider_encryption_key="provider-encryption-secret-material")
        self.assertNotIn("unit-test-secret", repr(settings))
        self.assertNotIn("provider-encryption-secret-material", repr(settings))


class AiGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_uses_openai_compatible_endpoint_without_returning_content(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer unit-test-secret")
            payload = request.read().decode()
            self.assertIn('"model":"unit-test-model"', payload.replace(" ", ""))
            self.assertIn('"enable_thinking":false', payload.replace(" ", ""))
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "TOP_AI_READY"}}]},
            )

        gateway = OpenAiCompatibleGateway(
            _settings(),
            transport=httpx.MockTransport(handler),
        )
        result = await gateway.probe()
        self.assertEqual(result.model, "unit-test-model")
        self.assertGreaterEqual(result.latency_ms, 0)

    async def test_probe_rejects_invalid_response_without_echoing_body(self) -> None:
        gateway = OpenAiCompatibleGateway(
            _settings(),
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(500, text="sensitive upstream details")
            ),
        )
        with self.assertRaisesRegex(AiGatewayError, "HTTP 500") as raised:
            await gateway.probe()
        self.assertNotIn("sensitive upstream details", str(raised.exception))

    async def test_structured_completion_enables_bounded_thinking_and_validates_json(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.read())
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            self.assertEqual(payload["max_tokens"], 16_384)
            self.assertEqual(
                payload["chat_template_kwargs"],
                {"enable_thinking": True, "thinking_budget": 12_288},
            )
            self.assertIn("JSON Schema", payload["messages"][0]["content"])
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    {
                                        "summary": "发现基础设施异常",
                                        "classification": "ENVIRONMENT",
                                        "confidence": 0.8,
                                        "probable_causes": [],
                                        "recommendations": ["检查执行器日志"],
                                        "missing_evidence": [],
                                    }
                                )
                            }
                        }
                    ]
                },
            )

        gateway = OpenAiCompatibleGateway(
            _settings(),
            transport=httpx.MockTransport(handler),
        )
        result = await gateway.complete_json(
            system_prompt="Analyze safely",
            user_prompt="evidence",
            response_model=AiRunAnalysisContent,
        )
        self.assertEqual(result.content.classification, "ENVIRONMENT")
        self.assertEqual(result.model, "unit-test-model")
        self.assertEqual(result.finish_reason, "stop")

    async def test_structured_completion_rejects_non_schema_output_without_echoing_it(self) -> None:
        gateway = OpenAiCompatibleGateway(
            _settings(),
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": '{"secret":"do-not-echo"}'}}]},
                )
            ),
        )
        with self.assertRaisesRegex(AiGatewayError, "invalid structured analysis") as raised:
            await gateway.complete_json(
                system_prompt="Analyze safely",
                user_prompt="evidence",
                response_model=AiRunAnalysisContent,
            )
        self.assertNotIn("do-not-echo", str(raised.exception))
