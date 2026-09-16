from __future__ import annotations

import unittest
from uuid import uuid4

from testops.api.ai_gateway import AiGatewayError, OpenAiCompatibleGateway
from testops.api.ai_provider_services import (
    AiGatewayManager,
    AiProviderSecretBox,
    create_ai_model_provider,
    delete_ai_model_provider,
    list_ai_model_providers,
    update_ai_model_provider,
)
from testops.api.config import Settings
from testops.api.database import create_database_runtime, create_schema
from testops.api.schemas import AiModelProviderCreate, AiModelProviderUpdate
from testops.api.services import InvalidRequest


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "ai_enabled": True,
        "ai_base_url": "https://environment-model.example/v1",
        "ai_api_key": "environment-secret-with-enough-entropy",
        "ai_model": "environment-model",
        "ai_provider_encryption_key": "provider-wrapping-key-with-enough-entropy",
    }
    values.update(overrides)
    return Settings(**values)


class AiProviderSecretTests(unittest.TestCase):
    def test_secret_box_round_trip_does_not_embed_plaintext(self) -> None:
        box = AiProviderSecretBox("provider-wrapping-key-with-enough-entropy")
        ciphertext = box.encrypt("custom-provider-secret")

        self.assertNotIn("custom-provider-secret", ciphertext)
        self.assertEqual(box.decrypt(ciphertext), "custom-provider-secret")

    def test_different_wrapping_key_cannot_decrypt_secret(self) -> None:
        ciphertext = AiProviderSecretBox("first-provider-wrapping-key-with-entropy").encrypt(
            "custom-provider-secret"
        )
        with self.assertRaisesRegex(AiGatewayError, "cannot be decrypted"):
            AiProviderSecretBox("second-provider-wrapping-key-with-entropy").decrypt(ciphertext)


class AiProviderPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.settings = _settings()
        self.engine, self.session_factory = create_database_runtime(
            Settings(database_url="sqlite+aiosqlite:///:memory:")
        )
        await create_schema(self.engine)
        self.actor_id = uuid4()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_provider_is_encrypted_selectable_and_can_become_default(self) -> None:
        async with self.session_factory() as session:
            provider = await create_ai_model_provider(
                session,
                AiModelProviderCreate(
                    name="Local Qwen",
                    base_url="https://custom-model.example/v1",
                    model="qwen-custom",
                    api_key="custom-provider-secret",
                ),
                self.actor_id,
                self.settings,
            )
            provider_id = provider.id
            self.assertIsNotNone(provider.api_key_ciphertext)
            self.assertNotIn("custom-provider-secret", provider.api_key_ciphertext or "")

        environment_gateway = OpenAiCompatibleGateway.from_settings(self.settings)
        self.assertIsNotNone(environment_gateway)
        manager = AiGatewayManager(self.settings, environment_gateway)
        async with self.session_factory() as session:
            options = await manager.model_options(session)
            self.assertEqual([item.model for item in options], ["environment-model", "qwen-custom"])
            self.assertTrue(options[0].is_default)

            resolved = await manager.resolve(session, str(provider_id))
            self.assertEqual(resolved._model, "qwen-custom")
            self.assertEqual(resolved._api_key, "custom-provider-secret")

            provider = await update_ai_model_provider(
                session,
                provider_id,
                AiModelProviderUpdate(is_default=True),
                self.actor_id,
                self.settings,
            )
            original_ciphertext = provider.api_key_ciphertext

        async with self.session_factory() as session:
            options = await manager.model_options(session)
            custom_option = next(item for item in options if item.model == "qwen-custom")
            environment_option = next(
                item for item in options if item.model == "environment-model"
            )
            self.assertTrue(custom_option.is_default)
            self.assertFalse(environment_option.is_default)
            resolved = await manager.resolve(session)
            self.assertEqual(resolved._model, "qwen-custom")

            provider = await update_ai_model_provider(
                session,
                provider_id,
                AiModelProviderUpdate(name="Local Qwen Updated"),
                self.actor_id,
                self.settings,
            )
            self.assertEqual(provider.api_key_ciphertext, original_ciphertext)

            provider = await update_ai_model_provider(
                session,
                provider_id,
                AiModelProviderUpdate(status="DISABLED"),
                self.actor_id,
                self.settings,
            )
            self.assertFalse(provider.is_default)
            resolved = await manager.resolve(session)
            self.assertEqual(resolved._model, "environment-model")

            await delete_ai_model_provider(session, provider_id, self.actor_id)

        async with self.session_factory() as session:
            self.assertEqual(await list_ai_model_providers(session), ())
            resolved = await manager.resolve(session)
            self.assertEqual(resolved._model, "environment-model")

    async def test_remote_plain_http_follows_existing_platform_safety_setting(self) -> None:
        async with self.session_factory() as session:
            with self.assertRaisesRegex(InvalidRequest, "trusted HTTP option"):
                await create_ai_model_provider(
                    session,
                    AiModelProviderCreate(
                        name="Unsafe HTTP",
                        base_url="http://model.example/v1",
                        model="unsafe-model",
                    ),
                    self.actor_id,
                    self.settings,
                )

    async def test_api_key_requires_server_side_wrapping_material(self) -> None:
        settings = Settings(database_url="sqlite+aiosqlite:///:memory:")
        async with self.session_factory() as session:
            with self.assertRaisesRegex(InvalidRequest, "encryption is unavailable"):
                await create_ai_model_provider(
                    session,
                    AiModelProviderCreate(
                        name="Missing Wrapping Key",
                        base_url="https://model.example/v1",
                        model="model",
                        api_key="provider-secret",
                    ),
                    self.actor_id,
                    settings,
                )
