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

DeepSeek Thinking 默认单次输出上限为 16,384 token，包含推理和可见文本。CLI 保留配置中显式设置的 `max_output_tokens`。模型输出仍受整轮运行预算限制。

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
3. 侧栏顶部的「新任务」开启普通对话。这类会话排在项目组下方，使用独立的聊天上下文。可写项目的新任务默认用 `auto`，输入框下方也可选择「只读」或「修改」。打开已有运行时，输入框以该运行的模式作为续问初值。
4. 通过会话右侧的 `⋯` 可归档或删除自己的会话。「已归档」分组支持恢复和删除。删除需确认，覆盖整条会话及专属状态目录，项目会话尚未导出的改动也会清理。队列中有待执行、正在运行或依赖该会话的复验时，需等任务结束或取消后再删除。
5. `auto` 放行工作区内的普通编辑和已登记的隔离验证，暂存补丁及需要单次确认的操作仍显示审批卡。「修改」继续逐项审批编辑与验证。完成后的工具调用折叠到「执行过程」，右侧「运行详情」提供日志和测试证据。悬停或点击输入框底部的上下文圆环，可查看最近一次模型请求的输入量与配置窗口，新运行的默认分母为 200,000 tokens，历史运行使用当时记录的窗口。有实际用量时采用返回值，其余情况展示估算。 [分流规则](SECURITY.md#运行模式与分流)
6. 在「查看改动」中展开文件，可选择「反馈此文件」或「反馈此改动块」。从线程最新完成的修复发送反馈时，原线程会新开一轮修订，编辑沿用当前运行模式的授权规则。
7. 选择改动块后可下载 Diff，或点击「在独立工作区应用所选并验证」。复验完成后点「预检并交接到 IDE」，再点「预检目标分支」。目标是当前项目目录里已检出的分支和提交，预检展示补丁基线、涉及文件和冲突。只有全部改动已复验、基线一致、目标能应用补丁且涉及文件没有本地改动时，才显示「下载已复验补丁」。切换目标分支或继续编辑后应重新预检，最后在 IDE 核对并应用。
8. 如果上轮中断，核对面板列出的未确认操作后再继续。可带上已结束轮次的历史，或用新提示检查同一 worktree。之前的审批仅作用于原操作。

Enter 提交，Shift+Enter 换行。运行中的线程可以编辑追问草稿，当前轮次结束后提交。

### 技能与网页搜索

使用支持搜索的 DeepSeek 模型时，普通对话和项目任务可按需调用 `ds_search`。公开查询按 L0 自动执行，命中凭据或本地路径保护规则的查询会被拦截。搜索来源可从「执行过程」展开。Web 对关联成员的搜索模型请求按周额度记账，CLI 按配置的运行预算记录调用。 [工具安全](SECURITY.md#l0-l3-风险分级) · [DeepSeek 搜索说明](https://api-docs.deepseek.com/guides/search/)

左侧用户名上方的「Skills 库」可搜索、查看和刷新本机技能。项目任务和普通对话的模型收到可用目录，可自主调用 `load_skill` 读取 `SKILL.md`。执行过程会显示名称与来源，技能中的操作仍遵循当前权限。

输入框底部的上下文圆环与运行模式之间有一个「＋」按钮。悬停或点击会展开「技能」，可以搜索并为当前会话选择最多三个技能版本，也可在同一面板移除。发送时加载所选正文，后续追问沿用选择。技能更新后需要重新选择新版本。项目技能默认关闭，登录用户可在对应项目的 Skills 库信任 `.stellarweaver/skills`，授权按成员和工作区保存。

本机 Compose 通过 `SW_WORKBENCH_AGENT_SKILLS_ROOT` 和 `SW_WORKBENCH_CODEX_SKILLS_ROOT` 指定共享目录，API 与 worker 均以只读方式挂载。共享目录按 Agents、Agents 下的 `skills-codex`、Codex 的顺序查找同名技能。已授权的项目目录也可提供项目技能。存在可用条目时，工作台向模型注册 `load_skill`。

### 上下文、续接与额度

追问默认带上最近最多八轮已完成的普通问答，以及文件哈希、验证结果和技能版本。文件或验证快照发生变化时，相关结果作为历史证据。中断后选择「用新提示检查工作区」可跳过历史对话和证据，再检查现有 worktree 及未确认操作。

| 限制 | 实际作用 |
| --- | --- |
| 单次模型请求窗口 | 默认 200,000 token。DeepSeek Thinking 预留 16,384 输出和 2,048 安全余量，可用输入为 181,568。超窗时尝试归纳较早的完整工具回合，仍放不下必需内容时显示 `CONTEXT_WINDOW_EXCEEDED`。 |
| 账户周额度 | 关联账户默认每 UTC 周 10,000,000 token。每次 DeepSeek 请求先预留，完成后按输入和输出结算。周一 00:00 UTC 即北京时间周一 08:00 重置，超额可显示 `WEEKLY_MODEL_QUOTA`。 |
| 单次操作超时 | 模型请求、命令和审批按各自的超时或有效期处理。Web 运行内累计 token、模型及工具调用次数和整轮时长无额外上限。 |

管理员可在「审计与用量」查看北京时间今日用量、自己的 UTC 周额度及审计。面板每 30 秒刷新，今日统计在北京时间零点切换。独立 CLI 评测和 DeepSeek 账户额度由各自的配置管理。 [数据库计量](DATABASE.md#审计与用量)

### 运行记录与撤销

运行结束后，在「运行详情 → 日志」可下载脱敏 JSON 或离线回放 HTML。导出包括可见对话、运行窗口、按序事件、工具操作和验证摘要。补丁在对应改动页单独下载。

隔离项目线程的最新运行可在「改动」中确认「撤销最近一次文件修改」。撤销仅处理已登记的单文件创建或编辑，先核对最后一次写入的哈希和文件权限，再恢复旧字节或删除本轮创建的文件。操作会在同一线程生成新运行和交付报告，先前的检查结果仍对应各自的历史快照。

### 实验记录

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
| `SW_WORKBENCH_PROVIDER`、`SW_WORKBENCH_REPAIR` | `fake` 用于离线演示，`deepseek` 使用 `.env` 中的模型密钥。修复开关设为 `1` 时，可写项目提供 `auto` 和「修改」。 |
| `SW_WEB_DEEPSEEK_THINKING`、`SW_WEB_DEEPSEEK_REASONING_EFFORT` | 默认为 `enabled`、`high`。在 Compose 的环境配置中修改后重新创建 API 和 worker，直接启动 Web 时可使用同名进程环境变量。 |
| `SW_WEB_CONTEXT_WINDOW_TOKENS` | Web 新运行的上下文窗口，默认 `200000`。API 在提交时保存该值，已入队的运行按保存值执行。 |
| `SW_G5_LOCAL_OWNER_ID`、`SW_G5_MEMBERS_FILE` | 为初始本机管理员选一个固定 UUID。成员文件使用绝对路径，示例见下方。已有实例保留原 UUID。 |
| `SW_G5_EXECUTOR_TOKEN` | 生成一个至少 32 字符的随机执行服务凭据，供 worker 和执行器内部通信。浏览器使用账号密码。 |
| `SW_WORKBENCH_PORT`、`SW_PG_PORT` | 本机覆盖层默认分别为 `8787`、`55434`。配置中的显式端口优先生效。 |
| `SW_WORKBENCH_EXTERNAL_VOLUMES`、`SW_WORKBENCH_POSTGRES_VOLUME`、`SW_WORKBENCH_STATE_VOLUME` | 首次启动使用默认新卷。复用迁移自旧项目的卷时设置 `true` 和两个实际卷名。 |
| `SW_WORKBENCH_HTTPS_PROXY` | worker 需要通过宿主代理访问模型时设置，例如 `http://host.docker.internal:7890`。 |

### Harness 增强开关速查

所有 Harness 增强开关默认关闭以保证评测基线可复现。Web 工作台在 `.stellarweaver/workbench.compose.env` 中设为 `1`（或指定值）并重启 `api` / `worker` / `executor` 生效；CLI 在 `stellarweaver.toml` 的 `[harness]` 与 `[context]` 中配置；SWE-bench 批量入口通过对应连字符参数（如 `--multi-edit-tools`、`--sticky-context-checkpoint`）启用。

| Web 环境变量 | CLI `[harness]` 字段 | 作用与依赖说明 |
| --- | --- | --- |
| `SW_WEB_DEVELOPMENT_TOOLS` | `development_tools` | 开放隔离沙箱内的 `run_tests`（支持 `selector`/`keyword` 细粒度过滤）、`run_python_snippet`（断网一次性复现脚本）、`discover_tests`、`check_syntax`、`run_linter`（要求固定摘要的验证镜像）。 |
| `SW_WEB_ISSUE_NAVIGATION` | `issue_navigation` | 启用题面信号提取与 `find_symbol`、`find_references`、`find_related_tests`。 |
| `SW_WEB_REPAIR_CONTROLLER` | `repair_controller` | 设为 `feedback`（提供错误分类与建议）或 `enforce`（同候选连续 3 次同签名失败停机），默认 `off`。 |
| `SW_WEB_PATCH_STAGING` | `patch_staging` | 开放只读暂存多文件补丁工具 `stage_patch`（仅导出审阅，不改工作区）。 |
| `SW_WEB_TASK_EVIDENCE` | `task_evidence` | 在上下文中维护运行内已核验的文件哈希、测试状态与暂存补丁证据。 |
| `SW_WEB_BUDGET_GUIDANCE` | `budget_guidance` | 在启用运行总预算时输出阶段与余量提示。 |
| `SW_WEB_PIPELINE_METRICS` | `pipeline_metrics` | 记录本地输入估算与服务端真实 token 的误差比，以及沙箱打包、校验、创建、执行与清理分段耗时。 |
| `SW_WEB_MULTI_EDIT_TOOLS` | `multi_edit_tools` | 扩展 `edit_file` 支持单文件一次最多 64 处非重叠精确替换，一次原子落盘。 |
| `SW_WEB_POST_EDIT_SYNTAX_HOOK` | `post_edit_syntax_hook` | 写入 `.py` 文件后执行内存 `ast.parse`，语法错误随当次工具结果返回并在压缩时保留。 |
| `SW_WEB_SMART_TOOL_TRUNCATION` | `smart_tool_truncation` | 上下文压缩时对超长工具输出按 35% 头 + 65% 尾保留，并为 `stdout`/`stderr` 双流分配配额。 |
| `SW_WEB_NAVIGATION_PREFILTER` | `navigation_prefilter` | 超过 100 个 `.py` 文件时先按符号名全仓库预筛并缓存源文件元数据，再做 AST 解析（依赖 `issue_navigation`）。 |
| `SW_WEB_MULTI_FILE_APPLY` | `multi_file_apply` | 开放跨文件事务写回工具 `apply_patch`（最多 12 文件，支持修改/新建/删除、I/O 补偿回滚与成组撤销）。 |
| `SW_WEB_PARALLEL_READ_TOOLS` | `parallel_read_tools` | 对连续的内置只读工具启用最多 4 路并发执行，同时保持 Journal 事件顺序与串行完全一致。 |
| `SW_WEB_SCOPED_TEST_GRANTS` | `scoped_test_grants` | 沙箱开发工具授予会话权限后，同工作区、沙箱边界与框架内可切换目标路径复用授权（依赖 `development_tools`）。 |
| `SW_WEB_INCREMENTAL_SANDBOX_SNAPSHOTS` | `incremental_sandbox_snapshots` | Worker→Broker 之间复用内容寻址基线缓存，仅上传变更文件与删除清单，摘要不符自动回退全量。 |
| `SW_WEB_IMPORT_AWARE_REPOMAP` | `import_aware_repomap` | RepoMap 对当前相关文件的直接导入方与被导入方加权，提高邻居模块进入预算窗口的召回。 |
| `SW_WEB_SUBAGENT_EXPLORATION` | `subagent_exploration` | 开放只读探索委派工具 `delegate_exploration`（Web 下最多 3 路滑动并发、支持静默流式与无 Thinking 收尾总结、最多 32 份文件哈希；可通过 `SW_WEB_EXPLORATION_MAX_MODEL_CALLS=20`、`SW_WEB_EXPLORATION_MAX_TOOL_CALLS=60`、`SW_WEB_EXPLORATION_TIMEOUT_SECONDS=300`、`SW_WEB_EXPLORATION_STALL_ROUNDS=3` 调整安全熔断）。 |
| `SW_WEB_CALIBRATED_CONTEXT_BUDGET` | `calibrated_context_budget` | 按最近 6 个服务端真实 `prompt_tokens` 样本动态校准本地估算系数，避免 200k 窗口过早压缩。 |
| `SW_WEB_STICKY_CONTEXT_CHECKPOINT` | `sticky_context_checkpoint` | 压缩后固定历史前缀检查点，可变证据置于末尾，减少重复压缩并提升前缀缓存稳定性。 |
| `SW_WEB_PROTECT_FRESH_TOOL_OUTPUT` | `protect_fresh_tool_output` | 压缩时逐字节保留最近 `SW_WEB_FRESH_TOOL_ROUNDS`（CLI `[context].fresh_tool_rounds`，默认 `2`）轮完整工具输出。 |

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

成员默认最多 3 个活跃运行、每个成员同时领取一个任务、每分钟最多创建 20 个新运行。`active_limit`、`weekly_tokens` 可按成员设置。`SW_G5_EXECUTOR_SLOTS` 控制执行容量。管理面板展示 UTC 周配额和项目审计，历史明细仍按 UTC 日聚合。变更成员配置时先排空任务，再重启 API 与 worker。团队 OIDC 验证范围见[评测与验收记录](EVALUATION.md#已记录的阶段验收)。
