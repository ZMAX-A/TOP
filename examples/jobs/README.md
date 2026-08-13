# Runner Job Examples

`yanjia-login-smoke.json` 是从 `case-v1.0.1` 选择 7 条登录用例生成的不可变示例
Run Snapshot。示例 URL 使用不可路由的 `example.invalid`，因此默认只能用于契约校验。

执行真实环境前，应使用新的 Run ID 和真实目标 URL 生成新 Job，并通过进程环境注入：

- `TESTOPS_SECRET_TEST_USERNAME`
- `TESTOPS_SECRET_TEST_PASSWORD`

Job 只保存 `secret://` 引用，不保存这两个变量的值。
