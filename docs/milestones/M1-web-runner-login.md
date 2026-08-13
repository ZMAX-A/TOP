# M1：Web Runner 登录模块纵向闭环

## 完成范围

- Run Snapshot 增加 Web 目标配置、普通变量和密钥绑定；
- 密钥值由 Runner 进程解析，Job 仅保存 `secret://` 引用；
- 登录模块支持 `input`、`click`；
- 支持 `text_contains`、`text_visible`、`url_contains`、`url_not_contains`；
- 每个 Run 使用独立目录，每条用例使用独立浏览器 Context；
- 失败时可采集截图和 Playwright Trace；
- 输出结构化 Case Result、Run Result、事件 JSONL 和制品元数据；
- 支持用例间的协作式取消；
- CLI 提供 `health`、`validate-job` 和 `execute-job`。

## 基线修复

`case-v1.0.0` 中 `TC-LOGIN-007` 使用 `url_contains('/')`，即使浏览器仍停留
在 `/login` 也会通过。Runner 会拒绝这种弱断言；`case-v1.0.1` 以父基线为只读
来源，将其改为 `url_not_contains('/login')`，其余 91 条用例及全部 Case UUID 不变。

## 密钥注入

真实执行前由任务运行环境设置：

- `TESTOPS_SECRET_TEST_USERNAME`；
- `TESTOPS_SECRET_TEST_PASSWORD`。

解析后的值不会进入进度事件；异常进入 Result 前还会执行值级脱敏。

## 验证边界

已完成无网络 headless Chromium 启动与渲染冒烟。未读取旧项目 `.env`，也未向
真实业务站点发送登录请求。`examples/jobs/yanjia-login-smoke.json` 使用
`example.invalid`，用于契约和调度联调；真实环境必须使用新的 Run ID 和目标 URL。

## 下一步

M2 将实现 PostgreSQL 项目/目标/基线/Run 持久化、Redis/Celery 调度，以及 API
创建 Run 后向 Runner 投递不可变 Job 的闭环。
