"""Generate language-neutral JSON Schema from the Pydantic source models."""

from __future__ import annotations

from pydantic import BaseModel

from .models import CaseBaseline, CaseDefinition, RunResult, RunSnapshot

type ModelType = type[BaseModel]

SCHEMA_MODELS: dict[str, ModelType] = {
    "case-baseline.schema.json": CaseBaseline,
    "case-definition.schema.json": CaseDefinition,
    "run-result.schema.json": RunResult,
    "run-snapshot.schema.json": RunSnapshot,
}


def schemas() -> dict[str, dict[str, object]]:
    return {
        filename: model.model_json_schema(mode="validation")
        for filename, model in SCHEMA_MODELS.items()
    }
