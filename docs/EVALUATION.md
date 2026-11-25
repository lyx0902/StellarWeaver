# SWE-bench Lite 评测体系与实验记录

本文档说明 StellarWeaver 的回归复现入口、SWE-bench Lite 统一评测协议、批量评测与看板发布命令、已有 SWE-bench Lite 实测记录，以及 23 题 `dev` / 300 题 `test` 实验规划。历史 20 题内部评测已归档至 [00_Sample_Run.md](archive/00_Sample_Run.md)，M0–M10 与 N 阶段功能验收见 [HARNESS_ACCEPTANCE.md](HARNESS_ACCEPTANCE.md)。

## 目录

- [复现入口](#复现入口)
- [统一评测协议](#统一评测协议)
- [SWE-bench Lite 批量评测与单题试跑](#swe-bench-lite-批量评测与单题试跑)
- [SWE-bench Lite 实验设计与对照规划](#swe-bench-lite-实验设计与对照规划)

---

## 复现入口

在仓库根目录执行（离线依赖同步后即可无网复现）：

```powershell
uv sync --locked --extra web
uv run --locked --offline stellarweaver doctor --offline
uv run --locked --offline python -m stellarweaver.demo
uv run --locked --extra web pytest -q -ra
uv run --locked --extra web ruff check .
uv run --locked --extra web mypy src/stellarweaver
```

前端类型与构建检查：

```powershell
Push-Location frontend; npm exec -- tsc --noEmit; Pop-Location
```

> 本地工具边界与安全夹具复现命令见 [SECURITY.md](SECURITY.md#本地工具边界评测)。

---

## 统一评测协议

统一契约（[swebench_contract.py](../src/stellarweaver/evals/swebench_contract.py)）约束所有 SWE-bench Lite 离线汇总、配对对照与工作台实验看板发布。

### 离线报告、看板发布与配对命令

```powershell
# 1. 将实验目录汇总为标准契约报告，并发布到工作台「实验记录」目录（无需重启容器）
uv run --locked --offline python -m stellarweaver.evals.swebench_contract report `
  .validation/<实验目录> `
  --output .validation/workbench-experiment/reports/<报告名>.json

# 2. 两份报告按题号做严格配对对照（须显式声明允许差异的控制项）
uv run --locked --offline python -m stellarweaver.evals.swebench_contract compare `
  .validation/workbench-experiment/reports/<基线报告>.json `
  .validation/workbench-experiment/reports/<实验报告>.json `
  --vary config_sha256 --vary prompt_sha256 --vary tools_sha256
```

### 数据契约与身份绑定

- 任何已发布报告由四层对象构成：**Manifest**（数据集修订/SHA-256、题目顺序、模型、预算、窗口、代码指纹）、**Attempt**（候选补丁、状态、用量与耗时）、**Score**（独立官方评分器在断网容器中的裁决）与 **Evidence**（摘要哈希、日志与验证制品）。
- 评分记录强绑定 `instance_id`、`attempt_id`、`patch_sha256`、`scorer_sha256`、`image`、`grader_version` 与 `raw_report_sha256`，任一哈希不匹配即拒绝复用或汇总。
- 公开输入（`task.json`）与隐藏评分字段（`scorer.json`）在 `prepare` 阶段物理隔离；Agent 仅读取公开题面与基线仓库。

### 分母、计量与配对规则

- **计划题数即分母**：`resolved_rate = resolved / planned`；未运行、超时、超预算、空补丁、环境错误与评分失败全部保留在分母中，禁止剔除后重算。
- **配对对照**：仅比较双方在相同数据集修订、模型、窗口、预算与评分条件下**同时具有独立判定**的题号，其余题号记录排除原因；允许变化的自变量必须通过 `--vary` 显式声明。
- **成本与缓存计量**：总输入 token 包含缓存命中与未命中；报告单独记录 `cached_input_tokens` 以便核算真实 Prefix Cache 命中率与谷期成本。

---

## SWE-bench Lite 批量评测与单题试跑

### 批量评测入口（`stellarweaver.evals.swebench`）

批量入口固定 SWE-bench Lite 上游修订 `b0dde1093fe417d83b7184254edf8199c1f0dff5`，校验 `dev`（23 题）和 `test`（300 题）的 Parquet SHA-256 与题号。

- **冻结窗口与预算**：`prepare` 默认冻结 `200,000` token 窗口（兼容旧清单回退 `100,000`），默认不限模型调用次数，每题默认 `max_tool_calls=50`、`max_wall_seconds=360`、`max_input_tokens_total=320,000`、`max_output_tokens_total=16,000`，均可在 `prepare` 阶段自定义冻结（DeepSeek Thinking 推荐 `--max-tool-calls 80 --max-wall-seconds 600 --max-input-tokens-total 3000000 --max-output-tokens-total 64000`，对照两组须保持一致）。
- **执行配方（`--recipes`）**：按题号或 `owner/repo` 指定 `image`（本地镜像 `sha256:...`）、`pytest_args`、可选 `dependencies`（`--assets` 下的只读依赖目录，容器内通过 `site.addsitedir("/deps")` 加载含 `.pth` 在内的依赖）和 `grader_image`（官方评分镜像 `repo@sha256:...`）。
- **一键预设与单因素消融**：`run` 支持 `--harness-profile baseline|full`（默认 `baseline` 全关；`full` 开启全部已交付 Harness 能力与 14 项流水线开关，含 `run_tests` 细粒度过滤、`run_python_snippet`、`git_log`/`git_show`、`apply_patch`、只读并发、上下文三项优化与子 Agent 探索），并支持在预设上用 `--no-<开关>` 或显式开关覆盖。展开后的完整配置与 `config_sha256` 冻结写入 `run-settings.json` 防漂移。
- **Windows 兼容与官方镜像复用**：基线检出统一传 `-c core.longpaths=true` 避免深层测试路径报错；补丁校验临时目录使用短路径系统 temp 并开启容错清理避免 `MAX_PATH` 异常；评分器以 `python -I -X utf8` 启动修复中文 Windows 下非 ASCII 测试补丁编码问题，并支持 `evaluate --instance-id` 定向重评。
- **按仓库分批评测助手（[swebench_eval_helper.py](../scripts/swebench_eval_helper.py)）**：支持直接复用 Docker Hub 官方预构建 SWE-bench 镜像，评测数据与产物统一存放在已 `.gitignore` 忽略的 `SWEBenchLite_Eval/` 目录中：
  - `prepare-recipes --repo <owner/repo>`：按仓库拉取官方镜像（带 180s 超时 4 次重试）并生成 `recipes.json`；
  - `prefetch-assets`：带 5 次重试与 `HEAD` 完整性校验预拉取基线提交；
  - `prune-images --report <合同报告>`：带防误删守卫（仅在 `not_evaluated=0` 且 `evaluation_error=0` 时清理本批镜像，避免本地磁盘膨胀）。

```powershell
$swRun = 'SWEBenchLite_Eval/runs/dev-full-marshmallow'
$swData = 'SWEBenchLite_Eval/datasets/dev.parquet'
$swSha = (Get-FileHash -LiteralPath $swData).Hash.ToLowerInvariant()
$swGrader = '.validation/swebench-lite-pilot/grader-venv/Scripts/python.exe'
$swAssets = 'SWEBenchLite_Eval/assets'
$swRecipes = 'SWEBenchLite_Eval/recipes/dev-marshmallow.json'

# 1. 按仓库拉取官方镜像并生成配方
uv run --locked python scripts/swebench_eval_helper.py prepare-recipes `
  --dataset $swData --grader-python $swGrader --repo marshmallow-code/marshmallow --output $swRecipes

# 2. 冻结清单与预算，并预拉取基线提交
uv run --locked python -m stellarweaver.evals.swebench prepare `
  --dataset $swData --dataset-sha256 $swSha --split dev `
  --grader-python $swGrader --recipes $swRecipes --output $swRun `
  --context-window-tokens 200000 --max-tool-calls 80 --max-wall-seconds 600 `
  --max-input-tokens-total 3000000 --max-output-tokens-total 64000
uv run --locked python scripts/swebench_eval_helper.py prefetch-assets `
  --experiment-dir $swRun --assets $swAssets

# 3. 运行（baseline / full / 单因素覆盖，支持 --fake-scripts 与 --retry-interrupted）
uv run --locked python -m stellarweaver.evals.swebench run `
  --output $swRun --assets $swAssets --env-file .env --harness-profile full --live

# 4. 导出、官方独立评分（支持 --instance-id 定向重评）、汇总、发布看板并清理本批镜像
uv run --locked python -m stellarweaver.evals.swebench export --output $swRun
uv run --locked python -m stellarweaver.evals.swebench evaluate --output $swRun --grader-python $swGrader
uv run --locked python -m stellarweaver.evals.swebench report --output $swRun
uv run --locked python -m stellarweaver.evals.swebench_contract report $swRun `
  --output .validation/workbench-experiment/reports/dev-full-marshmallow.json
uv run --locked python scripts/swebench_eval_helper.py prune-images `
  --recipes $swRecipes --report .validation/workbench-experiment/reports/dev-full-marshmallow.json
```

### SWE-bench Lite 实测与官方评分记录

#### 1. 2026-11-24 ~ 25 全量 `dev` 23 题 Full Harness 实测（`--harness-profile full` · DeepSeek Flash Thinking High · 200k 窗口）

> 完整 23 题逐题明细、官方镜像报错堆栈、6 道有效失败题轨迹与 Gold Patch 对比归因，见专题报告 [SWEBenchLiteEval.md](SWEBenchLiteEval.md)。

按 6 个仓库分批执行（预算冻结：`--context-window-tokens 200000 --max-tool-calls 80 --max-wall-seconds 600 --max-input-tokens-total 3000000 --max-output-tokens-total 64000`），全部 6 批合同报告已发布至工作台看板：

| 批次 | 仓库 | 计划题数 | 官方原始成绩 | 上游镜像异常 | 有效镜像成绩 | 工作台报告 |
| --- | --- | :---: | :---: | :---: | :---: | --- |
| Batch 1 | `marshmallow-code/marshmallow` | 2 | **2 / 2 (100%)** | 0 | **2 / 2 (100%)** | [dev-full-marshmallow.json](../.validation/workbench-experiment/reports/dev-full-marshmallow.json) |
| Batch 2 | `pydicom/pydicom` | 5 | **2 / 5 (40.0%)** | 2（pytest 8.3.5 破坏 nose `setup`） | **2 / 3 (66.7%)** | [dev-full-pydicom.json](../.validation/workbench-experiment/reports/dev-full-pydicom.json) |
| Batch 3 | `pylint-dev/astroid` | 5 | **3 / 5 (60.0%)** | 0 | **3 / 5 (60.0%)** | [dev-full-astroid.json](../.validation/workbench-experiment/reports/dev-full-astroid.json) |
| Batch 4 | `pvlib/pvlib-python` | 5 | **0 / 5 (0%)** | 5（NumPy 2.0 移除 `np.Inf` + 离线失败） | —（0 有效） | [dev-full-pvlib.json](../.validation/workbench-experiment/reports/dev-full-pvlib.json) |
| Batch 5 | `sqlfluff/sqlfluff` | 5 | **2 / 5 (40.0%)** | 0 | **2 / 5 (40.0%)** | [dev-full-sqlfluff.json](../.validation/workbench-experiment/reports/dev-full-sqlfluff.json) |
| Batch 6 | `pyvista/pyvista` | 1 | **0 / 1 (0%)** | 1（缺 `libGL.so.1` + 离线失败） | —（0 有效） | [dev-full-pyvista.json](../.validation/workbench-experiment/reports/dev-full-pyvista.json) |
| **合计** | **全量 `dev`** | **23** | **9 / 23 (39.1%)** | **8（34.8%）** | **9 / 15 (60.0%)** | 有效口径 Wilson 95% CI `[35.7%, 80.2%]` |

- **端到端稳定性**：**23/23 题全部 `model finished` 并产出合法 `valid_patch`**（0 空补丁、0 非法补丁、0 超时、0 Harness 误杀硬中断）；`pydicom-1413` 核心 `FAIL_TO_PASS` 实际已 **3/3 全过**，仅挂在官方镜像 `pytest 8.3.5` 的 2 个 nose `setup` 回归测试。
- **精确用量与成本**（由 23 份 `session.sqlite3` 精确汇总，DeepSeek-Flash 闲时定价：命中 ¥0.02/1M、未命中 ¥1.00/1M、输出 ¥4.00/1M）：
  - 总输入 `19,913,835`（~19.91M）、缓存命中 `17,258,493`（**86.67%** 命中率）、总输出 `386,007`（~0.39M）；
  - 闲时实测总费用 **¥4.54**（均摊 **¥0.20 / 题**）；单题中位耗时 `148.7s`（均值 `194.5s`，总计 `74.6` 分钟）、中位轮次 `18` 轮。
- **沉淀的 P0 工程修复项**：① `edit_file` 兼容 Windows CRLF 行尾（消除 `astroid-1196` 连续 8 次 `EDIT_NOT_FOUND` 绕路）；② 修复 `swebench.py` 将 `model.failed` 混入缓存统计导致重试题丢失 `cached_input_tokens` 的问题。

#### 2. 2026-11-24 三题冒烟实测（`--harness-profile full` · DeepSeek Flash Thinking High · 200k 窗口）

已发布至工作台看板（[full-3tasks-20261124-v2.json](../.validation/workbench-experiment/reports/full-3tasks-20261124-v2.json)、[full-astroid-20261124.json](../.validation/workbench-experiment/reports/full-astroid-20261124.json)），独立官方评分器 `swebench 5.0.2` 裁决结果如下：

| 题号 | 运行终态 | 候选状态 | 官方评分 | 模型 / 工具调用 | 总输入（缓存命中率） | 总输出 | 耗时 | 谷期实测成本 |
| --- | --- | --- | :---: | :---: | :---: | ---: | ---: | :---: |
| `marshmallow-code__marshmallow-1359` | `completed` | `valid_patch` | **`resolved`** | 26 / 37 | 1,168,939（**89.5%**） | 19,329 | 170s | ¥0.22 |
| `pydicom__pydicom-1694` | `completed` | `valid_patch` | **`resolved`** | 14 / 21 | 452,881（**82.6%**） | 6,920 | 148s | ¥0.11 |
| `pylint-dev__astroid-1333` | `completed` | `valid_patch` | `unresolved` | 32 / 44 | 2,440,849（**89.5%**） | 35,045 | 301s | ¥0.44 |

- **实测观察**：3 题全部干净 `completed` 并产出可应用的 `valid_patch`（0 环境故障、0 评分异常）；DeepSeek 实测 Prefix Cache 命中率达 **82.6%–89.5%**（均值 **88.7%**）；`astroid-1333` 所有 45 项 `PASS_TO_PASS` 通过，未过项为模型改了 `astroid/manager.py` 而非金标准 `astroid/modutils.py`。

#### 3. 2026-11-20 历史 Phase 2 基线试跑（旧 100k 窗口 · 无 Harness 增强）

- `marshmallow-code__marshmallow-1359`：`valid_patch` → **`resolved`**
- `pydicom__pydicom-1694`：`valid_patch` → **`resolved`**
- `pylint-dev__astroid-1333`：触及旧预算上限，未生成补丁（`not_evaluated`）

> 单题复现闸门试跑脚本（`stellarweaver.evals.swebench_pilot`）保留用于 `marshmallow-1359` 的历史复现对照，后续正式评测统一以 `stellarweaver.evals.swebench` 为准。

---

## SWE-bench Lite 实验设计与对照规划

### 架构与数据边界

- **执行架构**：Agent 运行器、可见测试容器与官方独立评分器物理隔离；评分阶段仅读取 `predictions.jsonl` 与冻结 `scorer.json`。
- **工作台看板**：Web「实验记录」以只读方式加载 `.validation/workbench-experiment/reports/*.json`，展示汇总指标、单题终态、用量与两份报告的严格配对差异。

### 阶段目标与基准迁移规划

| 阶段 | 数据集与范围 | 状态与目标 |
| --- | --- | --- |
| **SWE-bench `dev` 全量闭环（已完成）** | `dev`（23 题，`--harness-profile full`） | 已完成 6 仓库分批实跑与官方评分：**9/23 原始（39.1%）、9/15 有效镜像（60.0%）**，实测均摊 **¥0.20/题**。详见 [SWEBenchLiteEval.md](SWEBenchLiteEval.md)。 |
| **Phase 1–3a 改造与 TB 4.1 5 题冒烟（已完成）** | Harbor `0.25.0` · `terminal-bench/terminal-bench@4.1.0`（5 题显式 CPU，[harbor-tb41-smoke5.yaml](../deploy/harbor-tb41-smoke5.yaml)） | 已完成 Phase 1（CRLF / `run_linter` / 软提醒 / 缓存统计）、Phase 2（512k/64k 长程预设 / `length` 续写 / 双级失败引导 / 复读检测）与 Phase 3a（`terminal-minimal` 单 `bash` + Harbor 原生适配 + 真实评分合并与短路径归档）。5 题 CPU 冒烟实测 **2/5（40.0%）**、Prefix Cache **96.38%**、谷期 ¥13.37（同 5 题 Harbor Hub 一手对照：Claude Code **28.0% [7/25]**、Ante 0.3.5 **40.0% [2/5]** 且我方题均耗时更优 `34.4m` vs `53.1m`）；基于 610 次 `bash` 零编辑摩擦决定跳过 Phase 3b，并修复零字节断连虚扣输出预算 Bug。详见 [TerminalBench4Eval.md](TerminalBench4Eval.md)。 |
| **Terminal-Bench 4.1 63 题全量（下一步）** | `terminal-bench/terminal-bench@4.1.0`（63 题无 GPU 版，谷期窗口） | 预计谷期总费 ¥140–¥170（`n_concurrent_trials=2`），对标 Mini-SWE-agent（31.8%）、Claude Code（38.5%）与 Ante（40.9%）；如需进一步压降单题标准价，可重点评估长程 `bash` 输出截断长度与上下文压缩阈值。 |
| **SWE-bench Lite 正式集与消融（按需）** | `test`（300 题） | ① `baseline`（全关）vs `full`（全开）正式基线；② 单因素消融（`--no-development-tools`、`--no-issue-navigation`、`--no-subagent-exploration`、`--no-sticky-context-checkpoint`、`--repair-controller off|feedback|enforce`）。 |
