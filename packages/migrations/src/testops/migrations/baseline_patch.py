"""Derive a new immutable baseline from a published parent baseline."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid5

from testops.contracts import (
    AssertionDefinition,
    CaseBaseline,
    DerivedCaseBaselineSource,
)

from .legacy_excel import BASELINE_NAMESPACE, LegacyExcelMigrationError, MigrationResult


def derive_case_baseline(
    parent: CaseBaseline,
    *,
    parent_digest: str,
    version: str,
    change_set: str,
    assertion_updates: Mapping[str, AssertionDefinition],
) -> MigrationResult:
    """Create a new baseline while keeping case IDs and the parent untouched."""

    if version == parent.version:
        raise LegacyExcelMigrationError("派生基线必须使用新的版本号")
    if not assertion_updates:
        raise LegacyExcelMigrationError("派生基线至少需要一个明确变更")

    known_codes = {case.case_code for case in parent.cases}
    unknown_codes = sorted(set(assertion_updates) - known_codes)
    if unknown_codes:
        raise LegacyExcelMigrationError(f"派生变更引用未知用例：{', '.join(unknown_codes)}")

    changes: list[dict[str, object]] = []
    cases = []
    for case in parent.cases:
        replacement = assertion_updates.get(case.case_code)
        if replacement is None:
            cases.append(case)
            continue
        if replacement == case.assertion:
            raise LegacyExcelMigrationError(f"用例 {case.case_code} 的派生断言没有发生变化")
        cases.append(case.model_copy(update={"assertion": replacement}))
        changes.append(
            {
                "case_code": case.case_code,
                "field": "assertion",
                "rule": change_set,
                "from": case.assertion.model_dump(mode="json", exclude_none=True),
                "to": replacement.model_dump(mode="json", exclude_none=True),
            }
        )

    baseline = CaseBaseline(
        baseline_id=uuid5(BASELINE_NAMESPACE, f"baseline:{parent.project_key}:{version}"),
        project_key=parent.project_key,
        version=version,
        source=DerivedCaseBaselineSource(
            parent_baseline_id=parent.baseline_id,
            parent_version=parent.version,
            parent_digest=parent_digest,
            change_set=change_set,
        ),
        cases=tuple(cases),
    )
    enabled_count = sum(case.enabled for case in cases)
    audit = {
        "schema_version": "1.0",
        "migration": "derived-case-baseline",
        "source": {
            "baseline_id": str(parent.baseline_id),
            "version": parent.version,
            "digest": parent_digest,
        },
        "target": {
            "baseline_id": str(baseline.baseline_id),
            "project_key": baseline.project_key,
            "version": baseline.version,
        },
        "policies": {
            "case_ids": "preserved from parent baseline",
            "parent_immutability": "parent files are read only",
            "change_set": change_set,
        },
        "counts": {
            "source_cases": len(parent.cases),
            "baseline_cases": len(cases),
            "enabled_cases": enabled_count,
            "disabled_cases": len(cases) - enabled_count,
            "changes": len(changes),
            "warnings": 0,
            "errors": 0,
        },
        "changes": changes,
        "warnings": [],
        "errors": [],
    }
    return MigrationResult(baseline=baseline, audit=audit)
