"""Curated, citation-safe product knowledge for the TOP AI assistant."""

# ruff: noqa: E501 -- knowledge sentences stay intact as model-visible evidence.

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlatformKnowledgeEntry:
    key: str
    title: str
    aliases: tuple[str, ...]
    summary: str
    facts: tuple[str, ...]
    navigation: tuple[str, ...]
    source: str


PLATFORM_KNOWLEDGE: tuple[PlatformKnowledgeEntry, ...] = (
    PlatformKnowledgeEntry(
        key="platform-overview",
        title="TOP 平台能力总览",
        aliases=("平台功能", "功能介绍", "能做什么", "怎么用", "使用帮助", "TOP"),
        summary="TOP 管理用例基线、自动化包、测试运行、执行资源、质量分析、变更审批与审计。",
        facts=(
            "项目空间承载测试目标、环境、成员、基线、自动化包和 Run。",
            "系统管理中心负责用户、Runner Pool、Worker、验证器密钥、会话和审计等全局资源。",
            "平台以不可变基线、不可变自动化包摘要和 Run Snapshot 保证执行可追溯。",
            "AI 助手是只读能力，只能解释平台知识和已有证据，不能修改结果或触发执行。",
        ),
        navigation=("项目空间", "审批与变更", "AI 助手", "系统管理"),
        source="README.md",
    ),
    PlatformKnowledgeEntry(
        key="runner-pool",
        title="Runner Pool 与执行槽位",
        aliases=(
            "runner pool",
            "runnerpool",
            "runner 资源池",
            "资源池",
            "执行槽位",
            "worker 心跳",
            "没有健康节点",
            "能力不匹配",
            "容量耗尽",
            "为什么排队",
        ),
        summary="Runner Pool 是隔离和调度 Runner Worker 的执行资源池，控制任务去哪里执行以及可用并发容量。",
        facts=(
            "System Admin 创建和维护 Runner Pool，可配置目标类型、专属 Celery Queue、池级最大并发及 ACTIVE、DRAINING、DISABLED 状态。",
            "Project Admin 可为测试目标绑定默认 Pool，并在具体环境上覆盖目标绑定；未绑定资源走默认 Queue。",
            "Worker 通过稳定 worker_key 上报心跳、Runner 版本、能力标签、浏览器列表和本机槽位；过期或禁用 Worker 不贡献容量。",
            "可用容量取池级最大并发与匹配且健康 Worker 槽位的较小值，派发成功后建立槽位租约，Run 终态或排队取消时释放。",
            "NO_HEALTHY_RUNNER 表示没有健康且启用的 Worker；RUNNER_CAPABILITY_MISMATCH 表示目标类型或浏览器能力不匹配。",
            "RUNNER_POOL_CAPACITY_EXHAUSTED 表示匹配槽位已满；RUNNER_POOL_DRAINING 或 RUNNER_POOL_DISABLED 表示资源池不接收新任务。",
            "容量等待不是发布失败，资源恢复后待执行 Run 可以继续派发。",
        ),
        navigation=(
            "系统管理 > Runner Pool",
            "项目设置 > 测试目标",
            "项目设置 > 运行环境",
            "Run 执行详情",
        ),
        source="docs/milestones/M7.2-runner-pools.md",
    ),
    PlatformKnowledgeEntry(
        key="case-baseline-change",
        title="用例基线与变更审批",
        aliases=(
            "用例基线",
            "标准基线",
            "released 基线",
            "修改用例",
            "变更申请",
            "审批",
            "候选版本",
        ),
        summary="Released 用例基线不可原地修改；所有修订都通过候选版本、验证、异人审批和全量回归发布。",
        facts=(
            "Excel 是交换格式，数据库记录和不可变基线制品才是平台事实来源。",
            "新增、修改或删除用例会形成字段级 Diff 和新的候选基线版本，不能覆盖 Released 基线。",
            "提交人不能审批自己的变更；只有候选版本全量回归通过后才能发布。",
            "审批发布后历史 Run 仍保留其创建时冻结的基线版本和摘要。",
        ),
        navigation=("项目详情 > 标准基线", "项目详情 > 变更申请", "审批与变更"),
        source="docs/milestones/M0.5-case-baseline.md; README.md",
    ),
    PlatformKnowledgeEntry(
        key="automation-package",
        title="自动化包生命周期",
        aliases=("自动化包", "脚本包", "脚本版本", "激活包", "弃用包", "吊销包", "package"),
        summary="自动化代码以独立版本和不可变摘要管理，与用例基线版本分离。",
        facts=(
            "自动化包经历 DRAFT、验证、ACTIVE、DEPRECATED 或 REVOKED 等生命周期状态。",
            "普通 Run 和定时回归只能使用已激活包；候选包需通过 Released 基线全量验证后才能激活。",
            "弃用包仍可服务历史重跑；吊销包禁止新的普通执行、计划引用和历史重跑。",
            "Runner 只能执行心跳目录声明且本地再次确认的精确名称、版本和不可变摘要，不能按可变标签猜测替代。",
            "Run Snapshot 分别冻结用例基线和自动化包，两者版本不要求相同。",
        ),
        navigation=("项目详情 > 自动化包", "自动化包 > 关联 Run", "Run 执行快照"),
        source="docs/milestones/M9.1-automation-package-lifecycle.md; docs/milestones/M9.2-immutable-package-runtime-admission.md",
    ),
    PlatformKnowledgeEntry(
        key="run-lifecycle-rerun",
        title="Run、进度与重跑",
        aliases=(
            "run",
            "测试运行",
            "执行进度",
            "首轮",
            "第二轮",
            "第三轮",
            "复跑",
            "重跑",
            "取消执行",
        ),
        summary="每条 Run 代表一次平台测试执行，拥有不可变 Snapshot、状态机、逐用例进度、事件和结果。",
        facts=(
            "Run 创建后依次经过准入、排队、派发、执行和终态，详情页展示轮次、已测、成功、失败、事件与制品。",
            "项目最近 10 次运行按 Run 记录计数，不是单条测试用例的执行次数。",
            "完整重跑和仅异常用例重跑都会创建新的 Run，并保留来源 Run 血缘。",
            "重跑必须复用来源 Run 的不可变 Snapshot 与 Result，不会静默切换到当前基线、环境或自动化包。",
            "取消请求会阻止后续执行并释放排队或执行资源；已持久化的事件和结果证据仍然保留。",
        ),
        navigation=("项目详情 > 运行记录", "运行运营", "Run 执行详情"),
        source="docs/milestones/M5.3-run-operations.md; docs/milestones/M7.4-run-reliability.md",
    ),
    PlatformKnowledgeEntry(
        key="project-settings",
        title="项目、目标、环境与密钥引用",
        aliases=("项目设置", "测试目标", "运行环境", "环境配置", "密钥", "secret", "成员"),
        summary="项目设置管理成员、测试目标、运行环境、执行策略和只保存引用的测试密钥。",
        facts=(
            "测试目标描述被测系统类型，运行环境可覆盖目标默认 Runner Pool 等执行配置。",
            "项目和目标支持归档；服务端校验目标、环境和项目的归属关系。",
            "测试密钥只保存 secret:// 引用，真实值在执行边界解析，不能写入用例、日志、事件或制品名称。",
            "项目级超时、在途 Run 上限和 UTC 每日创建量会在 Run 创建时执行准入检查。",
        ),
        navigation=("项目详情 > 项目设置", "项目设置 > 成员", "项目设置 > 目标与环境"),
        source="docs/milestones/M3-identity-governance-frontend.md; docs/milestones/M7.1-execution-quotas.md",
    ),
    PlatformKnowledgeEntry(
        key="roles-permissions",
        title="角色与权限",
        aliases=(
            "权限",
            "角色",
            "system admin",
            "project admin",
            "tester",
            "reviewer",
            "viewer",
            "谁可以",
        ),
        summary="TOP 采用 System Admin 和项目级角色共同控制平台与项目操作。",
        facts=(
            "System Admin 管理全局用户、Runner Pool、Worker、验证器密钥、会话和审计。",
            "Project Admin 管理项目成员、目标、环境、执行策略以及项目级资源配置。",
            "Tester 负责测试执行相关操作，Reviewer 负责审批，Viewer 以查看为主；具体按钮仍由服务端权限校验决定。",
            "最后一名有效 System Admin 和 Project Admin 受服务端保护，不能被误停用或移除。",
        ),
        navigation=("系统管理 > 用户与会话", "项目设置 > 成员", "审批与变更"),
        source="docs/milestones/M3-identity-governance-frontend.md",
    ),
    PlatformKnowledgeEntry(
        key="system-management",
        title="系统管理中心",
        aliases=(
            "系统管理",
            "用户管理",
            "会话管理",
            "会话吊销",
            "审计日志",
            "验证器密钥",
            "worker 管理",
        ),
        summary="系统管理中心集中管理平台级用户、会话、审计、Runner 资源和供应链验证器状态。",
        facts=(
            "只有 System Admin 可以进入系统管理中心并维护平台级资源。",
            "用户禁用或密码重置会立即吊销该用户的全部活跃会话，单个会话也可以精确吊销。",
            "审计日志可按项目、操作者、动作和资源类型筛选，并保留系统和 Runner 等非用户操作者的记录。",
            "Runner Pool 与 Worker 的全局状态在系统管理中心维护，项目设置只负责目标和环境的绑定。",
            "验证器密钥页面只展示生命周期、轮换就绪度和脱敏标识，不返回私钥或真实凭据。",
        ),
        navigation=(
            "系统管理 > 用户",
            "系统管理 > 会话",
            "系统管理 > 审计",
            "系统管理 > Runner Pool",
            "系统管理 > 验证器密钥",
        ),
        source="docs/milestones/M5.2-system-management-center.md; docs/milestones/M9.6.2-verifier-key-lifecycle.md",
    ),
    PlatformKnowledgeEntry(
        key="dispatch-reliability",
        title="Outbox、队列与自动恢复",
        aliases=(
            "outbox",
            "celery",
            "消息队列",
            "派发失败",
            "调度失败",
            "reaper",
            "执行超时",
            "worker 失联",
            "run 一直排队",
        ),
        summary="事务 Outbox 保证 Run 与派发任务一致，Celery 负责执行投递，Reaper 负责超时和失联资源的幂等回收。",
        facts=(
            "Run、不可变 Snapshot、Run Case 和排队 Outbox 在同一数据库事务中创建，避免 Run 已建立但队列消息丢失。",
            "Outbox 发布器批量锁定待发布记录，使用确定性任务 ID、失败退避和幂等消费完成 Celery 投递。",
            "Runner Pool 场景下，Outbox 会先匹配目标、浏览器、不可变自动化包和健康容量，再申请槽位租约。",
            "Reaper 会自动收敛执行超时、Worker 心跳过期和已派发但无人认领的 Run，并释放租约、记录事件和审计。",
            "Run 长时间停留 QUEUED 时应依次检查 Outbox 积压、Redis、Celery、Worker 心跳和 Runner Pool 等待原因，不能直接修改 Run 状态。",
        ),
        navigation=("运行运营", "Run 执行详情 > 事件时间线", "系统管理 > Runner Pool", "系统观测"),
        source="docs/milestones/M2-control-plane-and-dispatch.md; docs/milestones/M7.4-run-reliability.md; docs/operations/production-runbook.md",
    ),
    PlatformKnowledgeEntry(
        key="execution-quotas",
        title="项目执行配额",
        aliases=("执行配额", "配额", "每日运行", "并发限制", "在途 run", "quota"),
        summary="执行配额限制项目同时在途的 Run 数和 UTC 每日创建量，避免单项目挤占平台资源。",
        facts=(
            "普通执行、候选验证、全量回归和重跑都经过同一项目配额准入边界。",
            "准入使用 PostgreSQL 项目行锁，防止并发创建绕过限制。",
            "项目设置页展示当前用量、剩余容量和预警；配额通过后仍需等待 Runner Pool 的实际容量。",
        ),
        navigation=("项目设置 > 执行策略", "运行运营"),
        source="docs/milestones/M7.1-execution-quotas.md",
    ),
    PlatformKnowledgeEntry(
        key="regression-schedules",
        title="定时回归计划",
        aliases=("定时回归", "回归计划", "cron", "定时任务", "补跑", "跳过策略"),
        summary="定时回归按 Cron 和 IANA 时区创建可追溯 Run，并支持错过执行后的补跑或跳过策略。",
        facts=(
            "计划支持五字段 Cron、IANA 时区、手动幂等触发和触发历史。",
            "自动生成的 Run 仍经过项目配额、Runner Pool、Outbox 和审计边界。",
            "界面明确展示 UTC 计划时刻，避免把本地时区和调度事实混淆。",
        ),
        navigation=("项目设置 > 回归计划", "运行运营 > 计划触发记录"),
        source="docs/milestones/M7.3-regression-schedules.md",
    ),
    PlatformKnowledgeEntry(
        key="quality-analytics",
        title="质量分析、SLO 与 Flaky Case",
        aliases=(
            "质量分析",
            "通过率",
            "执行可靠性",
            "slo",
            "趋势",
            "flaky",
            "不稳定用例",
            "失败聚类",
        ),
        summary="质量分析在滚动窗口内计算 Run/Case 通过率、执行可靠性、趋势、失败聚类和 Flaky Case。",
        facts=(
            "Run 通过率、Case 通过率和执行可靠性分别计算，不能用其中一个替代另一个。",
            "分析可按测试目标、环境和 Released 基线组合筛选，所有统计共享同一筛选边界。",
            "失败聚类会对 URL、UUID、数字和凭据字段归一化脱敏。",
            "Flaky Case 只分析 PASSED/FAILED，要求至少 3 次有结论执行且至少 2 次状态切换，并公开样本和截断边界。",
        ),
        navigation=("项目详情 > 质量分析", "质量分析 > 趋势", "质量分析 > Flaky Case"),
        source="docs/milestones/M7.5-quality-analytics.md; docs/milestones/M7.7-flaky-case-detection.md",
    ),
    PlatformKnowledgeEntry(
        key="quality-alerts",
        title="质量波动告警与 Webhook",
        aliases=("质量告警", "波动告警", "webhook", "静默", "告警确认", "重放", "通知"),
        summary="质量告警比较相邻 UTC 窗口，可通过签名 Webhook 投递并支持确认、限时静默和失败重放。",
        facts=(
            "告警分别比较 Run 通过率、Case 通过率和执行可靠性的百分点变化，缺少样本时标记为无可比数据。",
            "Webhook 地址脱敏展示，可选 HMAC-SHA256 签名；响应正文和真实密钥不落库。",
            "项目管理员可确认 WARNING/CRITICAL、设置最长 30 天静默，并对失败投递填写原因后重放。",
            "原失败投递保持不变，重放生成新记录并保留来源、操作人与审计证据。",
        ),
        navigation=("项目设置 > 质量告警", "质量分析 > 告警状态", "系统观测"),
        source="docs/milestones/M8.1-quality-change-alerts.md; docs/milestones/M8.5-quality-webhook-replay.md",
    ),
    PlatformKnowledgeEntry(
        key="artifacts-observability",
        title="执行事件、制品与审计",
        aliases=("制品", "截图", "trace", "日志", "事件时间线", "审计", "下载", "artifact"),
        summary="Run 事件、结果与制品元数据形成可追溯证据，制品下载经过项目授权和短时签名。",
        facts=(
            "Runner 事件持久化，支持断线后的幂等补传和带会话认证的 SSE 实时时间线。",
            "Worker 上传前校验制品 SHA-256 和大小，冲突对象不能被覆盖。",
            "浏览器不持有对象存储密钥；下载前检查项目权限，再签发默认 5 分钟有效的短时 URL 并记录审计。",
            "AI 只接收有界、脱敏的制品元数据，不读取制品 URI 或原始内容。",
        ),
        navigation=("Run 执行详情 > 事件时间线", "Run 执行详情 > 制品", "系统管理 > 审计"),
        source="docs/milestones/M4-run-observability-and-artifacts.md",
    ),
    PlatformKnowledgeEntry(
        key="execution-isolation",
        title="执行隔离与供应链准入",
        aliases=(
            "执行隔离",
            "容器隔离",
            "kubernetes",
            "供应链",
            "签名",
            "sbom",
            "provenance",
            "镜像摘要",
        ),
        summary="每个 Run 使用独立执行边界，并对自动化包镜像、签名和实际运行参数执行失败关闭校验。",
        facts=(
            "Compose Worker 默认将 Run 放入独立容器；Kubernetes Worker 为每个 Run 创建独立 Job。",
            "生产容器执行要求只读根文件系统、非默认网络以及 CPU、内存、PID 等资源限制，并回读实际参数形成证据。",
            "Run 容器不得挂载 Docker Socket，只能使用一次性只读 Snapshot、隔离工作区和显式网络策略。",
            "供应链准入校验不可变 Manifest Digest、签名、provenance 与 SBOM；任一必要检查失败即拒绝新执行。",
        ),
        navigation=("自动化包 > 供应链状态", "Run 执行详情 > 执行快照", "系统管理 > 验证器密钥"),
        source="docs/milestones/M9.4.1-supply-chain-admission.md; docs/milestones/M9.5.2-container-execution-isolation.md",
    ),
    PlatformKnowledgeEntry(
        key="ai-assistant",
        title="AI 助手的范围与边界",
        aliases=(
            "ai 助手",
            "copilot",
            "项目全局",
            "对话记录",
            "分析范围",
            "ai 能做什么",
            "模型",
            "模型配置",
            "自定义模型",
            "base url",
            "api key",
        ),
        summary="AI 助手结合平台功能知识、最近 10 次 Run 或所选 Run 证据进行只读问答。",
        facts=(
            "项目全局范围只统计最近 10 条 Run；所选 Run 范围会加入该 Run 的用例、事件、制品元数据和隔离证据。",
            "平台知识问答使用随版本维护的知识条目，并返回 knowledge: 开头的证据引用。",
            "全局与 Run 对话按用户、项目和范围分别持久化，刷新页面后可以恢复。",
            "系统管理员可以在系统管理的 AI 模型页新增 OpenAI-compatible 接口、测试连接、启停并切换默认模型；环境变量模型始终保留为可回退入口。",
            "自定义模型的 API Key 只在保存时提交，服务端加密存储，列表和详情接口不会返回密钥原文。",
            "问题、历史和运行证据在发送给模型前会做敏感信息遮蔽，回答还会校验引用并二次脱敏。",
            "AI 不会修改用例、测试结果、配置或服务，也不会自行触发重跑。",
        ),
        navigation=(
            "AI 助手 > AI 模型",
            "AI 助手 > 项目全局",
            "AI 助手 > 所选 Run",
            "系统管理 > AI 模型",
        ),
        source="apps/api/src/testops/api/ai_provider_services.py; apps/frontend/src/views/AiAssistantView.vue; apps/frontend/src/views/AdminView.vue",
    ),
    PlatformKnowledgeEntry(
        key="run-email-notifications",
        title="测试运行结果邮件通知",
        aliases=(
            "邮件通知",
            "邮箱通知",
            "测试结果发邮件",
            "run email",
            "smtp",
            "收件邮箱",
        ),
        summary="每位用户可配置最多 5 个收件邮箱，并在本人发起的 Run 结束后接收选定状态通知。",
        facts=(
            "入口位于左侧导航栏的“邮件通知设置”，每个账号可保存 1 至 5 个收件邮箱。",
            "可订阅 PASSED、FAILED、INFRA_ERROR、TIMED_OUT 和 CANCELED；只通知由当前账号发起的 Run。",
            "关闭邮件通知后不会发送任何邮件；开启后，同一 Run 会分别投递到该账号保存的全部收件邮箱。",
            "平台管理员需先通过环境变量配置 SMTP 发件服务，用户不需要也不能填写 SMTP 密码。",
            "每个收件地址拥有独立投递记录与重试状态，一个地址失败不会阻止其他地址发送。",
            "投递失败会指数退避重试；用户停用通知或更换邮箱时，尚未发送的旧投递会被取消。",
            "邮件包含项目、Run 状态、用例统计、基线/自动化包版本和详情链接，不包含测试凭据。",
        ),
        navigation=("左侧导航栏 > 邮件通知设置", "Run 执行详情"),
        source="docs/milestones/M10.4-run-email-notifications.md",
    ),
)


_WORD_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]*", re.IGNORECASE)


def _match_score(entry: PlatformKnowledgeEntry, query: str) -> int:
    normalized = query.casefold()
    score = 0
    for alias in entry.aliases:
        candidate = alias.casefold()
        if candidate in normalized:
            score += 20 + min(len(candidate), 20)
    title = entry.title.casefold()
    for word in _WORD_PATTERN.findall(normalized):
        if len(word) >= 3 and (
            word in title or any(word in alias.casefold() for alias in entry.aliases)
        ):
            score += 5
    return score


def build_platform_knowledge_evidence(
    query: str,
    *,
    max_entries: int = 4,
) -> tuple[dict[str, object], frozenset[str]]:
    """Return a compact catalog plus product-knowledge entries relevant to a question."""

    ranked = sorted(
        ((entry, _match_score(entry, query)) for entry in PLATFORM_KNOWLEDGE),
        key=lambda item: (-item[1], item[0].key),
    )
    matched = [entry for entry, score in ranked if score > 0][:max_entries]
    if not matched:
        matched = [PLATFORM_KNOWLEDGE[0]]
    document = {
        "scope": "VERSIONED_TOP_PRODUCT_KNOWLEDGE",
        "usage": (
            "Use matched_entries for feature behavior. The catalog only lists available topics. "
            "Do not infer current deployment state from product knowledge."
        ),
        "catalog": [
            {"key": entry.key, "title": entry.title, "summary": entry.summary}
            for entry in PLATFORM_KNOWLEDGE
        ],
        "matched_entries": [
            {
                "evidence_ref": f"knowledge:{entry.key}",
                "title": entry.title,
                "summary": entry.summary,
                "facts": list(entry.facts),
                "navigation": list(entry.navigation),
                "source": entry.source,
            }
            for entry in matched
        ],
    }
    refs = frozenset(f"knowledge:{entry.key}" for entry in matched)
    return document, refs
