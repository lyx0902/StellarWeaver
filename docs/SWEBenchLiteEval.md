# StellarWeaver SWE-bench Lite `dev`（23 题）全量评测报告与归因分析

> **实验日期**：2026-11-24 ~ 2026-11-25  
> **评测产物目录**：`SWEBenchLite_Eval/runs/dev-full-{marshmallow,pydicom,astroid,pvlib,sqlfluff,pyvista}/`  
> **Web 工作台合同报告**：`.validation/workbench-experiment/reports/dev-full-{marshmallow,pydicom,astroid,pvlib,sqlfluff,pyvista}.json`  
> **数据口径说明**：本文所有指标、实例 ID、报错堆栈与轨迹细节均严格提取自本次 6 批次实跑产物（`manifest.json`、`run-settings.json`、`agent-report.json`、`session.sqlite3`、`candidate.patch`、`scorer.json`、`test_output.txt`）及工作区源码。

---

## 1. 实验配置与总览（Executive Summary）

### 1.1 基础配置与 Harness 开关

| 配置项 | 实际值 | 来源 |
|---|---|---|
| 数据集与切分 | `SWE-bench/SWE-bench_Lite`（`dev`，23 题，revision `b0dde1093fe417d83b7184254edf8199c1f0dff5`） | `manifest.json` |
| 官方评分器版本 | `swebench 5.0.2`（复现容器使用 Docker Hub 官方预构建镜像，评分器以 `python -I -X utf8` 启动） | `manifest.json` / `swebench.py` |
| Git Commit Hash | `2648242e4881fff89f5d18b3db5bb4a817c672bb`（含未提交工作区改动：官方镜像复用、Windows `MAX_PATH` 临时目录修复、`-X utf8` 评分器修复） | 合同报告 `manifest.harness_commit` |
| 模型与思考模式 | `deepseek-flash`（DeepSeek-V4.1-Flash-1017），`thinking: enabled`，`reasoning_effort: high`，`max_output_tokens: 16384`，`supports_streaming: true` | `run-settings.json` |
| 上下文窗口与压缩阈值 | `context_window_tokens: 200000`，`trigger_ratio: 0.8`（160k 触发压缩），`target_ratio: 0.55`（压缩至 110k），`reserve_tokens: 2048`，`recent_complete_rounds: 6`，`fresh_tool_rounds: 2` | `run-settings.json` |
| 步数 / 时间 / 预算上限 | `max_model_calls: null`（不限），`max_tool_calls: 80`，`max_wall_seconds: 600`，`max_input_tokens_total: 3,000,000`，`max_output_tokens_total: 64,000`，`request_timeout_seconds: 90` | `manifest.json` / `run-settings.json` |
| Harness Profile | `full`（`policy.preset: benchmark`，`hints: false`，`web_search: false`，`completion_gate: false`） | `run-settings.json` |
| 循环保护开关 | `_stop_on_denied_tool = True`，`_stop_on_repeated_tool_failure = True` | `src/stellarweaver/runtime/loop.py#L118-L119` |
| 关键特性开关 | `parallel_read_tools: true`、`sticky_context_checkpoint: true`、`calibrated_context_budget: true`、`protect_fresh_tool_output: true`、`adaptive_round_retention: true`、`subagent_exploration: true`（`max_model_calls=20, max_tool_calls=60, timeout=300s`）、`repair_controller: "feedback"`、`post_edit_syntax_hook: true`、`development_tools: true`、`scoped_test_grants: true`、`incremental_sandbox_snapshots: true` | `run-settings.json` |

### 1.2 核心指标汇总表

| 指标 | 数值 | 备注 |
|---|---|---|
| 总题数 | **23** | 全部 23/23 题正常结束（`run_status=completed`，`reason="model finished"`），**23/23 产出合法 `candidate.patch`**（0 空补丁、0 非法补丁、0 超时、0 硬中断） |
| 官方镜像 / 环境异常题数 | **8** | `pydicom` 2 题（pytest 8 破坏 nose `setup`）+ `pvlib` 5 题（NumPy 2.0 崩溃）+ `pyvista` 1 题（缺 `libGL.so.1`） |
| 有效可评测题数 | **15** | `marshmallow` 2 + `pydicom` 3 + `astroid` 5 + `sqlfluff` 5 |
| **原始官方通过率** | **9 / 23（39.1%）** | `resolved: 9`、`unresolved: 10`、`evaluation_error: 4` |
| **剔除环境故障后的有效通过率** | **9 / 15（60.0%）** | Wilson 95% 置信区间约 **[35.7%, 80.2%]**；小样本（n=15）波动较大，仅反映 Harness 端到端闭环健康度，不宜直接外推至 `test` 300 题 |
| 总输入 Token（Input） | **19,913,835**（~19.91M） | 23 题合计（均摊 865,819 / 题） |
| 总缓存命中 Token（Cache Hit） | **17,258,493**（~17.26M） | 精确值由 23 题 `session.sqlite3` 中所有 `model.completed` 事件汇总得出（注：合同报告中 3 题因含 1 次 `model.failed` 重试被记为 `incomplete`，详见第 6 节） |
| 总输出 Token（Output） | **386,007**（~0.39M） | 23 题合计（均摊 16,783 / 题） |
| 平均 Prefix Cache 命中率 | **86.67%** | 合同报告完整上报的 20/23 题均值为 **87.43%**；全量 23 题从 `session.sqlite3` 精确汇总为 **86.67%** |
| 总费用与单题平均费用（闲时） | **¥4.54**（均摊 **¥0.20 / 题**） | 按 DeepSeek-Flash 闲时单价精确核算：Cache Hit `¥0.02/M`（¥0.345）+ Cache Miss `2,655,342` × `¥1.00/M`（¥2.655）+ Output `¥4.00/M`（¥1.544） |
| 单题耗时（平均 / 中位 / 最大） | **194.5s / 148.7s / 477.6s** | 总 Agent 求解耗时 **74.6 分钟**（最大耗时 `sqlfluff-1733`，占 600s 上限的 79.6%） |
| 模型轮次（平均 / 中位 / 最大） | **18.8 / 18.0 / 29 轮** | 最大轮次 `astroid-1333`（29 轮） |
| 工具调用次数（平均 / 中位 / 最大） | **25.6 / 25.0 / 40 次** | 最大调用 `astroid-1333`（40 次，占 80 次上限的 50%） |

---

## 2. 全量 23 题逐题结果明细表

> 注：费用按 DeepSeek-Flash 闲时单价（命中 ¥0.02/M、未命中 ¥1.00/M、输出 ¥4.00/M）由 `session.sqlite3` 精确计算；标 `*` 的 3 题在合同报告 JSON 中因含 1 次 `model.failed` 重试导致 `cached_input_tokens` 为 `null`，表中数值取自其 `session.sqlite3` 内全部 `model.completed` 事件的实测 `prompt_cache_hit_tokens`。

| # | Instance ID | 仓库 | 最终状态 | 耗时 | 轮次 / 工具 | Input / Hit% / Output | 费用 (¥) | 压缩 | 子 Agent | 一句话结果摘要 |
|---:|---|---|---|---:|---:|---|---:|:---:|:---:|---|
| 1 | `marshmallow-code__marshmallow-1343` | marshmallow | **Resolved** | 88.4s | 16 / 21 | 1.14M / 88.8% / 9.7k | 0.188 | 否 | 否 | 精准定位嵌套校验 `None` 判空并一次通过 |
| 2 | `marshmallow-code__marshmallow-1359` | marshmallow | **Resolved** | 38.4s | 10 / 12 | 0.20M / 82.6% / 2.8k | 0.049 | 否 | 否 | 0 工具失败，38s 极速修复并通过 |
| 3 | `pydicom__pydicom-901` | pydicom | Unresolved | 110.1s | 12 / 20 | 0.37M / 83.4% / 10.7k | 0.110 | 否 | 否 | 删除了默认 handler 但未新增 `debug(..., default_handler=True)` 参数 |
| 4 | `pydicom__pydicom-1139` | pydicom | Env/Image Error（兼模型差异） | 148.7s | 18 / 26 | 0.75M / 86.8% / 12.4k | 0.162 | 否 | 否 | pytest 8 致 2 个 P2P 必挂；另 `test_next` 期望 `AttributeError` 而模型抛 `TypeError` |
| 5 | `pydicom__pydicom-1256` | pydicom | **Resolved** | 102.1s | 15 / 19 | 0.52M / 81.9% / 5.2k | 0.124 | 否 | 否 | JSON 序列化修复精准通过 |
| 6 | `pydicom__pydicom-1413` | pydicom | Env/Image Error | 244.7s | 24 / 30 | 1.45M / 80.4%* / 29.1k | 0.423* | **是 (1)** | 否 | **核心 F2P 3/3 全过**，仅挂在官方镜像 pytest 8 的 2 个 nose `setup` P2P |
| 7 | `pydicom__pydicom-1694` | pydicom | **Resolved** | 287.9s | 20 / 25 | 0.75M / 86.4% / 7.9k | 0.146 | 否 | 否 | 成功修复并通过官方全部测试 |
| 8 | `pylint-dev__astroid-1196` | astroid | **Resolved** | 252.3s | 28 / 34 | 1.25M / 86.9%* / 44.4k | 0.363* | 否 | 否 | 遇 8 次 CRLF `EDIT_NOT_FOUND` 绕路，最终仍成功修复并通过 |
| 9 | `pylint-dev__astroid-1268` | astroid | Unresolved | 146.0s | 18 / 29 | 0.61M / 87.0% / 11.4k | 0.136 | 否 | 否 | `visit_unknown` 返回 `""` 而非 `str(node)`（P2P 91/91 全过） |
| 10 | `pylint-dev__astroid-1333` | astroid | **Resolved** | 296.5s | 29 / 40 | 2.16M / 92.7% / 29.2k | 0.314 | 否 | 否 | 全场最高输入 Token，但 92.7% 缓存命中控费优秀并最终通过 |
| 11 | `pylint-dev__astroid-1866` | astroid | **Resolved** | 139.2s | 24 / 29 | 0.66M / 87.8% / 9.5k | 0.131 | 否 | 否 | 成功修复并通过官方全部测试 |
| 12 | `pylint-dev__astroid-1978` | astroid | Unresolved | 186.6s | 17 / 25 | 0.54M / 79.3%* / 26.5k | 0.227* | 否 | 否 | 用 `catch_warnings` 静默警告，未用 `redirect_stdout/stderr` 转发至 `logger` |
| 13 | `pvlib__pvlib-python-1072` | pvlib | Env/Image Error | 82.5s | 11 / 16 | 0.21M / 79.3% / 11.1k | 0.091 | 否 | 否 | 补丁 cleanly applied，官方镜像 NumPy 2.0 `np.Inf` 崩溃（Exit 4） |
| 14 | `pvlib__pvlib-python-1154` | pvlib | Env/Image Error | 65.5s | 12 / 15 | 0.24M / 83.0% / 8.4k | 0.079 | 否 | 否 | 补丁 cleanly applied，官方镜像 NumPy 2.0 `np.Inf` 崩溃（Exit 4） |
| 15 | `pvlib__pvlib-python-1606` | pvlib | Env/Image Error | 132.6s | 15 / 19 | 0.52M / 85.8% / 20.5k | 0.164 | 否 | 否 | 官方镜像 NumPy 2.0 崩溃 + `pip install` 离线失败（`evaluation_error`） |
| 16 | `pvlib__pvlib-python-1707` | pvlib | Env/Image Error | 188.5s | 20 / 28 | 1.54M / 84.0% / 37.6k | 0.422 | **是 (1)** | 否 | 官方镜像 NumPy 2.0 崩溃 + `pip install` 离线失败（`evaluation_error`） |
| 17 | `pvlib__pvlib-python-1854` | pvlib | Env/Image Error | 50.3s | 13 / 19 | 0.32M / 84.6% / 5.1k | 0.076 | 否 | 否 | 官方镜像 NumPy 2.0 崩溃 + `pip install` 离线失败（`evaluation_error`） |
| 18 | `sqlfluff__sqlfluff-1517` | sqlfluff | Unresolved | 366.4s | 18 / 29 | 0.99M / 85.3% / 18.8k | 0.237 | 否 | 否 | 改了 `Delimited.match` 而非 `check_still_complete`，报错文案不匹配 |
| 19 | `sqlfluff__sqlfluff-1625` | sqlfluff | Unresolved | 327.1s | 18 / 25 | 0.76M / 86.8% / 24.6k | 0.211 | 否 | 否 | 误判需求改了 L031 规则逻辑，而 Gold 仅改 1 行告警文案 |
| 20 | `sqlfluff__sqlfluff-1733` | sqlfluff | **Resolved** | 477.6s | 28 / 36 | 1.98M / 87.1% / 20.9k | 0.373 | **是 (1)** | 否 | R26 触发压缩后仍精准收敛并通过，全量耗时最长（477.6s） |
| 21 | `sqlfluff__sqlfluff-1763` | sqlfluff | Unresolved | 435.6s | 27 / 39 | 1.66M / 91.2% / 23.5k | 0.270 | 否 | 否 | 实现了写前编码检查，但隐藏测试直接调用私有方法 `_safe_create_replace_file` |
| 22 | `sqlfluff__sqlfluff-2419` | sqlfluff | **Resolved** | 179.4s | 15 / 21 | 0.31M / 82.3% / 3.4k | 0.074 | 否 | 否 | 简洁精准修复并通过 |
| 23 | `pyvista__pyvista-4315` | pyvista | Env/Image Error | 127.7s | 24 / 31 | 0.98M / 89.6% / 13.3k | 0.172 | 否 | 否 | 官方镜像缺 `libGL.so.1` + 离线依赖失败（`evaluation_error`） |

---

## 3. 官方镜像 / 环境故障专项归因（非 Harness 原因）

本次 `dev` 集 23 题中，有 **8 题（34.8%）** 受制于 Docker Hub 上游官方预构建镜像本身的依赖腐化或系统库缺失，在容器内连基础回归测试都无法正常运行。

### 3.1 `pvlib/pvlib-python`（5 题：`1072 / 1154 / 1606 / 1707 / 1854`）

- **现象**：5 题补丁在容器内均 `Applied patch ... cleanly`，但 `pytest` 在加载 `conftest.py` 导入 `pvlib` 时全部立刻崩溃（`Test Exit Code: 4`，0 个用例执行）；其中 `1606 / 1707 / 1854` 在 `eval.sh` 执行 `python -m pip install -e .[all]` 阶段还因离线构建失败被官方评分器标为 `evaluation_error (network_unreachable)`。
- **关键报错堆栈**（取自 `test_output.txt`）：
```text
ImportError while loading conftest '/testbed/pvlib/tests/conftest.py'.
pvlib/tests/conftest.py:10: in <module>
    import pvlib
...
pvlib/singlediode.py:59: in <module>
    NsVbi=np.Inf, breakdown_factor=0., breakdown_voltage=-5.5,
/opt/miniconda3/envs/testbed/lib/python3.9/site-packages/numpy/__init__.py:397: in __getattr__
    raise AttributeError(
E   AttributeError: `np.Inf` was removed in the NumPy 2.0 release. Use `np.inf` instead.
```
- **结论**：上游官方镜像未锁定 `numpy<2` 而预装了 NumPy 2.0，与旧版 `pvlib` 源码中的 `np.Inf` 冲突，无论 Agent 生成何种补丁均不可能通过。

### 3.2 `pydicom/pydicom`（2 题：`1139 / 1413`）

- **现象**：`pydicom-1413` 的核心测试 `FAIL_TO_PASS` 已 **3/3 全部通过**，回归测试 `PASS_TO_PASS` 通过 299/301；`pydicom-1139` 的回归测试通过 36/38。两题挂掉的 2 个 `PASS_TO_PASS` 完全相同：
  - `pydicom/tests/test_valuerep.py::TestBadValueRead::test_read_bad_value_in_VR_default`
  - `pydicom/tests/test_valuerep.py::TestBadValueRead::test_read_bad_value_in_VR_enforce_valid_value`
- **根因**：官方镜像内置 **`pytest 8.3.5`**，已移除对旧式 nose 风格 `def setup(self)` 的支持，导致 `TestBadValueRead.setup()` 未执行而抛 `AttributeError: ... has no attribute 'tag'`。在当前官方镜像下，即便注入 Gold Patch，这 2 个 `PASS_TO_PASS` 仍会失败。

### 3.3 `pyvista/pyvista`（1 题：`4315`）

- **现象**：官方评分器判为 `evaluation_error (grader_infrastructure_failure / network_unreachable)`。
- **关键报错堆栈**（取自 `test_output.txt`）：
```text
File "/opt/miniconda3/envs/testbed/lib/python3.9/site-packages/pytest_pyvista/pytest_pyvista.py", line 8, in <module>
    import pyvista
...
File "/testbed/pyvista/_vtk.py", line 422, in <module>
    from vtkmodules.vtkInteractionWidgets import (
ImportError: libGL.so.1: cannot open shared object file: No such file or directory
>>>>> Test Exit Code: 1
```
- **结论**：上游官方镜像缺少系统 OpenGL 共享库 `libGL.so.1`（`libgl1-mesa-glx`），且 `eval.sh` 触发离线依赖拉取失败，0 个用例得以执行。

### 3.4 为何 `dev` 集不适合作为主力验收集

1. **镜像腐化率过高**：23 题中有 8 题（34.8%）因上游官方镜像依赖未锁死（NumPy 2.0、pytest 8.3.5、缺 `libGL.so.1`）导致官方评分器失效，拉低原始通过率近 21 个百分点。
2. **样本量过小**：有效题数仅 15 题，单题波动即造成 6.7 个百分点的指标跳变，难以区分 Harness 迭代带来的真实增益与随机噪声。
3. **跨平台摩擦干扰**：中文 Windows 环境下官方评分器写 `eval.sh` 缺 UTF-8 编码（已在本次实验期间于 `swebench.py` 以 `-X utf8` 修复）、长路径 `MAX_PATH`（已修复）以及 CRLF 行尾问题（待修）都会在老旧仓库上放大噪音。

---

## 4. 有效失败题深度归因分析

6 道环境正常的 `unresolved` 题目**全部正常完成收敛**（0 例触发 `_stop_on_repeated_tool_failure` / `_stop_on_denied_tool` 误杀，0 例触发 `context.compacted`，0 例触达步数/时间/预算上限）。整体失败格局高度集中于**「类别 4：模型推理/需求与隐藏测试契约偏差」**，并伴随局部**「类别 2：CRLF 编辑摩擦」**与**「类别 3：测试验证收敛不足」**。

---

### 4.1 `pydicom__pydicom-901`

- **主因分类**：**4. 模型推理/定位瓶颈（API 规格理解偏差）**
- **关键转折点**：
  - R01–R06：并行读取 `pydicom/config.py`、检索 `logger` 用法与历史 changelog，准确识别出问题在于 `pydicom` 导入时强制添加了 `StreamHandler` 并设置了 `WARNING` 级别。
  - R07：编辑 `pydicom/config.py`，直接删掉了 `StreamHandler` 和文件末尾的 `debug(False)`，改为 `debugging = False`。
  - R09：运行 `run_visible_tests` 全绿；R10 调用 `run_linter` 失败（容器缺 linter），R11 检查语法后直接结束。
- **Candidate Patch vs Gold Patch 差距**：
  - 模型方案：仅移除 `StreamHandler`，未给 `logger` 挂 `logging.NullHandler()`，且保持 `def debug(debug_on=True)` 单参数签名。
  - Gold Patch：挂上 `logger.addHandler(logging.NullHandler())`，并将 `debug` 扩展为 `def debug(debug_on=True, default_handler=True)`，末尾调用 `debug(False, False)`。
  - 隐藏测试 `TestDebug` 直接调用 `debug(False, False)` / `debug(True, True)`，5 个用例全部 `TypeError: debug() takes from 0 to 1 positional arguments but 2 were given`。
- **切 `max` effort 评估与改进点**：`max` effort 有望提醒模型遵循 Python 官方库日志最佳实践（挂 `NullHandler`），但新增 `default_handler` 这一未在 issue 中明示的新参数名仍带有隐藏测试耦合性；改进点是在提示或规划阶段引导模型优先检查相关 API 的向后兼容开关需求。

---

### 4.2 `pylint-dev__astroid-1268`

- **主因分类**：**4. 模型推理/定位瓶颈（语义返回值约定偏差）**；次因：**3. 验证与收敛不足**
- **关键转折点**：
  - R01–R05：准确定位到 `astroid/nodes/as_string.py` 缺少 `visit_unknown`，R05 用 `run_python_snippet` 复现 `AttributeError: 'AsStringVisitor' object has no attribute 'visit_unknown'`。
  - R10–R12：搜索 `as_string.py` 中其他返回 `""` 的 visitor（如空节点），据此拍板让 `visit_unknown` 返回 `""`，并同步修改了 `ChangeLog` 和 `tests/unittest_nodes.py`。
  - R13–R14：`run_tests` 局部单测通过；R16 `run_visible_tests` 返回 `exit_code=1`（无关用例或新加测试），模型未进一步核查 `Unknown.__str__()` 的语义即在 R17 结束。
- **Candidate Patch vs Gold Patch 差距**：
  - 模型方案：`def visit_unknown(self, node): return ""`。
  - Gold Patch：`def visit_unknown(self, node: "Unknown") -> str: return str(node)`（返回 `"Unknown"`）。
  - 隐藏测试 `AsStringTest::test_as_string_unknown` 断言 `as_string()` 输出 `"Unknown"` 失败；91/91 `PASS_TO_PASS` 全部通过。
- **切 `max` effort 评估与改进点**：`max` effort 下模型更可能检查 `Unknown.__str__()` 的已有定义（`node_classes.py`）而非盲从其他空节点的 `""` 惯例，**有较高希望挽救**。同时应减少无关文件修改（模型主动改了 `ChangeLog`）。

---

### 4.3 `pylint-dev__astroid-1978`

- **主因分类**：**4. 模型推理/定位瓶颈（根因定位偏差）**；次因：**2. 工具与补丁层故障（CRLF `EDIT_NOT_FOUND`）**
- **关键转折点**：
  - R01–R07：读取 `astroid/raw_building.py` 和 `fake_module_with_warnings` 测试夹具，用 `run_python_snippet` 复现 `np.long` 触发的 `FutureWarning`，**误将问题归结为 Python `warnings` 模块的警告抑制**。
  - R08 / R10：两次 `edit_file` 报 `EDIT_NOT_FOUND`；R09 用 `run_python_snippet` 查出文件为 `CRLF: 579` 行尾，R11 遇一次 `ProviderError` 自动重试后，R12 才成功写入补丁。
  - R13–R16：用自写复现脚本和局部 `run_tests` 验证警告消失后结束。
- **Candidate Patch vs Gold Patch 差距**：
  - 模型方案：在 `getattr(sys.modules[modname], name)` 外包了一层 `with warnings.catch_warnings(): warnings.simplefilter("ignore")`。
  - Gold Patch：用 `contextlib.redirect_stdout(io.StringIO())` 和 `redirect_stderr(io.StringIO())` 捕获 `getattr` 期间打印到 stdout/stderr 的内容，并分别通过 `logger.info` / `logger.error` 记录。
  - 隐藏测试 `test_build_module_getattr_catch_output` 通过自定义 `__getattr__` 向 stdout/stderr 打印内容并断言 `expected_out in caplog.text`，模型方案未做任何 stdout/stderr 重定向，1/1 F2P 失败（12/12 P2P 全过）。
- **切 `max` effort 评估与改进点**：Issue 描述的是第三方库（如 numpy/pandas）在 `getattr` 时打印输出/警告污染终端，`max` effort 对“捕获输出而非仅静默 `warnings`”的语义理解会更稳健，但 `caplog` 日志级别的精确要求仍有一定耦合度。工程上必须修复 `edit_file` 的 CRLF 兼容（消除无谓的编辑重试）。

---

### 4.4 `sqlfluff__sqlfluff-1517`

- **主因分类**：**4. 模型推理/定位瓶颈（修复层级选择偏差）**；次因：**3. 验证与收敛不足**
- **关键转折点**：
  - R01–R08：复现 `select id from tbl;;` 抛 `RuntimeError: Dropped elements in sequence matching!`，读取了 `helpers.py`（`check_still_complete`）和 `delimited.py`（`Delimited.match`）。
  - R09 / R12：模型认为 `check_still_complete` 的不变量不应破坏，转而在 `src/sqlfluff/core/parser/grammar/delimited.py` 中修改 `trim_non_code_segments` 与 unmatched 返回逻辑，使之变成普通 `PRS` 告警。
  - R15：`run_visible_tests` 与一次 `run_tests` 均返回 `exit_code=1`（3 处 `F`），模型未彻底排查全部失败用例，在 R16 用自写复现脚本确认返回 `exit: 65`（PRS）后即于 R17 结束。
- **Candidate Patch vs Gold Patch 差距**：
  - 模型方案：改动 `delimited.py` 约 15 行，输出通用 `Found unparsable section` 类 PRS 报错。
  - Gold Patch：仅改 `src/sqlfluff/core/parser/helpers.py` 中 `check_still_complete` 的 5 行，直接将 `RuntimeError` 替换为 `raise SQLParseError(f"Could not parse: {current_str}", segment=unmatched_segments[0])`。
  - 隐藏测试 `test__dialect__ansi_multiple_semicolons` 精确断言报错消息为 `Could not parse: ;` / `Could not parse: ;;;;;` 等，3 个参数化用例全挂（42/42 P2P 全过）。
- **切 `max` effort 评估与改进点**：`max` effort 有助于评估「改动底层 `Delimited` 风险 vs 在 `check_still_complete` 优雅抛出 `SQLParseError`」；同时 R15 `run_visible_tests` 已报 `FFF` 却未阻塞提交，说明 `completion_gate` 关闭时模型容易对局部自测绿灯过早满足。

---

### 4.5 `sqlfluff__sqlfluff-1625`

- **主因分类**：**4. 模型推理/定位瓶颈（需求误读）**；次因：**3. 验证与收敛不足**
- **关键转折点**：
  - R01–R10：读取 `src/sqlfluff/rules/L031.py`、`L031.yml` 与 `test/cli/commands_test.py`，并注意到 `CHANGELOG.md:533` 明确写着 `Added Rule L031 to enforce "avoid using alias in the FROM/JOIN clauses"`。
  - R11–R12：模型仍误将 Issue 理解为「单表无 JOIN 时不应触发 L031」，在 `Rule_L031` 中加入 `has_join` 判断，跳过无 JOIN 查询的已使用别名检查，并在 `L031.yml` 新增了反向 pass 用例。
  - R15–R16：`run_tests` 和 `run_visible_tests` 均返回 `exit_code=1`（`FFF`），模型未据此反思需求假设，R17 仅跑了局部测试通过即提交。
- **Candidate Patch vs Gold Patch 差距**：
  - 模型方案：修改 `Rule_L031` 判定逻辑并改测试夹具，保留旧文案 `"Avoid using aliases in join condition"`。
  - Gold Patch：**仅修改 1 行告警文案**：`"Avoid using aliases in join condition"` → `"Avoid aliases in from clauses and join conditions."`，未改动任何规则逻辑！
  - 隐藏测试 `test__cli__command_directed` 精确断言新文案失败（64/64 P2P 全过）。
- **切 `max` effort 评估与改进点**：R06 模型已搜索到 `CHANGELOG.md` 中 L031 的设计初衷（同时覆盖 `FROM` 和 `JOIN`），但仍被 Issue 标题带偏；`max` effort 在权衡「改行为 vs 改文案」时有中等概率挽救。此外，R15–R16 已有明显回归失败信号，值得在 Harness 层增加“结束前若有未解决测试失败则提示复核”的软性收敛机制。

---

### 4.6 `sqlfluff__sqlfluff-1763`

- **主因分类**：**4. 模型推理/定位瓶颈（隐藏测试耦合内部私有方法签名）**
- **关键转折点**：
  - R01–R17：深入排查 `linted_file.py`、`file_helpers.py` 与 `chardet` 行为，完整复现了 `chardet` 将含特殊 UTF-8 字符的短文件误判为 `Windows-1252`，导致 `open(fname, "w")` 先清空文件再抛 `UnicodeEncodeError` 的数据丢失 bug。
  - R19–R20：在 `file_helpers.py` 新增 `_detect_encoding`（优先校验合法 UTF-8），并在 `linted_file.py` 的 `persist_tree` 中加入 `write_buff.encode(self.encoding)` 写前预校验，防止截断清空原文件。
  - R21–R25：多组自写复现脚本与 `run_tests` 均验证原文件不再被清空；R26 某次 `run_tests` 失败后模型未继续展开即结束。
- **Candidate Patch vs Gold Patch 差距**：
  - 模型方案：写前编码检查 + 改进 `chardet` 探测逻辑（功能上确实解决了文件被清空的问题，且 63/63 P2P 全过）。
  - Gold Patch：采用 `tempfile.NamedTemporaryFile` + `shutil.move` 原子替换，并封装为静态方法 `LintedFile._safe_create_replace_file(fname, write_buff, encoding)`。
  - 隐藏测试 `test_safe_create_replace_file` **直接以白盒方式调用 `linter.LintedFile._safe_create_replace_file(...)`**，导致前 3 个用例因 `AttributeError` 失败（第 4 个 `incorrect_encoding` 因 `try...except: pass` 碰巧通过）。
- **切 `max` effort 评估与改进点**：此题属于经典的 SWE-bench 白盒测试耦合（隐藏测试直接调用 PR 作者新造的私有静态方法名 `_safe_create_replace_file`），**即便切到 `max` effort 也几乎不可能猜中该私有方法名**。这进一步印证了 SWE-bench Lite 老题存在测试过拟合问题。

---

### 4.7 补充说明：`pydicom__pydicom-1139`（环境故障 + 契约差异双因）

虽然 `pydicom-1139` 已因官方镜像 `pytest 8.3.5` 必挂 2 个 P2P 归入第 3 节，但其 `FAIL_TO_PASS`（2/3）中 `test_next` 失败值得单提：模型实现了符合 Python 惯例的 `__iter__`（`iter(str(self))`），因此 `next(PersonName(...))` 抛出 `TypeError`；而 Gold Patch 将 `PersonName` 自身实现为带 `_i` 状态的迭代器（`__next__`），未先 `iter()` 直接调用 `next()` 时因缺少 `_i` 抛出 `AttributeError`。此差异同样属于隐藏测试对特定实现细节的强耦合。

---

## 5. 成功案例中的共性观察与效率分析

对 9 道成功通过（`Resolved`）的题目进行轨迹统计，可观察到以下显著特征：

1. **只读并发（`parallel_read_tools`）全面生效**：
   - 全量 23 题中 **23/23 题**均触发了同轮并行只读工具调用（单题并行轮次 3~13 轮，单轮最高并发 2~3 个只读调用），其中 9 道通过题的并行轮次占总轮次比例达 **30%~45%**（如 `astroid-1333` 12 轮、`sqlfluff-1733` 9 轮），显著压缩了定位阶段的串行延迟。
2. **`sticky_context_checkpoint` 与 Prefix Cache 效率极高**：
   - 全量 23 题实测 Prefix Cache 命中率高达 **86.67%**，使总输入高达 19.91M Token 的全量 `dev` 评测实际闲时花费仅 **¥4.54**（单题均摊 ¥0.20）。
   - `astroid-1333` 在 29 轮 / 2.16M 输入 Token 下命中率达 **92.7%**，仅花费 ¥0.314。
   - 唯一触发上下文压缩后通过的 `sqlfluff-1733` 在 R26 将上下文从约 149.5k Token 压缩至 94.7k Token（保留 `context.checkpoint`），全程仍维持 **87.1%** 缓存命中率并顺利通过官方测试，验证了 `sticky_context_checkpoint` 在长轨迹下的稳定性。
3. **只读探索子 Agent（`ExplorationAgent` / `delegate_exploration`）全程零调用**：
   - 23 题中 **`delegate_exploration` 调用次数为 0**。日志确认该工具已正确注册并写入了任务提示词（`Available additional tools: ... delegate_exploration ...`），但 `deepseek-flash` 在中小型单仓任务中倾向直接使用 `parallel_read_tools`（`search_text` + `read_file`），从未主动委派子 Agent。这说明当前 `delegate_exploration` 的触发收益在 SWE-bench Lite 尺度的仓库中偏低，或需在大型多模块仓库中结合更明确的触发策略评估其价值。
4. **“通过但走弯路”的典型高消耗案例**：
   - **`pylint-dev__astroid-1196`（28 轮 / 34 工具 / 14 次工具失败 / 44.4k 输出 / ¥0.363）**：因 Windows 检出 CRLF 行尾，连续遭遇 **8 次 `edit_file` `EDIT_NOT_FOUND`**，模型被迫通过 `run_python_snippet` 检查字节流发现 `\r\n` 后才手动绕过，白白消耗了约 8 轮推理与大量思考输出。
   - **`sqlfluff__sqlfluff-1733`（28 轮 / 36 工具 / 477.6s / 1.98M 输入 / ¥0.373）**：前期花了 10 轮用 `run_python_snippet` 逐一排查规则组合（L003/L036/L039），叠加 `sqlfluff` 沙箱单次启动与 `pytest` 耗时较长，最终逼近 600s 墙钟上限（79.6%）。
   - **共性噪音：`run_linter` 在全部官方镜像中 100% 报错**：几乎每道题模型在收尾阶段都会尝试调用 `run_linter`（内置依赖 `ruff`），而官方 SWE-bench 容器内均未安装 `ruff`，导致每次调用必返 `exit_code=1` Traceback，既浪费 1 次工具调用，也增加无效错误上下文。

---

## 6. 下一步改进清单（Action Items）与基准迁移建议

### 6.1 按优先级排序的工程改造清单

| 优先级 | 改造项 | 对应代码文件 | 实测依据与预期收益 |
|---|---|---|---|
| **P0 必改** | **`edit_file` 行尾自动兼容（LF ↔ CRLF）+ 本地预取锁定 `core.autocrlf=false`** | `src/stellarweaver/tools/files.py`<br>`scripts/swebench_eval_helper.py` | 消除 `astroid-1196`（8 次失败）和 `astroid-1978`（2 次失败）的 `EDIT_NOT_FOUND` 摩擦；写回时保留文件原始行尾不污染 diff |
| **P0 必改** | **修复 `swebench.py` 缓存命中统计误判 `incomplete`** | `src/stellarweaver/evals/swebench.py#L672-L706` | 当前 `requests` 混入了 `model.failed`，导致单次网络重试（`pydicom-1413`、`astroid-1196/1978`）即丢弃全题 `cached_input_tokens`；改为仅对 `model.completed` 汇总缓存命中 |
| **P1 建议** | **探测容器工具可用性，动态隐藏或降级 `run_linter`** | `src/stellarweaver/tools/development.py`<br>`src/stellarweaver/evals/swebench.py` | 本轮中官方容器均无 `ruff`，每次 `run_linter` 均报 Traceback；在不可用环境中禁用或返回结构化提示，避免浪费轮次与污染上下文 |
| **P1 建议** | **增加软性收敛复核（未解决测试失败提醒）** | `src/stellarweaver/runtime/loop.py`<br>`src/stellarweaver/runtime/task_evidence.py` | `astroid-1268`、`sqlfluff-1517/1625/1763` 在最终提交前均有未解释的 `run_visible_tests` / `run_tests` 失败；在 `completion_gate=false` 下可考虑轻量注入一次「存在失败测试」提示（避免硬卡死） |
| **P2 观察** | **评估 `reasoning_effort: max` 在复杂推理题上的收益** | `src/stellarweaver/config.py` / CLI 参数 | 6 道有效失败题全部源自推理/需求判断而非 Harness 阻断；可在新基准上做 `high` vs `max` 的 A/B 对照（重点关注耗时与输出成本增幅） |
| **P2 观察** | **观察 `delegate_exploration` 在大型仓库中的触发率** | `src/stellarweaver/runtime/exploration.py`<br>`src/stellarweaver/runtime/loop.py` | 本轮 0/23 触发；无需在小型仓库强行诱导，待切到多文件/大型仓库基准（如 SWE-bench Pro / Terminal-Bench）后再观测其实际 ROI |

### 6.2 基准迁移建议与最小验收检查项

鉴于 SWE-bench Lite `dev` 集 34.8% 的官方镜像已腐化且样本量仅 23 题，建议下一阶段直接迁移至**容器环境更干净、且已有 DeepSeek-V4.1-Flash-1017 公开对照基线**的基准（例如 **Terminal-Bench 4.1 63 题无 GPU 版** 或 **SWE-bench Pro V2 HARD-51**）。

在正式启动下一阶段全量评测前，建议执行以下 **5 项最小验收检查（Pre-flight Checklist）**：

1. **P0 修复落地并过单测**：合入 `edit_file` CRLF 兼容修复与 `swebench.py` 缓存统计修复，`ruff check .`、`mypy src/stellarweaver` 与相关单元测试全绿。
2. **容器环境 3 题冒烟测试**：在新基准中随机抽 3 题（覆盖不同基础镜像），先验证空补丁 / 原始仓库在官方镜像内能正常启动 `pytest`（排除类似 NumPy 2.0 / pytest 8 / 缺 `libGL.so.1` 的上游镜像坏件）。
3. **开发工具链探活**：确认新基准容器内 `python`、`pytest`、`git` 路径及 `run_linter` 行为正常，无高频无意义 Traceback。
4. **Windows 路径与编码双保险**：确认评分器以 `-X utf8` 启动、补丁校验临时目录走短路径系统 temp、本地 git 资产库以 `core.longpaths=true` + `core.autocrlf=false` 预拉取。
5. **预算与对照锁定**：在 `manifest.json` 中显式冻结与 1017 公开基线可比的 `reasoning_effort`、`context_window_tokens`、`max_wall_seconds` 与 `max_tool_calls`，并启用按批次自动清理镜像守卫（`prune-images --report`）。
