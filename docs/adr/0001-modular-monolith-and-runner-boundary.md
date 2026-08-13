# ADR-0001：模块化控制面与独立 Runner

- 状态：Accepted
- 日期：2026-08-11

## 决策

MVP 控制面采用 FastAPI 模块化单体。高资源、易失败的浏览器任务通过稳定的
Run Snapshot 和 Run Result 契约交给独立 Web Runner，不把浏览器进程放入 API
服务，也不在首版拆分大量微服务。

现有 `../web` 项目迁移为 `WebPlaywrightAdapter`：页面对象和执行能力继续复用，
Excel 读取、结果回写、全局 `.auth_state.json` 和本地报告目录不进入 Runner 契约。

## 原因

- 治理服务与浏览器资源生命周期不同；
- 平台需要支持未来的 App 和 API Adapter；
- 模块化单体便于 MVP 内保持事务一致性；
- 契约先行可以在不重写旧页面对象的前提下逐步迁移。

## 后果

- 平台与 Runner 必须同时遵守版本化 JSON Schema；
- 每个 Run 都必须包含不可变的用例、脚本、环境和配置标识；
- Runner 不能直接访问平台业务数据库；
- 只有出现独立扩缩容、吞吐或团队边界需求时，才进一步拆分控制面服务。
