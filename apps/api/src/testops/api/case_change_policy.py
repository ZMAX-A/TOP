"""Classify case changes for governance diffing and executable validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from testops.contracts import CaseDefinition

EXECUTION_RELEVANT_FIELDS = frozenset(
    {
        "$case",
        "module_key",
        "module_name",
        "preconditions",
        "steps",
        "assertion",
        "enabled",
        "timeout_seconds",
        "execution_group",
    }
)


def comparable_case_document(case: CaseDefinition) -> dict[str, object]:
    """Exclude physical Excel coordinates that do not change case semantics."""

    return case.model_dump(mode="json", exclude_none=True, exclude={"source_trace"})


def changed_fields(
    before: Mapping[str, object] | None,
    after: Mapping[str, object] | None,
) -> list[str]:
    if before is None or after is None:
        return ["$case"]
    return sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))


def requires_execution_validation(
    *,
    change_type: str,
    after_document: Mapping[str, object] | None,
    fields: Sequence[str],
) -> bool:
    if change_type not in {"ADD", "MODIFY"} or after_document is None:
        return False
    if not bool(after_document.get("enabled", True)):
        return False
    return bool(EXECUTION_RELEVANT_FIELDS.intersection(fields))
