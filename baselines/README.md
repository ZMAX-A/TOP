# Case Baselines

此目录保存已发布、不可变的用例定义基线。Excel 只作为只读迁移输入，不是 Runner
运行时的事实来源。

每个版本目录包含：

- `case-baseline.json`：完整且可验证的用例定义；
- `migration-audit.json`：字段规范化、兼容推导和警告；
- `manifest.json`：基线 UUID、版本、用例数以及文件 SHA-256。

同一版本只允许逐字节相同的幂等生成。任何用例或迁移规则变化都必须发布新版本，
不能覆盖现有目录。

当前版本：

- `case-v1.0.0`：旧 Excel 的忠实、可审计迁移结果；
- `case-v1.0.1`：从 v1.0.0 派生，仅把 `TC-LOGIN-007` 从弱
  `url_contains('/')` 改为 `url_not_contains('/login')`。
