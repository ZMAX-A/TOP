# M6：发布加固

## 完成范围

- FastAPI 控制面增加独立 Prometheus Registry、进程/GC/平台指标、构建信息、数据库
  就绪 Gauge，以及按 HTTP 方法、路由模板和状态聚合的请求计数与延迟直方图；
- `/metrics` 支持可选 Bearer Token、禁止缓存，且 Compose 前端 Nginx 不代理该路径；
- Compose 增加可选 `observability` profile，Prometheus 使用 15 秒抓取与评估周期、15 天
  本地保留，并加载 API 可用性、数据库就绪、5xx 比例和 p95 延迟规则；
- GitHub Actions 建立 Python、前端、真实 PostgreSQL 迁移往返、完整 Compose 冒烟和
  Prometheus 配置五类任务；合并任务只授予仓库内容读取权限；
- 定时任务额外演练 Outbox/Worker 中断恢复，Compose 失败日志保留 14 天；
- Dependabot 每周检查 GitHub Actions、Python 和前端依赖；
- `backup_restore.py` 创建不可覆盖的 PostgreSQL/MinIO 联合备份，校验清单自身及每个
  文件的 SHA-256，并以显式目标确认和对象冲突预检保护恢复；
- 增加生产运行手册和发布清单，覆盖升级、备份、隔离恢复、监控、故障处置和证据留存。

## 安全与可运维约束

- HTTP 指标只使用框架路由模板作为标签，不把 Run、用户、项目等高基数字段写入
  Prometheus；每个应用实例拥有独立 Registry，应用工厂和测试不会重复注册；
- `METRICS_TOKEN` 配置后只接受恒定时间比较通过的 Bearer 凭据；本地无 Token 模式
  只允许在可信 Compose 网络使用；
- 数据库密码只通过 `PG*` 环境传给 `pg_dump`/`pg_restore`，不会出现在子进程参数、
  备份清单或 JSON 报告中；
- 备份目标目录必须不存在，中断目录保留 `.incomplete` 并禁止验证/恢复；清单路径必须
  保持在备份根目录内，拒绝绝对路径、反斜杠和 `..`；
- 恢复必须同时提供 `--replace-database` 和与 `DATABASE_URL` 数据库同名的
  `--confirm-database`；冲突对象默认拒绝覆盖；
- PostgreSQL downgrade 只在 CI 临时数据库中演练。生产回退使用受控备份，不把
  downgrade 当作在线回滚手段。

## 验证记录

- 全量 Python 测试 67 项通过，包含指标鉴权/路由模板/数据库就绪、备份篡改、清单路径
  逃逸、数据库凭据隔离和 Worker SHA-256 元数据兼容性；
- Ruff、契约 Schema、不可变基线/Job 制品和前端严格构建纳入本地与 CI 双重门禁；
- GitHub Actions、Dependabot、Compose、Prometheus 配置和告警规则通过本地 YAML
  解析；Prometheus `promtool` 校验由 Linux CI 容器执行；
- Alembic 离线 SQL和单 head 本地通过；真实 PostgreSQL 在 CI 执行
  `upgrade head → downgrade base → upgrade head`；
- 当前开发机没有 Docker/Podman，因此新增 Compose profile、Prometheus 容器、真实
  PostgreSQL 往返、联合备份和恢复仍需在 GitHub/部署环境首次执行并保存证据。

## 下一步

M7 可进入规模化执行与质量分析：调度配额、Runner 池隔离、定时回归、失败聚类、趋势
看板和按项目的容量/SLO 管理；开始前先完成 M6 发布清单中的首次环境验收。
