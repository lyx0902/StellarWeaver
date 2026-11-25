# 后续路线图

当前交互与执行主链路已完成 CLI M0–M5、工作台 G0–G11、Harness M0–M10，以及 **N 阶段（Batch 1–3）与上下文三项优化**——涵盖 200k 窗口、实测 Token 动态校准与粘性前缀检查点、单文件多块编辑、跨文件补丁事务写回与成组撤销、4 路保序只读并发、增量快照传输、缓存加速导航与导入加权 RepoMap、共享预算只读探索 Sub-agent、沙箱测试授权复用及全套开关部署。 [当前实现与验收](HARNESS_ACCEPTANCE.md#当前实现进度核对) · [历史开发进度](archive/PROGRESS.md)

## SWE-bench Lite 评测与基准演进（M11–M12）

| 顺序 | 工作 | 状态与验收标准 |
| --- | --- | --- |
| 1 | 完成 23 题 `dev` 环境与 `full` 实跑 | **已完成**：已支持直接复用官方镜像、`scripts/swebench_eval_helper.py` 按仓库分批拉取/预取/防误删清理，以及 `-X utf8` 与 `evaluate --instance-id` 定向重评。全量 23 题均产出合法补丁（官方原始 **9/23 = 39.1%**，剔除 8 题上游镜像腐化后 **9/15 = 60.0%**，均摊 ¥0.20/题）；详细归因见 [SWEBenchLiteEval.md](SWEBenchLiteEval.md)。 |
| 2 | 落地本轮暴露的 2 项 P0 工程修复 | 修复 `edit_file` 对 Windows CRLF 文件的匹配兼容（并锁定预取 `core.autocrlf=false`），以及 `swebench.py` 缓存命中统计把 `model.failed` 误判为 `incomplete` 的问题；同时评估在无 `ruff` 容器中动态隐藏或降级 `run_linter`。 |
| 3 | 迁移至高信噪比基准 / 跨批次验收 | 鉴于 `dev` 集 34.8% 上游镜像腐化，完成 3 题冒烟验收后优先切入环境更干净、且已有 DeepSeek-V4.1-Flash-1017 公开基线的基准（如 Terminal-Bench 4.1 63 题无 GPU 版或 SWE-bench Pro V2 HARD-51），同步验证跨批次实验管理。 |
| 4 | 推进单变量消融与 `test` 300 题（按需） | 固定模型、题目、评分器与总预算，逐项对照导航、受控开发工具、多块/跨文件编辑、并发只读、校准上下文与只读探索委派，在工作台「实验记录」核对配对通过率、Token 费用、Prefix Cache 命中率与耗时。 |

实验设计、配对统计口径与命令见[评测体系与实验计划](EVALUATION.md#swe-bench-lite-实验设计与对照规划)，全量 23 题实测与归因见[dev 23 题评测报告](SWEBenchLiteEval.md)。

## 架构与工作台后续演进

- **线程历史轮次分叉（`N6-a`）**：在线程私有裸仓库为每轮运行记录不可变快照引用，支持从任意历史轮次派生独立线程与独立 worktree，同时保证删除原线程不影响子线程。
- **前端流式与状态架构优化**：在 SSE 接收层合并高频 `assistant.delta` 减少重渲染，将根组件状态拆分为独立 Hook，并支持大补丁 Diff 折叠与探索子 Agent 过程的折叠展示。
- **应用验证与交付闭环**：演练 IDE 应用补丁后回传验证证据、PostgreSQL 与状态卷的联合备份恢复，以及在隔离环境管理开发服务与页面验证证据。
- **扩展与生产部署**：评估 Web 运行时 MCP 接入的信任与生命周期边界，验收实际组织 OIDC 身份源、HTTPS 与跨平台部署。
