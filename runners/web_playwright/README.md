# Web Playwright Runner

当前版本 `0.1.0` 完成登录模块纵向闭环。Runner 只消费 JSON Run Snapshot，不读取
业务 Excel，不回写旧项目，也不加载旧项目 `.env`。

```powershell
$env:PYTHONPATH="packages/contracts/src;runners/web_playwright/src"
python -m testops.runners.web health
python -m testops.runners.web validate-job examples/jobs/yanjia-login-smoke.json
```

真实执行需为 Job 使用新的 Run ID、真实 `base_url`，并在进程环境中注入
`TESTOPS_SECRET_TEST_USERNAME` 和 `TESTOPS_SECRET_TEST_PASSWORD`。运行制品写入
`--workspace-root/<run_id>`；已存在的 Run 目录会被拒绝复用。
