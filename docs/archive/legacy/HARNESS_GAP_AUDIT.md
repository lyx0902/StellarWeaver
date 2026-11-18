# StellarWeaver Harness 源码核对与差距分析

> 历史记录。现行说明见[项目文档](../../README.md)。

核对日期为 2026-11-18。范围包括当前工作区的 CLI、Web API、Agent 主循环、工具执行、PostgreSQL、Compose 和 React 前端。对照产品为 Codex 桌面端、Claude Code Desktop 和 DeepSeek 官方开源 Harness（DSH）。

## 当前进度

StellarWeaver 已实现单 Agent 编码循环和可部署的本地工作台。最影响体验的三块差距是任务级隔离、Web 多轮会话，以及从修改到验证和审阅的完整工作流。

| 阶段 | 源码和已记录的验证 |
| --- | --- |
| CLI 核心与会话 | 有单 Agent 循环、严格工具参数、调用和时间预算、取消与恢复。CLI 可在同一会话继续工作，并支持回放、导出、`/diff` 与受限的 `/undo`。见 [Agent 循环](../../../src/stellarweaver/runtime/loop.py#L1045)、[CLI 会话](../../../src/stellarweaver/cli.py#L279) 和 [改动管理](../../../src/stellarweaver/changes/__init__.py#L416)。 |
| G0–G1 共享运行与只读工作台 | CLI 和 Web 共用 Agent 装配。CLI 使用 SQLite。Web 使用 PostgreSQL 保存运行与工具事件，通过 SSE 回看，刷新后可恢复同一条运行记录。见 [共享运行装配](../../../src/stellarweaver/runtime/service.py#L15) 和 [Web API](../../../src/stellarweaver/web/app.py#L114)。 |
| G2 审批和交付 | Web 能展示待审批 diff、逐项授权或取消、记录测试结果并下载 JSON 报告。测试绑定文件快照，后续编辑会让先前测试证据标为过期。已有 Edge 和临时 Git 仓库的修复验收记录。见 [审批](../../../src/stellarweaver/web/approval.py#L101)、[报告](../../../src/stellarweaver/web/delivery.py#L135) 和 [G2 指南](WEB_G2_QUICKSTART.md)。 |
| G3 队列、恢复和部署 | 运行与队列同事务写入。独立 worker 串行领取，领取归属会被核对。已领取任务中断后会保留工具记录，缺少结果的操作显示为未确认。Compose 可以启动 PostgreSQL、迁移、API、worker 和前端。重启、强制终止和备份恢复有既有验收记录。见 [入队](../../../src/stellarweaver/web/db.py#L124)、[worker](../../../src/stellarweaver/web/worker.py#L16) 和 [进度记录](../PROGRESS.md#L3)。 |
| G4–G5 | G4 规划的是任务级执行隔离。G5 规划的是正式身份、项目授权、配额和审计。当前已有数据库租户隔离与本机令牌入口。见 [分阶段规划](../GUI_PLATFORM_IMPLEMENTATION_PLAN.md#L91) 和 [Web 入口](../../../src/stellarweaver/web/__main__.py#L92)。 |

本次核对时，`8787` 工作台健康，使用 DeepSeek，按只读模式运行。修复闭环已实现，部署者可在登记的工作区启用。见 [Compose 启动指南](WEB_G3_COMPOSE.md#L40)。

## 与三个 Harness 的差距

| 能力 | 当前实现 | 待补能力与对照依据 |
| --- | --- | --- |
| 任务隔离与执行范围 | 文件工具检查工作区路径和写入前后的哈希。CLI 管理子进程生命周期。Web 的可审批测试命令固定为 `python -B -m unittest -q`。 | 缺少每任务独立工作区、操作系统级执行隔离、网络和凭据边界。也缺少按项目配置 pytest、npm test、构建和 lint 的执行入口。见 [命令执行](../../../src/stellarweaver/tools/commands.py#L1)、[Web 审批规则](../../../src/stellarweaver/web/approval.py#L105) 和 [Codex 沙箱](https://learn.chatgpt.com/docs/agent-approvals-security)。 |
| 会话续接与实时交互 | CLI 支持续聊、恢复和逐字输出。Web 每次提交生成新的会话和运行，SSE 提供持久事件和断线重连。 | Web 缺少原线程追问、运行中补充要求、从历史分叉，以及从核对过的状态继续执行。CLI 的逐字输出尚未接入工作台。见 [CLI 会话](../../../src/stellarweaver/cli.py#L279)、[Web 提交](../../../src/stellarweaver/web/db.py#L124) 和 [Codex App Server](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)。 |
| 并行任务与子 Agent | Compose 启动一个 worker，依次处理任务。Agent 也按顺序处理一轮中的工具调用。 | 缺少多个独立工作区并行执行、任务间的改动交接，以及子 Agent 委派、预算分配与结果汇总。见 [worker](../../../src/stellarweaver/web/worker.py#L40)、[工具循环](../../../src/stellarweaver/runtime/loop.py#L325)、[Codex Worktrees](https://learn.chatgpt.com/docs/environments/git-worktrees) 和 [Claude Desktop Worktrees](https://code.claude.com/docs/en/desktop#run-a-session-in-its-own-worktree)。 |
| 改动审阅、回退与交付 | Web 有审批 diff、测试结果和交付报告。CLI 可撤销有记录且通过哈希检查的内置编辑。 | 缺少逐行批注、按改动块接受或撤回、应用候选补丁、处理冲突及提交和 PR 工作流。CLI 也缺少按对话轮次选择文件或会话检查点的撤销入口。见 [CLI 撤销](../../../src/stellarweaver/changes/__init__.py#L416)、[Codex Git 审阅](https://learn.chatgpt.com/docs/environments/local-environment#use-built-in-git-tools) 和 [Claude 检查点](https://code.claude.com/docs/en/checkpointing)。 |
| 桌面开发体验 | React 页面提供任务、历史、审批、时间线和报告。 | 缺少项目文件树和编辑器、交互终端、开发服务器管理、浏览器预览，以及让 Agent 用 DOM、截图与日志验证前端改动的流程。独立的桌面安装包也尚未形成。见 [前端工作台](../../../frontend/src/main.tsx#L215) 和 [Claude 应用预览](https://code.claude.com/docs/en/desktop#preview-your-app)。 |
| 上下文、模型与扩展 | 有分目录 `AGENTS.md`、Python AST repo map、规则式上下文压缩与预算。CLI 支持 Skills、本地 stdio MCP 和可配置的 OpenAI 兼容接口。DeepSeek 通过 Chat Completions 调用，适配器显式关闭 thinking。 | 缺少跨语言符号索引、跨会话项目记忆、Web 端 Skills/MCP 配置、远程 MCP 与对外 Agent SDK。DSH 已公开插件与 profile 体系、stdio JSON-RPC SDK 和桌面发行方式。见 [上下文实现](../../../src/stellarweaver/context/manager.py#L133)、[MCP 客户端](../../../src/stellarweaver/extensions/mcp.py#L1)、[DeepSeek 适配器](../../../src/stellarweaver/providers/openai_compatible.py#L177)、[DSH 项目](https://github.com/deepseek-ai/deepseek-harness) 和 [DSH SDK 命令](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/cli-help.md#dsh-sdk)。 |
| 远程和团队使用 | 有数据库租户行级隔离、本机访问令牌、持久任务和单 worker。 | 缺少正式用户身份、团队角色、项目授权、配额与审计。云端或 SSH 执行、跨设备交接、定时任务也尚未形成。见 [Web 身份入口](../../../src/stellarweaver/web/__main__.py#L92) 和 [G5 规划](../GUI_PLATFORM_IMPLEMENTATION_PLAN.md#L100)。 |

## 建议的推进顺序

1. **先完成 G4 任务级隔离。** 两个任务从同一仓库出发，在各自的工作区修改并运行测试，各自导出 diff。用测试确认任务只能访问自己的文件，模型和数据库凭据留在执行环境之外。Git worktree 负责分离改动，执行环境负责约束权限。
2. **让 Web 会话可继续。** 用户可以在原线程追问、看流式输出、查看测试结果，并在中断后根据已有记录选择继续方式。未确认的副作用保留给用户核对。
3. **把验证和审阅接成工作流。** 在隔离区运行项目配置的测试或构建，支持逐块审阅、应用补丁和验证改动。优先完善这些日常操作，再考虑原生桌面打包。

## 评测证据与范围

仓库的真实 DeepSeek 评测是 20 个固定小任务各运行一次。初评为 **19/20**，修正一项评分规则后，用原运行结果重评为 **20/20**，没有新增模型调用。这个结果覆盖该小型题集。要量化 Harness 之间的工程效果差距，需要在相同模型、仓库、任务和预算下重复评测，记录完成率、回归率、审批次数、耗时和 token 用量。见 [原始评测口径](../../../evals/RESULTS.md#L7)。

本轮依据源码、已有验收记录、官方文档和当前工作台状态整理。测试套件与模型评测沿用此前记录。
