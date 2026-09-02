# Web Playwright Runner

当前版本 `0.2.0` 支持登录模块，以及首个登录后业务切片 `TC-HOME-002`（首页搜索
不存在用户）。每条登录后用例使用独立浏览器 Context，并通过 Run 密钥绑定重新登录；
登录后可选的“知道了”弹窗不会阻塞业务步骤。Runner 只消费 JSON Run Snapshot，不读取
业务 Excel，不回写旧项目，也不加载旧项目 `.env`。

```powershell
$env:PYTHONPATH="packages/contracts/src;runners/web_playwright/src"
python -m testops.runners.web health
python -m testops.runners.web validate-job examples/jobs/yanjia-login-smoke.json
python -m testops.runners.web validate-job examples/jobs/yanjia-home-search-smoke.json
python scripts/smoke_home_search.py
```

真实执行需为 Job 使用新的 Run ID、真实 `base_url`，并在进程环境中注入
`TESTOPS_SECRET_TEST_USERNAME` 和 `TESTOPS_SECRET_TEST_PASSWORD`。运行制品写入
`--workspace-root/<run_id>`；已存在的 Run 目录会被拒绝复用。

`yanjia-login-smoke.json` 保留为 `0.1.0` 的历史不可变示例；新业务切片使用当前
`0.2.0` 包摘要。尚未迁移的首页跳转、顾客列表、顾客详情、影像、案例库和个人中心
模块仍会在 Job 校验阶段失败关闭。
