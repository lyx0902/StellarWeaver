# 固定任务评测与独立评分

这套评测在新建的临时 Git 工作区执行 20 个小任务，分别保留 Agent 结果、真实工具证据、文件差异与独立评分。它用于核验 StellarWeaver 的工程闭环，不等同于 SWE-bench，也不能据此推断任意仓库的编码能力。实际运行结果单独记录在 `RESULTS.md`；本页介绍复现方法和统计口径。

## 任务与版本

| ID | 分类 | 数量 | 主要检查 |
| --- | --- | --- | --- |
| B01–B08 | `bug` | 8 | 分页、查找边界、单位换算、共享状态、路径、错误状态等行为修复 |
| F01–F04 | `feature` | 4 | 默认值、CLI 参数、CSV 导出、确定性排序 |
| X01–X04 | `cross-file` | 4 | 跨层配置、helper 更名、统一异常、记录字段扩展 |
| T01–T02 | `test` | 2 | 新测试通过正确实现，并捕获指定的错误变异 |
| Q01–Q02 | `understanding` | 2 | 配置优先级、异常传播路径及对应源码证据 |

[`tasks.json`](tasks.json) 是公开任务清单，固定输入文件位于 `fixtures/<task_id>/`。每项包括：

- `task_id`、`category`、`title`、`task_prompt`：身份、分类与实际任务。
- `fixture_repo`、`baseline_sha256`：夹具来源和基线文件字节校验。
- `allowed_paths`：可修改路径；理解题为空，跨文件题要求至少两个路径有实际变化。
- `policy_file`：运行时生成的精确路径和命令授权说明。
- `model_config`、`limits`：模型配置说明以及请求、工具与时长上限。
- `oracle_entrypoint`、`scoring_rules`：重评分入口及行为、改动范围、多文件要求等规则。

运行器读取随 Python 包分发的 `src/stellarweaver/evals/catalog.json`，其中另含私有 oracle、参考动作所需内容和事实锚点。这些内容不会复制进 Agent 的任务工作区。新报告记录 catalog 的 SHA-256、评分器版本、评分源码聚合 hash 和来源快照；修改任务、oracle 或夹具后应视为不同评测版本，并保留对应报告。公开夹具和打包清单的一致性由集成测试检查。

## 离线运行

先安装锁定依赖，再列出任务：

```powershell
uv sync --locked
uv run python -m stellarweaver.evals list
```

运行全部离线参考动作脚本：

```powershell
uv run python -m stellarweaver.evals run --provider fake
```

该命令会打印实际报告路径。指定 `--output PATH` 时，PATH 必须尚不存在，运行器拒绝覆盖既有目录。每次运行都会建立新的工作区；不会提交 Git，也不会修改公开夹具。默认输出位于系统临时目录。

FakeModel 使用预先编写的参考动作，实际调用读写、编辑和命令工具，并接受独立 oracle 检查。它验证执行基础设施和评分器，**不衡量模型自主定位或修复能力**。报告标记 `measurement_kind=offline_reference_script_infrastructure`、`model_capability_claim=false`。

无需模型的自动化检查：

```powershell
uv run pytest tests/integration/test_evals.py tests/integration/test_evals_review.py -q
```

## 显式真实模型运行

将密钥填入项目根目录 `.env` 的 `STELLARWEAVER_API_KEY`，然后显式选择真实 provider：

```powershell
uv run python -m stellarweaver.evals run --provider deepseek --env-file .env --task B01
```

`--task` 可重复；例如只运行 B01 与 Q01：

```powershell
uv run python -m stellarweaver.evals run --provider deepseek --env-file .env --task B01 --task Q01
```

省略 `--task` 才会运行完整 20 题。真实调用会消耗 API 用量；默认离线命令不会自动升级成真实模型调用。缺少 Key 会报错，不会产生伪造连通或成功结果。

默认配置使用 `deepseek-flash`。每题默认最多 12 次模型请求、40 次工具调用和 180 秒；输出上限为 2048 tokens。工作区配置来自启动目录的 `stellarweaver.toml`，具体生效配置和资源统计以会话、报告为准。编码题只获得指定文件路径及精确 unittest argv 的授权；理解题采用只读策略。没有任意 Python 前缀或追加命令放行。

## 独立 oracle 如何评分

功能与修复题在 Agent 工作区之外生成临时 oracle 脚本，导入候选代码并执行固定行为断言。进程退出码为 0 还不够：脚本必须在全部检查后输出本次随机生成的完成标记，避免候选代码在导入时 `SystemExit(0)` 绕过检查。评分前后均检查实际文件差异，oracle 导入代码引发的额外文件修改同样会被纳入范围校验。

测试补充题把候选复制到独立临时目录，先运行正确实现，再替换为固定错误变异。新增测试必须通过正确实现，并让变异检查失败；空测试或恒真断言不能通过这一要求。生产文件和原有可见测试仍受允许路径约束。

理解题使用明确的事实锚点、路径/符号和真实 `read_file` 结果中的 SHA-256 评分；Q01 另检查 CLI、environment、file、default 的优先级顺序。没有另一个 LLM 充当唯一裁判。这是有限的规则评分，不是对自然语言解释完整性和所有语义错误的通用证明。

不要求候选 patch 与参考 patch 字节一致。最终 `grade.passed` 需要独立检查通过且 `valid_diff=true`；编码题必须有实际改动，跨文件题至少修改两个允许路径，所有题都不能出现允许范围外的文件变化。模型最后声称“已修复”不能替代这些证据。

## 报告与重评分

`report.json` 保存请求的任务 ID、实际已记录 case、模型和平台信息、catalog 校验值、每题工作区绝对路径、基线 hash、工具结果、`RunOutcome` 和独立评分。每题 `outcome.state_path` 指向实际 SQLite 会话；备份和大输出制品在对应会话目录。工作区、会话和制品必须保留，才能继续检查或重评分。 GitHub 仅提供[公开验证摘要](../docs/validation/README.md)和[历史结果说明](RESULTS.md)；本地原始报告、日志与来源包不随源码上传。公开摘要不能直接传给 `score`，也不能替代相应运行工作区。

将实际报告路径替换到以下命令：

```powershell
uv run python -m stellarweaver.evals score PATH_TO_REPORT_JSON
uv run python -m stellarweaver.evals score PATH_TO_REPORT_JSON --task B01
uv run python -m stellarweaver.evals score PATH_TO_REPORT_JSON --output NEW_RECEIPT_JSON
```

重评分不调用模型，会重新执行候选代码上的独立检查，并读取当前工作区字节；它不是历史事件的无副作用回放。未知任务、报告中缺少请求的任务或空报告会报错。`score` 输出的 `passed` 是当前独立行为与差异检查通过数量；运行报告中的完整任务成功率还要求原 Agent 流程正常完成，两者应分开解读。CLI 的 `replay` 才是只渲染历史、不执行模型或工具的入口。

`--output` 排他创建新 JSON 收据与相邻的 `.provenance` 目录，拒绝覆盖已有文件或来源包。收据同时记录原报告 SHA-256、原报告 catalog hash、本次实际规则 hash、`stellarweaver-evals/1` 版本、评分源码 hash、包/Python/平台版本和 UTC 时间。来源目录保存按内容 hash 命名的 catalog 与评分源码；`stellarweaver.evals.provenance.verify` 只校验字节，不导入其中 Python。新 `run` 报告也带 `scoring-provenance` 来源包。内容 hash 能发现修改，不代表操作系统禁止文件变更，也不是数字签名。

可以通过 `score REPORT --catalog TRUSTED_CATALOG_JSON --output NEW_RECEIPT_JSON` 显式选择历史规则。catalog 含可执行 oracle，只使用自己信任的文件；命令不会导入归档的评分 Python。历史源码快照供核查，复现旧实现需在独立可信环境审阅后手动选择相应版本。现有历史报告不被新功能自动改写。

旧 M5 清单已按原报告 SHA-256 精确重建到 `evals/provenance/`，详见[规则档案](provenance/README.md)。旧版评分 Python 当时没有存档，不能声称已恢复当时实现；用当前评分器加旧规则的复现收据会明确记录本次实现。

报告按 case 增量保存。若任务中断或基础设施异常，应比较 `requested_task_ids` 与实际 `cases`，明确哪些题尚未完成；部分报告不能写成已跑完 20 题。`--task` 选出的子集也不能使用 20 作为已运行数量。

## 指标口径

| 字段 | 含义与分母 |
| --- | --- |
| `attempted` | 报告中实际记录的 case 数；包括失败和受阻 case |
| `success` / `task_success_rate` | 独立 oracle 通过、改动范围有效且 `run_status=completed` 的数量 / `attempted` |
| `independent_behavior_success` | 独立行为与差异检查通过数量；即使 Agent 后续受阻，也不会冒充完整流程成功 |
| `failed` / `blocked` | `failed=attempted-success`；`blocked` 是其中受阻运行的单独统计，两项不能相加当作互斥类别 |
| `effective_diff` | 编码题有效改动数 / 实际运行编码题数；完整清单为 18，理解题不进入此分母 |
| `verification_execution` | 实际返回精确规定 argv 的执行结果、且有 exit_code 字段的编码题数 / 实际运行编码题数；拒绝、受阻、取消不计为已执行验证 |
| `tool_error_rate` | `status=error` 的工具结果数 / 排除拒绝、受阻、取消后的工具结果数；这是工具回合口径，包含参数校验错误，不是底层外部进程故障概率 |
| `permission_denied` / `cancelled` | 工具拒绝/受阻与取消单列，不用于稀释工具错误率 |
| `resources` | 每个实际 case 的请求数、工具数、输入/输出 tokens、Agent 时长，报告均值和中位数 |

当前运行器不静默跳过任务，`skipped` 为 0。token 估算和 usage 不完整标记保留在 outcome 中；不推测货币成本。恢复故障注入通过 pytest 单独验收，`recovery_rate` 保留为空，不混入 20 题开发任务成功率。

## 能力与隔离边界

候选测试、oracle 和命令在当前用户的 Host 权限下运行。把 oracle 放在任务目录之外、使用随机完成标记及限制工具授权，并不提供对恶意候选代码的文件系统或进程隔离；受控子进程环境也不能阻止代码直接访问同一 Host 上其他可读文件。需要更强的 oracle 保密与副作用隔离时，应使用独立容器或受限挂载环境。

真实模型的单次运行有波动，结果应同时保留配置、时间、任务数量、失败/受阻数量与独立重评分证据。当前没有完成 bash-only 与结构化工具比较，也没有完成无 repo map / 无压缩与启用后的消融。现有报告只能说明相应版本与配置下实际运行的结果，不能解释为这些组件带来确定提升的比较结论。
