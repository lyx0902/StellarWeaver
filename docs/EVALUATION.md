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
- **Windows 长路径兼容**：基线检出统一传 `-c core.longpaths=true`，避免深层测试路径触发 `Filename too long`。

```powershell
$swRun = '.validation/swebench-dev-full'
$swData = '.validation/swebench-lite-dataset/dev.parquet'
$swSha = (Get-FileHash -LiteralPath $swData).Hash.ToLowerInvariant()
$swGrader = '.validation/swebench-lite-pilot/grader-venv/Scripts/python.exe'
$swAssets = '.validation/swebench-phase2-assets'
$swRecipes = "$swAssets/recipes.json"

# 1. 冻结清单与预算
uv run --locked python -m stellarweaver.evals.swebench prepare `
  --dataset $swData --dataset-sha256 $swSha --split dev `
  --grader-python $swGrader --recipes $swRecipes --output $swRun `
  --context-window-tokens 200000 --max-tool-calls 80 --max-wall-seconds 600 `
  --max-input-tokens-total 3000000 --max-output-tokens-total 64000 `
  --instance-id marshmallow-code__marshmallow-1359 `
  --instance-id pylint-dev__astroid-1333 --instance-id pydicom__pydicom-1694

# 2. 运行（baseline / full / 单因素覆盖，支持 --fake-scripts 与 --retry-interrupted）
uv run --locked python -m stellarweaver.evals.swebench run `
  --output $swRun --assets $swAssets --env-file .env --harness-profile full --live

# 3. 导出、官方独立评分（swebench==5.0.2 断网容器）与汇总
uv run --locked python -m stellarweaver.evals.swebench export --output $swRun
uv run --locked python -m stellarweaver.evals.swebench evaluate --output $swRun --grader-python $swGrader
uv run --locked python -m stellarweaver.evals.swebench report --output $swRun
```

### 三题实测与官方评分记录

#### 1. 2026-11-24 Full Harness 实测（`--harness-profile full` · DeepSeek Flash Thinking High · 200k 窗口）

已发布至工作台看板（[full-3tasks-20261124-v2.json](../.validation/workbench-experiment/reports/full-3tasks-20261124-v2.json)、[full-astroid-20261124.json](../.validation/workbench-experiment/reports/full-astroid-20261124.json)），独立官方评分器 `swebench 5.0.2` 裁决结果如下：

| 题号 | 运行终态 | 候选状态 | 官方评分 | 模型 / 工具调用 | 总输入（缓存命中率） | 总输出 | 耗时 | 谷期实测成本 |
| --- | --- | --- | :---: | :---: | :---: | ---: | ---: | :---: |
| `marshmallow-code__marshmallow-1359` | `completed` | `valid_patch` | **`resolved`** | 26 / 37 | 1,168,939（**89.5%**） | 19,329 | 170s | ¥0.22 |
| `pydicom__pydicom-1694` | `completed` | `valid_patch` | **`resolved`** | 14 / 21 | 452,881（**82.6%**） | 6,920 | 148s | ¥0.11 |
| `pylint-dev__astroid-1333` | `completed` | `valid_patch` | `unresolved` | 32 / 44 | 2,440,849（**89.5%**） | 35,045 | 301s | ¥0.44 |

- **实测观察**：3 题全部干净 `completed` 并产出可应用的 `valid_patch`（0 环境故障、0 评分异常）；DeepSeek 实测 Prefix Cache 命中率达 **82.6%–89.5%**（均值 **88.7%**）；`astroid-1333` 所有 45 项 `PASS_TO_PASS` 通过，未过项为模型改了 `astroid/manager.py` 而非金标准 `astroid/modutils.py`。
- **成本参考（`deepseek-flash` 官方谷期定价：缓存命中 ¥0.02/1M、未命中 ¥1.00/1M、输出 ¥4.00/1M）**：三题均值约 **¥0.26 / 题**，推算 23 题 `dev` 单组约 **¥5–¥8**，300 题 `test` 单组约 **¥60–¥105**。

#### 2. 2026-11-20 历史 Phase 2 基线试跑（旧 100k 窗口 · 无 Harness 增强）

- `marshmallow-code__marshmallow-1359`：`valid_patch` → **`resolved`**
- `pydicom__pydicom-1694`：`valid_patch` → **`resolved`**
- `pylint-dev__astroid-1333`：触及旧预算上限，未生成补丁（`not_evaluated`）

> 单题复现闸门试跑脚本（`stellarweaver.evals.swebench_pilot`）保留用于 `marshmallow-1359` 的历史复现对照，后续正式评测统一以 `stellarweaver.evals.swebench` 为准。

---

## SWE-bench Lite 实验设计与对照规划

### 架构与数据边界

- **执行架构**：Agent 运行器、可见测试容器与官方独立评分器物理隔离；评分阶段仅读取 `predictions.jsonl` 与冻结 `scorer.json`。
- **工作台看板**：Web「实验记录」以只读方式加载 `.validation/workbench-experiment/reports/*.json`，展示汇总指标、单题终态、用量与两份报告的严格配对差异。

### 阶段目标与消融分组

| 阶段 | 数据集与范围 | 目标与分组设计 |
| --- | --- | --- |
| **开发集配对对照（下一步）** | `dev`（23 题，已冻结清单） | 同预算同窗口下跑 **`baseline` vs `full`** 配对对照，验证整套 Harness 相对基线的解决率、轮次与缓存命中率提升。 |
| **正式集基线与消融** | `test`（300 题） | ① `baseline`（全关）vs `full`（全开）正式基线；② 单因素消融（`--no-development-tools`、`--no-issue-navigation`、`--no-subagent-exploration`、`--no-sticky-context-checkpoint`、`--repair-controller off|feedback|enforce`）。 |
