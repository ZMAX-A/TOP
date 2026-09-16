# YanJia Android Appium 接入 TOP

本文描述 `yanjia-android-appium` 的正式接入边界。TOP 控制平面保存用例基线、不可变运行快照、进度与结果；Windows 设备 Worker 保留 ADB、Appium、应用和本机 Python 环境。平台不会读取 APP 项目的 `.env`，也不会把账号、密码或客户手机号写入基线和运行包。

## 架构与约束

- 目标类型：`APP`
- Runner 类型：`ANDROID_APPIUM`
- 自动化包：`yanjia-android-appium@1.0.0`
- 执行方式：Windows Worker 的 `IN_PROCESS` 控制器启动独立 PowerShell/Pytest 子进程
- 用例来源：`test_case.xlsx` 的 `自动化测试用例` 与 `自动化执行步骤`
- 结果来源：每次运行工作区中的 Allure JSON、Appium 日志和控制台日志
- 并发：一个设备 Worker 固定 `RUNNER_MAX_SLOTS=1`
- 重试：首轮结束后等待 30 秒，仅复跑失败且带 `readonly`、不带 `mutating`、`destructive`、`persistent`、`no_retry` 的用例

APP 设备依赖不能放入 Linux Web Worker。Android Worker 必须运行在已连接且已授权目标设备的 Windows 主机上，使用单独的 APP Runner Pool 和队列。

## 1. 生成不可变用例基线

迁移器只读源工作簿，不复制工作簿内容；TOP 基线中的每条用例只包含一个受管 `execute_excel_case` 操作和原始用例编号。

```powershell
python scripts/migrate_android_excel.py `
  --source "C:\path\to\APP\test_case.xlsx" `
  --project-key yanjia-ai-android `
  --version case-v1.0.0 `
  --output baselines/yanjia-ai-android/case-v1.0.0
```

发布前检查 `manifest.json`、`migration-audit.json` 和 `case-baseline.json`，再通过 TOP 基线 API 创建并发布该基线。源工作簿发生任何业务变化时必须生成新的 `case-vX.Y.Z`，不能覆盖已发布目录。

## 2. 计算运行包摘要

摘要只覆盖运行所需的白名单文件和 Android 适配器；`.env`、`.venv`、Git 历史、报告和输出目录不在包内。

```powershell
python scripts/package_android_appium.py --source "C:\path\to\APP"
```

如需审查实际包内容，导出到 APP 项目之外的空目录：

```powershell
python scripts/package_android_appium.py `
  --source "C:\path\to\APP" `
  --export-to "C:\temp\yanjia-android-appium-1.0.0"
```

在 TOP 的 APP 目标下创建自动化包草稿：

- name：`yanjia-android-appium`
- version：`1.0.0`
- runner_type：`ANDROID_APPIUM`
- image_repository：`local/yanjia-android-appium`
- digest：上一步输出的完整 `sha256:...`

按现有供应链流程提交摘要、provenance 和 SBOM 验证。Worker 启动时会重新计算已挂载 APP 源码的摘要；不一致时拒绝执行。

## 3. 创建 APP 目标、资源池和环境

1. 在项目设置中创建 `APP` 目标，不填写浏览器。
2. 创建只支持 `APP` 的 Runner Pool，例如 key `android-local`、queue `testops.pool.android-local`，并设置最大并发为 1。
3. 创建 APP 环境并绑定该 Pool；APP 环境不设置 `web_config`。
4. 环境至少绑定以下 Secret Ref：
   - `YANJIA_USERNAME`
   - `YANJIA_PASSWORD`
5. 执行 `TC-HOME-007` 或任何写入/删除用例时再绑定：
   - `SEEDED_CUSTOMER_PHONE`
6. 执行专用顾客写入/删除用例时再绑定：
   - `YANJIA_MUTATION_CUSTOMER_QUERY`

设备、服务地址等非敏感值可作为环境变量绑定：`ANDROID_UDID`、`APPIUM_SERVER_URL`、`APPIUM_STARTUP_TIMEOUT_SECONDS`、`YANJIA_APP_PACKAGE`。写入用例还必须显式设置 `YANJIA_ALLOW_MUTATION=true`；删除用例额外设置 `YANJIA_ALLOW_DESTRUCTIVE=true`。

## 4. 启动 Windows 设备 Worker

先在 Worker 进程环境中配置 TOP 控制平面、Redis、制品存储和 Secret 值。Secret 使用 `TESTOPS_SECRET_<绑定名>`，例如 `TESTOPS_SECRET_YANJIA_USERNAME`；脚本只检查变量是否存在，不显示值。

```powershell
.\scripts\run-android-worker.ps1 `
  -AppProjectRoot "C:\path\to\APP" `
  -WorkerKey android-local-01 `
  -PoolKey android-local `
  -QueueName testops.pool.android-local
```

启动器会：

- 重新计算白名单包摘要并注册精确摘要能力；
- 将 Worker 声明为仅支持 `APP`；
- 固定 `IN_PROCESS`、单槽位和最长 24 小时执行窗口；
- 保持控制台可见，以便观察 Celery、pytest、ADB 和 Appium 状态；
- 把 APP 根目录传给适配器，但不加载 APP `.env`。

## 5. 发起与验收运行

在“运行运营”中选择 APP 目标、APP 环境、Released Android 基线和 ACTIVE Android 包。APP 运行不会显示浏览器有头/无头选项。

至少完成以下验收：

1. 只读单例 `TC-HOME-001`：TOP 显示实时状态，最终为 PASSED，并可下载 Allure/日志。
2. 种子单例 `TC-HOME-007`：Secret 绑定正确时通过，缺失时在执行前被拒绝。
3. 可恢复写入单例：未设置授权变量时被拒绝；授权后先完成唯一客户预检，再执行并恢复。
4. 失败重试：只读失败在 30 秒后复跑一次；写入、删除和持久化用例不自动复跑。
5. 全量：Worker 单槽位执行所有启用用例，TOP 的最终 CaseResult 数与快照完全一致。

平台执行固定使用 `-NoWriteBack -SkipExcelValidation -IgnoreDotEnv`。TOP 的结构化 CaseResult 是平台记录；设备 Worker 不修改源 `test_case.xlsx`，避免测试结果回写改变不可变包摘要。
