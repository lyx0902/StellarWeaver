# 后续路线图

当前交互与执行主链路已完成 CLI M0–M5、工作台 G0–G11、Harness M0–M10、**N 阶段（Batch 1–3）与上下文三项优化**，以及 **Harness Phase 1–3a、Web Compose 开关同步、Terminal-Bench 4.1 5 题 CPU 冒烟、严苛 `TrialLock`/用量溯源修复与 `completion_evidence_reminder` 4-trial A/B**（676+ 项离线回归全绿）。 [当前实现与验收](HARNESS_ACCEPTANCE.md#当前实现进度核对) · [历史开发进度](archive/PROGRESS.md)

## 评测基准已完成里程碑与下一步计划

| 阶段 / 优先级 | 任务内容 | 状态与验收口径 |
| --- | --- | --- |
| **已归档** | SWE-bench Lite `dev` 23 题全量收官 | **已收官存档**：完成 6 仓库分批 `full` 实跑与官方评分，23/23 产出合法补丁（原始 **9/23 = 39.1%**，剔除 8 题上游镜像腐化后 **9/15 = 60.0%**，均摊 ¥0.20/题）。因官方镜像依赖腐化与私有白盒测试过拟合，**不再推进 `test` 300 题或进一步消融**；完整归因与历史记录已归档至 [SWEBenchLiteEval.md](SWEBenchLiteEval.md)。 |
| **已完成** | Phase 1–3a 改造、Web 开关透传、TB 4.1 冒烟与遥测/复核优化 | **已完成**：落地 CRLF 兼容、`run_linter` 降级、`long_horizon` 512k/64k 预设（并已针对重题进一步放宽至 `3h / 2000 次 / 300M 输入 / 3M 输出`）、流式 `length` 续写与复读检测、Web Compose 7 项 `SW_WEB_*` 重部署；Harbor `0.25.0` 5 题冒烟 **2/5 (40.0%)**（WAL 实报缓存 **97.58%**、标准价 `$3.094`，决定跳过 Phase 3b）；完成 `verification_status` 语义与严苛 `TrialLock`/v2 逐字段溯源修复（`c1da3b4`）；`completion_evidence_reminder` 4-trial A/B 成功揪出 `vf2` 2 项漏网 Bug 修至 `60/60`（总缓存 `97.97%`）。详见 [TerminalBench4Eval.md](TerminalBench4Eval.md)。 |
| **下一步 · P0** | 执行 **Terminal-Bench 4.1 `fast-agent` 19 题公开标准子集**（+ 已有 4 题 = 23 题覆盖） | 使用冻结配置 [harbor-tb41-fast19.yaml](../deploy/harbor-tb41-fast19.yaml)（512k 窗口 + `3h / 2000 次 / 300M / 3M` + `completion_evidence_reminder: true` + `delete: true` 适配 7.5 GiB Docker 内存），在谷期窗口跑通 19 题 `pass@1`（单并发 10–13h / 双并发 5.5–7h，谷期预计 ¥42–¥55）。报告严格拆为「主栏 19 题标准子集 + 补充栏 4 题多容器/高难专项」（**不混分母**），同基座硬碰硬对标 Claude Code 与 Ante 0.3.5。逐题档案见 [TB4.1Subset23.md](TB4.1Subset23.md)。 |
| **下一步 · P1（按需）** | 19 题低成本专项调优与单变量 A/B 消融 | 针对 19 题实测暴露的高成本或可挽回失败模式，在同一 19 题标准子集上开展低成本单变量对照（重点评估长程 `bash` 输出截断阈值、上下文压缩点等），结合 `ctrf.json` 子测试得分与 Prefix Cache 曲线验证收益。 |
| **可选 · P2** | Terminal-Bench 4.1 63 题无 GPU 全量复核（按需） | 在 19 题子集方案定型且预算充裕时，可视需求扩展至 63 题无 GPU 全量跑榜（`pass@1` 预计谷期 ¥130–¥170）。 |

评测体系配置参数与命令行参考见 [EVALUATION.md](EVALUATION.md)，23 题档案与同模型标杆见 [TB4.1Subset23.md](TB4.1Subset23.md)。

## 架构与工作台后续演进

- **线程历史轮次分叉（`N6-a`）**：在线程私有裸仓库为每轮运行记录不可变快照引用，支持从任意历史轮次派生独立线程与独立 worktree，同时保证删除原线程不影响子线程。
- **前端流式与状态架构优化**：在 SSE 接收层合并高频 `assistant.delta` 减少重渲染，将根组件状态拆分为独立 Hook，并支持大补丁 Diff 折叠与探索子 Agent 过程的折叠展示。
- **应用验证与交付闭环**：演练 IDE 应用补丁后回传验证证据、PostgreSQL 与状态卷的联合备份恢复，以及在隔离环境管理开发服务与页面验证证据。
- **扩展与生产部署**：评估 Web 运行时 MCP 接入的信任与生命周期边界，验收实际组织 OIDC 身份源、HTTPS 与跨平台部署。
