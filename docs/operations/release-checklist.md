# 生产发布验收清单

## 2026-08-20 本机预验收证据

- 全量 Pytest 93 项通过，仓库标准入口 68 项通过；Ruff 检查通过，140 个 Python 文件格式一致；
- Schema 导出检查和不可变基线/Runner 制品摘要检查通过；
- Vue 严格 TypeScript 检查和 Vite `0.21.0` 生产构建通过；
- Python、Playwright、Node、Nginx、PostgreSQL、Redis、MinIO、MinIO Client 和 Prometheus 默认镜像引用均固定到
  不可变摘要；隔离构建得到 API `e2997386b128`、Worker `17187aca2814`、Frontend `5ce6eddee8f3` 和
  Smoke Target `8ff1c6bcea86` 镜像 ID；
- 隔离渲染配置不含 `change-me-*`，空 `BOOTSTRAP_ADMIN_TOKEN` 使 Bootstrap 返回 503；Nginx 对超过 1 MiB
  请求返回 413 且不暴露版本号；`/metrics` 无凭据返回 401、有效 Bearer 返回 200，前端不代理该端点；
- Alembic 唯一 Head 为 `20260817_0015`，完整离线升级 SQL 生成通过；隔离 PostgreSQL 17 已完成
  `upgrade head -> downgrade base -> upgrade head` 往返并回到该 Head；
- M8.3 自动评估测试覆盖触发、升级、降级、恢复、无数据保持、冷却抑制、到期重触发和重复评估不重复入队；
- M8.4 人工处置测试覆盖确认/取消确认、状态变化清除旧确认、静默校验、静默期间抑制投递、到期补发和操作审计；
- M8.5 人工重放测试覆盖状态与原因校验、当前配置目标、重复抑制、投递链、事件 ID 幂等和审计脱敏；
- M8.6 测试覆盖到期评估、活跃静默、PENDING/FAILED、最老等待、重放计数及静默/确认/重放操作人显示名；
- Prometheus 告警规则 YAML 共 11 条，容器内 `promtool check config` 与 `promtool check rules` 均通过；
- 隔离 Compose `0.21.0` 全栈健康，普通冒烟和 Outbox/Worker 中断恢复冒烟均通过；两次成功 Run 均为
  8 个事件、1 个制品，两次预期失败 Run 均为 8 个事件、3 个制品，所有短时下载和摘要校验通过；
- 浏览器验收覆盖质量运营页、evaluator 最近/下次评估、Webhook 测试事件、SSRF 私网阻断、FAILED 人工重放、
  原始/子投递链和操作人显示名；`/metrics` 的 M8 质量指标可查询且质量快照成功值为 `1`；
- 隔离联合备份通过 `backup_restore.py verify`：PostgreSQL 数据库、4 个对象约 328 KiB；`manifest.json` SHA-256
  为 `61ef4eb4026694d473035495c47d3d78437b5732eeef3d7646c78447feda0f10`。恢复到全新 PostgreSQL/MinIO 后
  Alembic 为 `20260817_0015`，用户/项目/Run/制品数为 1/1/2/4，随机制品下载摘要一致，实测 RTO 为 20.097 秒；
- 本地测试 CA 的 HTTPS 接收器完成真实 TLS/HMAC 验收：投递 `c835c70b-cc86-411e-a490-64e610da1149` 一次成功、
  返回 204，接收端校验签名、事件类型和投递 ID；数据库明文签名密钥行数为 0；
- 两个隔离项目的 PostgreSQL 卷约 48.3–48.4 MiB，宿主 C 盘剩余 775.75 GiB；18 个隔离容器日志对 16 个
  敏感配置值扫描命中 0；
- Docker Scout 因未登录不可用，Trivy 官方镜像和 Windows 包均因外网超时未取得，因此漏洞扫描正式门禁保持未完成；
- 本节仅记录隔离本机证据，不替代 GitHub Actions、非提交人代码评审、异故障域备份、生产容量预测、真实外部
  HTTPS 告警接收器和最终发布批准。

## 合并门禁

- [ ] `Python quality and contracts` 通过；
- [ ] `Frontend typecheck and build` 通过；
- [ ] `PostgreSQL migration round trip` 通过；
- [ ] `Full Compose smoke` 通过且失败日志已检查；
- [ ] `Prometheus configuration` 通过；
- [ ] PR 已完成至少一名非提交人的代码评审。

## 配置与安全

- [ ] 所有默认口令、回调 Token、Bootstrap Token 和对象存储 Root 凭据已替换；
- [ ] Bootstrap 管理员完成后已移除 `BOOTSTRAP_ADMIN_TOKEN`；
- [ ] Runner 只获得所需密钥引用，日志与制品抽检无明文密钥；
- [ ] API、对象存储和数据库启用 TLS，入口限制 CORS 与请求体大小；
- [ ] MinIO 使用独立读写/备份账号，Bucket 版本控制和保留策略已启用；
- [ ] 应用和基础镜像已固定到验收过的不可变摘要并完成漏洞扫描；
- [ ] `/metrics` 不经公网前端代理，Bearer 凭据由受限文件提供。
- [ ] 质量 Webhook 只允许批准的 HTTPS 目标，签名密钥仅注入 dispatcher，出口层已配置域名允许清单；
- [ ] 告警确认和静默接口仅授权项目管理员，静默原因、确认说明及解除操作均能在审计日志中追溯；

## 数据与恢复

- [ ] 发布前完整备份通过 `backup_restore.py verify`；
- [ ] 备份副本位于独立故障域，并记录清单 SHA-256、保留期和负责人；
- [ ] 最近一次联合恢复演练在计划周期内，实际 RPO/RTO 满足业务目标；
- [ ] Alembic 只有一个 head，真实 PostgreSQL 已完成升级/回滚/再升级演练；
- [ ] 回退方案使用上一个应用镜像与已验证备份，不依赖临时修改数据库。

## 容量、监控与验收

- [ ] PostgreSQL、Redis、MinIO 和 Runner 工作区容量满足发布窗口与保留周期；
- [ ] API 可用性、数据库就绪、5xx 和 p95 延迟告警已接入接收器并测试；
- [ ] Outbox、Scheduler、Reaper、Worker、Run 终态率和制品上传有值班查询与处置步骤；
- [ ] 质量 Webhook PENDING/FAILED 投递、重试次数和签名密钥缺失有值班查询与处置步骤；
- [ ] 质量快照不可用、evaluator 延迟和 Webhook 最老 PENDING 年龄告警已接入接收器并完成测试；
- [ ] 静默截止、解除后补发、状态变化清除旧确认和 evaluator 停机恢复已纳入值班演练；
- [ ] 派发积压、计划延迟、失联 Worker 租约告警已接入接收器并完成测试；
- [ ] 成功与预期失败两条 Compose 冒烟 Run 均完成，制品摘要和短时 URL 正确；
- [ ] 人工验证登录、RBAC、项目设置、审批发布、运行运营和审计查询；
- [ ] 发布负责人、值班人、变更窗口和停止条件已确认。

## 发布证据

- [ ] GitHub Actions 运行链接；
- [ ] Git 提交与镜像摘要；
- [ ] 数据库迁移前后版本；
- [ ] 备份路径与 `manifest.sha256`；
- [ ] 冒烟 Run ID 与制品摘要；
- [ ] 指标/告警截图和最终批准人。
