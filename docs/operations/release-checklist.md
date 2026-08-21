# 生产发布验收清单

## 2026-08-21 M9.6.1 增量预验收证据

- 全量 Pytest 133 项通过、2 项因仓库外旧工作簿跳过；仓库标准入口 102 项通过；Ruff 检查通过，167 个 Python 文件
  格式一致；Schema 导出和不可变基线/Runner 制品摘要校验通过；
- 配置、认证和控制面测试覆盖 Ed25519 公钥环解析、HTTPS/SPIFFE 工作负载身份、重复 credential/公钥拒绝、双 key
  轮换、未知或错误 key、身份替换、过期/未来超限时间、nonce 重放、业务幂等及历史 HMAC 显式兼容开关；
- 新请求使用 `testops-supply-chain-envelope-v2` compact detached JWS，protected header 固定为 EdDSA、credential key id
  和专用 typ；API 只保存公钥、身份、算法、profile 与公钥指纹，私钥不进入控制面、数据库、Worker 或前端；
- RC1 将 v2 字节级协议提取为 API/CI 共用 contracts，并新增可信验证器 CLI；测试覆盖服务端实际接受客户端签名、报告
  契约校验、HTTPS/loopback 默认边界、拒绝重定向、身份不匹配、非规范 nonce、PKCS8/原始密钥读取及 dry-run 不输出
  签名或私钥材料；
- Alembic 唯一 Head 为 `20260821_0019`，完整离线升级和 `0019 -> 0018` 降级 SQL 生成通过；隔离 PostgreSQL 17
  从空库升级到 `20260821_0019`，历史行兼容约束和新 Ed25519 证据约束均由测试覆盖；
- Vue 严格 TypeScript 与 Vite `0.30.0` 生产构建通过；自动化包工作台展示签名算法、Envelope profile、工作负载
  身份和公钥 SHA-256 指纹；
- 独立 `testops-m961-verify` Compose 栈所有服务正常；API/Worker 镜像均含 `cryptography 50.0.0`，API 不含
  Kubernetes SDK、Worker 含 Kubernetes SDK；旧 `testops-m953-verify` 演示栈未改动；
- RC1 重建后普通 Smoke 的成功/预期失败 Run 为 `2794642a-b72f-44c5-94b6-a74997dbf546` /
  `c6b7cf5c-2e0c-47c3-9727-c2c8390941cd`，恢复 Smoke 为 `ae5a0882-5899-4c29-ae95-43220c597393` /
  `086445c0-b3b0-4fd9-9b3e-19de129a0e55`；每条 Run 均有 8 个事件，成功/失败路径分别验证 1/3 个制品及下载摘要；
  容器运行镜像 ID 为 `sha256:1abe5621e7a8683ae3c0aaae61fc4836fa02ce353528116da8bcd4414fc36f69`；镜像内
  验证器 CLI `--help` 通过，最终一分钟
  日志无问题模式命中，`compose-web-1` 心跳为 ACTIVE/新鲜状态，受管 Run 容器和卷均零残留；
- 本阶段固定“公钥 + 声明的工作负载身份”，尚不等同于 OIDC/SPIFFE 联邦身份验证。生产仍须完成真实 CI 私钥保护、
  TLS 入口、旧/新公钥并存与撤销、真实 Cosign/SLSA/SBOM 请求和审计演练；真实 Kubernetes 集群验收阻断项也仍然
  存在。本机新库升级和离线降级 SQL 不能替代生产 PostgreSQL 备份后的升级/回滚/再升级门禁。

## 2026-08-21 M9.5.3 增量预验收证据

- 全量 Pytest 125 项通过、2 项因仓库外旧工作簿跳过；仓库标准入口 100 项通过；Ruff 检查通过，162 个 Python 文件
  格式一致；Schema 导出和不可变基线/Runner 制品摘要校验通过；
- Kubernetes 单元、契约和控制面测试覆盖显式 CNI NetworkPolicy 运维证明、非默认 ServiceAccount、Token 自动挂载关闭、
  immutable Snapshot ConfigMap、最小 Secret、Job/Pod SecurityContext、CPU/内存/临时存储 requests=limits、镜像摘要
  回读、NetworkPolicy selector/CIDR、结果导出、安全 Tar 导入、异常清理、Worker 路由和调度优先级；
- Namespace 部署模板包含 Restricted Pod Security Admission、控制器/Run 双 ServiceAccount、Namespace Role/RoleBinding、
  短期投影控制器 Token 和外部 Secret 引用；不包含 ClusterRole、Docker Socket、hostPath 或明文控制面凭据；
- Kubernetes SDK 只安装在 Worker 的 `kubernetes-executor` extra；真实镜像检查确认 API 中不存在 `kubernetes` 模块、
  Worker 中存在，避免扩大控制面镜像攻击面；
- Vue 严格 TypeScript 与 Vite `0.29.0` 生产构建通过；运行详情展示 Namespace、ServiceAccount、Token 自动挂载状态和
  临时存储限额，Worker 管理页展示 Kubernetes 身份边界；
- 独立 `testops-m953-verify` Docker Compose 栈所有服务正常，普通及 Outbox/Worker 中断恢复 Smoke 均通过；每组成功/
  预期失败 Run 各 8 个事件、1/3 个制品，下载摘要一致，容器运行镜像 ID 为
  `sha256:fcaf9823771fe25891222b00fe4f436c79d6d8ef601fcde2afba59dec96efb1b`；近期错误日志无命中，受管
  Run 容器和卷均零残留；
- 本机没有 `kubectl` 或可用 Kubernetes Context，尚未执行真实 API Server、CNI NetworkPolicy、RBAC、调度、Pod
  Security Admission、正常/失败/取消/超时、控制器重启及孤儿资源回收验收。因此 M9.5.3 代码已实现，但真实集群验收
  仍是发布阻断项；Docker Smoke、Mock 和 YAML 检查不能替代该门禁；
- 本阶段不改变数据库结构，Alembic Head 仍为 `20260820_0018`；Python、API、Worker、Compose、Smoke 和前端发布
  元数据统一为 `0.29.0`。

## 2026-08-21 M9.5.2 增量预验收证据

- 全量 Pytest 120 项、仓库标准入口 94 项通过；Ruff 检查通过，159 个 Python 文件格式一致；Schema 导出和不可变
  基线/Runner 制品摘要校验通过；
- 容器执行单元与契约测试覆盖最小凭据环境、完整容器能力门禁、只读 Snapshot 输入卷、独立工作区卷、安全 Tar 导入、
  镜像标签与包摘要匹配、结果证据、冲突资源失败关闭、成功/异常清理和 Worker 路由；
- Vue 严格 TypeScript 检查和 Vite `0.28.0` 生产构建通过；运行详情展示不可变运行镜像和内存/CPU/PID 限额，Worker
  管理页展示执行模式、网络、只读根和资源能力；
- Python、API、Worker、Compose、Smoke 和前端发布元数据统一为 `0.28.0`；本阶段不改变数据库结构，Alembic Head
  仍为 `20260820_0018`；
- 独立 `testops-m952-verify` Compose 栈使用 Docker Engine 29.7.2 完成启动后实际参数回读自校验；普通及
  Outbox/Worker 中断恢复 Smoke 均通过，每组成功/预期失败 Run 各有 8 个事件、1/3 个制品，下载摘要一致；
- 两组 Result 均记录 `CONTAINER`、`RUN_SECRETS_ONLY`、只读根、ALLOWLIST、1 GiB、1000 millicores、256 PID 和
  不可变镜像 ID `sha256:8d2357bc5a1ffe3cfefc9bc18ac9e5d07dcf5b6988cefe032fa19ef05731dece`；恢复后栈全部
  服务健康，近期错误日志扫描无命中，一次性受管 Run 容器和卷均为零残留；
- Compose 中父 Worker 挂载 Docker Socket 是本地单节点的高信任执行控制边界，Run 容器未获得该挂载。生产发布仍需
  独立受控执行节点、最小化控制器权限，并迁移到 mTLS 远程执行服务或 Kubernetes 专用 ServiceAccount。

## 2026-08-20 M9.5.1 增量预验收证据

- 全量 Pytest 113 项、仓库标准入口 87 项通过；Ruff 检查通过，156 个 Python 文件格式一致；
- 隔离单元测试覆盖子进程只注入 Snapshot 明确绑定的测试密钥，排除数据库、Redis、控制面回调、MinIO 及其他
  Run 密钥；覆盖独立 HOME/TEMP、增量事件补传、Result 证据持久化和不夸大硬隔离能力；
- Worker 测试确认 `SUBPROCESS` 模式的父进程不构造 Adapter；控制面集成测试确认缺少独立进程或最小凭据能力时
  保持 WAITING 并返回 `RUNNER_ISOLATION_UNAVAILABLE`，包和浏览器准入仍失败关闭；
- Schema 导出、不可变基线/Runner 制品摘要、发布/部署契约及 Alembic `0018` Head 和离线
  `0018 -> 0017` 降级 SQL 通过；本阶段没有数据库迁移；
- Vue 严格 TypeScript 检查和 `0.27.0` 生产构建通过；运行详情明确展示隔离证据及未强制的只读根文件系统、
  网络和资源限制，Worker 管理页展示心跳隔离能力；
- Python、API、Worker、Compose、Smoke 和前端元数据统一为 `0.27.0`；独立 `testops-m951-verify` Compose 栈所有
  服务健康，API/Worker/Frontend 镜像清单摘要分别以 `43c75dc3d0f3`、`337c0fdaadab`、`b7f53c43a3f1` 开头；
- 真实 PostgreSQL 17 完成 `0018 -> 0017 -> 0018` 往返；普通及 Outbox/Worker 中断恢复 Smoke 均通过，每组
  成功/预期失败 Run 各有 8 个事件、1/3 个制品，下载摘要一致，隔离证据为 `SUBPROCESS`、`0.27.0` 和
  `RUN_SECRETS_ONLY`；每 Run 容器/Pod、只读根文件系统、网络策略及资源限制仍是 M9.5.2 门禁。

## 2026-08-20 M9.4.2 增量预验收证据

- 全量 Pytest 104 项、仓库标准入口 79 项通过；Ruff 检查通过，152 个 Python 文件格式一致；
- 专用验证器集成测试覆盖无签名用户会话、未知 credential、错误 HMAC、过期和未来超限时间、服务身份不匹配、
  同 nonce 重放、同报告新 nonce 幂等、Envelope 查询及自动化审计；
- keyring 单元测试覆盖默认失败关闭、双 key 轮换、至少 32 字节密钥和重复 credential 拒绝；部署契约确认 HMAC
  keyring 只进入 API 环境，未进入控制面公共锚点或 Worker，且未挂载 Docker Socket；
- Schema 导出检查、不可变基线/Runner 制品摘要检查及发布/部署契约测试通过；Vue 严格 TypeScript 检查和
  `0.26.0` 生产构建通过，工作台可查看 credential、nonce、签发/接收时间、请求和签名摘要；
- Python、API、Worker、Compose、Smoke 和前端元数据统一为 `0.26.0`；Alembic 唯一 Head 为
  `20260820_0018`，全量离线升级及 `0018 -> 0017` 离线降级 SQL 生成通过；
- 本机没有 Docker CLI，未执行真实 PostgreSQL 往返、Compose 全栈 Smoke、生产 TLS/随机密钥轮换或真实
  Cosign/SLSA/SBOM CI 请求；这些仍是发布门禁。

## 2026-08-20 M9.4.1 增量预验收证据

- 全量 Pytest 103 项、仓库标准入口 78 项通过；Ruff 检查通过，149 个 Python 文件格式一致；
- 供应链准入集成测试覆盖 PENDING/REJECTED 失败关闭、镜像摘要和证书身份不匹配、VERIFIED 报告幂等、历史记录、
  Run Snapshot 证据冻结、ACTIVE 后重新拒绝、新执行/计划/重跑阻断、System Admin 权限及审计；
- Schema 导出检查、不可变基线/Runner 制品摘要检查及发布/部署契约测试通过；五类允许清单在 Compose 中显式传递，
  空清单默认失败关闭，Worker 未挂载 Docker Socket；
- Vue 严格 TypeScript 检查和 `0.25.0` 生产构建通过；自动化包工作台显示准入状态、判定历史及签名 Bundle、
  provenance、SBOM、证书身份、builder 和源码修订证据；
- Python、API、Worker、Compose、Smoke 和前端发布元数据统一为 `0.25.0`；Alembic 唯一 Head 为
  `20260820_0017`，全量离线升级及 `0017 -> 0016` 离线降级 SQL 生成通过；
- 本机没有 Docker CLI，尚未执行真实 PostgreSQL 升级/降级/再升级往返和 Compose 全栈 Smoke；也未使用真实
  Cosign/SLSA/SBOM 制品执行 CI 验证。本节只证明控制面准入与失败关闭逻辑，不替代这些生产门禁。

## 2026-08-20 M9.3 增量预验收证据

- 全量 Pytest 100 项、仓库标准入口 75 项通过；Ruff 检查通过，146 个 Python 文件格式一致；
- 自动化包生命周期集成测试新增按包精确查询关联 Run 的断言，既有测试继续覆盖草稿、验证、激活、弃用、吊销、
  ACTIVE 回归计划保护和审计闭环；
- Vue 严格 TypeScript 检查和 `0.24.0` 生产构建通过，工作台路由、只读/管理权限、不可变包表单、关联 Run 与
  生命周期操作均进入独立生产分包；
- Python、API、Worker、Compose、Smoke 和前端发布元数据统一为 `0.24.0`；Alembic Head 仍为
  `20260820_0016`，M9.3 不改变数据库结构；
- 本节是 M9.3 UI/API 增量验证，不替代生产 Registry 凭据、OCI 签名/provenance、镜像扫描、隔离执行器和非提交人
  评审门禁。

## 2026-08-20 M9.2 增量预验收证据

- 全量 Pytest 100 项、仓库标准入口 75 项通过；Ruff 检查通过，145 个 Python 文件格式一致；
- 自动化包目录与调度测试覆盖精确摘要、非法/重复/超限目录、空目录拒绝、浏览器能力不匹配、包不可用等待原因和
  Worker Adapter 前二次失败关闭；
- Schema、不可变基线和 Runner Job 摘要检查通过；Vue 严格 TypeScript 检查和 `0.23.0` 生产构建通过；
- 唯一命名、独立端口的隔离 Compose `0.23.0` 全栈 Smoke 通过：成功/预期失败 Run 均为 8 个事件，分别产生
  1/3 个制品，4 次短时下载摘要一致；隔离项目容器、网络和卷已删除；
- M9.2 不改变数据库结构，Alembic Head 仍为 `20260820_0016`；本节不替代生产 OCI Manifest Digest、镜像扫描、
  签名/证明和非提交人评审门禁。

## 2026-08-20 本机预验收证据

- 全量 Pytest 93 项通过，仓库标准入口 68 项通过；Ruff 检查通过，140 个 Python 文件格式一致；
- Schema 导出检查和不可变基线/Runner 制品摘要检查通过；
- Vue 严格 TypeScript 检查和 Vite `0.21.0` 生产构建通过；
- Python、Playwright、Node、Nginx、PostgreSQL、Redis、MinIO、MinIO Client 和 Prometheus 默认镜像引用均固定到
  不可变摘要；隔离构建得到 API `e2997386b128`、Worker `17187aca2814`、Frontend `5ce6eddee8f3` 和
  Smoke Target `8ff1c6bcea86` 镜像 ID；
- 隔离渲染配置不含 `change-me-*`，空 `BOOTSTRAP_ADMIN_TOKEN` 使 Bootstrap 返回 503；Nginx 对超过 1 MiB
  请求返回 413 且不暴露版本号；`/metrics` 无凭据返回 401、有效 Bearer 返回 200，前端不代理该端点；
- Alembic 唯一 Head 为 `20260817_0015`，完整离线升级 SQL 生成通过；隔离 PostgreSQL 17 已完成
  `upgrade head -> downgrade base -> upgrade head` 往返并回到该 Head；
- M8.3 自动评估测试覆盖触发、升级、降级、恢复、无数据保持、冷却抑制、到期重触发和重复评估不重复入队；
- M8.4 人工处置测试覆盖确认/取消确认、状态变化清除旧确认、静默校验、静默期间抑制投递、到期补发和操作审计；
- M8.5 人工重放测试覆盖状态与原因校验、当前配置目标、重复抑制、投递链、事件 ID 幂等和审计脱敏；
- M8.6 测试覆盖到期评估、活跃静默、PENDING/FAILED、最老等待、重放计数及静默/确认/重放操作人显示名；
- Prometheus 告警规则 YAML 共 11 条，容器内 `promtool check config` 与 `promtool check rules` 均通过；
- 隔离 Compose `0.21.0` 全栈健康，普通冒烟和 Outbox/Worker 中断恢复冒烟均通过；两次成功 Run 均为
  8 个事件、1 个制品，两次预期失败 Run 均为 8 个事件、3 个制品，所有短时下载和摘要校验通过；
- 浏览器验收覆盖质量运营页、evaluator 最近/下次评估、Webhook 测试事件、SSRF 私网阻断、FAILED 人工重放、
  原始/子投递链和操作人显示名；`/metrics` 的 M8 质量指标可查询且质量快照成功值为 `1`；
- 隔离联合备份通过 `backup_restore.py verify`：PostgreSQL 数据库、4 个对象约 328 KiB；`manifest.json` SHA-256
  为 `61ef4eb4026694d473035495c47d3d78437b5732eeef3d7646c78447feda0f10`。恢复到全新 PostgreSQL/MinIO 后
  Alembic 为 `20260817_0015`，用户/项目/Run/制品数为 1/1/2/4，随机制品下载摘要一致，实测 RTO 为 20.097 秒；
- 本地测试 CA 的 HTTPS 接收器完成真实 TLS/HMAC 验收：投递 `c835c70b-cc86-411e-a490-64e610da1149` 一次成功、
  返回 204，接收端校验签名、事件类型和投递 ID；数据库明文签名密钥行数为 0；
- 两个隔离项目的 PostgreSQL 卷约 48.3–48.4 MiB，宿主 C 盘剩余 775.75 GiB；18 个隔离容器日志对 16 个
  敏感配置值扫描命中 0；
- Docker Scout 因未登录不可用，Trivy 官方镜像和 Windows 包均因外网超时未取得，因此漏洞扫描正式门禁保持未完成；
- 本节仅记录隔离本机证据，不替代 GitHub Actions、非提交人代码评审、异故障域备份、生产容量预测、真实外部
  HTTPS 告警接收器和最终发布批准。

## 合并门禁

- [ ] `Python quality and contracts` 通过；
- [ ] `Frontend typecheck and build` 通过；
- [ ] `PostgreSQL migration round trip` 通过；
- [ ] `Full Compose smoke` 通过且失败日志已检查；
- [ ] `Prometheus configuration` 通过；
- [ ] PR 已完成至少一名非提交人的代码评审。

## 配置与安全

- [ ] 所有默认口令、回调 Token、Bootstrap Token 和对象存储 Root 凭据已替换；
- [ ] Bootstrap 管理员完成后已移除 `BOOTSTRAP_ADMIN_TOKEN`；
- [ ] Runner 只获得所需密钥引用，日志与制品抽检无明文密钥；
- [ ] 五类供应链允许清单均为非空精确值，已移除 `example/testops` 示例；策略版本与 CI 验证报告一致；
- [ ] API 只注入批准验证器的 Ed25519 公钥、credential id 与 HTTPS/SPIFFE 工作负载身份；旧、新 key-id 轮换窗口、
  新指纹观测和旧 key 撤销时间已记录，私钥不进入控制面、数据库、Worker、日志或前端；
- [ ] 签名客户端使用 v2 compact detached JWS，并覆盖方法、路径、created、唯一 UUID nonce、原始 JSON 摘要、
  credential 和工作负载身份；过期、未来超限、错误签名、未知 key、身份不匹配和重复 nonce 均已实测失败关闭，
  生产入口强制 TLS；
- [ ] `SUPPLY_CHAIN_ALLOW_LEGACY_HMAC=false`；若迁移窗口临时启用旧 HMAC，已限制期限、记录责任人与旧 key 删除证据；
- [ ] CI 对最终 `repository@digest` 完成 Cosign identity/issuer、透明日志、SLSA provenance 和 SBOM/漏洞门禁验证，
  原始证据可按控制面记录的三个摘要检索；
- [ ] 所有用户 Bearer 会话均不能提交供应链判定；PENDING/REJECTED 包的验证、激活、新执行、计划触发和重跑均
  失败关闭；
- [ ] `RUNNER_PACKAGE_CATALOG` 只包含流水线确认的不可变 OCI 摘要，Worker 心跳与自动化包记录逐项一致；
- [ ] 注册 Worker 使用 `RUNNER_EXECUTION_MODE=SUBPROCESS`，心跳为 `RUN_SECRETS_ONLY`；抽检子进程环境不含
  数据库、Redis、回调 Token、MinIO 凭据或其他 Run 密钥；
- [ ] Run Result 隔离证据与实际能力一致；在容器执行器完成前，只读根文件系统、网络策略和资源限制保持未强制，
  不以子进程模式替代这些生产门禁；
- [ ] 新旧自动化包切换期间两个摘要均有健康 Worker；目录不匹配测试保持 WAITING，并显示
  `AUTOMATION_PACKAGE_UNAVAILABLE`；
- [ ] API、对象存储和数据库启用 TLS，入口限制 CORS 与请求体大小；
- [ ] MinIO 使用独立读写/备份账号，Bucket 版本控制和保留策略已启用；
- [ ] 应用和基础镜像已固定到验收过的不可变摘要并完成漏洞扫描；
- [ ] `/metrics` 不经公网前端代理，Bearer 凭据由受限文件提供。
- [ ] 质量 Webhook 只允许批准的 HTTPS 目标，签名密钥仅注入 dispatcher，出口层已配置域名允许清单；
- [ ] 告警确认和静默接口仅授权项目管理员，静默原因、确认说明及解除操作均能在审计日志中追溯；

## 数据与恢复

- [ ] 发布前完整备份通过 `backup_restore.py verify`；
- [ ] 备份副本位于独立故障域，并记录清单 SHA-256、保留期和负责人；
- [ ] 最近一次联合恢复演练在计划周期内，实际 RPO/RTO 满足业务目标；
- [ ] Alembic 只有一个 head，真实 PostgreSQL 已完成升级/回滚/再升级演练；
- [ ] 回退方案使用上一个应用镜像与已验证备份，不依赖临时修改数据库。

## 容量、监控与验收

- [ ] PostgreSQL、Redis、MinIO 和 Runner 工作区容量满足发布窗口与保留周期；
- [ ] API 可用性、数据库就绪、5xx 和 p95 延迟告警已接入接收器并测试；
- [ ] Outbox、Scheduler、Reaper、Worker、Run 终态率和制品上传有值班查询与处置步骤；
- [ ] 质量 Webhook PENDING/FAILED 投递、重试次数和签名密钥缺失有值班查询与处置步骤；
- [ ] 质量快照不可用、evaluator 延迟和 Webhook 最老 PENDING 年龄告警已接入接收器并完成测试；
- [ ] 静默截止、解除后补发、状态变化清除旧确认和 evaluator 停机恢复已纳入值班演练；
- [ ] 派发积压、计划延迟、失联 Worker 租约告警已接入接收器并完成测试；
- [ ] 成功与预期失败两条 Compose 冒烟 Run 均完成，制品摘要和短时 URL 正确；
- [ ] 人工验证登录、RBAC、项目设置、自动化包草稿/验证/激活/退役、审批发布、运行运营和审计查询；
- [ ] 发布负责人、值班人、变更窗口和停止条件已确认。

## 发布证据

- [ ] GitHub Actions 运行链接；
- [ ] Git 提交与镜像摘要；
- [ ] 数据库迁移前后版本；
- [ ] 备份路径与 `manifest.sha256`；
- [ ] 冒烟 Run ID 与制品摘要；
- [ ] 指标/告警截图和最终批准人。
