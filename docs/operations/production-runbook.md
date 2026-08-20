# TestOps Platform 生产运行手册

## 适用范围

本手册覆盖 `0.21.x` 控制面、Run Outbox、质量告警 Evaluator、Webhook Dispatcher、Scheduler、Reaper、Celery Worker、
PostgreSQL、Redis 和 MinIO 的发布、备份、恢复与基础故障处置。Compose 是单机验收基座，不替代生产 TLS、Ingress、托管
数据库、对象存储权限和集中日志方案。

## 发布前准备

1. 确认 CI 的 Python、Frontend、PostgreSQL migration、Compose smoke 和
   Prometheus config 必需任务全部通过；
2. 将所有 `change-me-*` 值替换为独立随机密钥；`BOOTSTRAP_ADMIN_TOKEN` 只在首次创建
   管理员时短暂启用，完成后从运行环境移除；
3. 固定应用与基础镜像到经过验证的不可变摘要，并保存镜像清单；
4. 运行一次完整备份和离线校验，记录备份位置、摘要、RPO/RTO 与恢复负责人；
5. 确认 PostgreSQL、MinIO、队列和 Runner 工作区容量，以及告警接收链路；
6. 确认前端代理对 SSE 关闭缓冲，但不公开代理 API `/metrics`；入口保持 1 MiB 请求体上限并隐藏 Nginx 版本；
7. 对最终应用镜像和全部基础镜像运行漏洞扫描并保存报告。扫描器未运行、数据库过期或下载失败都必须按门禁未完成
   处理，不能以摘要固定或本地构建成功替代扫描结论。

## 升级流程

升级只向前执行 Alembic。生产故障回退使用上一个应用镜像和已验证备份，不在带业务
数据的数据库上临时执行 `alembic downgrade`。

```powershell
# 1. 先创建并验证备份（见下一节）
docker compose stop scheduler reaper quality-alerts quality-webhooks outbox worker api
docker compose run --rm migrate
docker compose up -d api outbox quality-alerts quality-webhooks scheduler reaper worker frontend
docker compose ps
python scripts/smoke_compose.py
```

升级完成后检查 `/readyz`、Prometheus 告警、Outbox 待发布量、Worker 心跳和一条人工
验收 Run。任何迁移失败都应停止继续发布，保留日志并进入隔离恢复流程。

## 创建与验证备份

在受控运维主机安装与 PostgreSQL 服务端主版本兼容的 `pg_dump`，激活项目 Python
环境，并通过环境变量提供只读数据库和对象存储凭据。工具不会把密码写入命令参数、
清单或输出。

```powershell
$env:DATABASE_URL="postgresql+asyncpg://backup_user:REDACTED@db.internal:5432/testops?sslmode=require"
$env:MINIO_ENDPOINT="https://objects.internal"
$env:MINIO_ACCESS_KEY="testops-backup-reader"
$env:MINIO_SECRET_KEY="REDACTED"
$env:MINIO_BUCKET="testops-artifacts"

python scripts/backup_restore.py backup `
  --output-dir D:\testops-backups\2026-08-12T160000
python scripts/backup_restore.py verify `
  --backup-dir D:\testops-backups\2026-08-12T160000
```

完整备份包含：

- `postgres.dump`：`pg_dump --format=custom --no-owner --no-acl`；
- `objects/`：Bucket 当前可见对象的二进制内容；
- `manifest.json`：数据库、Bucket、对象键、大小、内容类型和 SHA-256；
- `manifest.sha256`：清单自身摘要。

备份目录已存在时命令会拒绝运行；中途中断会保留 `.incomplete`，验证和恢复均拒绝该
目录。`--skip-objects` 只适合明确的数据库专项备份，不能算作平台完整备份。

完成后将备份复制到与生产故障域隔离、启用加密和保留策略的存储，并至少每季度在隔离
环境执行一次真实恢复。推荐目标是每日完整备份、RPO 不超过 24 小时；实际值由业务方
确认并写入发布清单。

## 隔离恢复流程

恢复会对 `DATABASE_URL` 指向的数据库执行 `pg_restore --clean --if-exists`，属于破坏性
操作。先停止 API、Outbox、Quality Alerts、Scheduler、Reaper 与 Worker，确认连接指向隔离或已批准的目标，并预创建目标
数据库和 Bucket。工具在改写数据库前会校验全部文件并检查对象冲突。

```powershell
$env:DATABASE_URL="postgresql+asyncpg://restore_user:REDACTED@restore-db.internal:5432/testops_restore?sslmode=require"
$env:MINIO_ENDPOINT="https://restore-objects.internal"
$env:MINIO_ACCESS_KEY="testops-restore-writer"
$env:MINIO_SECRET_KEY="REDACTED"
$env:MINIO_BUCKET="testops-artifacts-restore"

python scripts/backup_restore.py verify `
  --backup-dir D:\testops-backups\2026-08-12T160000
python scripts/backup_restore.py restore `
  --backup-dir D:\testops-backups\2026-08-12T160000 `
  --confirm-database testops_restore `
  --replace-database
python -m alembic upgrade head
```

存在同名对象且长度或 `sha256` 元数据不一致时，恢复默认停止。只有确认目标 Bucket 可被
替换后才使用 `--overwrite-objects`。恢复完成后验证 Alembic head、用户/项目/Run 数量、
随机制品摘要与下载、登录/RBAC、Run 创建和 Worker 执行，再允许流量进入。

## 指标与告警

API 直接在 `/metrics` 输出 Prometheus 文本格式；标签使用路由模板，不包含 Run ID、
用户名或项目 ID。生产必须设置 `METRICS_TOKEN`，并在 Prometheus 使用受限文件：

```yaml
authorization:
  type: Bearer
  credentials_file: /run/secrets/testops_metrics_token
```

仓库告警规则覆盖：API 连续不可抓取、数据库就绪失败、5xx 比例持续超过 5%、五分钟
p95 延迟持续超过 2 秒，以及可靠性快照、派发积压、定时计划延迟、失联 Worker 租约、质量运营快照、质量
评估延迟和 Webhook PENDING 积压。
上线前必须把这些规则接入实际通知接收器并执行一次测试告警。

项目质量 Webhook 只允许 HTTPS 443，默认拒绝解析到私网或保留地址的目标。签名密钥通过
`TESTOPS_SECRET_<配置名称>` 注入 `quality-webhooks` 进程，数据库只保存 `secret://` 引用；生产还应在网络层
设置固定出口和目标域名允许清单，避免 DNS 重绑定绕过应用层检查。投递历史只保存响应状态码，不保存响应正文。
接收器应使用公开受信任且主机名匹配的证书链；如企业内部接收器使用私有 CA，必须在发布前把批准的 CA 显式加入
Dispatcher 使用的 Python/certifi 信任库并完成真实签名投递，不能只更新操作系统证书目录后假设 HTTP 客户端已信任。
`quality-alerts` 使用 `QUALITY_ALERT_POLL_SECONDS`、`QUALITY_ALERT_EVALUATION_INTERVAL_SECONDS` 和
`QUALITY_ALERT_BATCH_SIZE` 控制扫描节奏；项目自己的重复告警冷却期保存在 Webhook 配置中。评估器只写状态与
投递队列，不执行外部 HTTP 请求。项目管理员可以设置最长 30 天的限时静默并确认具体指标；静默不停止状态
计算，确认也不改变告警等级。静默截止时间会提前持久化的下次评估点，解除静默则安排立即评估，操作前应填写
可审计的维护原因或处置说明。FAILED 投递只能在根因修复后由项目管理员填写原因进行人工重放；重放会保留原
失败记录和事件 ID，使用当前已启用配置创建新的 PENDING 记录，并记录来源、操作者和原因。同一失败节点只允许
生成一个直接重放记录；若重放再次失败，应从新的失败记录继续，禁止反复点击旧记录制造并行重复投递。
值班看板应同时展示 `testops_quality_operations_snapshot_success`、evaluator 到期量/延迟、活跃静默、PENDING
数量/最老年龄和 FAILED 历史量；FAILED 是保留证据，不应单独按总量持续报警。

## 常见故障处置

| 现象 | 首要检查 | 安全处置 |
| --- | --- | --- |
| `/readyz` 返回 503 | PostgreSQL 连接、证书、容量、迁移版本 | 保持 API 摘流，恢复数据库连通后再放量 |
| Run 长时间停留 QUEUED | Outbox 日志、Redis、Celery ping、派发积压指标 | 恢复发布器/Worker；确认 Reaper 在线，不要直接改 Run 状态 |
| Run 自动变为 TIMED_OUT | Run 冻结的 `timeout_seconds`、事件时间线、Runner 日志 | 判断是否合理调高项目策略，再从不可变 Snapshot 重跑 |
| 失联 Worker 租约告警 | Worker 心跳、Reaper 日志、Pool 活动租约 | 隔离异常 Worker，确认租约自动回收后再恢复容量 |
| 定时计划延迟告警 | Scheduler 日志、到期计划量、数据库锁与配额 | 恢复 Scheduler，核对补跑/跳过记录，禁止手工改 `next_fire_at` |
| Runner 大量 INFRA_ERROR | 浏览器/网络/密钥引用、Runner 工作区容量 | 暂停新 Run，保留失败制品，修复后从来源 Snapshot 重跑 |
| 制品下载失败 | MinIO 可用性、Bucket 权限、对象摘要 | 禁止覆盖冲突对象，从已验证备份恢复到隔离 Bucket |
| 5xx 或延迟告警 | 按路由模板聚合指标、数据库慢查询、外部存储 | 限流或摘流，保存时间窗与日志，避免无证据重启循环 |
| 质量 Webhook 长期 PENDING/FAILED | `quality-webhooks` 日志、HTTPS/DNS、目标状态码、签名密钥环境变量 | 先发送测试事件验证修复，再对明确失败的记录执行一次人工重放；不要把真实 URL 或密钥复制到重放原因 |
| 自动告警状态长期不更新 | `quality-alerts` 日志、配置 `next_evaluation_at`、数据库锁与批量上限 | 恢复 evaluator；不要手工改状态或通知序号，先用状态 API 确认积压范围 |
| 静默到期后仍未补发 | 静默截止时间、`next_evaluation_at`、当前状态与最后已通知状态 | 恢复 evaluator 或解除静默触发重新评估；不要手工创建投递或修改通知序号 |
| 质量运营快照失败或 evaluator 延迟 | API 日志、数据库就绪、`quality-alerts` 进程和到期配置量 | 先恢复快照查询与 evaluator；不要以旧指标值判断当前无积压 |
| 误发布或迁移失败 | 发布版本、Alembic current、备份校验结果 | 停止写入，按批准的备份恢复；不直接编辑迁移版本表 |

## 演练与证据

每次生产发布保留 CI 链接、镜像摘要、迁移输出、备份清单摘要、恢复演练日期、冒烟结果
和告警截图。每季度至少演练一次数据库与对象联合恢复，每半年演练一次 Outbox/Worker
中断恢复，并把实际 RTO 与失败项回填到发布清单。
