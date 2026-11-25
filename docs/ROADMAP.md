# 后续路线图

当前交互与执行主链路已完成 CLI M0–M5、工作台 G0–G11、Harness M0–M10，以及 **N 阶段（Batch 1–3）与上下文三项优化**——涵盖 200k 窗口、实测 Token 动态校准与粘性前缀检查点、单文件多块编辑、跨文件补丁事务写回与成组撤销、4 路保序只读并发、增量快照传输、缓存加速导航与导入加权 RepoMap、共享预算只读探索 Sub-agent、沙箱测试授权复用及全套开关部署。 [当前实现与验收](HARNESS_ACCEPTANCE.md#当前实现进度核对) · [历史开发进度](archive/PROGRESS.md)

## SWE-bench Lite 评测与基准演进（M11–M12）

| 顺序 | 工作 | 状态与验收标准 |
| --- | --- | --- |
| 1 | 完成 23 题 `dev` 环境与 `full` 实跑 | **已完成**：已支持直接复用官方镜像、`scripts/swebench_eval_helper.py` 按仓库分批拉取/预取/防误删清理，以及 `-X utf8` 与 `evaluate --instance-id` 定向重评。全量 23 题均产出合法补丁（官方原始 **9/23 = 39.1%**，剔除 8 题上游镜像腐化后 **9/15 = 60.0%**，均摊 ¥0.20/题）；详细归因见 [SWEBenchLiteEval.md](SWEBenchLiteEval.md)。 |
| 2 | 落地 Phase 1–2 工程修复与长程容错 | **已完成**：已落地 CRLF 兼容、`run_linter` 零开销降级、未解决测试失败软提醒、缓存统计修复，以及 `long_horizon_deepseek_flash()`（512k/64k、`max_output_tokens_total=1M`）、`length` 安全续写、双级失败引导、流式复读检测与零字节断连记账修复（全量 668 项离线回归全绿）。 |
| 3 | 接入 Harbor 与 Terminal-Bench 4.1 冒烟 | **5 题 CPU 冒烟已完成**：已通过 Harbor `0.25.0` 原生 `BaseAgent` 与 `terminal-minimal` 单 `bash` MVP 完成 5 题跨领域纯 CPU 冒烟，实测 **2/5（40.0%）**、Prefix Cache **96.38%**、谷期总费 ¥13.37；基于 610 次 `bash` 零编辑摩擦决定跳过 Phase 3b。下一步在谷期推进 63 题无 GPU 全量跑榜，详见 [TerminalBench4Eval.md](TerminalBench4Eval.md) 与 [StellarWeaver_Harness_Roadmap.md](StellarWeaver_Harness_Roadmap.md)。 |
| 4 | 推进单变量消融与 `test` 300 题（按需） | 固定模型、题目、评分器与总预算，逐项对照导航、受控开发工具、多块/跨文件编辑、并发只读、校准上下文与只读探索委派，在工作台「实验记录」核对配对通过率、Token 费用、Prefix Cache 命中率与耗时。 |

实验设计、配对统计口径与命令见[评测体系与实验计划](EVALUATION.md#swe-bench-lite-实验设计与对照规划)，全量 23 题实测见[SWE-bench dev 23 题评测报告](SWEBenchLiteEval.md)，Terminal-Bench 4.1 冒烟实测见[TerminalBench4Eval.md](TerminalBench4Eval.md)。

## 架构与工作台后续演进

- **线程历史轮次分叉（`N6-a`）**：在线程私有裸仓库为每轮运行记录不可变快照引用，支持从任意历史轮次派生独立线程与独立 worktree，同时保证删除原线程不影响子线程。
- **前端流式与状态架构优化**：在 SSE 接收层合并高频 `assistant.delta` 减少重渲染，将根组件状态拆分为独立 Hook，并支持大补丁 Diff 折叠与探索子 Agent 过程的折叠展示。
- **应用验证与交付闭环**：演练 IDE 应用补丁后回传验证证据、PostgreSQL 与状态卷的联合备份恢复，以及在隔离环境管理开发服务与页面验证证据。
- **扩展与生产部署**：评估 Web 运行时 MCP 接入的信任与生命周期边界，验收实际组织 OIDC 身份源、HTTPS 与跨平台部署。
