# Worker

Worker 通过事务 Outbox 将控制面的 `run.queued`、`run.cancel_requested` 事件发布到
Celery。任务只接收不可变 Run Snapshot，站点操作仍封装在对应 Runner Adapter 中。

当前闭环包括：

- `run:<run_id>` 确定性任务 ID；
- JSON-only Celery 消息、late ack 和单任务预取；
- PREPARING/RUNNING 状态回调；
- Runner 进度事件实时回调，并在提交结果前从本地 `events.jsonl` 幂等补传；
- 在用例边界轮询持久化取消标记；
- 本地结果恢复和幂等结果重放；
- 本地制品 SHA-256/大小复核、MinIO 条件写上传和上传后元数据复核；
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
python -m celery -A testops.worker.celery_app:celery_app worker --loglevel=INFO --pool=solo
```

持续发布 Outbox：

```powershell
python scripts/dispatch_outbox.py --watch --interval-seconds 1
```

Worker 只解析 `TESTOPS_SECRET_<绑定名>` 形式的进程环境变量；真实密钥不得写入
Snapshot、日志、异常或制品名称。生产环境应使用仅限目标 Bucket 的专用 MinIO
服务账号，不能沿用本地 Root 凭据。
