# Harness M0–M10 实施与验收

本文汇总从源码审计、统一评测契约到工作台实验看板的实施和验收记录。各阶段的测试数据按执行批次保留，历史审计以当时的代码和部署为准。

| 阶段 | 主要交付 |
| --- | --- |
| [M0–M1](#m0-m1-审计与评测契约) | 源码审计、统一评测契约、历史评分重汇总和样本配对。 |
| [M2–M4](#m2-m4-固定夹具与授权) | 固定边界夹具、工具风险与授权、可信 Hook 及审批复核。 |
| [M5–M8](#m5-m8-沙箱与修复工具) | 容器沙箱、受控开发工具、Python 导航与修复控制。 |
| [M9–M10](#m9-m10-补丁暂存与实验看板) | 补丁暂存、任务证据、预算提示、实验看板与正式评测预注册。 |

## 当前实现进度核对

2026-11-22 核对基于当前 HEAD `91b7164` 与工作树，包括 M0–M10 之后的工作台增量。下表反映本次核对，后续各里程碑章节保留原批次的验收结果。总方案开头的架构图描述目标状态，M11–M12 的正式实验待执行。 [目标架构图](StellarWeaver_Harness_Complete_Improvement_Plan_2026-11-21.md#理想完成态-harness-架构图目标架构不是当前已经实现的功能清单) · [当前架构](ARCHITECTURE.md)

| 架构层 | 已接入能力 | 实现范围与待验收项 |
| --- | --- | --- |
| 交互与任务入口 | React、CLI、FastAPI、账号与项目权限、PostgreSQL 队列及 worker。Web 支持普通对话、带证据续接、流式重连及脱敏 JSON／HTML 导出。 | 两种入口共用 Agent，Web 按线程保存独立 worktree。待验收项包括从指定轮次派生线程、数据库与 worktree 联合恢复。 |
| Agent 决策与修复 | DeepSeek Thinking、Python Repo Map、符号与测试导航、错误分类、修复反馈及可选的重复失败终止。CLI 和 Web 默认 200k，Web 可自适应归纳较早的完整工具回合。 | 单次请求窗口按运行保存。超 100k 输入和长程压缩已有离线回归，真实模型下的 100k／200k 对照仍待运行。 |
| 任务证据与预算 | 运行内证据关联操作 ID、事件序号和文件哈希。Web 跨轮从 Journal 提取文件、检查和技能版本，并复核当前快照。 | Web 使用单次请求窗口、操作超时和账户周额度。独立评测沿用冻结预算，阶段级预算调度及通用假设跟踪仍待实现。 |
| 工具、权限与隔离 | 路径硬守卫、L0–L3、可信 Hook、审批及复核已接入。Git worktree 和断网验证容器支持测试、发现、语法检查和 Ruff。 | CLI 默认使用宿主执行器，Web 受当前运行模式及已登记容器配置约束。 |
| 技能与扩展 | Web 提供共享库、按成员与项目的技能信任、按版本选用技能和模型自动加载。CLI 提供可信 Skills 和 stdio MCP。 | Web 模型自主加载已有离线链路验收，实际任务的选用效果及 Web MCP 集成待验收。 |
| 多文件补丁 | `stage_patch` 支持校验、授权和导出暂存补丁。Web 的既有交付流程支持选择普通修复的改动块，并在独立 worktree 应用和复验。 | 暂存制品目前通过下载交接，尚未接入逐块复验与事务式写回。 |
| 撤销 | Web 在隔离线程内对最近一次已记录的文件创建或编辑发起撤销，校验哈希及备份后生成新运行和候选。 | 离线真 worktree 回归覆盖恢复、哈希冲突和原报告保留。 |
| 评测、统计与看板 | SWE-bench Lite 批量运行、独立评分、缓存、离线汇总和严格题号配对；看板读取历史轨迹、批量评测和固定边界夹具，支持筛选和脱敏导出。 | SWE-bench 与本地安全夹具仍分别运行。跨套件编排、外部安全适配、实际费用趋势和正式消融统计待交付。 |

本机 Compose 更新至 G11，API 和 worker 均加载 200k 默认值。窗口迁移回填已有模型事件，核对旧 100k 任务的原始事件和终态。近期工作台增量的测试与部署证据汇总见验证文档。 [工作台与部署验证](EVAL.md#工作台与部署验证) · [当前部署配置](USAGE.md#本机-compose)

工作台可写项目的新任务默认使用 `auto`。只读与修改仍可选择，既有运行保留自己的模式。SWE-bench 使用独立的无人值守策略。 [运行模式与分流](SECURITY.md#运行模式与分流)

目标图中列为可选的版本化项目知识、跨会话项目记忆、面向被测项目的开发服务与浏览器验证，以及多 Agent、多候选均尚未实现。已有浏览器验收覆盖工作台本身。

最新 M10 验收记录为 548 项离线测试通过、27 项跳过，其中数据库相关用例随后在临时环境补验。看板登记了五份报告，三道历史 SWE-bench 题中两道通过独立评分、一道未评分。23 道开发题清单尚无独立评分，可用配对数为 0。固定安全夹具通过三项边界检查和两项正常任务。正式 23/300 题、外部安全评测及其统计分析属于 M11–M12。 [验证证据](#m9-m10-验证证据)

## M0-M1 审计与评测契约

审计基线为 `7ececb5c34ccd33b9fb4f47738421d95d548b7cf`（`master`，开始时工作树干净）。总方案记录的是 `c981a915de368776dd6413a848bffe3a6b70a61c`。两者之间只新增了总方案及其归档版本，无运行时代码差异。总方案开头的蓝色节点表示已有主体，橙色节点表示新增、统一或增强目标。一个橙色节点也可能已有局部实现。 [总方案](StellarWeaver_Harness_Complete_Improvement_Plan_2026-11-21.md)

### M0 源码与测试清单

下表记录 M0 审计时的状态。M2 至 M10 的后续实现见对应章节。

| 判定 | 能力与边界 | 可复核位置 |
| --- | --- | --- |
| 已有 | Web、CLI 共享 `build_agent` 和单 Agent 循环。工具串行分派，具备模型与工具预算、取消、恢复。DeepSeek Thinking 的流式与非流式回传已有离线协议测试。 | [装配](../src/stellarweaver/runtime/service.py) · [主循环](../src/stellarweaver/runtime/loop.py) · [Thinking 回归](../tests/integration/test_deepseek_thinking.py) |
| 已有 | SWE-bench 已有 `prepare/run/import/export/evaluate/report`。固定数据集、题目顺序和基线，分开保存 `task.json` 与 `scorer.json`，可见测试和独立官方评分使用既有执行路径。 | [批量入口](../src/stellarweaver/evals/swebench.py) · [官方评分](../src/stellarweaver/evals/swebench_official.py) · [批量回归](../tests/integration/test_swebench_batch.py) |
| 已有 | 内部评测有评分来源快照与不可覆盖的重评分收据。SQLite/PostgreSQL Journal 保存消息、工具意图和结果，可支撑结果溯源。 | [评分来源](../src/stellarweaver/evals/provenance.py) · [来源回归](../tests/integration/test_eval_provenance.py) · [CLI Journal](../src/stellarweaver/session/store.py) · [Web Journal](../src/stellarweaver/web/journal.py) |
| 已有 | `ToolRegistry` 校验 schema 并执行准备、授权与复核。策略有三种预设和精确路径或 argv 授权。Web 审批绑定运行与操作指纹、有效期和单次消费。 | [工具注册](../src/stellarweaver/tools/registry.py) · [策略](../src/stellarweaver/policy/__init__.py) · [审批](../src/stellarweaver/web/approval.py) · [策略回归](../tests/unit/test_policy.py) · [复核回归](../tests/unit/test_registry_authorization_hooks.py) |
| 部分实现 | Python AST Repo Map 提取符号并为文件排序。搜索有 `rg` 与 Python 回退。独立 symbol/reference/test 导航、异常栈感知和调用关系索引仍待建设。 | [Repo Map](../src/stellarweaver/context/repo_map.py) · [搜索](../src/stellarweaver/tools/search.py) · [Repo Map 回归](../tests/unit/test_repo_map.py) |
| 部分实现 | 文件工具进行路径校验、SHA-256 复查和单文件原子写入，工作台可导出、复验已有补丁。模型可调用的多文件 staging 和事务式 Patch API 仍缺。 | [文件工具](../src/stellarweaver/tools/files.py) · [文件回归](../tests/unit/test_files.py) · [Web 审阅](../src/stellarweaver/web/review.py) |
| 部分实现 | 上下文按完整工具回合做确定性压缩。已有重复失败终止和可选完成回调，Marshmallow 旧试跑接入过固定复现门槛；批量 manifest 默认关闭 completion gate。任务级 Evidence State、通用修复控制器与自适应预算仍缺。 | [上下文](../src/stellarweaver/context/manager.py) · [上下文回归](../tests/unit/test_context.py) · [试跑回调](../src/stellarweaver/evals/swebench_pilot.py) |
| 部分实现 | 现有 guard 和授权前后回调是内部路径。CLI 支持可信 Skills 和 stdio MCP，Web Skills 为只读浏览。风险引擎、可配置的可信 Hook 管理器及 Web 扩展统一装配仍缺。 | [工具调度](../src/stellarweaver/tools/registry.py) · [CLI 扩展](../src/stellarweaver/extensions/) · [Web Skills](../src/stellarweaver/web/skills.py) |
| 部分实现 | Web 有私有 worktree 和断网的一次性验证容器，主要执行预配置命令。CLI `run_command` 使用宿主进程，Windows Job 负责进程生命周期。通用沙箱 profile、统一命令隔离和开发服务器或浏览器验证仍待实现。 | [验证隔离](../src/stellarweaver/web/isolation.py) · [执行服务](../src/stellarweaver/web/broker.py) · [宿主命令](../src/stellarweaver/tools/commands.py) · [隔离回归](../tests/integration/test_web_g4.py) |
| 部分实现 | 实验界面读取一份已发布报告，可查看事件和评分证据；多实验筛选、按样本轨迹对照、安全指标界面仍缺。M1 导出独立 JSON，未扩大此界面的读取契约。 | [实验视图](../frontend/src/experiment.tsx) · [实验 API](../src/stellarweaver/web/app.py) |
| 缺失 | 到 M0 审计时，统一 Manifest/Attempt/Score/Evidence、哈希校验与配对汇总尚未接通。M1 补齐 SWE-bench 契约；安全夹具、独立效果 Oracle、外部安全套件及修复过程指标归后续里程碑。跨任务项目记忆、多 Agent 和多候选是可选目标。 | [M1 评测协议](EVAL_PROTOCOL.md) · [里程碑总表](StellarWeaver_Harness_Complete_Improvement_Plan_2026-11-21.md#71-里程碑总表) |
| 不能确定 | 完整 `dev`/`test` 的可执行性和正式成绩、DeepSeek API 别名对应的权重修订、外部安全套件的可比性、真实部署的隔离性质均需对应证据。已保存 `dev` 清单 23 条，本机配方 3 条。历史三题评分仅有 2 个已保存候选获 `resolved`、1 个未评分。 | [现有实验记录](EVAL.md#phase-2-多仓库试跑记录) · [历史内部评测](archive/00_Sample_Run.md) |

### 离线验收与证据

本机证据目录为 `.validation/harness-m01-2026-11-21-7ececb5-356df29d/`。以下测试均用固定夹具、FakeModel 或模拟传输和模拟评分子进程。受资源保护的 Docker 和 PostgreSQL 用例以跳过原因单列。

| 检查 | 结果 | 证据文件 |
| --- | --- | --- |
| 原有批量 CLI 帮助 | 退出码 0，核对六个既有命令 | `cli-help.txt` |
| M0 受限环境初测 | 102 通过、15 失败、1 跳过。失败含 Git MSYS 启动权限、长临时路径与受管进程状态问题。 | `baseline.txt` · `baseline.xml` |
| M0 同一批用例本机复核 | 117 通过、0 失败、1 跳过。跳过原因为 Windows 符号链接权限。 | `baseline-native.txt` · `baseline-native.xml` |
| M1 最终回归 | 122 通过、0 失败、6 跳过。1 项符号链接权限，2 项 G4 缺独立容器或数据库夹具，3 项 G5 缺独立 PostgreSQL 夹具。另有 1 条 Starlette TestClient 依赖弃用提示。 | `m1-frozen.txt` · `m1-frozen.xml` |
| 静态检查 | Ruff 全仓库 lint、4 个变更 Python 文件的格式检查、76 个源码文件的 mypy 及 `git diff --check` 均通过。 | `static.txt` |
| 容器及镜像盘点 | 迁移服务最近退出码为 0，API、数据库、执行器为 healthy，worker 为运行中。缓存列表可见三道试跑题的评分镜像。结果为只读盘点，Docker 验证边界以独立测试为准。 | `local-services.txt` · `local-images.txt` |
| 历史文件校验、离线重汇总 | 80 个已盘点历史文件的 SHA-256 保持一致。保存的三题两次导出逐字节相同，SHA-256 `228c9be72907a5d485d96cfa8e9cc56c7e529eee513dcc81317af2bbb3d58b25`。开发集清单 23 题均为未开始，比较历史导入结果时缺少轨迹身份，已说明排除原因。 | `historical-before.json` · `historical-unchanged.txt` · `historical-report-one.json` · `historical-report-two.json` · `dev23-prepared-contract.json` · `historical-comparison.json` |

本机离线回归通过 `python -B -m pytest -q -ra --tb=short -m 'not live_model' -p no:cacheprovider` 执行，下列测试路径为最终批次的选择范围。

```text
tests/integration/test_swebench_batch.py
tests/integration/test_swebench_pilot_adapter.py
tests/integration/test_eval_provenance.py
tests/integration/test_deepseek_thinking.py
tests/integration/test_m3_context.py
tests/integration/test_public_runtime.py
tests/integration/test_instruction_approval.py
tests/integration/test_web_g4.py
tests/integration/test_web_g5.py
tests/unit/test_context.py
tests/unit/test_repo_map.py
tests/unit/test_files.py
tests/unit/test_policy.py
tests/unit/test_registry_authorization_hooks.py
tests/unit/test_runtime_limits.py
```

### M1 闸门

| 闸门 | 核对结果 |
| --- | --- |
| G-EVAL-01 | 真实历史制品重汇总两次，生成相同 JSON 与哈希。 |
| G-EVAL-02 | 临时仓库保留中断尝试收据，模拟评分验证同候选命中缓存、独立重评分产生新 ID、不同补丁分别评分，并拒绝身份和报告哈希不匹配。 |
| G-EVAL-03 | 合成数据将隐藏字段留给 scorer。FakeModel 的 SQLite 留有公开题面，越出 checkout 的文件读取被工具拒绝。 |
| G-EVAL-04 | 离线夹具分别验证可用补丁、空补丁、预算耗尽、模型错误、评分镜像不可用和未开始样本，计划分母恒定。 |
| G-EVAL-05 | 夹具检验题号配对、比较条件、显式变更项和不可比理由。历史导入候选缺乏完整运行信息，可单独汇总。 |

### M2 的输入条件

M2 以预注册的合成夹具和正常任务对照、独立副作用判定以及隔离测试资源为输入。G-EVAL-06 的验证结果见下节。样本级分母和比较口径见[评测协议](EVAL_PROTOCOL.md)。

## M2-M4 固定夹具与授权

M2–M4 沿用 M1 的评测契约、SWE-bench 执行器和历史评分制品。审批使用现有 PostgreSQL 表。

### 阶段结论

| 阶段 | 交付与证据 |
| --- | --- |
| M2 | 固定 5 项 FakeModel 夹具。应用层边界检查 3/3，正常任务 2/2。外部判定采用临时文件哈希、内存搜索接收器和最终文件内容。验证记录了 10 次工具调用、14 次 FakeModel 调用及 1 次授权。 |
| M3 | 共享工具路径接入 L0–L3 能力评估、受保护路径守卫和安全事件。模型请求 `ds_search` 时需要一次查询授权。MCP 只读标记决定可见性，参数交接需要精确授权或一次确认。搜索失败时保留状态与进程清理元数据，未通过路径过滤的输出不进入工具结果。 |
| M4 | 受信代码可注册 `PreToolUse`、`PermissionRequest`、`PostToolUse`。执行前 Hook 按优先级和标识排序，只能加强限制。授权后复核目标和能力，并在记录执行意图后再次核对。浏览器批准绑定租户、运行和完整调用，原子消费并记录过期、消费和失效事件。 |

第一轮夹具基线与最终运行使用相同的夹具清单哈希。基线发现 1 条未获批准的普通查询到达模拟接收器，边界检查 2/3。最终未批准查询的接收计数为 0，批准同一查询的接收计数为 1，临时文件哨兵保持一致。以上为 M2–M4 的历史验收。当期公开查询逐次审批，现行策略将通过保护检查的公开查询设为 L0，并使用对应的新夹具。 [现行搜索规则](SECURITY.md#l0-l3-风险分级)

### 本机证据

日志目录为 `.validation/harness-m234-2026-11-21-7ececb5-5f9f15ae/`。

| 验证 | 证据 |
| --- | --- |
| G-EVAL-06 固定夹具 | `m2-baseline.json`、`fixtures-one.json`、`fixtures-two.json`。最终两份 JSON 的 SHA-256 均为 `b218b7f04ab8aa8010ed5034343fe1c9b6f4e27ddf6658a7e51230a841e941aa`。 |
| G-EVAL-07 历史读取 | 复核 M1 记录的 80 个文件，哈希全部匹配。历史 SWE-bench 三题可只读重汇总，输出哈希与 M1 一致。内部 20 题有归档说明和现行离线样例，说明引用的原始 JSON 在此工作副本缺失，原始 20 题报告读取仍待复核。见 `historical-unchanged.txt`、`historical-swebench.json` 和[原始验收说明](archive/00_Sample_Run.md)。 |
| 全量离线回归 | 首轮 515 通过、10 失败、25 跳过。失败涉及搜索终态证据、更新后的授权预期、日志脱敏和离线打包缓存。修复并复跑后为 526 通过、0 失败、28 跳过。跳过涉及独立验证资源或平台权限，G5 数据库用例见下行。另有 1 条 Starlette 依赖弃用提示。见 `tests.txt`、`final.txt`、`final.xml`。 |
| Web 审批 | 独立临时 PostgreSQL 验证 G5 共 7 项，覆盖同操作跨运行和跨租户拒绝、超时、指纹更改、一次消费以及对应事件持久化。使用本地已有镜像，运行结束后移除了临时容器。见 `postgres-final.txt`、`postgres-final-cleanup.txt`。 |
| 静态检查 | 全仓库 Ruff lint、变更文件格式、mypy、TypeScript 和 `git diff --check` 通过。见 `static-final.txt` 和 `typescript-final.txt`。 |

本地运行夹具及相关验证的命令如下。PostgreSQL 集成测试使用单独迁移好的测试库。

```powershell
uv run --locked --offline python -m stellarweaver.evals.local_fixtures
python -B -m pytest -q -ra --tb=short -m 'not live_model' -p no:cacheprovider
python -B -m pytest -q -ra --tb=short -p no:cacheprovider tests/integration/test_web_g5.py
```

固定场景、判定口径、工具威胁模型和外部套件可行性见[工具安全与评测](SECURITY.md)。定位、编辑和冲突恢复指标见[修复过程夹具](EVAL.md#修复过程的离线夹具)。M5 对通用执行器、镜像、断网、资源约束和清理的验证见下节。

## M5-M8 沙箱与修复工具

CLI、Web 与评测共用 ToolRegistry 和 Agent 循环。开发工具、导航与修复反馈可独立启用。

### M5 沙箱执行

- `sandbox-workspace` 以非 root 在断网容器内运行快照。容器的临时改动不写回任务目录。CLI 在显式启用 `allow_argv` 后也能运行已授权的常规 argv。
- `sandbox-read-only` 的可信初始化阶段封存文件和目录，清除附加组并切为 UID/GID 65534 后执行命令。隔离快照不可写，进程仍可使用限大小的临时目录。
- 两个 Profile 均使用只读根文件系统、默认无网络、能力收紧、非特权执行、独立 PID 和资源上限。源目录通过限大小的普通文件归档传输，过滤凭据路径和 Git 元数据。没有主机 Docker Socket 和数据库凭据进入任务容器。旧 SWE-bench 配方专用的可选依赖目录仍由可信评测操作者提供，受限为指定资产下的只读挂载。
- 使用本地镜像，容器按实际 image ID 启动。CLI 和新增 Web 开发工具需要固定镜像。结果记录镜像 ID、快照哈希、网络、资源配置和清理状态。Docker 模式不可用时返回错误。CLI 的 Docker Profile 同时禁止通过扩展启动宿主 MCP 进程。

### M6 受控开发工具

| 工具 | 参数与行为 |
| --- | --- |
| `run_tests` | 选择 Python 测试文件或目录，框架为 unittest 或 pytest。 |
| `discover_tests` | 静态枚举 Python 测试定义，保留路径、行号、解析错误和截断状态。 |
| `check_syntax` | 编译指定 Python 文件以检查语法，禁止生成字节码。 |
| `run_linter` | 调用固定环境中的 Ruff 检查，结果依赖该镜像安装 Ruff。 |

工作台继续要求审批。远端执行服务接受固定命令编号或受控操作、框架与相对路径。执行前复检目标，调用后保留真实退出码、截断信息和诊断。工作台的「验证」可查看局部检查。交付报告的整体验证仍以配置要求的正式命令为准。

### M7 和 M8

- **M7** 从任务文本中提取 Python 路径、异常名、栈帧、符号和测试线索，辅助现有 Repo Map 排序。`find_symbol`、`find_references`、`find_related_tests` 返回静态候选、文件哈希和行号。支持简单导入别名、重导出和嵌套定义。按文件内容哈希更新索引，只重新解析变化文件。动态绑定、反射及扫描限额之外的内容仍不确定。
- **M8** 依据工具错误、退出码和日志结构产生诊断。Journal 记录操作 ID、失败类别、候选编辑指纹和失败签名。反馈模式给现有循环提供简短的下一步建议。强制模式在同一候选和同一操作的失败重复三次时停止，夹在中间的普通读取不会重置计数。控制器不提交新的工具调用，沿用原有取消、拒绝、预算和未知副作用处理。

### 启用方式

CLI 通过 `stellarweaver.toml` 的 `[execution]` 配置 Docker 和镜像，`[harness]` 分别设置 `development_tools`、`issue_navigation` 和 `repair_controller`。工作台的 Compose 环境可设置 `SW_RUNNER_IMAGE`、`SW_WEB_SANDBOX_PROFILE`、`SW_WEB_DEVELOPMENT_TOOLS`、`SW_WEB_ISSUE_NAVIGATION` 和 `SW_WEB_REPAIR_CONTROLLER`。

SWE-bench 的 `--sandbox-commands` 切换到共享快照执行器。`--development-tools` 在使用同一执行器的前提下开放四个开发工具。导航与修复反馈另有 `--issue-navigation` 和 `--repair-controller`。比较固定工具与开发工具时，两组都使用共享执行器，固定其余开关。`run-settings.json` 和工具 schema 哈希记录选择。正式评测仍按[统一评测协议](EVAL_PROTOCOL.md)登记。

本机证据目录为 `.validation/harness-m58-2026-11-21-7ececb5-98fab6d1/`。Docker 为 Windows Docker Desktop / WSL2、Linux amd64，复用本地 Python 3.12 镜像，镜像 ID 见 `docker-image.txt`。

| 检查 | 结果与证据 |
| --- | --- |
| 完整离线回归 | 541 通过，0 失败，27 跳过。跳过涉及独立数据库或平台权限，见 `full.txt` 和 `full.xml`。 |
| 独立 PostgreSQL 与 Docker 联测 | 25 通过，0 失败，0 跳过。验证了 G4/G5 原有任务隔离、审批和报告，并检查了结构化开发操作的执行服务入口。独立数据容器已移除，见 `integrated.txt` 和 `postgres-cleanup.txt`。 |
| 最终变更定向回归 | 25 通过，0 失败，7 跳过。包含缓存镜像下的只读权限、cgroup 限制、断网、清理、测试失败后修复，以及一题纯合成 SWE-bench 输入从 prepared 记录到新工具结果的验证。已确认相关命名空间没有残余容器，见 `release.txt` 和 `container-cleanup.txt`。 |
| 两种测试框架和镜像依赖 | unittest 通过失败后修复夹具。pytest 使用已有的纯 Python 包在同一沙箱执行，实际 1 项通过。Ruff 执行入口在该镜像上返回缺少模块与退出码 1，含 Ruff 镜像上的通过路径仍待验证，见 `pytest-sandbox-final.txt`。 |
| 静态检查与历史 | Ruff、mypy、TypeScript、Compose 配置与 `git diff --check` 均通过。M1 盘点的 80 个历史文件哈希不变，既有 SWE-bench 三题可复算。见 `static-verified.txt`、`typescript.txt`、`compose.txt`、`historical-unchanged.txt`。 |

```powershell
$env:STELLARWEAVER_M5_TEST_IMAGE = docker image inspect --format '{{.Id}}' python:3.12-slim-bookworm
uv run --locked --offline pytest -q tests/integration/test_harness_m58.py
```

初轮验收覆盖工作台源码和 Compose 配置。随后在实际运行实例上补验了 M6 的项目验证镜像。

### M6 项目验证镜像补验

新增 `deploy/Dockerfile` 的 `verification` 构建目标，锁定安装项目运行依赖以及 pytest、pytest-asyncio 和 Ruff。测试从 Git 工作区快照导入项目源码。pytest 保持关闭插件自动加载，并在加入任务源码路径前显式加载镜像中的 `pytest_asyncio.plugin`。

现有工作台已切换到新镜像。通过当前 worker 和 executor 对项目 Git HEAD 的临时快照执行 `run_tests` 和 `run_linter`，目标为 `tests/integration/test_public_runtime.py`。三个测试全部通过，其中两个为异步测试。Ruff 返回退出码 0。两次都使用断网容器并确认清理。数据库容器保持原标识，定向回归 26 项通过，6 项因需要独立 PostgreSQL 跳过。

本机证据保存在 `.validation/harness-m6-verification-2026-11-21-88967432/`，其中 `deployed-verification.txt` 记录实际测试结果、镜像和清理状态。[镜像构建与配置](USAGE.md#会话策略和改动)

## M9-M10 补丁暂存与实验看板

实验看板从工作台的只读实验目录读取已发布文件。

| 功能 | 验收范围 |
| --- | --- |
| M9 补丁暂存 | 独立开关 `patch_staging`。从目标文件版本生成 UTF-8 Git diff，在两个临时目录准备和复核。支持文本修改、新建和删除。快照和基线在授权后复核，写入私有制品后可从执行过程下载。当前交付止于暂存和审核，项目工作区没有自动应用入口。 |
| M9 任务证据 | 独立开关 `task_evidence`。从已写入 Journal 的工具结果保留操作 ID、事件序号和有限的文件指纹。过期文件从提示投影排除，原始工具事件仍在 Journal。当前投影以单轮运行周期为界。 |
| M9 预算提示 | 独立开关 `budget_guidance`。根据本轮候选和验证状态推导阶段，在剩余调用或时间较低时提示优先验证。原有模型、工具、Token 和时间硬上限仍由运行时执行。 |
| M10 实验看板 | 支持旧单题轨迹、SWE-bench M1 报告和固定边界夹具。可按题号与评分状态筛选，逐题查看候选、独立评分、失败归因、耗时和 Token 来源。按任务配对时展示排除原因。安全夹具单独展示正常任务与未授权副作用，各报告均可下载脱敏 JSON。 |

工作台使用 `SW_WEB_PATCH_STAGING`、`SW_WEB_TASK_EVIDENCE`、`SW_WEB_BUDGET_GUIDANCE` 三个独立开关。CLI 对应 `[harness]` 同名字段。评测入口支持 `--task-evidence` 和 `--budget-guidance`，设置随本轮冻结。需要人工审批的补丁暂存供 CLI 和工作台使用。

### 已发布的本地记录

实验目录的 `report.json` 保留历史单题轨迹，`reports/*.json` 收录 M1 汇总和 M2 边界夹具。当前目录新增三题汇总、23 题未开始清单，以及边界夹具的基线与验收结果。看板只读取明确发布的 JSON，评分源码、SQLite 原始会话库与测试答案文件没有下载入口。源文件摘要绑定原始字节，界面和下载版对常见凭据字段及推理正文做脱敏。原始文件由维护者留存，公开分享前仍需内容审阅。

### 正式评测预注册与成本

以冻结的共享沙箱和固定测试入口作为基线。开发工具、导航、修复反馈、任务证据和预算提示各自单独对照。固定数据修订、题号、题序、模型版本、权限、评分镜像和总预算。只放行与实验设计一致的配置、工具 schema 或提示变化。补丁暂存的人工审核成本单独记录。

按当前每题上限估算每组每次的输入、输出 Token 封顶，23 题分别为 **736 万 / 36.8 万**，300 题分别为 **9,600 万 / 480 万**。预算金额以审批时存档的每百万 Token 价格按输入、输出分别计算，输入按未命中缓存估上限，最后乘以组数和重复次数。费用、Docker 资源与并发上限在运行前单独批准。计量和配对口径见 [评测协议](EVAL_PROTOCOL.md)，实验设定见 [评测计划](EVAL_PLAN.md)。

首批 23 题以确认流程、分母和独立评分为目标，预注册范围为单组、单次。若批准正式对照，优先采用一组基线和一组单变量改进，总输入、输出上限分别为 **1,472 万 / 73.6 万**（23 题）或 **1.92 亿 / 960 万**（300 题）。首批评测前需冻结具体模型、单价、组别、资源上限和停止条件。

### M9-M10 验证证据

| 范围 | 结果 | 证据文件 |
| --- | --- | --- |
| 离线 Python 全套 | 548 通过、27 跳过。23 项依赖额外 PostgreSQL 配置，4 项受 Windows 符号链接或 POSIX 权限条件限制。 | `full.txt`、`full.xml` |
| 最终改动定向回归 | 13 通过、3 跳过。缺少的 PostgreSQL 和隔离容器场景在后续专用环境补跑。 | `target-final.txt` |
| PostgreSQL 和 Docker 补跑 | 38 通过。首次注册用例在另一份空库单独运行并通过，覆盖账号隔离、工作区选择和普通对话。 | `postgres-final.txt`、`first-account.txt` |
| 静态与看板 | Ruff、mypy、TypeScript 和 Vite 构建通过。Edge 使用固定 JSON 夹具对已部署前端验证待评分筛选、旧候选评分排除、用量来源和安全表切换。 | `static-deploy.txt`、`browser-deployed.txt`、`build-dashboard.txt` |
| 当前工作台 | 原 Compose 的 API、worker 和 executor 已更新，三个选配开关在当前部署开启。原 PostgreSQL 容器继续运行。FakeModel 冒烟生成待审补丁，产生任务证据和预算事件。 | `deploy-dashboard.txt`、`production-http.txt`、`worker-smoke.txt` |
| 实验证据 | 5 份发布报告可读取。历史基线索引的 80 个文件哈希一致。三题历史中 2 题有独立通过记录，23 题清单尚无独立评分，两份清单当前可用评分配对数为 0。 | `production-catalog-final.txt`、`historical-hashes.txt`、`published-hashes.txt` |

验收证据位于 `.validation/harness-m910-2026-11-21-b1bba826/`。该阶段使用本地夹具、隔离容器和已保存的评分完成验收。

## 后续验证

- 补丁逐块应用、跨轮任务证据，以及真实模型任务的费用与效率趋势仍待验收。
- 本地夹具和已保存的历史评分构成本阶段的验证证据。23 道开发题、300 道测试题和外部安全集的正式评测待单独确认，M9 对任务修复率的影响仍待验证。
