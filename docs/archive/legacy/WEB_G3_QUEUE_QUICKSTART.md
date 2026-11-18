# G3 持久化任务队列

> 历史记录。现行说明见[项目文档](../../README.md)。

想直接用浏览器体验可走 [Docker Compose 启动方式](WEB_G3_COMPOSE.md)。下文保留宿主机开发模式。

本批支持本机 API 与独立 worker。浏览器提交任务后，运行和队列记录在 PostgreSQL 的同一个事务中提交。worker 一次处理一个运行，按工作区注册配置读取文件，复用 G2 的审批、取消和报告。API 重启时，已入队的任务与 worker 中等候的审批继续保留。

## 用两个终端启动

沿用 [G1 的 PostgreSQL 和角色配置](WEB_G1_QUICKSTART.md)。停止旧的内嵌 API，然后用迁移角色更新数据库。应用进程使用普通应用角色。

```powershell
uv run --locked --extra web python -m stellarweaver.web migrate
```

终端一启动 API。需要 `STELLARWEAVER_DATABASE_DSN` 和现有的 `STELLARWEAVER_WEB_TOKEN`。

```powershell
uv run --locked --extra web python -m stellarweaver.web serve --queued --provider deepseek --enable-repair --workspace .
```

终端二启动 worker。需要同一个 `STELLARWEAVER_DATABASE_DSN`，DeepSeek Key 从现有 `.env` 读取。API 和 worker 应使用相同的工作区、provider、修复模式开关和状态目录。

```powershell
uv run --locked --extra web python -m stellarweaver.web worker --provider deepseek --enable-repair --workspace .
```

前端继续用 `npm.cmd run dev`，浏览器打开 [本地工作台](http://127.0.0.1:5173)。可在 API 和 worker 两侧都将 DeepSeek 改成 `--provider fake`，用只读任务验证离线接单。worker 可加 `--once`，领取至多一条任务后退出。

## 重启与取消

- API 启动后可以先提交任务，页面显示「排队中，等待 worker」。重启 API，再启动 worker，观察同一运行变为已领取和完成。
- 排队任务可以取消，取消后 worker 不会领取。执行中的取消通过数据库通知 worker，复用现有进程清理。API 单独重启时，仍在 worker 等待的审批可继续操作。
- 新 worker 启动时，将旧 worker 遗留的已领取运行标为 `interrupted`。已有工具启动记录却缺少结果时，验证状态为 `unknown`，以便核对工作区。已领取任务不会自动重派。
- 现阶段每个数据库只运行一个 worker。
