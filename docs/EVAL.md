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

历史 V1 在 Windows 11 / Python 3.12.6 核对了 CLI 可执行入口、读改测、回放和恢复、工具边界、扩展以及分发。验收缺口修订后的全量检查记录为 471 通过、4 跳过。随后缺 Git 启动修复执行了 21 项专项回归。原始分阶段记录见[历史验收](archive/01_CLI_AGENT_RES.md)和[开发记录](archive/PROGRESS.md)。

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

G5 组织身份使用签名测试服务完成验收。接入实际组织身份源、部署 HTTPS 和数据库与工作区卷的联合恢复仍是待验收项。浏览器自动化里的普通对话使用 FakeModel，当前工作台沿用 DeepSeek 配置。 [阶段交付](archive/PROGRESS.md) · [后续验收](ROADMAP.md#工作台后续验收)

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

20 题入口使用 Python 包内的题库与评分器。历史报告、日志和工作区保存在本机，结果口径、token 用量和规则修订见[历史评测结果](archive/00_Sample_Run.md)。

## 修复过程的离线夹具

`locate-edit-recover` 在包含目标源码和无关说明文件的临时工作区运行。固定 FakeModel 依次枚举、搜索和读取，先用过期版本哈希尝试精确编辑，再用正确版本编辑并读取结果。参考目标和预期内容由运行器独立核验。

```powershell
uv run --locked --offline python -m stellarweaver.evals.local_fixtures
```

| 指标 | 判定依据 |
| --- | --- |
| 文件定位 Top-1 | 第一个去重后的搜索路径是否匹配预注册目标。 |
| 编辑可靠性 | 目标文件最终 SHA-256 是否匹配预期内容。 |
| 失败到恢复 | Journal 是否记录 `FILE_CHANGED`，重试后最终字节是否通过独立判定。 |
| 工具效率 | 从运行结果读取模型调用和工具调用数，从审批回调记录人工交互次数。 |

该夹具用于验收定位、编辑和冲突反馈能否贯通。定位泛化、真实任务修复率、多文件原子性和沙箱资源限制各有独立验收口径。真实 SWE-bench 结果使用清单分母和独立评分。 [夹具源码](../src/stellarweaver/evals/local_fixtures.py) · [统一评测协议](EVAL_PROTOCOL.md) · [安全边界夹具](SECURITY.md#本地工具边界评测)

## SWE-bench Lite 单题复现试跑

`stellarweaver.evals.swebench_pilot` 支持 Marshmallow 开发题 `marshmallow-code__marshmallow-1359`。从本机试跑资料读取公开题目、固定基线和测试依赖，为每次运行新建工作区。`replay` 可对原始基线或通过 `--patch` 提供的补丁执行复现。`agent` 默认使用现有 DeepSeek 配置，也可传 `--fake-script PATH` 离线回放 FakeModel。加 `--no-gate` 运行对照组，两组提供同样的模型工具。

模型获得仓库名和 `base_commit`，用 `run_visible_tests` 与 `run_reproducer` 执行固定的断网容器检查。两项工具只接受空对象，程序绑定实际命令和工作目录，并走精确授权。常规命令工具从试跑工具表移除。导出的候选在另一个干净基线上运行 `git apply --check`，只有可应用的非空补丁生成 `predictions.jsonl`。Agent 终态、验证和官方评分状态在报告中分别记录，FakeModel 产物标记为脚本化验收。

复现脚本来自公开 issue，运行器在修改前确认预期异常。候选补丁准备交付时，在断网容器里运行同一份只读脚本。首次复现失败会追加一次修复机会，模型请求、执行工具和复现都在原有运行预算内。报告保留脚本哈希、候选快照、退出码和异常摘要。官方测试补丁及评分留在单独的评分环境。

本机试跑资料位于 `.validation/swebench-lite-pilot`，其中 `agent-input.json` 只含题号和公开题面，`marshmallow-1359` 保存基线提交，`test-deps` 包含该题测试依赖。已有资料时，可在新的输出目录重跑。

```powershell
uv run --locked python -m stellarweaver.evals.swebench_pilot agent `
  --assets .validation/swebench-lite-pilot `
  --output .validation/swebench-gate-next --env-file .env
```

## SWE-bench Lite 批量评测

M1 的标准报告可从现有实验制品离线生成，包含尝试和评分身份、证据摘要、计划分母及配对统计。 [统一评测协议](EVAL_PROTOCOL.md) · [M0–M1 源码审计与验收](HARNESS_ACCEPTANCE.md#m0-m1-审计与评测契约)

批量入口为 `python -m stellarweaver.evals.swebench`。它固定 SWE-bench Lite 修订 `b0dde1093fe417d83b7184254edf8199c1f0dff5`，逐一校验 `dev` 和 `test` 的 Parquet SHA-256、题数和题号。`prepare` 生成冻结清单，每题的公开题面与评分资料分别保存在 `task.json` 和 `scorer.json`。Agent 只加载公开题面，仓库目录仅包含题目指定的基线提交。

新建批量实验时，模型请求次数默认不限。每题仍有 50 次工具调用、360 秒、累计 320,000 个输入 token 和 16,000 个输出 token 的上限。冒烟测试可以在 `prepare` 时指定 `--max-model-calls 16`。`run` 使用清单中冻结的预算，旧清单保留当时的配置。使用新默认值需要准备一个新的输出目录。上面的单题复现入口保留 16 次上限，以复现实验记录。

准备独立 Python 环境并安装 `swebench==5.0.2`，将该环境的 Python 路径传给 `--grader-python`。评测前为所选题缓存两个环境。可见测试使用操作方指定的 Python 镜像 ID、公开依赖和 pytest 参数。官方评分使用题目对应的完整镜像摘要。`--recipes` 读取按题号或 `owner/repo` 索引的 JSON 对象，题号配置优先。各条配置字段如下。

| 字段 | 值 |
|---|---|
| `image` | 已安装的可见测试镜像 ID，格式为 `sha256:<64 位十六进制>` |
| `pytest_args` | 固定 pytest 参数，如 `["-q", "tests/test_fields.py"]`，模型通过 `run_visible_tests` 触发 |
| `dependencies` | 可选，`--assets` 下的公开依赖目录，对容器只读 |
| `grader_image` | 已安装的官方镜像完整引用，格式为 `仓库@sha256:<64 位十六进制>` |

`--assets` 可以缓存以题号命名的浅层 Git 仓库。缺少缓存时，运行器从公开 GitHub 地址只获取基线提交，同时检查本地对象库中不存在其他提交。可见测试容器断网，只读挂载当前候选，并在有大小上限的临时目录中测试副本。测试生成的文件留在临时目录。官方评分进程读取独立评分资料，使用无宿主目录挂载的断网容器。

```powershell
$swRun = '.validation/swebench-dev-smoke'
$swData = '.validation/swebench-lite-dataset/dev.parquet'
$swSha = (Get-FileHash -LiteralPath $swData).Hash.ToLowerInvariant()
$swGrader = '.validation/swebench-lite-pilot/grader-venv/Scripts/python.exe'
$swAssets = '.validation/swebench-phase2-assets'
$swRecipes = "$swAssets/recipes.json"

uv run --locked python -m stellarweaver.evals.swebench prepare `
  --dataset $swData --dataset-sha256 $swSha --split dev `
  --grader-python $swGrader --recipes $swRecipes --output $swRun `
  --instance-id marshmallow-code__marshmallow-1359 `
  --instance-id pylint-dev__astroid-1333 --instance-id pydicom__pydicom-1694
uv run --locked python -m stellarweaver.evals.swebench run --output $swRun --assets $swAssets --env-file .env --live
uv run --locked python -m stellarweaver.evals.swebench export --output $swRun
uv run --locked python -m stellarweaver.evals.swebench evaluate --output $swRun --grader-python $swGrader
uv run --locked python -m stellarweaver.evals.swebench report --output $swRun
```

`run` 也接受 `--fake-scripts`，脚本文件名为 `<instance_id>.json`。重复执行 `run` 会跳过已经完成的尝试。中断或环境失败后，`--retry-interrupted` 建立新的尝试目录并保留原记录。`import --predictions PATH` 可以导入以前的 JSONL。`evaluate` 默认复用已完成的评分，`--rescore` 使用新评分 ID。导出、评分和汇总均使用保存的补丁，无需再次调用模型。`task_results.jsonl` 保留全部计划题目的状态，`aggregate.json` 始终使用计划题数作为分母。

旧版 23 题 JSON 快照可用 `--legacy-dev` 回放并固定文件哈希。清单会标记上游修订未经核验。该快照缺少官方执行脚本等评分字段，用 `--official-cases` 补入已保存的官方评分条目。若某题缺少必要的评分字段，记录为 `evaluation_error`。

### Phase 2 多仓库试跑记录

2026-11-20 已校验固定修订 `dev` Parquet，并生成完整的 23 题清单。前期试跑使用旧版 `dev` 快照。随后用固定修订核对三个样本的题号、仓库、基线提交和公开题面，确认一致，再将已有补丁导入官方评分。官方评分器版本为 `swebench 5.0.2`，镜像和补丁均按摘要归档。

| 样本 | Agent 证据 | 官方评分 |
|---|---|---|
| Marshmallow `1359` | 导入之前的 DeepSeek 补丁 | `resolved`，缺陷测试 1/1，回归测试 76/76 |
| Pydicom `1694` | Thinking High 试跑，16 次模型调用后耗尽预算，保存可应用补丁 | `resolved`，缺陷测试 1/1，回归测试 26/26 |
| Astroid `1333` | Thinking High 试跑，16 次模型调用后耗尽预算，补丁为空 | `not_evaluated` |

本机证据位于 `.validation/swebench-phase2-smoke-v2` 和 `.validation/swebench-phase2-official-sample`。重复执行 `evaluate` 已核对会保留先前的评分 ID。可见测试隔离调整后，使用 FakeModel 触发真实测试，Pydicom 基线通过 26 项，Astroid 在 Python 3.10 中通过 46 项。测试写入留在临时副本，两项检查属于可见测试验收。按新默认预算冻结的 23 题清单位于 `.validation/swebench-phase2-dev23-uncapped`。已准备 3 个题目的执行配方，其余环境和完整实跑仍待验收。

## 后续实验

先冻结任务、评分器、预算及运行配置，再逐步扩大覆盖。每轮保留项目提交、平台、依赖、模型、权限、请求用量和评分器哈希。

| 阶段 | 设计 | 指标与产物 |
| --- | --- | --- |
| 建立重复基线 | 同一模型、任务版本、授权和预算，每题先运行 3 次。保留运行顺序和失败记录。 | 报告独立评分通过率、受阻与失败数、工具错误、实际/估算 token、时长中位数及四分位间距。每次运行保存独立 report 与评分收据。 |
| 组件消融 | 在固定任务和模型上分别切换 repo map、上下文压缩或工具策略。一次只改变一项，每设置每题 3 次。需要增加开关时先在实验配置中实现。 | 配对比较任务成功、工作区证据、请求数和用量，并记录各组可用权限。包含长会话和需要跨文件定位的用例。 |
| 平台、隔离与恢复 | Windows、Linux、macOS 分别运行适用套件。对审批前、写入后、验证中断和执行器重启各重复 3 次，并执行数据库与 worktree 联合恢复。 | 记录未授权写入、重复副作用、未确认结果、容器清理、历史续接与恢复时间。数据库和系统平台差异单独记录。 |
| Harness 对照 | 匹配基线提交、任务、模型和预算后比较相同操作，无法统一模型时各自报告配置。 | 按工作流记录完成状态、人工确认步骤、补丁可合并性、失败恢复和验证证据。 [功能对照](archive/04_HARNESS_GAP_AUDIT.md#对照与剩余差距) |

新实验输出放在忽略提交的 `.validation/`，公开文档记录可复现配置与汇总。SWE-bench Lite 的分组和统计口径见[专题实施方案](EVAL_PLAN.md)，阶段顺序见[路线图](ROADMAP.md)。
