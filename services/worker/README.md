# Worker

Worker 通过事务 Outbox 将控制面的 `run.queued`、`run.cancel_requested` 事件发布到
Celery。任务只接收不可变 Run Snapshot，站点操作仍封装在对应 Runner Adapter 中。

质量告警 Webhook 使用独立的 `quality_webhook_deliveries` 队列和
`scripts/dispatch_quality_webhooks.py`，不与 Run/Celery Outbox 混用；dispatcher 只保存 HTTP 状态码和脱敏
错误，可选签名密钥从 `TESTOPS_SECRET_<配置名称>` 环境变量读取。

`scripts/evaluate_quality_alerts.py` 是独立的项目质量评估循环。它按配置中的持久化到期时间计算相邻 UTC
窗口，把触发、升级、降级和恢复事件事务性写入上述队列；通知序号、信号指纹和冷却期防止重复或抖动投递。
限时静默只抑制自动投递，不停止计算或推进已通知状态；静默到期会提前调度评估，以便补发仍成立的状态变化。
FAILED 投递的人工重放会创建新的 PENDING 记录并保留原失败记录和原事件 ID；dispatcher 无需特殊分支，仍按
普通持久化记录处理。新记录使用当前已启用配置，因此应先用测试事件验证目标和签名密钥，再由管理员重放。
控制面的低基数指标会统计 PENDING/FAILED、最老 PENDING 年龄和重放记录；dispatcher 不直接推送指标，也不在
标签中写入项目、目标或投递 ID。

当前闭环包括：

- `run:<run_id>` 确定性任务 ID；
- JSON-only Celery 消息、late ack 和单任务预取；
- PREPARING/RUNNING 状态回调；
- Runner 进度事件实时回调，并在提交结果前从本地 `events.jsonl` 幂等补传；
- 在用例边界轮询持久化取消标记；
- 本地结果恢复和幂等结果重放；
- 本地制品 SHA-256/大小复核、MinIO 条件写上传和上传后元数据复核；
- Worker 心跳声明预装的不可变自动化包 `repository@sha256`，Outbox 只向精确承载该包的 Worker 派发；
- Worker 在进入 Adapter 前再次按 `RUNNER_PACKAGE_CATALOG` 校验 Runner 类型、OCI 仓库和摘要，未配置或不匹配
  时以 `AUTOMATION_PACKAGE_UNAVAILABLE` 失败关闭；
- `SUBPROCESS` 兼容模式将每个 Run 放入独立 Python 子进程；Compose 默认 `CONTAINER` 模式使用独立 Docker 容器、
  只读根、专用输入/输出卷、受控网络和 CPU/内存/PID 限额；
- `KUBERNETES` 模式为每个 Run 创建独立 Job、immutable Snapshot ConfigMap、最小 Secret 和 Egress NetworkPolicy，
  Run Pod 使用非默认 ServiceAccount 且禁用 Token 自动挂载；
- 三种隔离执行器都只向子执行边界注入 Snapshot 明确绑定的 `TESTOPS_SECRET_*`；数据库、Redis、回调 Token 和 MinIO
  凭据只留在可信控制器。Docker/Kubernetes 硬隔离只有在回读实际运行参数后才写入 Result 证据；
- 失败发布指数退避，成功后将 Outbox 标记为 `PUBLISHED`。

完整容器栈由根目录 `compose.yaml` 启动，Worker 使用内置 Chromium、非 root
`pwuser` 和持久化 `runner-workspaces` 卷：

```powershell
docker compose up -d --build worker outbox
docker compose logs -f worker outbox
```

完整队列、浏览器和 MinIO 验收使用 `docker compose --profile smoke up -d --build`
及 `python scripts/smoke_compose.py`。加 `--exercise-recovery` 会先停止 Worker/Outbox，
在停机窗口创建 Run，再恢复服务并确认事务 Outbox 未丢任务。

Windows 本地运行：

```powershell
$env:PYTHONPATH="packages/contracts/src;apps/api/src;runners/web_playwright/src;services/worker/src"
$env:RUNNER_CALLBACK_TOKEN="replace-with-the-api-token"
$env:MINIO_ENDPOINT="http://127.0.0.1:9000"
$env:MINIO_ACCESS_KEY="testops-local"
$env:MINIO_SECRET_KEY="change-me-local-only"
$env:MINIO_BUCKET="testops-artifacts"
$env:RUNNER_PACKAGE_CATALOG='[{"runner_type":"WEB_PLAYWRIGHT","image_repository":"testops-worker","digest":"sha256:<64-hex>"}]'
$env:RUNNER_EXECUTION_MODE="CONTAINER"
$env:RUNNER_CONTAINER_IMAGE="testops-worker:0.30.0"
$env:RUNNER_CONTAINER_NETWORK_POLICY="ALLOWLIST"
$env:RUNNER_CONTAINER_NETWORK="testops-runner-sandbox"
$env:RUNNER_CONTAINER_MEMORY_MIB="1024"
$env:RUNNER_CONTAINER_CPU_MILLIS="1000"
$env:RUNNER_CONTAINER_PIDS_LIMIT="256"
$env:RUNNER_EXECUTOR_TIMEOUT_SECONDS="3600"
python -m celery -A testops.worker.celery_app:celery_app worker --loglevel=INFO --pool=solo
```

持续发布 Outbox：

```powershell
python scripts/dispatch_outbox.py --watch --interval-seconds 1
```

Worker 只解析 `TESTOPS_SECRET_<绑定名>` 形式的进程环境变量；真实密钥不得写入
Snapshot、日志、异常或制品名称。生产环境应使用仅限目标 Bucket 的专用 MinIO
服务账号，不能沿用本地 Root 凭据。`RUNNER_PACKAGE_CATALOG` 只声明已经随 Worker 镜像预装并验证的运行时；
不得通过该变量触发远程下载，也不能使用标签、协议 URL 或缺少摘要的引用。

容器模式的父 Worker 是高信任执行控制器，可以访问 Docker Engine；每 Run 容器不会继承该访问权。Run 容器必须使用
只读根文件系统、固定非 root 身份、专用输入/输出卷、内部 allowlist 或 deny-all 网络和有界资源。生产环境应将 Worker
部署到专用节点或改用 mTLS 远程执行服务/Kubernetes ServiceAccount，不要把 Docker Socket 暴露到普通业务容器。

Kubernetes 控制器示例见 `infra/kubernetes/m9.5.3-runner.yaml`。部署前必须替换示例 Registry 和镜像摘要，外部创建
`testops-worker-control-plane` Secret，并确认 Namespace 的 Pod Security Admission 与 CNI NetworkPolicy 确实生效。
`RUNNER_KUBERNETES_NETWORK_POLICY_ENFORCED=true` 是显式运维证明，不会自动探测 CNI；没有完成验证时必须保持 false，
Worker 将拒绝以 Kubernetes 模式启动。控制器使用 Namespace Role 和短期投影 Token；Run Pod 不继承控制器身份。
自行构建 Worker 时应安装 `.[kubernetes-executor,platform,runner]`；API 与其他控制面镜像不应安装
`kubernetes-executor` extra。
