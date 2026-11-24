# StellarWeaver 项目文档

StellarWeaver 是一个本地 Coding Agent。它根据任务读取代码、提出并执行经授权的变更、运行项目检查并交付可审阅的结果。命令行 CLI 和浏览器工作台共用 Python Agent 循环，分别提供终端自动化与可交互的任务管理。

## 当前能力

| 能力 | CLI | Web 工作台 |
| --- | --- | --- |
| 模型与交互 | `chat` 交互、`exec` 单次运行、可见文本流式输出、JSONL；支持 DeepSeek 和离线 FakeModel。 | 按轮展示流式回复，保留任务时间线。同项目可在原线程追问。普通对话独立分组。 |
| 项目操作与导航 | 枚举、读取、搜索、AST 符号与引用导航（含字节预过滤与源码缓存）、多处 `edit_file`、`apply_patch` 跨文件写回、只读并发、只读子 Agent 探索（`delegate_exploration`）、执行命令与 Git 差异。 | 在选定项目中运行只读或修复任务；侧栏按工作区组织会话，支持归档与恢复；与 CLI 共用全套导航、写回与探索工具（按开关启用）。 |
| 权限与预算 | 支持只读、询问和工作区编辑策略。校验工具参数、文件哈希、风险与运行预算；支持沙箱内测试命令作用域授权复用（`scoped_test_grants`）。 | 可写项目默认 `auto`，可切换只读或修改；支持同线程跨轮沙箱授权复用。默认每账户每 UTC 周 1 亿 token，今日统计按北京时间显示。 |
| 测试和改动交付 | 使用 `/diff` 区分启动基线与会话增量；写后 Python 语法检查（`post_edit_syntax_hook`）；`/undo` 支持单文件与 `apply_patch` 成组撤销。 | 隔离验证（支持 Worker→Broker 增量快照 `incremental_sandbox_snapshots`）、按块审阅、复验、IDE 交接，以及单文件/成组撤销。 |
| 会话与恢复 | SQLite 会话记录，支持列表、回放、导出和中断恢复（含 `apply_patch` 混合态检测）。 | PostgreSQL 持久队列及事件流。支持带文件与验证证据续接、查看未确认操作、导出脱敏 JSON 或离线回放 HTML。 |
| 上下文与扩展 | 默认 200k 窗口，支持实测校准预算、固定前缀压缩、最近回合原文保护、分目录 `AGENTS.md`、导入加权 Python RepoMap、Skills 和本地 stdio MCP。 | 默认 200k 并保存各轮窗口，共用上下文校准与前缀压缩管线。模型可自主加载技能，用户可固定版本或按项目授权技能。DeepSeek Flash 可调用 `ds_search`。 |
| 身份与部署 | 本机配置与当前操作系统用户权限。 | 本机 Compose 使用账号密码和会话 Cookie。可选团队配置支持 OIDC、成员角色和项目授权。 |

当前本机入口为 `http://127.0.0.1:8787/`，使用 DeepSeek。项目源目录只读挂载，线程在持久卷中的独立 Git worktree 内编辑。专用执行服务接收该线程的快照并启动验证容器。普通对话可直接从侧栏「新任务」开始。

## 一次修复的完整流程

选择 Git 工作区并发起修复。Agent 读取相关文件后，在隔离 worktree 编辑并验证。`auto` 自动执行范围内的普通编辑与登记的检查，「修改」在对应动作前展示审批卡。任务结束后，可查看工具过程、测试退出码和补丁。选中改动块后，工作台基于同一提交创建审阅工作区并重跑配置的检查。

终端可用离线演示复现一次失败与修复。演示使用临时 Git 仓库，实际测试退出码依次为 `1`、`0`。

```powershell
uv sync --locked
uv run --offline python -m stellarweaver.demo
```

## 阅读顺序

| 文档 | 内容 |
| --- | --- |
| [使用与部署](USAGE.md) | CLI 命令、模型配置、Harness 增强开关速查、浏览器操作、Compose 与团队配置。 |
| [架构与调用链](ARCHITECTURE.md) | 模块职责、主循环（并发读/探索/上下文管线）、异步执行、授权边界和中断恢复。 |
| [数据库](DATABASE.md) | PostgreSQL、SQLite、表关系、连接参数、迁移与恢复。 |
| [工具安全、授权与评测](SECURITY.md) | 信任边界、L0–L3 风险分级、作用域授权复用、审批与 Hook、本地安全夹具。 |
| [评测体系、验证记录与实验计划](EVALUATION.md) | 复现入口、统一评测协议、离线夹具与 20 题、SWE-bench Lite 批量评测与实验规划、阶段验收。 |
| [Harness 实施与验收](HARNESS_ACCEPTANCE.md) | M0–M10 与 N 阶段（Batch 1–3）及上下文优化的实现进度核对与验收证据。 |
| [后续路线图](ROADMAP.md) | 已完成能力概览、SWE-bench Lite 正式集实验与下一阶段重点。 |
| [历史归档索引](archive/README.md) | 历史阶段进度、原始设计方案（含 M0–M10 总方案与 N 阶段方案）及外部工作流差距审计。 |

实施快照以[当前实现进度核对](HARNESS_ACCEPTANCE.md#当前实现进度核对)为准；已完成的历史规划文档见 [M0–M10 总方案](archive/05_HARNESS_IMPROVEMENT_PLAN_2026-11-21.md) · [N 阶段方案](archive/06_HARNESS_N_STAGE_PLAN.md) · [归档索引](archive/README.md)。
