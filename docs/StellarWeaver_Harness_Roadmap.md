# StellarWeaver Harness 多阶段演进与长程评测改造方案

> **适用对象**：可直接作为开发规格说明书交给 Coding Agent（如 Neon / Claude Code / Codex）按阶段实施。  
> **基线依据**：[lyx0902/StellarWeaver](https://github.com/lyx0902/StellarWeaver)（commit `2648242e`）源码审查 + [SWEBenchLiteEval.md](https://github.com/lyx0902/StellarWeaver/blob/master/docs/SWEBenchLiteEval.md) 23 题实测归因 + `DeepSeek-V4.1-Flash-1017` 主流 Harness 对照数据。  
> **版本修订**：2026-11-25 已按仓库真实源码架构与安全契约完成逐行核对与修订。

## 1. 背景校准：实测基线与高分 Harness 差距总览

### 1.1 当前基线（SWE-bench Lite `dev` 23 题，`deepseek-flash` = DeepSeek-V4.1-Flash-1017）

| 维度 | 当前实测结果 | 诊断结论 |
| :--- | :--- | :--- |
| 有效通过率 | **9 / 15（60.0%）**（剔除 8 题官方镜像依赖腐化） | 主链路稳定（0 空补丁、0 超时、0 硬中断），但 `n=15` 95% Wilson CI 为 `[35.7%, 80.2%]`，不宜再在 Lite `dev` 调参 |
| 成本与缓存 | 总费 **¥4.54**（均摊 **¥0.20/题**），Prefix Cache 命中率 **86.67%** | `sticky_context_checkpoint` + `parallel_read_tools` 效果极佳，改造中必须保留前缀稳定性 |
| 工具层摩擦 | `astroid-1196` 连续 **8 次**、`astroid-1978` **2 次** `EDIT_NOT_FOUND`；几乎每题收尾 `run_linter` 报 `No module named ruff` | 纯确定性工具 Bug，属于零争议 P0/P1 修复项 |
| 收敛与验证 | `astroid-1268`、`sqlfluff-1517/1625` 均在 `run_visible_tests` 报 `exit_code=1` 后仅凭局部 `run_tests` 通过即 `model finished` 提交 | 6 道有效失败题中最能通过 Harness 直接挽回 2–3 题的突破口 |
| 探索子 Agent | `delegate_exploration` 全程 **0 次调用** | `deepseek-flash` 偏爱并行只读工具；未调用的工具 Schema 在通用终端任务中白占 Preamble Token |

### 1.2 下一阶段目标基准与外部 1017 标杆

下一阶段主战基准推荐迁移至完全开源、依赖锁定、镜像轻量的 **[Terminal-Bench 4.1](https://www.tbench.ai/)（63 题无 GPU 版）**，辅以 **[SWE-bench Pro V2 HARD-51](https://arxiv.org/html/2610.24206v1)**（51 题真实大仓库修 Bug）。当前同模型（`DeepSeek-V4.1-Flash-1017`）公开标杆如下：

| 基准 | Agent Harness | 思考强度 | 成绩 | 题均成本 / 耗时 | 来源 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Terminal-Bench 4.1**（66 题） | **Ante** (0.3.5) | `max` | **40.9%**（27/66） | $0.575/题 · 整轮 77.4 min | [Antigma Eval](https://antigma.ai/eval) |
| **Terminal-Bench 4.1**（66 题） | **Claude Code** | `max`（avg@5） | **38.5% ± 1.92 SE** | 未公开 | [tbench.ai](https://www.tbench.ai/) |
| **Terminal-Bench 4.0**（同 66 题） | **Claude Code** | 官方模型卡 | **42.3%** | 未公开 | [HF 模型卡](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017) |
| **Terminal-Bench 4.0**（同 66 题） | **Mini-SWE-agent** | `max`（avg@3） | **31.82% ± 2.62%** | $0.54/题 · 39.65M tok · **41m10s/题** | [Vals AI](https://www.vals.ai/benchmarks/terminal-bench-4) |
| **Bug Hunt Bench**（105 Bug） | **DeepSeek Harness / Pi / Claude Code / OpenCode** | `max`（n=5–7） | **42.7 / 42.2 / 39.1 / 37.6** | Pi 仅 $0.83/轮（无 subagent） | [Bug Hunt Bench](https://github.com/phuryn/bug-hunt-bench/blob/main/results/run-notes.md) |

---

## 2. 源码级差距定位矩阵

以下每一项均已核对 [StellarWeaver](https://github.com/lyx0902/StellarWeaver) 源码精确位置，并与高分 Harness 实证经验及现有安全边界对齐：

| 编号 | 源码文件与精确位置 | 当前实现行为 | 实测 / 基准证据 | 改造目标 |
| :--- | :--- | :--- | :--- | :--- |
| **G1** | [tools/files.py](https://github.com/lyx0902/StellarWeaver/blob/master/src/stellarweaver/tools/files.py) `_scan_file` (L174) & `prepare_edit_file` (L378–397)；[tools/staging.py](https://github.com/lyx0902/StellarWeaver/blob/master/src/stellarweaver/tools/staging.py) (L93) | 读文件剥离 `\r`（模型永远看到 LF），但编辑时仅把 `new_text` 转回 CRLF，**未对 `old_text` 做 CRLF 归一化**；`stage_patch`/`apply_patch` 写回 LF 会污染 CRLF 文件 diff | `astroid-1196` 连续 8 次、`astroid-1978` 2 次 `EDIT_NOT_FOUND` | 匹配时兼容 LF/CRLF（切片长度取转换后长度），`edit_file` 与 `staging` 写回保持原文件行尾，Git 检出锁定 `core.autocrlf=false` |
| **G2** | [tools/development.py](https://github.com/lyx0902/StellarWeaver/blob/master/src/stellarweaver/tools/development.py) `_WORKER` (L74–76) & `register_development_tools` (L308) | 宿主机同步注册 `run_linter`，容器内硬编码 `runpy.run_module("ruff")` | 官方容器均无 `ruff`，每题收尾白耗 1 轮并产生 Traceback 噪音 | `_WORKER` 内 `find_spec("ruff")` 优雅降级无 Traceback；新增 `include_linter` 显式开关在已知无 `ruff` 镜像中跳过注册（零额外容器探活开销） |
| **G3** | [evals/swebench.py](https://github.com/lyx0902/StellarWeaver/blob/master/src/stellarweaver/evals/swebench.py) (L672–706) | `requests` 混入 `model.failed`，只要存在单次重试即把整题缓存统计标为 `incomplete` | 23 题中 3 题丢失缓存报告，需手动查 `session.sqlite3` | 仅对 `model.completed` 事件聚合缓存与费用统计，单独记录 `failed_attempts` |
| **G4** | [runtime/loop.py](https://github.com/lyx0902/StellarWeaver/blob/master/src/stellarweaver/runtime/loop.py) `_drive()` (L1642) & [runtime/task_evidence.py](https://github.com/lyx0902/StellarWeaver/blob/master/src/stellarweaver/runtime/task_evidence.py) (L33–60) | `completion_gate=false` 时直接返回 `model finished`；`TaskEvidence` 未追踪 `apply_patch` 编辑，且仅靠单一 `last_check` 会被局部 `run_tests` 绿灯覆盖 | `astroid-1268`、`sqlfluff-1517/1625` 均在 `run_visible_tests` 失败后仅凭局部 `run_tests` 通过即提交 | 追踪当前编辑版本下所有未修复的失败测试（按 `(tool, target, selector, keyword)`），提交前最多注入 1 次轻量软提醒 |
| **G5** | [config.py](https://github.com/lyx0902/StellarWeaver/blob/master/src/stellarweaver/config.py) `ModelConfig` / `LimitsConfig` / `RetryConfig`；[evals/swebench.py](https://github.com/lyx0902/StellarWeaver/blob/master/src/stellarweaver/evals/swebench.py) | `config.py` 已支持 `reasoning_effort="max"` 但 `swebench.py` CLI 尚未暴露；`request_timeout_seconds=90`、`max_output_tokens=16384`、`max_wall_seconds=900`（评测 600s）、`RetryConfig.max_retries` 硬上限 `le=2` | TB 4.x 单题均值 **41 分钟 / 39.65M tokens**，1017 首字延迟 ~40s | 解禁 `RetryConfig` 上限、补齐 CLI `--reasoning-effort`；先用真实 API 探针校验线上 `deepseek-flash` 上下文/输出上限后再固化 `long_horizon` 预设 |
| **G6** | [runtime/loop.py](https://github.com/lyx0902/StellarWeaver/blob/master/src/stellarweaver/runtime/loop.py) `_drive()` (L1624) & `_observe_tool()` (L597–628)；[providers/streaming.py](https://github.com/lyx0902/StellarWeaver/blob/master/src/stellarweaver/providers/streaming.py) | `stop_reason == "length"` 直接判 `failed`；存在 `RepairController` 与 `_failure_repeats>=3` 两套重复失败逻辑；`reasoning_content` 在 `ChatCompletionAccumulator` 静默累加，无复读循环检测 | [arXiv:2610.04433](https://arxiv.org/html/2610.04433v1) 证实无截断续写丢 8–9 分；[dsh #11943](https://github.com/deepseek-ai/deepseek-harness/discussions/11943) 记录 1017 偶发复读 | `length` 安全续写（校验/丢弃半截 `tool_calls`、防空 `assistant` 消息）；统一重复失败为「工具尾部软引导 + 高阈值熔断」；在 `ChatCompletionAccumulator.feed()` 加流式复读检测 |
| **G7** | [tools/commands.py](https://github.com/lyx0902/StellarWeaver/blob/master/src/stellarweaver/tools/commands.py) `controlled_environment` (L59–85) & `execute_command` (L253–430)；[web/isolation.py](https://github.com/lyx0902/StellarWeaver/blob/master/src/stellarweaver/web/isolation.py) | 宿主机 `max_timeout_seconds=300` 硬拒绝、未注入非交互环境变量；`DockerRunner` 为一次性 `--rm --read-only` 快照容器，不支持持久容器内 `apt`/常驻 daemon | [apache/maka DSH 复盘](https://github.com/apache/maka/blob/main/docs/eval/terminal-bench-2.1-deepseek-v4-flash-nine-arm.md)：5 分钟超时 + `tzdata` 交互卡死 + 退出杀后台服务直接白丢 4 题 | 宿主机仅注入非交互环境变量与放宽超时上限（**保留强进程清理契约**）；持久容器操作与后台守护进程保活由 Harbor `docker exec` 适配器层承担 |
| **G8** | [config.py](https://github.com/lyx0902/StellarWeaver/blob/master/src/stellarweaver/config.py) `HarnessConfig` & [tools/registry.py](https://github.com/lyx0902/StellarWeaver/blob/master/src/stellarweaver/tools/registry.py) | 19 个开关散落配置；内置仅有 `run_command(argv)` 和禁止覆写的 `create_file`，尚无面向隔离持久容器的 `bash` / `write_file` 极简 Profile | TB 4.1 中 70%+ 为非 Python 实务任务；[Pi](https://github.com/phuryn/bug-hunt-bench/blob/main/results/run-notes.md) 和 [Ante `pi` profile](https://github.com/AntigmaLabs/ante) 用 4 工具极简集跑出顶尖性价比 | 新增 `swe-python` 与**仅限隔离容器 Profile 注册**的 `terminal-minimal`（`bash` / `read_file` / `edit_file` / `write_file`），在 TB 4.1 中关闭 Python 专用工具和零调用的 `delegate_exploration` |

---

## 3. 分阶段实施路线图（可直接按阶段派发给 Agent 执行）

### Phase 1：消除实测摩擦与“草率提交”（预计 0.5–1 天，P0/P1）

**阶段目标**：彻底修掉 SWE-bench Lite `dev` 实测暴露的确定性工具 Bug 与验证收敛缺口，不改变现有核心协议与缓存前缀稳定性。

#### Task 1.1：`edit_file` 与 `staging`（`stage_patch`/`apply_patch`）的 LF/CRLF 自动兼容
* **目标文件**：`src/stellarweaver/tools/files.py`、`src/stellarweaver/tools/staging.py`、`src/stellarweaver/evals/swebench_pilot.py`、`scripts/swebench_eval_helper.py`
* **具体改动**：
  1. 在 `prepare_edit_file`（L378–397）中，检测原文件行尾类型（纯 CRLF、纯 LF 或混合）。
  2. 当原文件包含 `\r\n` 且 `text.count(edit.old_text) == 0` 时，构造 `matched_old = edit.old_text.replace("\r\n", "\n").replace("\n", "\r\n")` 后重试精确匹配；**切片终点必须使用 `start + len(matched_old)`（而非原始 `len(edit.old_text)`）**；若为混合行尾文件，可在统一归一化为 `\n` 的副本上定位行区间并精确映射回原字节区间，禁止引入模糊语义匹配以保持确定性。
  3. 写回替换内容 `replacement` 时严格沿用原匹配片段的行尾风格（不破坏无改动行的行尾）。
  4. 在 `tools/staging.py` 中抽取统一的**行尾归一化辅助函数**，由 `_prepare_patch`（L93）与 `prepare_apply_patch`（L215）**共同调用**（严禁两处逻辑分叉导致准备阶段与写入阶段哈希校验冲突）：当原文件为纯 CRLF 且模型传入 LF `change.content` 时，编码前统一转为 CRLF，避免 `stage_patch`/`apply_patch` 产生整文件换行脏 diff。
  5. 在 `scripts/swebench_eval_helper.py`（`prefetch-assets`）和 `src/stellarweaver/evals/swebench_pilot.py`（`checkout()`）中均显式传 `-c core.autocrlf=false` 与 `-c core.longpaths=true`。
* **单测要求**：在 `tests/` 补充纯 LF、纯 CRLF、混合行尾、多处编辑（`MultiEditFileArgs`，复用工具名 `edit_file`）、`apply_patch` CRLF 保持与哈希一致以及 `EDIT_AMBIGUOUS` / `EDIT_OVERLAPPING` 回归测试。

#### Task 1.2：`run_linter` 零开销优雅降级与按环境显式开关
* **目标文件**：`src/stellarweaver/tools/development.py`、`src/stellarweaver/runtime/service.py`、`src/stellarweaver/evals/swebench.py`、`src/stellarweaver/config.py`
* **具体改动**：
  1. **容器内优雅降级（消除 Traceback）**：在 `tools/development.py` 的 `_WORKER`（L74–76）中，执行 `run_linter` 前先检查 `importlib.util.find_spec("ruff")`；缺失时干净打印结构化提示 `"ruff is not installed in this environment; skip linting"` 并以退出码 `0` 返回（附带结构化标记 `skipped=True`），不再拉起 `runpy.run_module("ruff")` 吐 Python Traceback。
  2. **按环境显式隐藏（省去 Tool Schema Token 且零额外启动开销）**：为 `register_development_tools(registry, *, include_linter: bool = True)` 增加显式开关（由 `HarnessConfig` 或调用方传入）；**不在宿主机同步 `attach_harness()` 中调用宿主机 `find_spec("ruff")`，也不每题额外起 `docker run` 探活**。在 `evals/swebench.py` 复用官方 SWE-bench 镜像（或 recipe 未声明 linter 可用）时直接传 `include_linter=False` 跳过注册 `run_linter`。
* **单测要求**：验证 `include_linter=False` 时 `ToolRegistry` 不暴露 `run_linter`；Mock 容器无 `ruff` 时 `_WORKER` 执行 `run_linter` 返回结构化跳过结果且无 Traceback。

#### Task 1.3：未解决测试失败的轻量软提醒（Soft Convergence Guard）
* **目标文件**：`src/stellarweaver/runtime/loop.py`、`src/stellarweaver/runtime/task_evidence.py`、`src/stellarweaver/config.py`
* **具体改动**：
  1. 在 `HarnessConfig` 新增布尔开关 `soft_test_failure_reminder: bool = True`（独立于重型 `completion_gate`）；确保开启该开关时 `loop.py`（L233–237）亦会初始化轻量追踪器（不强制耦合 `task_evidence` 注入）。
  2. 补齐 `TaskEvidence.observe()`（L50）对全部写入口的编辑状态追踪与重置：`edit_file`（含 `MultiEditFileArgs` 批量模式，仓库内复用同一工具名 `"edit_file"`）、`create_file`，以及 **`apply_patch`（按 `data["files"]` 数组展开追踪 `create` / `modify` / `delete`，其中 `delete` 时 `after_sha256` 为 `None`）**。
  3. 针对「先 `run_visible_tests` 失败、再局部 `run_tests` 通过即提交」的真实漏网模式：在当前编辑指纹（`candidate_edit_sha256`）下按测试键 `(tool_name, target, selector, keyword)` 记录未解决失败集合——失败时加入集合，同键测试复跑通过时移除，有新代码编辑时清空重置。
  4. 在 `Agent._drive()`（L1642）即将返回 `("completed", "model finished", reply.text)` 前：若 `self.completion_check is None`、开关开启、且当前编辑版本存在未解决失败测试集合非空，**最多拦截 1 次**，向对话注入一条简短 `user` 提示，列出未解决的失败测试项与退出码，要求模型确认失败原因、排查是否遗漏边界或需求误读后再决定结束。
* **单测要求**：使用 `FakeProvider` 构造 3 组单测：① `run_visible_tests` 失败 → `run_tests` 局部通过 → 模型试图结束 → 收到软提醒并继续修复；② 最多只拦截 1 次防死循环；③ 编辑前复现失败 → 编辑代码 → 测试通过 → 不触发误拦截。

#### Task 1.4：修复 `swebench.py` 缓存命中统计误判 `incomplete`
* **目标文件**：`src/stellarweaver/evals/swebench.py`（L672–706）
* **具体改动**：
  1. 汇总 `cached_input_tokens` 与判断是否 `incomplete` 时，仅过滤并统计 `event.type == "model.completed"` 事件（忽略中途重试的 `model.failed` 事件），并在报告中单独保留 `failed_model_calls` 计数。
  2. 仅当存在 `model.completed` 事件本身缺失 `prompt_cache_hit_tokens` 时才将 `cached_input_tokens` 标为 `None`（`incomplete`）。
* **Phase 1 验收标准**：
  * `ruff check .`、`mypy src/stellarweaver`、`pytest` 全绿。
  * 重跑 `astroid-1196 / 1268 / 1978` 与 `sqlfluff-1517 / 1625` 共 5 题（闲时约 ¥1），确认 0 次 `EDIT_NOT_FOUND`、0 次 `run_linter` Traceback、缓存报告 5/5 完整，并观察软提醒是否捞回 1–2 题。

---

### Phase 2：解除短程天花板 & 补齐 1017 长程容错（预计 1–2 天，P0）

**阶段目标**：将 StellarWeaver 从“适合 10 分钟、20 轮的 Lite 短题”升级为“能稳定跑完 40–120 分钟、30M–50M tokens 的 TB 4.1 / SWE-bench Pro V2 长程任务”。

#### Task 2.1：解禁硬上限校验、补齐 CLI 参数并新增 `long_horizon` 配置预设
* **目标文件**：`src/stellarweaver/config.py`、`src/stellarweaver/runtime/loop.py`、`src/stellarweaver/evals/swebench.py`
* **具体改动**：
  1. `RetryConfig.max_retries` 将校验上限从 `le=2` 放宽至 `le=8`；同步更新 `Agent.__init__`（L146）中的 `if not 0 <= max_retries <= 2` 为 `0 <= max_retries <= 8`（`max_delay_seconds` 上限放宽至 `le=120`）。
  2. `ModelConfig.request_timeout_seconds` 上限放宽至 `le=900`（注：该参数同时作用于 `httpx` 流式 chunk 间静默超时）；针对 `provider == "deepseek"` 且 `thinking == "enabled"`，将默认 `request_timeout_seconds` 提升至 `180`–`300`（适应 1017 ~40s 首字延迟与长思考）。
  3. `ModelConfig.reasoning_effort` 与 `OpenAICompatibleChatProvider` 已支持 `"max"`，本任务补齐 `evals/swebench.py` `prepare` CLI 的 `--reasoning-effort low|high|max` 参数与 `manifest.json` 冻结记录。
  4. **前置探针验证**：在写死长程预设数值前，用 1 次真实 API 探针确认线上 `api.deepseek.com` 的 `deepseek-flash` 实际接受的最大 `context_window_tokens` 与 `max_tokens`（避免线上硬拒 HTTP 400），并**顺手验证 Thinking 模式下是否接受 `user -> user` / `tool -> user`（为 Task 2.2 的纯思考空文本截断策略提供实测依据）**。
  5. 新增命名配置预设工厂（如 `long_horizon_deepseek_flash()`），按探针确认后的能力冻结参数：
     * `model = "deepseek-flash"`，`thinking = "enabled"`，`reasoning_effort = "max"`
     * `context_window_tokens = 512_000`（在 API 实测上限内可配至 `800_000`，保留 `sticky_context_checkpoint = True` 作为高水位兜底）
     * `max_output_tokens = 65_536`（若 API 上限更小则取实测允许最大值）
     * `LimitsConfig`：`max_model_calls = None`、`max_tool_calls = 300`、`max_wall_seconds = 3600`（Harbor 任务中服从原生 timeout）、`max_input_tokens_total = 80_000_000`、`max_output_tokens_total = 600_000`
     * `RetryConfig`：`max_retries = 5`

#### Task 2.2：`stop_reason == "length"` 安全续写恢复（防输出截断白丢分）
* **目标文件**：`src/stellarweaver/runtime/loop.py`
* **具体改动**：
  1. 在 `_drive()`（L1624）中，当 `reply.stop_reason in {"length", "max_tokens"}` 时，不再立即设置 `initial_stop = ("failed", ...)`；增加单任务续写上限（如 `max_length_continuations = 3`）。
  2. **严格防御截断的 `tool_calls` 与空消息协议违规**：
     * **若响应包含 `tool_calls`**：逐条用 `json.loads` 校验 `arguments_json` 是否为完整合法 JSON 字典——**绝不盲目执行被截断的半截工具调用**；仅对前面已完整闭合的调用放行，或统一丢弃该轮不完整 `tool_calls`、仅保留已生成的可见文本并触发续写。
     * **若响应无 `tool_calls` 且 `reply.text` 非空**：保留已有文本消息，向对话注入简短续写指令（`"Your previous response hit the output token limit before completing. Continue from where you left off without repeating completed work."`）。
     * **若响应无 `tool_calls` 且 `reply.text` 为空**（纯思考阶段耗尽 `max_output_tokens`）：**不写入空 `assistant` 消息**；依据 Task 2.1 探针结果保证 Role 交替合规——若 API 拒绝 `user -> user` / `tool -> user`，则写入一条极短非空占位 `assistant` 文本（如 `"[Thinking truncated at output token limit.]"`）再接 `user` 续写提示，同时记录 `model.truncated_thinking` 事件要求模型缩短思考直接输出行动/调用工具。
  3. 仅当连续触发截断超过 `max_length_continuations` 时才判为 `failed`。
* **单测要求**：用 `FakeProvider` 覆盖 3 种 `length` 场景（纯文本截断续写、带半截非法 JSON `tool_calls` 拦截不执行、纯思考空文本截断不写空 `assistant` 消息）。

#### Task 2.3：统一重复失败策略（软引导 + 高阈值熔断）与安全边界保持
* **目标文件**：`src/stellarweaver/runtime/loop.py`、`src/stellarweaver/runtime/repair.py`
* **具体改动**：
  1. 收敛 `loop.py`（L597–628）中并存的 `self._repair.observe()` 与 `self._failure_repeats` 两套重复失败计数，统一到清晰的两级阈值（可配置）：
     * **软引导阈值（默认 `3`）**：不立即停止；为**不打断 DeepSeek Thinking 同一 `user` 轮次内的 `reasoning_content` 连续性与 Prompt Cache 前缀**，将聚焦诊断提示附加在该次 `tool` 结果内容尾部（而非额外插一条 `user` 消息），提示模型检查环境/读取完整报错或切换方案；
     * **硬熔断阈值（默认 `6`）**：连续相同指纹失败达第 6 次再终止任务，防止真正卡死烧钱。
  2. `_stop_on_denied_tool` 仅在 `policy.preset == "benchmark"` 下允许配置为 `False`（返回结构化拒绝结果给模型自行调整）；**严格保留 CLI/Web 交互模式（`ask` / `auto`）下用户显式拒绝审批立即停止的安全契约与现有单测**。

#### Task 2.4：`DeepSeek-V4.1-Flash-1017` 流式推理复读循环检测
* **目标文件**：`src/stellarweaver/providers/streaming.py`、`src/stellarweaver/providers/openai_compatible.py`
* **具体改动**：
  1. 鉴于当前 `OpenAICompatibleChatProvider.stream()`（L375–378）仅向 `loop.py` 产出 `text.delta`，而 `reasoning_content` 在 `ChatCompletionAccumulator.feed(chunk)` 内静默累加，**将低开销滑动窗口复读检测器直接内置在 `ChatCompletionAccumulator.feed()` 中**。为避免误杀 `content` 中正常生成的重复代码结构、测试样例或大表格，**检测器默认仅监控 `reasoning_content`**，并采用高保守阈值（达到最小缓冲长度后，连续 ≥ 15–20 次高度重复短语/短行才触发，精准匹配 [deepseek-harness #11943](https://github.com/deepseek-ai/deepseek-harness/discussions/11943) 记录的思考阶段复读模式）。
  2. 命中复读阈值时，在 `feed()` 中直接抛出 `ProviderError("Detected repetitive generation loop in stream", retryable=True, partial_stream=True)`——自动无缝复用 `loop.py` 现成的 `model.interrupted`（丢弃脏流、不污染历史）+ `model.retry_scheduled` 退避重试链路（同时设定单轮复读重试上限如 2 次）。
* **Phase 2 验收标准**：
  * 所有新增容错路径均有 `FakeProvider` / 流式单元测试覆盖；现有 `ask`/`auto` 拒绝审批安全单测全绿。
  * 在 **Terminal-Bench 4.1** 或 **SWE-bench Pro V2 HARD-51** 上抽取 5 道长程题冒烟，验证：0 例因 600s / 3M 预算误杀、0 例因单次 `length` 截断或瞬态 429 直接崩溃。

---

### Phase 3：Terminal-Bench 4.1 持久容器执行与极简 Profile（预计 2–3 天，P1）

**阶段目标**：补齐跨领域终端执行短板，参考 [Ante](https://github.com/AntigmaLabs/ante) 与 [Pi](https://github.com/phuryn/bug-hunt-bench/blob/main/results/run-notes.md) 的架构设计，让 StellarWeaver 在 TB 4.1（非 Python 占比 70%+）上跑出第一梯队成绩，同时**不削弱宿主机 CLI/Web 的安全隔离边界**。

#### Task 3.1：宿主机 `tools/commands.py` 非交互环境与超时放宽（严守清理契约）
* **目标文件**：`src/stellarweaver/tools/commands.py`、`src/stellarweaver/config.py`
* **具体改动**：
  1. **非交互环境变量默认注入**：在 `controlled_environment()`（L59–85）中默认注入（允许 `environment_overrides` 覆盖）：
     * `DEBIAN_FRONTEND="noninteractive"`
     * `GIT_TERMINAL_PROMPT="0"`
     * `PIP_NO_INPUT="1"`
     * `CI="true"`
  2. **放宽单命令超时上限**：在 `ExecutionConfig` 中支持按 Profile 放宽 `max_timeout_seconds`（从默认 `300` 放宽至 `1800`，即 30 分钟），并在命令工具描述中提示模型对编译、大包安装、ML 训练显式传更大的 `timeout_seconds`。
  3. **架构约束**：**不修改宿主机 `execute_command` 的进程组 / Windows Job Object 强清理契约**（不在宿主机加 `keep_background`，以免破坏进程收敛与跨平台测试）。需要跨调用保活的后台守护进程（HTTP/DB/Worker daemon）仅在 Task 3.3 的 Harbor 持久任务容器（`docker exec`）中运行与存活。

#### Task 3.2：引入命名 Harness Profile（`swe-python` vs 容器专用 `terminal-minimal`）
* **目标文件**：`src/stellarweaver/config.py`、`src/stellarweaver/tools/registry.py`、`src/stellarweaver/tools/terminal.py`（新建，容器专用）
* **具体改动**：
  1. 将 `HarnessConfig` 的 19 个零散开关收敛为可复用的命名 Profile，同时保留单项覆盖能力：
     * **`swe-python`（现有默认增强版）**：开启 `parallel_read_tools`、`sticky_context_checkpoint`、`calibrated_context_budget`、`protect_fresh_tool_output`、`post_edit_syntax_hook`、`import_aware_repomap`、`repair_controller="feedback"`、`soft_test_failure_reminder=True`，按环境控制 `include_linter`。
     * **`terminal-minimal`（面向 Harbor / Terminal-Bench 4.1 持久隔离容器）**：参考 Ante 的 `pi` Profile 与 Bug Hunt Bench 的 Pi 设计——关闭 `subagent_exploration`（实测 0 调用）、`import_aware_repomap`、`issue_navigation`、`development_tools`、`post_edit_syntax_hook`、`git` 专用工具，并**关闭宿主机 `ChangesManager.capture_baseline()` / `RepoMap` / `InstructionLoader`**（因 Harbor 容器内目标文件分散在 `/app`、`/etc`、`/tmp`，并非宿主机挂载 Git 仓库）。工具面按渐进两步落地，严禁暴露给宿主机 `ask`/`auto` 模式：
       * **Phase 3a（MVP，单 `bash` 工具）**：首批仅通过 Harbor 容器 exec 暴露 1 个 `bash` 工具，零文件封装返工，先跑通 5 题冒烟并验证持久容器、守护进程保活、缓存命中率与 verifier 闭环；
       * **Phase 3b（按需扩展至 4 工具）**：若 5 题冒烟显示 `sed/heredoc` 编辑大文件易错或耗轮次，再在 `tools/terminal.py` 中通过容器内轻量 Python 脚本（走 stdin/stdout 字节流，完全不碰宿主机 `tools/files.py`）封装容器版 `read_file` / `edit_file` / `write_file`，复用同一套精确匹配与 CRLF 保留逻辑。
     * 保留 `parallel_read_tools`、`sticky_context_checkpoint`、`calibrated_context_budget`、`repair_controller="feedback"`。
  2. 同步在运行 `manifest.json` 中完整记录所用 Profile 名、工具清单与 Preamble Token 大小，方便后续做严格的 A/B 对比。

#### Task 3.3：接入 Harbor 原生 Agent 适配接口（Terminal-Bench 4.1）
* **目标文件**：`src/stellarweaver/evals/terminal_bench.py`（新建）
* **具体改动**：
  1. **优先对接 Harbor 原生 Agent 适配接口（禁止重复造轮子）**：Phase 3 开工前先检查所安装 Harbor 版本的原生 Agent 接口，在 `evals/terminal_bench.py` 中仅实现轻量 Agent 包装类，把命令执行直接委托给 Harbor 传入的持久任务容器 `exec` 接口——**不手写 `docker run`、任务目录解析（`instruction.md` / `task.toml`）或 verifier 容器编排**，既大幅减省维护成本，又保证与官方榜单 100% 同口径；同时与现有一次性 `--rm --read-only` 快照的 `DockerRunner` 彻底解耦，天然支持容器内 `apt-get`、`/etc` 修改与后台 daemon 服务保活。
  2. **按需增强轻量 PTY**：若 5 题冒烟中出现交互式 CLI 阻塞，再在容器执行层补一个基于 `pty` 模块的轻量交互模式。
  3. 接入 `terminal-minimal` + `long_horizon` 配置，记录与 `SWEBenchLiteEval.md` 同口径的每题指标（`resolved`、`duration`、`turns`、`tool_calls`、`input/cache_hit/output tokens`、实际费用、触发压缩/续写/软提醒/复读重试次数），并自动生成 `docs/TerminalBench4Eval.md` 所需的 JSON/CSV 汇总。

---

### Phase 4：谷期正式跑榜与 A/B 验收（1 天开发 + 1 个谷期窗口运行）

1. **谷期窗口与成本闸门**（依据 [DeepSeek Pricing](https://api-docs.deepseek.com/quick_start/pricing/)）：
   * **谷期时段**：北京时间工作日 `18:00–次日 09:00` 及 `12:00–14:00`，或**周末/法定节假日全天**（半价：人民币 `¥0.02 / ¥1.00 / ¥4.00` 每百万 Token；美元 `$0.003 / $0.15 / $0.60` 每百万 Token）。
   * **强制 5 题冒烟成本闸门**：全量开跑前先抽 5 道代表性长程题试跑，实测 Prefix Cache 命中率（确认未因动态终端 stdout 打穿缓存跌破 65%）与单题实际费用，达标后再放行 63 题全量。
   * **63 题无 GPU 版 1 轮（pass@1）预算参考**：在缓存命中率 ≥ 80% 时预计 **$17 – $19（约 ¥120 – ¥140）**；开 **8–16 并发容器**，约 **3 – 6 小时**跑完。
2. **阶段性验收目标（Terminal-Bench 4.1，63 题无 GPU 版，`deepseek-flash` 1017 `max`）**：
   * **及格线**：超过 `Mini-SWE-agent`（TB 4.0 基线 **31.8%**） -> 目标 **≥ 34%**（≥ 22/63）。
   * **优秀线**：对齐 `Claude Code`（TB 4.1 官方榜 **38.5%**）与 `Ante 0.3.5`（**40.9%**） -> 目标 **38% – 42%**（24–27/63）。
3. **可选低成本 A/B 实验（抽 15–20 题）**：
   * 对比 `terminal-minimal` vs `full` Profile 的通过率与 Preamble Token 账单；
   * 对比 `reasoning_effort = high` vs `max` 的性价比拐点。

---

## 4. 给实施 Agent 的执行约束与检查清单

当把本文档交给 Coding Agent（如 Neon）实施时，请要求其严格遵守以下约束：

1. **按 Phase 顺序分 PR/Commit 推进**：每个 Phase 单独提交、单独验收，禁止把 Phase 1–3 混在一个大改动里，确保每次变更都可独立归因。
2. **保护已有核心不变性（不可破坏）**：
   * 不得破坏 `context/manager.py` 的 `ContextBaseline` 固定前缀字节一致性（否则会打穿 86.67% 的 Prompt Cache 命中率）；
   * 不得破坏 `tools/registry.py` 的统一参数校验、`PolicyEngine` 鉴权与 `workspace_write_lock` 写锁边界；
   * 不得破坏宿主机 `tools/commands.py` 的进程组 / Windows Job Object 强清理契约，也不得在宿主机 `ask`/`auto` 模式下暴露容器专用 `bash`/`write_file` 或放宽用户显式拒绝（`denied`）立即停止规则；
   * 不得把确定性压缩改为调用 LLM 做摘要。
3. **强制质量门禁**：每个 Task 完成后必须运行并确保全绿：
   * `ruff check .`
   * `mypy src/stellarweaver`
   * `pytest`（包含本次新增的回归单测）
4. **文档同步**：每个 Phase 完成后同步更新 `docs/ARCHITECTURE.md`（新增 Profile 与容错机制说明）以及最终评测报告 `docs/TerminalBench4Eval.md`。
