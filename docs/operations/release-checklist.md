# 生产发布验收清单

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

## 数据与恢复

- [ ] 发布前完整备份通过 `backup_restore.py verify`；
- [ ] 备份副本位于独立故障域，并记录清单 SHA-256、保留期和负责人；
- [ ] 最近一次联合恢复演练在计划周期内，实际 RPO/RTO 满足业务目标；
- [ ] Alembic 只有一个 head，真实 PostgreSQL 已完成升级/回滚/再升级演练；
- [ ] 回退方案使用上一个应用镜像与已验证备份，不依赖临时修改数据库。

## 容量、监控与验收

- [ ] PostgreSQL、Redis、MinIO 和 Runner 工作区容量满足发布窗口与保留周期；
- [ ] API 可用性、数据库就绪、5xx 和 p95 延迟告警已接入接收器并测试；
- [ ] Outbox、Worker、Run 终态率和制品上传有值班查询与处置步骤；
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
