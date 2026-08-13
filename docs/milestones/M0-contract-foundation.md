# M0：契约与工程基础

## 目标

在接入数据库、队列和前端之前，先稳定平台与 Web Runner 之间的边界，并消除旧
项目中执行器、校验器和 Excel 说明各自维护能力列表的问题。

## 完成条件

- [x] 当前目录建立平台单仓骨架；
- [x] 动作和断言注册表成为唯一事实来源；
- [x] Case Definition、Run Snapshot、Run Result 可验证；
- [x] JSON Schema 可重复生成；
- [x] API 提供健康检查、能力注册表和 Schema；
- [x] Web Runner 可从文件读取并验证不可变 Job；
- [x] 把现有 Excel 固化为 `case-v1.0.0` 并完成 92 条用例转换；
- [ ] 将旧项目执行入口改造成消费 Run Snapshot；
- [ ] 当前 8 项旧校验器能力漂移归零。

## 下一步

1. 将旧 `StepExecutor` 和 `AssertionExecutor` 包装进 `WebPlaywrightAdapter`；
2. 用登录模块完成第一条真实端到端 Runner Job；
3. 让 Runner 只消费 Run Snapshot，不再读取业务 Excel。
