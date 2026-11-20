# 验证记录与实验计划

StellarWeaver 的验证覆盖工具行为、真实模型、持久化和浏览器操作。每项结果标注执行环境、输入和判定依据。历史报告按当时的代码、模型及评分规则解释。

## 复现入口

在项目根目录运行。默认测试使用本地文件、临时 Git 仓库、真实进程与离线模型夹具。

```powershell
uv sync --locked --extra web
uv run --locked --extra web ruff check .
uv run --locked --extra web ruff format --check .
uv run --locked --extra web mypy src/stellarweaver
uv run --locked --extra web pytest -q -ra
uv run --offline stellarweaver doctor --offline
uv run --offline python -m stellarweaver.demo
uv run --offline python -m stellarweaver.extensions.demo
```

| 验证层 | 输入和条件 | 可检查结果 |
| --- | --- | --- |
| CLI、Agent 与工具 | `tests/unit`、`tests/integration` 与离线演示。临时 Git 仓库带已知错误，测试环境提供 Git。 | 工具参数校验、精确编辑、哈希冲突、无 TTY 拒绝、真实测试失败后通过、超时和取消。 |
| 工作台数据库 | 迁移一个空的测试库，以应用角色设置 `STELLARWEAVER_TEST_DATABASE_DSN`。首次注册专项使用独立的空库和 `STELLARWEAVER_LOCAL_TEST_DSN`。`-m postgres` 选择数据库测试。 | 租户隔离、队列事务、事件游标、API 重启、审批、保留历史和账号归属。 |
| 工作区与验证容器 | 设置 `STELLARWEAVER_G4_TEST_IMAGE` 为本地已有的 Linux 验证镜像，例如 `python:3.12-slim-bookworm`，并提供可用的 Docker。 | 临时 worktree、真实验证退出码、超时或取消清理、补丁选择与复验。 |
| 浏览器 | 从 `frontend/` 运行 `npm ci`、`npm run build` 和 `npm run test:e2e`。Playwright 默认使用 Edge，部署型用例由 `SW_WEB_E2E_BASE_URL` 指向测试实例。 | 登录、项目切换、流式事件、审批、补丁、归档与恢复。具体夹具要求见[浏览器用例](../frontend/tests)和[测试配置](../frontend/playwright.config.ts)。 |
| 真实模型 | 显式选择 `--provider deepseek`，由 `.env` 提供密钥。`doctor --check-model` 用于连通性检查，评测命令用于记录真实任务。 | 可见回复、工具调用和实际用量，以运行记录及独立检查为准。 |
| Python 分发 | `uv build --offline`，以及 `tests/integration/test_packaging.py`。 | wheel、sdist、离线加载和本机数据排除。 |

数据库与 Docker 用例因夹具缺失而跳过时，pytest 输出列出原因。Edge 的本机账号测试需要配置 `SW_WEB_LOCAL_PROJECT_A` 和 `SW_WEB_LOCAL_PROJECT_B`，指向可选择的两个已提交 Git 夹具。补丁交接测试使用 `SW_WEB_LOCAL_REVIEW_PROJECT` 指定含 `calc.py`、`test_calc.py` 的已提交修复夹具，并启用修复与隔离执行。使用测试账号和测试库可以覆盖首次注册、继承、跨账号隔离与注销后的重新登录。命令和部署配置见[使用指南](USAGE.md)和[数据库](DATABASE.md)。

## 已记录的验收

### CLI 与模型适配

历史 V1 在 Windows 11 / Python 3.12.6 核对了 CLI 可执行入口、读改测、回放和恢复、工具边界、扩展以及分发。验收缺口修订后的全量检查记录为 471 通过、4 跳过。随后缺 Git 启动修复执行了 21 项专项回归。原始分阶段记录见[历史验收](archive/V1_ACCEPTANCE.md)和[开发记录](archive/PROGRESS.md)。

模型协议测试使用实际 SDK 和 `httpx.MockTransport`，覆盖请求字段、流式分片、工具 ID 配对、缺失用量、截断、断连及错误重试。真实 M2 小任务记录了测试退出码 `1 → 0`、14 条独立行为断言通过，以及模型实际请求与工具结果。默认窗口随后从 32,768 调整为 100,000，历史真实任务保留原配置。

#### DeepSeek Thinking 接入

用 `deepseek-flash`、Thinking `enabled`、`high` 对同一份合成调度文件分别运行流式和非流式验证。首个请求触发一次只读 `read_file`。第二个请求返回的结果与独立枚举一致，第三个请求回传了上一轮非空推理并成功回答追问。下表使用服务端实际用量，推理 token 已计入输出。

| 模式 | 模型请求 | 输入 token | 输出 token | 其中推理 token | 总耗时 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 流式 | 3 | 6,699 | 2,940 | 2,890 | 21.2 秒 |
| 非流式 | 3 | 7,980 | 4,233 | 4,183 | 28.8 秒 |

早期使用 4,096 token 输出上限的组合推理探测耗尽输出预算，服务端返回 `length`。主链路随后将 DeepSeek Thinking 的默认单次输出上限调整到 8,192。测试同时验证了 SQLite 无原始推理、历史恢复和压缩后工具 ID 配对。原有 20 题评测入口保留 Non-thinking 配置，SWE-bench Lite 成绩沿用历史记录。

### 工作台与部署验证

| 阶段 | 记录的执行范围 | 结果 |
| --- | --- | --- |
| G1–G2 | PostgreSQL、临时 Git 仓库、Edge 与一次 DeepSeek 编辑试跑。 | 浏览器恢复历史、审批和报告下载，离线修复测试退出码 `1 → 0`。DeepSeek 试跑完成读取和经批准编辑，该任务的验证命令未运行。 |
| G3 | 队列、Compose、进程中断与数据库转储恢复。 | API 重启后仍可领取，未确认的操作保留待核对状态。Compose 交付批次 40 项相关回归通过。 |
| G4 | 私有 worktree、选中改动块复验、继续和取消。 | 安装了真实执行环境的全量测试 499 通过、4 跳过。后续线程界面迭代另有真实 DeepSeek 只读流式与用量显示记录。 |
| G5 | 两账号跨租户、签名 OIDC 夹具、并行容器、执行器中断与 PostgreSQL 回灌。 | 全量 501 通过、4 跳过，Edge 端到端 2 项通过。执行器重启清理遗留容器，正在验证的任务保留未确认结果。本机部署另验证一次真实 DeepSeek 回复及用量结算。 |
| 本机账号与多工作区 | 注册、历史继承、项目选择、归档、普通对话及源目录隔离。 | Web/PostgreSQL 回归 25 项通过，Agent 与上下文专项 44 通过、2 跳过，Edge 流程通过。`8787` 登录和注册界面、旧令牌拒绝及历史数量已在线核对。 |
| 审阅反馈与补丁交接 | 本机账号发起同线程修订、选择改动复验、检查目标分支、导出经复验的补丁。 | Web 回归 26 项通过。最终换行兼容改动后重跑交接专项，并在 CRLF 工作区完成 1 项 Edge 端到端测试。覆盖跨账号拒绝、反馈幂等与过期反馈、本地改动、暂存和基线冲突。 |
| 用量面板与 Skills 库 | 北京时间今日用量、UTC 额度、审计和读取时间的 30 秒刷新，已登录用户只读浏览技能。 | 相关 Python 回归 45 项通过，Edge 跨午夜刷新及技能浏览通过。G6 → G7 迁移验证已有事件的回填和缺少事件时的兼容处理。 |

G5 组织身份使用签名测试服务完成验收。接入实际组织身份源、部署 HTTPS 和数据库与工作区卷的联合恢复仍是待验收项。浏览器自动化里的普通对话使用 FakeModel，当前工作台沿用 DeepSeek 配置。阶段明细见[历史进度](archive/PROGRESS.md)和[路线图](ROADMAP.md)。

## 20 题评测及评分口径

固定任务集覆盖缺陷修复、小功能、跨文件修改、测试补充和源码理解。运行终态、修改范围和独立行为检查三项共同决定任务是否通过。断言、变异检测及文件哈希为评分提供证据。

| 运行 | 结果 | 证据范围 |
| --- | --- | --- |
| FakeModel 参考动作 | 20/20 通过 | 验证工具、持久化、固定夹具和独立评分链路。 |
| DeepSeek `deepseek-flash` 历史实测 | 每题 1 次，初评 19/20 | 固定小任务集。20 项运行均正常结束。 |
| 评分规则修订后的重评分 | 20/20，新增模型调用 0 次 | Q01 修正与题目无关的额外读取要求，保留初评记录，并从同一批报告和工作区重算。 |

```powershell
uv run python -m stellarweaver.evals run --provider fake
uv run python -m stellarweaver.evals run --provider deepseek --env-file .env --output .validation/live-eval
```

待再次评测时使用新的输出目录。公开仓库提供用例、评分器和规则来源，历史原始报告、日志和工作区保存在本机。历史结果的时间、token、分母和规则修订见[评测结果](../evals/RESULTS.md)。重评分及来源记录见[评测手册](../evals/README.md)与[评分规则档案](../evals/provenance/README.md)。

## SWE-bench Lite 单题复现试跑

`stellarweaver.evals.swebench_pilot` 支持 Marshmallow 开发题 `marshmallow-code__marshmallow-1359`。从本机试跑资料读取公开题目、基线 checkout 和测试依赖，为每次运行新建工作区。`replay` 可对原始基线或通过 `--patch` 提供的补丁执行复现。`agent` 使用现有 DeepSeek 配置，可加 `--no-gate` 运行对照组，两种配置提供相同的测试命令。

复现脚本来自公开 issue，运行器在修改前确认预期异常。候选补丁准备交付时，在断网容器里运行同一份只读脚本。首次复现失败会追加一次修复机会，模型请求、执行工具和复现都在原有运行预算内。报告保留脚本哈希、候选快照、退出码和异常摘要。官方测试补丁及评分留在单独的评分环境。

本机试跑资料位于 `.validation/swebench-lite-pilot`，其中 `agent-input.json` 只含题号和公开题面，`marshmallow-1359` 保存基线提交，`test-deps` 包含该题测试依赖。已有资料时，可在新的输出目录重跑。

```powershell
uv run --locked python -m stellarweaver.evals.swebench_pilot agent `
  --assets .validation/swebench-lite-pilot `
  --output .validation/swebench-gate-next --env-file .env
```

## 后续实验

先冻结任务、评分器、预算及运行配置，再逐步扩大覆盖。每轮保留项目提交、平台、依赖、模型、权限、请求用量和评分器哈希。

| 阶段 | 设计 | 指标与产物 |
| --- | --- | --- |
| 建立重复基线 | 同一模型、任务版本、授权和预算，每题先运行 3 次。保留运行顺序和失败记录。 | 报告独立评分通过率、受阻与失败数、工具错误、实际/估算 token、时长中位数及四分位间距。每次运行保存独立 report 与评分收据。 |
| 组件消融 | 在固定任务和模型上分别切换 repo map、上下文压缩或工具策略。一次只改变一项，每设置每题 3 次。需要增加开关时先在实验配置中实现。 | 配对比较任务成功、工作区证据、请求数和用量，并记录各组可用权限。包含长会话和需要跨文件定位的用例。 |
| 平台、隔离与恢复 | Windows、Linux、macOS 分别运行适用套件。对审批前、写入后、验证中断和执行器重启各重复 3 次，并执行数据库与 worktree 联合恢复。 | 记录未授权写入、重复副作用、未确认结果、容器清理、历史续接与恢复时间。数据库和系统平台差异单独记录。 |
| Harness 对照 | 匹配基线提交、任务、模型和预算后再比较相同操作，无法绑定同一模型时独立报告各产品配置。 | 按工作流记录是否完成、人工确认步骤、补丁可合并性、失败恢复和验证证据。产品能力另见[功能对照](ROADMAP.md#harness-能力对照)。 |

新实验输出放在忽略提交的 `.validation/` 或 `evals/runs/`，公开文档记录可复现配置与汇总。阶段通过标准见[路线图](ROADMAP.md)。
