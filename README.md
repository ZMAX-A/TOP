# TestOps Platform

多项目自动化测试治理与执行平台，当前开发版本为 `0.15.0`。当前仓库是平台新代码的唯一开发目录；同级
`../web` 是现有颜佳 AI Web 自动化项目，只作为首个 Web Runner 的迁移来源。

## 当前里程碑

M0（协议与工程骨架）、M0.5（旧 Excel 用例基线迁移）、M1（Web Runner
登录模块纵向闭环）、M2（控制面持久化与 Run 调度）、M3（身份、用例治理与
Vue 管理端）和 M4（执行可观测性与制品管理）已完成。
M5.1（完整容器运行基座）、M5.2（系统管理中心）、M5.3（运行运营中心）和
M6（发布加固）、M7.1（项目执行配额）、M7.2（Runner Pool 与执行槽位）和
M7.3（定时回归计划）、M7.4（运行可靠性与队列可观测性）、M7.5（项目质量分析与 SLO）和
M7.6（目标、环境与基线质量维度）和 M7.7（Flaky Case 识别）已实现。

- 统一动作和断言能力注册表；
- 平台用例、Runner Job、Runner Result 契约；
- FastAPI 控制面最小入口；
- Web Runner 的 Job 校验入口；
- 旧 Excel 到不可变 Case Baseline 的确定性迁移器；
- 已发布 `yanjia-ai-web/case-v1.0.0`：92 条用例，89 条启用、3 条禁用；
- 已发布 `case-v1.0.1`，只修复成功登录用例会误通过的弱 URL 断言；
- Web Runner 可执行登录模块 JSON Job，隔离运行目录并产出结构化 Result/Artifact；
- PostgreSQL/SQLAlchemy 控制面模型和首个 Alembic 迁移；
- 项目、目标、环境、不可变基线、自动化包和 Run REST API；
- 幂等 Run 创建、不可变 Run Snapshot、显式状态机和事务 Outbox；
- Redis/Celery Worker 投递、取消轮询、Runner 状态/结果回调和制品元数据入库；
- 本地身份提供者使用 scrypt 密码哈希和只保存 SHA-256 摘要的不透明会话令牌；
- System Admin、Project Admin、Tester、Reviewer、Viewer 五类角色和项目级权限；
- 用例草稿、字段级 Diff、受影响用例验证、异人审批、候选全回归和确认发布；
- Released 基线只读，所有用例修订均生成新的候选版本和审计事件；
- Vue 3 + TypeScript + Element Plus 管理端覆盖登录、项目、基线、Run 和审批工作台；
- Runner 事件持久化、断线幂等补传和带会话认证的 SSE 实时时间线；
- 本地制品在 Worker 侧校验 SHA-256/大小后条件写入 MinIO，不允许覆盖冲突对象；
- 制品下载先校验项目权限，再签发默认 5 分钟有效的短时 URL 并记录审计；
- Run 详情页展示快照、逐用例进度、失败诊断、事件时间线和制品下载；
- `/healthz` 进程存活与 `/readyz` 数据库就绪检查；
- 密钥通过 `secret://` 绑定和进程环境解析，不进入 Job、事件或错误信息；
- 可生成并提交给前端或其他 Runner 使用的 JSON Schema；
- Compose 统一编排迁移、API、Outbox、Celery Worker、Vue/Nginx、PostgreSQL、Redis
  与 MinIO，并使用健康状态控制启动顺序；
- 独立登录靶站和可重复冒烟脚本覆盖成功 Run、预期失败制品、短时下载 URL、密钥
  防泄漏，以及可选的 Outbox/Worker 中断恢复；
- 系统管理中心覆盖用户创建、启停、角色、密码重置、登录会话吊销和审计筛选；
- 项目设置覆盖成员角色、项目/目标归档、运行环境和仅保存 `secret://` 的密钥引用；
- 最后一名有效 System Admin 和 Project Admin 均受服务端保护，不能被误停用或移除；
- 运行列表支持状态、目标、环境、创建人、用例编号、来源 Run 和创建时间组合筛选；
- 运行运营中心支持分页、批量取消、完整重跑和仅异常用例重跑，并展示来源血缘；
- 重跑只从来源 Run 的不可变 Snapshot/Result 取值，不重新解析当前环境或基线；每次
  重跑均生成新 Run、独立幂等键、Outbox 事件和审计记录；
- 项目执行配额限制在途 Run 和 UTC 每日创建量；普通执行、验证、回归和重跑统一经过
  PostgreSQL 项目行锁保护的准入检查，项目设置页展示实时用量、预警和剩余容量；
- Runner Pool 支持目标默认绑定、环境覆盖、Worker 心跳与能力标签；Outbox 按健康容量申请
  槽位租约并投递专属 Queue，运行列表和详情页可解释无健康节点、能力不匹配与容量耗尽；
- 定时回归计划支持五字段 Cron、IANA 时区、错过执行的补跑或跳过策略、手动幂等触发和触发历史；
  自动生成的 Run 继续经过项目配额、Runner Pool、Outbox 和审计边界，界面明确展示 UTC 计划时刻；
- 项目执行策略为每个 Run 冻结超时上限，Runner 状态回调将槽位租约绑定到实际 Worker；独立
  Reaper 幂等回收超时 Run、失联 Worker 租约和无人认领的已派发 Run，并保留事件与审计证据；
- 项目质量分析在 UTC 滚动窗口内分别计算 Run/Case 通过率和执行可靠性，支持项目级目标通过率、
  SLO 达标状态、日趋势，以及对 URL、UUID、数字和凭据字段归一化脱敏后的失败聚类；
- 质量分析可按项目内目标、环境和 Released 基线独立或组合筛选，服务端校验资源归属及目标/环境关系，
  汇总、趋势与失败聚类始终共享同一筛选边界；
- Flaky Case 识别只分析 PASSED/FAILED 结果，要求至少 3 次有结论执行和 2 次状态切换，并公开样本数、
  通过/失败分布、切换率、最新状态与截断边界，避免把一次性回归或修复误报为 Flaky；
- 控制面输出低基数 Prometheus 指标，支持 Bearer 保护、数据库就绪指标和可选本地
  Prometheus profile，并附带可用性、错误率、延迟、派发积压、计划延迟和僵尸租约告警规则；
- GitHub Actions 对 Python、前端、契约制品、迁移图、真实 PostgreSQL 迁移往返、
  Compose 冒烟与 Prometheus 配置建立只读质量门，并在失败时保留 Compose 日志；
- 备份恢复工具生成 PostgreSQL 自定义 dump、MinIO 当前对象和逐文件 SHA-256 清单；
  恢复前必须通过完整性、路径逃逸、目标数据库确认和对象冲突预检；
- 不读取或复制旧项目的 `.env`、`.venv`、报告和登录状态。

## 目录

```text
apps/api/                    FastAPI 控制面
apps/frontend/               Vue 3 管理端
packages/contracts/          平台与 Runner 共用契约
packages/migrations/         只读旧资产迁移器
baselines/                   已发布不可变用例基线
runners/web_playwright/      WebPlaywrightAdapter
services/worker/             Outbox 发布与 Celery 执行 Worker
.github/                     CI 与依赖更新策略
infra/                       本地基础设施与 Prometheus 规则
docs/                        ADR、里程碑与生产运行手册
tests/                       单元与契约测试
```

## 本地验证

Python 3.12 或更高版本：

```powershell
python -m pip install -e ".[dev,platform,runner]"
python -m pytest
python -m ruff check .
python scripts/export_schemas.py --check
python scripts/verify_artifacts.py
```

从旧 Excel 重新验证首个基线（结果必须与已发布文件逐字节一致）：

```powershell
python scripts/migrate_legacy_excel.py `
  --source ..\web\test_cases\test_case.xlsx `
  --project-key yanjia-ai-web `
  --version case-v1.0.0 `
  --output baselines\yanjia-ai-web\case-v1.0.0
```

### 完整 Compose 栈

安装 Docker Desktop 后，可一次启动完整平台。`smoke` profile 会额外启动只用于本地
验收的确定性登录靶站；不会访问旧项目或外部测试系统。

```powershell
Copy-Item .env.example .env
docker compose --profile smoke up -d --build
python scripts/smoke_compose.py
```

冒烟会创建或复用 `platform-smoke` 项目，执行一个成功用例和一个故意失败用例，校验
事件顺序、Screenshot/Trace/Log 上传、下载摘要/大小和密钥不落库。验证事务 Outbox
在 Worker 与发布器短暂停机后仍能恢复投递：

```powershell
python scripts/smoke_compose.py --exercise-recovery
```

入口地址：管理端 `http://127.0.0.1:8080`、API 文档
`http://127.0.0.1:8000/docs`、MinIO Console `http://127.0.0.1:9001`；Smoke Target
健康检查为 `http://127.0.0.1:18080/healthz`。查看状态和日志：

```powershell
docker compose --profile smoke ps
docker compose logs --tail 200 api outbox worker
```

常规开发不需要靶站时使用 `docker compose up -d --build`。停止服务使用
`docker compose down`；该命令默认保留 PostgreSQL、Redis、MinIO 和 Runner 工作区卷。

启动内部 Prometheus（默认 `http://127.0.0.1:9090`）：

```powershell
docker compose --profile observability up -d prometheus
```

控制面指标只在 API 的 `/metrics` 暴露，前端 Nginx 不代理该路径。生产环境应设置
`METRICS_TOKEN` 并从 Prometheus 的受限凭据文件发送 Bearer Token；本地示例配置仅适用
于可信 Compose 网络。

### 备份与恢复

备份命令只创建新目录，拒绝覆盖已有路径；恢复命令会清理并替换目标数据库，必须先按
运行手册停掉写入服务并确认目标名称：

```powershell
python scripts/backup_restore.py backup --output-dir backups\2026-08-12T160000
python scripts/backup_restore.py verify --backup-dir backups\2026-08-12T160000
```

恢复流程、冲突处理和演练要求见
[`docs/operations/production-runbook.md`](docs/operations/production-runbook.md)。

### 按组件本地开发

只用 Compose 启动基础设施，再在 PowerShell 中分别启动控制面和调度链路：

```powershell
Copy-Item .env.example .env
docker compose up -d postgres redis minio
$env:DATABASE_URL="postgresql+asyncpg://testops:change-me-local-only@localhost:5432/testops"
$env:RUNNER_CALLBACK_TOKEN="replace-with-a-local-token"
$env:BOOTSTRAP_ADMIN_TOKEN="replace-with-a-one-time-token"
$env:MINIO_ENDPOINT="http://127.0.0.1:9000"
$env:MINIO_PUBLIC_ENDPOINT="http://127.0.0.1:9000"
$env:MINIO_ACCESS_KEY="testops-local"
$env:MINIO_SECRET_KEY="change-me-local-only"
$env:MINIO_BUCKET="testops-artifacts"
python scripts/db_upgrade.py
python scripts/dev_api.py
```

首次启动时，另开 PowerShell 窗口创建唯一的初始系统管理员。成功后再次调用会返回
`409 Conflict`；数据库中只保存初始化完成标记，不保存初始化 Token。

```powershell
$body = @{
  username = "admin"
  display_name = "System Admin"
  password = "replace-with-a-strong-password"
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/auth/bootstrap `
  -Headers @{ "X-Bootstrap-Token" = $env:BOOTSTRAP_ADMIN_TOKEN } `
  -ContentType "application/json" `
  -Body $body
```

启动管理端：

```powershell
Set-Location apps/frontend
npm.cmd install
npm.cmd run dev
```

另开两个 PowerShell 窗口，并设置相同的环境变量和源码路径：

```powershell
$env:PYTHONPATH="packages/contracts/src;apps/api/src;runners/web_playwright/src;services/worker/src"
$env:MINIO_ENDPOINT="http://127.0.0.1:9000"
$env:MINIO_ACCESS_KEY="testops-local"
$env:MINIO_SECRET_KEY="change-me-local-only"
$env:MINIO_BUCKET="testops-artifacts"
python -m celery -A testops.worker.celery_app:celery_app worker --loglevel=INFO --pool=solo
```

```powershell
$env:PYTHONPATH="packages/contracts/src;apps/api/src;services/worker/src"
python scripts/dispatch_outbox.py --watch
```

验证 Runner Job：

```powershell
$env:PYTHONPATH="packages/contracts/src;runners/web_playwright/src"
python -m testops.runners.web validate-job tests/fixtures/run_snapshot.valid.json
```

验证登录模块 Job 与本地浏览器：

```powershell
python -m testops.runners.web validate-job examples/jobs/yanjia-login-smoke.json
python scripts/smoke_playwright.py
```

## 核心约束

1. 已发布用例基线不可直接修改。
2. Excel 是交换格式，数据库和不可变快照才是事实来源。
3. 用例版本与自动化脚本包版本分离。
4. Runner 只接收 JSON Job，不直接读取或回写业务 Excel。
5. 每个 Run 使用独立临时目录、浏览器上下文和登录状态。
6. 密钥只以引用形式进入 Job，不能写入用例、日志或制品名称。
7. 已发布基线只能逐字节复用；修复必须发布新版本，不能原地覆盖。
8. 提交人不能审批自己的变更；只有通过候选版本全量回归才能发布。
9. Worker 只能上传与本地元数据摘要和大小一致的制品，已存在的冲突对象不可覆盖。
10. 浏览器不获得对象存储密钥；制品下载必须经过项目授权并使用短时签名 URL。
11. 重跑必须保留来源执行时的配置、基线和自动化包，不能静默切换到当前资源版本。
12. 任何恢复都必须先离线验证完整备份，并显式确认目标数据库；不允许在未知对象冲突
    或仍有写入流量时执行覆盖恢复。

阶段设计与验证证据见 [`docs/milestones`](docs/milestones)，M5 与 M6 详见
[`M5.1-full-stack-runtime.md`](docs/milestones/M5.1-full-stack-runtime.md)、
[`M5.2-system-management-center.md`](docs/milestones/M5.2-system-management-center.md) 和
[`M5.3-run-operations.md`](docs/milestones/M5.3-run-operations.md)，以及
[`M6-release-hardening.md`](docs/milestones/M6-release-hardening.md) 和
[`M7.1-execution-quotas.md`](docs/milestones/M7.1-execution-quotas.md) 和
[`M7.2-runner-pools.md`](docs/milestones/M7.2-runner-pools.md) 和
[`M7.3-regression-schedules.md`](docs/milestones/M7.3-regression-schedules.md) 和
[`M7.4-run-reliability.md`](docs/milestones/M7.4-run-reliability.md) 和
[`M7.5-quality-analytics.md`](docs/milestones/M7.5-quality-analytics.md) 和
[`M7.6-quality-dimensions.md`](docs/milestones/M7.6-quality-dimensions.md) 和
[`M7.7-flaky-case-detection.md`](docs/milestones/M7.7-flaky-case-detection.md)。发布前还必须完成
[`生产验收清单`](docs/operations/release-checklist.md)。
