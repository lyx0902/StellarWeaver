# G5 团队工作台

> 历史记录。现行说明见[项目文档](../../README.md)。

G5 在 G4 的线程、审批和补丁审阅基础上增加团队账号、按项目授权和专用容器执行服务。Compose 试点可以用独立项目名、端口和数据卷运行。正式身份由运维配置的 OIDC 服务提供。

## 重启已升级的本机工作台

已配置 `.stellarweaver/workbench.compose.env` 和 `.stellarweaver/workbench.members.json` 的本机工作台可以从项目根目录执行以下命令。打开 `http://127.0.0.1:8787/`，用账号名和密码注册、登录。首次注册继承既有 StellarWeaver 线程，随后注册的账号拥有各自的项目和会话。密码通过回环 HTTP 请求提交，PostgreSQL 只保存加盐哈希。数据库仍可从本机 `55434` 连接。

```powershell
docker compose --env-file .env --env-file .stellarweaver/workbench.compose.env -p stellarweaver -f deploy/compose.yaml -f deploy/compose.workbench.yaml -f deploy/compose.g5.yaml -f deploy/compose.g5-local.yaml up -d --wait
```

修改源码后，可在命令末尾加 `--build` 更新镜像。`compose.g5-local.yaml` 专用于本机。`SW_WORKBENCH_PROJECTS_ROOT` 指定当前机器允许选择的项目根目录，API 和 worker 以只读方式挂载。本机账号可以从这个目录挑选 Git 项目，改动仍在每个线程的独立 worktree 中。

左侧「新建工作区」用于选择项目文件夹，工作区标题可折叠。点击某个工作区的 `＋` 启动项目任务，点击会话标题回到对应线程。会话可归档或恢复，输入框显示当前项目路径。顶部「新任务」直接开启普通对话，历史位于工作区列表下方，普通对话不开启项目工具。

## 账号与项目

准备一个 UTF-8 JSON 成员配置。`subject` 填身份服务实际签发的 `sub`，`id` 和 `tenant` 使用固定 UUID。Compose 试点挂载一个 Git 项目，所以 `workspace_indexes` 使用 `[0]`。同一个项目可授权给不同租户，各租户的线程与任务彼此隔离。

```json
[
  {
    "id": "30000000-0000-0000-0000-000000000001",
    "tenant": "20000000-0000-0000-0000-000000000001",
    "name": "项目管理员",
    "role": "admin",
    "workspace_indexes": [0],
    "subject": "替换成管理员的 sub"
  },
  {
    "id": "30000000-0000-0000-0000-000000000002",
    "tenant": "20000000-0000-0000-0000-000000000001",
    "name": "项目成员",
    "role": "member",
    "workspace_indexes": [0],
    "subject": "替换成成员的 sub",
    "active_limit": 3,
    "daily_tokens": 500000
  }
]
```

| 角色 | 浏览范围 | 操作权限 |
| --- | --- | --- |
| `member` | 获授权项目中自己的线程和补丁 | 提交、续接、审批和取消自己的任务 |
| `admin` | 获授权项目中的所有线程、补丁、审计和用量 | 审批和取消项目任务，提交和续接自己的任务 |
| `viewer` | 获授权项目中的线程、报告和补丁 | 只读 |

成员配置由部署方维护。目前变更身份或项目权限时，先排空相关任务，再同时重启 API 和 worker 读取新配置。多个宿主项目可通过部署配置显式绑定多个 workspace，成员配置中的索引对应启动时的注册顺序。

## 用 Compose 启动本机试点

在身份服务中创建支持授权码、PKCE S256、`client_secret_basic` 和 RS256 ID token 的 Web 客户端。回调地址填写 `${SW_G5_OIDC_ORIGIN}/api/v1/auth/callback`。如果身份服务要求 HTTPS 回调，使用实际的 HTTPS 入口，并将下面的 `SW_G5_OIDC_ORIGIN` 改为该入口、将 `SW_G5_OIDC_LOOPBACK_HTTP` 设为 `0`。

在项目根目录执行 PowerShell。`STELLARWEAVER_API_KEY` 可从当前项目 `.env` 读取。数据库密码和执行服务令牌在第一次建试点时生成，需保存在本地部署配置中供重启复用。

```powershell
$env:SW_WORKBENCH_WORKSPACE = 'D:\path\to\committed-git-project'
$env:SW_G5_MEMBERS_FILE = 'D:\path\to\members.json'
$env:SW_WORKBENCH_PORT = '18788'
$env:SW_WORKBENCH_IMAGE = 'stellarweaver-workbench:g5-local'
$env:SW_WORKBENCH_REPAIR = '1'
$env:SW_WORKBENCH_PROVIDER = 'deepseek'
$env:SW_WORKBENCH_EXTERNAL_VOLUMES = 'false'
$env:SW_WORKBENCH_POSTGRES_VOLUME = 'stellarweaver-g5-pilot_postgres_data'
$env:SW_WORKBENCH_STATE_VOLUME = 'stellarweaver-g5-pilot_workbench_state'
$env:SW_PG_ADMIN_PASSWORD = '<试点数据库管理员密码>'
$env:SW_PG_APP_PASSWORD = '<试点数据库运行账号密码>'
$env:SW_G5_EXECUTOR_TOKEN = '<至少 32 位随机字母、数字、横线或下划线>'
$env:SW_G5_OIDC_ISSUER = 'https://identity.example.org'
$env:SW_G5_OIDC_ORIGIN = 'http://127.0.0.1:18788'
$env:SW_G5_OIDC_LOOPBACK_HTTP = '1'
$env:SW_G5_OIDC_CLIENT_ID = '<客户端 ID>'
$env:SW_G5_OIDC_CLIENT_SECRET = '<客户端密钥>'
docker compose --env-file .env -p stellarweaver-g5-pilot -f deploy/compose.yaml -f deploy/compose.workbench.yaml -f deploy/compose.g5.yaml up -d --build --wait
```

打开 `http://127.0.0.1:18788/`（或实际的 HTTPS 入口），点击「使用组织账号登录」。可把 provider 改为 `fake` 做离线试跑。自动化验收启用专门的测试账号模式，使用 `SW_G5_ALLOW_TEST_TOKENS=1` 和 `SW_G5_TEST_TOKENS`，后者为成员 `token_env` 名称到临时随机令牌的 JSON 对象。使用 OIDC 时将测试账号开关设为 `0`。

## 配额与审计

- 每位成员最多每分钟新建 20 个运行。活跃运行上限包含排队、执行和审批等待，可用 `active_limit` 调整。每个成员同一时刻最多领到一个任务，worker 按租户交替领取。`SW_G5_EXECUTOR_SLOTS` 默认是 `2`，可配 `1` 至 `8`。
- DeepSeek 按 UTC 日和用户计量。请求前预留估算输入及输出上限，完成后优先按模型返回的实际总 token 数结算。未确认的请求保留预留额度。`daily_tokens` 按用户配置。这一预算独立于对话框里的 100k 上下文容量。FakeModel 不计模型用量。
- 管理员在「审计与用量」查看获授权项目的提交、审批、取消和取报告／补丁等操作，以及已记录的模型用量。审计保存操作者、项目、运行标识和操作类型，页面只显示最近记录。试点沿用 PostgreSQL 和工作区数据卷，保留周期及恢复演练由部署方配置。

## 执行边界和恢复

应用 worker 持有任务记录、会话和 DeepSeek 配置。专用 `executor` 在私有网络接收限大小的任务快照以及配置命令的索引。只有 `executor` 挂载 Docker socket。验证容器使用独立目录、禁网、资源上限和非特权用户，任务源码与数据库、模型密钥通过分开的服务流转。Docker 宿主和能够管理 executor 的运维人员属于信任边界。

执行服务断开后，工作台把无法确认的验证标为失败或未确认。executor 启动时清理本 Compose 项目标签下遗留的验证容器，新验证由用户重新发起。现有 G4 工作台不会自动切换到团队身份。公开接入前仍需配置实际身份服务、HTTPS 入口和审计留存，并完成备份恢复演练。

数据库恢复已在隔离环境用 `pg_dump -Fc` 导出、`pg_restore --exit-on-error` 回灌到空库，并核对迁移版本及旧运行记录。完整备份需要在停止接收任务并排空运行后保存同一时间点的 PostgreSQL、工作区状态卷和部署配置。应先在隔离环境还原这三部分，确认历史线程、各角色访问和已有 worktree 的续接，再切换实际工作台。
