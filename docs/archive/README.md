# 历史实施方案与归档文档

本目录收录已完成的原始规划方案、阶段进度、差距审计和历史评测证据。现行操作与模块说明见[项目文档](../README.md)。

| 文档 | 范围 |
| --- | --- |
| [开发进度](PROGRESS.md) | M0–M5 的 CLI 基础、G0–G5 的工作台交付，以及后续工作台和实验增量。 |
| [Harness 差距审计](04_HARNESS_GAP_AUDIT.md) | 按当时源码和已有证据核对能力，比较外部 Harness 的对应工作流。 |
| [Harness 完整改进方案（M0–M10）](05_HARNESS_IMPROVEMENT_PLAN_2026-11-21.md) | 2026-11-21 制定的 M0–M10 目标架构、里程碑与验收门槛（已交付）。 |
| [Harness N 阶段改进方案（Batch 1–3）](06_HARNESS_N_STAGE_PLAN.md) | 围绕多处编辑、跨文件原子写回、并发读、增量快照、导航加速与子 Agent 探索的 N 阶段规划（已交付）。 |
| [评测优先安全实施路线图](StellarWeaver_Evaluation_First_Security_Implementation_Roadmap.md) | 早期以评测驱动安全边界落地的规划草案。 |
| [CLI 实现规范](01_CLI_AGENT_PLAN.md) | M0–M5 原始设计要求与 V1 最终验收清单。 |
| [V1 验收记录](01_CLI_AGENT_RES.md) | 早期 CLI 版本的逐项核对及证据范围。 |
| [GUI 实施方案](02_GUI_PLAN.md) | G0–G5 的立项方案、数据模型与分阶段验收条件。 |
| [SWE-bench Lite 原始实施方案](03_SWEBench_Lite_PLAN.md) | 原始阶段设计、接口草案、消融与工作台验收要求。 |
| [内部评测结果](00_Sample_Run.md) | 历史 20 题实测、规则修订和零模型重评分。 |

当前复现命令、统一协议、已有结果与 SWE-bench Lite 实验计划见[评测体系、验证记录与实验计划](../EVALUATION.md)；最新实现与验收汇总见[Harness 实施与验收](../HARNESS_ACCEPTANCE.md)。
