from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from testops.migrations.android_excel import migrate_android_excel


def test_android_excel_migration_creates_managed_cases(tmp_path: Path) -> None:
    source = tmp_path / "test_case.xlsx"
    workbook = Workbook()
    cases = workbook.active
    cases.title = "自动化测试用例"
    cases.append(
        [
            "用例ID",
            "模块",
            "测试场景",
            "测试点",
            "优先级",
            "前置条件",
            "操作步骤",
            "输入数据",
            "数据类型",
            "期望结果",
            "超时(秒)",
            "自动化状态",
            "标签",
            "是否执行",
            "备注",
            "执行分组",
        ]
    )
    cases.append(
        [
            "TC-HOME-001",
            "首页",
            "首页加载",
            "首页加载",
            "P0",
            "已登录",
            "打开首页",
            "",
            "",
            "首页可见",
            30,
            "AUTOMATED",
            "p0,requires_auth",
            "是",
            "",
            "AUTO",
        ]
    )
    cases.append(
        [
            "TC-DETAIL-001",
            "顾客详情",
            "修改资料",
            "保存资料",
            "P1",
            "专用顾客",
            "修改并恢复",
            "",
            "",
            "保存成功",
            60,
            "AUTOMATED",
            "mutating,restorable",
            "否",
            "",
            "SERIAL",
        ]
    )
    steps = workbook.create_sheet("自动化执行步骤")
    steps.append(["用例ID", "步骤序号", "是否执行"])
    steps.append(["TC-HOME-001", 1, "是"])
    steps.append(["TC-DETAIL-001", 1, "是"])
    workbook.save(source)
    workbook.close()

    result = migrate_android_excel(
        source,
        project_key="yanjia-ai-android",
        version="case-v1.0.0",
    )

    assert len(result.baseline.cases) == 2
    assert sum(case.enabled for case in result.baseline.cases) == 1
    home = result.baseline.cases[0]
    assert home.steps[0].operation == "execute_excel_case"
    assert home.steps[0].input == "TC-HOME-001"
    assert home.assertion.type == "excel_case_passed"
    assert "readonly" in home.tags
    assert "mutating" in result.baseline.cases[1].tags
    assert result.audit["counts"]["enabled_step_rows"] == 2
