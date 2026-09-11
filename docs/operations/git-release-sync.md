# Web 自动化 Git Release 同步

TOP 的 Web 自动化更新链路以不可变 Release 为边界，不允许 Worker 在 Run 前执行
`git pull`。脚本和依赖固定到 OCI manifest digest，Excel 用例固定到独立 Case Baseline digest；
平台只读取 GitHub Release 中的 `testops-release-manifest.json`，先预览差异，再创建治理草稿。

## 发布端

Web 仓库包含两个入口：

- `.github/workflows/testops-release.yml`：标签触发离线框架检查、Excel 规范化、Runner
  镜像构建、SBOM/provenance、Cosign OIDC 签名和 GitHub Release 资产发布。
- `scripts/build_testops_release_manifest.py`：把规范化 Case Baseline、标签提交 SHA 和最终
  OCI digest 合成为平台可校验的清单。

完整 Runner 包发布标签必须使用 `v<major>.<minor>.<patch>`，并与 Web 仓库的 `VERSION` 完全
一致；Excel-only Release 使用对应的 `case-v<major>.<minor>.<patch>` 基线标签。Web
脚本/镜像版本与 TOP 用例基线版本是两条独立序列：`TESTOPS_BASELINE_VERSION` 保存下一版
Case Baseline，例如 Web 从 `v1.0.0` 升到 `v1.0.1` 时，平台已有 `case-v1.0.1`，本次清单可
使用 `case-v1.0.2`。有用例变化时基线版本必须高于平台当前 Released 基线，不能复用已有版本。
工作流固定了 `TESTOPS_REVISION`，升级平台迁移器或 Runner 适配器时应通过单独评审更新该提交
SHA，不能改成 `main`。

### Excel-only 快车道

当提交只修改 Excel 用例和 `TESTOPS_BASELINE_VERSION`，且没有修改 `pages/`、`tests/`、
`utils/`、`test_assets/`、依赖或调度入口时，Release Manifest 应复用当前 ACTIVE 自动化包的
精确 `name + version + digest + image_repository`。TOP 会复用已有包记录和供应链验证，不创建
新的 PENDING 包，也不重复构建、扫描或签名镜像。

该模式仍必须生成不可变 Git 标签和 Release 资产，完成语义差异、审核和正式发布前的全量回归。
CI 必须验证从被复用包的源 revision 到当前 revision 仅存在允许的 Excel-only 变更；若脚本、
依赖或测试资源发生变化，必须退出快车道并走完整 Runner 包发布。

创建并推送标签后，Release 至少应包含：

- `testops-release-manifest.json`
- `case-baseline.json`
- `migration-audit.json`
- `manifest.json`

`testops-release-manifest.json` 不得包含账号、密码、Cookie、`.env` 内容或其他运行时秘密。

## 平台端

项目管理员进入“自动化包”页面，选择 Web 目标并点击“从 Git 标签同步”：

1. 输入 `https://github.com/ZMAX-A/web` 和发布标签。
2. 点击“预览并校验”。API 会解析标签到最终 commit，下载指定 Release 资产，校验仓库、标签、
   commit、Case Baseline canonical digest 和 OCI digest，并显示新增、修改、删除用例。
3. 点击“创建草稿”。有用例差异时创建精确的 DRAFT Case Baseline 和 Change Request；无用例
   差异时沿用当前 Released 基线。包坐标发生变化时创建 PENDING 自动化包草稿；包坐标与已有
   记录完全一致时复用现有包。

该操作不会直接发布基线或激活包。后续顺序是：

1. 可信 CI 验证器核验镜像签名、透明日志、provenance 和 SBOM，并提交供应链准入结果。
2. 有用例变化时提交 Change Request，完成评审、候选验证和全量回归，再发布基线。
3. 使用最终 Released 基线完成自动化包全量验证，再激活该不可变 digest。

如果 Manifest 精确引用平台中已经存在的 ACTIVE 包，则第 1、3 步复用该包已有证据，不创建或
重新激活自动化包；候选基线仍按第 2 步治理。启用 Snapshot Excel Runner 后，Worker 会在每个
Run 的隔离目录中根据 `RunSnapshot.cases` 生成临时 Excel，并记录基线摘要和运行时工作簿摘要，
镜像内 Excel 仅作为结构与样式模板，不再作为执行数据源。

公共仓库无需 GitHub Token。私有仓库或需要提高 API 配额时，只在 API 服务环境设置
`GITHUB_RELEASE_TOKEN`；不要写入前端、Release 清单或仓库。还可配置
`GITHUB_RELEASE_TIMEOUT_SECONDS`（1–60 秒）和
`GITHUB_RELEASE_MAX_MANIFEST_BYTES`（1024–10000000 字节）。

供应链策略应精确允许：

- 源仓库：`https://github.com/ZMAX-A/web`
- 证书身份：该仓库 `.github/workflows/testops-release.yml` 的实际 GitHub OIDC workflow identity
- 证书颁发者：`https://token.actions.githubusercontent.com`

不要把仓库允许范围扩大为组织通配，也不要在平台 UI 中提供“跳过验证”开关。
