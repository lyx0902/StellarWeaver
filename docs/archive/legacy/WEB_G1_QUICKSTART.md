# G0 / G1：本地只读工作台

> 历史记录。现行说明见[项目文档](../../README.md)。

本批把现有 Agent 与真实 PostgreSQL 接入浏览器。点击运行后可看工具时间线；刷新页面会重新读取数据库中的同一个运行，不会重新执行任务。每次提交创建一条新会话；选择历史运行只查看，不继续对话。CLI 仍用原来的 SQLite。

已使用 G1 数据库并准备升级的读者可参阅 [G2 本地审批与修复](WEB_G2_QUICKSTART.md)。

## 在 Windows 启动

需要 Git、Python/uv、Node/npm 和已启动的 Docker Desktop（Linux 容器模式）。下面在仓库根目录执行，口令和访问令牌只用于本地示例。若该 Compose 项目已有卷，使用初始化时的原口令；改环境变量不会自动轮换数据库角色口令。

终端一：启动 PostgreSQL、迁移并启动 API。

```powershell
uv sync --locked --extra web
$env:SW_PG_ADMIN_PASSWORD = 'sw-local-migration-only'
$env:SW_PG_APP_PASSWORD = 'sw-local-app-only'
docker compose --env-file NUL -p stellarweaver-workbench -f deploy/compose.yaml up -d --wait
$env:STELLARWEAVER_MIGRATION_DSN = "postgresql+psycopg://stellarweaver_admin:${env:SW_PG_ADMIN_PASSWORD}@127.0.0.1:55432/stellarweaver"
uv run --locked --extra web python -m stellarweaver.web migrate
Remove-Item Env:STELLARWEAVER_MIGRATION_DSN
$env:STELLARWEAVER_DATABASE_DSN = "postgresql+psycopg://stellarweaver_app:${env:SW_PG_APP_PASSWORD}@127.0.0.1:55432/stellarweaver"
$env:STELLARWEAVER_WEB_TOKEN = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
$env:STELLARWEAVER_WEB_TOKEN
uv run --locked --extra web python -m stellarweaver.web serve --provider fake --workspace .
```

终端二：

```powershell
Set-Location frontend
npm ci
npm run dev
```

用浏览器打开 `http://127.0.0.1:5173`，输入终端一生成的本地令牌，提交一条任务。FakeModel 固定调用真实的 `list_files`，不会按照任意提示推理。等待工具时间线显示文件列表和 completed，刷新页面核对会话 ID 及事件仍相同。

需要用现有 DeepSeek 时，在终端一重启 API，改为：

```powershell
uv run --locked --extra web python -m stellarweaver.web serve --provider deepseek --env-file .env --workspace .
```

沿用 `STELLARWEAVER_API_KEY`、`deepseek-flash` 和 Chat Completions；Key 留在服务端。只有真实模式会把任务及相关工作区上下文发给模型。每个要开放的工作区由服务启动者用 `--workspace` 登记，浏览器只传工作区 ID。当前按仓库默认模型/预算配置运行，不采纳目标仓库的 provider 地址或凭据文件。

## 可复现检查

- 后端：给独立的已迁移测试库设置 `STELLARWEAVER_TEST_DATABASE_DSN`，执行 `uv run --locked --extra web pytest tests/integration/test_web_postgres.py -q`。测试会写入用随机 ID 隔离的虚拟租户，请不要对日常数据库运行。
- 浏览器：创建 `.validation/gui-g1-fixture` 临时 Git 仓库，写入 `g1-browser-fixture.txt`；命令环境设独立测试库的 `STELLARWEAVER_DATABASE_DSN` 和至少 32 字符的 `STELLARWEAVER_WEB_TOKEN`。在 `frontend` 执行 `npm run test:e2e`，需要已安装 Microsoft Edge；测试自启 Vite/API，要求 5173/8765 空闲。
- 静态检查：前端 `npm run build`；后端 `uv run --locked --extra web ruff check .`、`uv run --locked --extra web mypy src/stellarweaver`。
- 停止：用 Ctrl+C 停止前后端；用同一 Compose 项目和环境运行 `docker compose --env-file NUL -p stellarweaver-workbench -f deploy/compose.yaml stop`。停止不会删除数据卷。

## 当前实现边界

- 部署只有数据库容器，API 与前端在宿主运行。发布镜像、异步存储适配器、独立 worker、浏览器审批/取消、任务容器和多用户登录分别留在后续阶段。未提供浏览器文件写入或任意命令入口。
- 当前 PostgreSQL journal 用短事务。为兼容 Windows 的 asyncio 子进程循环，G1 将同步驱动留在运行线程中，API 以线程方式执行数据库查询；没有阻塞 ASGI 事件循环的长事务。运行线程内的数据库语句仍有自己的超时，并不受 asyncio 瞬时抢占。
- 退出时等待本进程任务收尾；异常重启把已接收但没有结果的旧运行标成 interrupted，不继续执行或虚构工具结果。当前只读运行不写更改基线，不提供从历史继续修改的功能。
- SQLite 与 PostgreSQL 不双写。持久消息、工具回合、结果和终态事件进 PostgreSQL；当前浏览器显示完整持久事件，不展示逐字草稿。浏览器不直接访问 SQLite 数据库或制品目录。
- 访问令牌只适合本机单用户，后端只监听本机回环。两个测试租户验证数据边界，不代表多用户产品已上线。后续计划见[实施方案](../GUI_PLATFORM_IMPLEMENTATION_PLAN.md)。

