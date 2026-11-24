# 评测体系、验证记录与实验计划

本页统一说明 StellarWeaver 的**验证复现入口**、**统一评测协议（数据契约与配对口径）**、**SWE-bench Lite 批量评测与实验设计**以及**已记录的阶段验收**。所有实验遵循“题面与评分资料隔离、计划分母恒定、可离线重汇总与独立重评分”原则。历史报告按当时的代码、模型与评分规则解释。

## 目录

- [复现入口](#复现入口)
- [统一评测协议](#统一评测协议)
- [修复过程离线夹具与 20 题评测](#修复过程离线夹具与-20-题评测)
- [SWE-bench Lite 批量评测与单题试跑](#swe-bench-lite-批量评测与单题试跑)
- [SWE-bench Lite 实验设计与对照规划](#swe-bench-lite-实验设计与对照规划)
- [已记录的阶段验收](#已记录的阶段验收)

---

## 复现入口

在项目根目录执行。默认离线回归使用本地文件、临时 Git 仓库、真实子进程与 `FakeModel` 夹具，无需外网和模型密钥。

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
| CLI、Agent 与 Harness 流水线 | `tests/unit`、`tests/integration` 与离线演示。临时 Git 仓库带已知错误，环境提供 Git。 | 工具校验、单文件多块编辑、跨文件补丁与成组撤销、只读并发保序、上下文校准与粘性检查点、导航预筛与缓存、只读探索委派及预算恢复。 |
| 工作台数据库 | 迁移一个空的测试库，以应用角色设置 `STELLARWEAVER_TEST_DATABASE_DSN`。首次注册专项使用 `STELLARWEAVER_LOCAL_TEST_DSN`。用 `-m postgres` 选择。 | 租户隔离、队列事务、事件游标、API 重启、审批、成组撤销、跨轮测试授权复用、父子探索用量结算与账号归属。 |
| 工作区与验证容器 | 设置 `STELLARWEAVER_G4_TEST_IMAGE` / `STELLARWEAVER_M5_TEST_IMAGE` / `STELLARWEAVER_BATCH3_TEST_IMAGE` 为本地已有的 Linux 验证镜像（如 `python:3.12-slim-bookworm`），并提供 Docker。 | 临时 worktree、真实验证退出码、增量快照展开与权限校验、超时或取消清理、补丁选择与复验。 |
| 浏览器 | 在 `frontend/` 运行 `npm ci`、`npm run build` 和 `npm run test:e2e`。Playwright 默认使用 Edge，部署型用例由 `SW_WEB_E2E_BASE_URL` 指向测试实例。 | 登录、项目切换、流式事件、审批、补丁交接、归档与恢复。见[浏览器用例](../frontend/tests)与[测试配置](../frontend/playwright.config.ts)。 |
| 真实模型 | 显式选择 `--provider deepseek`，由 `.env` 提供密钥。`doctor --check-model` 检查连通性。 | 可见回复、DeepSeek Thinking 回传、工具调用和实际用量。 |
| Python 分发 | `uv build --offline` 及 `tests/integration/test_packaging.py`。 | wheel、sdist、离线加载与本机数据排除。 |

数据库与 Docker 用例在缺少独立夹具时会跳过并列出原因。部署与数据库迁移步骤见[使用与部署](USAGE.md)和[数据库文档](DATABASE.md)。

---

## 统一评测协议

M1 定义的标准化评测契约从既有实验制品离线生成报告，无需重新请求模型或启动评分容器。独立 SWE-bench Lite 批量与试跑配置固定为 **100,000 token** 窗口，交互式 CLI 与 Web 默认采用 **200,000 token**；窗口变化需在实验设置中单独登记。

### 离线报告与配对命令

```powershell
uv run --locked --offline python -m stellarweaver.evals.swebench_contract report .validation/swebench-phase2-official-sample
uv run --locked --offline python -m stellarweaver.evals.swebench_contract report .validation/swebench-phase2-official-sample --output .validation/sample-contract.json
uv run --locked --offline python -m stellarweaver.evals.swebench_contract compare .validation/BASELINE .validation/VARIANT --vary source_sha256
```

报告默认写到标准输出；`--output` 必须指向源实验目录之外的新文件（拒绝覆盖）。输出 JSON 键序稳定且不含生成时间，同一输入字节可复现同一个输出 SHA-256。

### 数据契约与身份绑定

| 记录 | 绑定信息 |
| --- | --- |
| **Manifest** | 契约版本、suite、数据集修订和校验哈希、split、有序题号、Harness 提交及 Python 源码哈希、预算、评分器版本、配置哈希。实验 ID 由原始 manifest 哈希导出。 |
| **Task / Attempt** | 每题保留 Git 基线与历次尝试；`attempt.json` 记录单次尝试，`solve.json` 指向当前尝试。保存运行态、候选态、补丁哈希、模型设置、工具 schema/策略/配置哈希、调用次数、耗时及用量来源。 |
| **Score** | 独立评分 ID、题号与尝试关联、补丁哈希、评分器版本、镜像摘要、评分输入哈希与原始报告哈希。每次独立评分生成新评分目录，`evaluation.json` 指向当前评分。 |
| **Evidence** | 源实验内的相对路径、SHA-256 和字节数，索引公开题面、尝试收据、SQLite Journal、候选补丁与评分日志，不复制隐藏测试和参考答案。 |

### 分母、计量与配对规则

- **计划分母恒定**：`resolved_rate` 始终使用官方 `resolved` 数除以 manifest 中预注册的计划题目总数；`graded` 仅计官方已判定的通过与未通过题。未开始、空补丁、预算耗尽、策略拦截、环境错误和未评分各自单列。
- **状态分离**：`run_status`（Agent 运行终态）、`candidate_status`（候选补丁状态）、`evaluation_status`（独立官方评分状态）和 `security_assessment` 严格分开；`completed` 不等于 `resolved`。
- **用量来源**：`usage_source` 明确区分 `reported`（服务端返回）、`estimated`（估算）、`incomplete` 与 `unavailable`。
- **严格配对比较（`compare`）**：按 `instance_id` 配对，先核对数据修订、split、基线、冻结输入、预算和策略等控制条件，再核对模型身份、隔离环境和评分器。`--vary` 仅允许显式声明 `source_sha256`、`config_sha256`、`prompt_sha256` 或 `tools_schema_sha256` 变化；不满足可比条件的样本列出排除原因，绝不强行合并。

---

## 修复过程离线夹具与 20 题评测

### 修复过程与安全边界离线夹具

```powershell
uv run --locked --offline python -m stellarweaver.evals.local_fixtures
```

`locate-edit-recover` 在临时工作区使用固定 `FakeModel` 依次枚举、搜索、读取，先以过期版本哈希触发 `FILE_CHANGED`，再用最新哈希精确编辑并校验最终字节。指标涵盖文件定位 Top-1、编辑可靠性（SHA-256）、冲突恢复与工具效率。配套安全边界夹具见[工具安全与授权](SECURITY.md#本地工具边界评测)。

### 历史 20 题内部评测

固定任务集覆盖缺陷修复、小功能、跨文件修改、测试补充和源码理解，由运行终态、修改范围和独立行为断言共同判定。历史基线与重评分证据见[历史内部评测结果](archive/00_Sample_Run.md)。

| 运行 | 结果 | 证据范围 |
| --- | --- | --- |
| FakeModel 参考动作 | 20/20 通过 | 验证工具、持久化、固定夹具和独立评分链路。 |
| DeepSeek `deepseek-flash` 历史实测 | 每题 1 次，初评 19/20 | 固定小任务集，20 项运行均正常结束。 |
| 评分规则修订后的重评分 | 20/20，新增模型调用 0 次 | Q01 修正与题目无关的额外读取要求，利用原制品重算。 |

```powershell
uv run python -m stellarweaver.evals run --provider fake
uv run python -m stellarweaver.evals run --provider deepseek --env-file .env --output .validation/live-eval
```

---

## SWE-bench Lite 批量评测与单题试跑

### 批量评测入口（`stellarweaver.evals.swebench`）

批量入口固定 SWE-bench Lite 上游修订 `b0dde1093fe417d83b7184254edf8199c1f0dff5`，校验 `dev`（23 题）和 `test`（300 题）的 Parquet SHA-256 与题号。`prepare` 将公开题面写入 `task.json`，隐藏测试与参考字段隔离在 `scorer.json`。

- **冻结窗口与预算**：`prepare` 默认冻结 200,000 token 上下文窗口（旧清单缺少该字段时回退 100,000），不限模型请求次数；默认每题约束 `max_tool_calls=50`、`max_wall_seconds=360`、累计输入 `320,000` token、累计输出 `16,000` token，并全部支持通过 `--context-window-tokens`、`--max-model-calls`、`--max-tool-calls`、`--max-wall-seconds`、`--max-input-tokens-total`、`--max-output-tokens-total` 在 `prepare` 阶段自定义冻结（DeepSeek Thinking 建议调大输出与输入总量，且对照两组保持一致）。
- **执行配方（`--recipes`）**：按题号或 `owner/repo` 索引 JSON 对象，包含 `image`（本地可见测试镜像 `sha256:...`）、`pytest_args`（固定可见测试参数）、可选 `dependencies`（`--assets` 下只读依赖目录）和 `grader_image`（官方评分镜像 `repo@sha256:...`）。
- **独立评分环境**：在独立 Python 环境安装 `swebench==5.0.2`，通过 `--grader-python` 传入。官方评分容器断网且不挂载宿主目录。

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

`run` 支持 `--harness-profile baseline|full` 一键预设（默认 `baseline` 全关，`full` 开启全部已交付 Harness 能力与 14 项流水线开关），并支持在预设上以显式开关或 `--no-<开关>` 做单因素消融覆盖（如 `--harness-profile full --no-subagent-exploration`），同时支持 `--fake-scripts` 离线回放与 `--retry-interrupted` 断点重试。展开后的完整配置与 `config_sha256` 均冻结写入 `run-settings.json` 防漂移。`evaluate` 默认校验并复用已完成评分，`--rescore` 生成新评分 ID。

### 单题复现试跑入口（`stellarweaver.evals.swebench_pilot`）

用于 `marshmallow-code__marshmallow-1359` 的公开 issue 复现闸门对照：Agent 仅使用固定 `run_visible_tests` 与 `run_reproducer`，候选交付前在断网容器内运行只读复现脚本，首次失败可追加一次修复机会。

```powershell
uv run --locked python -m stellarweaver.evals.swebench_pilot agent `
  --assets .validation/swebench-lite-pilot `
  --output .validation/swebench-gate-next --env-file .env
```

### Phase 2 三题试跑证据

2026-11-20 已校验固定修订 `dev` Parquet 并冻结 23 题清单（`.validation/swebench-phase2-dev23-uncapped`），已有三题官方评分（`swebench 5.0.2`）：

| 样本 | Agent 证据 | 官方评分 |
| --- | --- | --- |
| Marshmallow `1359` | 导入早期 DeepSeek 补丁 | `resolved`（缺陷测试 1/1，回归 76/76） |
| Pydicom `1694` | Thinking High，16 次请求耗尽预算，保存可应用补丁 | `resolved`（缺陷测试 1/1，回归 26/26） |
| Astroid `1333` | Thinking High，16 次请求耗尽预算，补丁为空 | `not_evaluated` |

---

## SWE-bench Lite 实验设计与对照规划

实验按 **「离线夹具验收 → 23 道 `dev` 开发与验收 → 冻结配置后的 300 道 `test` 正式基线」** 推进。

### 架构与数据边界

| 环节 | 输入与职责 | 对外证据 |
| --- | --- | --- |
| **Agent** | 仅获取公开题面、仓库名和基线提交，每题独立工作区，断网容器运行可见测试，关闭网页搜索与宿主凭据。 | 终态、事件、用量、候选 diff、可应用性与快照哈希。 |
| **评分器** | 独立读取隐藏 `test_patch`、官方镜像和候选补丁，判定 `FAIL_TO_PASS` 与 `PASS_TO_PASS`。 | 补丁哈希、镜像摘要、评分器版本、评分 ID 与原始报告。 |
| **汇总与看板** | 按清单汇总全部分母，保留脱敏证据供工作台「实验记录」筛选和配对对照。 | 指标汇总、失败归因、配对矩阵与脱敏 JSON。 |

### 阶段目标与消融分组

| 组别 | 配置设计 | 研究目标 |
| --- | --- | --- |
| `A0` / `A1` | DeepSeek Flash Non-thinking vs Thinking High（同环境预算） | 测量思考模式对修复率与成本的影响。 |
| `B0`（正式基线） | Thinking High + 冻结的统一安全沙箱与固定预算 | 建立可重复的正式 Harness 基线。 |
| `B1`–`B5`（单变量消融） | 在 `B0` 上分别独立开关：导航与 RepoMap（`H1/N4`）、受控开发工具与修复反馈（`H0/H2`）、多块/跨文件编辑与只读并发（`N1/N2`）、校准上下文与粘性前缀缓存（`H4/N4`）、只读探索委派（`N6-b`） | 固定题号、模型与总预算，按 `instance_id` 配对检验各模块对通过率、Token 费用、缓存命中与耗时的真实贡献。 |

统计口径：对同一实例的配对通过差异使用 McNemar 或配对 bootstrap 报告区间；费用按冻结单价核算，`Cost per Resolved` 在通过数为零时记为 `N/A`。正式 `dev` 23 题与 `test` 300 题启动前须单独确认预算、镜像缓存与停止条件。

---

## 已记录的阶段验收

### DeepSeek Thinking 协议实测

使用 `deepseek-flash`（Thinking `enabled`、`high`）对合成文件分别验证流式与非流式工具回合，确认推理内容回传、SQLite 不落盘原始推理及工具 ID 严格配对（推理 token 计入输出）：

| 模式 | 模型请求 | 输入 token | 输出 token | 其中推理 token | 总耗时 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 流式 | 3 | 6,699 | 2,940 | 2,890 | 21.2 秒 |
| 非流式 | 3 | 7,980 | 4,233 | 4,183 | 28.8 秒 |

### 工作台与 Harness 演进验收摘要

| 阶段 | 执行范围 | 核心验收结论 |
| --- | --- | --- |
| CLI V1（M0–M5） | Windows 11 / Python 3.12，CLI 读改测、恢复、扩展与打包 | 471 通过、4 跳过；见[V1 历史验收](archive/01_CLI_AGENT_RES.md)。 |
| Web G1–G5 | PostgreSQL 队列、隔离 worktree、断网执行器、OIDC 夹具与 Edge 端到端 | 501 通过、4 跳过；执行器中断清理与跨租户隔离通过。 |
| Web G6–G11 | 本机账号、多项目、审阅反馈、补丁交接、UTC 周额度、Skills 库与默认 200k 窗口 | 200k 窗口回归 152 项 Python + 3 项浏览器测试通过。 |
| Harness M0–M10 | 统一评测契约、L0–L3、Hooks、沙箱开发工具、导航、修复控制、补丁暂存与实验看板 | 548 项离线测试通过 + 38 项 PG/Docker 补跑通过；明细见[Harness 验收记录](HARNESS_ACCEPTANCE.md)。 |
| Harness N 阶段（Batch 1–3）与上下文优化 | 多块/跨文件编辑与成组撤销、4 路只读并发、沙箱测试授权复用、增量快照、导航缓存与导入加权、只读探索 Sub-agent、动态 Token 校准与粘性前缀检查点 | 586 项离线测试通过（0 失败）+ 临时 PG 结算/撤销与真实 Docker 展开通过；明细见[Harness 验收记录](HARNESS_ACCEPTANCE.md#n-阶段-batch-1-3-与上下文优化验收)。 |

更早期的分阶段开发流水记录保存在[开发进度](archive/PROGRESS.md)，外部工作流对比保存在[Harness 差距审计](archive/04_HARNESS_GAP_AUDIT.md)。
