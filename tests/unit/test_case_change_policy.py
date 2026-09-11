from __future__ import annotations

from uuid import uuid4

from testops.api.case_change_policy import (
    changed_fields,
    comparable_case_document,
    requires_execution_validation,
)
from testops.contracts import (
    AssertionDefinition,
    CaseDefinition,
    CasePriority,
    CaseSourceTrace,
    StepDefinition,
)


def _case(*, row_number: int = 2, title: str = "登录", expected: str = "/") -> CaseDefinition:
    return CaseDefinition(
        case_id=uuid4(),
        case_code="TC-LOGIN-001",
        module_key="login",
        module_name="账号登录",
        title=title,
        test_point="验证登录",
        priority=CasePriority.P0,
        steps=(StepDefinition(operation="nav", input="/login"),),
        assertion=AssertionDefinition(type="url_contains", expected=expected),
        source_trace=CaseSourceTrace(worksheet="自动化测试用例", row_number=row_number),
    )


def test_physical_excel_row_move_is_not_a_semantic_change() -> None:
    case_id = uuid4()
    before = _case(row_number=2).model_copy(update={"case_id": case_id})
    after = _case(row_number=20).model_copy(update={"case_id": case_id})

    assert comparable_case_document(before) == comparable_case_document(after)


def test_metadata_only_change_does_not_require_affected_case_run() -> None:
    case_id = uuid4()
    before = comparable_case_document(_case(title="旧标题").model_copy(update={"case_id": case_id}))
    after = comparable_case_document(_case(title="新标题").model_copy(update={"case_id": case_id}))
    fields = changed_fields(before, after)

    assert fields == ["title"]
    assert not requires_execution_validation(
        change_type="MODIFY",
        after_document=after,
        fields=fields,
    )


def test_assertion_change_requires_affected_case_run() -> None:
    case_id = uuid4()
    before = comparable_case_document(
        _case(expected="/old").model_copy(update={"case_id": case_id})
    )
    after = comparable_case_document(
        _case(expected="/new").model_copy(update={"case_id": case_id})
    )
    fields = changed_fields(before, after)

    assert fields == ["assertion"]
    assert requires_execution_validation(
        change_type="MODIFY",
        after_document=after,
        fields=fields,
    )


def test_enabled_add_requires_validation_but_delete_does_not() -> None:
    after = comparable_case_document(_case())

    assert requires_execution_validation(
        change_type="ADD",
        after_document=after,
        fields=["$case"],
    )
    assert not requires_execution_validation(
        change_type="DELETE",
        after_document=None,
        fields=["$case"],
    )
