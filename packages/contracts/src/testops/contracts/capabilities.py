"""Single source of truth for keyword operations and assertions.

The legacy project duplicated these lists in its executor, validator and Excel
documentation. The platform keeps one registry and derives validation and API
responses from it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

CapabilityKind = Literal["operation", "assertion"]


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    key: str
    kind: CapabilityKind
    description: str
    requires_locator: bool = False
    consumes_input: bool = False
    requires_input: bool = False
    aliases: tuple[str, ...] = ()


OPERATIONS: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(
        "input",
        "operation",
        "输入文本",
        requires_locator=True,
        consumes_input=True,
        requires_input=True,
    ),
    CapabilitySpec(
        "input_enter",
        "operation",
        "输入文本并按 Enter",
        requires_locator=True,
        consumes_input=True,
        requires_input=True,
    ),
    CapabilitySpec("click", "operation", "点击元素", requires_locator=True),
    CapabilitySpec(
        "select",
        "operation",
        "选择下拉选项",
        requires_locator=True,
        consumes_input=True,
        requires_input=True,
    ),
    CapabilitySpec("verify", "operation", "验证元素可见", requires_locator=True),
    CapabilitySpec("hover", "operation", "鼠标悬停", requires_locator=True),
    CapabilitySpec("scroll", "operation", "滚动到元素或页面位置"),
    CapabilitySpec("wait", "operation", "等待指定秒数"),
    CapabilitySpec("nav", "operation", "导航到 URL", consumes_input=True),
    CapabilitySpec("find_click", "operation", "查找候选项并点击", requires_locator=True),
    CapabilitySpec(
        "upload",
        "operation",
        "上传文件",
        requires_locator=True,
        consumes_input=True,
        requires_input=True,
    ),
    CapabilitySpec(
        "daterange",
        "operation",
        "输入日期范围",
        requires_locator=True,
        consumes_input=True,
        requires_input=True,
        aliases=("date_range",),
    ),
    CapabilitySpec("switch_tab", "operation", "切换或打开标签页"),
    CapabilitySpec("retry_report", "operation", "重试报告生成", requires_locator=True),
)


ASSERTIONS: tuple[CapabilitySpec, ...] = (
    CapabilitySpec("text_equals", "assertion", "文本完全相等"),
    CapabilitySpec("text_contains", "assertion", "文本包含指定内容"),
    CapabilitySpec(
        "text_visible",
        "assertion",
        "指定文本可见",
        aliases=("visible_text",),
    ),
    CapabilitySpec("text_hidden", "assertion", "指定文本不可见"),
    CapabilitySpec("text_not_empty", "assertion", "元素文本非空", requires_locator=True),
    CapabilitySpec("value_equals", "assertion", "输入值相等", requires_locator=True),
    CapabilitySpec("element_visible", "assertion", "元素可见", requires_locator=True),
    CapabilitySpec("element_disabled", "assertion", "元素禁用", requires_locator=True),
    CapabilitySpec("element_count", "assertion", "元素数量相等", requires_locator=True),
    CapabilitySpec("attr_equals", "assertion", "元素属性相等", requires_locator=True),
    CapabilitySpec("url_contains", "assertion", "URL 包含指定内容"),
    CapabilitySpec("url_not_contains", "assertion", "URL 不包含指定内容"),
    CapabilitySpec("url_matches", "assertion", "URL 匹配正则"),
    CapabilitySpec("empty_list", "assertion", "列表为空", requires_locator=True),
    CapabilitySpec("list_contains", "assertion", "列表包含指定内容", requires_locator=True),
    CapabilitySpec("date_in_range", "assertion", "日期处于范围内", requires_locator=True),
    CapabilitySpec("value_in_range", "assertion", "数值处于范围内", requires_locator=True),
    CapabilitySpec("file_verify", "assertion", "验证下载文件"),
    CapabilitySpec("age_in_range", "assertion", "年龄处于范围内"),
    CapabilitySpec("date_format", "assertion", "日期格式正确", requires_locator=True),
    CapabilitySpec("text_optional", "assertion", "文本可为空或有值", requires_locator=True),
)


def _build_index(specs: tuple[CapabilitySpec, ...]) -> dict[str, CapabilitySpec]:
    index: dict[str, CapabilitySpec] = {}
    for spec in specs:
        for key in (spec.key, *spec.aliases):
            if key in index:
                raise RuntimeError(f"duplicate capability key: {key}")
            index[key] = spec
    return index


_OPERATION_INDEX = _build_index(OPERATIONS)
_ASSERTION_INDEX = _build_index(ASSERTIONS)


def get_operation_spec(key: str) -> CapabilitySpec:
    try:
        return _OPERATION_INDEX[key.strip()]
    except KeyError as exc:
        raise ValueError(f"unsupported operation: {key}") from exc


def get_assertion_spec(key: str) -> CapabilitySpec:
    try:
        return _ASSERTION_INDEX[key.strip()]
    except KeyError as exc:
        raise ValueError(f"unsupported assertion: {key}") from exc


def capability_payload() -> dict[str, list[dict[str, object]]]:
    """Return a JSON-safe payload for the control-plane API and frontend."""

    return {
        "operations": [asdict(spec) for spec in OPERATIONS],
        "assertions": [asdict(spec) for spec in ASSERTIONS],
    }
