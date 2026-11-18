# 用 Docker Compose 启动本地工作台

> 历史记录。现行说明见[项目文档](../../README.md)。

Compose 启动 PostgreSQL、一次性 Alembic 迁移、API 和独立 worker。API 同时提供构建好的前端和 `/api/v1`，宿主机只需 Docker Desktop 和浏览器。工作台默认监听 `127.0.0.1:8787`，工作区默认以只读方式挂载。

## 首次启动

从 StellarWeaver 仓库根目录运行。容器版在 Docker Desktop 中统一显示为 `stellarweaver`，浏览器入口为 `8787`，PostgreSQL 宿主机端口为 `55434`。

```powershell
$settings = '.stellarweaver/workbench.compose.env'
if (-not (Test-Path -LiteralPath $settings)) {
    New-Item -ItemType Directory -Force .stellarweaver | Out-Null
    $workspace = (Get-Location).Path.Replace('\', '/').Replace("'", "\'")
    $lines = @(
        "SW_PG_ADMIN_PASSWORD=$([guid]::NewGuid().ToString('N'))"
        "SW_PG_APP_PASSWORD=$([guid]::NewGuid().ToString('N'))"
        "SW_WORKBENCH_TOKEN=$([guid]::NewGuid().ToString('N'))$([guid]::NewGuid().ToString('N'))"
        "SW_PG_PORT=55434"
        "SW_WORKBENCH_WORKSPACE='$workspace'"
        "SW_WORKBENCH_PROVIDER=fake"
    )
    [IO.File]::WriteAllLines((Join-Path (Get-Location).Path $settings), [string[]]$lines)
}
$dc = @('--env-file', $settings, '-p', 'stellarweaver', '-f', 'deploy/compose.yaml', '-f', 'deploy/compose.workbench.yaml')
docker compose @dc up -d --build --wait
if ($LASTEXITCODE -ne 0) { throw '工作台启动失败，请查看 Compose 日志' }
((Get-Content $settings | Where-Object { $_ -like 'SW_WORKBENCH_TOKEN=*' }) -replace '^SW_WORKBENCH_TOKEN=', '') | Set-Clipboard
```

打开 [工作台](http://127.0.0.1:8787/)，粘贴令牌，提交只读任务。FakeModel 会列出真实工作区文件。配置保存在 Git 忽略目录，重跑上述命令会复用原来的数据库口令和令牌。

旧配置可在 `.stellarweaver/workbench.compose.env` 中把 `STELLARWEAVER_WEB_TOKEN=` 改名为 `SW_WORKBENCH_TOKEN=`，保留等号后面的值。这样宿主机开发版在 PowerShell 中设置的 `STELLARWEAVER_WEB_TOKEN` 就不会覆盖容器版令牌。

保存配置后，每次在项目根目录用同一条命令启动：

```powershell
docker compose --env-file .stellarweaver/workbench.compose.env -p stellarweaver -f deploy/compose.yaml -f deploy/compose.workbench.yaml up -d --wait
```

要使用 DeepSeek，将配置中的 `SW_WORKBENCH_PROVIDER` 改为 `deepseek`，并在 `$dc` 里把 `'--env-file', '.env'` 加在原来的 env 文件参数前。模型 Key 从你已有的 `.env` 读取，只传给 worker。

如果容器访问 DeepSeek 需要宿主机上的 HTTP 代理，可在 Compose 配置里设置 `SW_WORKBENCH_HTTPS_PROXY`。例如 Windows 代理监听 `7890` 且容器可以访问时，填 `http://host.docker.internal:7890`。该配置只传给 worker。

G3 修复需要把信任的测试仓库以读写模式挂载，并设置 `SW_WORKBENCH_REPAIR=1` 和 `SW_WORKBENCH_WORKSPACE_READ_ONLY=false`。命令在持久 worker 容器内运行。G4 的私有 worktree、一次性验证容器和补丁审阅可通过 [G4 Compose 配置](WEB_G4_WORKFLOW.md) 启用。

## 查看状态与维护

```powershell
docker compose @dc ps -a
docker compose @dc logs --tail 80 api worker
docker compose @dc stop
docker compose @dc up -d --build --wait
```

若 API 启动日志提示 `Configure an ASCII web token of at least 32 characters`，检查 Compose 配置中的 `SW_WORKBENCH_TOKEN`。修正后可以用 `docker compose @dc up -d --no-deps --wait api` 重新创建 API 容器。

迁移容器显示 `Exited (0)` 表示已完成。停止或更新会保留数据库卷和 worker 的制品卷。工作区的文件改动保存在宿主机路径。

API 重启后，仍在 worker 上等待审批的操作继续可见。worker 异常退出后，替代 worker 会将已领取的任务标为 `interrupted`。已有开始记录却缺少结果的工具显示为未确认。活动任务的目录和时间线可用于人工核对。

若从宿主机版切换并想接续它的历史，先处理排队任务，停止旧 API 和 worker，再备份 PostgreSQL。容器版可以挂载原数据库卷，并沿用该卷初始化时的数据库口令。容器部署按宿主机挂载路径登记工作区，原有运行历史仍可查看。

如果从旧的 `stellarweaver-compose` 项目更名，先停止旧项目，然后在本机配置里设 `SW_WORKBENCH_EXTERNAL_VOLUMES=true`、`SW_WORKBENCH_POSTGRES_VOLUME=stellarweaver-compose_postgres_data` 和 `SW_WORKBENCH_STATE_VOLUME=stellarweaver-compose_workbench_state`。用新项目名启动时会挂载原数据库卷和制品卷。旧项目容器可在新入口验证后移除。

在迁移、更新或切换前，可从容器导出数据库。恢复时用新建的空数据库演练，并确认历史和队列都可查。需要完整保存制品时，停止 worker 后同时备份 `workbench_state` 卷和宿主机工作区。

```powershell
New-Item -ItemType Directory -Force .stellarweaver | Out-Null
$db = docker compose @dc ps -q db
docker exec -u postgres $db pg_dump -Fc -U stellarweaver_admin -d stellarweaver -f /tmp/stellarweaver.dump
docker cp "${db}:/tmp/stellarweaver.dump" ./.stellarweaver/stellarweaver.dump
```

在同一 PostgreSQL 实例中，把备份恢复到一个尚未使用的演练数据库。原数据库可以继续提供服务。

```powershell
$restoreDb = 'stellarweaver_restore_check'
docker cp ./.stellarweaver/stellarweaver.dump "${db}:/tmp/stellarweaver-restore.dump"
docker exec -u postgres $db createdb -U stellarweaver_admin $restoreDb
if ($LASTEXITCODE -ne 0) { throw '请选择新的演练数据库名' }
docker exec -u postgres $db pg_restore --exit-on-error -U stellarweaver_admin -d $restoreDb /tmp/stellarweaver-restore.dump
if ($LASTEXITCODE -ne 0) { throw '恢复失败' }
docker exec -u postgres $db psql -X -U stellarweaver_admin -d $restoreDb -c 'SELECT count(*) FROM sw.runs; SELECT count(*) FROM sw.run_jobs;'
```
