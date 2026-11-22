# StellarWeaver 项目文档

StellarWeaver 是一个本地 Coding Agent。它根据任务读取代码、提出并执行经授权的变更、运行项目检查并交付可审阅的结果。命令行 CLI 和浏览器工作台共用 Python Agent 循环，分别提供终端自动化与可交互的任务管理。

## 当前能力

| 能力 | CLI | Web 工作台 |
| --- | --- | --- |
| 模型与交互 | `chat` 交互、`exec` 单次运行、可见文本流式输出、JSONL；支持 DeepSeek 和离线 FakeModel。 | 按轮展示流式回复，保留任务时间线。同项目可在原线程追问。普通对话独立分组。 |
| 项目操作 | 枚举、读取和搜索文件，新建和精确编辑，执行命令，检查 Git 状态与差异。 | 在选定项目中运行只读或修复任务。侧栏按工作区组织会话，支持归档与恢复。 |
| 权限与预算 | 支持只读、询问和工作区编辑策略。校验工具参数、文件哈希、风险与运行预算。 | 可写项目默认 `auto`，可切换只读或修改。默认每账户每 UTC 周 1000 万 token，今日统计按北京时间显示。 |
| 测试和改动交付 | 使用 `/diff` 区分启动基线与会话增量。`/undo` 可撤销符合条件的内置修改。 | 隔离验证、按块审阅、复验和 IDE 交接。线程可撤销最近一次已记录的文件创建或编辑。 |
| 会话与恢复 | SQLite 会话记录，支持列表、回放、导出和恢复。 | PostgreSQL 持久队列及事件流。支持带文件与验证证据续接、查看未确认操作、导出脱敏 JSON 或离线回放 HTML。 |
| 上下文与扩展 | 默认 200k 窗口，支持历史压缩、分目录 `AGENTS.md`、Python repo map、Skills 和本地 stdio MCP。 | 默认 200k 并保存各轮窗口，支持自适应压缩。模型可自主加载技能，用户可固定版本或按项目授权技能。DeepSeek Flash 可调用 `ds_search`。 |
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
| [使用与部署](USAGE.md) | CLI 命令、模型配置、浏览器操作、Compose 与团队配置。 |
| [架构与调用链](ARCHITECTURE.md) | 模块职责、同步与异步执行、授权边界和中断恢复。 |
| [数据库](DATABASE.md) | PostgreSQL、SQLite、表关系、连接参数、迁移与恢复。 |
| [验证与实验](EVAL.md) | 功能检查、真实模型评测、复现入口和下一批实验。 |
| [工具安全、授权与评测](SECURITY.md) | 信任边界、L0–L3 风险分级、审批与 Hook、本地安全夹具。 |
| [Harness M0–M10 实施与验收](HARNESS_ACCEPTANCE.md) | 源码审计、评测契约、工具安全、修复流程、实验看板及各阶段证据。 |
| [后续路线图](ROADMAP.md) | SWE-bench Lite 实验、工作台交付和后续验收项。 |
| [评测协议](EVAL_PROTOCOL.md) · [SWE-bench Lite 计划](EVAL_PLAN.md) | 实验输入、候选与评分的来源、配对口径，以及开发集和正式集的实验设计。 |
| [开发进度](archive/PROGRESS.md) · [Harness 差距审计](archive/04_HARNESS_GAP_AUDIT.md) | 历史阶段交付和当时的外部工作流对照。 |

总方案的目标架构图用于说明里程碑及验收门槛。实施快照以[当前实现进度核对](HARNESS_ACCEPTANCE.md#当前实现进度核对)为准。 [总方案](StellarWeaver_Harness_Complete_Improvement_Plan_2026-11-21.md) · [归档索引](archive/README.md)
