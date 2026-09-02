# Runner Job Examples

`yanjia-login-smoke.json` 是从 `case-v1.0.1` 选择 7 条登录用例生成的不可变示例
Run Snapshot，固定到历史 Web Runner `0.1.0`。

`yanjia-home-search-smoke.json` 从同一已发布基线精确选择 `TC-HOME-002`，固定到 Web
Runner `0.2.0`，用于验收“独立登录 → 首页搜索不存在用户 → 展示暂无内容”的首个
登录后业务迁移切片。两个示例 URL 均使用不可路由的 `example.invalid`，默认只能用于
契约和 Runner 能力校验；可用 `python scripts/smoke_home_search.py` 对本地确定性靶站
执行真实 Chromium 验收。

执行真实环境前，应使用新的 Run ID 和真实目标 URL 生成新 Job，并通过进程环境注入：

- `TESTOPS_SECRET_TEST_USERNAME`
- `TESTOPS_SECRET_TEST_PASSWORD`

Job 只保存 `secret://` 引用，不保存这两个变量的值。
