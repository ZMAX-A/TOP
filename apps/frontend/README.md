# Frontend

Vue 3 + TypeScript + Element Plus 管理端。M3 覆盖登录、项目和用例治理工作流；
M4 增加 Run 执行详情、带认证的实时事件流、逐用例失败诊断和短时授权制品下载。
M5.1 增加 Nginx 生产构建镜像：同源代理 `/api`、`/healthz`、`/readyz`，关闭 API
响应缓冲以保证 SSE 事件及时到达，并为 Vue Router 提供 SPA fallback。

```powershell
npm install
npm run dev
```

开发服务器默认将 `/api`、`/healthz` 和 `/readyz` 代理到
`http://127.0.0.1:8000`。可通过 `VITE_API_URL` 指向其他控制面地址。

```powershell
npm run typecheck
npm run build
```

从仓库根目录启动容器版管理端：

```powershell
docker compose up -d --build frontend
```

默认入口为 `http://127.0.0.1:8080`。
