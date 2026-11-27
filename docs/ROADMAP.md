# 后续路线图

当前交互与执行主链路已完成 CLI M0–M5、工作台 G0–G11、Harness M0–M10、**N 阶段（Batch 1–3）与上下文三项优化**，以及 **Harness Phase 1–3a、Web Compose 开关同步、Terminal-Bench 4.1 5 题 CPU 冒烟、严苛 `TrialLock`/用量溯源修复与 `completion_evidence_reminder` 4-trial A/B**（676+ 项离线回归全绿）。 [当前实现与验收](HARNESS_ACCEPTANCE.md#当前实现进度核对) · [历史开发进度](archive/PROGRESS.md)

## 评测基准已完成里程碑与下一步计划

| 阶段 / 优先级 | 任务内容 | 状态与验收口径 |
| --- | --- | --- |
| **已归档** | SWE-bench Lite `dev` 23 题全量收官 | **已收官存档**：完成 6 仓库分批 `full` 实跑与官方评分，23/23 产出合法补丁（原始 **9/23 = 39.1%**，剔除 8 题上游镜像腐化后 **9/15 = 60.0%**，均摊 ¥0.20/题）。因官方镜像依赖腐化与私有白盒测试过拟合，**不再推进 `test` 300 题或进一步消融**；完整归因与历史记录已归档至 [SWEBenchLiteEval.md](SWEBenchLiteEval.md)。 |
| **已完成** | Phase 1–3a 改造、Web 开关透传、TB 4.1 冒烟与开跑前加固 | **已完成**：落地 CRLF 兼容、`run_linter` 降级、`long_horizon` 512k/64k 预设（已放宽至 `3h / 2000 次 / 300M 输入 / 3M 输出`）、流式容错、Web Compose 7 项开关与 5 题冒烟 **2/5 (40.0%)**（WAL 缓存 **97.58%**、标准 `$3.094`）；完成 `completion_evidence_reminder` 4-trial A/B（`vf2` 修至 `60/60`，缓存 **97.97%**）；补齐 **开跑前 Docker 内存预检（`preflight`/`run`）、作业级根锁分母校验（`absent`/`corrupt` 拦截与 `summarize`）、报错去噪归一化（含 `RepairController` `bash` 键修复）及只读 `PrefixCacheAuditor` 旁路审计**。详见 [TerminalBench4Eval.md](TerminalBench4.1Eval.md) 与 [EVALUATION.md](EVALUATION.md)。 |
| **下一步 · P0** | 执行 **Terminal-Bench 4.1 `fast-agent` 19 题公开标准子集**（+ 已有 4 题 = 23 题覆盖） | 本机 WSL2 内存已调至支持 `8 GiB` 单容器（实测 Docker 可用 `9.48 GB` / `9.71 GiB`），通过 `python -m stellarweaver.evals.terminal_bench run --config deploy/harbor-tb41-fast19.yaml --env-file .env` 在谷期窗口跑通 19 题 `pass@1`（单并发 `10–13h`，512k 窗口预计 ¥42–¥55）。报告严格拆为「主栏 19 题标准子集 + 补充栏 4 题多容器/高难专项」（**不混分母**），同基座对标 Claude Code 与 Ante 0.3.5。逐题档案见 [TB4.1Subset23.md](TB4.1Subset23.md)。 |
| **下一步 · P1（按需）** | 19 题低成本专项调优与单变量 A/B 消融 | 针对 19 题实测暴露的高成本或可挽回失败模式，在同一 19 题标准子集上开展低成本单变量对照（如上下文窗口 `512k` vs `184k–256k` 控费对照、长程 `bash` 输出截断阈值等），结合 `ctrf.json` 子测试得分与 Prefix Cache 曲线验证收益。 |
| **可选 · P2** | Terminal-Bench 4.1 63 题无 GPU 全量复核（按需） | 在 19 题子集方案定型且预算充裕时，可视需求扩展至 63 题无 GPU 全量跑榜（`pass@1` 预计谷期 ¥130–¥170）。 |

评测体系配置参数与命令行参考见 [EVALUATION.md](EVALUATION.md)，23 题档案与同模型标杆见 [TB4.1Subset23.md](TB4.1Subset23.md)。

## 评测完成后的延迟实现项（Post-Eval Backlog）

以下高价值架构灵感来自外部同类 Agent 实操对比（如 `RuoCun`），为避免评测期破坏稳定性或引入基准污染，**统一延后至 19 题基准实测完成后推进**：

- **`ArchiveRead`（压缩/截断原文按 ID 分页回查）**：借鉴确定性归档 + 正则 ID/会话作用域校验的分页读回机制，优先为 `swe-python` / Web 模式补齐「零 LLM 压缩省钱 + 长输出关键断言零丢失」闭环（`terminal-minimal` 保持单 `bash` 极简不变）。
- **`fork_agent`（继承父前缀的缓存共享子代理）**：重构 `ExplorationAgent` 及未来独立 `Reviewer` 子代理，采用深拷贝父消息前缀（修复未闭合 `tool_calls`、兼容 `reasoning_content`）并在尾部追加子任务提示的方案，使子代理首轮即可命中 ~99% Prefix Cache，破解冷启动 0% 缓存成本瓶颈。
- **托管后台 Shell 轮询与带证据校验的项目经验记忆（仅日常 Web/CLI 工作台）**：为长耗时构建/本地服务增加带终态收据的异步轮询工具，并探索显式开关下的受控经验记忆（每条绑定适用条件、测试证据与源文件指纹校验；**严格禁止在基准评测模式开启**）。

## 架构与工作台后续演进

- **线程历史轮次分叉（`N6-a`）**：在线程私有裸仓库为每轮运行记录不可变快照引用，支持从任意历史轮次派生独立线程与独立 worktree，同时保证删除原线程不影响子线程。
- **前端流式与状态架构优化**：在 SSE 接收层合并高频 `assistant.delta` 减少重渲染，将根组件状态拆分为独立 Hook，并支持大补丁 Diff 折叠与探索子 Agent 过程的折叠展示。
- **应用验证与交付闭环**：演练 IDE 应用补丁后回传验证证据、PostgreSQL 与状态卷的联合备份恢复，以及在隔离环境管理开发服务与页面验证证据。
- **扩展与生产部署**：评估 Web 运行时 MCP 接入的信任与生命周期边界，验收实际组织 OIDC 身份源、HTTPS 与跨平台部署。
