# 使用与部署

## CLI

项目从源码安装，运行需要 Python 3.12+、uv 和 Git。文件搜索优先使用 `rg`，缺少时使用内置 Python 搜索。离线命令要求依赖已经同步或缓存在本机。

```powershell
uv sync --locked
uv run stellarweaver doctor --offline
uv run --offline python -m stellarweaver.demo
```

默认 provider 为可脚本化的 FakeModel。使用真实模型时，将仓库根目录的 [.env.example](../.env.example) 复制为 `.env`，设置 `STELLARWEAVER_API_KEY`。可在目标工作区放置由[配置模板](../stellarweaver.example.toml)生成的 `stellarweaver.toml`，也可通过 `--config` 指定配置。进程环境优先于 dotenv；使用 `--env-file` 可显式选择凭据文件。普通 CLI 启动时优先查找启动目录的 `.env`，再按配置回退到工作区。

```powershell
uv run stellarweaver doctor --check-model --provider deepseek --env-file .env
uv run stellarweaver chat --workspace . --provider deepseek --env-file .env --stream
uv run stellarweaver exec --workspace . --provider deepseek --env-file .env --policy read-only --json --prompt "梳理项目入口和核心调用链，给出文件路径与行号。"
```

`doctor --offline` 检查本机配置和可执行文件。`doctor --check-model` 显式发送一条短模型请求。DeepSeek 默认模型为 `deepseek-flash`，协议为 Chat Completions。`openai-compatible-chat` 接受自定义模型和服务地址，具体能力由实际 endpoint 决定。可通过 `--stream` 或 `--no-stream` 覆盖 CLI 的流式选择。

CLI 和 Web 的 DeepSeek 默认启用 Thinking，强度为 `high`。CLI 通过 `[model]` 的 `thinking` 和 `reasoning_effort` 调整。设置 `thinking = "disabled"` 可运行 Non-thinking，启用时强度可选 `low`、`high`、`max`。原始推理只在当前进程的工具对话中回传，界面、持久化会话和导出记录保存可见回复与工具证据。

DeepSeek Thinking 默认单次输出上限为 8,192 token，包含推理和可见文本。CLI 保留配置中显式设置的 `max_output_tokens`。模型输出仍受整轮运行预算限制。

### 会话、策略和改动

下列命令使用既有 CLI 会话的 ID。`replay` 读取历史并输出事件，`resume` 进入恢复流程。`export` 支持导出持久会话记录。

```powershell
uv run stellarweaver sessions list
uv run stellarweaver replay SESSION_ID --json
uv run stellarweaver export SESSION_ID --help
uv run stellarweaver resume SESSION_ID --provider deepseek --env-file .env --prompt "重新读取当前状态，继续验证。"
```

| 操作 | 行为 |
| --- | --- |
| `/status`、`/model` | 查看任务状态、预算与模型配置，或切换模型。 |
| `/plan`、`/compact`、`/clear` | 在原有权限内规划，压缩历史，或开启新的活动上下文。原始 Journal 保留。 |
| `/diff` | 查看相对启动基线的新增变化、已记录的内置编辑，以及待核对的操作。 |
| `/undo` | 按逆序撤销最后一项符合条件的内置修改。重新检查文件哈希、备份和授权，Git index 保持原状态。有后续外部修改时返回冲突，维护者按记录处理。 |
| `/skill`、`/help`、`/exit` | 查看或选择技能、查询命令、退出交互。 |

`read-only` 供源码阅读。`ask` 按策略请求确认。`workspace-edit` 允许受控文件编辑，命令仍走相应授权规则。需要交互批准的操作在无 TTY 时停止。本地 stdio MCP 的发现、调用及关闭流程由[集成测试](../tests/integration/test_mcp_extensions.py)覆盖。配置字段见[示例配置](../stellarweaver.example.toml)，加载与协议边界见[架构](ARCHITECTURE.md#模型上下文与扩展)。

在 Git 工作区可使用 `[execution]` 的 `backend = "docker"` 和固定本地镜像启用断网沙箱。设置 `allow_argv = true` 才会向模型公开沙箱里的通用命令工具，命令仍需授权。`[harness]` 可单独开启开发工具、Python 代码导航和修复反馈。工作台与评测的开关及验证口径见 [M5–M8 验收](HARNESS_ACCEPTANCE.md#m5-m8-沙箱与修复工具)。

需要用工作台验证 StellarWeaver 自身时，可从仓库根目录构建专用镜像。镜像包含锁定的项目依赖、pytest、pytest-asyncio 和 Ruff。执行时从任务快照读取源码，pytest 只显式加载镜像中安装的异步插件，容器保持断网。

```powershell
docker build --target verification -t stellarweaver-verification:m6-local -f deploy/Dockerfile .
$env:SW_RUNNER_IMAGE = docker image inspect --format '{{.Id}}' stellarweaver-verification:m6-local
$env:SW_WEB_DEVELOPMENT_TOOLS = '1'
```

把 `SW_RUNNER_IMAGE` 的固定 ID 与开发工具开关存入 `.stellarweaver/workbench.compose.env`，按下文更新当前 Compose 项目。其他仓库需要的依赖由其自己的验证镜像提供。

## Web 工作台

1. 打开 `http://127.0.0.1:8787/`，用账号名和密码注册或登录，用户名显示在侧栏底部。升级已有单人部署时，首个注册账号继承原登记项目的历史。后续账号拥有各自的工作区与会话。
2. 点击「新建工作区」，从已配置的项目根目录选择 Git 项目。工作区标题可以折叠，组内 `＋` 从该项目发起新任务。点击 `＋` 左侧的编辑图标可修改工作区别名，留空可恢复原名称。点击会话标题切换，输入框底部单独一行显示当前项目路径。已结束的会话可以归档和恢复。
3. 侧栏顶部的「新任务」开启普通对话。这类会话排在项目组下方，支持流式回复、追问和归档，使用独立的聊天上下文。项目线程加载仓库上下文，并按只读或修复模式提供相应工具。
4. 修复中的编辑与验证显示审批卡。完成后的工具调用折叠到「执行过程」，右侧「运行详情」提供日志和测试证据。悬停或点击输入框底部的上下文圆环，可查看最近一次模型请求的输入量与配置窗口，默认分母为 100,000 tokens。有实际用量时采用返回值，其余情况展示估算。
5. 在「查看改动」中展开文件，可选择「反馈此文件」或「反馈此改动块」。从线程最新完成的修复发送反馈时，原线程会新开一轮修订，编辑仍按原有规则审批。
6. 选择改动块后可下载 Diff，或点击「在独立工作区应用所选并验证」。复验完成后点「预检并交接到 IDE」，再点「预检目标分支」。目标是当前项目目录里已检出的分支和提交，预检展示补丁基线、涉及文件和冲突。只有全部改动已复验、基线一致、目标能应用补丁且涉及文件没有本地改动时，才显示「下载已复验补丁」。切换目标分支或继续编辑后应重新预检，最后在 IDE 核对并应用。
7. 如果上轮中断，核对面板列出的未确认操作后再继续。可带上已结束轮次的历史，或用新提示检查同一 worktree。之前的审批仅作用于原操作。

Enter 提交，Shift+Enter 换行。运行中的线程可以编辑追问草稿，当前轮次结束后提交。

使用支持搜索的 DeepSeek 模型时，普通对话和项目任务均可按需请求 `ds_search`，例如查询 Node.js 官网的最新稳定版本。工作台会显示即将外发的查询原文，批准一次后才发送。CLI 使用终端审批，无 TTY 时需要批准的搜索会被阻止。搜索结果的来源可从「执行过程」展开，搜索调用计入原有模型请求、token 和工具预算。 [DeepSeek 搜索说明](https://api-docs.deepseek.com/guides/search/)

管理员在左侧打开「审计与用量」可查看北京时间今日用量、自己的 UTC 日额度和最近审计。面板打开后每 30 秒重新查询用量和审计，并显示服务端最近读取时间。今日用量在北京时间零点切换，UTC 日额度在北京时间 08:00 重置。

左侧用户名上方的「Skills 库」可搜索、查看和刷新本机技能。正文以只读文档展示，列表同时给出来源和扫描提示。可在本机 Compose 配置中将 `SW_WORKBENCH_AGENT_SKILLS_ROOT`、`SW_WORKBENCH_CODEX_SKILLS_ROOT` 指向希望提供给工作台已登录用户浏览的目录。Agents 根目录下已有的 `skills-codex` 会作为独立来源扫描。路径通过 API 容器只读挂载，每个来源扫描一级子目录中的 `SKILL.md`。

本机初始管理员可在左侧「实验记录」切换已发布的批量评测、边界夹具和单题轨迹。可筛选题号和评分状态，逐题查看候选、失败原因、独立评分、耗时和 Token 用量，并按题号对照两份报告、查看不可比原因或下载脱敏 JSON。`SW_WORKBENCH_EXPERIMENT_ROOT` 指向只读挂载目录。根目录 `report.json` 保留旧单题记录，`reports/*.json` 用于发布 M1 批量报告和 M2 夹具报告。 [看板与发布口径](HARNESS_ACCEPTANCE.md#已发布的本地记录)

## 本机 Compose

### 已部署实例

在 StellarWeaver 仓库根目录用现有 `.env`、`.stellarweaver/workbench.compose.env` 和成员配置启动。`migrate` 服务先更新数据库，随后服务健康检查通过。

```powershell
$dc = @('--env-file', '.env', '--env-file', '.stellarweaver/workbench.compose.env',
  '-p', 'stellarweaver', '-f', 'deploy/compose.yaml', '-f', 'deploy/compose.workbench.yaml',
  '-f', 'deploy/compose.g5.yaml', '-f', 'deploy/compose.g5-local.yaml')
docker compose @dc up -d --wait
docker compose @dc ps -a
Invoke-RestMethod 'http://127.0.0.1:8787/healthz'
```

更新源码后在 `up` 命令中加入 `--build`。用 `docker compose @dc logs --tail 80 api worker executor migrate` 检查启动、模型连接或验证服务状态。用 `docker compose @dc stop` 停止服务并保留数据卷。数据库端口、迁移、备份和恢复见[数据库文档](DATABASE.md)。

### 新机器首次配置

Docker Desktop 使用 Linux 容器。先拉取验证镜像 `docker pull python:3.12-slim-bookworm`。准备一个带有已提交 HEAD 的 Git 项目，并在 StellarWeaver 根目录创建 `.env` 和 `.stellarweaver/workbench.compose.env`。已有数据卷的部署沿用其数据库密码、卷名及工作区配置。

| 配置项 | 本机工作台填写方法 |
| --- | --- |
| `SW_PG_ADMIN_PASSWORD`、`SW_PG_APP_PASSWORD` | 为首次创建的数据库分别设置密码。管理员供迁移，应用角色供日常运行。 |
| `SW_WORKBENCH_WORKSPACE`、`SW_WORKBENCH_PROJECTS_ROOT` | 初始 Git 项目的绝对路径，以及允许用户浏览选择的上级项目目录。用正斜杠表示 Windows 路径，例如 `D:/Projects/Example` 与 `D:/Projects`。 |
| `SW_WORKBENCH_PROVIDER`、`SW_WORKBENCH_REPAIR` | `fake` 用于离线演示，`deepseek` 使用 `.env` 中的模型密钥。`1` 启用经审批的修复模式。 |
| `SW_WEB_DEEPSEEK_THINKING`、`SW_WEB_DEEPSEEK_REASONING_EFFORT` | 默认为 `enabled`、`high`。在 Compose 的环境配置中修改后重新创建 API 和 worker，直接启动 Web 时可使用同名进程环境变量。 |
| `SW_G5_LOCAL_OWNER_ID`、`SW_G5_MEMBERS_FILE` | 为初始本机管理员选一个固定 UUID。成员文件使用绝对路径，示例见下方。已有实例保留原 UUID。 |
| `SW_G5_EXECUTOR_TOKEN` | 生成一个至少 32 字符的随机执行服务凭据，供 worker 和执行器内部通信。浏览器使用账号密码。 |
| `SW_WORKBENCH_PORT`、`SW_PG_PORT` | 本机覆盖层默认分别为 `8787`、`55434`。配置中的显式端口优先生效。 |
| `SW_WORKBENCH_EXTERNAL_VOLUMES`、`SW_WORKBENCH_POSTGRES_VOLUME`、`SW_WORKBENCH_STATE_VOLUME` | 首次启动使用默认新卷。复用迁移自旧项目的卷时设置 `true` 和两个实际卷名。 |
| `SW_WORKBENCH_HTTPS_PROXY` | worker 需要通过宿主代理访问模型时设置，例如 `http://host.docker.internal:7890`。 |

本机 `.stellarweaver/workbench.members.json` 的最小示例。`id` 与 `SW_G5_LOCAL_OWNER_ID` 相同，初始租户使用源码定义的本机租户 UUID。准备好文件后按上面的 `$dc` 命令执行 `up -d --build --wait`。

```json
[
  {
    "id": "30000000-0000-0000-0000-000000000001",
    "tenant": "408f4403-a999-48de-94dc-cc049f8a95fd",
    "name": "本机管理员",
    "role": "admin",
    "workspace_indexes": [0]
  }
]
```

### 项目验证命令

服务器通过 `SW_VERIFY_COMMANDS` 配置 argv 列表，默认 `[["python","-m","unittest","-q"]]`。例如可在 `.stellarweaver/workbench.compose.env` 中配置下面两项。

```dotenv
SW_RUNNER_IMAGE=python:3.12-slim-bookworm
SW_VERIFY_COMMANDS='[["python","-m","unittest","-q"],["python","scripts/check_build.py"]]'
```

脚本路径以隔离任务根目录为准。使用 pytest、Node 或系统编译器时，选择已装好相应依赖的可信 Linux 镜像。镜像需包含 Python 3.12+，且不声明 `VOLUME`。Agent 单次运行检查需审批，补丁复验按配置顺序运行全部检查。之后修改文件会让旧验证证据失效。

### 团队身份配置

团队 OIDC 使用 `compose.yaml`、`compose.workbench.yaml` 与 `compose.g5.yaml`。本机账号入口由额外的 `compose.g5-local.yaml` 配置。这两种运行方式共享同一应用代码。

OIDC 客户端需支持授权码、PKCE S256、`client_secret_basic` 和 RS256 ID token。设置 `SW_G5_OIDC_ISSUER`、`SW_G5_OIDC_ORIGIN`、`SW_G5_OIDC_CLIENT_ID` 和 `SW_G5_OIDC_CLIENT_SECRET`，回调为 `<origin>/api/v1/auth/callback`。以 HTTPS 运行时使用实际入口，回环 HTTP 测试可启用 `SW_G5_OIDC_LOOPBACK_HTTP=1`。成员文件按固定 `id`、`tenant`、签发的 `subject` 和 `workspace_indexes` 分配项目。标准 Compose 注册一个静态项目，索引为 `0`。

| 角色 | 范围 |
| --- | --- |
| `member` | 访问获授权项目中自己的线程，提交、继续、审批与取消自己的任务。 |
| `admin` | 查看获授权项目的线程、审计和用量，管理该项目的审批及取消，继续自己拥有的线程。 |
| `viewer` | 读取获授权项目的运行、报告和补丁。 |

成员默认最多 3 个活跃运行、每个成员同时领取一个任务、每分钟最多创建 20 个新运行。`active_limit`、`daily_tokens` 可按成员设置。`SW_G5_EXECUTOR_SLOTS` 控制执行容量。管理面板展示按 UTC 日核算的 DeepSeek 用量和项目审计。变更成员配置时先排空任务，再重启 API 与 worker。团队 OIDC 验证范围见[验收记录](EVAL.md)。
